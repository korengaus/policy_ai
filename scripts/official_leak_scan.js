// OFFICIAL-EVIDENCE LEAK SCAN (AUDIT-HARDENING) — the committed successor to
// the session-scratch harnesses that caught leaks #3 and #4 and then vanished.
// A guard that disappears is not a guard: this file is tracked, is invoked by
// scripts/b2b_readiness_audit.py (C7 leak-scan row — the gate cannot pass
// while skipping it), and runs standalone as:
//
//     node scripts/official_leak_scan.js <rows.json>
//
// <rows.json> = {id: {content_nature, source_candidates, normalized_claims,
// claims, source_reliability_summary, debug_summary, evidence_summary,
// source_reliability_reason}} — the audit dumps the genuine-flagged rows
// automatically; regenerate manually with the same SELECT if running alone.
//
// WHY NODE: the five assertion surfaces are frontend display logic, and the
// scan EXECUTES the real main.js chain (vm-extracted, never reimplemented):
// officialPeriodicEditionMismatch stamp → officialEvidenceIsGenuine →
// officialStatusLabel / guardStoredProse / the answer-line loader. Porting it
// to Python would create a second (third) copy of display logic that drifts.
//
// THE FIVE SURFACES where the official-evidence conclusion can be asserted:
//   1. status labels        — DYNAMIC: officialStatusLabel() on each row
//   2. export text          — routes through officialStatusLabel; the wiring
//                             is SOURCE-PINNED below, the value is surface 1
//   3. stored prose         — DYNAMIC: guardStoredProse(evidence_summary)
//   4. per-candidate reason — DYNAMIC: guardStoredProse(reliability_reason) +
//                             the reasonSuppressed swap SOURCE-PINNED
//   5. answer sentence      — DYNAMIC: the real loadAnswerLines() +
//                             the mode-ternary SOURCE-PINNED
// A source pin failing means a surface was rewired without updating the scan
// — that is a FAIL, not a skip.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const mainJs = fs.readFileSync(
  path.join(ROOT, "frontend", "scripts", "main.js"), "utf8");
const dataPath = process.argv[2];
if (!dataPath) {
  console.error("usage: node scripts/official_leak_scan.js <rows.json> "
    + "(scripts/b2b_readiness_audit.py generates and passes this file)");
  process.exit(2);
}
const rows = JSON.parse(fs.readFileSync(dataPath, "utf8"));

function extract(startMarker, endMarker = "\n    }") {
  const start = mainJs.indexOf(startMarker);
  assert.ok(start > 0, `extraction marker not found: ${startMarker}`);
  const end = mainJs.indexOf(endMarker, start);
  assert.ok(end > start, `end marker not found after: ${startMarker}`);
  return mainJs.slice(start, end + endMarker.length);
}

// ---- source pins: every consuming surface must still route through the one
// predicate / guard. If any of these disappears, the scan is blind to that
// surface and must FAIL loudly rather than pass quietly.
const SOURCE_PINS = [
  // verdict block + detail status label
  ['status-label wiring', '<span class="verdict-value">${escapeHtml(officialStatusLabel(result))}</span>'],
  // reviewer/export model + export line
  ["export model wiring", "officialStatus: officialStatusLabel(result),"],
  ["export line wiring", '["공식 근거 상태", model.officialStatus],'],
  // share-image canvas input
  ["share-image wiring", 'data-share-official="${escapeHtml(officialStatusLabel(result))}"'],
  // stored prose guard on the 근거 요약 surface
  ["stored-prose wiring", "guardStoredProse(verification.evidence_summary, sourceReliabilitySummary)"],
  // per-candidate reason swap (MATCHER-GUARD)
  ["candidate-reason wiring", 'const reasonSuppressed = periodSuppressed && source.verification_role === "primary_evidence";'],
  ["candidate-reason guard", "guardStoredProse(source.reliability_reason"],
];
// answer-line mode ternary (drift pin, same as the folded-in answer-line scan)
const MODE_EXPR = `const answerOfficialMode = ((result.content_nature ?? null) === "market_commercial" && !cardHasGenuineOfficial)
          ? "omit"
          : (cardHasGenuineOfficial ? "found" : "none");`;

const failures = [];
for (const [name, pin] of SOURCE_PINS) {
  if (!mainJs.includes(pin)) failures.push(`SOURCE PIN LOST: ${name}`);
}
if (!mainJs.includes(MODE_EXPR)) failures.push("SOURCE PIN LOST: answer-line mode ternary");

// ---- real chain, vm-extracted ----------------------------------------------
const src = [
  // predicate block + WeakSet stamp + STORED_PROSE_ASSERT_RE + guardStoredProse
  extract("const OFFICIAL_PERIODIC_FAMILY_RE", "// DISPLAY-HONESTY (①)"),
  extract("function officialEvidenceIsGenuine(summary, debug) {"),
  extract("function officialStatusLabel(result) {"),
  extract("async function loadAnswerLines() {"),
].join("\n");

