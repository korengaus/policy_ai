# FADED-CLAIMS Slice 1 — operator-run detection generator: find clusters that
# SPREAD WIDELY then went SILENT (no member published for N+ days) and write a
# ranked CANDIDATE shortlist for the semi-auto review flow (S2). The rule does
# the finding; the operator does the final "did it fade, or did it conclude?"
# judgment — candidates are NEVER public by themselves (status='pending').
#
# USAGE (operator, LOCAL or Worker Shell — DATABASE_URL at the external
# Postgres; USE_POSTGRES_WRITE=true required only for the real run):
#   python scripts/generate_faded_candidates.py --dry-run            # ranked print, NO write
#   python scripts/generate_faded_candidates.py --dry-run --min-outlets 7 --min-silence-days 30
#   python scripts/generate_faded_candidates.py                      # upsert candidates
#   python scripts/generate_faded_candidates.py --selftest           # offline check
#
# SAFETY:
#   * Writes ONLY the self-created faded_claim_candidates table (CREATE TABLE
#     IF NOT EXISTS — the brainmap_graph/weekly_reports precedent; no Alembic,
#     postgres_storage.py untouched). Table materializes on the first real run.
#   * VERDICT-FREE: reads brainmap_graph.graph_json + analysis_results
#     (id, title, published_at, original_url) ONLY. No verdict/score column is
#     ever selected; detection = spread + dates + title keywords.
#   * ★UPSERT PRESERVES OPERATOR WORK: re-runs match rows by cluster_stable_id
#     and NEVER overwrite an existing status ('approved'/'dismissed') or
#     reviewed_at — only the measured numbers (outlet_count, last_at,
#     silence_days, score, generated_at) refresh. New clusters -> 'pending'.
#   * HONESTY: a candidate row asserts ONLY that observed coverage stopped —
#     never that the claim is false or the policy failed/was abandoned. The
#     public copy (S3) carries that framing; nothing here is public.
#   * Fail-closed env guards; --dry-run needs only DATABASE_URL. No numpy
#     (reads the already-built graph JSON).

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Outlet identity — REUSED, never re-derived (import, don't copy). Fallback
# only: the graph's stored cluster.outlet_count (F1A) is the primary number
# (it pooled duplicate-text rows at build time, which member rows alone
# undercount); the helper recomputes from member original_urls when a
# pre-F1A graph row lacks the field.
from build_brainmap_graph import normalize_outlet_host  # noqa: E402

DEFAULT_MIN_OUTLETS = 5
DEFAULT_MIN_SILENCE_DAYS = 21
DEFAULT_TOP_N = 25
# Forward-looking markers: a title implying follow-up coverage was expected.
# SCORE BOOST only — never a hard filter (a wide-then-silent cluster without
# a marker still deserves operator eyes). Overridable via --markers.
DEFAULT_MARKERS = ("발표", "예정", "추진", "계획", "검토", "도입", "시행")
MARKER_SCORE_BOOST = 1.25

SELECT_NEWEST_GRAPH_SQL = (
    "SELECT id, generated_at, graph_json FROM brainmap_graph "
    "ORDER BY id DESC LIMIT 1"
)
# Display/date/outlet fields ONLY — deliberately no verdict/score column.
# (claim_text feeds the Slice-4a topicality judge prompt; a display field.)
SELECT_ROWS_SQL = (
    "SELECT id, title, published_at, original_url, claim_text "
    "FROM analysis_results"
)

CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS faded_claim_candidates ("
    "id SERIAL PRIMARY KEY, "
    "cluster_stable_id TEXT UNIQUE, "
    "representative_analysis_id INTEGER, "
    "title TEXT, "
    "outlet_count INTEGER, "
    "first_at TEXT, "
    "last_at TEXT, "
    "silence_days INTEGER, "
    "marker_hit BOOLEAN, "
    "score REAL, "
    "status TEXT DEFAULT 'pending', "
    "reviewed_at TEXT, "
    "generated_at TEXT, "
    "ai_recommendation TEXT, "
    "ai_reason TEXT, "
    "ai_confidence REAL, "
    "ai_judged_at TEXT)"
)
# Slice 4a — the live table predates the ai_* columns; idempotent additive
# ALTERs bring it up to the def above (script-owned DDL precedent).
ADD_AI_COLUMNS_SQL = (
    "ALTER TABLE faded_claim_candidates ADD COLUMN IF NOT EXISTS ai_recommendation TEXT",
    "ALTER TABLE faded_claim_candidates ADD COLUMN IF NOT EXISTS ai_reason TEXT",
    "ALTER TABLE faded_claim_candidates ADD COLUMN IF NOT EXISTS ai_confidence REAL",
    "ALTER TABLE faded_claim_candidates ADD COLUMN IF NOT EXISTS ai_judged_at TEXT",
)
SELECT_EXISTING_SQL = (
    "SELECT status, reviewed_at FROM faded_claim_candidates "
    "WHERE cluster_stable_id = %s"
)
INSERT_SQL = (
    "INSERT INTO faded_claim_candidates "
    "(cluster_stable_id, representative_analysis_id, title, outlet_count, "
    "first_at, last_at, silence_days, marker_hit, score, status, "
    "reviewed_at, generated_at, "
    "ai_recommendation, ai_reason, ai_confidence, ai_judged_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
# Refresh MEASURED fields only — status/reviewed_at are deliberately absent,
# and so are the ai_* fields: a HUMAN-JUDGED row freezes what the AI said
# when the human decided (audit trail).
UPDATE_SQL = (
    "UPDATE faded_claim_candidates SET "
    "representative_analysis_id = %s, title = %s, outlet_count = %s, "
    "first_at = %s, last_at = %s, silence_days = %s, marker_hit = %s, "
    "score = %s, generated_at = %s "
    "WHERE cluster_stable_id = %s"
)
# Pending rows additionally refresh the AI recommendation fields.
UPDATE_PENDING_SQL = (
    "UPDATE faded_claim_candidates SET "
    "representative_analysis_id = %s, title = %s, outlet_count = %s, "
    "first_at = %s, last_at = %s, silence_days = %s, marker_hit = %s, "
    "score = %s, generated_at = %s, "
    "ai_recommendation = %s, ai_reason = %s, ai_confidence = %s, "
    "ai_judged_at = %s "
    "WHERE cluster_stable_id = %s"
)


def marker_hit(title: str, markers=DEFAULT_MARKERS) -> bool:
    return any(marker in (title or "") for marker in markers)


# ---------------------------------------------------------------------------
# Slice 4a — AI judgment layer (AI RECOMMENDS, human confirms; NEVER
# auto-publish). One tool-free Sonnet call per candidate classifies the EVENT
# SHAPE: forward-looking promise/plan with no visible follow-up (-> recommend
# approve) vs a concluded one-time event (-> recommend dismiss). TOPICALITY
# ONLY — this never judges truth: no verdict column is read, and ai_reason is
# guarded against truth vocabulary (build_brainmap_graph guard concept).
# Infra mirrors hot_topics._call_anthropic_pick (lazy Anthropic import,
# tool-free, fully decoupled from the verdict-path judge — never run_judge /
# LLMRequest), with the same record_llm_call cost logging. Fail-soft: any
# error -> ai_* stay None and the candidate still lists (the human gate
# never depends on the AI).
# ---------------------------------------------------------------------------
_JUDGE_MODEL_DEFAULT = "claude-sonnet-4-6"
_JUDGE_MAX_TOKENS = 256
# ai_reason must never carry truth vocabulary — the recommendation is about
# event shape, not veracity.
AI_REASON_FORBIDDEN = ("검증", "사실", "거짓", "확인됨")


def _build_judge_prompt(title: str, claim_text: str) -> str:
    claim = (claim_text or "").strip()[:200]
    claim_line = f"주장 요약: {claim}\n" if claim else ""
    return (
        "당신은 한국 정책 뉴스의 '보도 흐름'을 분류하는 분석가입니다.\n"
        f"기사 제목: {title}\n{claim_line}\n"
        "이 사안은 여러 매체가 보도한 뒤 후속 보도가 관찰되지 않았습니다. "
        "다음 두 유형 중 어느 쪽인지 판단하세요.\n"
        "A) 미래를 예고한 약속·계획·발표(도입 예정, 추진 검토 등)인데 후속 소식이 "
        "없는 경우 → recommendation을 \"approve\"로.\n"
        "B) 그 자체로 종결된 일회성 사건(협약 체결 완료, 시상식, 조사 완료, 단순 "
        "통계 발표 등)이라 후속이 원래 없는 경우 → recommendation을 \"dismiss\"로.\n"
        "주의: 사안의 진위 여부는 판단 대상이 아닙니다. reason에는 보도 흐름의 "
        "유형만 짧게 쓰고, 진위에 대한 표현은 쓰지 마세요.\n"
        "출력 형식: 다른 설명 없이 JSON 객체만. "
        '예: {"recommendation":"dismiss","confidence":0.9,"reason":"협약 체결이 완료된 일회성 사건"}'
    )


def _extract_json_object(text: str):
    """Robustly extract a JSON object from the model's text (fenced / bare /
    embedded in prose). Returns dict or None."""
    if not text:
        return None
    candidate = str(text).strip()
    if candidate.startswith("```"):
        import re
        candidate = re.sub(r"^```[a-zA-Z0-9]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    import re
    match = re.search(r"\{.*\}", str(text), re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return None


def reason_vocab_ok(reason: str) -> bool:
    return not any(word in (reason or "") for word in AI_REASON_FORBIDDEN)


def _call_anthropic_judge(prompt: str, model: str):
    """Tool-free Anthropic call (hot_topics.py:270-285 pattern). May raise;
    the caller is fail-soft. Isolated so tests can monkeypatch it."""
    from anthropic import Anthropic  # lazy import

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return client.messages.create(
        model=model,
        max_tokens=_JUDGE_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )


def judge_candidate(candidate: dict, claim_text: str, model: str):
    """One candidate -> {ai_recommendation, ai_reason, ai_confidence} or None
    on ANY failure (missing key, network, bad JSON, invalid values, or a
    truth-vocabulary reason — the guard refuses rather than stores)."""
    try:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return None
        prompt = _build_judge_prompt(candidate.get("title") or "", claim_text)
        import time as _time
        start = _time.time()
        message = _call_anthropic_judge(prompt, model)
        latency_ms = int((_time.time() - start) * 1000)
        try:
            from llm_observability import estimate_cost_usd, record_llm_call
            usage = getattr(message, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
            record_llm_call(
                caller="faded_judge", model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                estimated_cost_usd=estimate_cost_usd(model, input_tokens, output_tokens),
                latency_ms=latency_ms, success=True, provider="anthropic",
            )
        except Exception:
            pass  # cost logging is best-effort, never blocks the judgment
        parts = []
        for block in getattr(message, "content", None) or []:
            if str(getattr(block, "type", "") or "") == "text":
                parts.append(str(getattr(block, "text", "") or ""))
        parsed = _extract_json_object("\n".join(parts))
        if not parsed:
            return None
        recommendation = str(parsed.get("recommendation") or "").strip().lower()
        if recommendation not in ("approve", "dismiss"):
            return None
        reason = str(parsed.get("reason") or "").strip()[:300]
        if not reason or not reason_vocab_ok(reason):
            return None
        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))
        return {
            "ai_recommendation": recommendation,
            "ai_reason": reason,
            "ai_confidence": confidence,
        }
    except Exception:  # noqa: BLE001 — fail-soft; the human gate never depends on AI
        return None


def build_candidates(graph, rows_by_id, today, min_outlets=DEFAULT_MIN_OUTLETS,
                     min_silence_days=DEFAULT_MIN_SILENCE_DAYS,
                     markers=DEFAULT_MARKERS, top_n=DEFAULT_TOP_N):
    """Pure compute: graph JSON + {id: (title, published_at, original_url)} ->
    ranked candidate dicts. A cluster qualifies iff outlet_count >= min_outlets
    AND its NEWEST member publish date is >= min_silence_days old. Clusters
    with zero dated members are skipped (silence unmeasurable). Score =
    outlet_count * log(silence_days), * MARKER_SCORE_BOOST on a forward-looking
    title marker (boost only — never a gate)."""
    members_by_cluster: dict = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        node_id = node.get("id")
        if cid is None or node_id is None:
            continue
        members_by_cluster.setdefault(cid, []).append(node_id)

    candidates = []
    for cluster in graph.get("clusters") or []:
        cid = cluster.get("cluster_id")
        member_ids = members_by_cluster.get(cid) or []
        if cid is None or not member_ids:
            continue
        # Outlet count: stored (F1A, dup-row-pooled) first; recompute from
        # member original_urls only when an old graph row lacks it.
        outlet_count = cluster.get("outlet_count")
        if not isinstance(outlet_count, int) or outlet_count <= 0:
            hosts = set()
            for mid in member_ids:
                row = rows_by_id.get(mid)
                host = normalize_outlet_host(row[2] if row else "")
                if host:
                    hosts.add(host)
            outlet_count = len(hosts)
        if outlet_count < min_outlets:
            continue

        dated = sorted(
            value for value in (
                (rows_by_id.get(mid) or (None, None, None))[1]
                for mid in member_ids
            ) if value
        )
        if not dated:
            continue
        last_at = dated[-1]
        try:
            silence_days = (today - date.fromisoformat(last_at[:10])).days
        except ValueError:
            continue
        if silence_days < min_silence_days:
            continue

        label_title = cluster.get("label_title") or ""
        representative_id = min(member_ids)
        for mid in sorted(member_ids):
            row = rows_by_id.get(mid)
            if label_title and row and row[0] == label_title:
                representative_id = mid
                break
        hit = marker_hit(label_title, markers)
        score = outlet_count * math.log(max(silence_days, 2))
        if hit:
            score *= MARKER_SCORE_BOOST
        candidates.append({
            "cluster_stable_id": cluster.get("stable_id"),
            "representative_analysis_id": representative_id,
            "title": label_title,
            "outlet_count": outlet_count,
            "first_at": dated[0],
            "last_at": last_at,
            "silence_days": silence_days,
            "marker_hit": hit,
            "score": round(score, 3),
        })
    candidates.sort(key=lambda c: (-c["score"], c["representative_analysis_id"]))
    top = candidates[:top_n]
    for rank, candidate in enumerate(top, start=1):
        candidate["rank"] = rank
    return top


def plan_upsert(existing, candidate):
    """Pure: decide the row this candidate should end up as. ``existing`` is
    None (new cluster) or {"status", "reviewed_at"} from the current table.
    ★Operator work is NEVER lost: an existing status/reviewed_at is preserved
    verbatim; only the measured numbers refresh. Slice 4a: ``refresh_ai`` is
    True only for new/still-pending rows — a HUMAN-JUDGED row freezes its
    ai_* fields as the audit of what the AI said when the human decided."""
    if existing is None:
        return {"action": "insert", "status": "pending", "reviewed_at": None,
                "refresh_ai": True}
    status = existing.get("status") or "pending"
    return {
        "action": "update",
        "status": status,
        "reviewed_at": existing.get("reviewed_at"),
        "refresh_ai": status == "pending",
    }


def print_shortlist(candidates):
    if not candidates:
        print("[faded] no candidates matched the thresholds.")
        return
    print("[faded] rank | score | outlets | silence | marker | last_at    | id    | title")
    for candidate in candidates:
        print("  #%-3d %8.2f %7d %8dd %7s  %s  id=%-5s %s"
              % (candidate["rank"], candidate["score"],
                 candidate["outlet_count"], candidate["silence_days"],
                 "Y" if candidate["marker_hit"] else "-",
                 (candidate["last_at"] or "")[:10],
                 candidate["representative_analysis_id"],
                 (candidate["title"] or "")[:70]))
        if candidate.get("ai_recommendation"):
            print("        AI 추천: %s — %s"
                  % ("승인" if candidate["ai_recommendation"] == "approve" else "기각",
                     (candidate.get("ai_reason") or "")[:80]))


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic graph, fixed today. No DB, no network.
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    print("=== GENERATE-FADED-CANDIDATES --selftest (offline; no DB) ===")
    today = date(2026, 7, 11)
    graph = {
        "nodes": [
            {"id": 1, "cluster_id": 0}, {"id": 2, "cluster_id": 0},
            {"id": 3, "cluster_id": 0},
            {"id": 4, "cluster_id": 1}, {"id": 5, "cluster_id": 1},
            {"id": 6, "cluster_id": 2}, {"id": 7, "cluster_id": 2},
            {"id": 8, "cluster_id": 3}, {"id": 9, "cluster_id": 3},
            {"id": 10, "cluster_id": 4}, {"id": 11, "cluster_id": 4},
            {"id": 12, "cluster_id": 5}, {"id": 13, "cluster_id": 5},
        ],
        "clusters": [
            # A: wide + long-silent + marker -> kept, boosted.
            {"cluster_id": 0, "stable_id": "aaa", "outlet_count": 8,
             "label_title": "청년 지원금 도입 검토"},
            # B: wide but RECENT (5d) -> excluded by silence.
            {"cluster_id": 1, "stable_id": "bbb", "outlet_count": 9,
             "label_title": "전세 대출 발표"},
            # C: long-silent but narrow (3 outlets) -> excluded by spread.
            {"cluster_id": 2, "stable_id": "ccc", "outlet_count": 3,
             "label_title": "복지 계획 발표"},
            # D: wide + longest-silent, NO marker -> kept (marker never gates).
            {"cluster_id": 3, "stable_id": "ddd", "outlet_count": 6,
             "label_title": "전세 대출 급증"},
            # E: all members undated -> skipped (silence unmeasurable).
            {"cluster_id": 4, "stable_id": "eee", "outlet_count": 7,
             "label_title": "보험 제도"},
            # F: NO stored outlet_count -> fallback distinct-host recompute (5).
            {"cluster_id": 5, "stable_id": "fff",
             "label_title": "소상공인 지원 추진"},
        ],
    }
    rows = {
        1: ("청년 지원금 도입 검토", "2026-06-01T00:00:00+00:00", "https://a.kr/1"),
        2: ("기타", "2026-05-20T00:00:00+00:00", "https://b.kr/2"),
        3: ("기타2", None, "https://c.kr/3"),
        4: ("전세 대출 발표", "2026-07-06T00:00:00+00:00", "https://a.kr/4"),
        5: ("기타3", "2026-07-01T00:00:00+00:00", "https://b.kr/5"),
        6: ("복지 계획 발표", "2026-05-01T00:00:00+00:00", "https://a.kr/6"),
        7: ("기타4", "2026-04-01T00:00:00+00:00", "https://b.kr/7"),
        8: ("전세 대출 급증", "2026-05-12T00:00:00+00:00", "https://a.kr/8"),
        9: ("기타5", "2026-04-20T00:00:00+00:00", "https://b.kr/9"),
        10: ("보험 제도", None, "https://a.kr/10"),
        11: ("기타6", None, "https://b.kr/11"),
        12: ("소상공인 지원 추진", "2026-06-05T00:00:00+00:00", "https://m.one.kr/x"),
        13: ("기타7", "2026-06-01T00:00:00+00:00", "https://two.kr/y"),
    }
    # Give cluster F five distinct hosts across its 2 members' urls? Distinct
    # hosts come from member rows only — extend member 13's host set via more
    # members is overkill; instead lower the threshold for the F check below.
    shortlist = build_candidates(graph, rows, today, min_outlets=5,
                                 min_silence_days=21, top_n=10)
    by_sid = {c["cluster_stable_id"]: c for c in shortlist}

    a_ok = ("aaa" in by_sid and "ddd" in by_sid
            and "bbb" not in by_sid and "ccc" not in by_sid
            and "eee" not in by_sid)
    print("  [%s] (a) filter: silence>=21 & outlets>=5 kept; recent/narrow/"
          "undated excluded" % ("ok" if a_ok else "xx"))
    # A: 8*log(40)*1.25 ~ 36.9 ; D: 6*log(60) ~ 24.6 -> A first.
    b_ok = ([c["cluster_stable_id"] for c in shortlist[:2]] == ["aaa", "ddd"]
            and by_sid["aaa"]["marker_hit"] is True
            and by_sid["ddd"]["marker_hit"] is False)
    print("  [%s] (b) score ranking (marker boosts, never gates)"
          % ("ok" if b_ok else "xx"))
    c_ok = (by_sid["aaa"]["silence_days"] == 40
            and by_sid["aaa"]["last_at"] == "2026-06-01T00:00:00+00:00"
            and by_sid["aaa"]["first_at"] == "2026-05-20T00:00:00+00:00"
            and by_sid["aaa"]["representative_analysis_id"] == 1)
    print("  [%s] (c) dates/silence/representative correct" % ("ok" if c_ok else "xx"))
    # Fallback outlet computation: rerun with min_outlets=2 -> F qualifies via
    # 2 distinct recomputed hosts (one.kr + two.kr; m. stripped by the helper).
    fallback = build_candidates(graph, rows, today, min_outlets=2,
                                min_silence_days=21, top_n=10)
    f_row = next((c for c in fallback if c["cluster_stable_id"] == "fff"), None)
    d_ok = f_row is not None and f_row["outlet_count"] == 2
    print("  [%s] (d) missing stored outlet_count -> distinct-host fallback "
          "(normalize_outlet_host reused)" % ("ok" if d_ok else "xx"))
    # ★Upsert preservation: approved/dismissed + reviewed_at survive a re-run.
    kept = plan_upsert({"status": "approved", "reviewed_at": "2026-07-01T00:00:00+00:00"},
                       by_sid["aaa"])
    fresh = plan_upsert(None, by_sid["ddd"])
    dismissed = plan_upsert({"status": "dismissed", "reviewed_at": "2026-07-02T00:00:00+00:00"},
                            by_sid["aaa"])
    pending = plan_upsert({"status": "pending", "reviewed_at": None}, by_sid["aaa"])
    e_ok = (kept == {"action": "update", "status": "approved",
                     "reviewed_at": "2026-07-01T00:00:00+00:00",
                     "refresh_ai": False}
            and fresh == {"action": "insert", "status": "pending",
                          "reviewed_at": None, "refresh_ai": True}
            and dismissed["status"] == "dismissed"
            and dismissed["refresh_ai"] is False
            and pending["refresh_ai"] is True)
    print("  [%s] (e) UPSERT preserves operator status/reviewed_at; ai_* refresh "
          "only while pending" % ("ok" if e_ok else "xx"))
    # (g) Slice 4a — judge plumbing (offline): JSON extraction, value
    # validation, and the truth-vocabulary guard on ai_reason.
    parsed = _extract_json_object(
        '```json\n{"recommendation":"dismiss","confidence":0.9,'
        '"reason":"협약 체결이 완료된 일회성 사건"}\n```')
    g_ok = (parsed and parsed["recommendation"] == "dismiss"
            and reason_vocab_ok("협약 체결이 완료된 일회성 사건")
            and not reason_vocab_ok("사실이 아닌 주장")
            and not reason_vocab_ok("검증되지 않은 발표")
            and _extract_json_object("no json here") is None)
    print("  [%s] (g) judge JSON parsing + truth-vocab guard" % ("ok" if g_ok else "xx"))
    blob = json.dumps(shortlist, ensure_ascii=False)
    f_ok = ("verdict" not in blob and "confidence" not in blob
            and "truth" not in blob)
    print("  [%s] (f) candidate payload is verdict-free" % ("ok" if f_ok else "xx"))

    ok = all([a_ok, b_ok, c_ok, d_ok, e_ok, f_ok, g_ok])
    print()
    print("SELFTEST: %s" % ("PASS (filter + ranking + dates + outlet fallback "
                            "+ upsert-preserve + verdict-free + judge "
                            "plumbing)" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_faded_candidates",
        description="Find clusters that spread widely then went silent and "
                    "upsert a ranked candidate shortlist (status='pending') "
                    "for the semi-auto review flow.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graph; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the ranked shortlist; NO CREATE TABLE, NO write.")
    parser.add_argument("--min-outlets", type=int, default=DEFAULT_MIN_OUTLETS,
                        help="Minimum distinct-outlet spread (default %d)."
                             % DEFAULT_MIN_OUTLETS)
    parser.add_argument("--min-silence-days", type=int,
                        default=DEFAULT_MIN_SILENCE_DAYS,
                        help="Minimum days since the newest member publish "
                             "date (default %d)." % DEFAULT_MIN_SILENCE_DAYS)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help="Candidates to keep (default %d)." % DEFAULT_TOP_N)
    parser.add_argument("--markers", default=",".join(DEFAULT_MARKERS),
                        help="Comma-separated forward-looking title markers "
                             "(score boost only, never a gate).")
    parser.add_argument("--no-judge", dest="judge", action="store_false",
                        help="Skip the per-candidate AI recommendation calls "
                             "(default ON; ~25 short Sonnet calls per run).")
    parser.set_defaults(judge=True)
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    markers = tuple(part.strip() for part in (args.markers or "").split(",")
                    if part.strip()) or DEFAULT_MARKERS

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0
    if not args.dry_run and os.environ.get("USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it "
              "true, or use --dry-run.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    generated_at = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date()
    print("GENERATE-FADED-CANDIDATES — min_outlets=%d min_silence=%dd top_n=%d%s"
          % (args.min_outlets, args.min_silence_days, args.top_n,
             " (DRY-RUN)" if args.dry_run else ""))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_NEWEST_GRAPH_SQL)
            graph_row = cur.fetchone()
        if not graph_row:
            print("[faded] no brainmap_graph row — run "
                  "scripts/build_brainmap_graph.py first.")
            return 1
        graph_ref, _graph_generated_at, graph_json = graph_row
        try:
            graph = json.loads(graph_json)
        except (TypeError, ValueError):
            print("[faded] newest brainmap_graph row holds invalid JSON — aborting.")
            return 1
        with conn.cursor() as cur:
            cur.execute(SELECT_ROWS_SQL)
            rows_by_id = {row[0]: tuple(row[1:]) for row in cur.fetchall()}

        shortlist = build_candidates(
            graph, rows_by_id, today,
            min_outlets=args.min_outlets,
            min_silence_days=args.min_silence_days,
            markers=markers, top_n=args.top_n,
        )

        # Slice 4a — AI recommendation per candidate (fail-soft; the human
        # gate never depends on it). Runs in dry-run too so the operator can
        # see the recommendations before any write; skip with --no-judge.
        if args.judge and shortlist:
            judge_model = (os.environ.get("ANTHROPIC_MODEL", "").strip()
                           or _JUDGE_MODEL_DEFAULT)
            judged = 0
            for candidate in shortlist:
                row = rows_by_id.get(candidate["representative_analysis_id"])
                claim_text = row[3] if row and len(row) > 3 else ""
                verdict = judge_candidate(candidate, claim_text or "", judge_model)
                if verdict:
                    candidate.update(verdict)
                    judged += 1
            print("[faded] AI judge: %d/%d candidates judged (model=%s)"
                  % (judged, len(shortlist), judge_model))

        print_shortlist(shortlist)
        print("[faded] graph ref=%s | %d candidate(s)" % (graph_ref, len(shortlist)))

        if args.dry_run:
            print("[faded] DRY-RUN — no CREATE TABLE, no write.")
            return 0

        ai_judged_at = generated_at
        inserted = updated = 0
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            for ddl in ADD_AI_COLUMNS_SQL:
                cur.execute(ddl)
            for candidate in shortlist:
                cur.execute(SELECT_EXISTING_SQL,
                            (candidate["cluster_stable_id"],))
                row = cur.fetchone()
                existing = ({"status": row[0], "reviewed_at": row[1]}
                            if row else None)
                plan = plan_upsert(existing, candidate)
                has_ai = bool(candidate.get("ai_recommendation"))
                if plan["action"] == "insert":
                    cur.execute(INSERT_SQL, (
                        candidate["cluster_stable_id"],
                        candidate["representative_analysis_id"],
                        candidate["title"], candidate["outlet_count"],
                        candidate["first_at"], candidate["last_at"],
                        candidate["silence_days"], candidate["marker_hit"],
                        candidate["score"], plan["status"],
                        plan["reviewed_at"], generated_at,
                        candidate.get("ai_recommendation"),
                        candidate.get("ai_reason"),
                        candidate.get("ai_confidence"),
                        ai_judged_at if has_ai else None,
                    ))
                    inserted += 1
                elif plan["refresh_ai"]:
                    # Still pending — refresh measured fields AND the AI
                    # recommendation; status/reviewed_at untouched.
                    cur.execute(UPDATE_PENDING_SQL, (
                        candidate["representative_analysis_id"],
                        candidate["title"], candidate["outlet_count"],
                        candidate["first_at"], candidate["last_at"],
                        candidate["silence_days"], candidate["marker_hit"],
                        candidate["score"], generated_at,
                        candidate.get("ai_recommendation"),
                        candidate.get("ai_reason"),
                        candidate.get("ai_confidence"),
                        ai_judged_at if has_ai else None,
                        candidate["cluster_stable_id"],
                    ))
                    updated += 1
                else:
                    # Human-judged — measured fields ONLY; status/reviewed_at
                    # AND ai_* frozen (audit of what the AI said at decision
                    # time) by never appearing in UPDATE_SQL.
                    cur.execute(UPDATE_SQL, (
                        candidate["representative_analysis_id"],
                        candidate["title"], candidate["outlet_count"],
                        candidate["first_at"], candidate["last_at"],
                        candidate["silence_days"], candidate["marker_hit"],
                        candidate["score"], generated_at,
                        candidate["cluster_stable_id"],
                    ))
                    updated += 1
        conn.commit()
        print("[faded] wrote candidates: %d inserted (pending), %d refreshed "
              "(status preserved)" % (inserted, updated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
