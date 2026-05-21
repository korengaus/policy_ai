// Phase 2 M8.1 — server-backed reviewer UI regression tests.
//
// Goal: prove the new admin section is wired in and that its pure helpers
// return the documented Korean strings without ever exposing the configured
// token. The reviewer UI module exposes a small surface via
// window.__serverReviewHelpers so we can exercise it in a vm sandbox without
// running a real browser or starting the FastAPI server.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const rootDir = path.resolve(__dirname, "..");
const htmlPath = path.join(rootDir, "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

// 1. HTML structural checks ---------------------------------------------------
const requiredIds = [
  "serverReviewToken",
  "serverReviewTokenSaveBtn",
  "serverReviewTokenClearBtn",
  "serverReviewStatusFilter",
  "serverReviewRefreshBtn",
  "serverReviewList",
  "serverReviewDetail",
  "serverReviewDetailBody",
  "serverReviewDecisionType",
  "serverReviewReviewerId",
  "serverReviewComment",
  "serverReviewPublicNote",
  "serverReviewSubmitDecisionBtn",
  "serverReviewHistory",
  // M8.2 — analysis-to-review queue bridge.
  "serverReviewRegisterCurrentBtn",
  "serverReviewRegisterStatus",
];
for (const id of requiredIds) {
  assert.ok(
    html.includes(`id="${id}"`),
    `index.html should expose the admin element id="${id}"`
  );
}

// The X-Review-Token header alias must appear in the admin panel copy so the
// reviewer knows which header is sent (the token value itself must NOT be
// hardcoded anywhere — assert below).
assert.ok(
  html.includes("X-Review-Token"),
  "admin panel should reference the X-Review-Token header by name"
);

// The disabled-API banner must surface the exact operator-facing message.
assert.ok(
  html.includes(
    "리뷰 API가 비활성화되어 있습니다. 로컬/운영 환경에서 REVIEW_API_ENABLED 설정이 필요합니다."
  ),
  "admin panel should include the deterministic disabled-API message"
);

// The four documented review decision values must appear in the decision
// dropdown so the reviewer can submit each one without manual JSON editing.
for (const decision of ["approve", "reject", "needs_more_evidence", "comment"]) {
  assert.ok(
    html.includes(`value="${decision}"`),
    `decision dropdown should offer value="${decision}"`
  );
}

// Reserved publication-side statuses must NOT appear as decision values, and
// no publish/correct endpoint may be referenced from the frontend.
for (const reserved of ["published", "corrected"]) {
  assert.ok(
    !html.includes(`value="${reserved}"`),
    `decision dropdown must not surface reserved status value="${reserved}"`
  );
}
assert.ok(
  !/\/review\/tasks\/[^"'`\s]*\/publish/.test(html),
  "frontend must not reference a /review/tasks/.../publish endpoint"
);
assert.ok(
  !/\/review\/tasks\/[^"'`\s]*\/correct/.test(html),
  "frontend must not reference a /review/tasks/.../correct endpoint"
);

// M8.2 — analysis-to-review bridge: the from-result endpoint path must
// appear verbatim, and only in the call site (no query-string token, no
// hash fragment).
assert.ok(
  html.includes("/review/tasks/from-result"),
  "frontend must call /review/tasks/from-result for the M8.2 bridge"
);
assert.ok(
  !/\/review\/tasks\/from-result[^"'`\s]*[?#]/.test(html),
  "from-result path must not embed query-string or fragment data (token must stay in header)"
);
// The M8.2 friendly-empty Korean message must remain stable.
assert.ok(
  html.includes(
    "등록할 분석 결과가 없습니다. 먼저 분석을 실행하거나 기록에서 결과를 선택하세요."
  ),
  "frontend must surface the deterministic 'no current result' message"
);

// Token storage must remain sessionStorage-only — the M8.2 bridge must
// not introduce a second token store via localStorage.
const reviewSection = html.slice(html.indexOf("serverReviewToken"));
assert.ok(
  !/localStorage[^\n]*(?:[Rr]eview|REVIEW)[^\n]*[Tt]oken/.test(reviewSection),
  "review token must not be stored in localStorage"
);

// Token must NOT be hardcoded in committed source. Heuristic: the marker
// "X-Review-Token" appears as a header alias only; common token-storage
// patterns ("Bearer <token>", literal hex strings 32+ chars next to the alias)
// are flagged here.
const hexMatch = html.match(/X-Review-Token[^\n]*[0-9a-fA-F]{32,}/);
assert.ok(
  !hexMatch,
  `index.html must not embed a literal token next to X-Review-Token; matched: ${hexMatch && hexMatch[0]}`
);
assert.ok(
  !/Bearer\s+[A-Za-z0-9._-]{20,}/.test(html),
  "index.html must not embed a Bearer token literal"
);

// 2. Pure helper checks via a vm sandbox -------------------------------------
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

function createElementStub() {
  return {
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    removeChild() {},
    setAttribute() {},
    getAttribute() { return ""; },
    select() {},
    click() {},
    remove() {},
    closest() { return null; },
    querySelector() { return createElementStub(); },
    querySelectorAll() { return []; },
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    style: {},
    dataset: {},
    hidden: false,
    value: "",
    checked: false,
    disabled: false,
    innerHTML: "",
    textContent: "",
    href: "",
    download: "",
  };
}

function createSandbox() {
  const sessionStore = new Map();
  const localStore = new Map();
  const sandbox = {
    console: { log() {}, warn() {}, error: console.error, debug() {}, info() {} },
    localStorage: {
      getItem(k) { return localStore.has(k) ? localStore.get(k) : null; },
      setItem(k, v) { localStore.set(k, String(v)); },
      removeItem(k) { localStore.delete(k); },
    },
    sessionStorage: {
      getItem(k) { return sessionStore.has(k) ? sessionStore.get(k) : null; },
      setItem(k, v) { sessionStore.set(k, String(v)); },
      removeItem(k) { sessionStore.delete(k); },
    },
    Blob: function Blob(parts) {
      this.size = (parts || []).reduce(
        (acc, p) => acc + (typeof p === "string" ? p.length : (p && p.length) || 0),
        0
      );
    },
    document: {
      getElementById() { return createElementStub(); },
      querySelector() { return createElementStub(); },
      querySelectorAll() { return []; },
      createElement() { return createElementStub(); },
      addEventListener() {},
      execCommand() { return true; },
      body: createElementStub(),
    },
    window: {
      location: { origin: "http://127.0.0.1:8000", search: "" },
      scrollTo() {},
      addEventListener() {},
      matchMedia() {
        return { matches: false, addEventListener() {}, removeEventListener() {} };
      },
    },
    navigator: { clipboard: { async writeText() {} } },
    URL: { createObjectURL() { return "blob:test"; }, revokeObjectURL() {} },
    alert() {},
    confirm() { return true; },
    fetch() { throw new Error("network disabled in regression fixtures"); },
    setTimeout() {},
    clearTimeout() {},
  };
  sandbox.window.sessionStorage = sandbox.sessionStorage;
  sandbox.window.localStorage = sandbox.localStorage;
  vm.createContext(sandbox);
  vm.runInContext(scripts.join("\n"), sandbox, { filename: "web/index.html" });
  return sandbox;
}

const sandbox = createSandbox();
const helpers = sandbox.window.__serverReviewHelpers;

assert.ok(helpers, "window.__serverReviewHelpers must be exposed for testing");
assert.strictEqual(
  helpers.disabledMessage,
  "리뷰 API가 비활성화되어 있습니다. 로컬/운영 환경에서 REVIEW_API_ENABLED 설정이 필요합니다.",
  "disabledMessage must match the documented operator-facing string"
);

assert.strictEqual(
  helpers.formatErrorMessage(503),
  helpers.disabledMessage,
  "503 must map to the disabled-API message"
);

assert.strictEqual(
  helpers.formatErrorMessage(403),
  helpers.forbiddenMessage,
  "403 must map to the forbidden message (no token detail leakage)"
);

// 403 message must NOT leak the configured token or hint at its value.
assert.ok(
  /토큰/.test(helpers.forbiddenMessage),
  "forbidden message should mention the token in the user copy"
);
assert.ok(
  !/[0-9a-fA-F]{16,}/.test(helpers.forbiddenMessage),
  "forbidden message must not embed any hex token-like literal"
);

assert.strictEqual(helpers.formatErrorMessage(404).includes("찾을 수 없"), true);
assert.strictEqual(helpers.formatErrorMessage(409).includes("적용할 수 없"), true);
assert.strictEqual(helpers.formatErrorMessage(400).includes("요청"), true);
assert.strictEqual(helpers.formatErrorMessage(0).includes("네트워크"), true);

// Status-label mapping should resolve known statuses and degrade gracefully.
assert.ok(helpers.formatStatusLabel("pending_review").includes("pending_review"));
assert.ok(helpers.formatStatusLabel("approved").includes("approved"));
assert.ok(helpers.formatStatusLabel("rejected").includes("rejected"));
assert.ok(helpers.formatStatusLabel("needs_more_evidence").includes("needs_more_evidence"));
// Unknown status falls back to the raw key (never to an empty/error label).
assert.strictEqual(helpers.formatStatusLabel("totally_unknown"), "totally_unknown");
assert.strictEqual(helpers.formatStatusLabel(""), "(없음)");

// 3. M8.2 from-result helper checks ------------------------------------------
assert.strictEqual(
  helpers.fromResultPath,
  "/review/tasks/from-result",
  "fromResultPath helper must equal the documented endpoint",
);
assert.strictEqual(
  helpers.noCurrentResultMessage,
  "등록할 분석 결과가 없습니다. 먼저 분석을 실행하거나 기록에서 결과를 선택하세요.",
  "noCurrentResultMessage must match the documented Korean copy",
);
assert.strictEqual(
  typeof helpers.buildFromResultPayload, "function",
  "buildFromResultPayload helper must be exposed for testing",
);

// Helper must be defensive against missing / nullish input.
const emptyPayload = helpers.buildFromResultPayload(null, 0);
assert.ok(emptyPayload && typeof emptyPayload === "object",
  "buildFromResultPayload(null, 0) must still return an object");
const emptyResults = emptyPayload.result_payload.result.results;
assert.ok(
  emptyResults && typeof emptyResults.length === "number" && emptyResults.length === 0,
  "buildFromResultPayload must default results to an empty list on null context",
);
assert.strictEqual(emptyPayload.item_index, 0);
assert.strictEqual(emptyPayload.job_id, null);
assert.strictEqual(emptyPayload.result_id, null);

// Helper must NOT mutate the original context — deepStrictEqual before/after.
const sourceItem = {
  result_id: 42,
  title: "테스트 제목",
  original_url: "https://example.go.kr/x",
  final_decision: { decision_label: "사실 확인 필요" },
  policy_confidence: { verification_strength: "moderate" },
  verification_card: { summary: "draft" },
  normalized_claims: [{ claim_text: "정부가 청년 보조금을 신설한다." }],
};
const context = {
  query: "전세사기",
  maxNews: 1,
  results: [sourceItem],
  analyzedAt: "2026-05-21T00:00:00Z",
};
const contextSnapshot = JSON.parse(JSON.stringify(context));
const builtPayload = helpers.buildFromResultPayload(context, 0);
assert.deepStrictEqual(
  context, contextSnapshot,
  "buildFromResultPayload must not mutate the source context",
);
// Original result item is also untouched (no rewrites of final_decision /
// policy_confidence / verification_card).
assert.deepStrictEqual(sourceItem, contextSnapshot.results[0]);

// Payload shape checks.
assert.strictEqual(builtPayload.result_id, "42",
  "result_id should be coerced to string from the focused item");
assert.strictEqual(builtPayload.item_index, 0);
assert.strictEqual(builtPayload.job_id, null,
  "job_id must be null when the frontend has no live job handle");
assert.strictEqual(builtPayload.query, "전세사기");
assert.ok(builtPayload.result_payload && typeof builtPayload.result_payload === "object");
assert.ok(Array.isArray(builtPayload.result_payload.result.results));
assert.strictEqual(builtPayload.result_payload.result.results.length, 1);
// Verdict-side fields are passed through verbatim (the server snapshot
// extractor reads them — the UI must not rewrite them).
assert.deepStrictEqual(
  builtPayload.result_payload.result.results[0].final_decision,
  { decision_label: "사실 확인 필요" },
);
assert.deepStrictEqual(
  builtPayload.result_payload.result.results[0].policy_confidence,
  { verification_strength: "moderate" },
);
assert.deepStrictEqual(
  builtPayload.result_payload.result.results[0].verification_card,
  { summary: "draft" },
);

// Out-of-range / non-integer item_index must coerce to 0 without throwing.
const oob = helpers.buildFromResultPayload(context, 99);
assert.strictEqual(oob.item_index, 0);
const nan = helpers.buildFromResultPayload(context, "abc");
assert.strictEqual(nan.item_index, 0);

// The helper must NOT introduce any semantic-truth label of its own — only
// pass through existing fields. Sample sanity check: an item without a
// semantic_evidence_summary stays without one.
assert.ok(
  !("semantic_evidence_summary" in builtPayload.result_payload.result.results[0]),
  "buildFromResultPayload must not inject semantic labels into the payload",
);
assert.ok(
  !("semantic_label" in builtPayload),
  "buildFromResultPayload must not expose a semantic label on the payload",
);

console.log("server-review UI smoke tests passed");