// Assertions that must never appear on a period-suppressed row's surfaces.
const AFFIRMATIVE_LABEL = "공식 근거 확인";
const ANSWER_AFFIRMATIVE = "대응 문서를 찾았습니다";

async function scanRow(id, row) {
  const srs = (() => {
    try { const v = JSON.parse(row.source_reliability_summary); return v && typeof v === "object" ? v : {}; }
    catch { return {}; }
  })();
  const debug = (() => {
    try { return JSON.parse(row.debug_summary) || {}; } catch { return {}; }
  })();
  const result = {
    result_id: Number(id),
    content_nature: row.content_nature,
    source_candidates: row.source_candidates,
    normalized_claims: row.normalized_claims,
    claims: row.claims,
    source_reliability_summary: srs,
    debug_summary: debug,
  };
  const line = {
    attrs: { "data-answer-id": String(id) }, textContent: "", hidden: true,
    getAttribute(name) { return this.attrs[name] ?? null; },
  };
  const sandbox = {
    document: { querySelectorAll: (sel) => (sel === ".answer-line[data-answer-id]" ? [line] : []) },
    // spread stubbed found:false — the official clause (the leak surface) is
    // independent of spread and still fully exercised.
    fetch: async () => ({ ok: true, json: async () => ({ found: false }) }),
    API_BASE: "", encodeURIComponent, Number, String, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${src}
    const result = ${JSON.stringify(result)};
    __suppressed = officialPeriodicEditionMismatch(result);           // stamp
    __label = officialStatusLabel(result);                            // surface 1 (+2 via pins)
    __genuine = officialEvidenceIsGenuine(result.source_reliability_summary, result.debug_summary);
    __prose = guardStoredProse(${JSON.stringify(row.evidence_summary || "")}, result.source_reliability_summary);   // surface 3
    __reason = guardStoredProse(${JSON.stringify(row.source_reliability_reason || "")}, result.source_reliability_summary); // surface 4
    __assertRe = STORED_PROSE_ASSERT_RE;
    ${MODE_EXPR.replace("const answerOfficialMode", "var answerOfficialMode")
      .replace("cardHasGenuineOfficial", "__genuine").replace("cardHasGenuineOfficial", "__genuine")}
    __mode = answerOfficialMode;`, sandbox);
  line.attrs["data-answer-official"] = sandbox.__mode;
  await vm.runInContext("loadAnswerLines()", sandbox);                 // surface 5
  return {
    suppressed: sandbox.__suppressed, label: sandbox.__label,
    prose: sandbox.__prose, reason: sandbox.__reason,
    assertRe: sandbox.__assertRe,
    sentence: line.hidden ? "" : line.textContent,
  };
}

(async () => {
  const KNOWN = [7871, 9534, 13977];
  let suppressedSeen = [];
  for (const id of Object.keys(rows).map(Number).sort((a, b) => a - b)) {
    const r = await scanRow(id, rows[id]);
    if (!r.suppressed) continue;
    suppressedSeen.push(id);
    if (r.label === AFFIRMATIVE_LABEL) {
      failures.push(`id ${id}: status label asserts ${AFFIRMATIVE_LABEL}`);
    }
    if (r.assertRe.test(r.prose)) {
      failures.push(`id ${id}: stored prose (evidence_summary) still asserts`);
    }
    if (r.assertRe.test(r.reason)) {
      failures.push(`id ${id}: candidate reason line still asserts`);
    }
    if (r.sentence.includes(ANSWER_AFFIRMATIVE)) {
      failures.push(`id ${id}: answer sentence says ${ANSWER_AFFIRMATIVE}`);
    }
    console.log(`suppressed id ${id}: label=${r.label} | sentence=${r.sentence || "(hidden)"}`);
  }
  // guard-the-guard: the known suppressed cards, when present in the input,
  // must actually be DETECTED — a predicate that stopped firing would
  // otherwise make every check above pass vacuously.
  for (const id of KNOWN) {
    if (rows[String(id)] && !suppressedSeen.includes(id)) {
      failures.push(`known suppressed id ${id} NOT detected — predicate chain broken or drifted`);
    }
  }
  if (failures.length) {
    for (const f of failures) console.error("LEAK-SCAN FAIL:", f);
    process.exit(1);
  }
  console.log(`LEAK SCAN PASSED: ${suppressedSeen.length} suppressed row(s) [${suppressedSeen}] assert nothing on all 5 surfaces (${Object.keys(rows).length} rows scanned)`);
})().catch((e) => { console.error("LEAK-SCAN CRASH:", e); process.exit(1); });
