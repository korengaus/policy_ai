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
// HERO-CLAMP: the hero summary is clipped by CSS, not by JS, so the check that
// guards it must read the CSS. Both constants are parsed from the files that
// OWN them — the clamp from main.css, the character budget from main.js — so
// neither can drift from the thing it polices.
const mainCss = fs.readFileSync(
  path.join(ROOT, "frontend", "styles", "main.css"), "utf8");
// TRENDING-LINEAGE-JOIN: the sidebar heading lives in the template and the
// display join lives in the backend, so both are read here — each constant
// comes from the file that owns it.
const templateHtml = fs.readFileSync(
  path.join(ROOT, "frontend", "template.html"), "utf8");
const apiServerPy = fs.readFileSync(path.join(ROOT, "api_server.py"), "utf8");
// UNGATED-FIX-GATES: binding-tail alternation, read from main.js at load.
let BINDING_TAIL_RE = null;
// SIDEBAR-TITLE-CLEANUP: marker families, read from main.js at load.
let MARKER_FAMILIES = [];
// Each family is a NAMED constant in main.js, so the read can be VERIFIED
// against the file rather than counted against a literal — see the
// SIDEBAR-TITLE-CLEANUP guard. Declared here, beside MARKER_FAMILIES, because
// leadingMarkerFamilies() is called at load (below) long before its own
// definition — a const declared next to the function would still be in its
// temporal dead zone at that call.
const LEADING_MARKER_FAMILY_SOURCES = [
  ["LEADING_TITLE_BRACKET_RE", /const LEADING_TITLE_BRACKET_RE = \/(.*?)\/;/],
  ["LEADING_TITLE_MARKER_RE", /const LEADING_TITLE_MARKER_RE = \/(.*?)\/;/],
];
// What the LAST leadingMarkerFamilies() call actually resolved: which families
// main.js still DECLARES, and which of those this scanner could not read.
let MARKER_FAMILY_DIAG = { declared: [], unreadable: [] };

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

// TITLE-TAIL-STRIP: original_url joined the columns — the title-outlet-tail
// zero class verifies a tail against the ROW'S OWN host, so a dump without
// it leaves that class dormant (helpers no-op on an empty url; the vacuity
// controls still exercise it). scripts/b2b_readiness_audit.py's RENDER_COLS
// must gain the same column for the C8 gate to run the class on real rows.
const ROW_COLUMNS = ["title", "claim_text", "content_nature", "claims",
  "normalized_claims", "evidence_snippets", "evidence_sources",
  "source_candidates", "source_reliability_summary",
  "source_reliability_reason", "evidence_summary", "debug_summary",
  "evidence_extraction_summary", "contradiction_summary",
  "contradiction_checks", "missing_context", "verdict_label",
  "policy_alert_level", "original_url"];

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
  // HERO-CLAMP: the 이/가 subject-particle discriminator, pinned in the same
  // commit that introduced it.
  "claimTailIsSubjectParticle",
  // TITLE-TAIL-STRIP: outlet-tail verification chain (title surface). Pinned
  // in the SAME commit that introduced them, per this file's own docstring —
  // the bodies are still extracted from main.js, so a stub or rename fails.
  // HERO-MARKET-SKIP: the mapper, so the gate can assert end-to-end that it
  // carries content_nature. Pinned in the same commit.
  "mapHistoryRowToResult", "parseMaybeJson",
  "outletTailApexHost", "outletTailEvidenceAdd", "stripVerifiedOutletTail",
  // TAIL-LEAK-WHOLE-CARD: per-row context + the shared-launderer strip.
  "activeOutletTailContext", "setActiveOutletTailContext",
  "stripEchoedOutletTail",
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
  // SIDEBAR-TITLE-CLEANUP: the bracket family, pinned in the same commit
  // that introduced it.
  "LEADING_TITLE_MARKER_RE", "LEADING_TITLE_BRACKET_RE",
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
  // `let` as well as `const`: mutable module state (e.g. a per-row render
  // context) is pinnable too. Without this a helper touching module state had
  // to be INLINED to get past the guard — debt this file's own docstring says
  // not to accumulate.
  return extractRange(`    const ${name} =`, ";\n")
    || extractRange(`    let ${name} =`, ";\n");
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

// TITLE-TAIL-STRIP: tail->apex evidence REGENERATED FROM THE DUMP at every
// scan, folded through the SAME pinned outletTailEvidenceAdd the browser
// uses — never stored, never hand-written. Rows without original_url (older
// dumps) contribute nothing and verify nothing, so the class goes dormant
// rather than guessing.
sandbox.__evidenceRows = Object.entries(rows)
  .map(([id, r]) => [id, r.title || "", r.original_url || ""]);
vm.runInContext(`
  __tailEvidence = {};
  for (const [id, t, u] of __evidenceRows) {
    outletTailEvidenceAdd(__tailEvidence, t, u, id);
  }
`, sandbox);

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
    original_url: row.original_url,
  };
  sandbox.__row = result;
  // TAIL-LEAK-WHOLE-CARD: mirror the app's per-row stamp (topicCardFromResult
  // and renderResults both call this before any of the row's text is
  // laundered), so the scan renders exactly what a reader gets.
  vm.runInContext("setActiveOutletTailContext(__row, __tailEvidence)", sandbox);
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
        // REBUTTAL-COUNT-RECONCILE mirror: the app summary call site now also
        // receives the checks and the join (reconciling line).
        contra: renderContradictionSummary(__contraSum, __contraChecks,
              conflictCandidateJoin(r.source_candidates, genuine))
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
      // TAIL-STRIP-EMPTY-SUMMARY: the SAME card summary rendered with the
      // row's tail context CLEARED. Comparing the two isolates one question —
      // "did the strip make text vanish?" — from the separate, pre-existing
      // rule that hides a summary which merely repeats the title. The constant
      // is the render itself: no list, no threshold, just the same function
      // run twice with the only variable being the strip.
      faceNoStrip: (() => {
        const saved = activeOutletTailContext;
        activeOutletTailContext = null;
        try { return stripCardFaceWrapper(topSummaryLine(r)); }
        finally { activeOutletTailContext = saved; }
      })(),
      // TAIL-LEAK-WHOLE-CARD: the title line is still rendered (a leak there
      // must still fail), but the tail class no longer READS this privately —
      // it scans the joined text like every other zero class. See the SURFACE
      // RULE above.
      titleShown: stripVerifiedOutletTail(
        stripLeadingTitleMarker(publicInstitutionName(r.title || "")),
        r.original_url || "", __tailEvidence),
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
// ★ SURFACE RULE — enforced below, not merely remembered.
//
// EVERY zero class reads the JOINED card text (rendered[id].text). A class
// that reads a narrower, private surface WILL pass while the same value leaks
// through the other renderers: TITLE-TAIL-STRIP's tail class read its own
// titleFace mirror, went green, and 36 of 39 tailed rows still showed the tail
// across eight sections. That is the eighth time one value has been gated on
// the one surface we happened to look at.
//
// A class needing a narrower surface must (a) name that surface in
// NARROW_SURFACE_CLASSES below and (b) state why the joined text cannot serve.
// The assertion under that table fails the run if a class narrows silently,
// so the rule is checked by the scanner rather than trusted to review.
// ---------------------------------------------------------------------------
const NARROW_SURFACE_CLASSES = {
  // The face check compares the rendered face against the SAME line rendered
  // without the budget. The joined text cannot serve: it holds neither the
  // pre-truncation string nor the section boundary the comparison needs.
  "card-face-truncation": "face vs faceFull — needs the untruncated counterpart",
};

// ---------------------------------------------------------------------------
// RECORDED NARROWING — the detail view's byte-identical rule (DETAIL-TITLE-TAIL).
//
// The detail view is held byte-identical on purpose. As of DETAIL-TITLE-TAIL
// exactly ONE string is exempt: the detail TITLE LINE (main.js, the `const
// title` in renderResults' per-result map, feeding the header anchor and
// data-share-title). It now uses the same stripVerifiedOutletTail expression
// as the card title, because the two surfaces were visibly disagreeing about
// one headline. Every other detail string — claims, snippets, sources,
// candidates, contradiction, reliability, publisher/기관·도메인 fields —
// remains byte-identical, and the 발행처 value is data, never a tail.
//
// This needs no new class: titleShown already joins rendered[id].text, so the
// existing z:title-outlet-tail scans this surface under the SURFACE RULE
// above. It simply had nothing to catch here until the strip reached it.
// Widening this exemption beyond the title line requires the same explicit
// note, so the narrowing stays visible to anyone reading scope decisions.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// TITLE-OUTLET-TAIL (whole card). Fires when this row's VERIFIED tail — the
// one its own original_url already proved — still ends a line that echoes the
// shown title, ANYWHERE in the rendered card. The shape test is the measured
// discriminator (198 title-shaped vs 62 prose/dateline/publisher-field), and
// it is the SAME predicate main.js strips with: the class re-applies
// stripEchoedOutletTail to the rendered text and fails if that changes
// anything, so scanner and app can never disagree about what counts.
// ---------------------------------------------------------------------------
function titleTailLeak(text, stripped) {
  return String(text || "") !== String(stripped || "");
}

// ---------------------------------------------------------------------------
// TAIL-STRIP-EMPTY-SUMMARY (ZERO class). A strip may SHORTEN a rendered string
// or do nothing; it may never make text disappear. Fires when the card summary
// is empty WITH the row's tail context applied but non-empty without it — i.e.
// the strip itself blanked the line. Deliberately NOT "any empty summary":
// hiding a summary that merely repeats the title is a separate, older and
// intentional rule (NARRATIVE-3B), and 8 of 213 sampled rows already collapsed
// that way before any tail work existed. A class that failed on those would be
// asserting a policy this codebase does not hold.
// ---------------------------------------------------------------------------
function strippedAwaySummary(face, faceNoStrip) {
  return !String(face || "").trim() && !!String(faceNoStrip || "").trim();
}

// ---------------------------------------------------------------------------
// HERO-CLAMP (ZERO class). The hero summary is the most prominent text on the
// site and is clipped by `-webkit-line-clamp`, which paints its ellipsis
// wherever the line happens to wrap — no JS change can move that cut. This
// scanner has no browser, so it does not try to re-measure wrapping. It asserts
// the RELATIONSHIP that was measured to hold: at the recorded character budget
// the longest hero summary in the pool occupied 2 lines at desktop and 4 at
// phone, i.e. it fits the clamp. That stays true only while the clamp does not
// SHRINK and the budget does not GROW, so those two are what is checked.
//
// Both constants are read from the files that own them, never restated here:
//   clamp  <- frontend/styles/main.css, the .topic-card--hero .topic-card-summary rule
//   budget <- frontend/scripts/main.js, CARD_FACE_MAX_CHARS
// Baselines live in card_render_baselines.json under "hero_clamp".
// ---------------------------------------------------------------------------
function heroClampFromCss(css) {
  const rule = /\.topic-card--hero\s+\.topic-card-summary\s*\{([^}]*)\}/.exec(css);
  if (!rule) return null;
  const m = /-webkit-line-clamp\s*:\s*(\d+)/.exec(rule[1]);
  return m ? Number(m[1]) : null;
}
function heroBudgetFromJs(js) {
  const m = /const\s+CARD_FACE_MAX_CHARS\s*=\s*(\d+)/.exec(js);
  return m ? Number(m[1]) : null;
}

// ---------------------------------------------------------------------------
// TRENDING-LINEAGE-JOIN (ZERO classes). The sidebar rendered 1,2,4,5 under a
// heading that says five, because a row arrived with a null representative and
// the renderer maps rank BEFORE it filters. Two assertions, both with their
// constants derived rather than typed.
//
// (1) HEADING vs SLICE. The heading's number is read from the heading string in
//     frontend/template.html; the list length is read from the slice expression
//     that bounds renderTrendingTop5 in main.js. A heading saying five while
//     the code slices four fails on the mismatch, not on a literal.
// (2) DURABLE JOIN. The api_server display join must consult the lineage id
//     BEFORE the stable_id. This is the assertion that would have caught the
//     original bug: stable_id is sha256(member set) and churns the moment a
//     cluster gains a member, which is exactly what "trending" measures, so a
//     join keyed on it silently drops the fastest-growing rows. Because the
//     renderer maps rank before filtering, a null reaching it is also what
//     produces the visible gap — so this assertion is what keeps the sequence
//     gap-free, and the gap-shape note below records that coupling.
// ---------------------------------------------------------------------------
function trendingHeadingCount(templateHtml) {
  // the sidebar panel heading, whatever number it currently names
  const m = /확산\s*성장[^<]*?(\d+)/.exec(templateHtml);
  return m ? Number(m[1]) : null;
}
function trendingSliceCount(js) {
  // Trailing "(" — same exact-anchor rule as the verdict owner: without it a
  // suffixed rename (renderTrendingTop5Legacy) still matches and this reads
  // the wrong body. The parenthesised form is already used for this very
  // function elsewhere in this file, so this only makes the two agree.
  const fn = js.indexOf("async function renderTrendingTop5(");
  if (fn < 0) return null;
  const body = js.slice(fn, fn + 4000);
  const m = /trending\s*\)\s*\?\s*body\.trending\.slice\(0,\s*(\d+)\)/.exec(body)
    || /\.slice\(0,\s*(\d+)\)/.exec(body);
  return m ? Number(m[1]) : null;
}
function trendingJoinPrefersLineage(apiPy) {
  const i = apiPy.indexOf('display.get(entry');
  if (i < 0) return null;                       // join moved or removed
  const win = apiPy.slice(Math.max(0, i - 400), i + 400);
  const lin = win.indexOf("cluster_lineage_id");
  const sid = win.indexOf("cluster_stable_id");
  return lin >= 0 && (sid < 0 || lin < sid);
}
// The renderer emits rank from the pre-filter index (map(...i+1).filter), so a
// dropped row leaves a hole. Detected here so the coupling is recorded: if this
// shape is present, assertion (2) is what must hold.
// ---------------------------------------------------------------------------
// UNGATED-FIX-GATES — three shipped fixes had no gate, which is the shape that
// produces "fixed, then found again". One check each.
//
// (1) card-face-binding-tail  ZERO. The existing card-face class asks whether a
//     cut is MARKED and whether it landed on a word boundary — and a hanging
//     particle sits exactly ON a word boundary, so reverting
//     CARD-FACE-DANGLING-TRIM passes it today. This asks the different
//     question: does the face END on a tail that binds to a following word.
//     NO SECOND COPY OF THE RULE: the two named parts are the pinned helpers
//     themselves (CLAIM_DANGLING_JOSA, claimTailIsSubjectParticle) and the
//     third is the binding-tail literal PARSED OUT OF main.js below, so editing
//     the rule in main.js moves this check with it.
// (2) grid-write-single-path  ZERO, source-shape. See the note at its call
//     site: the real assertion (the grid is written once per load) needs a
//     browser and cannot be made here.
// ---------------------------------------------------------------------------

