// CARD-RENDER-AUDIT — the committed render scanner (sibling of
// scripts/official_leak_scan.js, same philosophy: a guard that disappears is
// not a guard — the leak scan's scratch predecessor vanished with its session
// and had to be rebuilt when the fourth leak surface appeared).
//
// WHAT IT DOES: vm-executes the REAL frontend/scripts/main.js render chain
// (never reimplemented) over a deterministic row sample and measures what a
// reader would actually see, per defect class:
//   ZERO classes  — regressions of deliberate fixes (English reason
//                   sentences, raw enums, literal \uXXXX escapes, mixed-scale
//                   scores, English labels, HTML-as-text, snake_case
//                   identifiers). ANY occurrence is a FAIL.
//   CEILING classes — ingest artefacts we live with (bullet furniture,
//                   hero-restates-title, sentence joins, digit-start claims,
//                   ?-mojibake, empty sections, candidate-count tail). Their
//                   baseline rates live in scripts/card_render_baselines.json
//                   and only GROWTH beyond tolerance warns — the null-verdict
//                   fossil / spine-artifact pattern.
//   GRID          — the answer-sentence omission grid (15 mode×spread cases)
//                   must all compose grammatical sentences. Any bad case FAILs.
//
// USAGE:
//   node scripts/card_render_audit.js <rows.json> [baselines.json]
// <rows.json> = {"_meta": {"max_id": N}, "rows": {id: {column: string|null}}}
// with the columns listed in ROW_COLUMNS below. scripts/b2b_readiness_audit.py
// (C8 render-scan row) dumps the mod-14 sample plus the latest-500 window and
// invokes this automatically; the gate is not passable while skipping it.
// Baselines default to scripts/card_render_baselines.json next to this file.
//
// SOURCE PINS: the scanner extracts ~60 helpers from main.js by marker. A
// rename/removal means "PIN LOST: <name>" and exit 1 BEFORE any scanning — a
// missing dependency must blind the scan loudly, not quietly. A REFACTOR THAT
// ADDS a new helper surfaces as "UNPINNED DEPENDENCY: <name>" at render time
// (there is deliberately NO stub fallback): review the new helper, then add
// it to PINNED_DEPS. Additionally every rendered section must be non-empty on
// at least one sampled row (a renderer collapsing to "" is a silent-coverage
// failure) and every ZERO-class detector must fire on its built-in control
// specimen (a detector that can no longer fire is vacuous → FAIL).
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const mainJs = fs.readFileSync(
  path.join(ROOT, "frontend", "scripts", "main.js"), "utf8");

const dataPath = process.argv[2];
if (!dataPath) {
  console.error("usage: node scripts/card_render_audit.js <rows.json> "
    + "[baselines.json] (scripts/b2b_readiness_audit.py generates the dump)");
  process.exit(2);
}
const baselinePath = process.argv[3]
  || path.join(__dirname, "card_render_baselines.json");
let BASE;
try {
  BASE = JSON.parse(fs.readFileSync(baselinePath, "utf8"));
} catch (e) {
  console.error("RENDER-SCAN FAIL: baselines unreadable at " + baselinePath
    + " — the growth check cannot run without its recorded baselines: "
    + e.message);
  process.exit(1);
}
const payload = JSON.parse(fs.readFileSync(dataPath, "utf8"));
const rows = payload.rows || payload; // legacy flat form accepted
const maxId = Number((payload._meta || {}).max_id) || Math.max(
  ...Object.keys(rows).map(Number));

const ROW_COLUMNS = ["title", "claim_text", "content_nature", "claims",
  "normalized_claims", "evidence_snippets", "evidence_sources",
  "source_candidates", "source_reliability_summary",
  "source_reliability_reason", "evidence_summary", "debug_summary",
  "evidence_extraction_summary", "contradiction_summary",
  "contradiction_checks", "missing_context", "verdict_label",
  "policy_alert_level"];

