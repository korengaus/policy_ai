"""DESIGN-3B-1d Phase 1 — READ-ONLY impact of tightening has_genuine_official_support
to STRONG-only (label + box + the VERDICT-PATH TOPICGATE bypass).

Background (confirmed in code):
  * has_genuine_official_support = extract_primary_document_match(...) is not None
    OR (count of source_candidates with official_body_match > 0).
  * LOOPHOLE: official_evidence_resolution._resolve_source sets official_body_match=True
    for BOTH strong_official_direct_support AND medium_official_contextual_support,
    so the second disjunct counts MEDIUM (context-only, off-topic) matches → spurious
    "공식 근거 확인" (e.g. welfare claim ↔ 해양수산부 펀드).
  * The SAME variable (verification_card.py: _has_genuine_official_support) is threaded
    into the TOPICGATE gate as has_genuine_signal (the bypass) AND stored as
    source_reliability_summary["has_genuine_official_support"] (label/box). So tightening
    it is VERDICT-ADJACENT: rows that flip genuine→not LOSE the bypass and may be
    flagged by the topic-mismatch gate → the existing main.py clamp lowers the score.

STRICT predicate measured here (strong-only):
    extract_primary_document_match present  OR  any source_candidate with
    official_body_match==True AND classification == strong_official_direct_support.

This probe measures, over recent rows:
  PART B (label): current-genuine vs strict-genuine vs FLIPS, with per-flip detail so
    the flips can be eyeballed as the spurious ones (and 258-type kept).
  PART C (verdict): for each flip, approximate (read-only, srs-mirror) whether losing
    the bypass would let the topic-mismatch gate FLAG the driving doc → clamp → a
    score change on a NEW analysis. Counts the verdict-path movement.

IMPORTANT: this simulates over STORED rows = "would change on re-analysis", NOT a
change to stored values (no backfill). The gate truly runs on the unstored Lane-A
official_evidence_results item; the probe approximates via the srs mirror
(top_official_detail_title) — a CONSERVATIVE estimate, consistent with topicgate_probe.

STRICTLY SELECT / READ-ONLY. No writes/DDL. Never prints DATABASE_URL or secrets.

Run in the Render Worker Shell AFTER the deploy commit:
    git log --oneline -1
    PYTHONPATH=. python scripts/genuine_tighten_probe.py --limit 400 --show 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

PRIMARY_MARKER_FIELDS = ("policy_briefing_news_item_id", "national_law_mst")
STRONG = "strong_official_direct_support"
PRIMARY_MIN_SCORE = 75

# --- material-overlap heuristic (mirrors scripts/topicgate_probe.py intent) ----
INSTITUTIONS = [
    "금융위원회", "금융위", "금융감독원", "금감원", "국토교통부", "국토부", "기획재정부",
    "기재부", "한국은행", "국세청", "관세청", "경찰청", "검찰", "법무부", "고용노동부",
    "노동부", "행정안전부", "보건복지부", "복지부", "교육부", "산업통상자원부", "중소벤처기업부",
    "중기부", "공정거래위원회", "공정위", "환경부", "여성가족부", "농림축산식품부",
    "해양수산부", "해수부", "국무회의", "국회", "주택도시보증공사", "신용보증기금",
    "기술보증기금", "예금보험공사", "주택금융공사", "산업은행", "수출입은행", "기업은행",
    "IBK", "두나무", "카카오", "네이버", "토스", "코레일", "한국전력", "한전",
]
_LOCALGOV_RE = re.compile(r"[가-힣]{1,3}(?:특별자치도|특별자치시|광역시|특별시|시청|군청|구청|도청|군)")
GENERIC = frozenset({
    "지원", "금융", "정책", "확대", "강화", "추진", "검토", "발표", "운영", "시행",
    "사업", "제도", "관리", "대책", "방안", "계획", "활성화", "개선", "혜택", "보조",
    "협력", "회의", "간담회", "안내", "서비스", "행사", "발언", "참석", "개최", "관련",
    "주요", "뉴스", "기사", "정부", "이번", "해당", "공식", "출처", "내용", "결과",
    "현황", "공고", "공지", "보도자료", "설명자료", "브리핑", "확인", "필요", "추가",
})
MATERIAL_SHORT = frozenset({"전세", "월세", "주택", "대출", "금리", "규제", "세금",
                            "양도세", "청년", "신혼", "출산", "귀농", "노인", "복지", "DSR"})


def _institutions(text):
    t = text or ""
    found = {n for n in INSTITUTIONS if n in t}
    for m in _LOCALGOV_RE.finditer(t):
        found.add(m.group(0))
    return found


def _spec_tokens(text):
    out = set()
    for tok in re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}", text or ""):
        if tok in MATERIAL_SHORT:
            out.add(tok); continue
        if tok in GENERIC:
            continue
        out.add(tok)
    for a in MATERIAL_SHORT:
        if a in (text or ""):
            out.add(a)
    return out


def _has_material_overlap(claim_text, doc_title):
    if _institutions(claim_text) & _institutions(doc_title):
        return True
    if _spec_tokens(claim_text) & _spec_tokens(doc_title):
        return True
    return False


def _get_engine():
    import sqlalchemy as sa
    raw = os.environ.get("DATABASE_URL")
    if raw:
        url = raw.replace("postgresql+psycopg://", "postgresql://")
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        try:
            engine = sa.create_engine(url)
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return engine
        except Exception as exc:  # noqa: BLE001
            print(f"NOTE: direct DATABASE_URL engine unavailable ({type(exc).__name__}); "
                  "falling back to postgres_storage.get_engine().", file=sys.stderr)
    try:
        import postgres_storage
        return postgres_storage.get_engine()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no engine available ({type(exc).__name__}).", file=sys.stderr)
        return None


def _parse_json(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _trunc(v, n=80):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _candidate_score(c):
    return max(_num(c.get("official_evidence_score")), _num(c.get("official_final_direct_match_score")),
               _num(c.get("official_body_match_score")), _num(c.get("score")))


def _current_genuine(srs, debug):
    flag = srs.get("has_genuine_official_support")
    if flag is None:
        flag = _num(debug.get("official_body_matches")) > 0
    return bool(flag)


def _strict_genuine(cands):
    """strong-only: a strong primary-marker match OR any STRONG official_body_match."""
    for c in cands or []:
        if not isinstance(c, dict):
            continue
        clf = str(c.get("official_evidence_classification") or c.get("official_direct_match_classification") or "")
        has_marker = any(str(c.get(f) or "").strip() for f in PRIMARY_MARKER_FIELDS)
        if c.get("official_body_match") and clf == STRONG:
            if has_marker and _candidate_score(c) >= PRIMARY_MIN_SCORE:
                return True
            if not has_marker:        # strong body match without a marker still counts as strong
                return True
    return False


def _claim_blob(claim_text, title, normalized):
    parts = [str(claim_text or ""), str(title or "")]
    for nc in normalized or []:
        if isinstance(nc, dict):
            for k in ("actor", "target", "object", "action"):
                parts.append(str(nc.get(k) or ""))
    return " ".join(p for p in parts if p)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="genuine_tighten_probe")
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--show", type=int, default=20)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import sqlalchemy as sa
    engine = _get_engine()
    if engine is None:
        print("ERROR: Postgres engine unavailable.", file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 4000))
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, claim_text, title, policy_confidence_score, verification_strength, "
            "normalized_claims, source_reliability_summary, source_candidates, debug_summary "
            "FROM analysis_results ORDER BY id DESC LIMIT :n"
        ), {"n": limit}).mappings().all()

    n = len(rows)
    cur_genuine = strict_genuine_ct = 0
    flips = []          # genuine now, not under strict
    kept = []           # genuine under strict (the real ones)
    for r in rows:
        srs = _parse_json(r["source_reliability_summary"]) or {}
        srs = srs if isinstance(srs, dict) else {}
        debug = _parse_json(r["debug_summary"]) or {}
        debug = debug if isinstance(debug, dict) else {}
        cands = _parse_json(r["source_candidates"]) or []
        normalized = _parse_json(r["normalized_claims"]) or []

        cur = _current_genuine(srs, debug)
        strict = _strict_genuine(cands)
        if cur:
            cur_genuine += 1
        if strict:
            strict_genuine_ct += 1

        rec = {
            "id": r["id"],
            "claim": _trunc(r["claim_text"], 80),
            "title": _trunc(r["title"], 60),
            "doc": _trunc(srs.get("top_official_detail_title"), 70),
            "clf": srs.get("official_direct_match_classification"),
            "dscore": srs.get("official_direct_match_score"),
            "score": r["policy_confidence_score"],
            "vstrength": r["verification_strength"],
            "current_mismatch": bool(srs.get("official_mismatch")),
            "claim_blob": _claim_blob(r["claim_text"], r["title"], normalized),
        }
        if cur and not strict:
            flips.append(rec)
        elif strict:
            kept.append(rec)

    # ---- PART C: verdict-path impact of the flips ----
    verdict_changes = []
    for f in flips:
        overlap = _has_material_overlap(f["claim_blob"], f["doc"])
        would_flag = not overlap                 # non-housing material gate fires on no overlap
        would_change = would_flag and not f["current_mismatch"] and _num(f["score"]) > 20
        f["overlap"] = overlap
        f["would_flag_without_bypass"] = would_flag
        f["verdict_would_change"] = would_change
        if would_change:
            verdict_changes.append(f)

    def pct(x):
        return f"{x}/{n} ({x/n*100:.1f}%)" if n else "0/0"

    print("=" * 92)
    print(f"DESIGN-3B-1d genuine-tighten impact — scanned {n} rows")
    print("=" * 92)
    print(f"  current has_genuine_official_support: {pct(cur_genuine)}")
    print(f"  STRICT (strong-only) genuine:         {pct(strict_genuine_ct)}")
    print(f"  FLIPS (genuine -> NOT under strict):  {len(flips)}")
    print(f"  kept-genuine (strong, real):          {len(kept)}")

    print("\n" + "=" * 92)
    print("PART B — LABEL FLIPS (eyeball: are these the spurious off-topic ones?)")
    print("  label change per flip: '공식 근거 확인' -> '관련 공식자료 있음 (직접 검증 아님)'")
    print("=" * 92)
    for f in flips[:args.show]:
        print(f"\n  id={f['id']} clf={f['clf']} dmatch_score={f['dscore']} stored_score={f['score']}")
        print(f"     CLAIM   : {f['claim']}")
        print(f"     MATCHED : {f['doc']}")

    print("\n" + "=" * 92)
    print("PART C — VERDICT-PATH IMPACT (would the score move if the bypass is lost?)")
    print("  (read-only 'would change on re-analysis', via srs-mirror approximation — NOT a")
    print("   stored-value change; conservative vs the real Lane-A gate.)")
    print("=" * 92)
    print(f"  flips total:                                  {len(flips)}")
    print(f"  flips currently NOT mismatched (bypass active): {sum(1 for f in flips if not f['current_mismatch'])}")
    print(f"  flips with NO material overlap (gate would flag): {sum(1 for f in flips if f['would_flag_without_bypass'])}")
    print(f"  ** VERDICT SCORE WOULD CHANGE (flag & not-clamped & score>20): {len(verdict_changes)} **")
    for f in verdict_changes[:args.show]:
        print(f"\n  id={f['id']} stored_score={f['score']} vstrength={f['vstrength']} -> would clamp <=20/none")
        print(f"     CLAIM   : {f['claim']}")
        print(f"     MATCHED : {f['doc']}  (clf={f['clf']})")

    print("\n" + "=" * 92)
    print("PART D — READOUT")
    print("=" * 92)
    print(f"  * label-only honesty win: {len(flips)} spurious '공식 근거 확인' downgraded.")
    print(f"  * VERDICT score movement if tightened EVERYWHERE (unified): {len(verdict_changes)} rows.")
    print("    - ZERO  -> tighten genuine everywhere freely (label+box+bypass), no score moves.")
    print("    - >0    -> choose: (a) UNIFIED tighten = honest label+score, but verdict-path")
    print("              (needs Export + TOPICGATE discipline), OR (b) SEPARATE strict predicate")
    print("              for LABEL+BOX only, leave the TOPICGATE bypass on the current looser")
    print("              genuine -> verdict scores untouched (but re-creates honest-label/")
    print("              inflated-score split-brain for those rows).")
    print("  Eyeball the PART B flips = the spurious ones (해수부 etc.) and confirm kept = real (258).")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
