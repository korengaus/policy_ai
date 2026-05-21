// Phase 2 M8.1 + M8.2 + M8.7 — server-backed reviewer UI regression tests.
//
// Goal: prove the new admin section is wired in and that its pure helpers
// return the documented Korean strings without ever exposing the configured
// token. The reviewer UI module exposes a small surface via
// window.__serverReviewHelpers so we can exercise it in a vm sandbox without
// running a real browser or starting the FastAPI server.
//
// M8.7 additions tightened safety:
//   * internal/admin-only wording + "게시가 아님" disclaimer
//   * no /review/* auto-fetch on init (even with a stored session token)
//   * token clear surfaces a deterministic lockout message and the helper
//     constant is exposed for assertions
//   * `published` / `corrected` removed from the UI status-label dict (they
//     remain reserved server-side, but the UI carries no display label,
//     and they remain absent from the decision dropdown)
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

// M8.7 — internal/admin-only wording must be present in the reviewer
// section so users cannot mistake the panel for a public publication tool.
const M87_REQUIRED_WORDING = [
  "관리자 전용",
  "내부 검수",
  "사람 검토 필요",
  "검수 큐 등록",
  "게시가 아님",
  "이 도구는 내부 운영자용입니다.",
];
for (const phrase of M87_REQUIRED_WORDING) {
  assert.ok(
    html.includes(phrase),
    `M8.7 admin/internal wording missing from index.html: ${phrase}`,
  );
}

// M8.7 — wording that would imply public publication, final truth, or
// auto-approval must NOT appear in the reviewer area. Scope the check
// to the server-review section so the regular policy report wording is
// untouched.
const serverReviewSectionStart = html.indexOf("serverReviewDetails");
const serverReviewSectionEnd = html.indexOf("</details>", serverReviewSectionStart);
assert.ok(
  serverReviewSectionStart > 0 && serverReviewSectionEnd > serverReviewSectionStart,
  "could not locate the server-review section bounds in index.html"
);
const serverReviewSectionHtml = html.slice(
  serverReviewSectionStart, serverReviewSectionEnd
);
for (const banned of [
  "공개 게시", "자동 게시", "최종 진실", "공식 발표",
  "자동 승인", "auto-publish", "auto_publish",
]) {
  assert.ok(
    !serverReviewSectionHtml.includes(banned),
    `server-review section must not include banned wording: ${banned}`,
  );
}

