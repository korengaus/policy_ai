# SHOWCASE-REVIEWER Phase 2 — CARD-DETAIL reviewer probe (pin-OUT, SELECT-only).
#
# The title layer passed (recall 4/4, drift 0, ~$0.013/wk). This probe measures
# the layer that matters: the card DETAIL, where every defect only a human ever
# caught actually lived. ★Row 13977 (a 2021년 11월 statistics release judged in
# a 2026 window, its official documents five years apart from the claim) is the
# real test — it passed every machine check and was found by eye. Row 13700
# carried 30+ irrelevant official documents. If the reviewer cannot see what
# only a person saw, this layer is not worth building.
#
# ★WHAT IS FED (the design decision): NOT the raw /history record — that shows
# scores, matched words and internal diagnostics no reader sees, and feeding it
# truncated a previous session's attempt. Instead the sample rows are rendered
# through the REAL frontend/scripts/main.js chain by REUSING the committed
# scripts/card_render_audit.js (its renderRow + pinned helpers, never
# reimplemented): an embedded Node driver vm-executes the audit with a trapped
# process.exit, then calls renderRow per row and emits the reader-visible text
# of every public card section (tags stripped, per-section 3,500-char cap with
# an explicit […N자 생략] marker; the 공식 문서 후보 count survives any cut).
# Node is required — the same requirement the C8 render-scan gate already
# imposes on this environment.
#
# AS-JUDGED CONDITION: today's chain stamps the post-13977 caveat chip
# ("공식 본문 불일치 가능성") onto candidate rows. For row 13977 ONLY, lines
# equal to that stamp are dropped from the fed text so the reviewer sees the
# card as it stood when a human caught it. Disclosed in the output.
#
# SAMPLE: this week's weekly top-10 representative rows (13700 is one of them)
# + the first 6 distinct representatives from older archived weeks + row 13977
# forced in — every one reachable by a reader from the exposed surface.
# Reviewer: claude-sonnet-5, thinking disabled (extraction task; Phase 1
# measured thinking exhausting the cap), 3 cards per request, TWO passes over
# identical batches so determinism is measurable, smoke-check after call 1,
# parse failure prints response structure and exits 2 (never a clean-looking
# empty run).
#
# Joe runs once in the Render Worker Shell:
#     PYTHONPATH=. python scripts/showcase_reviewer_card_probe.py
#
# SAFETY: SELECT-only (weekly_reports.payload_json, analysis_results render
# columns). Temp-file dump for the Node driver only (the C8 precedent) — no DB
# write, no stored field touched: truth_claim stays False,
# operator_review_required stays True, verdict_label untouched. Credentials
# from the environment ONLY, never printed. pin-OUT scripts/* — zero log.*
# call sites, the 331/16 pins do not move. Output ≤ ~40 lines.

import json
import os
import re
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

MODEL = "claude-sonnet-5"
PRICE_STD = (3.0, 15.0)    # $/MTok in, out
PRICE_INTRO = (2.0, 10.0)  # through 2026-08-31
BATCH_SIZE = 3             # detail cards are ~3K tokens each
MAX_TOKENS_PER_BATCH = 2000
THINKING = {"type": "disabled"}  # extraction task; budget_tokens 400s on Sonnet 5
MUST_INCLUDE = (13977, 13700)
STAMP = "공식 본문 불일치 가능성"  # post-13977 caveat chip, stripped from 13977 only

# ---------------------------------------------------------------- prompt ----
# THE PROMPT IS THE DESIGN — printed verbatim. The consistency-vs-truth
# distinction is stated with the operator's own example pair.
SYSTEM_PROMPT = """당신은 정책 뉴스 검증 카드의 화면 표시 심사관이다. 각 항목은 카드 상세 화면에서 독자에게 실제로 보이는 문자열 전체다(섹션 제목은 [ ]로 표시, […N자 생략]은 길이 제한 표시일 뿐 결함이 아니다).
허용된 질문은 정확히 셋뿐이다:
(a) genre — 이 카드의 주장이 정책 관련 보도가 아닌 장르인가? (형사사건, 사건사고, 시세, 부고, 광고, 단순 행사 안내)
(b) surface — 독자에게 기계 부스러기가 보이는가? (영어 코드 조각, enum 값, 원시 타임스탬프, 리터럴 이스케이프, HTML이 글자로 노출, 깨진 인코딩, 단어 중간에서 잘린 문장)
(c) consistency — 화면에 보이는 것끼리 서로 모순되는가? (주장을 뒷받침한다고 제시된 공식 문서의 연도·시점이 주장과 동떨어짐, 라벨이 자기 숫자와 어긋남, 기간이 말이 안 됨, 제시된 문서들이 주장 내용과 무관함)
구분 예시 — "이 공식 문서는 주장과 연도가 다르다"는 consistency 관찰이므로 허용. "이 주장은 거짓 같다", "이 출처는 신뢰할 수 없다"는 진위·신뢰 판정이므로 절대 금지. 주장의 사실 여부와 출처의 신뢰성은 어떤 표현으로도 평가하지 않는다.
확신이 없으면 flag하지 않는다. 지시된 JSON으로만 답한다."""