// ---------------------------------------------------------------------------
// SOURCE PINS — every helper the render chain needs, extracted from main.js
// by marker. Order does not matter here; snippets are sorted by their
// position in main.js before evaluation so const-initializer order is
// preserved exactly as the browser sees it.
// ---------------------------------------------------------------------------
const PINNED_DEPS = [
  // display sanitizers / launderers (REAL — a stub here would hide leaks)
  "sanitizeDisplayText", "repairMojibake", "stripLeadingTitleMarker",
  "cleanArticleTextForPolicyAnalysis", "splitArticleSentences",
  "isArticleBoilerplateSentence", "stripInternalDiagnosticText",
  "publicInstitutionName", "userFacingReportText", "escapeHtml", "safeUrl",
  "cleanConceptKeysForDisplay", "stripInternalDiagnosticText",
  // formatters (closed-vocabulary maps)
  "formatSignal", "formatAlert", "formatLevel", "formatDirection",
  "formatRecommendation", "formatVerdict", "formatReviewStatus",
  "formatSourceType", "formatTechnicalLabel", "formatDiagnosticText",
  "formatDisplayDate", "formatReasonCounts", "formatReadableValue",
  "formatList", "formatEvidenceSummaryLabel", "formatClaimStatus",
  "formatClaimType", "formatUncertainty", "formatSourcePurpose",
  "formatReliabilityLevel", "formatVerificationRole", "formatEvidenceType",
  "formatSupportsClaim", "formatExtractionConfidence",
  "formatContradictionStatus", "formatContradictionRisk",
  // section renderers (the surfaces under audit)
  "advDefList", "renderCollapsibleSection", "renderClaimList",
  "renderNormalizedClaims", "renderEvidenceSnippets", "renderEvidenceSources",
  "renderSourceCandidates", "renderSourceReliabilitySummary",
  "renderSourceQueries", "renderEvidenceExtractionSummary",
  "renderContradictionSummary", "renderContradictionChecks",
  "conflictCandidateJoin",
  // card face (home/feed card summary) + its truncation budget
  "topSummaryLine", "stripCardFaceWrapper", "truncateCardFaceClaim",
  "CARD_FACE_MAX_CHARS",
  // official-evidence predicate chain + status label + answer line
  "officialEvidenceIsGenuine", "officialStatusLabel", "isOfficialLikeSource",
  "loadAnswerLines",
  // hero-claim promotion chain
  "claimTextsOverlap", "sanitizeClaimText", "buildReviewerSafeClaim",
  "exportClaimText", "limitClaimSentences", "claimIsBoilerplateFurniture",
  "truncateClaimOnBoundary", "isGenericClaimPlaceholder",
  "claimLooksSuspicious", "hasDirectOfficialSupport",
  "officialEvidenceStateForResult", "buildOfficialEvidenceState",
  "numberValue", "stripCertaintyWords", "sanitizePublicExportText",
  "claimLooksAlignedWithResult", "keywordSetForClaimAlignment",
  "substantiveClaimForPromotion", "claimIsQuoteLead", "claimIsDeicticLead",
  "cautiousClaimPrefix", "needsHumanReviewForResult", "advIsEmptyDisplay",
  "publicSourceFilterText", "officialDirectMatchLabel", "publicSupportLabel",
  "publicSourceReason", "publicSourceTypeLabel", "stripQuoteLeadWrapper",
  "fallbackClaimFromTitle", "officialDirectScoreForResult",
  "quantitativeClaimForReselection", "sourceExclusionLabel",
  "sourceTraceability", "sourceDomain", "CLAIM_VERB_ENDER", "CLAIM_TRIM_FLOOR_CHARS", "CLAIM_DANGLING_JOSA", "CLAIM_TERMINAL_PUNCT", "polishClaimEnding",
  // consts (extracted `const X = …;` → evaluated in main.js source order)
  "CLAIM_MAX_CHARS", "CLAIM_FURNITURE_MARKERS", "ARTICLE_NOISE_PATTERNS",
  "ARTICLE_NOISE_SENTENCE_PATTERNS", "POLICY_SIGNAL_PATTERN",
  "CLAIM_QUOTE_CHARS", "CLAIM_SAID_VERBS", "CLAIM_SPEAKER_LEAD",
  "CLAIM_DEICTIC_MARK", "CLAIM_QUOTED_SPAN", "CLAIM_QUANTITY",
  "EVIDENCE_STATE_LABELS", "SOURCE_TYPE_LABELS",
  "LEADING_TITLE_MARKER_RE",
];
// The suppression-stamp block (WeakSet + predicate + guardStoredProse) is one
// contiguous region, pinned the same way the leak scan pins it.
const BLOCK_PINS = [
  ["official-period block", "const OFFICIAL_PERIODIC_FAMILY_RE",
    "// DISPLAY-HONESTY (①)"],
];

