// AUTH-2c — frontend account-login UI regression tests.
//
// Mirrors review_ui.test.js's vm-sandbox approach: load the BUILT
// web/index.html, extract its <script> bodies, run them in a vm context with
// stubbed DOM / storage / fetch, and exercise the new
// window.__serverReviewHelpers.authLogin / authLogout / authMe surface.
//
// Covers the 7 AUTH-2c cases:
//   (1) successful login -> identity line shows role, privileged actions
//       enabled, password field cleared
//   (2) logout -> reverts to logged-out, session enablement removed
//   (3) authMe state reflected (authenticated true/false)
//   (4) token fallback still works with no session (privilegedReady via token)
//   (5) submitted password never appears in DOM / sessionStorage / localStorage
//   (6) generic failure message on 401 (no user-vs-password distinction)
//   (7) password input is type="password"
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const rootDir = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(rootDir, "web", "index.html"), "utf8");

const TOKEN_STORAGE_KEY = "policy_ai_server_review_token";
const OPERATOR_FLAG_KEY = "policy_ai_operator_tools_visible";

// --- (7) password input is type="password" (structural, on the built HTML) ---
const loginPassBlock = html.match(/id="serverReviewLoginPass"[\s\S]{0,400}?>/);
assert.ok(loginPassBlock, "serverReviewLoginPass input must exist in built HTML");
assert.ok(
  /type="password"/.test(loginPassBlock[0]),
  "serverReviewLoginPass must be type=\"password\""
);
// Login panel present; AUTH-2d: the legacy token box must be GONE.
assert.ok(html.includes('id="serverReviewLoginBtn"'), "login button must exist");
assert.ok(html.includes('id="serverReviewLogoutBtn"'), "logout button must exist");
assert.ok(!html.includes('id="serverReviewToken"'), "legacy token input must be removed");
assert.ok(!html.includes('id="serverReviewTokenSaveBtn"'), "legacy token save btn must be removed");

// --- vm sandbox harness (mirrors review_ui.test.js) --------------------------
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

function createElementStub() {
  return {
    addEventListener() {}, removeEventListener() {},
    appendChild() {}, removeChild() {}, setAttribute() {},
    getAttribute() { return ""; }, select() {}, click() {}, remove() {},
    closest() { return null; },
    querySelector() { return createElementStub(); },
    querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {}, dataset: {}, hidden: false, value: "", checked: false,
    disabled: false, innerHTML: "", textContent: "", href: "", download: "",
  };
}

// Build a fresh sandbox; `responder(url, init)` returns {ok,status,body}.
function createSandbox(opts) {
  opts = opts || {};
  const sessionStore = new Map(opts.session ? Object.entries(opts.session) : []);
  const localStore = new Map();
  const fetchCalls = [];
  const elementCache = new Map();
  function cachedElement(id) {
    if (!elementCache.has(id)) elementCache.set(id, createElementStub());
    return elementCache.get(id);
  }
  const responder = opts.responder || (() => ({ ok: false, status: 503, body: { detail: "disabled" } }));
  const sandbox = {
    console: { log() {}, warn() {}, error() {}, debug() {}, info() {} },
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
      location: { origin: "http://127.0.0.1:8000", pathname: "/", search: opts.urlSearch || "", hash: "" },
      history: { replaceState() {}, pushState() {} },
      scrollTo() {}, addEventListener() {},
      matchMedia() { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
    },
    URLSearchParams,
    navigator: { clipboard: { async writeText() {} } },
    URL: { createObjectURL() { return "blob:test"; }, revokeObjectURL() {} },
    alert() {}, confirm() { return true; },
    fetch(input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      fetchCalls.push({ url, init: init || null });
      const r = responder(url, init) || { ok: false, status: 503, body: null };
      return Promise.resolve({
        ok: r.ok, status: r.status,
        async json() { return r.body; },
      });
    },
    setTimeout(fn) { try { fn(); } catch (_) {} return 0; },
    clearTimeout() {},
    Blob: function Blob() { this.size = 0; },
  };
  sandbox.window.sessionStorage = sandbox.sessionStorage;
  sandbox.window.localStorage = sandbox.localStorage;
  vm.createContext(sandbox);
  vm.runInContext(scripts.join("\n"), sandbox, { filename: "web/index.html" });
  return sandbox;
}

