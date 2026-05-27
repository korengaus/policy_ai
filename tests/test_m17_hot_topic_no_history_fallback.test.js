// M17-search-quality — hot-topic card no longer falls back to localStorage history.
//
// Phase 1 diagnosis identified that `currentTopicCards` silently
// surfaced prior localStorage records when the current search returned
// no results, making it look like 전세대출 articles were the response to
// queries like "기후변화 정책". Phase 2 removes the fallback and migrates
// the localStorage key from `policy_ai_recent_analysis` to
// `policy_ai_recent_analysis_v2` (with a one-shot cleanup of the legacy key).
//
// These tests load the served `web/index.html` into a VM sandbox in the
// same shape as `tests/localstorage_slim.test.js` and pin the new contract.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const rootDir = path.resolve(__dirname, "..");
const htmlPath = path.join(rootDir, "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);

function createElementStub() {
  return {
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    removeChild() {},
    setAttribute() {},
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
    value: "",
    checked: false,
    disabled: false,
    innerHTML: "",
    textContent: "",
    href: "",
    download: "",
  };
}

function createSandbox({ preExistingStorage = {} } = {}) {
  const storage = new Map(Object.entries(preExistingStorage));
  const sandbox = {
    console: { log() {}, warn() {}, error: console.error, debug() {} },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
      removeItem(key) { storage.delete(key); },
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
      location: { origin: "http://127.0.0.1:8000", search: "", hostname: "127.0.0.1" },
      scrollTo() {},
      addEventListener() {},
      matchMedia() { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
    },
    navigator: { clipboard: { async writeText() {} } },
    URL: { createObjectURL() { return "blob:test"; }, revokeObjectURL() {} },
    alert() {},
    confirm() { return true; },
    fetch() { throw new Error("Network calls are disabled in this fixture"); },
    setTimeout() {}, clearTimeout() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(scripts.join("\n"), sandbox, { filename: "web/index.html" });
  sandbox.__storage = storage;
  return sandbox;
}

function makeHistoryRecord(query, title) {
  // Slim shape matching what `saveLocalAnalysisHistory` writes — minimum
  // fields needed by `getHistoryResults` + `topicCardFromResult`.
  return {
    id: `${query}-record`,
    query,
    analyzed_at: "2026-05-01T00:00:00.000Z",
    summary_results: [
      {
        result_id: 1,
        title,
        original_url: `https://example.com/${encodeURIComponent(query)}`,
        topic: query,
        final_decision: { policy_alert_level: "WATCH" },
        policy_confidence: { policy_confidence_score: 60 },
        verification_card: { verdict_label: "draft_needs_review", verdict_confidence: 60 },
      },
    ],
  };
}

// === Test 1: currentTopicCards returns [] when no current results ===
//
// Pins the H1 fix: with currentReportContext.results empty and no
// preferredResults supplied, the function MUST return an empty array
// instead of pulling from localStorage history.
{
  const sandbox = createSandbox();
  // Materialize the return value as JSON in the VM context so the
  // comparison doesn't rely on cross-realm Array prototype identity
  // (Node's deepStrictEqual checks the prototype chain — arrays
  // produced inside vm.runInContext have a different Array.prototype).
  const cardsJson = vm.runInContext(
    `currentReportContext = null; JSON.stringify(currentTopicCards());`,
    sandbox,
  );
  assert.strictEqual(
    cardsJson, "[]",
    "currentTopicCards must return [] when no current results AND no preferred results"
  );
}

// === Test 2: currentTopicCards ignores localStorage history ===
//
// Even with multiple history records pre-populated under the new key,
// currentTopicCards must NOT surface them — that was the H1 bug.
{
  const preExistingStorage = {
    // Pre-populate the NEW key so the legacy-cleanup step doesn't wipe it.
    policy_ai_recent_analysis_v2: JSON.stringify([
      makeHistoryRecord("전세대출", "청년 버팀목 전세대출 2년새 반토막"),
      makeHistoryRecord("전세사기", "전세사기 피해 청년에 학자금 대출 상환 지원"),
    ]),
  };
  const sandbox = createSandbox({ preExistingStorage });
  // Confirm history records ARE readable via the existing helper
  // (so the test exercises the real condition the bug created).
  const historyCount = vm.runInContext(
    `safeReadLocalHistory().length`,
    sandbox,
  );
  assert.strictEqual(
    historyCount, 2,
    "history records must be in localStorage so the fallback would have surfaced them"
  );
  // Now the actual contract: even with history present, the hot-topic
  // card source returns nothing when there are no current results.
  const cardsJson = vm.runInContext(
    `currentReportContext = null; JSON.stringify(currentTopicCards());`,
    sandbox,
  );
  assert.strictEqual(
    cardsJson, "[]",
    "currentTopicCards must NOT fall back to localStorage even when history records exist"
  );
}

// === Test 3: localStorage migration clears the legacy key ===
//
// One-shot cleanup at page-load init: the old `policy_ai_recent_analysis`
// key is removed; the new `policy_ai_recent_analysis_v2` key is the
// active storage target.
{
  const preExistingStorage = {
    policy_ai_recent_analysis: JSON.stringify([
      makeHistoryRecord("전세대출", "old housing record that must not surface"),
    ]),
  };
  const sandbox = createSandbox({ preExistingStorage });
  // The init code at module load already ran when the sandbox was
  // created. The legacy key must have been removed.
  assert.strictEqual(
    sandbox.__storage.has("policy_ai_recent_analysis"),
    false,
    "legacy storage key policy_ai_recent_analysis must be removed at init"
  );
  // The new key is the constant the runtime now uses for reads/writes.
  const activeKey = vm.runInContext(`LOCAL_HISTORY_KEY`, sandbox);
  assert.strictEqual(
    activeKey, "policy_ai_recent_analysis_v2",
    "LOCAL_HISTORY_KEY must be the bumped key"
  );
}

console.log("M17 hot-topic no-history-fallback tests passed.");
