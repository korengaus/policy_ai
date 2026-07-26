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
  // FIELD-VALUE-GUARD (surface 6 — field VALUES, the fifth leak's class):
  // the gated sites must keep their suppression wiring, and the mixed-scale
  // denominator must stay scale-aware.
  ["snippet field gate", "const fieldSuppressed = periodSuppressed && isOfficialLikeSource(snippet);"],
  ["candidate field gate", "const fieldValueSuppressed = periodSuppressed && isOfficialLikeSource(source);"],
  ["public-card strength gate", "periodSuppressed && isOfficialLikeSource(source)"],
  ["scale-aware denominator", "scoreValue > 5"],
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
  // FIELD-VALUE-GUARD: the real snippet renderer + the real formatters whose
  // OUTPUT is the assertion, + the real official-source detector. Their other
  // presentation deps are stubbed in the sandbox (stubs render values
  // verbatim, so assertions cannot hide in a stub).
  extract("function renderEvidenceSnippets(claims, evidenceSnippets"),
  extract("function formatEvidenceType(value) {"),
  extract("function formatSupportsClaim(value) {"),
  extract("function isOfficialLikeSource(source) {"),
  extract("function escapeHtml(value) {"),
  extract("function safeUrl(value) {", "\n      }\n    }"),
].join("\n");

// Presentation stubs for renderEvidenceSnippets' non-asserting deps —
// identity/pass-through so every VALUE reaches the output for scanning.
const RENDER_STUBS = `
  const advDefList = (pairs) => pairs.map(([l, v]) => v === "" || v == null ? "" : l + ": " + String(v)).filter(Boolean).join("\\n");
  const userFacingReportText = (t, f) => (t == null || t === "" ? f : String(t));
  const publicInstitutionName = (t) => String(t == null ? "" : t);
  const limitClaimSentences = (t) => String(t == null ? "" : t);
  const cleanArticleTextForPolicyAnalysis = (t) => String(t == null ? "" : t);
  const CLAIM_MAX_CHARS = 400;
  const formatTechnicalLabel = (v) => String(v == null ? "" : v);
  const formatExtractionConfidence = (v) => String(v == null ? "" : v);
  const formatDiagnosticText = (v) => String(v == null ? "" : v);
`;
// Synthetic official snippet carrying the exact 13977-shape assertive FIELD
// VALUES. The claim/doc data is irrelevant here — the WeakSet stamp comes
// from the row's REAL srs object; this fixture only exercises the renderer.
const ASSERTIVE_SNIPPET = {
  claim_index: 0,
  evidence_text: "고용률은 70.2%로 전년동월대비 0.1%p 하락",
  source_title: "고용노동부 보도자료", publisher: "고용노동부",
  source_url: "https://www.korea.kr/briefing/pressReleaseView.do?newsId=1",
  evidence_type: "direct_support", supports_claim: "supports",
  relevance_score: 90, evidence_quality_score: 100,
};

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
  vm.runInContext(`const sanitizeDisplayText = (v) => String(v == null ? "" : v);
    ${RENDER_STUBS}
    ${src}
    const result = ${JSON.stringify(result)};
    __suppressed = officialPeriodicEditionMismatch(result);           // stamp
    __label = officialStatusLabel(result);                            // surface 1 (+2 via pins)
    __genuine = officialEvidenceIsGenuine(result.source_reliability_summary, result.debug_summary);
    __prose = guardStoredProse(${JSON.stringify(row.evidence_summary || "")}, result.source_reliability_summary);   // surface 3
    __reason = guardStoredProse(${JSON.stringify(row.source_reliability_reason || "")}, result.source_reliability_summary); // surface 4
    __assertRe = STORED_PROSE_ASSERT_RE;
    // surface 6 (FIELD-VALUE-GUARD): the real snippet renderer against the
    // row's REAL stamped srs object, with the assertive fixture; plus an
    // unsuppressed control proving the renderer would assert without the gate.
    const FIXTURE = ${JSON.stringify(ASSERTIVE_SNIPPET)};
    __fieldOut = renderEvidenceSnippets(["테스트 주장"], [FIXTURE], result.source_reliability_summary);
    __fieldCtl = renderEvidenceSnippets(["테스트 주장"], [FIXTURE], {});
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
    fieldOut: sandbox.__fieldOut, fieldCtl: sandbox.__fieldCtl,
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
    // surface 6 — assertive FIELD VALUES (the fifth leak's class):
    if (r.fieldOut.includes("직접 근거")) {
      failures.push(`id ${id}: snippet field 근거 유형 still asserts 직접 근거`);
    }
    if (/주장 지지: *지지/.test(r.fieldOut)) {
      failures.push(`id ${id}: snippet field 주장 지지 still asserts 지지`);
    }
    if (!(r.fieldOut.includes("90") && r.fieldOut.includes("100"))) {
      failures.push(`id ${id}: NEVER-RENUMBER violated — a stored number vanished from the snippet block`);
    }
    if (!r.fieldCtl.includes("직접 근거")) {
      failures.push(`id ${id}: control render lost 직접 근거 — the field check is vacuous`);
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
  // SUPPRESSION-UNIFY: machine-readable id set for the audit's parity
  // cross-check (the Python predicate evaluates the SAME input rows; the two
  // implementations exist deliberately — see Phase 1 — and this line is how
  // divergence becomes impossible to miss). Emitted on success AND failure.
  console.log(`JS_SUPPRESSED_IDS=${JSON.stringify(suppressedSeen.sort((a, b) => a - b))}`);
  if (failures.length) {
    for (const f of failures) console.error("LEAK-SCAN FAIL:", f);
    process.exit(1);
  }
  console.log(`LEAK SCAN PASSED: ${suppressedSeen.length} suppressed row(s) [${suppressedSeen}] assert nothing on all 6 surfaces incl. field values (${Object.keys(rows).length} rows scanned)`);
})().catch((e) => { console.error("LEAK-SCAN CRASH:", e); process.exit(1); });
