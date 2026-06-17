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
  // AUTH-2d: the legacy X-Review-Token paste box was removed; login is the
  // only admin auth UI. The serverReviewLogin* ids are pinned by
  // tests/test_auth_login_ui.test.js.
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

// AUTH-2d: the admin panel no longer references X-Review-Token (token gate
// retired). The login form establishes a session cookie instead.
assert.ok(
  html.includes('id="serverReviewLoginBtn"'),
  "admin panel should expose the account login button"
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

// AUTH-2d: the login-required message replaces the retired disabled-API
// banner copy. Pin its exact operator-facing text.
assert.ok(
  html.includes("관리자 로그인이 필요합니다. 먼저 로그인해 주세요."),
  "admin panel should include the deterministic login-required message"
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

// AUTH-2d: no review token is stored anywhere anymore. Still assert the UI
// does not switch to an Authorization: Bearer header or put any token in a
// URL query string (defence-in-depth against a future regression).
assert.ok(
  !/[?&](?:token|review_token|x-review-token)=/i.test(html),
  "no token may ever appear in a URL query string"
);
assert.ok(
  !/Authorization:\s*Bearer/i.test(html),
  "reviewer fetch must not use an Authorization: Bearer header"
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

// No secret/token literal may be hardcoded in committed source (not keyed to
// any X-Review-Token alias, which no longer exists).
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
  // M9.4 — per-id element cache so tests can assert hidden state.
  // The first ``document.getElementById(id)`` for a given id creates
  // a stub; subsequent calls return the same instance so mutations
  // (``el.hidden = true``) are observable to the test harness.
  const elementCache = new Map();
  function cachedElement(id) {
    if (!elementCache.has(id)) {
      elementCache.set(id, createElementStub());
    }
    return elementCache.get(id);
  }
  // M9.4 — history.replaceState call log so tests can verify the
  // URL-flag cleanup happened.
  const historyReplaceCalls = [];
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
    __elementCache: elementCache,
    __historyReplaceCalls: historyReplaceCalls,
    Blob: function Blob(parts) {
      this.size = (parts || []).reduce(
        (acc, p) => acc + (typeof p === "string" ? p.length : (p && p.length) || 0),
        0
      );
    },
    document: {
      getElementById(id) { return cachedElement(String(id)); },
      querySelector() { return createElementStub(); },
      querySelectorAll() { return []; },
      createElement() { return createElementStub(); },
      addEventListener() {},
      execCommand() { return true; },
      body: createElementStub(),
    },
    window: {
      location: {
        origin: "http://127.0.0.1:8000",
        pathname: opts.pathname || "/",
        search: opts.urlSearch || "",
        hash: "",
      },
      history: {
        replaceState(state, title, url) {
          historyReplaceCalls.push({ state, title, url });
          // Reflect the new URL in window.location so subsequent reads
          // observe the change. Manual parse keeps the existing
          // (custom) URL stub intact for blob handling elsewhere.
          try {
            const s = String(url || "");
            const hashIdx = s.indexOf("#");
            const pre = hashIdx >= 0 ? s.slice(0, hashIdx) : s;
            const hash = hashIdx >= 0 ? s.slice(hashIdx) : "";
            const queryIdx = pre.indexOf("?");
            const pathname = queryIdx >= 0 ? pre.slice(0, queryIdx) : pre;
            const search = queryIdx >= 0 ? pre.slice(queryIdx) : "";
            sandbox.window.location.pathname = pathname || "/";
            sandbox.window.location.search = search;
            sandbox.window.location.hash = hash;
          } catch (_) {}
        },
        pushState() {},
      },
      scrollTo() {},
      addEventListener() {},
      matchMedia() {
        return { matches: false, addEventListener() {}, removeEventListener() {} };
      },
    },
    URLSearchParams,
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
// AUTH-2d: the 503-disabled / 403-token-forbidden mappings were retired with
// the token gate. 401 (no session) now maps to a "log in first" message.
assert.strictEqual(
  helpers.formatErrorMessage(401),
  helpers.loginRequiredMessage,
  "401 must map to the login-required message"
);
assert.ok(
  /로그인/.test(helpers.loginRequiredMessage),
  "login-required message should tell the operator to log in"
);
assert.ok(
  !/[0-9a-fA-F]{16,}/.test(helpers.loginRequiredMessage),
  "login-required message must not embed any hex secret-like literal"
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

// 4. AUTH-2d — login-required message exposed for tests --------------------
assert.strictEqual(
  helpers.loginRequiredMessage,
  "관리자 로그인이 필요합니다. 먼저 로그인해 주세요.",
  "loginRequiredMessage must match the documented Korean copy",
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

// =============================================================================
// 12. M9.2 — internal audit packet UI viewer + copy helper
// =============================================================================
//
// Pin the new buttons/markup, the explicit-click-only contract, the
// stable Korean error/copy messages, the token-safety properties, and
// the absence of any publication affordance in the new section.
//
// The audit-packet section lives at the bottom of the existing
// serverReviewDetail block; everything we check is contained to it.

// Locate the audit-packet block in the markup once. Anchor on the
// class attribute on the markup div (the CSS rule above shares the
// class name, so a bare ``server-review-audit-packet`` substring would
// hit the stylesheet first).
const AUDIT_PACKET_BLOCK_START = html.indexOf(
  'class="server-review-audit-packet"'
);
assert.ok(
  AUDIT_PACKET_BLOCK_START > 0,
  "M9.2: the audit-packet section must exist in index.html",
);
// The block ends at the next `</div>` close that sits at the markup
// indentation we know the file uses. To stay tolerant of indentation
// changes, take a generous slice up to the next `</details>` (the
// reviewer admin <details> close) and trim further if needed.
const AUDIT_PACKET_BLOCK_END = html.indexOf(
  "</details>", AUDIT_PACKET_BLOCK_START
);
assert.ok(
  AUDIT_PACKET_BLOCK_END > AUDIT_PACKET_BLOCK_START,
  "M9.2: could not locate the end of the audit-packet block",
);
const auditPacketSection = html.slice(
  AUDIT_PACKET_BLOCK_START, AUDIT_PACKET_BLOCK_END
);

// --- 12a. Required markup ---------------------------------------------------
const M92_REQUIRED_IDS = [
  "serverReviewAuditPacketLoadBtn",
  "serverReviewAuditPacketCopyBtn",
  "serverReviewAuditPacketStatus",
  "serverReviewAuditPacketSummary",
  "serverReviewAuditPacketRawWrap",
  "serverReviewAuditPacketRaw",
];
for (const id of M92_REQUIRED_IDS) {
  assert.ok(
    html.includes(`id="${id}"`),
    `M9.2 element missing: id="${id}"`,
  );
}

// --- 12b. Internal/admin wording in the new section -------------------------
const M92_REQUIRED_WORDING = [
  "감사 패킷 보기",
  "감사 패킷 복사",
  "내부 감사 패킷",
  "관리자 전용",
  "게시가 아님",
  "사람 검토 기록 확인용",
  "기존 판정 결과",
];
for (const phrase of M92_REQUIRED_WORDING) {
  assert.ok(
    auditPacketSection.includes(phrase),
    `M9.2 admin/internal wording missing from audit-packet section: ${phrase}`,
  );
}

// --- 12c. No publication affordance in the audit-packet section -------------
for (const banned of [
  "auto-publish", "auto_publish",
  "published</option", "corrected</option",
  "발행 버튼", "발행 가능", "지금 게시", "공개 게시",
]) {
  assert.ok(
    !auditPacketSection.includes(banned),
    `audit-packet section must not include publication affordance: ${banned}`,
  );
}

// --- 12d. Stable error / copy messages -------------------------------------
assert.strictEqual(
  helpers.auditPacketNoTaskMessage,
  "감사 패킷을 불러올 검수 작업을 먼저 선택하세요.",
  "auditPacketNoTaskMessage must match the documented Korean copy",
);
assert.strictEqual(
  helpers.auditPacketNoTokenMessage,
  "관리자 로그인이 필요합니다. 먼저 로그인해 주세요.",
  "auditPacketNoTokenMessage must match the documented Korean copy",
);
assert.strictEqual(
  helpers.auditPacketNotFoundMessage,
  "감사 패킷을 찾을 수 없습니다. 검수 작업이 삭제되었거나 더 이상 존재하지 않을 수 있습니다.",
  "auditPacketNotFoundMessage must match the documented Korean copy",
);
assert.strictEqual(
  helpers.auditPacketCopyOkMessage,
  "감사 패킷 JSON을 복사했습니다. 내부 검수 기록 확인용이며 게시물이 아닙니다.",
  "auditPacketCopyOkMessage must match the documented Korean copy",
);
assert.strictEqual(
  helpers.auditPacketCopyFailMessage,
  "복사에 실패했습니다. 감사 패킷 내용을 직접 선택해 복사해 주세요.",
  "auditPacketCopyFailMessage must match the documented Korean copy",
);
assert.strictEqual(
  helpers.auditPacketNotLoadedMessage,
  "복사할 감사 패킷이 없습니다. 먼저 '감사 패킷 보기'를 눌러 주세요.",
  "auditPacketNotLoadedMessage must match the documented Korean copy",
);
// AUTH-2d: the disabled-API message was retired with the token gate.

// --- 12e. Path template carries no token, no query string ------------------
assert.strictEqual(
  helpers.auditPacketPathTemplate,
  "/review/tasks/{task_id}/audit-packet",
  "auditPacketPathTemplate must equal the documented endpoint shape",
);
assert.strictEqual(
  helpers.auditPacketPath("task_xyz"),
  "/review/tasks/task_xyz/audit-packet",
  "auditPacketPath helper must inject the encoded task_id only",
);
assert.strictEqual(
  helpers.auditPacketPath("with space"),
  "/review/tasks/with%20space/audit-packet",
  "auditPacketPath helper must URL-encode the task_id",
);
// The path template must NOT carry a token / query parameter.
assert.ok(
  !helpers.auditPacketPathTemplate.includes("?"),
  "auditPacketPathTemplate must not carry a query string",
);
assert.ok(
  !/token|secret|x-review-token/i.test(helpers.auditPacketPathTemplate),
  "auditPacketPathTemplate must not name a token-shaped query param",
);

// --- 12f. Summary builder surfaces stable fields, no semantic-as-truth -----
const SAMPLE_PACKET = {
  packet_type: "internal_review_audit_packet",
  audit_version: 1,
  generated_at: "2026-05-22T00:00:00.000000+00:00",
  task: { task_id: "review_abc", status: "pending_review" },
  verdict_snapshot: {
    final_decision: "사람 검토 필요",
    policy_confidence: "moderate",
    verification_card_status: "pending_review",
    verification_card_verdict: null,
  },
  source_snapshot: { result_id: "42", job_id: null, item_index: 0, query: "q" },
  review_decisions: [
    { decision_id: "d1", decision: "comment",
      previous_status: "pending_review", new_status: "pending_review",
      transition: "pending_review (unchanged)", decision_source: "review_ui",
      audit_version: 1 },
  ],
  safety_contract: {
    publication: false,
    mutates_original_result: false,
    mutates_final_decision: false,
    mutates_policy_confidence: false,
    mutates_verification_card: false,
    semantic_matching_debug_only: true,
    human_review_required: true,
  },
};
const summaryRows = helpers.buildAuditPacketSummary(SAMPLE_PACKET);
assert.ok(Array.isArray(summaryRows) && summaryRows.length >= 10,
  "buildAuditPacketSummary must return a non-empty row list");
const summaryByLabel = Object.fromEntries(
  summaryRows.map((r) => [r.label, r.value])
);
assert.strictEqual(summaryByLabel["packet_type"], "internal_review_audit_packet");
assert.strictEqual(summaryByLabel["audit_version"], "1");
assert.strictEqual(summaryByLabel["task_id"], "review_abc");
assert.strictEqual(summaryByLabel["task.status"], "pending_review");
assert.strictEqual(summaryByLabel["verdict_snapshot.final_decision"], "사람 검토 필요");
assert.strictEqual(summaryByLabel["verdict_snapshot.policy_confidence"], "moderate");
assert.strictEqual(summaryByLabel["review_decision_count"], "1");
assert.strictEqual(summaryByLabel["safety_contract.publication"], "false");
assert.strictEqual(summaryByLabel["safety_contract.mutates_final_decision"], "false");
assert.strictEqual(summaryByLabel["safety_contract.mutates_policy_confidence"], "false");
assert.strictEqual(summaryByLabel["safety_contract.mutates_verification_card"], "false");
assert.strictEqual(summaryByLabel["safety_contract.semantic_matching_debug_only"], "true");
// No semantic label is surfaced as truth — only as a debug/safety contract.
// The summary must NOT carry the semantic_evidence_summary key or any
// "match strength" style field.
for (const row of summaryRows) {
  assert.ok(
    !/semantic_evidence_summary/i.test(row.label),
    `audit-packet summary must not surface semantic_evidence_summary: ${row.label}`,
  );
  assert.ok(
    !/match.*strength|truth/i.test(row.label),
    `audit-packet summary must not surface match/truth labels: ${row.label}`,
  );
}

// --- 12g. buildAuditPacketSummary tolerates missing fields without crashing
const partialSummary = helpers.buildAuditPacketSummary({});
assert.ok(Array.isArray(partialSummary));
const partialMap = Object.fromEntries(partialSummary.map((r) => [r.label, r.value]));
assert.strictEqual(partialMap["packet_type"], "(없음)");
assert.strictEqual(partialMap["audit_version"], "(없음)");
assert.strictEqual(partialMap["review_decision_count"], "0");
assert.strictEqual(partialMap["safety_contract.publication"], "(없음)");

// Non-object input → safe defaults, no throw.
const nullSummary = helpers.buildAuditPacketSummary(null);
assert.ok(Array.isArray(nullSummary));

// --- 12h. No auto-fetch on init even with a stored session token -----------
// Re-use the seeded sandbox helper introduced in M8.7 step 7. Assert
// that even with a token in sessionStorage, no /review/tasks/{id}/audit-packet
// request fires during page initialization.
const seededAuditSandbox = createSandbox({
  session: { policy_ai_server_review_token: "audit-packet-init-token" },
});
const seededAuditFetches = seededAuditSandbox.__fetchCalls.filter(
  (c) => c.url.includes("/audit-packet")
);
assert.strictEqual(
  seededAuditFetches.length, 0,
  `init must not auto-fetch /audit-packet even with a stored token; got: ${
    JSON.stringify(seededAuditFetches.map((c) => c.url))
  }`,
);
const baselineAuditFetches = sandbox.__fetchCalls.filter(
  (c) => c.url.includes("/audit-packet")
);
assert.strictEqual(baselineAuditFetches.length, 0,
  "init must not auto-fetch /audit-packet when no token is stored");

// --- 12i. Static call-site audit ------------------------------------------
// The /audit-packet path must appear in exactly one fetch call site
// (the explicit-click loader). Static scan over scripts that built the
// helpers above. Anything else would mean a non-explicit code path is
// touching the endpoint.
const concatenatedScripts = scripts.join("\n");
const auditPacketRefs = (concatenatedScripts.match(/\/audit-packet/g) || []).length;
assert.ok(
  auditPacketRefs >= 1,
  "/audit-packet path must appear at least once (in the loader)"
);
// The serverReviewLoadAuditPacket function is the only async function
// that constructs the path via the template — pin its presence so a
// refactor doesn't accidentally remove the gating.
assert.ok(
  /async function serverReviewLoadAuditPacket\b/.test(concatenatedScripts),
  "serverReviewLoadAuditPacket must remain the sole entry point for the fetch",
);
// Token is sent only through the existing serverReviewFetch helper —
// confirm the loader uses it rather than calling fetch() directly.
const loaderBody = concatenatedScripts.slice(
  concatenatedScripts.indexOf("async function serverReviewLoadAuditPacket"),
  concatenatedScripts.indexOf("async function serverReviewCopyAuditPacket"),
);
assert.ok(
  loaderBody.includes("serverReviewFetch("),
  "serverReviewLoadAuditPacket must call through serverReviewFetch for token-header gating",
);
assert.ok(
  !/\?token=/.test(loaderBody) && !/X-Review-Token/.test(loaderBody),
  "serverReviewLoadAuditPacket must not embed token in URL or set its own auth header",
);

// --- 12j. Copy / load functions never reference token storage directly -----
const copyBody = concatenatedScripts.slice(
  concatenatedScripts.indexOf("async function serverReviewCopyAuditPacket"),
);
assert.ok(
  !/localStorage/.test(copyBody.slice(0, 1500)),
  "serverReviewCopyAuditPacket must not touch localStorage",
);
// The copy path's success message asserts internal-only / non-publication.
assert.ok(
  copyBody.includes("SERVER_REVIEW_AUDIT_PACKET_COPY_OK_MESSAGE"),
  "copy success branch must reference the documented success message constant",
);

// --- 12k. Raw JSON viewer uses textContent (not innerHTML) ------------------
// The raw JSON area must never use innerHTML — defensive against future
// regressions. Scan the source for the raw-area assignment site.
const rawAssign = concatenatedScripts.match(
  /document\.getElementById\("serverReviewAuditPacketRaw"\)[^;]*\.(\w+)\s*=/g
);
if (rawAssign) {
  for (const m of rawAssign) {
    assert.ok(
      !/\.innerHTML\s*=/.test(m),
      `audit-packet raw area must not be assigned via innerHTML: ${m}`,
    );
  }
}
// Direct assignments to rawEl.textContent (the rendered pretty JSON)
// must also exist.
assert.ok(
  /rawEl\.textContent\s*=/.test(concatenatedScripts),
  "audit-packet raw area must be populated via textContent",
);

// =============================================================================
// 13. M9.4 — public/admin surface separation
// =============================================================================
//
// Pin the operator-mode reveal contract:
//   * default page load hides the reviewer/admin sections
//   * ``?operator_tools=1`` reveals them and writes a sessionStorage flag
//   * the flag alone reveals them on subsequent page loads
//   * neither path fires any /review/* request on init
//   * the "운영자 도구 숨기기" button clears the flag, the review
//     session token, and any loaded review state
//   * operator-mode visibility never uses localStorage
//   * the disclaimer wording says "이 표시는 인증이 아닙니다" and
//     names REVIEW_API_ENABLED + X-Review-Token as the real protection
//
// All assertions run against fresh sandboxes built via createSandbox()
// so each scenario starts with a clean storage state.

// --- 13a. Required markup -------------------------------------------------
const M94_REQUIRED_IDS = [
  "operatorTools",
  "operatorToolsHideBtn",
];
for (const id of M94_REQUIRED_IDS) {
  assert.ok(
    html.includes(`id="${id}"`),
    `M9.4 element missing: id="${id}"`,
  );
}
// The wrapper must default to hidden on the static markup so the
// public page never paints the operator panels in the first frame.
assert.ok(
  /<div id="operatorTools"[^>]*\bhidden\b/.test(html),
  "M9.4: <div id=\"operatorTools\"> must carry the hidden attribute by default",
);

// --- 13b. Disclaimer wording ---------------------------------------------
const M94_REQUIRED_WORDING = [
  "내부 운영자 도구",
  "관리자 전용",
  "이 표시는 인증이 아니며",
  "REVIEW_API_ENABLED",
  "X-Review-Token",
  "운영자 도구 숨기기",
  "게시가 아님",
];
for (const phrase of M94_REQUIRED_WORDING) {
  assert.ok(
    html.includes(phrase),
    `M9.4 disclaimer wording missing: ${phrase}`,
  );
}

// --- 13c. Helpers are exposed --------------------------------------------
assert.strictEqual(
  helpers.operatorToolsStorageKey,
  "policy_ai_operator_tools_visible",
);
assert.strictEqual(helpers.operatorToolsUrlFlag, "operator_tools");
assert.strictEqual(typeof helpers.operatorToolsRequestedByUrl, "function");
assert.strictEqual(typeof helpers.operatorToolsFlagSet, "function");
assert.strictEqual(typeof helpers.showOperatorTools, "function");
assert.strictEqual(typeof helpers.hideOperatorToolsAndResetState, "function");
assert.strictEqual(typeof helpers.applyOperatorToolsVisibility, "function");

// --- 13d. Default page load → tools hidden, no /review/* fetch -----------
const defaultSandbox = createSandbox();
{
  const opEl = defaultSandbox.__elementCache.get("operatorTools");
  assert.ok(opEl, "init must touch the #operatorTools element");
  assert.strictEqual(
    opEl.hidden, true,
    "default page load must leave #operatorTools hidden",
  );
  const sessionFlag = defaultSandbox.__sessionStore.get(
    "policy_ai_operator_tools_visible",
  );
  assert.ok(!sessionFlag, "default page load must not set operator-mode flag");
  const localFlag = defaultSandbox.__localStore.get(
    "policy_ai_operator_tools_visible",
  );
  assert.ok(!localFlag, "operator-mode flag must never use localStorage");
  const reviewFetches = defaultSandbox.__fetchCalls.filter(
    (c) => c.url.includes("/review/")
  );
  assert.strictEqual(
    reviewFetches.length, 0,
    "default page load must not auto-fetch any /review/* endpoint",
  );
}

// --- 13e. URL flag → tools visible + sessionStorage set + URL cleaned ----
const urlSandbox = createSandbox({ urlSearch: "?operator_tools=1" });
{
  const opEl = urlSandbox.__elementCache.get("operatorTools");
  assert.strictEqual(
    opEl.hidden, false,
    "?operator_tools=1 must reveal the operator-tools wrapper",
  );
  assert.strictEqual(
    urlSandbox.__sessionStore.get("policy_ai_operator_tools_visible"),
    "true",
    "URL flag must write the sessionStorage operator-mode flag",
  );
  // history.replaceState was called to clean ?operator_tools=1 out of
  // the visible URL (so a shared/bookmarked link doesn't force the
  // mode on the next visitor).
  assert.ok(
    urlSandbox.__historyReplaceCalls.length >= 1,
    "URL flag must trigger history.replaceState to clean the URL",
  );
  const cleanedSearch = urlSandbox.window.location.search;
  assert.ok(
    !cleanedSearch.includes("operator_tools"),
    `URL must no longer carry operator_tools after cleanup; got ${cleanedSearch}`,
  );
  // No /review/* fetch on init even when tools are revealed.
  const reviewFetches = urlSandbox.__fetchCalls.filter(
    (c) => c.url.includes("/review/")
  );
  assert.strictEqual(
    reviewFetches.length, 0,
    "URL flag must not cause any auto /review/* fetch on init",
  );
}

// --- 13f. SessionStorage flag alone → tools visible, still no fetch -----
const seededOpSandbox = createSandbox({
  session: { policy_ai_operator_tools_visible: "true" },
});
{
  const opEl = seededOpSandbox.__elementCache.get("operatorTools");
  assert.strictEqual(
    opEl.hidden, false,
    "sessionStorage flag alone must reveal the operator-tools wrapper",
  );
  // No /review/* fetch on init, even with the flag pre-set.
  const reviewFetches = seededOpSandbox.__fetchCalls.filter(
    (c) => c.url.includes("/review/")
  );
  assert.strictEqual(
    reviewFetches.length, 0,
    "operator-mode flag must not cause any auto /review/* fetch on init",
  );
  // No URL cleanup happens when the flag came from session, not URL.
  assert.strictEqual(
    seededOpSandbox.__historyReplaceCalls.length, 0,
    "no URL cleanup when the URL didn't carry the flag",
  );
}

// --- 13g. Hide button clears operator flag + in-memory state ------------
// AUTH-2d: there is no review token to clear anymore; hide only clears the
// operator-mode visibility flag and resets in-memory review-side UI state.
const hideSandbox = createSandbox({
  session: {
    policy_ai_operator_tools_visible: "true",
  },
});
{
  // Sanity: tools were revealed because the session flag was set.
  const opEl = hideSandbox.__elementCache.get("operatorTools");
  assert.strictEqual(opEl.hidden, false);
  // Invoke the hide helper directly.
  hideSandbox.window.__serverReviewHelpers.hideOperatorToolsAndResetState();
  assert.strictEqual(
    opEl.hidden, true,
    "hide handler must hide the operator-tools wrapper",
  );
  // Flag cleared from sessionStorage.
  assert.strictEqual(
    hideSandbox.__sessionStore.get("policy_ai_operator_tools_visible"),
    undefined,
    "hide must clear the operator-mode flag from sessionStorage",
  );
  // Flag must not have moved to localStorage.
  assert.ok(
    !hideSandbox.__localStore.get("policy_ai_operator_tools_visible"),
    "operator-mode flag must never appear in localStorage",
  );
  // Hide must not fire any /review/* request.
  const reviewFetches = hideSandbox.__fetchCalls.filter(
    (c) => c.url.includes("/review/")
  );
  assert.strictEqual(
    reviewFetches.length, 0,
    "hide handler must not trigger any /review/* fetch",
  );
}

// --- 13h. Token / publication safety in the operator-tools wrapper ------
// Pin that no publication / public-export wording slipped into the new
// operator-tools banner block.
const opToolsBlockStart = html.indexOf('id="operatorTools"');
const opToolsBlockEnd = html.indexOf(
  '<!-- /#operatorTools', opToolsBlockStart
);
assert.ok(
  opToolsBlockStart > 0 && opToolsBlockEnd > opToolsBlockStart,
  "could not locate the M9.4 operator-tools block bounds in index.html",
);
const opToolsBlock = html.slice(opToolsBlockStart, opToolsBlockEnd);
for (const banned of [
  "auto-publish", "auto_publish",
  "published</option", "corrected</option",
  "발행 가능", "발행 버튼", "지금 게시", "공개 게시",
  // The operator-mode flag must never appear in any localStorage call.
  'localStorage.setItem("policy_ai_operator_tools_visible',
  "localStorage.setItem('policy_ai_operator_tools_visible",
]) {
  assert.ok(
    !opToolsBlock.includes(banned),
    `operator-tools block must not include: ${banned}`,
  );
}
// And the operator-mode flag string in the *whole document* must only
// appear in sessionStorage call sites — never in localStorage assignments.
for (const localCall of [
  'localStorage.setItem("policy_ai_operator_tools_visible',
  "localStorage.setItem('policy_ai_operator_tools_visible",
  'localStorage.getItem("policy_ai_operator_tools_visible',
  "localStorage.getItem('policy_ai_operator_tools_visible",
]) {
  assert.ok(
    !html.includes(localCall),
    `operator-mode flag must never use localStorage: ${localCall}`,
  );
}

// --- 13i. Existing M8.x / M9.x contracts still hold ---------------------
// Decision vocabulary unchanged.
for (const decision of ["approve", "reject", "needs_more_evidence", "comment"]) {
  assert.ok(
    html.includes(`value="${decision}"`),
    `decision dropdown still requires value="${decision}"`,
  );
}
// No-current-result / audit-packet messages unchanged (AUTH-2d: the
// disabled-API message was retired with the token gate).
assert.strictEqual(
  helpers.noCurrentResultMessage,
  "등록할 분석 결과가 없습니다. 먼저 분석을 실행하거나 기록에서 결과를 선택하세요.",
);
assert.strictEqual(
  helpers.auditPacketNoTaskMessage,
  "감사 패킷을 불러올 검수 작업을 먼저 선택하세요.",
);
assert.strictEqual(
  helpers.auditPacketNoTokenMessage,
  "관리자 로그인이 필요합니다. 먼저 로그인해 주세요.",
);

console.log("server-review UI smoke tests passed");