function extractRange(startMarker, endMarker) {
  const start = mainJs.indexOf(startMarker);
  if (start < 0) return null;
  const end = mainJs.indexOf(endMarker, start);
  if (end < 0) return null;
  return { pos: start, src: mainJs.slice(start, end + endMarker.length) };
}
function extractDep(name) {
  for (const marker of
    [`    function ${name}(`, `    async function ${name}(`]) {
    const got = extractRange(marker, "\n    }");
    if (got) return got;
  }
  return extractRange(`    const ${name} =`, ";\n");
}

const pinFailures = [];
const snippets = [];
const seen = new Set();
for (const name of PINNED_DEPS) {
  if (seen.has(name)) continue;
  seen.add(name);
  const got = extractDep(name);
  if (!got) pinFailures.push(`SOURCE PIN LOST: ${name} — renamed or removed `
    + "from main.js; the scan is blind to every surface using it");
  else snippets.push({ name, ...got });
}
for (const [label, startM, endM] of BLOCK_PINS) {
  const got = extractRange(startM, endM);
  if (!got) pinFailures.push(`SOURCE PIN LOST: ${label}`);
  else snippets.push({ name: label, ...got });
}
if (pinFailures.length) {
  for (const f of pinFailures) console.error("RENDER-SCAN FAIL:", f);
  console.error(`RENDER SCAN FAILED: ${pinFailures.length} source pin(s) lost `
    + "— fix the pins before trusting any rate below");
  process.exit(1);
}
snippets.sort((a, b) => a.pos - b.pos);
const src = snippets.map((s) => s.src).join("\n");

const sandbox = {
  console, Number, String, Array, Object, JSON, Math, RegExp, Date, isNaN,
  encodeURIComponent, decodeURIComponent, URL, Set, WeakSet, Map,
  API_BASE: "",
  fetch: async () => ({ ok: true, json: async () => ({ found: false }) }),
  document: { querySelectorAll: () => [] },
};
vm.createContext(sandbox);
try {
  vm.runInContext(src, sandbox);
} catch (e) {
  console.error("RENDER-SCAN FAIL: extracted chain does not evaluate — "
    + "an extraction boundary drifted: " + e.message);
  process.exit(1);
}

