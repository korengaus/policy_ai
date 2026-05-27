// Phase 2 M3 — localStorage slim record + safe wrapper regression tests.
//
// Goal: prove that history/review-queue writes (a) strip heavy nested fields
// before persisting, (b) survive QuotaExceededError without throwing, and
// (c) preserve enough metadata for topic-card / history-row rendering.
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

function createSandbox({ quotaAfterBytes = Infinity } = {}) {
  const storage = new Map();
  const warnings = [];
  const sandbox = {
    console: {
      log() {},
      warn(...args) { warnings.push(args.map(String).join(" ")); },
      error: console.error,
      debug() {},
    },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) {
        const str = String(value);
        let total = str.length;
        for (const [k, v] of storage.entries()) {
          if (k !== key) total += v.length;
        }
        if (total > quotaAfterBytes) {
          const err = new Error("QuotaExceededError");
          err.name = "QuotaExceededError";
          err.code = 22;
          throw err;
        }
        storage.set(key, str);
      },
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
  sandbox.__warnings = warnings;
  return sandbox;
}

function makeFullResult(query, idx) {
  // Approximates a real /analyze result with heavy nested fields the slim
  // builder is expected to strip. Each large array contains 30 entries to
  // keep the payload comfortably above any "noise" threshold.
  const big = (label) => Array.from({ length: 30 }, (_, i) => ({
    title: `${label} ${i}`,
    url: `https://example.com/${label}/${i}`,
    body: "x".repeat(200),
    source_type: "news",
  }));
  return {
    result_id: 100 + idx,
    title: `${query} 결과 ${idx}`,
    original_url: `https://news.example.com/${query}/${idx}`,
    topic: "금융/정책",
    claims: Array.from({ length: 10 }, (_, i) => `${query} 주장 ${i}`),
    normalized_claims: Array.from({ length: 10 }, (_, i) => `정규 주장 ${i}`),
    source_candidates: big("candidate"),
    source_queries: ["q1", "q2"],
    evidence_snippets: big("snippet"),
    claim_evidence_map: { c1: ["s1", "s2", "s3"] },
    contradiction_checks: big("contradiction"),
    bias_framing_analysis: big("bias"),
    debug_summary: {
      needs_human_review: true,
      official_body_matches: 1,
      official_body_candidates: 2,
      official_bodies_fetched: 1,
      evidence_quality_summary: { strong: 1, medium: 1, weak: 0, average_evidence_quality_score: 60 },
      evidence_strength_summary: { strong: 1, medium: 1, weak: 0 },
    },
    policy_confidence: { policy_confidence_score: 60, risk_level: "medium", action_priority: "medium" },
    policy_impact: { impact_level: "medium", impact_direction: "uncertain" },
    final_decision: {
      policy_alert_level: "WATCH",
      market_signal: "policy_uncertainty",
      decision_summary: "공식 본문 확인 필요",
    },
    verification_card: {
      claim_text: `${query} 주장 텍스트 ${idx}`,
      verdict_label: "draft_needs_review",
      verdict_confidence: 60,
      evidence_summary: `${query} 근거 요약 ${idx}`,
      evidence_sources: big("source"),
      source_candidates: big("vc-candidate"),
      evidence_snippets: big("vc-snippet"),
      source_reliability_summary: {
        official_detail_available: true,
        official_candidate_count: 2,
        official_evidence_status: "direct_support",
        official_detail_status: "direct_support",
      },
      debug_summary: {
        needs_human_review: true,
        official_body_matches: 1,
        official_body_candidates: 2,
        official_bodies_fetched: 1,
        evidence_quality_summary: { strong: 1, medium: 1, weak: 0, average_evidence_quality_score: 60 },
        evidence_strength_summary: { strong: 1, medium: 1, weak: 0 },
      },
    },
    claim_text: `${query} 주장 텍스트 ${idx}`,
    verdict_label: "draft_needs_review",
    evidence_summary: `${query} 근거 요약 ${idx}`,
    review_status: "draft_needs_review",
  };
}

// === Test 1: slim record drops heavy fields but keeps render-essential metadata ===
{
  const sandbox = createSandbox();
  const fullResponse = {
    status: "ok",
    results: [makeFullResult("금융위", 0), makeFullResult("금융위", 1)],
  };
  vm.runInContext(
    `this.__historyResult = saveLocalAnalysisHistory("금융위", 2, ${JSON.stringify(fullResponse)});`,
    sandbox
  );
  const raw = sandbox.__storage.get("policy_ai_recent_analysis_v2");
  assert.ok(raw, "history should be written to localStorage");
  const parsed = JSON.parse(raw);
  assert.strictEqual(parsed.length, 1, "should save one slim record");
  const record = parsed[0];
  // Slim shape preserved.
  assert.strictEqual(record.query, "금융위");
  assert.strictEqual(record.results_count, 2);
  assert.ok(Array.isArray(record.summary_results), "summary_results must be present");
  assert.strictEqual(record.summary_results.length, 2);
  for (const summary of record.summary_results) {
    assert.ok(summary.result_id, "summary should carry result_id for hydration");
    assert.ok(summary.title, "summary should keep title");
    assert.ok(summary.original_url, "summary should keep original_url");
    assert.ok(summary.final_decision, "summary should keep final_decision");
    assert.ok(summary.policy_confidence, "summary should keep policy_confidence");
    assert.ok(summary.verification_card, "summary should keep slim verification_card");
    // Heavy fields dropped.
    assert.strictEqual(summary.evidence_snippets, undefined, "slim must drop evidence_snippets");
    assert.strictEqual(summary.evidence_sources, undefined, "slim must drop evidence_sources");
    assert.strictEqual(summary.source_candidates, undefined, "slim must drop source_candidates");
    assert.strictEqual(summary.contradiction_checks, undefined, "slim must drop contradiction_checks");
    assert.strictEqual(summary.bias_framing_analysis, undefined, "slim must drop bias_framing_analysis");
    assert.strictEqual(summary.claim_evidence_map, undefined, "slim must drop claim_evidence_map");
    assert.strictEqual(summary.claims, undefined, "slim must drop claims");
    assert.strictEqual(summary.normalized_claims, undefined, "slim must drop normalized_claims");
    assert.strictEqual(summary.verification_card.evidence_snippets, undefined, "slim verification_card must drop evidence_snippets");
    assert.strictEqual(summary.verification_card.evidence_sources, undefined, "slim verification_card must drop evidence_sources");
    assert.strictEqual(summary.verification_card.source_candidates, undefined, "slim verification_card must drop source_candidates");
  }
  // Top-level record must NOT carry the full response payload.
  assert.strictEqual(record.response, undefined, "slim record must not store response payload");
  // Size sanity: slim payload should be way under the full payload.
  const fullSize = JSON.stringify(fullResponse).length;
  const slimSize = raw.length;
  assert.ok(
    slimSize < fullSize / 3,
    `slim record (${slimSize} bytes) should be a fraction of full payload (${fullSize} bytes)`
  );
}

