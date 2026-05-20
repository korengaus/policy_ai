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

console.log("server-review UI smoke tests passed");