// (set after the parser is defined; see below)

// The binding-tail alternation as it currently exists inside
// truncateCardFaceClaim. Read from source rather than restated: a drift in
// main.js changes this check instead of silently disagreeing with it.
function bindingTailReFromJs(js) {
  const fn = js.indexOf("function truncateCardFaceClaim(");
  if (fn < 0) return null;
  const body = js.slice(fn, fn + 3000);
  const m = /!\/\(\?:([^)]+)\)\$\/\.test\(tail\)/.exec(body);
  return m ? new RegExp("(?:" + m[1] + ")$") : null;
}

// The face's own trailing eojeol, stripped of the site's cut marker and of any
// non-word trailing punctuation — the same shape truncateCardFaceClaim tests.
function faceTrailingTail(face) {
  const body = String(face || "").replace(/…+$/, "").replace(/[.\s]+$/, "");
  const words = body.split(/\s+/);
  if (!words.length) return "";
  return words[words.length - 1].replace(/[^0-9a-z가-힣]+$/gi, "");
}

// (2) The grid is written through exactly one function (writeFeedGridHtml), the
// write-once guard HERO-PAINT-ORDER introduced. A regression that reintroduces
// a direct `hotTopicsEl.innerHTML =` bypasses that guard and restores the
// double paint. Counting the assignment sites is observable from source; the
// paint COUNT itself is not (see the call site).
function gridWriteSites(js) {
  return (js.match(/hotTopicsEl\.innerHTML\s*=/g) || []).length;
}

BINDING_TAIL_RE = bindingTailReFromJs(mainJs);
MARKER_FAMILIES = leadingMarkerFamilies(mainJs);

// ---------------------------------------------------------------------------
// HERO-FALLTHROUGH-DISCLOSURE (ZERO class). The hero is the first USABLE
// trending row, not the growth leader. Whenever it is not rank 1 the eyebrow
// must name the rank it actually is.
//
// The expected rank is DERIVED FROM THE PICKER, not from a literal: this
// simulation applies resolveTrendingHeroPick's own three skip conditions —
// parsed out of main.js so a change to them moves this check too — to a row
// set, takes the first survivor, and asserts the eyebrow heroBandHtml builds
// names that row's 1-based position whenever it exceeds 1.
// ---------------------------------------------------------------------------
function heroSkipRulesFromJs(js) {
  // Trailing "(" — exact anchor; see trendingSliceCount above.
  const fn = js.indexOf("async function resolveTrendingHeroPick(");
  if (fn < 0) return null;
  const body = js.slice(fn, fn + 2000);
  return {
    // the id guard and the content-class guard, read from the picker itself
    idGuard: /Number\.isInteger\(rid\)\s*&&\s*rid\s*>\s*0/.test(
      body.replace(/!Number\.isInteger\(rid\) \|\| rid <= 0/, "Number.isInteger(rid) && rid > 0"))
      || /rid <= 0/.test(body),
    skipClass: (/content_nature[\s\S]{0,40}===\s*"([a-z_]+)"/.exec(body) || [])[1] || null,
  };
}
// The eyebrow rank fragment as heroBandHtml builds it — parsed, not restated.
function heroRankNoteFromJs(js) {
  const i = js.indexOf("const heroRankNote =");
  if (i < 0) return null;
  const m = /heroRank > (\d+) \? ` \$\{heroRank\}(\S+)`/.exec(js.slice(i, i + 200));
  return m ? { above: Number(m[1]), suffix: m[2] } : null;
}
function heroEyebrowNamesRank(eyebrow, rank, note) {
  if (!note) return false;
  if (rank <= note.above) return !new RegExp("\\d+" + note.suffix).test(eyebrow);
  return eyebrow.includes(" " + rank + note.suffix);
}

// ---------------------------------------------------------------------------
// ADAPTER-FIELD-CONTRACT (ZERO class). A rule can exist, be tested by nothing,
// and never run: the hero skipped market_commercial rows for its whole life
// while the mapper dropped content_nature, so the comparison read undefined.
// Every other class here inspects RENDERED TEXT, and a rule that never fires
// produces no text — so all of them are blind to this by construction.
//
// The assertion is a RELATIONSHIP, never a field list: for each adapter, every
// field name read on an identifier assigned from that adapter must appear in
// that adapter's own return literal. Both sides are parsed from source at scan
// time and NO FIELD NAME IS TYPED ANYWHERE in this check — the read set comes
// from the consumers, the produced set from the adapter's `return {}`, and the
// built-in property names that must be ignored are taken by reflection off the
// JS prototypes rather than listed.
//
// THREE THINGS IT REFUSES TO GUESS, each reported rather than passed:
//   * a return literal containing a spread — absence cannot be concluded
//     through `...x`, so the adapter is skipped and named;
//   * an adapter with no provable holder — if nothing in the file is assigned
//     from it, there is no consumer set to derive, so it is skipped and named;
//   * an identifier assigned from the adapter but absent from its allow-entry
//     — that is FAILURE, not a skip. `record`, `item` and `result` each denote
//     several different objects in this file, so a new holder appearing must be
//     reviewed by a person instead of silently widening the read set.
// Reads are scoped to the enclosing function of each assignment, which is what
// makes the identifier reuse tractable at all; cross-function flow (an array of
// mapped rows read elsewhere) is NOT covered and is reported as such.
// ---------------------------------------------------------------------------

// allow-entry: identifiers (never field names) proven to hold each adapter's
// output. A derived holder missing from this list fails the run.
const ADAPTER_CONTRACTS = [
  { fn: "mapHistoryRowToResult", holders: ["result", "fullResult"] },
  // ADAPTER-CONTRACT-EXTEND: no identifier in this file is ever assigned from
  // buildSlimResultSummary — but its output is not unreachable either, it is
  // read back through getHistoryResults. That accessor returns whichever of
  // FOUR arrays exists (hydrated cache, response.results, results,
  // summary_results), so an element it yields may be a full server result just
  // as easily as a slim summary. Attributing those reads to THIS builder would
  // be a false pass, which is worse than the honest skip. Stays skipped, with
  // the reason sharpened from "no provable holder" to the real obstruction.
  { fn: "buildSlimResultSummary",
    skip: "consumed only through getHistoryResults, which merges four array "
      + "shapes, so a read cannot be attributed to this builder" },
  // ADAPTER-CONTRACT-EXTEND: covered through the STORAGE ROUND TRIP. There is
  // no direct holder assignment, but the consumer chain is real and every hop
  // is proven from source at scan time (see adapterRoundTripSite).
  { fn: "buildSlimHistoryRecord", holders: [], roundTrip: {
    writer: "safeWriteLocalHistory", key: "LOCAL_HISTORY_KEY",
    reader: "safeReadLocalHistory", consumer: "renderHistory", ident: "row" } },
  { fn: "buildSlimReviewItem", holders: [], roundTrip: {
    writer: "safeWriteReviewQueue", key: "REVIEW_QUEUE_KEY",
    reader: "safeReadReviewQueue", consumer: "renderReviewQueue", ident: "item" } },
  { fn: "topicCardFromResult", holders: ["card"] },
  // ADAPTER-DENOMINATOR: found by the parse, absent from this list until now —
  // the defect the measured denominator exists to catch. Its return is a bare
  // four-key literal (no spread), it is held by `metrics` at four assignment
  // sites, and `metrics` is assigned from NOTHING else in main.js, so every
  // read attributes unambiguously. It feeds renderMetrics and the homepage
  // metric tiles, where a dropped key prints an empty tile — the most prominent
  // reader surface in the product. No field name is typed here: both sides are
  // parsed, exactly as for the other holders.
  { fn: "computeMetrics", holders: ["metrics"] },
  // ADAPTER-COVER-LARGE: 21 produced keys, 8 holder sites. Attribution
  // verified by probe: every site's enclosing function assigns `state`/
  // `officialState` exactly ONCE, always from this adapter; the other user of
  // the name `state` (debugState) assigns in a different enclosing function,
  // so scoped reads cannot conflate. No merge sits upstream of any read.
  { fn: "buildOfficialEvidenceState", holders: ["state", "officialState"] },
  // ADAPTER-COVER-LARGE: 11 produced keys, 19 reads across its two consumers —
  // the operator dashboard (model.*) and the public export (reviewerModel.*).
  // Attribution verified: one assignment per scope, both from this adapter,
  // neither name assigned from anything else in those scopes.
  { fn: "buildReviewerDashboardModel", holders: ["model", "reviewerModel"] },
  // ADAPTER-COVER-LARGE: the most-consumed undeclared adapter (6 holder sites,
  // 17 reads). Its two returns DIVERGE — the error literal adds `reason` — so
  // the produced set is the UNION, disclosed as such on the covered line (see
  // UNANIMITY-OR-UNION above). Attribution verified: each of the six scopes
  // assigns `result` exactly once, always from this adapter, and every read in
  // those scopes is ok/status/body — no other `result` source is present.
  { fn: "serverReviewFetch", holders: ["result"] },
  // Declared skip, reviewed: its output is assigned to `record`, a name this
  // file also uses for localStorage history records and for the (never-passed)
  // fourth parameter of topicCardFromResult. The reads that matter sit in a
  // different function from the assignment, so no sound scope exists for it
  // here. Excluded deliberately rather than by an empty allow-entry.
  { fn: "buildLocalHistoryRecord", skip: "identifier `record` is reused across shapes" },
  // ADAPTER-CONTRACT-EXTEND, spread resolved? NO. Its return opens with
  // `...(existingItem || {})`, and existingItem is a PREVIOUSLY STORED queue
  // item read back at runtime — not a literal this file can parse. A spread of
  // a parsable object literal could be unfolded and merged into the produced
  // set; a spread of runtime state cannot, because its key set is whatever an
  // older version happened to write. Stays skipped, reason now states which
  // kind of spread it is.
  { fn: "buildReviewQueueItem",
    skip: "return spreads `existingItem`, a runtime value read back from "
      + "storage, not a parsable literal" },
];

// ADAPTER-CONTRACT-EXTEND: the round-trip declarations above name six main.js
// functions. Those names are DEPENDENCIES of this scan exactly as PINNED_DEPS
// are, so a rename must be loud here too — otherwise the chain would simply
// stop verifying. The list is DERIVED from the declarations rather than typed,
// so adding a chain pins its hops automatically and the two can never drift.
// They are pinned by PRESENCE only, deliberately not added to PINNED_DEPS:
// those get evaluated in the sandbox, and renderHistory / renderReviewQueue are
// DOM writers whose bodies this check only ever reads as source.
{
  const roundTripPinFailures = [];
  for (const c of ADAPTER_CONTRACTS) {
    if (!c.roundTrip) continue;
    for (const dep of [c.roundTrip.writer, c.roundTrip.reader, c.roundTrip.consumer]) {
      if (!extractDep(dep)) {
        roundTripPinFailures.push(`SOURCE PIN LOST: ${dep} — named by the `
          + `${c.fn} storage round-trip contract but no longer in main.js; that `
          + "chain would stop verifying silently");
      }
    }
    if (!mainJs.includes(c.roundTrip.key)) {
      roundTripPinFailures.push(`SOURCE PIN LOST: ${c.roundTrip.key} — the `
        + `storage key the ${c.fn} round trip is proven through is gone`);
    }
  }
  if (roundTripPinFailures.length) {
    for (const f of roundTripPinFailures) console.error("RENDER-SCAN FAIL:", f);
    console.error(`RENDER SCAN FAILED: ${roundTripPinFailures.length} round-trip `
      + "source pin(s) lost — fix the pins before trusting the adapter contract");
    process.exit(1);
  }
}

// built-in member names, taken by reflection — not a typed list
const BUILTIN_MEMBERS = new Set([
  ...Object.getOwnPropertyNames(Object.prototype),
  ...Object.getOwnPropertyNames(Array.prototype),
  ...Object.getOwnPropertyNames(String.prototype),
  ...Object.getOwnPropertyNames(Number.prototype),
  ...Object.getOwnPropertyNames(Function.prototype),
  ...Object.getOwnPropertyNames(Promise.prototype),
  ...Object.getOwnPropertyNames(Map.prototype),
  ...Object.getOwnPropertyNames(Set.prototype),
]);

function adapterBody(js, name) {
  for (const marker of [`    function ${name}(`, `    async function ${name}(`]) {
    const s = js.indexOf(marker);
    if (s < 0) continue;
    const e = js.indexOf("\n    }", s);
    if (e < 0) continue;
    return { start: s, end: e + 6 };
  }
  return null;
}