// ---------------------------------------------------------------------------
// per-row render — all PUBLIC card-detail sections (bias sections are off the
// card and the pipeline-debug section is operator-gated; neither is public)
// ---------------------------------------------------------------------------
const J = (v) => { try { return JSON.parse(v); } catch { return null; } };
// SCANNER-DECODE-FIX: decode exactly what main.js escapeHtml emits — &amp;
// &lt; &gt; &quot; and the ZERO-PADDED numeric &#039;. The old map only knew
// bare &#39;, so the scanner read correctly-escaped apostrophes as literal
// text and reported its own blind spot as a reader-facing defect. Numeric
// entities decode generically (bare, zero-padded, hex). &amp; decodes LAST
// on purpose: a double-escaped &amp;#039; must surface as &#039; in decoded
// output so the undecoded-html-entity zero class catches the real leak
// instead of silently un-double-escaping it.
// KEEP IN SYNC with visibleText in scripts/showcase_reviewer_card_probe.py.
const cp = (n) => (n >= 0 && n <= 0x10ffff ? String.fromCodePoint(n) : "�");
const decode = (s) => s
  .replace(/&#(\d{1,7});/g, (_, d) => cp(Number(d)))
  .replace(/&#x([0-9a-fA-F]{1,6});/g, (_, h) => cp(parseInt(h, 16)))
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
  .replace(/&amp;/g, "&");
const visible = (html) => decode(String(html)
  .replace(/<[^>]*>/g, "\n")).replace(/[ \t]+/g, " ");

function renderRow(id, row) {
  const result = {
    result_id: Number(id), title: row.title, claim_text: row.claim_text,
    content_nature: row.content_nature,
    claims: J(row.claims), normalized_claims: J(row.normalized_claims),
    evidence_snippets: J(row.evidence_snippets),
    evidence_sources: J(row.evidence_sources),
    source_candidates: J(row.source_candidates),
    source_reliability_summary: J(row.source_reliability_summary) || {},
    debug_summary: J(row.debug_summary) || {},
    evidence_summary: row.evidence_summary,
    verdict_label: row.verdict_label,
    policy_alert_level: row.policy_alert_level,
    missing_context: row.missing_context,
  };
  sandbox.__row = result;
  sandbox.__extract = J(row.evidence_extraction_summary);
  sandbox.__contraSum = J(row.contradiction_summary);
  sandbox.__contraChecks = J(row.contradiction_checks);
  return vm.runInContext(`(() => {
    const r = __row;
    const genuine = officialEvidenceIsGenuine(r.source_reliability_summary, r.debug_summary);
    officialPeriodicEditionMismatch(r); // stamp, exactly as the app does
    return {
      sections: {
        hero: escapeHtml(exportClaimText(r)),
        label: escapeHtml(officialStatusLabel(r)),
        claims: renderClaimList(r.claims) + renderNormalizedClaims(r.normalized_claims),
        snippets: renderEvidenceSnippets(r.claims, r.evidence_snippets, r.source_reliability_summary),
        sources: renderEvidenceSources(r.evidence_sources, genuine, r.source_reliability_summary),
        cands: renderSourceCandidates(r.source_candidates, genuine, r.source_reliability_summary),
        srs: renderSourceReliabilitySummary(r.source_reliability_summary, genuine),
        extract: renderEvidenceExtractionSummary(__extract),
        // REBUTTAL-PATH-WIRING mirror: the app call site (main.js 반박 section
        // builder) now threads conflictCandidateJoin(sourceCandidates,
        // cardHasGenuineOfficial) as the third argument; the genuine const
        // above IS that flag here. Without this the scanner renders the
        // pre-join view and the reviewer would keep flagging a defect the app
        // no longer shows. (NB: this comment lives inside a template literal —
        // no backticks.)
        contra: renderContradictionSummary(__contraSum)
          + renderContradictionChecks(r.claims, __contraChecks,
              conflictCandidateJoin(r.source_candidates, genuine)),
      },
      nCands: Array.isArray(r.source_candidates) ? r.source_candidates.length : 0,
      // CARD FACE (home/feed card summary) — the surface topicCardFromResult
      // renders. faceFull is the SAME string with the budget removed, so the
      // check below can tell "this was cut" from "this is short" without
      // guessing anything about Korean sentence shape.
      face: stripCardFaceWrapper(topSummaryLine(r)),
      faceFull: stripCardFaceWrapper(
        userFacingReportText(exportClaimText(r), "")),
    };
  })()`, sandbox);
}

// ---------------------------------------------------------------------------
// CARD-FACE TRUNCATION (id 13700: "…수료증이 발급된" — cut one character short
// of 발급된다, unmarked). Found by eye because every detector here looked for
// English, machine tokens, joins or mojibake, and a Korean sentence cut
// mid-word is none of those.
//
// This check is deliberately MECHANICAL and asks nothing about how a Korean
// sentence may legitimately end (headline-style noun endings are valid and
// common — a linguistic "does this look finished" test measured 52% on real
// cards and would be pure noise). It compares the rendered face against the
// SAME string rendered without the budget and asserts two invariants:
//   1. if the face is shorter than the full line, it must carry the site's
//      "…" marker — a cut the reader cannot see is the actual defect;
//   2. the cut must land on whitespace or punctuation in the full line, never
//      inside an eojeol.
// Invariant 2 is skipped when the retained body is not a literal prefix of the
// full line (the boundary helper may trim a trailing comma/full stop), so the
// check declines to fire rather than guess. Returns null = clean.
const FACE_CUT_BOUNDARY = /[\s.,!?…·、，)\]"'」』]/;
function cardFaceTruncationDefect(face, full) {
  const f = String(face || "");
  const g = String(full || "");
  if (!f || !g || f === g) return null;
  if (!/…$/.test(f)) return "cut without the … marker";
  const body = f.replace(/…+$/, "").replace(/[.,\s]+$/, "");
  if (!body || !g.startsWith(body)) return null;
  const next = g.charAt(body.length);
  if (next && !FACE_CUT_BOUNDARY.test(next)) return "cut inside a word";
  return null;
}

// ---------------------------------------------------------------------------
// ZERO classes — regressions of deliberate fixes; ANY hit FAILs. Each entry
// carries a control specimen its regex MUST match (vacuity guard).
// ---------------------------------------------------------------------------
const ZERO = [
  ["english-reason-sentence",
    /Official documents? excluded from verification|no material entity or specific policy term|No official document candidate links found/i,
    "x Official document excluded from verification: y"],
  ["raw-document_type", /\bdocument_type\s*:/,
    "excluded document_type: press_release"],
  ["machine-enum",
    /\b(policy_briefing_api|national_law_api|fss_bodo_api|official_evidence_resolved|explicit_conflict|context_mismatch)\b/,
    "수집 방식 policy_briefing_api"],
  ["english-label", /품질 (strong|medium|weak)|claim #\d/,
    "품질 strong 0"],
  ["literal-unicode-escape", /\\u[0-9A-Fa-f]{4}/,
    "피싱 \\uDB80\\uDEB1 신종"],
  ["html-markup-as-text", /<(p|span|div|br|a|strong|em|table|ul|li)[ >/]/,
    'x <p style="line-height:140%"> y'],
  // SCANNER-DECODE-FIX: after the completed decode above, NO entity shape may
  // survive in reader-visible text — a survivor means a double-escape or an
  // undecoded stored entity, i.e. a real leak the old blind spot was hiding.
  // Prose ampersands (R&D, AT&T) cannot false-positive: the pattern requires
  // a full entity shape ending in ";".
  ["undecoded-html-entity",
    /&#\d+;|&#x[0-9a-fA-F]+;|&(quot|amp|lt|gt|apos|nbsp);/,
    "따옴표 &#039; 잔류"],
];
// Generic snake_case identifiers — a ZERO class since DISPLAY-LEAK-FIX-2
// laundered the last residue (possible_redirect risk flags; the
// news_context/primary_source/fact_check legacy conflict echo). Tested
// against URL-STRIPPED text: URLs quoted inside real article prose
// legitimately contain underscores (heat_wave_night_leaflet.pdf) and are
// content, not machine text. Vacuity-controlled below like every zero class.
const SNAKE_RE = /(^|[^\w가-힣])[a-z]{2,}_[a-z]{2,}[a-z_]*\b/;
const SNAKE_CONTROL = "판정 근거 explicit_conflict";
const stripUrls = (t) => t.replace(/https?:\/\/\S+/g, " ")
  .replace(/[\w.-]+\.(kr|com|org|net)\S*/g, " ");
// mixed-scale needs its numeric predicate, kept as a special zero class
const MIXED_SCALE_RE = /(신뢰도|점수|품질|관련도)[:\s]{0,4}(\d+)\s*\/\s*5(?!\d)/g;
const MIXED_CONTROL = "출처 신뢰도: 95/5";

const failures = [];
const warns = [];

// vacuity controls — a detector that cannot fire any more is a FAIL
for (const [name, re, specimen] of ZERO) {
  if (!re.test(specimen)) failures.push(
    `VACUOUS DETECTOR: zero-class "${name}" no longer matches its control`);
}
{
  const m = [...MIXED_CONTROL.matchAll(MIXED_SCALE_RE)];
  if (!m.length || Number(m[0][2]) <= 5) failures.push(
    "VACUOUS DETECTOR: mixed-scale no longer matches its control");
}
if (!SNAKE_RE.test(stripUrls(SNAKE_CONTROL))) failures.push(
  "VACUOUS DETECTOR: snake-case no longer matches its control");
// card-face controls: the 13700 shape (unmarked cut) and a mid-word cut that
// merely carries the marker must BOTH be caught; a clean cut and an untruncated
// face must NOT be.
{
  const FULL = "요건 충족 시 수료증이 발급된다";
  if (!cardFaceTruncationDefect("요건 충족 시 수료증이 발급된", FULL)) failures.push(
    "VACUOUS DETECTOR: card-face truncation misses the unmarked-cut control");
  if (!cardFaceTruncationDefect("요건 충족 시 수료증이 발급된…", FULL)) failures.push(
    "VACUOUS DETECTOR: card-face truncation misses the mid-word control");
  if (cardFaceTruncationDefect("요건 충족 시 수료증이…", FULL)) failures.push(
    "OVER-EAGER DETECTOR: card-face truncation fires on a clean marked cut");
  if (cardFaceTruncationDefect(FULL, FULL)) failures.push(
    "OVER-EAGER DETECTOR: card-face truncation fires on an untruncated face");
}

// ---------------------------------------------------------------------------
// scan
// ---------------------------------------------------------------------------
const windows = { mod14: [], latest500: [] };
for (const id of Object.keys(rows).map(Number).sort((a, b) => a - b)) {
  if (id % 14 === 0) windows.mod14.push(id);
  if (id > maxId - 500) windows.latest500.push(id);
}
const counts = {};   // window -> class -> {n, ids[]}
const sectionSeen = new Set();
const candTail = { mod14: [], latest500: [] };
const rendered = {}; // id -> fullText (rendered once, counted per window)
const t0 = Date.now();

for (const id of Object.keys(rows).map(Number).sort((a, b) => a - b)) {
  let out;
  try {
    out = renderRow(id, rows[String(id)]);
  } catch (e) {
    const m = /^(\w+) is not defined/.exec(e.message);
    failures.push(m
      ? `UNPINNED DEPENDENCY: ${m[1]} (row ${id}) — a main.js refactor added `
        + "a helper this scan does not extract; review it and add it to "
        + "PINNED_DEPS"
      : `RENDER CRASH on row ${id}: ${e.message.slice(0, 120)}`);
    break; // one loud failure; per-row spam helps nobody
  }
  const secs = Object.entries(out.sections).map(([k, v]) => [k, visible(v)]);
  for (const [k, v] of secs) if (v.replace(/\s+/g, "")) sectionSeen.add(k);
  rendered[id] = {
    text: secs.map(([, v]) => v).join("\n"), secs, nCands: out.nCands,
    faceDefect: cardFaceTruncationDefect(out.face, out.faceFull),
  };
}

if (!failures.length) {
  for (const k of ["hero", "label", "claims", "snippets", "sources", "cands",
    "srs", "extract", "contra"]) {
    if (!sectionSeen.has(k)) failures.push(
      `SECTION BLANK: "${k}" rendered empty on every sampled row — the `
      + "render chain changed shape and this scan lost that surface");
  }
}

const hit = (win, cls, id) => {
  const c = (counts[win] = counts[win] || {});
  const e = (c[cls] = c[cls] || { n: 0, ids: [] });
  e.n += 1;
  if (e.ids.length < 5) e.ids.push(id);
};

for (const [win, ids] of Object.entries(windows)) {
  for (const id of ids) {
    const r = rendered[id];
    if (!r) continue;
    const t = r.text;
    for (const [name, re] of ZERO) if (re.test(t)) hit(win, "z:" + name, id);
    for (const m of t.matchAll(MIXED_SCALE_RE)) {
      if (Number(m[2]) > 5) { hit(win, "z:mixed-scale", id); break; }
    }
    if (SNAKE_RE.test(stripUrls(t))) hit(win, "z:snake-case-identifier", id);
    if (r.faceDefect) hit(win, "z:card-face-truncation", id);
    // ceilings
    if (/[■-◿①-⓿⬚-⬯※▣◈▲△▴▷]/.test(t)) hit(win, "c:bullet_char", id);
    if (/[가-힣]\?[가-힣]/.test(t)) hit(win, "c:question_mojibake", id);
    if (/(습니다|입니다|한다|았다|었다|이다|된다|힌다)[.!?][가-힣]/.test(t)) hit(win, "c:sentence_join", id);
    const hero = visible(r.secs.find(([k]) => k === "hero")[1]).trim();
    if (/^[0-9]/.test(hero)) hit(win, "c:hero_digit_start", id);
    const norm = (s) => String(s || "").replace(/[\s\p{P}]+/gu, "");
    const nh = norm(hero.replace(/^(보도 내용은|기사 제목과 요약 기준으로는)/, ""));
    const nt = norm(rows[String(id)].title);
    if (nh && nt && (nh.includes(nt) || nt.includes(nh))) hit(win, "c:hero_restates_title", id);
    let empt = false;
    for (const [k, v] of r.secs) {
      if (k !== "hero" && k !== "label" && !v.replace(/\s+/g, "")) empt = true;
    }
    if (empt) hit(win, "c:empty_section", id);
    candTail[win].push(r.nCands);
  }
}

// ---------------------------------------------------------------------------
// evaluate against baselines
// ---------------------------------------------------------------------------
const rateLines = [];
for (const [win, ids] of Object.entries(windows)) {
  const n = ids.length || 1;
  const winBase = (BASE.windows || {})[win];
  if (!winBase) {
    failures.push(`BASELINES MISSING for window "${win}" in ${baselinePath}`);
    continue;
  }
  for (const [name] of ZERO.concat(
    [["mixed-scale"], ["snake-case-identifier"], ["card-face-truncation"]])) {
    const e = (counts[win] || {})["z:" + name];
    if (e) failures.push(
      `ZERO-CLASS REGRESSION [${win}] ${name}: ${e.n} row(s) e.g. ids `
      + `${e.ids.join(",")} — this class was deliberately fixed to zero and is `
      + "back on the card in front of a reader");
  }
  for (const [cls, base] of Object.entries(winBase.ceilings)) {
    const e = (counts[win] || {})["c:" + cls] || { n: 0, ids: [] };
    const rate = e.n / n;
    rateLines.push(`RATE [${win}] ${cls}: ${e.n}/${n} = ${(100 * rate).toFixed(1)}%`
      + ` (baseline ${(100 * base.rate).toFixed(1)}% +${(100 * base.tol).toFixed(1)}pp)`);
    if (rate > base.rate + base.tol) warns.push(
      `CEILING RISE [${win}] ${cls}: ${(100 * rate).toFixed(1)}% > baseline `
      + `${(100 * base.rate).toFixed(1)}% + ${(100 * base.tol).toFixed(1)}pp `
      + `(e.g. ids ${e.ids.join(",")}) — the artefact is GROWING; find the `
      + "ingest change before it becomes the norm");
    // guard-the-guard: a well-established artefact measuring EXACTLY zero
    // means the detector or its surface went dead, not that ingest got clean
    if (base.rate >= 0.02 && e.n === 0 && ids.length >= 100) failures.push(
      `DEAD DETECTOR [${win}] ${cls}: baseline ${(100 * base.rate).toFixed(1)}% `
      + "but measured 0 — the check or the surface it reads went blind");
  }
  const tail = candTail[win].slice().sort((a, b) => b - a);
  if (tail.length) {
    const q = (p) => tail[Math.max(0, Math.floor(tail.length * (1 - p)) - 1)] || 0;
    const p90 = q(0.9), p99 = q(0.99);
    const cb = winBase.cand_tail;
    rateLines.push(`RATE [${win}] cand_tail: p90=${p90} p99=${p99} max=${tail[0]}`
      + ` (baseline p90=${cb.p90} p99=${cb.p99})`);
    if (p90 > cb.p90 * cb.warn_factor || p99 > cb.p99 * cb.warn_factor) {
      warns.push(`CEILING RISE [${win}] candidate-count tail: p90=${p90} `
        + `p99=${p99} vs baseline p90=${cb.p90} p99=${cb.p99} `
        + "— cards are accumulating even more unrelated documents");
    }
  }
}

// ---------------------------------------------------------------------------
// answer-sentence omission grid (deterministic; any bad case FAILs)
// ---------------------------------------------------------------------------
(async () => {
  const okPatterns = [
    /^이 주장은 \d+개 매체에서 (\d+일에 걸쳐 )?보도됐고, 정부 공식 자료에서 대응 문서를 (찾았|찾지 못했)습니다\.$/,
    /^이 주장은 \d+개 매체에서 (\d+일에 걸쳐 )?보도됐습니다\.$/,
    /^정부 공식 자료에서 대응 문서를 (찾았|찾지 못했)습니다\.$/,
  ];
  for (const mode of ["found", "none", "omit"]) {
    for (const spread of [
      { name: "api_absent", ok: false },
      { name: "not_found", ok: true, json: { found: false } },
      { name: "count0", ok: true, json: { found: true, cluster: { outlet_count: 0 } } },
      { name: "span0", ok: true, json: { found: true, cluster: { outlet_count: 3 }, timeline: { span_days: 0 } } },
      { name: "span5", ok: true, json: { found: true, cluster: { outlet_count: 3 }, timeline: { span_days: 5 } } },
    ]) {
      const line = {
        attrs: { "data-answer-id": "1", "data-answer-official": mode },
        textContent: "", hidden: true,
        getAttribute(n) { return this.attrs[n] ?? null; },
      };
      sandbox.document.querySelectorAll = (sel) =>
        sel === ".answer-line[data-answer-id]" ? [line] : [];
      sandbox.fetch = async () => ({ ok: spread.ok, json: async () => spread.json || {} });
      await vm.runInContext("loadAnswerLines()", sandbox);
      const sentence = line.hidden ? "" : line.textContent;
      const bad = (sentence && !okPatterns.some((p) => p.test(sentence)))
        || (!sentence && mode !== "omit");
      if (bad) failures.push(`ANSWER GRID ${mode}/${spread.name}: `
        + `"${sentence || "EMPTY"}" — ungrammatical or missing composition`);
    }
  }

  // ---- report ---------------------------------------------------------------
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  for (const line of rateLines) console.log(line);
  for (const w of warns) console.log("RENDER-SCAN WARN:", w);
  for (const f of failures) console.error("RENDER-SCAN FAIL:", f);
  const summary = `mod14=${windows.mod14.length} latest500=${windows.latest500.length}`
    + ` rows, ${secs}s, zero-classes clean=${!failures.some((f) => f.includes("ZERO-CLASS"))}`
    + `, warns=${warns.length}`;
  if (failures.length) {
    console.error(`RENDER SCAN FAILED: ${summary}`);
    process.exit(1);
  }
  console.log(`RENDER SCAN ${warns.length ? "PASSED WITH WARNS" : "PASSED"}: ${summary}`);
})().catch((e) => {
  console.error("RENDER-SCAN CRASH:", e);
  process.exit(1);
});