// M8.7 — the panel's <summary> label itself is the opt-in gate. Pin it
// so a future edit can't silently expand the panel by default.
assert.ok(
  /<summary>\s*내부 검수 도구 열기 \(관리자 전용\)\s*<\/summary>/.test(html),
  "server-review <details> summary must use the M8.7 internal-admin label"
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

// M8.7 — there must be exactly one X-Review-Token write-site in the
// fetch helper. We can't easily count call sites textually, but we can
// assert the helper does not also place the token in URLs, query
// strings, request bodies, or alternative auth headers.
assert.ok(
  !/[?&](?:token|review_token|x-review-token)=/i.test(html),
  "token must never appear in a URL query string"
);
assert.ok(
  !/Authorization:\s*Bearer/i.test(html),
  "reviewer fetch must not switch to an Authorization: Bearer header"
);
assert.ok(
  !/"token"\s*:\s*[A-Za-z_\$]/.test(html.slice(serverReviewSectionStart)),
  "reviewer JSON body must not carry a token field"
);

// M8.7 — published / corrected are reserved server-side. They must NOT
// appear as UI labels in the status dropdown / status chip dict. The
// decision-value check above already covers the dropdown; here we pin
// the absence of localized labels for them.
for (const reservedLabel of ["published:", "corrected:",
                             "발행됨 (예약)", "정정됨 (예약)"]) {
  assert.ok(
    !html.includes(reservedLabel),
    `reserved server status must not have a UI label: ${reservedLabel}`,
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

function createSandbox(options) {
  const opts = options || {};
  const sessionStore = new Map(opts.session ? Object.entries(opts.session) : []);
  const localStore = new Map();
  const fetchCalls = [];
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
    __sessionStore: sessionStore,
    __localStore: localStore,
    __fetchCalls: fetchCalls,
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
    fetch(input, init) {
      // Record every fetch invocation so individual tests can assert
      // that no /review/* request was fired automatically. The default
      // shape returns a benign 503-disabled-like response so any code
      // path that *does* call fetch sees a predictable rejection rather
      // than a thrown error.
      fetchCalls.push({
        url: typeof input === "string" ? input : (input && input.url) || "",
        init: init || null,
      });
      return Promise.resolve({
        ok: false, status: 503,
        async json() { return { detail: "disabled (test sandbox)" }; },
      });
    },
    setTimeout(fn) { try { fn(); } catch (_) {} return 0; },
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

// 4. M8.7 — token cleared message exposed for tests ------------------------
assert.strictEqual(
  helpers.tokenClearedMessage,
  "검수 토큰이 해제되었습니다. 서버 검수 작업을 보려면 다시 토큰을 적용해 주세요.",
  "tokenClearedMessage must match the documented Korean copy",
);

// 5. M8.7 — published / corrected absent from the UI status-label dict.
assert.ok(
  helpers.statusLabels && typeof helpers.statusLabels === "object",
  "statusLabels helper must be exposed for safety assertions",
);
assert.ok(!("published" in helpers.statusLabels),
  "UI status-label dict must not carry a label for the reserved 'published' status");
assert.ok(!("corrected" in helpers.statusLabels),
  "UI status-label dict must not carry a label for the reserved 'corrected' status");
// Unknown status (incl. the reserved ones) falls back to the raw key —
// the UI surfaces no localized "발행됨/정정됨" copy.
assert.strictEqual(helpers.formatStatusLabel("published"), "published");
assert.strictEqual(helpers.formatStatusLabel("corrected"), "corrected");

// 6. M8.7 — formatErrorMessage never echoes the token in any branch.
//    Run every numeric status it handles (plus a few unknowns) and
//    assert the rendered string carries no token-shaped literal. The
//    check intentionally excludes plain SCREAMING_SNAKE constant names
//    (e.g. REVIEW_API_ENABLED) that the disabled-API copy references —
//    real tokens are hex / base64 / random, not underscored.
const TOKEN_HEX_RE = /[0-9a-fA-F]{16,}/;
const TOKEN_BASE64_RE = /[A-Za-z0-9+/=]{24,}/;
for (const status of [0, 400, 403, 404, 409, 500, 503, 599]) {
  const message = helpers.formatErrorMessage(status);
  assert.ok(typeof message === "string" && message.length > 0,
    `formatErrorMessage(${status}) must return a non-empty string`);
  assert.ok(
    !TOKEN_HEX_RE.test(message),
    `formatErrorMessage(${status}) must not echo a hex token literal: ${message}`,
  );
  assert.ok(
    !TOKEN_BASE64_RE.test(message),
    `formatErrorMessage(${status}) must not echo a base64-looking token literal: ${message}`,
  );
}

// 7. M8.7 — no automatic /review/* fetch on page initialization.
//    Re-run the script with a token already in sessionStorage and assert
//    that no /review/tasks call was fired by init. The operator must
//    press "큐 새로고침" / "토큰 적용" / "검수 큐에 등록" explicitly.
const seededSandbox = createSandbox({
  session: { policy_ai_server_review_token: "test-session-token" },
});
const seededFetches = seededSandbox.__fetchCalls.filter(
  (c) => c.url.includes("/review/")
);
assert.strictEqual(
  seededFetches.length, 0,
  `init must not auto-fetch /review/* even with a stored token; got: ${
    JSON.stringify(seededFetches.map((c) => c.url))
  }`
);
// Also check that the regular (no-token) sandbox didn't fetch /review/*.
const baselineFetches = sandbox.__fetchCalls.filter(
  (c) => c.url.includes("/review/")
);
assert.strictEqual(baselineFetches.length, 0,
  "init must not auto-fetch /review/* when no token is stored");

// 8. M8.7 — registration safety: the no-current-result and disabled-API
//    messages remain stable. Already pinned above; here we add a
//    duplicate-registration vocabulary check so the idempotent banner
//    stays conservative.
assert.ok(
  /이미 검수 큐에 등록된 결과입니다\.\s*기존 검수 작업을 표시합니다\s*\(사람 검토 필요\)/.test(html),
  "duplicate-registration banner must use the conservative '사람 검토 필요' copy"
);
assert.ok(
  /검수 큐 등록 완료\.\s*사람 검토 대기 상태로 추가되었습니다\./.test(html),
  "fresh-registration banner must use the '사람 검토 대기' copy (no publication wording)"
);

// 9. M8.7 — the reviewer area must contain no UI affordance that says
//    "publish" / "auto-publish" / "published" / "corrected" as an
//    available action. Decision-value checks above pin the dropdown;
//    here we re-scan the server-review section text.
const PUBLICATION_BANNED = [
  "publish", "Publish",
  "auto-publish", "auto_publish",
  "발행 가능", "지금 게시", "발행 버튼",
];
for (const banned of PUBLICATION_BANNED) {
  assert.ok(
    !serverReviewSectionHtml.includes(banned),
    `server-review section must not include publication affordance: ${banned}`,
  );
}

// 10. M8.7 — semantic safety: the server-review area must not carry any
//     wording that re-labels semantic signals as user-facing truth.
for (const banned of [
  "의미 매칭 결과 게시", "의미 매칭 자동 승인",
  "AI가 사실로 판정", "AI 최종 진실",
]) {
  assert.ok(
    !serverReviewSectionHtml.includes(banned),
    `server-review section must not re-label semantic signals: ${banned}`,
  );
}

// 11. M9.0 — decision history shows audit fields (source / id /
//     reviewer / transition). Pin via the renderer's template literals
//     so any future edit that drops a field shows up as a test failure.
//     The renderer formats audit chips inline; assert each chip is in
//     the markup verbatim.
const HISTORY_RENDERER_REQUIRED = [
  "${transition}",
  "source: ${source}",
  "id: ${decisionId}",
  "리뷰어: ${reviewer}",
  "server-review-history-audit",
];
for (const fragment of HISTORY_RENDERER_REQUIRED) {
  assert.ok(
    html.includes(fragment),
    `decision history renderer must contain audit fragment: ${fragment}`,
  );
}
// The decision dropdown vocabulary remains exactly the four allowed
// decisions — pinned again here in case the M9.0 edits perturbed it.
assert.ok(
  /value="approve"[^>]*>approve/.test(html)
    && /value="reject"[^>]*>reject/.test(html)
    && /value="needs_more_evidence"[^>]*>needs_more_evidence/.test(html)
    && /value="comment"[^>]*>comment/.test(html),
  "decision dropdown must keep exactly approve / reject / needs_more_evidence / comment",
);
// No publication affordance leaked into the new history rendering.
// We only ban *affordance* wording — the M8.7 "발행 안 함" disclaimer
// elsewhere is legitimate. Decision-value checks above already pin
// the dropdown vocabulary.
const sectionFromHistory = html.slice(
  html.indexOf("serverReviewHistory"),
  html.indexOf("serverReviewBindEvents"),
);
for (const banned of [
  "auto-publish", "auto_publish",
  "published</option", "corrected</option",
  "발행 버튼", "발행 가능",
]) {
  assert.ok(
    !sectionFromHistory.includes(banned),
    `decision history block must not include publication affordance: ${banned}`,
  );
}

console.log("server-review UI smoke tests passed");