// ---------------------------------------------------------------------------
// ADAPTER-LITERAL-PARSE — read an object literal's keys from its BRACES, not
// from line anchors.
//
// The previous extraction matched `^\s{6,}key:` and `^\s{6,}key,$`, i.e. it
// could only see a key that sat alone on its own indented line. Nine functions
// in main.js return a SINGLE-LINE literal (`return { strength, quality };`),
// and for every one of them the old pair matched nothing and the result was an
// EMPTY key set — indistinguishable from a literal that genuinely has no keys.
// That is the dangerous direction: declaring such a function would have compared
// every read against zero produced keys and failed all of them, loudly and
// wrongly. An empty answer must never be the same value as "could not read".
//
// This scanner walks the literal with a bracket stack, skipping strings,
// template substitutions and comments, and recognises a key only in the three
// forms whose name is unambiguous in the source: `name:`, shorthand `name` /
// `name,`, and a quoted key whose content is itself a valid identifier.
// ANYTHING it cannot resolve soundly — a computed `[expr]:` key, a method
// shorthand, a numeric key, an arrow body it cannot tell from a literal — makes
// the whole parse UNPARSEABLE with a stated reason, never an approximation: an
// approximate key set produces false failures on real reads.
//
// DEPTH: keys are collected at the TOP LEVEL of the returned literal only (see
// ADAPTER-NESTED-KEYS below). The line-anchored pair this replaced had a
// 6-space floor that swept up nested keys as well, so a read of a name that
// exists ONLY inside one of the values passed the contract — coverage the gate
// did not have. Reads are matched on their FIRST path segment
// (adapterReadKeys), so a legitimate `holder.response.status` still checks
// against `response`, which is top level; nothing correct depends on the
// nested names being in this set.
//
// Returns { keys } | { spread: true } | { unparseable: reason }.
function parseObjectLiteralKeys(src, open) {
  const keys = new Set();
  const stack = ["{"];
  // ADAPTER-NESTED-KEYS: collect at the TOP LEVEL of the returned literal only.
  // A nested literal's keys are fields of one of the adapter's VALUES, not of
  // its output: for `{ a: 1, response: { status: "ok" } }` a consumer reading
  // holder.status is reading undefined, yet the all-depth set contained
  // "status" and the read passed. Nested elements are still walked (so the
  // cursor advances correctly and an unreadable nested form is still reported
  // unparseable) — they are simply not added.
  const atTopLevel = () => stack.length === 1;
  let expectKey = true;                 // at an element position of a `{`
  let i = open + 1;
  const IDENT = /[A-Za-z_$]/;
  const IDENT_RE = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
  while (i < src.length && stack.length) {
    const ch = src[i];
    if (ch === "/" && src[i + 1] === "/") {
      const nl = src.indexOf("\n", i);
      if (nl < 0) return { unparseable: "unterminated line comment" };
      i = nl + 1;
      continue;
    }
    if (ch === "/" && src[i + 1] === "*") {
      const close = src.indexOf("*/", i);
      if (close < 0) return { unparseable: "unterminated block comment" };
      i = close + 2;
      continue;
    }
    if (ch === " " || ch === "\n" || ch === "\r" || ch === "\t") { i += 1; continue; }
    const top = stack[stack.length - 1];
    if (expectKey && top === "{") {
      if (src.startsWith("...", i)) return { spread: true };
      if (ch === "}") { stack.pop(); i += 1; expectKey = false; continue; }
      if (ch === "[") return { unparseable: "computed key `[expr]:`" };
      if (ch === '"' || ch === "'" || ch === "`") {
        const q = readString(src, i);
        if (q.error) return { unparseable: q.error };
        let j = skipTrivia(src, q.end);
        if (src[j] !== ":") return { unparseable: "quoted element that is not a key" };
        if (!IDENT_RE.test(q.value)) {
          return { unparseable: `quoted key ${JSON.stringify(q.value)} is not an identifier` };
        }
        if (atTopLevel()) keys.add(q.value);
        i = j + 1;
        expectKey = false;
        continue;
      }
      if (!IDENT.test(ch)) return { unparseable: `element starting with ${JSON.stringify(ch)}` };
      let j = i;
      while (j < src.length && /[A-Za-z0-9_$]/.test(src[j])) j += 1;
      const name = src.slice(i, j);
      const k = skipTrivia(src, j);
      if (src[k] === ":") { if (atTopLevel()) keys.add(name); i = k + 1; expectKey = false; continue; }
      if (src[k] === ",") { if (atTopLevel()) keys.add(name); i = k + 1; expectKey = true; continue; }
      if (src[k] === "}") { if (atTopLevel()) keys.add(name); i = k; expectKey = false; continue; }
      if (src[k] === "(") return { unparseable: `method shorthand ${name}()` };
      return { unparseable: `element ${name} followed by ${JSON.stringify(src[k] || "EOF")}` };
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      const q = readString(src, i);
      if (q.error) return { unparseable: q.error };
      i = q.end;
      continue;
    }
    if (ch === "{" || ch === "[" || ch === "(") {
      stack.push(ch);
      // A nested `{` is treated as an object literal — the same assumption the
      // line-anchored pair made. An arrow/function body lands here too and is
      // reported unparseable rather than guessed at.
      expectKey = ch === "{";
      i += 1;
      continue;
    }
    if (ch === "}" || ch === "]" || ch === ")") {
      const want = { "}": "{", "]": "[", ")": "(" }[ch];
      if (stack[stack.length - 1] !== want) return { unparseable: "unbalanced brackets" };
      stack.pop();
      i += 1;
      expectKey = false;
      continue;
    }
    if (ch === "," && stack[stack.length - 1] === "{") { expectKey = true; i += 1; continue; }
    i += 1;
  }
  if (stack.length) return { unparseable: "literal never closes inside the function body" };
  return { keys };
}

function skipTrivia(src, i) {
  while (i < src.length) {
    const ch = src[i];
    if (ch === " " || ch === "\n" || ch === "\r" || ch === "\t") { i += 1; continue; }
    if (ch === "/" && src[i + 1] === "/") {
      const nl = src.indexOf("\n", i);
      if (nl < 0) return src.length;
      i = nl + 1;
      continue;
    }
    if (ch === "/" && src[i + 1] === "*") {
      const close = src.indexOf("*/", i);
      if (close < 0) return src.length;
      i = close + 2;
      continue;
    }
    return i;
  }
  return i;
}

// Reads one string/template literal starting at src[i]. Template substitutions
// are skipped whole so a `${obj.key}` inside can never be read as a key.
function readString(src, i) {
  const quote = src[i];
  let j = i + 1;
  let value = "";
  while (j < src.length) {
    const ch = src[j];
    if (ch === "\\") { value += src[j + 1] || ""; j += 2; continue; }
    if (ch === quote) return { value, end: j + 1 };
    if (quote === "`" && ch === "$" && src[j + 1] === "{") {
      let depth = 1;
      j += 2;
      while (j < src.length && depth) {
        if (src[j] === "{") depth += 1;
        else if (src[j] === "}") depth -= 1;
        j += 1;
      }
      continue;
    }
    value += ch;
    j += 1;
  }
  return { error: "unterminated string literal" };
}

// ADAPTER-RETURN-ANCHOR — read EVERY object-literal return, and require them to
// agree, instead of positionally trusting the last one.
//
// The old anchor was `lastIndexOf("return {")`: whichever literal happened to be
// written last won. That is a choice made by source order, not by meaning, and
// it fails in the direction that is hardest to notice — the parse SUCCEEDS and
// describes the wrong literal. serverReviewFetch is the live case: its error
// path returns four keys (…, reason) and its success path three, and the
// positional anchor silently published the three-key set as the adapter's
// contract. A consumer reading `.reason` would have been failed for reading a
// key the adapter really does produce, on the path where it matters.
//
// The rule is UNANIMITY-OR-UNION, and neither half ever picks a literal:
//   * if every object-literal return produces the same key set, that set is
//     the answer no matter which return a consumer reaches;
//   * if they DIFFER, the produced set is the UNION of the literals, marked
//     ``divergent`` so every surface that prints it says so. The union is not
//     a tiebreaker — it is exact for the defect class this gate exists to
//     catch: a failing read is one that sees undefined on EVERY path ("the
//     comparison sees undefined, so whatever rule depends on it silently does
//     not run"), and a key produced on any path is not that. serverReviewFetch
//     is the live case: its error literal carries ``reason`` and its success
//     literal does not; a consumer reading .reason reads a real value on the
//     path where it matters and must not be failed, while a consumer reading
//     a key in NEITHER literal still fails. "Most keys wins" stays rejected —
//     that WOULD pick, and would describe a function by its richer error path.
//     Path-sensitive checking (is .reason only read on the error path?) is
//     beyond a source parse and is not claimed.
//
// KNOWN RESIDUAL, not fixed here: this looks only at returns that ARE object
// literals. A function whose success path returns a non-literal (a parsed
// value, a variable) while its guard paths return `{}` — safeReadReviewerActions
// — still reports an agreed, empty set. Catching that needs each return
// attributed to its OWNING function, since inner callbacks return values too
// (computeMetrics has three such returns inside reduce/filter callbacks, and a
// naive "every return must be a literal" rule would mark that COVERED adapter
// unparseable for returns that are not its own). That attribution is a separate
// decision with its own machinery; it is deliberately not invented here.
// ---------------------------------------------------------------------------
// ADAPTER-RETURN-OWNERSHIP — attribute each `return` to the function that
// actually returns it.
//
// The collection above this change was `body.indexOf("return {")`: every
// literal in the TEXT of the body counted, including returns inside reduce/
// filter/map callbacks that belong to the callback, not the adapter
// (computeMetrics owns ONE return; its body contains four). Worse, a function
// whose own success path returns a NON-literal expression while a guard path
// returns `{}` was described by the guard alone — safeReadReviewerActions
// reported an agreed EMPTY set, confidently describing nothing.
//
// The scan walks the body once with the same string/comment discipline as
// parseObjectLiteralKeys, plus regex-literal skipping (bodies are full of
// /.../ containing quotes and braces; a literal-scan inside a return literal
// rarely meets one, a whole-body scan always does). Every `{` is tagged as a
// FUNCTION body or not when it opens:
//   * previous token `=>`                                  -> function body
//   * previous token `)` whose matching `(` is headed by the `function`
//     keyword, a `function`-preceded name, or a plain non-control identifier
//     (method / getter shorthand — `ident(){` is not legal anywhere else)    -> function body
//   * `(` headed by a control keyword (if/for/while/switch/catch), or any
//     other opener (object literal, bare block)             -> not
// A `return` counts as the adapter's OWN only at nested-function depth zero.
//
// Own returns are then classified: `{` -> literal (parsed as before);
// `;`/newline (ASI), `null`, `undefined`, or an array literal -> key-less,
// harmless (they cannot carry an identifier key, so the literal description
// stands); anything else -> the function owns a return whose shape this parse
// cannot read, and the WHOLE description is UNPARSEABLE naming it — a partial
// key set stated as the set is exactly the empty-set lie being repaired.
const OWNERSHIP_CONTROL = new Set(["if", "for", "while", "switch", "catch"]);


function skipRegexLiteral(src, i) {
  // src[i] === "/" in a regex-allowed position. Returns the index after the
  // closing "/" (flags excluded — they are plain idents and scan harmlessly),
  // or i + 1 when a newline arrives first (not a regex after all).
  let j = i + 1;
  let inClass = false;
  while (j < src.length) {
    const ch = src[j];
    if (ch === "\\") { j += 2; continue; }
    if (ch === "\n") return i + 1;
    if (ch === "[") inClass = true;
    else if (ch === "]") inClass = false;
    else if (ch === "/" && !inClass) return j + 1;
    j += 1;
  }
  return j;
}


function ownReturns(body) {
  // The scan must begin INSIDE the outer function's own body — its header
  // brace would otherwise be tagged as a nested-function frame and every own
  // return would sit at depth 1. The opener is the first `{` AFTER the
  // parameter list's matching `)`, walked with paren counting so a default
  // parameter like `context = {}` cannot be mistaken for the body opener.
  let start = body.indexOf("(");
  if (start < 0) return [];
  let depth = 0;
  while (start < body.length) {
    const c = body[start];
    if (c === '"' || c === "'" || c === "`") {
      const q = readString(body, start);
      start = q.error ? start + 1 : q.end;
      continue;
    }
    if (c === "(") depth += 1;
    else if (c === ")") { depth -= 1; if (depth === 0) { start += 1; break; } }
    start += 1;
  }
  const opener = body.indexOf("{", start);
  if (opener < 0) return [];
  const returns = [];
  const frames = [];              // "fn" | "other" per open `{`
  let fnDepth = 0;
  const parens = [];              // { head, head2 } token before each `(`
  let lastParen = null;           // info for the most recently CLOSED `(...)`
  let prev = null;                // last significant token
  let prev2 = null;
  const IDENT_START = /[A-Za-z_$]/;
  const setPrev = (tok) => { prev2 = prev; prev = tok; };
  const REGEX_ALLOWED_AFTER = new Set([
    null, "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "=>",
    "return", "typeof", "case", "in", "of", "+", "-", "*", "%", "<", ">",
  ]);
  let i = opener + 1;             // inside the outer body; frames start empty
  while (i < body.length) {
    const ch = body[i];
    if (ch === "/" && body[i + 1] === "/") {
      const nl = body.indexOf("\n", i);
      i = nl < 0 ? body.length : nl + 1;
      continue;
    }
    if (ch === "/" && body[i + 1] === "*") {
      const close = body.indexOf("*/", i);
      i = close < 0 ? body.length : close + 2;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      const q = readString(body, i);
      i = q.error ? i + 1 : q.end;
      setPrev("string");
      continue;
    }
    if (ch === "/") {
      if (REGEX_ALLOWED_AFTER.has(prev)) {
        i = skipRegexLiteral(body, i);
      } else {
        i += 1;                   // division
      }
      setPrev("/");
      continue;
    }
    if (ch === " " || ch === "\t" || ch === "\r" || ch === "\n") { i += 1; continue; }
    if (ch === "=" && body[i + 1] === ">") { setPrev("=>"); i += 2; continue; }
    if (IDENT_START.test(ch)) {
      let j = i;
      while (j < body.length && /[A-Za-z0-9_$]/.test(body[j])) j += 1;
      const word = body.slice(i, j);
      if (word === "return" && fnDepth === 0 && prev !== ".") {
        // classify what this OWN return yields; stop at a newline — ASI makes
        // `return \n expr` a bare return, and the old exact-string match
        // treated it the same way.
        let k = j;
        while (k < body.length && (body[k] === " " || body[k] === "\t")) k += 1;
        const nxt = body[k];
        if (nxt === "{") {
          returns.push({ kind: "literal", open: k });
        } else if (nxt === ";" || nxt === "\n" || nxt === undefined
                   || nxt === "[") {
          returns.push({ kind: "bare" });
        } else {
          let e = j;
          while (e < body.length && /[A-Za-z0-9_$ .]/.test(body[e])) e += 1;
          const head = body.slice(j, e).trim();
          if (head === "null" || head === "undefined") {
            returns.push({ kind: "bare" });
          } else {
            returns.push({
              kind: "expr",
              snippet: body.slice(k, k + 48).split("\n")[0],
            });
          }
        }
      }
      setPrev(word);
      i = j;
      continue;
    }
    if (ch === "(") { parens.push({ head: prev, head2: prev2 }); setPrev("("); i += 1; continue; }
    if (ch === ")") { lastParen = parens.pop() || null; setPrev(")"); i += 1; continue; }
    if (ch === "{") {
      let tag = "other";
      if (prev === "=>") tag = "fn";
      else if (prev === ")") {
        const info = lastParen || {};
        const head = info.head;
        if (head === "function" || info.head2 === "function") tag = "fn";
        else if (typeof head === "string" && /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(head)
                 && !OWNERSHIP_CONTROL.has(head)
                 && head !== "return" && head !== "typeof" && head !== "string") {
          tag = "fn";             // method / getter shorthand
        }
      }
      frames.push(tag);
      if (tag === "fn") fnDepth += 1;
      setPrev("{");
      i += 1;
      continue;
    }
    if (ch === "}") {
      const tag = frames.pop();
      if (tag === "fn") fnDepth -= 1;
      setPrev("}");
      i += 1;
      continue;
    }
    setPrev(ch);
    i += 1;
  }
  return returns;
}


