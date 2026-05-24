// M15.0c — V2 client static + contract pins.
//
// The V2 client lives as a delimited section INSIDE
// frontend/scripts/main.js (and thus inside the built
// web/index.html); see docs/V2_ASYNC_API.md "Frontend integration"
// for why we did not promote it to a separate file in this
// milestone. This test extracts that section from the built HTML
// and asserts:
//
//   1. The delimiters are present and unique (catches accidental
//      removal during future refactors).
//   2. All M15.0b SSE event types are wired
//      (progress/status/completed/failed/timeout/unavailable/not_found).
//   3. The graceful-degradation fallback chain is wired
//      (EventSource → polling → legacy /jobs/analyze → sync /analyze).
//   4. All M15.0b stage names have Korean translations.
//   5. The analyze() handler resets the progress UI in `finally`.
//
// No browser is launched. We grep the built HTML source.
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const rootDir = path.resolve(__dirname, "..");
const htmlPath = path.join(rootDir, "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

// ---------------------------------------------------------------------------
// 1. V2 client section delimiters
// ---------------------------------------------------------------------------

const BEGIN = "// M15.0c — V2 client (begin)";
const END = "// M15.0c — V2 client (end)";
const beginIdx = html.indexOf(BEGIN);
const endIdx = html.indexOf(END);
assert.notStrictEqual(beginIdx, -1, "V2 client begin marker missing from built HTML");
assert.notStrictEqual(endIdx, -1, "V2 client end marker missing from built HTML");
assert.ok(endIdx > beginIdx, "V2 client end must come after begin");
assert.strictEqual(
  html.lastIndexOf(BEGIN), beginIdx,
  "V2 client begin marker must appear exactly once",
);
assert.strictEqual(
  html.lastIndexOf(END), endIdx,
  "V2 client end marker must appear exactly once",
);
const v2Section = html.slice(beginIdx, endIdx);

// ---------------------------------------------------------------------------
// 2. SSE event types wired
// ---------------------------------------------------------------------------

for (const eventName of [
  "progress", "status", "completed", "failed",
  "timeout", "unavailable", "not_found",
]) {
  assert.ok(
    v2Section.includes(`addEventListener("${eventName}"`),
    `V2 client must register an EventSource listener for "${eventName}"`,
  );
}

// ---------------------------------------------------------------------------
// 3. Fallback chain wired
// ---------------------------------------------------------------------------

assert.ok(
  v2Section.includes("v2PollJobUntilTerminal"),
  "V2 client must define a polling fallback (v2PollJobUntilTerminal)",
);
assert.ok(
  v2Section.includes("typeof EventSource === \"undefined\""),
  "V2 client must guard against missing EventSource",
);

// requestPolicyAnalysis (defined AFTER the V2 section) must call V2
// first, then legacy async, then sync.
const requestPolicyAnalysisMatch = html.match(
  /async function requestPolicyAnalysis\(\{ query, maxNews \}, onProgress\) \{([\s\S]*?)\n    \}/,
);
assert.ok(requestPolicyAnalysisMatch, "requestPolicyAnalysis function must exist");
const requestBody = requestPolicyAnalysisMatch[1];
const v2Idx = requestBody.indexOf("requestPolicyAnalysisV2");
const asyncIdx = requestBody.indexOf("requestPolicyAnalysisAsync");
const legacyIdx = requestBody.indexOf("requestPolicyAnalysisLegacy");
assert.notStrictEqual(v2Idx, -1, "requestPolicyAnalysis must call requestPolicyAnalysisV2");
assert.notStrictEqual(asyncIdx, -1, "requestPolicyAnalysis must call requestPolicyAnalysisAsync");
assert.notStrictEqual(legacyIdx, -1, "requestPolicyAnalysis must call requestPolicyAnalysisLegacy");
assert.ok(v2Idx < asyncIdx,
          "V2 must be tried before legacy async (fallback order)");
assert.ok(asyncIdx < legacyIdx,
          "Legacy async must be tried before sync (fallback order)");

// ---------------------------------------------------------------------------
// 4. Stage labels — all M15.0b emitted stages translated
// ---------------------------------------------------------------------------

for (const stage of [
  "queued", "pipeline_started", "saving_results",
  "completed", "failed",
]) {
  assert.ok(
    v2Section.includes(`${stage}:`),
    `V2_STAGE_LABELS_KO must include a translation for "${stage}"`,
  );
}

// Korean labels actually present
for (const koreanLabel of [
  "대기열에 등록됨", "검증 파이프라인 실행 중",
  "결과 저장 중", "완료", "실패",
]) {
  assert.ok(
    v2Section.includes(koreanLabel),
    `V2 client must include Korean label "${koreanLabel}"`,
  );
}

// ---------------------------------------------------------------------------
// 5. analyze() handler resets progress UI in finally
// ---------------------------------------------------------------------------

const analyzeMatch = html.match(
  /async function analyze\(\) \{([\s\S]*?)\n    \}/,
);
assert.ok(analyzeMatch, "analyze() function must exist");
const analyzeBody = analyzeMatch[1];
assert.ok(
  analyzeBody.includes("v2ResetProgress()"),
  "analyze() must call v2ResetProgress() in finally so the bar always hides",
);

// ---------------------------------------------------------------------------
// 6. Progress UI elements present in HTML
// ---------------------------------------------------------------------------

assert.ok(html.includes('id="v2ProgressWrap"'),
          "template.html must include the V2 progress wrap element");
assert.ok(html.includes('id="v2ProgressBar"'),
          "template.html must include the V2 progress bar element");
assert.ok(html.includes('id="v2ProgressText"'),
          "template.html must include the V2 progress text element");
assert.ok(html.includes('.v2-progress-wrap'),
          "main.css must define .v2-progress-wrap");
assert.ok(html.includes('.v2-progress-bar'),
          "main.css must define .v2-progress-bar");

// ---------------------------------------------------------------------------
// 7. No accidental modification of regression-pinned Korean phrases
// ---------------------------------------------------------------------------

// These four phrases are pinned by tests/regression.test.js. They
// live in the methodology static HTML — M15.0c must not have moved
// or altered them.
for (const pinnedPhrase of [
  "공식 후보만 있음",
  "공식기관 후보는 있으나 상세 본문 미확인",
  "의미 매칭 근거 부족",
  "사람 검토 필요",
]) {
  assert.ok(
    html.includes(pinnedPhrase),
    `Regression-pinned methodology phrase missing: "${pinnedPhrase}"`,
  );
}
assert.ok(!html.match(/methodology[\s\S]{0,2000}100%/),
          "methodology section must not include 100% certainty language");

console.log("V2 client static pins passed");