USER_TEMPLATE = """다음 {n}개 카드를 심사하라.

{rows}

각 카드마다 {{"id","genre","surface","consistency","note"}}를 내라. note는 flag한 경우에만 한두 문장, 아니면 빈 문자열."""

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

DRIFT_RE = re.compile(r"사실|허위|진위|거짓|신뢰|믿을|믿기")

RENDER_COLS = ("title", "claim_text", "content_nature", "claims",
               "normalized_claims", "evidence_snippets", "evidence_sources",
               "source_candidates", "source_reliability_summary",
               "source_reliability_reason", "evidence_summary",
               "debug_summary", "evidence_extraction_summary",
               "contradiction_summary", "contradiction_checks",
               "missing_context", "verdict_label", "policy_alert_level")

# ------------------------------------------------------------ Node driver ---
# Reuses the COMMITTED scripts/card_render_audit.js: vm-executes it with a
# trapped process.exit (the scan's own verdict is irrelevant here), then calls
# its renderRow per row. visibleText mirrors the audit's `visible` const.
NODE_DRIVER = r'''
const fs = require("fs"), path = require("path"), vm = require("vm");
const [ROOT, rowsPath, outPath] = process.argv.slice(2);
const AUDIT = path.join(ROOT, "scripts", "card_render_audit.js");
const src = fs.readFileSync(AUDIT, "utf8");
const fakeProcess = {
  argv: ["node", "card_render_audit.js", rowsPath], env: {},
  exit: (code) => { const e = new Error("audit exit " + code); e.__exit = code; throw e; },
  stdout: { write() {} }, stderr: { write() {} },
};
const ctx = vm.createContext({
  require, console: { log() {}, error() {} }, process: fakeProcess,
  __dirname: path.join(ROOT, "scripts"), __filename: AUDIT,
  JSON, Math, Date, String, Number, Array, Object, RegExp, Error, Buffer,
  setTimeout, clearTimeout, TextEncoder, TextDecoder, URL,
});
let execError = null;
try { vm.runInContext(src, ctx, { filename: AUDIT }); }
catch (e) { execError = e; }
if (typeof ctx.renderRow !== "function") {
  console.error("DRIVER FAIL: renderRow not defined after executing " + AUDIT
    + " - " + (execError && execError.message));
  process.exit(1);
}
// SCANNER-DECODE-FIX — KEEP IN SYNC with `decode` in
// scripts/card_render_audit.js (same map, same &amp;-last order, so the two
// instruments read identical reader text; that file is the authoritative
// copy and carries the full rationale).
const cp = (n) => (n >= 0 && n <= 0x10ffff ? String.fromCodePoint(n) : "�");
const decode = (s) => s
  .replace(/&#(\d{1,7});/g, (_, d) => cp(Number(d)))
  .replace(/&#x([0-9a-fA-F]{1,6});/g, (_, h) => cp(parseInt(h, 16)))
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
  .replace(/&amp;/g, "&");
const visibleText = (html) => decode(String(html)
  .replace(/<[^>]*>/g, "\n")).replace(/[ \t]+/g, " ");
const SECTION_NAMES = { hero: "핵심 문장", label: "상태 라벨", claims: "주장 목록",
  snippets: "근거 문장", sources: "근거 출처", cands: "공식 문서 후보",
  srs: "출처 요약", extract: "근거 추출 요약", contra: "대조 검토" };
const CAP = 3500;
const rows = JSON.parse(fs.readFileSync(rowsPath, "utf8")).rows;
const out = {};
for (const id of Object.keys(rows)) {
  let rendered;
  try { rendered = ctx.renderRow(id, rows[id]); }
  catch (e) { out[id] = { error: String(e && e.message) }; continue; }
  const parts = [];
  const face = visibleText(rendered.face || "").trim();
  if (face) parts.push("[카드 요약] " + face);
  parts.push("[공식 문서 후보 수] " + rendered.nCands + "건");
  for (const key of Object.keys(rendered.sections)) {
    let text = visibleText(rendered.sections[key] || "")
      .split("\n").map(s => s.trim()).filter(Boolean).join("\n");
    if (!text) continue;
    if (text.length > CAP) text = text.slice(0, CAP) + " …[이하 " + (text.length - CAP) + "자 생략]";
    parts.push("[" + (SECTION_NAMES[key] || key) + "]\n" + text);
  }
  out[id] = { text: parts.join("\n"), nCands: rendered.nCands };
}
fs.writeFileSync(outPath, JSON.stringify(out), "utf8");
'''