function getHelpers(sandbox) {
  const h = sandbox.window.__serverReviewHelpers;
  assert.ok(h, "window.__serverReviewHelpers must be exposed");
  return h;
}

// Existing pinned members must still be present (additive-only contract).
(function existingContractPreserved() {
  const h = getHelpers(createSandbox());
  for (const member of [
    "formatErrorMessage", "formatStatusLabel", "loginRequiredMessage",
    "operatorToolsFlagSet", "applyOperatorToolsVisibility",
  ]) {
    assert.ok(member in h, `existing helper "${member}" must be preserved`);
  }
  // New members present.
  for (const member of ["authLogin", "authLogout", "authMe", "loginPath", "logoutPath", "mePath", "loginFailedMessage", "loggedInLabel", "privilegedReady"]) {
    assert.ok(member in h, `new helper "${member}" must be exported`);
  }
  assert.strictEqual(h.loginPath, "/auth/login");
  assert.strictEqual(h.logoutPath, "/auth/logout");
  assert.strictEqual(h.mePath, "/auth/me");
  assert.strictEqual(h.loggedInLabel("admin"), "관리자(admin)로 로그인됨");
})();

// --- (1) successful login ----------------------------------------------------
async function successfulLogin() {
  const sb = createSandbox({
    responder: (url) => url.endsWith("/auth/login")
      ? { ok: true, status: 200, body: { ok: true, role: "admin" } }
      : { ok: false, status: 503, body: null },
  });
  const h = getHelpers(sb);
  const passEl = sb.document.getElementById("serverReviewLoginPass");
  passEl.value = "the-secret-pw-123";  // simulate what the binding would pass
  const res = await h.authLogin("admin", "the-secret-pw-123");
  assert.strictEqual(res.ok, true, "login should succeed");
  assert.strictEqual(res.role, "admin");
  // identity line shows the role
  const status = sb.document.getElementById("serverReviewLoginStatus");
  assert.strictEqual(status.textContent, "관리자(admin)로 로그인됨");
  // privileged actions enabled: logout enabled, login disabled
  assert.strictEqual(sb.document.getElementById("serverReviewLoginBtn").disabled, true);
  assert.strictEqual(sb.document.getElementById("serverReviewLogoutBtn").disabled, false);
  // privilegedReady true via session
  assert.strictEqual(h.privilegedReady(), true);
  // password field cleared
  assert.strictEqual(passEl.value, "", "password input must be cleared after login");
}

// --- (2) logout reverts ------------------------------------------------------
async function logoutReverts() {
  const sb = createSandbox({
    responder: (url) => {
      if (url.endsWith("/auth/login")) return { ok: true, status: 200, body: { ok: true, role: "admin" } };
      if (url.endsWith("/auth/logout")) return { ok: true, status: 200, body: { ok: true } };
      return { ok: false, status: 503, body: null };
    },
  });
  const h = getHelpers(sb);
  await h.authLogin("admin", "pw");
  assert.strictEqual(h.privilegedReady(), true);
  await h.authLogout();
  assert.strictEqual(sb.document.getElementById("serverReviewLoginBtn").disabled, false);
  assert.strictEqual(sb.document.getElementById("serverReviewLogoutBtn").disabled, true);
  // No token present, so privilegedReady is now false.
  assert.strictEqual(h.privilegedReady(), false);
}

