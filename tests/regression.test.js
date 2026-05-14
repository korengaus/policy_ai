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
      add() {},
      remove() {},
      toggle() {},
      contains() { return false; },
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

function createSandbox() {
  const storage = new Map();
  const sandbox = {
    console: {
      log() {},
      warn() {},
      error: console.error,
    },
    localStorage: {
      getItem(key) {
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, value) {
        storage.set(key, String(value));
      },
      removeItem(key) {
        storage.delete(key);
      },
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
      location: { origin: "http://127.0.0.1:8000" },
      scrollTo() {},
      addEventListener() {},
      matchMedia() {
        return { matches: false, addEventListener() {}, removeEventListener() {} };
      },
    },
    navigator: {
      clipboard: {
        async writeText() {},
      },
    },
    Blob: function Blob(parts, options) {
      this.parts = parts;
      this.options = options;
    },
    URL: {
      createObjectURL() { return "blob:test"; },
      revokeObjectURL() {},
    },
    alert() {},
    confirm() { return true; },
    fetch() {
      throw new Error("Network calls are disabled in regression fixtures");
    },
    setTimeout() {},
    clearTimeout() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(scripts.join("\n"), sandbox, { filename: "web/index.html" });
  return sandbox;
}

function weakOfficialFixture({ query, title, topic, contradictionStatus }) {
  return {
    query,
    maxNews: 1,
    analyzedAt: "2026-05-13T00:00:00.000Z",
    results: [
      {
        title,
        original_url: `https://example.com/${encodeURIComponent(query)}`,
        topic,
        summary: `${query} 관련 보도 내용은 공식 상세문서 직접 일치 여부를 추가 확인해야 합니다.`,
        policy_confidence: {
          policy_confidence_score: query === "금융위" ? 18 : 12,
          risk_level: "high",
          action_priority: "medium",
        },
        policy_impact: {
          impact_level: "high",
          impact_direction: "uncertain",
        },
        final_decision: {
          policy_alert_level: "WATCH",
          final_score: query === "금융위" ? 18 : 12,
          action_recommendation: "더 직접적인 공식 보도자료나 정책 설명자료를 추가 확인하세요.",
          market_signal: "policy_uncertainty",
          decision_summary: "공식 상세문서 직접 일치가 부족해 사람 검토가 필요합니다.",
        },
        verification_card: {
          verdict_label: "draft_verified",
          verdict_confidence: 30,
          claim_text: `${query} 관련 보도 내용은 공식 상세문서 기준으로 추가 확인이 필요하다.`,
          evidence_summary: "공식 상세자료 후보는 확인했지만, 기사 핵심 주장과 직접 일치하는 공식 근거는 아직 충분하지 않습니다.",
          missing_context: "더 직접적인 공식 보도자료나 정책 설명자료 확인이 필요합니다.",
          last_checked_at: "2026-05-13T00:00:00.000Z",
          review_status: "draft_needs_official_confirmation",
          source_reliability_summary: {
            official_evidence_status: "candidate_only",
            official_detail_status: "weak_candidate_only",
            official_direct_match_score: 0,
            top_source_title: "공식 상세 근거 부족",
            official_mismatch: false,
          },
          debug_summary: {
            official_resolution_direct_matches: 0,
            official_resolution_contextual_matches: 0,
            official_resolution_weak_candidates: 0,
            evidence_strength_summary: { strong: 0, medium: 0, weak: 2 },
            evidence_quality_summary: {
              strong: 0,
              medium: 0,
              weak: 2,
              average_evidence_quality_score: 22,
            },
          },
          contradiction_summary: {
            contradiction_status: "insufficient_contradiction_evidence",
            overall_contradiction_risk: "low",
            summary: contradictionStatus,
          },
          evidence_sources: [],
          source_candidates: [
            {
              title: "공식기관 후보 문서",
              url: "https://www.fsc.go.kr/",
              source_type: "official_government",
              purpose: "primary_source",
              reliability_score: 85,
              official_direct_match_classification: "weak_official_candidate_only",
              official_match_reason: "공식기관 후보는 있으나 제목/본문이 넓은 주제 수준에서만 겹칩니다.",
            },
          ],
          claims: [`${query} 관련 보도 내용은 추가 확인이 필요하다.`],
          normalized_claims: [],
          evidence_snippets: [],
          contradiction_checks: [],
          bias_framing_analysis: [],
        },
      },
    ],
  };
}

const fixtures = [
  weakOfficialFixture({
    query: "금융위",
    title: "금융위 ELS 관련 제도 점검 보도",
    topic: "금융/정책",
    contradictionStatus: "반박 여부를 판단할 독립 근거가 부족해 공식 확인이 필요합니다.",
  }),
  weakOfficialFixture({
    query: "전세사기",
    title: "전세사기 피해 지원 관련 보도",
    topic: "전세사기",
    contradictionStatus: "반박 여부를 판단할 독립 근거가 부족해 공식 확인이 필요합니다.",
  }),
];

const requiredSections = [
  "정책 AI 검증 리포트",
  "검색어",
  "최고 경고 단계",
  "평균 신뢰도",
  "[검증 결과 요약 카드]",
  "[검토자 판단 대시보드]",
  "[검토자 액션]",
  "핵심 요약",
  "왜 이렇게 판단했나요?",
  "근거와 출처 요약",
  "AI 초안 판정",
  "공식 근거 상태",
  "공식 상세문서 상태",
  "의미 매칭 상태",
  "반박/모순 상태",
  "사람 검토 필요 여부",
  "마지막 확인 시간",
  "근거 강도",
  "근거 품질",
];

const overconfidentPhrases = [
  "공식적으로 확정됨",
  "직접 입증됨",
  "검증 완료",
  "공식 확인 완료",
  "공식 근거가 비교적 강합니다",
];

const cautiousPhrases = [
  "사람 검토 필요",
  "사람 검토 대기",
  "추가 공식 출처 확인 필요",
  "직접 일치 약함",
  "상세 공식문서 부족",
  "직접 일치하는 공식 근거는 아직 충분하지 않습니다",
];

function runFixture(fixture) {
  const sandbox = createSandbox();
  const fixtureJson = JSON.stringify(fixture);
  vm.runInContext(
    `
      currentReportContext = ${fixtureJson};
      selectedResultIndex = 0;
      renderResults(currentReportContext.results, 0);
      this.__reportText = buildReportText();
      this.__reportMarkdown = buildReportText();
    `,
    sandbox
  );
  return {
    text: sandbox.__reportText,
    markdown: sandbox.__reportMarkdown,
  };
}

for (const fixture of fixtures) {
  const { text, markdown } = runFixture(fixture);
  for (const output of [text, markdown]) {
    assert.ok(output && output.length > 200, `${fixture.query}: export output should be generated`);
    for (const section of requiredSections) {
      assert.ok(output.includes(section), `${fixture.query}: missing section "${section}"`);
    }
    for (const phrase of overconfidentPhrases) {
      assert.ok(!output.includes(phrase), `${fixture.query}: overconfident phrase leaked: ${phrase}`);
    }
    assert.ok(
      cautiousPhrases.some((phrase) => output.includes(phrase)),
      `${fixture.query}: expected cautious official-evidence wording`
    );
    assert.ok(output.includes("AI 초안 판정: 사람 검토 대기"), `${fixture.query}: AI draft should wait for human review`);
    assert.ok(!/\bundefined\b|\bnull\b|\[object Object\]/.test(output), `${fixture.query}: broken placeholder leaked`);
  }
}

console.log(`regression smoke tests passed (${fixtures.length} fixtures, text + markdown export)`);