// === Test 2: review queue writes a slim shape ===
{
  const sandbox = createSandbox();
  // Make the result require human review so it gets queued.
  const fullResult = makeFullResult("전세사기", 0);
  fullResult.verdict_label = "draft_needs_review";
  fullResult.verification_card.verdict_label = "draft_needs_review";
  fullResult.verification_card.debug_summary.needs_human_review = true;
  const responseData = { status: "ok", results: [fullResult] };
  vm.runInContext(
    `
      const responseData = ${JSON.stringify(responseData)};
      const stableHistoryKey = buildStableHistoryKey("전세사기", responseData.results);
      this.__upsertResult = upsertReviewQueue("전세사기", 1, responseData, stableHistoryKey);
    `,
    sandbox
  );
  const raw = sandbox.__storage.get("policy_ai_review_queue");
  assert.ok(raw, "review queue should be written");
  const parsed = JSON.parse(raw);
  assert.strictEqual(parsed.length, 1, "one queue item expected");
  const item = parsed[0];
  assert.strictEqual(item.query, "전세사기");
  assert.ok(item.summary_results, "queue item should carry slim summary_results");
  assert.strictEqual(item.response, undefined, "queue item must not store full response");
  assert.strictEqual(item.summary_results[0].evidence_snippets, undefined, "queue summary should drop evidence_snippets");
  assert.strictEqual(item.summary_results[0].evidence_sources, undefined, "queue summary should drop evidence_sources");
  assert.ok(item.result_id || item.summary_results[0].result_id, "queue item should retain result_id for hydration");
}

// === Test 3: QuotaExceededError is handled gracefully ===
{
  // First, write something legitimately to find a baseline size, then set quota
  // just below the next write so we hit QuotaExceededError on save.
  const sandbox = createSandbox();
  const responseData = { status: "ok", results: [makeFullResult("금융위", 0)] };
  vm.runInContext(
    `this.__historyResult = saveLocalAnalysisHistory("금융위", 1, ${JSON.stringify(responseData)});`,
    sandbox
  );
  const baseline = sandbox.__storage.get("policy_ai_recent_analysis_v2").length;

  // Now create a sandbox with a tight quota and attempt to write a second item.
  const tight = createSandbox({ quotaAfterBytes: Math.max(50, Math.floor(baseline * 0.6)) });
  let threw = false;
  try {
    vm.runInContext(
      `
        const r1 = ${JSON.stringify(responseData)};
        saveLocalAnalysisHistory("금융위", 1, r1);
        const r2 = ${JSON.stringify({ status: "ok", results: [makeFullResult("부동산", 0)] })};
        saveLocalAnalysisHistory("부동산", 1, r2);
      `,
      tight
    );
  } catch (err) {
    threw = true;
  }
  assert.strictEqual(threw, false, "QuotaExceededError must not propagate to caller");
  const quotaWarnings = tight.__warnings.filter((line) => /quota exceeded|giving up/i.test(line));
  assert.ok(quotaWarnings.length > 0, "should log a warning when quota is hit");
}

// === Test 4: hydration cache returns full results once seeded ===
{
  const sandbox = createSandbox();
  const fullResult = makeFullResult("금융위", 0);
  const responseData = { status: "ok", results: [fullResult] };
  vm.runInContext(
    `
      const r = ${JSON.stringify(responseData)};
      this.__renderData = stabilizeAnalysisResponseForRender(r, "금융위", 1);
      this.__readBack = safeReadLocalHistory();
      const record = this.__readBack[0];
      this.__resolved = getHistoryResults(record);
    `,
    sandbox
  );
  const resolved = sandbox.__resolved;
  assert.ok(Array.isArray(resolved), "getHistoryResults should resolve to an array");
  assert.strictEqual(resolved.length, 1, "in-session resolve should return the cached full results");
  // The cached entry should still be full (not slim).
  assert.ok(resolved[0].evidence_snippets && resolved[0].evidence_snippets.length > 0,
    "hydrated cache result should retain full evidence_snippets");
}

console.log("localStorage slim tests passed (4 scenarios)");
