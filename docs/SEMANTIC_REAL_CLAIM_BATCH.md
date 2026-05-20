# Semantic Real-Claim Evaluation Batch (Phase 2 M6.2)

A small, local-only evaluation batch of anonymized real-claim-like Korean
policy / news scenarios. It exists to bridge the gap between the
synthetic calibration fixture (M6.0, 36 cases) and a production canary:
**before** any Render activation, the operator should be able to run
deterministic + live OpenAI evaluation against scenarios that resemble
real user queries, not just the engineered traps from the calibration
fixture.

## A. Purpose

- Evaluate semantic matching quality on production-like anonymized claim
  scenarios across realistic Korean policy domains (housing,
  small-business aid, youth rent, health insurance, tax, disaster
  relief, education, transport, childcare, finance, consumer protection,
  negation / refutation).
- Surface mismatch patterns the calibration fixture may have missed —
  particularly actor / scope / policy-name ambiguities that are common in
  real news but harder to engineer in synthetic test cases.
- Give the operator a repeatable scorecard to inspect *before* flipping
  any Render env var or considering a debug canary.

## B. What this does NOT do

- It does **not** enable production embeddings. Render keeps
  `SEMANTIC_MATCHING_ENABLED=false` / `EMBEDDING_PROVIDER=disabled`.
- It does **not** change verdicts. `policy_decision`, `policy_scoring`,
  and `verification_card` do not import the new module or read its
  output — pinned by
  `tests/test_semantic_real_claim_batch.py::VerdictIsolationTests`.
- It does **not** verify claims. Every report includes the conservative
  disclaimer: *semantic match strength is metadata only*.
- It does **not** replace human review. Cases the guardrails capped to
  `weak` / `contextual` still belong in the reviewer queue.
- It does **not** use real private data. The fixture text is synthetic,
  URLs use `example.<ministry>.go.kr` hosts, and no real names appear.
- It does **not** introduce pgvector, Qdrant, Redis, or Celery.

## C. Fixture description

The bundled fixture
(`tests/fixtures/semantic_real_claim_batch_sample.json`) contains
**15 anonymized real-claim-like cases** across the following categories
and policy domains:

| case_id | category | domain |
| --- | --- | --- |
| `real_housing_fraud_legal_finance` | direct_support | 전세사기 / housing fraud |
| `real_housing_fraud_automatic_payment_claim` | eligibility_mismatch | 전세사기 / housing fraud |
| `real_youth_rent_universal_claim` | eligibility_mismatch | 청년 월세 / youth rent |
| `real_sme_emergency_grant_amount` | number_mismatch | 소상공인 지원 / small-business aid |
| `real_health_insurance_general_overview` | contextual_only | 건강보험 / health insurance |
| `real_tax_cut_date_and_finality_mismatch` | date_mismatch | 세금 감면 / tax cut |
| `real_disaster_relief_partial_support` | partial_support | 재난지원금 / disaster relief |
| `real_education_local_vs_central` | local_vs_central_authority | 교육비 지원 / education |
| `real_transport_pilot_vs_nationwide` | finality_mismatch | 교통비 지원 / transport |
| `real_childcare_universal_vs_conditional` | eligibility_mismatch | 보육 / childcare |
| `real_finance_actor_mismatch` | actor_mismatch | 금융 / 대출 |
| `real_consumer_protection_refund_guide` | direct_support | 소비자 보호 / consumer protection |
| `real_negation_fake_youth_subsidy` | negation_or_refutation | 청년 보조금 정정 보도 |
| `real_negation_policy_withdrawal` | negation_or_refutation | 종부세 추진 보류 |
| `real_budget_proposal_finality_mismatch` | finality_mismatch | 예산안 국회 제출 |

Mismatch / trap categories make up **13/15 = 87%** of the batch (every
category except `direct_support`).

The schema matches `tests/fixtures/semantic_calibration_cases.json` so
the same evaluator helpers (`semantic_calibration.evaluate_case`,
`semantic_calibration.summarize_calibration_results`) work without
modification. Cases declare the canonical M5.7 guardrail flag name in
`expected.risk_flags` where applicable (`number_mismatch`,
`date_mismatch`, `eligibility_mismatch`, `finality_mismatch`,
`negation_mismatch`); categories without a direct guardrail
(`actor_mismatch`, `local_vs_central_authority`, `partial_support`) may
carry documentation-only labels.

## D. Deterministic local evaluation (offline, CI-safe)

```
python scripts/evaluate_real_claim_batch.py \
  --provider deterministic --no-network \
  --show-failures --show-matches
```