def pick_sample_ids(conn):
    """This week's top-10 reps + 6 older-week reps + MUST_INCLUDE, in a
    deterministic order both passes share."""
    with conn.cursor() as cur:
        cur.execute("SELECT week_start, payload_json FROM weekly_reports "
                    "ORDER BY id DESC")
        weeks = []
        seen_weeks = set()
        for week_start, payload_json in cur.fetchall():
            if week_start in seen_weeks:
                continue
            seen_weeks.add(week_start)
            weeks.append(json.loads(payload_json))
    ids = []
    for item in (weeks[0].get("top") if weeks else []) or []:
        rid = item.get("representative_analysis_id")
        if rid and rid not in ids:
            ids.append(rid)
    older = []
    for week in weeks[1:]:
        for item in week.get("top") or []:
            rid = item.get("representative_analysis_id")
            if rid and rid not in ids and rid not in older:
                older.append(rid)
    ids += older[:6]
    for rid in MUST_INCLUDE:
        if rid not in ids:
            ids.append(rid)
    return ids


def render_cards(conn, ids):
    """SELECT render columns, dump to a temp file, render via the embedded
    driver through the committed audit chain. Returns {id: reader_text}."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, %s FROM analysis_results WHERE id = ANY(%%s)"
                    % ", ".join(RENDER_COLS), (list(ids),))
        fetched = cur.fetchall()
    rows = {str(r[0]): {c: (None if v is None else str(v))
                        for c, v in zip(RENDER_COLS, r[1:])} for r in fetched}
    missing = [i for i in ids if str(i) not in rows]
    if missing:
        print("MISSING ROWS (not in analysis_results): %s" % missing)
    tmpdir = tempfile.mkdtemp(prefix="showcase_probe_")
    rows_path = os.path.join(tmpdir, "rows.json")
    driver_path = os.path.join(tmpdir, "driver.js")
    out_path = os.path.join(tmpdir, "rendered.json")
    with open(rows_path, "w", encoding="utf-8") as fh:
        json.dump({"_meta": {"max_id": max(ids)}, "rows": rows}, fh,
                  ensure_ascii=False)
    with open(driver_path, "w", encoding="utf-8") as fh:
        fh.write(NODE_DRIVER)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(["node", driver_path, root, rows_path, out_path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    if proc.returncode != 0 or not os.path.exists(out_path):
        print("RENDER DRIVER FAILED (node required, as for the C8 gate): %s"
              % (proc.stderr or proc.stdout or "").strip()[-300:])
        raise SystemExit(2)
    with open(out_path, encoding="utf-8") as fh:
        rendered = json.load(fh)
    cards = {}
    for rid in ids:
        entry = rendered.get(str(rid)) or {}
        if "error" in entry:
            print("RENDER ERROR on %s: %s" % (rid, entry["error"][:120]))
            continue
        text = entry.get("text") or ""
        if rid == 13977:  # AS-JUDGED: drop the post-fix caveat chip lines
            text = "\n".join(ln for ln in text.splitlines()
                             if ln.strip() != STAMP)
        cards[rid] = text
    return cards


def _extract_json(text):
    trimmed = text.strip()
    if trimmed.startswith("```"):
        trimmed = re.sub(r"^```[a-zA-Z]*\s*", "", trimmed)
        trimmed = re.sub(r"\s*```$", "", trimmed)
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start != -1 and end > start:
        return json.loads(trimmed[start:end + 1])
    return json.loads(trimmed)


def _dump_response(resp, text, tag, exc):
    kinds = ",".join(b.type for b in resp.content) or "NO CONTENT BLOCKS"
    print("PARSE FAILURE on %s: %s" % (tag, exc))
    print("  stop_reason=%s | %d content blocks [%s]"
          % (resp.stop_reason, len(resp.content), kinds))
    if resp.stop_reason == "max_tokens":
        print("  -> TRUNCATED at max_tokens=%d — raise MAX_TOKENS_PER_BATCH"
              % MAX_TOKENS_PER_BATCH)
    print("  raw text head: %r" % text[:400])
    raise SystemExit(2)


def run_reviewer(client, ordered_ids, cards, smoke=False):
    verdicts, tokens_in, tokens_out = {}, 0, 0
    batches = [ordered_ids[i:i + BATCH_SIZE]
               for i in range(0, len(ordered_ids), BATCH_SIZE)]
    fmt = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    for index, batch in enumerate(batches, start=1):
        rows = "\n\n".join("=== 카드 id=%s ===\n%s" % (rid, cards[rid])
                           for rid in batch)
        kwargs = dict(
            model=MODEL, max_tokens=MAX_TOKENS_PER_BATCH, thinking=THINKING,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": USER_TEMPLATE.format(n=len(batch), rows=rows)}])
        try:
            resp = client.messages.create(output_config=fmt, **kwargs)
        except TypeError:
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
            print("SMOKE: %s stop_reason=%s, %d/%d verdicts — continuing"
                  % (tag, resp.stop_reason, len(batch_verdicts), len(batch)))
        for verdict in batch_verdicts:
            # normalize "카드 id=13977"-style echoes to the bare numeric id
            match = re.search(r"\d+", str(verdict["id"]))
            verdicts[match.group(0) if match else str(verdict["id"])] = verdict
        tokens_in += resp.usage.input_tokens
        tokens_out += resp.usage.output_tokens
    return verdicts, (tokens_in, tokens_out)


def flagged(v):
    return v and (v["genre"] or v["surface"] or v["consistency"])


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
        ids = pick_sample_ids(conn)
        cards = render_cards(conn, ids)
    ordered_ids = [i for i in ids if i in cards]

    total_chars = sum(len(t) for t in cards.values())
    print("SHOWCASE-REVIEWER CARD PROBE — SELECT-only, %d cards rendered via "
          "the committed card_render_audit.js chain, %d chars total, model %s, "
          "2 passes × %d batches of ≤%d, thinking off"
          % (len(ordered_ids), total_chars, MODEL,
             (len(ordered_ids) + BATCH_SIZE - 1) // BATCH_SIZE, BATCH_SIZE))
    print("AS-JUDGED: '%s' chip lines stripped from 13977's text only" % STAMP)
    print("REVIEWER PROMPT (verbatim):")
    print(SYSTEM_PROMPT)

    client = anthropic.Anthropic()
    run1, usage1 = run_reviewer(client, ordered_ids, cards, smoke=True)
    run2, usage2 = run_reviewer(client, ordered_ids, cards)

    print("HEADLINE VERDICTS (run 1, verbatim):")
    for rid, expect in ((13977, "consistency"), (13700, "consistency")):
        v = run1.get(str(rid))
        if not v:
            print("  %s: NO VERDICT RETURNED" % rid)
            continue
        ok = bool(v.get(expect))
        print("  %s [%s expected]: %s | g=%s s=%s c=%s | note: %s"
              % (rid, expect, "CAUGHT" if ok else "MISSED",
                 v["genre"], v["surface"], v["consistency"], v["note"][:150]))
    known = {str(i) for i in MUST_INCLUDE}
    false_pos = [v for iid, v in run1.items()
                 if iid not in known and flagged(v)]
    print("FLAGS ON BELIEVED-CLEAN CARDS (run 1): %d of %d — classify before "
          "assuming noise (title layer's 8th defect started as an apparent FP)"
          % (len(false_pos), len(ordered_ids) - len(known)))
    for v in false_pos[:3]:
        print("  %s g=%s s=%s c=%s: %s"
              % (v["id"], v["genre"], v["surface"], v["consistency"],
                 v["note"][:110]))
    changed = [iid for iid in run1 if not run2.get(iid) or
               (run1[iid]["genre"], run1[iid]["surface"], run1[iid]["consistency"])
               != (run2[iid]["genre"], run2[iid]["surface"], run2[iid]["consistency"])]
    print("DETERMINISM: %d/%d cards changed verdict between runs%s"
          % (len(changed), len(ordered_ids),
             " (" + ", ".join(changed[:6]) + ")" if changed else ""))
    drift = [v for v in list(run1.values()) + list(run2.values())
             if v["note"] and DRIFT_RE.search(v["note"])]
    print("TRUTH-DRIFT CHECK: %d notes used truth/trust vocabulary%s"
          % (len(drift), " — DESIGN FAILURE, quote: \"%s\"" % drift[0]["note"][:80]
                if drift else ""))
    tokens_in = usage1[0] + usage2[0]
    tokens_out = usage1[1] + usage2[1]
    std = tokens_in / 1e6 * PRICE_STD[0] + tokens_out / 1e6 * PRICE_STD[1]
    intro = tokens_in / 1e6 * PRICE_INTRO[0] + tokens_out / 1e6 * PRICE_INTRO[1]
    per_item = tokens_in / 2.0 / max(1, len(ordered_ids))
    weekly = (std / 2.0) * (10.0 / max(1, len(ordered_ids)))
    print("COST: %d in + %d out tokens for BOTH passes = $%.3f std ($%.3f "
          "intro) | ~%d in-tokens/card" % (tokens_in, tokens_out, std, intro,
                                           per_item))
    print("  projected weekly (≈10 new detail cards, 1 pass) ≈ $%.3f std — "
          "vs the title layer's ~$0.013" % weekly)
    return 0


if __name__ == "__main__":
    sys.exit(main())
