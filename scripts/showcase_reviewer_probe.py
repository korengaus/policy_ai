# SHOWCASE-REVIEWER Phase 1 — READ-ONLY reviewer probe (pin-OUT, SELECT-only).
#
# Measures whether an LLM reviewer over the EXPOSED surface would have caught
# the defects we already know about, and at what false-positive cost. The
# reviewer is limited to THREE questions — genre / surface residue / internal
# consistency — and is explicitly forbidden from any truth-or-falsity
# assessment (that is the one thing this product does not sell). The prompt is
# printed verbatim so the constraint can be verified by eye.
#
# SAMPLE: every row of every archived weekly_reports top-10 (newest row per
# week_start, the same row /api/weekly-report serves), rendered as weekly.html
# displayed them. PLUS a 10-label slice of the NEWEST brainmap_graph row:
# ★the child-homicide row and the 2017 인구동향 row appear on the brainmap
# surface, NOT in any archived weekly top-10 (verified against all 40 archived
# rows on 2026-07-29) — without that slice, recall on 2 of the 4 known-defect
# classes is unmeasurable. Cluster labels ARE reader-visible (brainmap.html).
#
# Runs the reviewer TWICE over the identical sample (claude-sonnet-5) so
# run-to-run determinism is measurable. Prints: sample size, the prompt
# verbatim, recall against the 4 known defects, false positives (up to 3
# quoted), disagreement count, drift check, tokens and dollars.
#
# PARSE-ROBUSTNESS (post-first-run fix): the first Worker run died inside
# json.loads — on claude-sonnet-5 adaptive thinking is ON by default and
# max_tokens caps thinking + text TOGETHER, so one 50-item response can come
# back with an empty or truncated text block. Now: items go in BATCHES of 10
# (identical chunking both runs, so determinism stays valid), fences and
# preamble are stripped before parsing, and ★any parse failure prints the
# response structure (stop_reason, block types, first 400 chars) and exits 2 —
# a parse failure can never look like a clean run or an empty result set.
#
# THINKING-OFF (post-second-run fix): the diagnostic then measured the cause
# exactly — batch 1/5 returned stop_reason=max_tokens with ONE thinking block
# and an empty text head: thinking exhausted the whole 4000-token cap before
# a single verdict was written. Fix: thinking={"type": "disabled"} on every
# call — this is a formatted-extraction task (three closed questions per item,
# JSON out), not a reasoning task, so thinking is pure overhead here. A
# thinking BUDGET is not an option on this model (budget_tokens returns 400 on
# claude-sonnet-5; adaptive is the only on-mode). With thinking off, the full
# 4000 max_tokens is text budget (~700 needed per 10-item batch). ★The run
# SMOKE-CHECKS batch 1 first — stop_reason + parsed verdict count printed
# before the remaining nine calls are made, so a repeat failure costs 1 call.
#
# Joe runs once in the Render Worker Shell (both env vars already exist there):
#     PYTHONPATH=. python scripts/showcase_reviewer_probe.py
#
# SAFETY: SELECT-only (weekly_reports.payload_json, brainmap_graph.graph_json).
# No writes, no stored field touched — truth_claim stays False,
# operator_review_required stays True, verdict_label untouched. Credentials
# from the environment ONLY; neither is ever printed. pin-OUT scripts/* —
# zero log.* call sites, the 331/16 pins do not move. Output ≤ ~40 lines.

import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

MODEL = "claude-sonnet-5"
# $/MTok: standard, and intro pricing valid through 2026-08-31.
PRICE_STD = (3.0, 15.0)
PRICE_INTRO = (2.0, 10.0)
# 10 items per request keeps each verdict JSON small enough to be reliable;
# with thinking disabled the whole 4000 is text budget — ~3300 tokens of
# headroom above the ~700-token verdict payload.
BATCH_SIZE = 10
MAX_TOKENS_PER_BATCH = 4000
THINKING = {"type": "disabled"}  # extraction task; budget_tokens 400s on Sonnet 5