function adapterProducedKeys(js, name) {
  const b = adapterBody(js, name);
  if (!b) return null;
  const body = js.slice(b.start, b.end);
  const own = ownReturns(body);
  const exprs = own.filter((r) => r.kind === "expr");
  const literalOpens = own.filter((r) => r.kind === "literal");
  if (!literalOpens.length) {
    if (exprs.length) return null;    // returns only expressions — not literal-shaped
    return null;
  }
  if (exprs.length) {
    return {
      spread: false,
      unparseable: "owns a non-literal return (`return "
        + exprs[0].snippet.trim()
        + "`) — the literal(s) do not describe every path this function can "
        + "return an object on",
    };
  }
  const parsed = literalOpens.map((r) => parseObjectLiteralKeys(body, r.open));
  if (!parsed.length) return null;
  const unreadable = parsed.find((p) => p.unparseable);
  if (unreadable) return { spread: false, unparseable: unreadable.unparseable };
  if (parsed.some((p) => p.spread)) return { spread: true };
  const signature = (p) => [...p.keys].sort().join(",");
  const signatures = [...new Set(parsed.map(signature))];
  if (signatures.length > 1) {
    const union = new Set();
    for (const p of parsed) for (const k of p.keys) union.add(k);
    return { spread: false, keys: union, divergent: signatures };
  }
  return { spread: false, keys: parsed[0].keys };
}

// identifiers assigned from the adapter, each with the function body it sits in
function adapterHolderSites(js, name) {
  const sites = [];
  const re = new RegExp(
    "(?:const|let|var)?\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*(?:await\\s+)?" + name + "\\(", "g");
  let m;
  while ((m = re.exec(js)) !== null) {
    // enclosing function body: nearest preceding top-level function opener
    let s = js.lastIndexOf("\n    function ", m.index);
    const sa = js.lastIndexOf("\n    async function ", m.index);
    if (sa > s) s = sa;
    if (s < 0) continue;
    const e = js.indexOf("\n    }", m.index);
    if (e < 0) continue;
    sites.push({ ident: m[1], start: s, end: e + 6 });
  }
  return sites;
}

// field names read on a holder inside its own assignment scope
function adapterReadKeys(js, site) {
  const scope = js.slice(site.start, site.end);
  const keys = new Set();
  const re = new RegExp("\\b" + site.ident + "\\s*(?:\\?\\.|\\.)\\s*([A-Za-z_][A-Za-z0-9_]*)", "g");
  let m;
  while ((m = re.exec(scope)) !== null) {
    const before = scope.slice(0, m.index);
    const lineStart = before.lastIndexOf("\n") + 1;
    const line = scope.slice(lineStart, scope.indexOf("\n", m.index));
    if (line.trim().startsWith("//") || line.trim().startsWith("*")) continue;
    if (!BUILTIN_MEMBERS.has(m[1])) keys.add(m[1]);
  }
  return keys;
}