Runs against the bundled fixture using the deterministic hash-bigram
provider. No network, no API key required. Same scorecard shape as
`evaluate_semantic_calibration.py` (M5.6), because the underlying
`semantic_calibration.summarize_calibration_results` helper is shared.

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--provider` | `deterministic` | `disabled`, `deterministic`, `openai`, `auto` |
| `--case-file` | `tests/fixtures/semantic_real_claim_batch_sample.json` | swap fixtures |
| `--max-cases` | – | cap evaluated cases |
| `--show-failures` | off | print only failed cases with reasons |
| `--show-matches` | off | print top match snippets (truncated) |
| `--json-out` | – | structured per-case + scorecard JSON |
| `--csv-out` | – | per-case CSV row export |
| `--markdown-out` | – | human-readable report file |
| `--threshold-support` | – | override `SEMANTIC_MIN_SCORE_FOR_SUPPORT` for this run |
| `--threshold-context` | – | override `SEMANTIC_MIN_SCORE_FOR_CONTEXT` for this run |
| `--no-network` | off | block any live network call; pairs with `--provider openai` |
| `--fail-on-unavailable` | off | exit code 2 if provider reports `available=False` |
| `--fail-on-regression` | off | exit code 3 if any case failed its expectations |
| `--live-confirm-token` | – | required for live OpenAI; pass `LIVE_OPENAI_OK` |

Generated reports go under `reports/`, which is gitignored — they stay
local unless you explicitly stage them. Do not commit reports.

## E. OpenAI no-network guard

```
python scripts/evaluate_real_claim_batch.py --provider openai --no-network
```

`--no-network` forces the OpenAI provider offline regardless of env. The
provider reports `available=False`, no live call is attempted, and the
API key is never logged. Use this as a sanity check before running the
live path.

## F. Optional live OpenAI local evaluation (opt-in)

Live OpenAI evaluation is gated by **two** things at once:

1. `--live-confirm-token LIVE_OPENAI_OK` on the command line, AND
2. A fully configured environment (`SEMANTIC_MATCHING_ENABLED=true`,
   `EMBEDDING_PROVIDER=openai`, `EMBEDDING_MODEL`, `OPENAI_API_KEY`).

Missing the token returns exit code 4. Token correct but env missing
returns exit code 2. The script never logs the API key.

PowerShell:

```powershell
$env:SEMANTIC_MATCHING_ENABLED = "true"
$env:EMBEDDING_PROVIDER = "openai"
$env:EMBEDDING_MODEL = "text-embedding-3-small"
$env:OPENAI_API_KEY = "<your-key>"
python scripts/evaluate_real_claim_batch.py `
  --provider openai `
  --live-confirm-token LIVE_OPENAI_OK `
  --show-failures --show-matches `
  --json-out reports/semantic_real_claim_batch_openai.json `
  --markdown-out reports/semantic_real_claim_batch_openai.md
```

bash/zsh:

```bash
export SEMANTIC_MATCHING_ENABLED=true
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=text-embedding-3-small
export OPENAI_API_KEY=<your-key>
python scripts/evaluate_real_claim_batch.py \
  --provider openai \
  --live-confirm-token LIVE_OPENAI_OK \
  --show-failures --show-matches \
  --json-out reports/semantic_real_claim_batch_openai.json \
  --markdown-out reports/semantic_real_claim_batch_openai.md
```

Important:

- Do **not** paste the API key into chat or commit it anywhere.
- Do **not** commit generated reports. `reports/` is gitignored.
- Do **not** run live evaluation in CI. CI exercises only the
  deterministic + `--no-network` path.

## G. Activation gate

Before considering a Render canary (still **not** in this milestone):

1. Expanded synthetic fixture passes (`evaluate_semantic_calibration.py
   --provider deterministic --fail-on-regression`, 36/36 in M6.0).
2. Real-claim batch passes — both deterministic
   (`evaluate_real_claim_batch.py --provider deterministic --no-network
   --fail-on-regression`) and live OpenAI (manual, gated by
   `LIVE_OPENAI_OK`).
3. `overstrong_count = 0` for both providers across both fixtures.
4. Actor / scope / same-topic-wrong-policy cases stay below the
   `SEMANTIC_MIN_SCORE_FOR_SUPPORT` threshold (default `0.72`) on the
   OpenAI provider. If any case raw-scores `strong` on those categories,
   that is the trigger to extend `semantic_fact_guardrails.py` with an
   actor / policy-scope extractor before proceeding.
5. `average_runtime_ms` fits the Render request budget (probe + main
   pipeline together).
6. No verdict-side change. `final_decision`, `policy_confidence`, and
   `verification_card` continue to ignore the semantic summary.
7. Render smoke test (`scripts/smoke_async_job.py`) still passes against
   the deployed instance with the current Render env.
8. Human-review language preserved verbatim (`사람 검토 필요`,
   `의미 매칭 근거 부족`, `공식 출처 확인 필요`).

Only when all eight clear should an operator consider a debug-only
canary (e.g. `max_news=1`, internal-facing endpoint, no UI surface).

## H. Future path

- Replace the synthetic batch with anonymized **historical claims** from
  production logs — keep URLs / names sanitized, but use real claim
  shapes and real official-body excerpts.
- Grow the batch to 50–100 cases so the activation-readiness signal
  becomes statistically meaningful across domains.
- If the live OpenAI run on the expanded historical batch surfaces raw
  `strong` on `actor_mismatch` / `same_topic_wrong_policy` / `local_vs_central_authority`
  patterns, extend `semantic_fact_guardrails.py` with a small,
  deterministic actor / policy-scope extractor (M6.3 candidate). Do not
  ship a canary before guardrails cover the failure modes the live data
  reveals.
- Only after a clean run against the historical batch should an operator
  flip `SEMANTIC_MATCHING_ENABLED=true` on Render — and even then, as a
  debug-only canary, not a user-facing rollout.

## I. Validation

```
python tests/test_semantic_real_claim_batch.py
python scripts/evaluate_real_claim_batch.py --provider deterministic --no-network --show-failures
python scripts/evaluate_real_claim_batch.py --provider openai --no-network --fail-on-unavailable
```

CI runs the first two on every push. None make a live API call.