# ---------------------------------------------------------------- prompt ----
# THE PROMPT IS THE DESIGN. Printed verbatim below so the three-question
# constraint (and the absence of truth vocabulary) can be verified directly.
SYSTEM_PROMPT = """당신은 정책 뉴스 확산 리포트의 화면 표시 심사관이다. 각 항목은 독자에게 실제로 보이는 문자열(제목·배지·날짜)이다.
허용된 질문은 정확히 셋뿐이다:
(a) genre — 정책 관련 보도가 아닌 장르인가? (형사사건, 사건사고, 시세/단순시황, 부고, 광고/홍보, 단순 행사 안내)
(b) surface — 독자에게 기계 부스러기가 보이는가? (영어 코드 조각, enum 값, 원시 타임스탬프, [대괄호] 형식 접두어, 제목 끝의 매체명 꼬리, 깨진 인코딩, 문장 중간에서 잘린 텍스트)
(c) consistency — 라벨이 자기 숫자와 모순되거나, 날짜가 제시된 기간·기준 시점과 말이 되지 않는가? (예: 기준 시점과 동떨어진 연도)
절대 금지: 주장의 사실 여부, 진위, 신뢰도에 대한 판단은 어떤 표현으로도 하지 않는다. 내용이 의심스러워 보여도 그것은 flag 사유가 아니다.
확신이 없으면 flag하지 않는다. 지시된 JSON으로만 답한다."""

USER_TEMPLATE = """다음 {n}개 항목을 심사하라.

{rows}

각 항목마다 {{"id","genre","surface","consistency","note"}}를 내라. note는 flag한 경우에만 한 문장, 아니면 빈 문자열."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "genre": {"type": "boolean"},
                    "surface": {"type": "boolean"},
                    "consistency": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["id", "genre", "surface", "consistency", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

# ------------------------------------------------------------ ground truth --
# Matched by CONTENT, not by id — cluster ids churn between graph builds.
TAIL_RE = re.compile(r"-\s*(뉴시스|울산종합일보)\s*$")
GROUND_TRUTH = (
    ("genre/형사사건", "genre", lambda t: "살해" in t),
    ("consistency/2017년 인구동향", "consistency",
     lambda t: "2017" in t and "인구동향" in t),
    ("surface/[금융 HOT 뉴스] 접두어", "surface",
     lambda t: t.startswith("[금융 HOT 뉴스]")),
    ("surface/매체명 꼬리(뉴시스·울산종합일보)", "surface",
     lambda t: bool(TAIL_RE.search(t))),
)
# Truth-drift detector over the reviewer's own notes (design failure if hit).
DRIFT_RE = re.compile(r"사실|허위|진위|거짓|검증")

# A mechanically-clean screen for brainmap FILLER labels only (the believed-
# fine rows false positives are measured against) — never applied to weekly
# rows and never a substitute for the reviewer's own judgement.
ANY_TAIL_RE = re.compile(r"-\s*\S+(뉴스|일보|신문|타임스|Pn)\s*$")


def day(value):
    return (value or "")[:10]


def render_weekly_item(week_start, week_end, item):
    """One weekly top-10 row, as weekly.html rendered it (title + badge row)."""
    parts = ["%s개 매체" % item.get("outlet_count")]
    wf, wl = day(item.get("window_first_at")), day(item.get("window_last_at"))
    if wf and wl:
        parts.append("확산 기간 %s → %s" % (wf, wl))
    if item.get("window_member_count"):
        parts.append("이번 주 보도 %s건" % item["window_member_count"])
    near = item.get("near_anchor_outlet_count")
    if isinstance(near, (int, float)):
        parts.append("문구 거의 동일: %d개 매체" % max(0, int(near) - 1))
    return {
        "id": "W%s#%s" % (week_start, item.get("rank")),
        "screen": "주간 리포트 %s ~ %s" % (week_start, week_end),
        "title": item.get("title") or "",
        "badges": " · ".join(parts),
    }


def load_sample(conn):
    items = []
    with conn.cursor() as cur:
        cur.execute("SELECT week_start, week_end, payload_json "
                    "FROM weekly_reports ORDER BY id DESC")
        seen_weeks = set()
        for week_start, week_end, payload_json in cur.fetchall():
            if week_start in seen_weeks:  # newest row per week wins (API rule)
                continue
            seen_weeks.add(week_start)
            payload = json.loads(payload_json)
            for item in payload.get("top") or []:
                items.append(render_weekly_item(week_start, week_end, item))
        cur.execute("SELECT graph_json FROM brainmap_graph "
                    "ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    weekly_count = len(items)
    if row:
        graph = json.loads(row[0])
        stamp = day(graph.get("generated_at")) or "최신"
        clusters = graph.get("clusters") or []
        titles = {i["title"] for i in items}

        def add(cluster):
            items.append({
                "id": "B%s" % cluster.get("cluster_id"),
                "screen": "뉴스 연결지도 (기준 시점 %s)" % stamp,
                "title": cluster.get("label_title") or "",
                "badges": cluster.get("size_label")
                or "%s개 매체" % cluster.get("outlet_count"),
            })

        # The two known-defect labels first (matched by content), then clean
        # fillers by outlet_count so the defects sit among believed-fine rows.
        wanted = [c for c in clusters
                  if any(m((c.get("label_title") or "")) for _, _, m in GROUND_TRUTH)
                  and (c.get("label_title") or "") not in titles]
        for cluster in wanted:
            add(cluster)
        fillers = sorted(clusters, key=lambda c: -(c.get("outlet_count") or 0))
        for cluster in fillers:
            if len(items) >= weekly_count + 10:
                break
            title = cluster.get("label_title") or ""
            if (not title or title in titles or cluster in wanted
                    or title.startswith("[") or ANY_TAIL_RE.search(title)):
                continue
            add(cluster)
    return items


def _extract_json(text):
    """Parse the outermost JSON object; tolerate ``` fences and preamble."""
    trimmed = text.strip()
    if trimmed.startswith("```"):
        trimmed = re.sub(r"^```[a-zA-Z]*\s*", "", trimmed)
        trimmed = re.sub(r"\s*```$", "", trimmed)
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start != -1 and end > start:
        return json.loads(trimmed[start:end + 1])
    return json.loads(trimmed)  # empty/no-brace body — raises with real error