// ADAPTER-CONTRACT-EXTEND: a holder proven through a STORAGE ROUND TRIP.
// Direct assignment is not the only way an adapter's output reaches a reader:
// buildSlimHistoryRecord is mapped over, JSON-serialised under a storage key,
// read back under that SAME key, and iterated by a renderer. Nothing is
// assumed — every hop below is located in main.js at scan time, and a hop that
// cannot be found is reported by name rather than quietly dropping coverage.
// Returns { site } on success or { failedHop } on the first unprovable link.
function adapterRoundTripSite(js, fn, rt) {
  const w = adapterBody(js, rt.writer);
  if (!w) return { failedHop: `writer ${rt.writer}() not found` };
  const wBody = js.slice(w.start, w.end);
  // hop 1: the writer maps the adapter over its input, serialises, and stores
  // it under the declared key.
  if (!new RegExp("\\.map\\(\\s*(?:" + fn + "\\b|\\([^)]*\\)\\s*=>\\s*" + fn + "\\()").test(wBody)) {
    return { failedHop: `${rt.writer}() no longer maps ${fn} over its input` };
  }
  if (!/JSON\.stringify\s*\(/.test(wBody)) return { failedHop: `${rt.writer}() no longer serialises` };
  if (!new RegExp("safeStorage\\.set\\(\\s*" + rt.key + "\\b").test(wBody)) {
    return { failedHop: `${rt.writer}() no longer writes ${rt.key}` };
  }
  // hop 2: the reader reads back that SAME key and parses it.
  const r = adapterBody(js, rt.reader);
  if (!r) return { failedHop: `reader ${rt.reader}() not found` };
  const rBody = js.slice(r.start, r.end);
  if (!new RegExp("safeStorage\\.get\\(\\s*" + rt.key + "\\b").test(rBody)) {
    return { failedHop: `${rt.reader}() no longer reads ${rt.key}` };
  }
  if (!/JSON\.parse\s*\(/.test(rBody)) return { failedHop: `${rt.reader}() no longer parses` };
  // hop 3: the reader's output is handed to the declared consumer.
  if (!new RegExp(rt.consumer + "\\(\\s*" + rt.reader + "\\(").test(js)) {
    return { failedHop: `${rt.consumer}(${rt.reader}()) wiring is gone` };
  }
  // hop 4: the consumer exists, and its body is the scope the reads live in.
  const c = adapterBody(js, rt.consumer);
  if (!c) return { failedHop: `consumer ${rt.consumer}() not found` };
  return { site: { ident: rt.ident, start: c.start, end: c.end } };
}

// SCHEMA DRIFT. Both readers reshape with `{ ...storedRecord, ... }`, so a
// record written by an OLDER version legitimately carries keys today's builder
// no longer produces, and a consumer may still read them on purpose. Treating
// every such read as a defect would manufacture false positives out of
// deliberate backward compatibility.
// The split is by DEFENCE, not by a list of tolerated names: a read is a
// COMPAT read when its own expression falls back — an alternation (`||`/`??`)
// that also reads a key the builder DOES produce, or that ends in a literal.
// Anything else is BARE: today's writer produces undefined there and whatever
// depends on it silently does not run, which is exactly the original class.
// Compat reads are printed, never silently dropped.
function readIsDefended(line, ident, producedKeys) {
  if (!/\|\||\?\?/.test(line)) return false;
  if (/(?:\|\||\?\?)\s*(?:"|'|`|\d|\[|\{)/.test(line)) return true;
  const re = new RegExp("\\b" + ident + "\\s*(?:\\?\\.|\\.)\\s*([A-Za-z_][A-Za-z0-9_]*)", "g");
  let m;
  while ((m = re.exec(line)) !== null) if (producedKeys.has(m[1])) return true;
  return false;
}

// the source line a given read sits on, for the defence test above
function readLine(js, site, key) {
  const scope = js.slice(site.start, site.end);
  const m = new RegExp("\\b" + site.ident + "\\s*(?:\\?\\.|\\.)\\s*" + key + "\\b").exec(scope);
  if (!m) return "";
  const s = scope.lastIndexOf("\n", m.index) + 1;
  const e = scope.indexOf("\n", m.index);
  return scope.slice(s, e < 0 ? undefined : e);
}

// ---------------------------------------------------------------------------
// ADAPTER-DENOMINATOR — how many adapters EXIST, measured, not declared.
//
// ADAPTER_CONTRACTS is hand-written, so "4 of 7" was never a count of this
// file: 7 was whatever had been remembered. computeMetrics — four keys, a
// unique holder, feeding the homepage metric tiles — sat outside it entirely
// and nothing said so. A hand-written enumeration is exactly how this project's
// enforcement loop once dropped newly added classes in silence, so the
// denominator is now parsed the same way both sides of the contract already are.
//
// ADAPTER-SHAPED, defined by the SAME three predicates the contract check needs
// before it can check anything at all:
//   1. adapterProducedKeys finds an object literal as the function's return,
//   2. adapterHolderSites finds its output assigned to at least one identifier,
//   3. adapterReadKeys finds at least one non-builtin field read on that
//      identifier inside the assignment scope.
// That is what makes the definition sound rather than a heuristic: it is not a
// second opinion about what an adapter is, it is the existing checker's own
// precondition. Anything this finds is something the gate COULD check; anything
// it misses (an output only ever mapped/passed, never held — the round-trip
// forms) could not be checked from a holder even if declared, which is why
// those carry an explicit roundTrip declaration instead.
//
// Reported, never covered: a function found here is NOT thereby audited. The
// caller discloses each undeclared one by name so a new adapter announces
// itself on its first run instead of sitting unaudited for a milestone.
function adapterShapedFunctions(js) {
  const found = [];
  const seen = new Set();
  for (const m of js.matchAll(/\n    (?:async )?function ([A-Za-z_][A-Za-z0-9_]*)\(/g)) {
    const name = m[1];
    if (seen.has(name)) continue;
    seen.add(name);
    const produced = adapterProducedKeys(js, name);
    if (!produced) continue;                       // (1) no return object literal
    const sites = adapterHolderSites(js, name);
    if (!sites.length) continue;                   // (2) never held by an identifier
    const reads = sites.reduce((n, s) => n + adapterReadKeys(js, s).size, 0);
    if (!reads) continue;                          // (3) held, but no field read
    found.push({
      name,
      idents: [...new Set(sites.map((s) => s.ident))],
      sites: sites.length,
      reads,
      // Stated per function so an undeclared line says how hard covering it
      // would be, without this file deciding that for anyone. ADAPTER-LITERAL-
      // PARSE: "0 produced key(s)" and "unparseable" are now DIFFERENT answers —
      // the first is a measurement of the literal, the second is a statement
      // about this parser. Reading them as the same value is what made nine
      // functions look empty when they are not.
      shape: produced.spread
        ? "return literal has a spread"
        : (produced.unparseable
          ? `return literal UNPARSEABLE — ${produced.unparseable}`
          : (produced.divergent
            ? `${produced.keys.size} produced key(s), UNION of `
              + `${produced.divergent.length} divergent return literals`
            : (produced.keys.size
              ? `${produced.keys.size} produced key(s)`
              : "0 produced key(s) — the literal is genuinely empty, not unread"))),
    });
  }
  return found;
}

// returns { failures[], covered[], skipped[], compat[] } — pure, so it can be
// pointed at any revision's main.js for the vacuity proof
function adapterFieldContract(js) {
  const out = { failures: [], covered: [], skipped: [], compat: [] };
  for (const c of ADAPTER_CONTRACTS) {
    if (c.skip) { out.skipped.push([c.fn, c.skip]); continue; }
    const produced = adapterProducedKeys(js, c.fn);
    if (!produced) { out.skipped.push([c.fn, "no return literal found"]); continue; }
    if (produced.spread) { out.skipped.push([c.fn, "return literal has a spread"]); continue; }
    // ADAPTER-LITERAL-PARSE: an unreadable literal is a SKIP with its reason,
    // never an empty produced set. Comparing reads against {} would fail every
    // one of them — loud, and wrong about the adapter.
    if (produced.unparseable) {
      out.skipped.push([c.fn, `return literal cannot be parsed (${produced.unparseable})`]);
      continue;
    }
    let sites = adapterHolderSites(js, c.fn);
    // ADAPTER-CONTRACT-EXTEND: fall back to the declared round trip only when
    // no direct holder exists. A DECLARED chain that no longer verifies is a
    // FAILURE, not a skip — silently losing a chain we once proved is the
    // vacuity this file exists to prevent.
    let viaRoundTrip = false;
    if (!sites.length && c.roundTrip) {
      const rt = adapterRoundTripSite(js, c.fn, c.roundTrip);
      if (rt.failedHop) {
        out.failures.push(`ADAPTER-FIELD-CONTRACT: ${c.fn} declares a storage `
          + `round-trip consumer chain that no longer verifies — ${rt.failedHop}. `
          + "Re-prove the chain or remove the declaration; leaving it would let "
          + "the adapter read as covered while nothing is checked");
        continue;
      }
      sites = [rt.site];
      viaRoundTrip = true;
    }
    if (!sites.length) { out.skipped.push([c.fn, "no provable holder assignment"]); continue; }
    const allow = new Set(viaRoundTrip ? [c.roundTrip.ident] : c.holders);
    const unexpected = [...new Set(sites.map((s) => s.ident))].filter((i) => !allow.has(i));
    if (unexpected.length) {
      out.failures.push(`ADAPTER-FIELD-CONTRACT: ${c.fn} output is assigned to `
        + `${unexpected.join(", ")}, which is not in its allow-entry. These names `
        + "denote several shapes in this file, so widen the entry deliberately "
        + "rather than letting the read set grow silently");
      continue;
    }
    let checked = 0;
    for (const site of sites) {
      for (const key of adapterReadKeys(js, site)) {
        checked += 1;
        if (produced.keys.has(key)) continue;
        // Only a round trip can legitimately carry an older version's key —
        // a directly-assigned holder holds exactly what the adapter returned,
        // so its rule is unchanged and no read is ever excused there.
        if (viaRoundTrip
            && readIsDefended(readLine(js, site, key), site.ident, produced.keys)) {
          out.compat.push([c.fn, `${site.ident}.${key}`]);
          continue;
        }
        out.failures.push(`ADAPTER-FIELD-CONTRACT: ${c.fn} never returns `
          + `"${key}", but ${site.ident}.${key} is read on its output — the `
          + "comparison sees undefined, so whatever rule depends on it "
          + "silently does not run");
      }
    }
    // divergent (union) produced-sets are carried to the covered line so the
    // disclosure names it — union coverage must never read as unanimity.
    out.covered.push([c.fn, sites.length, checked, viaRoundTrip,
                      produced.divergent || null]);
  }
  return out;
}

// ---------------------------------------------------------------------------
// SIDEBAR-TITLE-CLEANUP (ZERO class). No rendered title may still begin with a
// leading format marker. The check does not restate the pattern: it applies
// the SHIPPED stripLeadingTitleMarker and fails if the result differs from the
// input, so whatever that helper strips is what this enforces. Adding or
// removing a marker family in main.js moves this check with it.
// ---------------------------------------------------------------------------
// The marker families are read as REGEX SOURCES from main.js and applied
// directly. Calling stripLeadingTitleMarker instead would make the helper its
// own oracle: a helper that strips nothing would render every title "clean"
// and the class would be vacuously green in every direction. (That is exactly
// what the first version of this check did, and the neutered-helper probe
// caught it.) Reading the declared constants keeps the pattern out of this
// file while still giving the check an independent opinion.
function leadingMarkerFamilies(js) {
  const out = [];
  const declared = [];
  const unreadable = [];
  for (const [name, re] of LEADING_MARKER_FAMILY_SOURCES) {
    // DECLARED is asked of THE FILE, not of this scanner's own list. A family
    // main.js no longer declares has been RETIRED, and a retirement must not
    // be reported as "cannot read the regexes from main.js" — a parser failure
    // that did not happen. Only a family the file still declares, and that
    // this scanner then fails to extract or compile, is a parse failure.
    if (!new RegExp("const " + name + "\\s*=").test(js)) continue;
    declared.push(name);
    const m = re.exec(js);
    let read = false;
    if (m) {
      try { out.push(new RegExp(m[1])); read = true; } catch (e) { /* unusable */ }
    }
    if (!read) unreadable.push(name);
  }
  MARKER_FAMILY_DIAG = { declared, unreadable };
  return out;
}
function leadingMarkerSurvives(title, families) {
  const t = String(title || "");
  if (!t.trim()) return false;
  // A title that is NOTHING but a marker legitimately keeps its stored text
  // (the strip shortens or does nothing), so it is not a finding.
  for (const re of families) {
    if (re.test(t) && t.replace(re, "").trim()) return true;
  }
  return false;
}

function trendingRankMapsBeforeFilter(js) {
  // Trailing "(" — exact anchor; see trendingSliceCount above.
  const fn = js.indexOf("async function renderTrendingTop5(");
  if (fn < 0) return null;
  const body = js.slice(fn, fn + 4000);
  return /rank-num[^]*?\$\{i \+ 1\}/.test(body) && /\.filter\(Boolean\)/.test(body);
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
// TRENDING-URL-TAIL (ZERO class, WIRING form). The sidebar 확산 성장 Top 5 is
// the one title surface the per-row classes above CANNOT reach: it renders from
// GET /api/trending — snapshot cluster labels joined to a representative row —
// and this scan's dump holds analysis rows, not that payload. So there is no
// rendered[id].text for a sidebar line to join, and a per-row z: class would be
// vacuous here by construction rather than merely narrow. What IS checkable
// from source, and is exactly what failed before, is the WIRING: the renderer
// must route its title through the same pinned verifier every other surface
// uses, with the row's OWN url as the proof. Pure over `js` so it can be
// pointed at any revision — which is how the pre-change failure was shown.
function trendingTailWiring(js) {
  const out = [];
  const s = js.indexOf("    async function renderTrendingTop5(");
  if (s < 0) {
    out.push("SOURCE PIN LOST: renderTrendingTop5 — the sidebar renderer is "
      + "gone or renamed; the trending title surface is unchecked");
    return out;
  }
  const e = js.indexOf("\n    }", s);
  const body = js.slice(s, e < 0 ? undefined : e + 6);
  if (!/stripVerifiedOutletTail\(/.test(body)) {
    out.push("z:trending-title-outlet-tail: the sidebar renders a title "
      + "without stripVerifiedOutletTail — an outlet tail reaches the reader "
      + "on a surface every other title path already strips");
    return out;
  }
  if (!/stripVerifiedOutletTail\([\s\S]{0,160}?original_url/.test(body)) {
    out.push("z:trending-title-outlet-tail: the sidebar strips a tail without "
      + "verifying it against the row's OWN original_url — verification "
      + "against the row's url or nothing; no frequency or name-list rule");
  }
  return out;
}

// title-outlet-tail controls — synthetic rows, so no corpus state can make
// these vacuously pass. Both verification proofs are exercised (literal host
// match; >=2-row same-apex evidence majority) and both refusal duties too
// (a prose subtitle on a mismatched host; a single-row Korean tail).
{
  const ctl = (expr) => vm.runInContext(expr, sandbox);
  if (ctl(`stripVerifiedOutletTail(
        "정책 브리핑 자료 정리 발표 - v.daum.net", "https://v.daum.net/v/1", null)`)
      !== "정책 브리핑 자료 정리 발표") {
    failures.push("VACUOUS DETECTOR: title-outlet-tail literal (host) control "
      + "no longer strips");
  }
  if (ctl(`(() => {
        const e = {};
        outletTailEvidenceAdd(e, "가상 정책 보도 자료 하나 - 가상매체", "https://news.fakeoutlet.co.kr/a", "1");
        outletTailEvidenceAdd(e, "가상 정책 보도 자료 둘 - 가상매체", "https://m.fakeoutlet.co.kr/b", "2");
        return stripVerifiedOutletTail(
          "가상 정책 보도 자료 셋 - 가상매체", "https://fakeoutlet.co.kr/c", e);
      })()`) !== "가상 정책 보도 자료 셋") {
    failures.push("VACUOUS DETECTOR: title-outlet-tail evidence (majority) "
      + "control no longer strips");
  }
  const PROSE = "통계 밖 청년들의 기록 - 일하고 싶지만 일할 수 없는";
  if (ctl(`stripVerifiedOutletTail(${JSON.stringify(PROSE)},
        "https://news.kbs.co.kr/x", null)`) !== PROSE) {
    failures.push("OVER-EAGER DETECTOR: title-outlet-tail stripped an "
      + "unverified prose tail");
  }
  const ONCE = "가상 정책 보도 자료 넷 - 한번매체";
  if (ctl(`(() => {
        const e = {};
        outletTailEvidenceAdd(e, ${JSON.stringify(ONCE)}, "https://onceoutlet.kr/a", "1");
        return stripVerifiedOutletTail(${JSON.stringify(ONCE)}, "https://onceoutlet.kr/a", e);
      })()`) !== ONCE) {
    failures.push("OVER-EAGER DETECTOR: title-outlet-tail stripped on "
      + "single-row evidence");
  }
  // WHOLE-CARD controls: with a row context stamped, the shape test must fire
  // on a title-echoing line end and must NOT fire on the three shapes the
  // measurement said to leave alone (publisher field, wire dateline, prose
  // citation). These run on synthetic strings, so no corpus state can make
  // them vacuously pass.
  ctl(`setActiveOutletTailContext(
        { title: "정책 브리핑 자료 정리 발표 - v.daum.net",
          original_url: "https://v.daum.net/v/1" }, null)`);
  const cases = [
    ["title-echo line end", "정책 브리핑 자료 정리 발표 v.daum.net", true],
    ["publisher field", "v.daum.net", false],
    ["wire dateline", "[서울=v.daum.net] 구무서 기자 = 정책 브리핑 자료 정리 발표 관련", false],
    ["prose citation", "정책 브리핑 자료 정리 발표 v.daum.net 에 따르면 추가 확인이 필요하다", false],
  ];
  for (const [label, input, shouldStrip] of cases) {
    const got = ctl(`stripEchoedOutletTail(${JSON.stringify(input)})`);
    if (shouldStrip && got === input) {
      failures.push(`VACUOUS DETECTOR: whole-card tail strip missed the `
        + `"${label}" control`);
    }
    if (!shouldStrip && got !== input) {
      failures.push(`OVER-EAGER DETECTOR: whole-card tail strip altered the `
        + `"${label}" control`);
    }
  }
  // shorten-or-nothing controls: a string that is NOTHING BUT the tail, and a
  // short title-echo, must both come back unchanged rather than blank.
  ctl(`setActiveOutletTailContext(
        { title: "짧은제목 - v.daum.net", original_url: "https://v.daum.net/v/1" }, null)`);
  for (const input of ["v.daum.net", "짧은제목 v.daum.net"]) {
    const got = ctl(`stripEchoedOutletTail(${JSON.stringify(input)})`);
    if (!String(got || "").trim()) {
      failures.push("SHORTEN-OR-NOTHING: stripEchoedOutletTail emptied "
        + JSON.stringify(input));
    }
  }
  // the class must fire when a strip DOES blank a line, and not otherwise
  if (!strippedAwaySummary("", "본문 요약")) {
    failures.push("VACUOUS DETECTOR: summary-emptied-by-strip misses its "
      + "blanked-summary control");
  }
  if (strippedAwaySummary("", "") || strippedAwaySummary("본문", "본문 요약")) {
    failures.push("OVER-EAGER DETECTOR: summary-emptied-by-strip fires on a "
      + "summary the strip did not blank");
  }
  ctl("setActiveOutletTailContext({}, null)"); // clear before the scan

  // HERO-CLAMP: constants must be readable, and must not have moved against
  // the baseline in a direction that would let the clamp start clipping.
  const hb = (BASE.hero_clamp || {});
  const clamp = heroClampFromCss(mainCss);
  const budget = heroBudgetFromJs(mainJs);
  if (clamp === null) {
    failures.push("HERO-CLAMP: cannot read -webkit-line-clamp from "
      + "frontend/styles/main.css (.topic-card--hero .topic-card-summary) — "
      + "the rule was renamed or removed and this check is now blind");
  } else if (hb.clamp && clamp < hb.clamp) {
    failures.push(`HERO-CLAMP: clamp fell to ${clamp} from a baseline of `
      + `${hb.clamp}; the hero summary can now be clipped mid-phrase`);
  }
  if (budget === null) {
    failures.push("HERO-CLAMP: cannot read CARD_FACE_MAX_CHARS from main.js");
  } else if (hb.budget_chars && budget > hb.budget_chars) {
    failures.push(`HERO-CLAMP: card-face budget rose to ${budget} from `
      + `${hb.budget_chars} without re-measuring the clamp fit`);
  }
  // the reader must not be able to detect a clamp cut, so a face may never
  // carry the browser's own ellipsis position — ours is the only marker.
  if (heroClampFromCss(".topic-card--hero .topic-card-summary { -webkit-line-clamp: 2; }") !== 2) {
    failures.push("VACUOUS DETECTOR: heroClampFromCss cannot read its control");
  }
  if (heroBudgetFromJs("    const CARD_FACE_MAX_CHARS = 99;") !== 99) {
    failures.push("VACUOUS DETECTOR: heroBudgetFromJs cannot read its control");
  }
  // subject-particle discriminator: must accept the hero's own shape and must
  // refuse the noun tails the cut population ranked highest.
  for (const [word, want] of [["수료증이", true], ["정부가", true],
    ["평가", false], ["국가", false], ["추가", false], ["없이", false]]) {
    const got = ctl(`claimTailIsSubjectParticle(${JSON.stringify(word)})`);
    if (got !== want) {
      failures.push(`HERO-CLAMP: claimTailIsSubjectParticle(${word}) = ${got}, `
        + `expected ${want} — the 이/가 discriminator drifted`);
    }
  }

  // SIDEBAR-TITLE-CLEANUP controls: every marker family the helper claims to
  // strip must actually be caught, and a clean title must not fire. The
  // specimens are built from the helper's OWN regex sources, so a family added
  // to main.js is exercised here without being typed twice.
  {
    // VERIFIED, NOT COUNTED. This floor was `MARKER_FAMILIES.length < 2`, and
    // leadingMarkerFamilies can return at most 2 — so the floor EQUALLED the
    // maximum and no number could have worked: any legitimate retirement of a
    // family would report "cannot read the regexes from main.js", a parser
    // failure that did not happen. The fix is a different signal, not a better
    // number. main.js NAMES each family, so the read is checked against the
    // file: a family the file still declares but this scanner could not
    // extract or compile is a parse failure and is named; a family the file no
    // longer declares was retired and is silent here. The separate zero case
    // stays absolute — resolving nothing at all leaves the marker check blind
    // whatever the cause, and a count of the thing being read cannot police
    // itself.
    if (MARKER_FAMILY_DIAG.unreadable.length) {
      failures.push("SIDEBAR-TITLE-CLEANUP: main.js declares "
        + `${MARKER_FAMILY_DIAG.unreadable.join(", ")} but this scan could not `
        + "read the regex literal — the marker check is blind to that family; "
        + "the constant was reshaped, not retired");
    } else if (MARKER_FAMILIES.length === 0) {
      failures.push("SIDEBAR-TITLE-CLEANUP: no leading-marker family resolved "
        + "from main.js at all — the marker check is blind");
    }
    for (const spec of ["[포토] 국산 농산물 공급 협력 업무협약 체결",
                        "■ 불릿 마커가 붙은 정책 보도 제목"]) {
      if (!leadingMarkerSurvives(spec, MARKER_FAMILIES)) {
        failures.push("VACUOUS DETECTOR: title-leading-marker misses "
          + JSON.stringify(spec));
      }
    }
    for (const clean of ["한국거래소, 배출권거래제 정책포럼 개최",
                         "통계청, 2025년 2/4분기 가계동향조사 결과 발표"]) {
      if (leadingMarkerSurvives(clean, MARKER_FAMILIES)) {
        failures.push("OVER-EAGER DETECTOR: title-leading-marker fires on "
          + JSON.stringify(clean));
      }
    }
    // shorten-or-nothing: a title that is ONLY a marker keeps its stored text
    const only = ctl('stripLeadingTitleMarker("[포토]")');
    if (!String(only || "").trim()) {
      failures.push("SIDEBAR-TITLE-CLEANUP: a marker-only title rendered "
        + "empty — the strip must shorten or do nothing");
    }
  }

  // --- TRENDING-LINEAGE-JOIN assertions -----------------------------------
  const headN = trendingHeadingCount(templateHtml);
  const sliceN = trendingSliceCount(mainJs);
  if (headN === null) {
    failures.push("TRENDING: cannot read the 확산 성장 heading number from "
      + "frontend/template.html — the check is blind");
  } else if (sliceN === null) {
    failures.push("TRENDING: cannot read the slice bound from "
      + "renderTrendingTop5 — the check is blind");
  } else if (headN !== sliceN) {
    failures.push(`TRENDING: the sidebar heading names ${headN} but the code `
      + `slices ${sliceN} — the list cannot satisfy its own heading`);
  }
  const durable = trendingJoinPrefersLineage(apiServerPy);
  if (durable === null) {
    failures.push("TRENDING: cannot find the display join in api_server.py — "
      + "the durable-key assertion is blind");
  } else if (!durable) {
    failures.push("TRENDING: the display join reads cluster_stable_id before "
      + "cluster_lineage_id. stable_id is a hash of the member set, so it "
      + "churns exactly on the growing clusters this panel ranks — a null "
      + "representative and a gap in the sidebar numbering follow");
  }
  if (trendingRankMapsBeforeFilter(mainJs)) {
    // Not a failure on its own: the renderer is unchanged by design this
    // milestone. Recorded so the coupling is explicit — while rank is mapped
    // before the filter, the durable-join assertion above is the ONLY thing
    // keeping the sequence gap-free.
    warns.push("TRENDING NOTE: renderTrendingTop5 still numbers rows before "
      + "filtering, so any null representative reaching it leaves a hole; the "
      + "durable-join assertion is what prevents that.");
  }
  // vacuity: both parsers must read their controls, and the join check must
  // reject the pre-fix shape.
  if (trendingHeadingCount('<h2>확산 성장 Top 7</h2>') !== 7) {
    failures.push("VACUOUS DETECTOR: trendingHeadingCount cannot read its control");
  }
  if (trendingSliceCount("async function renderTrendingTop5() { body.trending.slice(0, 9) }") !== 9) {
    failures.push("VACUOUS DETECTOR: trendingSliceCount cannot read its control");
  }
  if (trendingJoinPrefersLineage('display.get(entry["cluster_stable_id"])') !== false) {
    failures.push("VACUOUS DETECTOR: trendingJoinPrefersLineage accepts the "
      + "pre-fix stable_id-only join");
  }

  // --- UNGATED-FIX-GATES ---------------------------------------------------
  // (1) binding-tail predicate must be composable and must actually fire.
  if (!BINDING_TAIL_RE) {
    failures.push("UNGATED-FIX: cannot read the binding-tail alternation from "
      + "truncateCardFaceClaim in main.js — the dangling check is blind");
  } else {
    // controls: a bound tail must be caught by SOME limb, a clean noun must not
    const bound = ["지원 규모를", "확대 및", "정책 통해", "요건 충족 시 수료증이"];
    const clean = ["교육생 모집", "감축계획 수립", "정책포럼 개최"];
    for (const s of bound) {
      if (!faceDanglingDefect("공식 자료와 추가 대조가 필요한 정책 보도 내용의 세부 사항과 관련 기관 협의 결과에 따르면 " + s + "…", "공식 자료와 추가 대조가 필요한 정책 보도 내용의 세부 사항과 관련 기관 협의 결과에 따르면 추가 확인이 필요한 세부 항목과 후속 조치 계획에 대한 설명이 이어졌다 그리고 관계 부처는 이를 재확인했다 관계 부처는 후속 일정과 세부 지침을 다시 안내할 예정이라고 밝혔다", ctl)) {
        failures.push(`VACUOUS DETECTOR: card-face-binding-tail misses "${s}"`);
      }
    }
    for (const s of clean) {
      if (faceDanglingDefect("공식 자료와 추가 대조가 필요한 정책 보도 내용의 세부 사항과 관련 기관 협의 결과에 따르면 " + s, "공식 자료와 추가 대조가 필요한 정책 보도 내용의 세부 사항과 관련 기관 협의 결과에 따르면 추가 확인이 필요한 세부 항목과 후속 조치 계획에 대한 설명이 이어졌다 그리고 관계 부처는 이를 재확인했다 관계 부처는 후속 일정과 세부 지침을 다시 안내할 예정이라고 밝혔다", ctl)) {
        failures.push(`OVER-EAGER DETECTOR: card-face-binding-tail fires on "${s}"`);
      }
    }
  }
  // (2) source-shape guard must reject its pre-fix shape.
  if (gridWriteSites("hotTopicsEl.innerHTML = a; hotTopicsEl.innerHTML = b;") !== 2) {
    failures.push("VACUOUS DETECTOR: gridWriteSites cannot count its control");
  }

  // --- HERO-FALLTHROUGH-DISCLOSURE ----------------------------------------
  const skips = heroSkipRulesFromJs(mainJs);
  const note = heroRankNoteFromJs(mainJs);
  if (!skips || !skips.skipClass) {
    failures.push("HERO-FALLTHROUGH: cannot read the skip rules from "
      + "resolveTrendingHeroPick — the rank disclosure check is blind");
  } else if (!note) {
    failures.push("HERO-FALLTHROUGH: heroBandHtml no longer builds a rank "
      + "fragment, so a hero below rank 1 renders as if it were the leader");
  } else {
    // Simulate the picker over row sets built from ITS OWN skip rules, then
    // assert the eyebrow the app would emit names the surviving row's rank.
    const eyebrow = (rank) => {
      const n = rank > note.above ? " " + rank + note.suffix : "";
      return "확산 성장" + n + " · 4개 매체 · 주간 스냅샷 기준 · 검증이 아닙니다";
    };
    const mkRows = (firstUsableAt) => Array.from({ length: 5 }, (_, i) => ({
      rid: i + 1 === firstUsableAt ? 100 + i : 0,           // id guard
      cn: i + 1 === firstUsableAt ? "government_policy" : skips.skipClass,
    }));
    const pick = (rows) => {
      for (let i = 0; i < rows.length; i += 1) {
        if (!(rows[i].rid > 0)) continue;
        if (rows[i].cn === skips.skipClass) continue;
        return i + 1;
      }
      return 0;
    };
    for (const at of [1, 2, 3, 5]) {
      const rank = pick(mkRows(at));
      if (rank !== at) {
        failures.push(`HERO-FALLTHROUGH: skip simulation disagrees with the `
          + `picker (expected rank ${at}, got ${rank})`);
      }
      if (!heroEyebrowNamesRank(eyebrow(rank), rank, note)) {
        failures.push(`HERO-FALLTHROUGH: the eyebrow does not name rank `
          + `${rank} when the hero falls through to it`);
      }
    }
    // rank 1 must stay byte-identical: no rank token at all
    if (/\d+위/.test(eyebrow(1))) {
      failures.push("HERO-FALLTHROUGH: rank 1 eyebrow gained a rank token — "
        + "the unchanged case must stay byte-identical");
    }
    // vacuity: a build that never names the rank must be caught
    if (heroEyebrowNamesRank("확산 성장 · 4개 매체", 3, note)) {
      failures.push("VACUOUS DETECTOR: heroEyebrowNamesRank accepts an "
        + "eyebrow that omits the rank");
    }

    // --- HERO-MARKET-SKIP -------------------------------------------------
    // The picker skips rows of one content class, but for its whole life the
    // mapper dropped that column, so the comparison read undefined and the
    // rule never ran. The class name is READ FROM THE PICKER (skips.skipClass)
    // rather than typed, so renaming the class moves this check with it.
    // The assertion is end-to-end through the SHIPPED mapper: a row carrying
    // the class must come out of mapHistoryRowToResult still carrying it, or
    // the skip is dead again.
    const mapped = (cn) => ctl(
      `(mapHistoryRowToResult({ id: 1, title: "t", content_nature: `
      + `${JSON.stringify(cn)} }).content_nature ?? null)`);
    if (mapped(skips.skipClass) !== skips.skipClass) {
      failures.push(`HERO-MARKET-SKIP: mapHistoryRowToResult drops `
        + `content_nature, so the picker's ${skips.skipClass} skip compares `
        + "against undefined and never fires — a row of that class can lead "
        + "the page");
    }
    // absence must stay absence: a row without the column is never skipped
    for (const empty of [null, undefined]) {
      if (mapped(empty) !== null) {
        failures.push("HERO-MARKET-SKIP: a row with no content_nature no "
          + "longer maps to null — absence must not become a skip");
      }
    }
    // vacuity: the assertion must reject a mapper that drops the field
    if (((x) => x ?? null)(undefined) === skips.skipClass) {
      failures.push("VACUOUS DETECTOR: HERO-MARKET-SKIP cannot distinguish a "
        + "dropped field from a carried one");
    }
  }

  // --- TRENDING-URL-TAIL --------------------------------------------------
  for (const f of trendingTailWiring(mainJs)) failures.push(f);
  // Non-vacuity: the same check must FIRE on a source where the sidebar does
  // not verify. The specimen is this file's own renderer with the verifier
  // call removed — no hand-written stand-in that could drift from the real one.
  {
    const s = mainJs.indexOf("    async function renderTrendingTop5(");
    if (s >= 0) {
      const neutered = mainJs.replace(/stripVerifiedOutletTail\(\s*\n?\s*marked,[\s\S]{0,120}?\);/, "marked;");
      if (neutered !== mainJs && !trendingTailWiring(neutered).length) {
        failures.push("VACUOUS DETECTOR: trending-title-outlet-tail passes a "
          + "sidebar renderer whose verifier call was removed");
      }
    }
  }

  // --- ADAPTER-FIELD-CONTRACT ---------------------------------------------
  const contract = adapterFieldContract(mainJs);
  for (const f of contract.failures) failures.push(f);
  for (const [fn, sites, checked, viaRoundTrip, divergent] of contract.covered) {
    warns.push(`ADAPTER-FIELD-CONTRACT covered: ${fn} (${sites} `
      + `${viaRoundTrip ? "storage round-trip" : "holder"} site(s), `
      + `${checked} field read(s) verified against `
      + (divergent
        ? `the UNION of ${divergent.length} divergent return literals — a `
          + "read passing here may be path-dependent; only never-produced "
          + "keys fail)"
        : "its return literal)"));
  }
  // Compat reads are stated, never swallowed: each is a key today's builder
  // does not produce, read behind a fallback, i.e. deliberate support for a
  // record an older version wrote.
  for (const [fn, read] of contract.compat) {
    warns.push(`ADAPTER-FIELD-CONTRACT compat read: ${fn} does not produce `
      + `${read}, but the read falls back to a produced key or a literal — `
      + "treated as deliberate older-schema support, not a defect");
  }
  for (const [fn, why] of contract.skipped) {
    warns.push(`ADAPTER-FIELD-CONTRACT skipped: ${fn} — ${why}. Coverage of this `
      + "class is INCOMPLETE by construction; absence of a finding here is not "
      + "evidence the adapter is clean");
  }
  // ADAPTER-DENOMINATOR: three numbers, not one. `found` is measured from the
  // scanned file; `declared` is the length of the hand-written list; `covered`
  // is what was actually checked. Only the first is evidence about main.js.
  {
    const shaped = adapterShapedFunctions(mainJs);
    const declaredNames = new Set(ADAPTER_CONTRACTS.map((c) => c.fn));
    const undeclared = shaped.filter((f) => !declaredNames.has(f.name));
    warns.push("ADAPTER-FIELD-CONTRACT denominator: "
      + `${shaped.length} adapter-shaped function(s) found by parse, `
      + `${ADAPTER_CONTRACTS.length} declared, ${contract.covered.length} covered`
      + (undeclared.length
        ? ` — ${undeclared.length} present in main.js but ABSENT from `
          + "ADAPTER_CONTRACTS, each named below"
        : " — every adapter-shaped function is declared"));
    // One line per undeclared adapter, the same shape as the skipped: lines, so
    // an adapter added next month announces itself on its first run. These are
    // DISCLOSURES, not defect signals: nothing here claims the adapter is
    // broken, only that nothing is checking it.
    for (const f of undeclared) {
      warns.push(`ADAPTER-FIELD-CONTRACT undeclared: ${f.name} (${f.shape}; held `
        + `by ${f.idents.join("/")} at ${f.sites} site(s), ${f.reads} field `
        + "read(s)) — present in main.js but absent from ADAPTER_CONTRACTS, so "
        + "NOTHING verifies those reads against its return. Not covered by this "
        + "pass; declare it deliberately or record why it cannot be");
    }
  }
  // vacuity: a synthetic adapter that drops a field its consumer reads must be
  // caught, and one that carries it must not. No field name is typed — the
  // specimen's own identifier is reused on both sides.
  {
    const bad = "    function __probeAdapter(row) {\n"
      + "      return {\n        kept: row.kept,\n      };\n    }\n"
      + "    function __probeConsumer() {\n"
      + "      const result = __probeAdapter(x);\n"
      + "      if (result.missing) return 1;\n    }\n";
    const good = bad.replace("        kept: row.kept,", "        kept: row.kept,\n        missing: row.missing,");
    const save = ADAPTER_CONTRACTS.slice();
    ADAPTER_CONTRACTS.length = 0;
    ADAPTER_CONTRACTS.push({ fn: "__probeAdapter", holders: ["result"] });
    const badRun = adapterFieldContract(bad);
    const goodRun = adapterFieldContract(good);
    ADAPTER_CONTRACTS.length = 0;
    for (const c of save) ADAPTER_CONTRACTS.push(c);
    if (!badRun.failures.length) {
      failures.push("VACUOUS DETECTOR: adapter-field-contract misses an adapter "
        + "that drops a field its consumer reads");
    }
    if (goodRun.failures.length) {
      failures.push("OVER-EAGER DETECTOR: adapter-field-contract fires on an "
        + "adapter that carries the field its consumer reads");
    }
    // an unexpected holder must FAIL, not widen the read set
    ADAPTER_CONTRACTS.length = 0;
    ADAPTER_CONTRACTS.push({ fn: "__probeAdapter", holders: [] });
    const ambiguous = adapterFieldContract(good);
    ADAPTER_CONTRACTS.length = 0;
    for (const c of save) ADAPTER_CONTRACTS.push(c);
    if (!ambiguous.failures.length) {
      failures.push("VACUOUS DETECTOR: adapter-field-contract accepts a holder "
        + "absent from its allow-entry instead of failing on the ambiguity");
    }
  }
  // ADAPTER-CONTRACT-EXTEND vacuity: the ROUND-TRIP path needs its own proof —
  // the direct-holder probe above never exercises it. A synthetic chain whose
  // builder drops a key its renderer reads BARE must fail; carrying the key
  // must pass; defending the read must be excused as compat; and a broken hop
  // must fail rather than silently stop covering. No field name is typed.
  {
    const chain = (produced, read) =>
      "    function __probeBuild(rec) {\n      return {\n        kept: rec.kept,\n"
      + (produced ? "        drifted: rec.drifted,\n" : "") + "      };\n    }\n"
      + "    function __probeWrite(rows) {\n"
      + "      const slim = (rows || []).map(__probeBuild);\n"
      + "      const s = JSON.stringify(slim);\n"
      + "      safeStorage.set(__PROBE_KEY, s);\n    }\n"
      + "    function __probeRead() {\n"
      + "      const raw = safeStorage.get(__PROBE_KEY);\n"
      + "      return JSON.parse(raw);\n    }\n"
      + "    function __probeRender(rows) {\n"
      + "      return rows.map((row) => " + read + ");\n    }\n"
      + "    function __probeBoot() {\n      __probeRender(__probeRead());\n    }\n";
    const RT = { writer: "__probeWrite", key: "__PROBE_KEY", reader: "__probeRead",
                 consumer: "__probeRender", ident: "row" };
    const save = ADAPTER_CONTRACTS.slice();
    const run = (js) => {
      ADAPTER_CONTRACTS.length = 0;
      ADAPTER_CONTRACTS.push({ fn: "__probeBuild", holders: [], roundTrip: RT });
      const r = adapterFieldContract(js);
      ADAPTER_CONTRACTS.length = 0;
      for (const c of save) ADAPTER_CONTRACTS.push(c);
      return r;
    };
    const dropped = run(chain(false, "row.drifted"));
    const carried = run(chain(true, "row.drifted"));
    const defended = run(chain(false, 'row.kept || row.drifted || "x"'));
    const brokenHop = run(chain(true, "row.drifted").replace("safeStorage.set(__PROBE_KEY, s);", ""));
    if (!dropped.failures.length) {
      failures.push("VACUOUS DETECTOR: adapter-field-contract round trip misses "
        + "a builder that drops a key its renderer reads through storage");
    }
    if (carried.failures.length || !carried.covered.length) {
      failures.push("OVER-EAGER DETECTOR: adapter-field-contract round trip "
        + "fires on a builder that carries the key its renderer reads");
    }
    if (defended.failures.length || !defended.compat.length) {
      failures.push("VACUOUS DETECTOR: adapter-field-contract round trip does "
        + "not classify a fallback-defended read as an older-schema compat read");
    }
    if (!brokenHop.failures.length) {
      failures.push("VACUOUS DETECTOR: adapter-field-contract round trip goes "
        + "quiet when a declared hop disappears instead of failing");
    }
  }

  const sites = gridWriteSites(mainJs);
  if (sites !== 1) {
    failures.push(`UNGATED-FIX grid-write-single-path: ${sites} direct `
      + "hotTopicsEl.innerHTML assignments (expected exactly 1, inside "
      + "writeFeedGridHtml). A second write path bypasses the write-once guard "
      + "and restores the double paint HERO-PAINT-ORDER removed");
  }
}

// (1) helper: does this face tail bind? Composed from the two PINNED helpers
// plus the literal parsed from main.js — never a restatement of the rule.
function faceTailBinds(tail, ctl) {
  if (!tail) return false;
  if (BINDING_TAIL_RE && BINDING_TAIL_RE.test(tail)) return true;
  return !!ctl(`(CLAIM_DANGLING_JOSA.test(${JSON.stringify(tail)}) `
    + `|| claimTailIsSubjectParticle(${JSON.stringify(tail)}))`);
}

// A face may legitimately keep a binding tail when backing off would leave less
// than CLAIM_TRIM_FLOOR_CHARS — that exemption is part of the shipped trim, not
// a defect, and it accounts for the residue CARD-FACE-DANGLING-TRIM recorded.
// The floor is the PINNED constant, read from main.js through the sandbox, so
// this cannot disagree with the trim about where the floor is.
function faceDanglingDefect(face, faceFull, ctl) {
  // SCOPE: the card-face trim only runs when the unbudgeted line EXCEEDS the
  // budget — truncateCardFaceClaim returns early otherwise. A face that was
  // already short arrived pre-cut from the claim chain (it still carries that
  // path's ASCII "..."), so a binding tail there is not this fix regressing and
  // must not be blamed on it. faceFull is the same line rendered without the
  // budget, which the scanner already computes for the truncation class.
  const budget = Number(ctl("CARD_FACE_MAX_CHARS")) || 0;
  if (String(faceFull || "").length <= budget) return false;
  const tail = faceTrailingTail(face);
  if (!faceTailBinds(tail, ctl)) return false;
  const body = String(face || "").replace(/…+$/, "").replace(/[.\s]+$/, "");
  const words = body.split(/\s+/);
  if (words.length < 2) return false;          // nothing to back off to
  const shorter = words.slice(0, -1).join(" ").replace(/[,，、·\s]+$/, "");
  const floor = Number(ctl("CLAIM_TRIM_FLOOR_CHARS")) || 0;
  return shorter.length >= floor;              // the trim COULD have backed off
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
  // TAIL-LEAK-WHOLE-CARD: the title line JOINS the scanned text, so the tail
  // class covers title + every section with one predicate.
  const joined = [out.titleShown, ...secs.map(([, v]) => v)].join("\n");
  rendered[id] = {
    text: joined, secs, nCands: out.nCands,
    faceDefect: cardFaceTruncationDefect(out.face, out.faceFull),
    faceDangling: faceDanglingDefect(out.face, out.faceFull,
      (e) => vm.runInContext(e, sandbox)),
    emptiedByStrip: strippedAwaySummary(out.face, out.faceNoStrip),
    titleShown: out.titleShown,
    titleMarker: leadingMarkerSurvives(out.titleShown, MARKER_FAMILIES),
    titleTailDefect: titleTailLeak(
      joined,
      vm.runInContext("stripEchoedOutletTail(__scanText)",
        Object.assign(sandbox, { __scanText: joined }))),
  };
}

// TAIL-LEAK-WHOLE-CARD: losing the title surface must be as loud as losing a
// section — a scan that silently stops rendering titles cannot catch tails.
if (!failures.length
    && !Object.values(rendered).some((r) => String(r.titleShown || "").trim())) {
  failures.push('SECTION BLANK: "title" rendered empty on every sampled '
    + "row — the title line left the scanned text");
}

// SURFACE RULE enforcement: every zero class must read the joined card text
// unless it is declared narrow with a stated reason.
{
  const narrowing = ["card-face-truncation", "title-outlet-tail"];
  for (const cls of narrowing) {
    const declared = Object.prototype.hasOwnProperty.call(
      NARROW_SURFACE_CLASSES, cls);
    const readsJoined = cls === "title-outlet-tail";
    if (!readsJoined && !declared) {
      failures.push(`SURFACE RULE: zero-class "${cls}" reads a private `
        + "surface without an entry in NARROW_SURFACE_CLASSES — declare the "
        + "surface and why the joined card text cannot serve");
    }
    if (readsJoined && declared) {
      failures.push(`SURFACE RULE: zero-class "${cls}" is declared narrow but `
        + "reads the joined text — remove its NARROW_SURFACE_CLASSES entry");
    }
  }
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
    if (r.faceDangling) hit(win, "z:card-face-binding-tail", id);
    if (r.titleTailDefect) hit(win, "z:title-outlet-tail", id);
    if (r.titleMarker) hit(win, "z:title-leading-marker", id);
    if (r.emptiedByStrip) hit(win, "z:summary-emptied-by-strip", id);
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
  // SIDEBAR-TITLE-CLEANUP: enforce EVERY class recorded under the "z:"
  // prefix, derived from what the scan actually emitted rather than from a
  // typed list. The old hand list silently omitted four classes
  // (title-outlet-tail, card-face-binding-tail, summary-emptied-by-strip
  // and this milestone's title-leading-marker): their hits were counted and
  // then dropped on the floor, so a real corpus row could carry the defect
  // without ever failing a run.
  const zeroSeen = new Set(Object.keys(counts[win] || {})
    .filter((k) => k.startsWith("z:")).map((k) => k.slice(2)));
  for (const [name] of ZERO) zeroSeen.add(name);
  for (const name of zeroSeen) {
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
// VERDICT-LABEL-SURFACES gate — no reader-visible surface may render the bare
// adjudication word (판정) as a LABEL. The canonical wording is OWNED by
// web/claim.html (the cold-email landing where it was first replaced): the
// renamed column header and the qualified draft_verified value are PARSED from
// that file, never typed here, so this gate cannot drift from the page it
// mirrors. Scanned sources: main.js render templates + template.html. Each
// pattern is anchored to a reader-shipping label position; operator-only
// surfaces (server review panel's 판정 기록/이력/유형, the operatorToolsFlagSet-
// gated dashboard tiles) and prose that DENIES adjudication are deliberately
// not matchable by these anchors. The test-pinned export lines
// (tests/regression.test.js requiredSections) are likewise excluded until the
// slice that co-updates those pins.
// ---------------------------------------------------------------------------
{
  const claimHtml = fs.readFileSync(path.join(ROOT, "web", "claim.html"), "utf8");
  const thm = claimHtml.match(/<th>([^<]*검증 상태[^<]*)<\/th>/);
  const dvm = claimHtml.match(/draft_verified:\s*"([^"]+)"/);
  if (!thm || !dvm) {
    failures.push("SOURCE PIN LOST: web/claim.html no longer carries the "
      + "renamed status column header and/or a draft_verified display value "
      + "— the vocabulary owner moved; re-anchor the adjudication-label gate");
  } else {
    const CANON_HEADER = thm[1];
    const CANON_DRAFT_VERIFIED = dvm[1];
    // DUAL-AXIS-CLARITY: draft_likely_true joined the pinned pair — its old
    // value was a truth claim outright, so its renamed value must stay
    // identical across surfaces exactly like draft_verified's.
    // CLAIM-PAGE-MISSING-LABELS: this was a hardcoded three-key list that
    // compared VALUES. It could not see ABSENCE — a key present in main.js but
    // missing from claim.html has no value to disagree with, so the claim page
    // silently fell back to "추가 검증 필요" (draft_unverified's display) on
    // 6,167 rows across two keys before anyone noticed. Both key sets are now
    // ENUMERATED from their own files and compared as SETS, so any key added
    // to main.js in future must appear on the claim page or this fails.
    // [:=] catches both forms: main.js sets some keys by mutation
    // (VERDICT_LABELS.key = "…"), the claim page uses an object literal.
    const verdictKeyMap = (src, blockRe) => {
      const block = src.match(blockRe);
      const out = new Map();
      if (block) {
        for (const m of block[1].matchAll(/(draft_\w+)\s*:\s*"([^"]+)"/g)) {
          out.set(m[1], m[2]);
        }
      }
      // mutation-assigned keys live outside the literal
      for (const m of src.matchAll(
        /VERDICT_LABELS\.(draft_\w+)\s*=\s*"([^"]+)"/g)) out.set(m[1], m[2]);
      return out;
    };
    const mainLabels = verdictKeyMap(
      mainJs, /const VERDICT_LABELS = \{([\s\S]*?)\n {4}\};/);
    const claimLabels = verdictKeyMap(
      claimHtml, /var VERDICT_LABELS = \{([\s\S]*?)\n {2}\};/);
    // VACUITY: ZERO, not a size — the rule follows the CONSEQUENCE of a
    // partial read, which here is already loud. The comparison below runs in
    // BOTH directions and names every key it cannot pair (MISSING STATUS KEY,
    // ORPHAN STATUS KEY) or whose value disagrees (ADJUDICATION LABEL), so a
    // side that reads 7 of 10 does not sail past — it reports the three keys
    // by name, which is strictly more useful than a vacuity failure. The old
    // floor was a hand-typed 8 against maps holding 10 (8 literal + 2 assigned
    // by mutation); retiring the two dead display-text keys would have landed
    // it exactly on 8 and turned the next legitimate retirement into a
    // reported parser failure that did not happen.
    //
    // Only ZERO is dangerous, and only ZERO fires. If BOTH sides parse empty,
    // both loops iterate nothing and the check passes in silence — the one
    // genuinely vacuous state. One side empty is not silent, but it is
    // MISDIAGNOSED (ten "the claim page has forked" failures for what is a
    // broken regex), so it is named here as the parse failure it is. A
    // VERDICT_LABELS literal that exists but holds no keys is not a state
    // either file can be in.
    //
    // STATED LIMIT: the old floor did incidentally catch one thing this does
    // not — both regexes breaking to the SAME non-empty subset, which would
    // agree and measure less than it claims. That is bounded rather than
    // eliminated: the two patterns differ in keyword, delimiter and closing
    // indentation across two files of different syntax (`const … \n    };` in
    // JS, `var … \n  };` in HTML), so they cannot plausibly fail alike, and
    // the values are compared too, not just the key sets. Tightening this
    // further would mean corroborating against the owner set, which belongs to
    // a different check — not this one's comparison to widen.
    if (mainLabels.size === 0 || claimLabels.size === 0) {
      failures.push("VACUOUS DETECTOR: verdict-label map parser read "
        + `${mainLabels.size} main.js / ${claimLabels.size} claim.html keys — `
        + [mainLabels.size ? null : "main.js",
           claimLabels.size ? null : "web/claim.html"].filter(Boolean).join(" and ")
        + " parsed to ZERO keys, so the VERDICT_LABELS literal moved or was "
        + "reshaped; this is a parser failure, not a forked surface, and the "
        + "missing-key check cannot run blind");
    } else {
      for (const [key, want] of mainLabels) {
        if (!claimLabels.has(key)) {
          failures.push(`MISSING STATUS KEY: web/claim.html has no ${key} — `
            + `main.js renders ${JSON.stringify(want)} but the claim page `
            + "falls back silently, showing a DIFFERENT stored state's label "
            + "to readers arriving from a cold email");
        } else if (claimLabels.get(key) !== want) {
          failures.push(`ADJUDICATION LABEL: main.js VERDICT_LABELS.${key} is `
            + `${JSON.stringify(want)} but the claim page ships `
            + `${JSON.stringify(claimLabels.get(key))} — one value must read `
            + "the same everywhere");
        }
      }
      for (const key of claimLabels.keys()) {
        if (!mainLabels.has(key)) {
          failures.push(`ORPHAN STATUS KEY: web/claim.html defines ${key} and `
            + "main.js does not — the two surfaces have forked");
        }
      }
    }
    const BARE_LABEL_PATTERNS = [
      [/판정 \$\{/g, "bare adjudication prefix before an interpolated value"],
      [/>AI 판정</g, "legend axis heading claims adjudication"],
      [/>판정 단계</g, "tile labels the alert level as an adjudication stage"],
      [/>AI 초안 판정</g, "tile labels the AI draft state as an adjudication"],
      [/>검증 완료</g, "legend row labels an automated provisional pass as "
        + "completed verification"],
    ];
    for (const [src, name] of [[mainJs, "main.js"],
                               [templateHtml, "template.html"]]) {
      for (const [re, why] of BARE_LABEL_PATTERNS) {
        const n = (src.match(re) || []).length;
        if (n) failures.push(`ADJUDICATION LABEL: ${name} matches ${re} `
          + `x${n} — ${why}; reuse the claim page's shipped wording `
          + `(${JSON.stringify(CANON_HEADER)} / `
          + `${JSON.stringify(CANON_DRAFT_VERIFIED)})`);
      }
    }
    // vacuity: every pattern must still fire on its pre-fix specimen.
    const CONTROL = '판정 ${x} >AI 판정< >판정 단계< >AI 초안 판정< >검증 완료<';
    for (const [re] of BARE_LABEL_PATTERNS) {
      if (!(CONTROL.match(re) || []).length) {
        failures.push(`VACUOUS DETECTOR: adjudication-label pattern ${re} `
          + "no longer matches its control");
      }
    }
  }
}

// ---------------------------------------------------------------------------
// VERDICT-STYLE-COVERAGE gate — every verdict label the BACKEND CAN ACTUALLY
// PRODUCE must have a dot colour and a tier class on the front end. The owner
// is verification_card._verdict_label's return literals, parsed here from
// verification_card.py — the SAME source the five Python pins read
// (honesty_guard, the b2b audit's LEGAL_VERDICT_LABELS, verdict_producer_
// comparison's LABEL_SEVERITY_RANK, llm_judge's LABEL_SEVERITY_RANK, main's
// _P3_TO_ALERT_TIER). No label name is typed in this scanner; both sides come
// from source, so shipping a new draft_* label fails here naming the label.
//
// DIRECTION — READ THIS BEFORE TRUSTING A PASS: this is COVERAGE, NOT
// EQUALITY. owner ⊆ map keys. A key the maps style that NO producer emits does
// NOT fail; it is DISCLOSED as a warn instead. A passing run therefore proves
// only that nothing ships unstyled — it is NOT evidence the maps are clean.
// Equality is unavailable today precisely because such keys exist (below).
//
// WHY main.js ONLY, not web/index.html: index.html is GENERATED from main.css
// + main.js by frontend/build_index.py, which records sha256 in
// frontend/dist_checksum.txt. Its copies of both maps are byte-duplicates by
// construction, so pinning them would re-measure one fact twice, and a
// main.js/index.html divergence is a STALE BUILD — a different failure with a
// different owner, already caught by the checksum path. The census undercounted
// only because this scanner never read index.html; that is a reporting gap,
// not a second thing to pin.
// ---------------------------------------------------------------------------
{
  const vcPy = fs.readFileSync(path.join(ROOT, "verification_card.py"), "utf8");
  // OWNER ANCHOR — MATCHED, THEN VERIFIED.
  //
  // This was indexOf("def _verdict_label"), a bare substring. A SUFFIXED
  // rename (_verdict_label_v2, _verdict_label_old) still matched it, so the
  // slice below would land on a DIFFERENT function and the coverage check
  // would PASS while measuring labels nobody asked about. A floor that fires
  // wrongly is noise; an anchor on the wrong body is a check that reports on
  // the wrong thing in silence, which is worse.
  //
  // SHAPE: /^def _verdict_label\(/gm — `def` must start at COLUMN 0, so a
  // method or a nested def cannot be mistaken for the module-level owner; the
  // name must be exact; and `(` must follow IMMEDIATELY, so no suffixed or
  // prefixed neighbour can match. A multi-line signature is fine — only the
  // opening paren is required, not the parameter list.
  //
  // VERIFIED, NOT TRUSTED. Python's grammar is not regular and this file is
  // read as TEXT, so no lexical rule can prove the anchor landed on the real
  // function. Rather than claim more than it delivers, it asserts the one
  // thing it can check cheaply: that EXACTLY ONE such definition exists. Zero
  // means the owner was renamed or moved; more than one means the scan cannot
  // know which definition is live. Both fail loudly instead of picking one.
  //
  // WHAT STILL DEFEATS IT, stated rather than papered over: the literal text
  // `def _verdict_label(` at column 0 inside a docstring or triple-quoted
  // string would anchor on prose, because a text scan cannot tell code from a
  // string. And an owner that KEEPS its name while delegating its return
  // literals to a helper would anchor correctly and read a subset — caught not
  // here but by the vacuity floor (if it falls to zero) and by
  // tests/test_honesty_guard.py, which binds verification_card._verdict_label
  // as an OBJECT through inspect.getsource and therefore cannot mis-anchor at
  // all. That test is corroboration, not a substitute: it runs in pytest,
  // while the b2b audit's C8 row consumes THIS scanner's verdict.
  const ownerDefs = [...vcPy.matchAll(/^def _verdict_label\(/gm)];
  // Scope to the owning function, exactly as inspect.getsource does on the
  // Python side: from its `def` to the next top-level def/class. An
  // unresolved anchor yields "" here and is named explicitly below rather
  // than being allowed to read some neighbouring function's body.
  const after = ownerDefs.length === 1 ? vcPy.slice(ownerDefs[0].index) : "";
  const nextTop = after.slice(1).search(/\n(?:def |class )/);
  const fnSrc = nextTop < 0 ? after : after.slice(0, nextTop + 1);
  const ownerLabels = new Set(
    [...fnSrc.matchAll(/return "(draft_[a-z_]+)"/g)].map((m) => m[1]));
  const styleMapKeys = (name) => {
    const block = mainJs.match(
      new RegExp("const " + name + " = \\{([\\s\\S]*?)\\n {4}\\};"));
    return new Set(block
      ? [...block[1].matchAll(/(draft_\w+)\s*:/g)].map((m) => m[1]) : []);
  };
  const STYLE_MAPS = [["VERDICT_DOT_COLORS", styleMapKeys("VERDICT_DOT_COLORS")],
                      ["VERDICT_TIER_CLASSES", styleMapKeys("VERDICT_TIER_CLASSES")]];
  // VACUITY FLOOR — DERIVED, and scoped to what it is actually for.
  //
  // It exists for ONE failure: a parse silently yielding an empty set, which
  // makes `owner ⊆ mapKeys` vacuously true, so every check below passes while
  // measuring nothing. It is NOT a rule about how large the vocabulary should
  // be. The floor used to be a hand-typed 8 on both sides; removing the two
  // dead style keys brought both maps to exactly 8, so the floor came to rest
  // ON the current count and could no longer tell "a label was legitimately
  // retired" from "the parser broke" — the next intentional vocabulary change
  // would have reported a parser failure that did not happen. Lowering the
  // literal would only re-arm the same trap further out.
  //
  // OWNER SIDE — absolute, and irreducibly so. If the owner parses to nothing,
  // the coverage loop has nothing to iterate and passes blind; and no rule
  // derived from the owner can police the owner, because a floor computed from
  // a broken parse is not a floor. So this stays absolute, at the smallest
  // honest value: at least one return literal. That fires on exactly the modes
  // it exists for — verification_card.py moved, _verdict_label renamed, or the
  // return-literal shape changed, all of which yield zero matches — and stays
  // quiet if the vocabulary legitimately shrinks to seven.
  //   The residual mode a >=1 floor CANNOT see is a PARTIAL under-read (the
  // def-to-next-def slice truncating, so 3 of 8 labels are found). That is
  // already caught, by a different mechanism in a different language:
  // tests/test_honesty_guard.py derives the same set through
  // inspect.getsource(verification_card._verdict_label) — Python's own source
  // introspection, not this scanner's text slice — and asserts set EQUALITY
  // against the five Python mirrors, so an under-read fails there naming the
  // labels. Two independent extractions of one owner; this one need not also
  // be the one that catches its own partial failure.
  //
  // MAP SIDE — derived from the owner, not typed. A style map parsing to ZERO
  // keys while the owner parsed at least one is a broken parse: a map literal
  // that exists but styles nothing is not a state the front end can be in. A
  // map parsing to FEWER keys than the owner but not zero is a REAL coverage
  // gap, and the loop below already reports it as UNSTYLED naming the label —
  // strictly more useful than a vacuity failure, so this must not pre-empt it.
  // Nothing that used to fire has gone quiet: a map at 1..7 keys now falls
  // through to that loop and still exits non-zero.
  const blindMaps = STYLE_MAPS.filter(([, keys]) => keys.size === 0);
  if (ownerLabels.size === 0 || blindMaps.length) {
    failures.push("VACUOUS DETECTOR: verdict-style parser read "
      + `${ownerLabels.size} owner label(s) from verification_card.py and `
      + STYLE_MAPS.map(([n, k]) => `${k.size} ${n}`).join(" / ")
      + " key(s) from main.js — "
      + (ownerLabels.size === 0
        ? (ownerDefs.length !== 1
          ? `OWNER ANCHOR ${ownerDefs.length ? "AMBIGUOUS" : "LOST"}: `
            + `verification_card.py has ${ownerDefs.length} module-level `
            + "`def _verdict_label(` definition(s), expected exactly 1 — "
            + (ownerDefs.length
              ? "the scan cannot know which one is live, so it refuses to "
                + "pick"
              : "the owner was renamed or moved. NOTE the anchor is exact by "
                + "design: a suffixed rename (_verdict_label_v2) used to "
                + "still match a substring anchor and slide the scan silently "
                + "onto another function's body")
          : "verification_card._verdict_label yielded NO return literals, so "
            + "the coverage check would compare against an empty owner and "
            + "pass blind; it stopped returning string literals")
        : `${blindMaps.map(([n]) => n).join(" and ")} parsed to zero keys `
          + `while the owner yielded ${ownerLabels.size}, so the map literal `
          + "moved or was reshaped — this is a parser failure, not an "
          + "unstyled label")
      + ". The floor is the owner's own count, never a typed size: a "
      + "vocabulary that legitimately shrinks does NOT trip this");
  } else {
    for (const [name, keys] of STYLE_MAPS) {
      for (const label of [...ownerLabels].sort()) {
        if (!keys.has(label)) {
          failures.push(`UNSTYLED VERDICT LABEL: main.js ${name} has no `
            + `${label}, but verification_card._verdict_label returns it — the `
            + "label ships to readers taking the map's silent fallback "
            + "(grey dot / vt-muted pill), so a real verdict state renders as "
            + "the styling of an unknown one");
        }
      }
    }
    const extras = [].concat(...STYLE_MAPS.map(([, keys]) =>
      [...keys].filter((k) => !ownerLabels.has(k))));
    const deadKeys = [...new Set(extras)].sort();
    rateLines.push("VERDICT-STYLE-COVERAGE: "
      + `${ownerLabels.size}/${ownerLabels.size} owner label(s) styled in `
      + STYLE_MAPS.map(([n, k]) => `${n} (${k.size} keys)`).join(" and ")
      + ` — COVERAGE, NOT EQUALITY: ${deadKeys.length} map key(s) that no `
      + "producer emits do NOT fail this check, so a PASS is not evidence the "
      + "maps are clean");
    if (deadKeys.length) {
      // DISCLOSURE, not a finding: styling for verdicts that cannot occur is
      // real, but removing it is a front-end edit and a separate decision.
      // Named here so it stops being forgotten — it was, until today. Carries
      // no measured window tag and no ceiling-class name, so
      // b2b_readiness_audit.classify_render_warns files it as a disclosure and
      // it cannot hold a B2B send.
      warns.push("VERDICT-STYLE-COVERAGE dead keys: main.js styles "
        + `${deadKeys.join(", ")} in `
        + STYLE_MAPS.filter(([, k]) => deadKeys.some((d) => k.has(d)))
          .map(([n]) => n).join(" and ")
        + ", but verification_card._verdict_label cannot return "
        + (deadKeys.length > 1 ? "them" : "it")
        + " — presentation for a verdict state that cannot occur. NOT a "
        + "failure: this gate is coverage-only (owner must be styled), so "
        + "extra keys are disclosed rather than gated. Deleting them is a "
        + "front-end change and a separate decision");
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
