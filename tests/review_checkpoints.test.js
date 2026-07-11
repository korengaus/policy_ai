// REVIEW-ASSIST Slice 1 — "확인 포인트" decision-support block guard tests.
//
// The block surfaces already-computed fields (disagreement_signal, official
// link, claim/evidence pairs) so the human reviews FASTER. It must NEVER
// drift into recommending approve/reject or a truth leaning — the "사람
// 검토됨" badge is only honest while the decision is 100% human. This test:
//   1. extracts the block's source (marker-delimited) from
//      frontend/scripts/main.js, executes it in a vm sandbox, and renders
//      it against fixtures;
//   2. asserts the rendered text contains NONE of the forbidden verdict
//      words (승인/기각/추천/검증/사실/거짓/참/판정), with the single
//      sanctioned exception of the descriptive phrase "판정 불일치";
//   3. scans the block's static source the same way, so copy in branches a
//      fixture misses cannot drift either;
//   4. pins the built web/index.html: block present, mounted behind
//      operatorToolsFlagSet, byte-identical to the source region.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const rootDir = path.resolve(__dirname, "..");
const mainJs = fs.readFileSync(
  path.join(rootDir, "frontend", "scripts", "main.js"), "utf8"
);
const html = fs.readFileSync(path.join(rootDir, "web", "index.html"), "utf8");

// 1. Extract the marker-delimited block source -------------------------------
const START = "// REVIEW-ASSIST-1 CHECKPOINTS START";
const END = "// REVIEW-ASSIST-1 CHECKPOINTS END";
const startIdx = mainJs.indexOf(START);
const endIdx = mainJs.indexOf(END);
assert.ok(startIdx > 0 && endIdx > startIdx,
  "REVIEW-ASSIST-1 markers must exist in frontend/scripts/main.js");
const region = mainJs.slice(mainJs.indexOf("\n", startIdx) + 1, endIdx);

// The built index.html must carry the identical region (build parity) and
// the operator_tools-gated mount next to the reviewer action card.
assert.ok(html.includes(region),
  "web/index.html must contain the identical checkpoint block (rebuild via frontend/build_index.py)");
assert.ok(
  html.includes('${operatorToolsFlagSet() ? renderReviewerCheckpoints(result) : ""}'),
  "checkpoint block must be mounted behind operatorToolsFlagSet()"
);

// 2. Execute the block in a sandbox ------------------------------------------
const sandbox = {
  escapeHtml: (v) => String(v ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;"),
  safeUrl: (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : ""),
};
vm.createContext(sandbox);
vm.runInContext(
  `${region}\n__exports = { renderReviewerCheckpoints, REVIEWER_CHECKPOINT_FORBIDDEN_WORDS, REVIEWER_CHECKPOINT_ALLOWED_PHRASE };`,
  sandbox
);
const {
  renderReviewerCheckpoints,
  REVIEWER_CHECKPOINT_FORBIDDEN_WORDS,
  REVIEWER_CHECKPOINT_ALLOWED_PHRASE,
} = sandbox.__exports;

// Spread into a local-realm array: the vm context has its own Array.prototype,
// and deepStrictEqual rejects cross-realm prototypes.
assert.deepStrictEqual(
  [...REVIEWER_CHECKPOINT_FORBIDDEN_WORDS],
  ["추천", "승인", "기각", "검증", "사실", "거짓", "참", "판정"],
  "forbidden verdict-vocab list must stay intact"
);
assert.strictEqual(REVIEWER_CHECKPOINT_ALLOWED_PHRASE, "판정 불일치");

const assertNoForbiddenVocab = (text, label) => {
  const stripped = text.split(REVIEWER_CHECKPOINT_ALLOWED_PHRASE).join("");
  for (const word of REVIEWER_CHECKPOINT_FORBIDDEN_WORDS) {
    assert.ok(
      !stripped.includes(word),
      `${label} must not contain forbidden verdict word: ${word}`
    );
  }
};

// 3. Fixture with all three checkpoints present ------------------------------
// Fixture strings are deliberately neutral (no forbidden substrings) so any
// hit comes from the block's OWN copy.
const fixture = {
  verification_card: {
    debug_summary: {
      disagreement_signal: {
        p1_label: "CAUTION", p2_label: "WATCH",
        p3_label: "uncertain", p3_implied_tier: "WATCH",
        agreed: false,
        disagreement_description: "P1=CAUTION P2=WATCH P3=uncertain(WATCH) — P1≠P2, P1≠P3",
      },
    },
    source_reliability_summary: {
      top_official_detail_url: "https://www.moel.go.kr/news/detail.do?id=1",
      top_official_detail_title: "고용노동부 보도자료 원문",
    },
    normalized_claims: ["청년 지원 예산이 내년에 늘어난다"],
    evidence_snippets: [{
      claim_index: 0,
      evidence_text: "예산안에 청년 지원 항목 증액이 포함되었다",
      source_title: "기획재정부 예산안 개요",
      source_url: "https://www.moef.go.kr/doc/2",
    }],
  },
};
const rendered = renderReviewerCheckpoints(fixture);
assert.ok(rendered.includes("확인 포인트"), "block must be titled 확인 포인트");
assert.ok(rendered.includes("내부 판정 불일치"),
  "agreed=false must surface the internal-disagreement check line");
assert.ok(rendered.includes("P1=CAUTION P2=WATCH"),
  "disagreement_description must be surfaced verbatim");
assert.ok(rendered.includes("https://www.moel.go.kr/news/detail.do?id=1"),
  "official-source URL must render as a clickable link");
assert.ok(rendered.includes('target="_blank"') && rendered.includes('rel="noopener noreferrer"'),
  "official link must open safely in a new tab");
assert.ok(rendered.includes("청년 지원 예산이 내년에 늘어난다"),
  "claim text must render");
assert.ok(rendered.includes("예산안에 청년 지원 항목 증액이 포함되었다"),
  "matched evidence text must render next to the claim");
assertNoForbiddenVocab(rendered, "rendered 확인 포인트 block");

// The block must not carry red/green semantics or verdict-confidence.
for (const banned of ["red", "green", "#f00", "#0f0", "confidence"]) {
  assert.ok(!rendered.toLowerCase().includes(banned),
    `rendered block must not carry verdict-leaning styling/field: ${banned}`);
}

// 4. Graceful empty: agreed signal, no official URL, no claims → renders "".
assert.strictEqual(
  renderReviewerCheckpoints({
    verification_card: {
      debug_summary: { disagreement_signal: { agreed: true } },
    },
  }),
  "",
  "agreed=true with nothing else to surface must render nothing"
);
assert.strictEqual(renderReviewerCheckpoints({}), "",
  "empty result must render nothing");

// 5. Static-source scan: strip the guard-list literal itself plus the
// sanctioned phrase, then no forbidden word may appear anywhere in the
// block's source — copy in rarely-hit branches cannot drift either.
const staticSource = region
  .replace(/const REVIEWER_CHECKPOINT_FORBIDDEN_WORDS = \[[^\]]*\];/, "")
  .replace(/const REVIEWER_CHECKPOINT_ALLOWED_PHRASE = "[^"]*";/, "");
assertNoForbiddenVocab(staticSource, "checkpoint block static source");

console.log("review_checkpoints.test.js: all assertions passed");