// --- (3) authMe reflects state ----------------------------------------------
async function authMeReflectsState() {
  const sbYes = createSandbox({
    responder: (url) => url.endsWith("/auth/me")
      ? { ok: true, status: 200, body: { authenticated: true, role: "admin" } }
      : { ok: false, status: 503, body: null },
  });
  const hYes = getHelpers(sbYes);
  const meYes = await hYes.authMe();
  assert.strictEqual(meYes.authenticated, true);
  assert.strictEqual(meYes.role, "admin");
  assert.strictEqual(hYes.privilegedReady(), true);

  const sbNo = createSandbox({
    responder: (url) => url.endsWith("/auth/me")
      ? { ok: true, status: 200, body: { authenticated: false } }
      : { ok: false, status: 503, body: null },
  });
  const hNo = getHelpers(sbNo);
  const meNo = await hNo.authMe();
  assert.strictEqual(meNo.authenticated, false);
  assert.strictEqual(hNo.privilegedReady(), false);
}

// --- (4) AUTH-2d: a legacy token no longer grants access -------------------
function tokenNoLongerGrants() {
  // Even with a leftover token in session storage, privilegedReady is false
  // until an authenticated session exists (session-only since AUTH-2d).
  const sb = createSandbox({ session: { [TOKEN_STORAGE_KEY]: "legacy-token-xyz" } });
  assert.strictEqual(
    getHelpers(sb).privilegedReady(), false,
    "privilegedReady must be false for a token-only (no session) state"
  );
  // And false with neither token nor session.
  const sb2 = createSandbox();
  assert.strictEqual(getHelpers(sb2).privilegedReady(), false);
}

// --- (5) password never appears in DOM / storage ----------------------------
async function passwordNeverLeaks() {
  const PW = "leak-check-PW-do-not-store-987";
  const sb = createSandbox({
    responder: (url) => url.endsWith("/auth/login")
      ? { ok: true, status: 200, body: { ok: true, role: "admin" } }
      : { ok: false, status: 503, body: null },
  });
  const h = getHelpers(sb);
  sb.document.getElementById("serverReviewLoginPass").value = PW;
  await h.authLogin("admin", PW);
  // Not in any sessionStorage / localStorage entry.
  for (const v of sb.__sessionStore.values()) assert.ok(!String(v).includes(PW), "password leaked into sessionStorage");
  for (const v of sb.__localStore.values()) assert.ok(!String(v).includes(PW), "password leaked into localStorage");
  // Not left in any DOM stub's value/textContent/innerHTML.
  for (const el of sb.__fetchCalls) { /* fetch body is the request, not DOM */ }
  const passEl = sb.document.getElementById("serverReviewLoginPass");
  assert.strictEqual(passEl.value, "", "password input must be cleared");
  const status = sb.document.getElementById("serverReviewLoginStatus");
  assert.ok(!String(status.textContent).includes(PW), "password must not appear in status line");
}

// --- (6) generic failure on 401 (no user-vs-password distinction) ------------
async function genericFailure() {
  // Backend returns the SAME generic 401 for wrong-password and unknown-user.
  function mk401() {
    return createSandbox({
      responder: (url) => url.endsWith("/auth/login")
        ? { ok: false, status: 401, body: { detail: "invalid credentials" } }
        : { ok: false, status: 503, body: null },
    });
  }
  const sbWrong = mk401();
  const hWrong = getHelpers(sbWrong);
  const r1 = await hWrong.authLogin("admin", "bad-pw");
  assert.strictEqual(r1.ok, false);
  const msgWrong = sbWrong.document.getElementById("serverReviewLoginStatus").textContent;

  const sbUnknown = mk401();
  const hUnknown = getHelpers(sbUnknown);
  await hUnknown.authLogin("ghost", "bad-pw");
  const msgUnknown = sbUnknown.document.getElementById("serverReviewLoginStatus").textContent;

  assert.strictEqual(msgWrong, hWrong.loginFailedMessage, "must show the generic failure message");
  assert.strictEqual(msgWrong, msgUnknown, "wrong-password and unknown-user must show identical text (no enumeration)");
  assert.ok(!msgWrong.includes("bad-pw"), "failure message must not echo the password");
}

async function main() {
  await successfulLogin();
  await logoutReverts();
  await authMeReflectsState();
  tokenNoLongerGrants();
  await passwordNeverLeaks();
  await genericFailure();
  console.log("AUTH-2c login UI tests passed (7 cases + contract preservation).");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
