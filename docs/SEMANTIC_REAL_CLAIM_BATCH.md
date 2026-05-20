# Semantic Real-Claim Evaluation Batch (Phase 2 M6.2 + M6.4)

A local-only evaluation batch of anonymized real-claim-like Korean policy
/ news scenarios. It exists to bridge the gap between the synthetic
calibration fixture (M6.0, 36 cases) and a production canary: **before**
any Render activation, the operator should be able to run deterministic +
live OpenAI evaluation against scenarios that resemble real user
queries, not just the engineered traps from the calibration fixture.

## M6.4 update — expanded historical-style real-claim batch

The batch was expanded from 15 cases (M6.2) to **72 cases** (M6.4)
spanning 12 categories and 14+ policy domains. The expansion is
evaluation-only: nothing in M6.4 changes verdict logic, alters Render
configuration, runs live OpenAI calls, or weakens the conservative
wording the pipeline already uses.

Why the 15-case batch wasn't enough:

- M6.3 live OpenAI on the 15-case batch showed clean results
  (`overstrong=0`, `top1=1.000`, actor/scope cases below the strong
  threshold) — but with only 15 cases, the activation-readiness signal
  was too narrow to detect uncommon failure modes like
  `same_topic_wrong_policy`, `actor_mismatch`, or partial support across
  multiple domains.
- The expanded batch (target 50–100; landed on 72) carries at least
  5 cases per guardrail-mapped category and at least 8 cases combined
  across the highest-risk scope-mismatch categories (`actor_mismatch`,
  `local_vs_central_authority`, `same_topic_wrong_policy`). That gives
  the next OpenAI run enough signal to detect a raw-`strong` false
  positive on those categories if one appears.

What the expansion does NOT do:

- Does **not** enable semantic matching in production. Render keeps
  `SEMANTIC_MATCHING_ENABLED=false` / `EMBEDDING_PROVIDER=disabled`.
- Does **not** modify `render.yaml` or any production env var.
- Does **not** change `final_decision`, `policy_confidence`, the
  verification card, methodology wording, or export wording.
- Does **not** add a new external dependency, embedding cache backend,
  or background worker. pgvector / Qdrant / Redis / Celery decisions
  belong to a later phase.
- Does **not** introduce live OpenAI calls in CI. Live evaluation
  remains an opt-in local action behind the `RUN_LIVE_OPENAI_EVAL`
  operator confirmation token + `--live-confirm-token LIVE_OPENAI_OK`
  CLI gate.

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
**72 anonymized real-claim-like cases** across the following category
distribution (M6.4):

| category | count | description |
| --- | --- | --- |
| `direct_support` | 13 | Source directly supports the claim across 13 policy domains (housing fraud, childcare, education, SME, labor, energy, agriculture, health, voucher, legal aid, transport, consumer protection). |
| `eligibility_mismatch` | 8 | Claim asserts universal eligibility (`누구나`, `모든 청년`, `모든 국민`, `모든 가구`); source describes age / income / residence restrictions. |
| `number_mismatch` | 8 | Claim and source share the unit but disagree on the value (만원 / 억원 / %). |
| `date_mismatch` | 7 | Claim and source disagree on year, month, or application period. |
| `finality_mismatch` | 7 | Claim treats policy as final (`확정`, `시행`); source is budget proposal / pilot / under review / under negotiation. |
| `negation_or_refutation` | 6 | Source explicitly refutes (`사실이 아닙니다`, `보류`, `정정`). |
| `partial_support` | 6 | Source confirms program exists but lacks the critical amount / date / eligibility the claim asserts. |
| `same_topic_wrong_policy` | 6 | Same topic words, different policy (voucher vs loan, grant vs R&D, rate vs screening, loan vs guarantee, employment vs internship). |
| `local_vs_central_authority` | 4 | Claim attributes action to central government; source is Seoul / Busan / Gyeonggi / 시도교육청 scope. |
| `actor_mismatch` | 3 | Claim names one ministry; source is from a different ministry on a different but topically related policy. |
| `no_body` | 2 | Source has metadata but empty `official_body_text` — agent must report `unavailable`. |
| `contextual_only` | 2 | Source describes the broad program but no specific amount / date / action. |
| **total** | **72** | |

Mismatch / trap categories make up **59/72 = 82%** of the batch
(everything except `direct_support`). The combined
actor / local-vs-central / same-topic-wrong-policy floor is satisfied at
**13** cases — well above the M6.4 minimum of 8 combined.

Per-category minimums enforced by `tests/test_semantic_real_claim_batch.py`:

- `direct_support` ≥ 10
- `number_mismatch` ≥ 6
- `date_mismatch` ≥ 5
- `eligibility_mismatch` ≥ 6
- `finality_mismatch` ≥ 6
- `negation_or_refutation` ≥ 5
- `partial_support` ≥ 5
- `same_topic_wrong_policy` ≥ 5
- `no_body` ≥ 2
- combined `actor_mismatch + local_vs_central_authority + same_topic_wrong_policy` ≥ 8

Domain coverage spans 전세사기, 청년 월세, 소상공인 지원, 건강보험, 세금,
재난지원금, 교육비, 교통비, 보육/육아, 금융/대출, 노동/고용, 소비자 보호,
지역화폐, 에너지 바우처, 농어민 지원, 부동산 / 주택 정책, 학자금, and
백신/예방접종.

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

- M6.4 grew the batch from 15 to 72 cases, hitting the spec's 50–100
  range and the preferred ~72 target. The next escalation is to replace
  the synthetic batch with anonymized **historical claims** from
  production logs — keep URLs / names sanitized, but use real claim
  shapes and real official-body excerpts.
- If a future live OpenAI run on the expanded batch (or on a historical
  batch) surfaces raw `strong` on `actor_mismatch` /
  `same_topic_wrong_policy` / `local_vs_central_authority` patterns,
  extend `semantic_fact_guardrails.py` with a small, deterministic
  actor / policy-scope extractor (M6.5 candidate). Do not ship a canary
  before guardrails cover the failure modes the live data reveals.
- Only after a clean live-OpenAI run against an anonymized historical
  batch should an operator flip `SEMANTIC_MATCHING_ENABLED=true` on
  Render — and even then, as a debug-only canary, not a user-facing
  rollout.

## I. Validation

```
python tests/test_semantic_real_claim_batch.py
python scripts/evaluate_real_claim_batch.py --provider deterministic --no-network --show-failures
python scripts/evaluate_real_claim_batch.py --provider openai --no-network --fail-on-unavailable
```

CI runs the first two on every push. None make a live API call.