def _dump_response(resp, text, tag, exc):
    """★Never let a parse failure look like a clean run. Show what the API
    actually returned (structure first, raw head second), then exit 2."""
    kinds = ",".join(b.type for b in resp.content) or "NO CONTENT BLOCKS"
    print("PARSE FAILURE on %s: %s" % (tag, exc))
    print("  stop_reason=%s | %d content blocks [%s]"
          % (resp.stop_reason, len(resp.content), kinds))
    if resp.stop_reason == "max_tokens":
        print("  -> response TRUNCATED at max_tokens=%d (thinking+text share "
              "the cap) — raise MAX_TOKENS_PER_BATCH" % MAX_TOKENS_PER_BATCH)
    print("  raw text head: %r" % text[:400])
    raise SystemExit(2)


def run_reviewer(client, items, smoke=False):
    """One full pass: identical batches of BATCH_SIZE, verdicts merged.
    smoke=True prints batch 1's stop_reason + verdict count the moment it
    parses — the one-call verification gate before the other nine calls."""
    verdicts, tokens_in, tokens_out = {}, 0, 0
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    fmt = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    for index, batch in enumerate(batches, start=1):
        rows = "\n".join(
            "- id=%s | 화면=%s\n  제목: %s\n  배지: %s"
            % (i["id"], i["screen"], i["title"], i["badges"]) for i in batch)
        kwargs = dict(
            model=MODEL, max_tokens=MAX_TOKENS_PER_BATCH, thinking=THINKING,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": USER_TEMPLATE.format(n=len(batch), rows=rows)}])
        try:
            resp = client.messages.create(output_config=fmt, **kwargs)
        except TypeError:  # older SDK without the typed kwarg — same wire param
            resp = client.messages.create(extra_body={"output_config": fmt},
                                          **kwargs)
        tag = "batch %d/%d" % (index, len(batches))
        if resp.stop_reason == "refusal":
            print("REVIEWER REQUEST REFUSED on %s (stop_reason=refusal)" % tag)
            raise SystemExit(2)
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            batch_verdicts = _extract_json(text)["verdicts"]
        except (ValueError, KeyError, TypeError) as exc:
            _dump_response(resp, text, tag, exc)
        if smoke and index == 1:
            print("SMOKE (thinking off): %s stop_reason=%s, %d/%d verdicts "
                  "parsed — continuing" % (tag, resp.stop_reason,
                                           len(batch_verdicts), len(batch)))
        for verdict in batch_verdicts:
            verdicts[verdict["id"]] = verdict
        tokens_in += resp.usage.input_tokens
        tokens_out += resp.usage.output_tokens
    return verdicts, (tokens_in, tokens_out), len(batches)


def flagged(verdict):
    return verdict and (verdict["genre"] or verdict["surface"]
                        or verdict["consistency"])


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — run in the Render Worker Shell.")
        return 0

    import anthropic
    import psycopg

    url = (os.environ["DATABASE_URL"]
           .replace("postgresql+psycopg://", "postgresql://")
           .replace("postgresql+psycopg2://", "postgresql://"))
    with psycopg.connect(url) as conn:
        items = load_sample(conn)

    client = anthropic.Anthropic()
    run1, usage1, batches = run_reviewer(client, items, smoke=True)
    run2, usage2, _ = run_reviewer(client, items)

    print("SHOWCASE-REVIEWER PROBE — SELECT-only, %d items "
          "(weekly top-10 rows + brainmap labels), model %s, "
          "2 passes × %d batches of ≤%d"
          % (len(items), MODEL, batches, BATCH_SIZE))
    print("REVIEWER PROMPT (verbatim):")
    print(SYSTEM_PROMPT)
    missing = [i["id"] for i in items if i["id"] not in run1 or i["id"] not in run2]
    if missing:
        print("WARNING: %d items got no verdict (%s…) — treat MISSED classes "
              "below with suspicion" % (len(missing), ", ".join(missing[:5])))

    gt_ids = set()
    print("RECALL vs known defects (run 1):")
    caught = 0
    for label, dimension, matcher in GROUND_TRUTH:
        matches = [i for i in items if matcher(i["title"])]
        gt_ids.update(i["id"] for i in matches)
        if not matches:
            print("  %s: NOT-IN-SAMPLE" % label)
            continue
        hits = [i for i in matches if (run1.get(i["id"]) or {}).get(dimension)]
        caught += bool(hits)
        print("  %s: %s (%d/%d rows)"
              % (label, "CAUGHT" if hits else "MISSED", len(hits), len(matches)))
    print("  recall: %d/4 defect classes" % caught)

    false_pos = [v for iid, v in run1.items()
                 if iid not in gt_ids and flagged(v)]
    healthy = len(items) - len(gt_ids)
    print("FALSE POSITIVES (run 1): %d of %d believed-fine rows"
          % (len(false_pos), healthy))
    by_id = {i["id"]: i for i in items}
    for v in false_pos[:3]:
        title = by_id.get(v["id"], {}).get("title", "")[:46]
        print("  %s \"%s\" g=%s s=%s c=%s: %s"
              % (v["id"], title, v["genre"], v["surface"], v["consistency"],
                 v["note"][:60]))

    changed = [iid for iid in run1 if not run2.get(iid) or
               (run1[iid]["genre"], run1[iid]["surface"], run1[iid]["consistency"])
               != (run2[iid]["genre"], run2[iid]["surface"], run2[iid]["consistency"])]
    print("DETERMINISM: %d/%d rows changed verdict between the two runs%s"
          % (len(changed), len(items),
             " (" + ", ".join(changed[:6]) + ")" if changed else ""))

    drift = [v for v in list(run1.values()) + list(run2.values())
             if v["note"] and DRIFT_RE.search(v["note"])]
    print("TRUTH-DRIFT CHECK: %d notes used truth/verification vocabulary%s"
          % (len(drift), " — DESIGN FAILURE, quote: \"%s\"" % drift[0]["note"][:60]
                if drift else ""))

    tokens_in = usage1[0] + usage2[0]
    tokens_out = usage1[1] + usage2[1]
    std = tokens_in / 1e6 * PRICE_STD[0] + tokens_out / 1e6 * PRICE_STD[1]
    intro = tokens_in / 1e6 * PRICE_INTRO[0] + tokens_out / 1e6 * PRICE_INTRO[1]
    per_pass = std / 2.0
    weekly = per_pass * (10.0 / max(1, len(items)))  # ≈10 new rows/week, 1 pass
    print("COST: %d in + %d out tokens for BOTH passes = $%.4f std ($%.4f intro)"
          % (tokens_in, tokens_out, std, intro))
    print("  per 50-item pass ≈ $%.4f · projected weekly (≈10 new rows, "
          "1 pass) ≈ $%.4f" % (per_pass, weekly))
    return 0


if __name__ == "__main__":
    sys.exit(main())
