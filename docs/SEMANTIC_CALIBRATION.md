# Semantic Calibration (Phase 2 M5.6 + M6.0)

Calibration tooling that measures semantic matching quality against
fixture cases **before** real embeddings are turned on in production.

## M6.0 update — expanded real-policy fixture

The calibration fixture (`tests/fixtures/semantic_calibration_cases.json`)
was expanded from 8 seed cases to 36 cases spanning realistic Korean
policy domains. The expansion is evaluation-only: nothing in M6.0 changes
verdict logic, alters Render configuration, runs live OpenAI calls, or
weakens the conservative wording the pipeline already uses.

Why the seed fixture wasn't enough:

- The M5.9 local OpenAI run showed real embeddings produce more confident
  raw `strong` labels than the deterministic surrogate (5 raw `strong` vs
  3 on the 8-case seed), and the guardrails capped half of them. With
  only 8 cases — most engineered as adversarial traps — the cap ratio
  alone was hard to interpret as a broad signal.
- Several realistic mismatch patterns (finality-only, negation /
  refutation, partial-support, same-topic-wrong-policy, local-vs-central,
  actor-mismatch) were not represented at all.
- A 30+ case set with ≥ 40% mismatch traps gives the comparison report
  enough signal to recommend a `debug_canary_candidate` honestly (or to
  refuse to) on the next live OpenAI evaluation.

What the expansion does NOT do:

- Does **not** enable semantic matching in production. Render keeps
  `SEMANTIC_MATCHING_ENABLED=false` / `EMBEDDING_PROVIDER=disabled`.
- Does **not** modify `render.yaml` or any production env var.
- Does **not** change `final_decision`, `policy_confidence`, the
  verification card, methodology wording, or export wording.
- Does **not** add a new external dependency, embedding cache backend, or
  background worker. pgvector / Qdrant / Redis / Celery decisions belong
  to a later phase.
- Does **not** introduce live OpenAI calls in CI. Live evaluation
  remains an opt-in local action behind the `RUN_LIVE_OPENAI_EVAL`
  operator confirmation token (see `SEMANTIC_PROVIDER_COMPARISON.md`).

### Category coverage in the expanded fixture

| Category | Count | What it exercises |
| --- | --- | --- |
| `direct_support` | 4 | Source directly supports the claim across multiple policy domains (housing-fraud relief, national health screening, disaster relief payout, dark-pattern consumer-protection regulation). |
| `contextual_only` | 4 | Source describes the same broad program but no specific amount / date / action (housing finance overview, tax overview, labor overview, education overview). |
| `unrelated` | 3 | Source is topically distinct from the claim (school lunch vs housing, traffic safety vs health, disaster drill vs consumer protection). |
| `number_mismatch` | 4 | Claim and source share the unit but disagree on the value (subsidy 100 vs 50만원, loan limit 8 000 vs 3 000만원, disaster subsidy 300 vs 100만원, VAT cut 5% vs 1%). |
| `date_mismatch` | 4 | Year and/or month disagree (pilot 2025 vs launch 2026, implementation year 2027 vs 2025 pilot, application period May vs June, 2026 launch vs 2024 review). |
| `eligibility_mismatch` | 3 | Universal eligibility claim vs source describing restrictions (income cap, age band, household income condition). |
| `finality_mismatch` | 3 | Claim treats the policy as final / 확정 but source is a budget proposal, pilot vs nationwide, or committee under review. |
| `negation_or_refutation` | 2 | Source explicitly refutes the claim (`사실이 아닙니다`, 보류, 정정). |
| `partial_support` | 2 | Source confirms the program exists but lacks the critical amount or date the claim asserts. |
| `same_topic_wrong_policy` | 2 | Same topic words but a different policy (youth-rent voucher vs youth-jeonse loan, SME emergency aid vs SME R&D budget). |
| `local_vs_central_authority` | 2 | Claim attributes action to central government but the source describes a local/지자체 or pilot scope. |
| `actor_mismatch` | 1 | Claim names one ministry (금융위원회) but the source is from another (국토교통부). |
| `no_body` | 1 | Source has metadata but empty `official_body_text` — agent must report `unavailable`. |
| `contradiction_like` | 1 | Claim says 최종 확정; source says 검토 진행 중 / 확정되지 않 — encoded as a separate seed bucket from the M6.0 `finality_mismatch` cases for backward compatibility. |

Mismatch / trap categories make up **~89%** of the fixture (everything
except `direct_support`). The deterministic baseline still passes 36/36
with `--fail-on-regression`, with the M5.7 guardrails actively capping
10/36 cases (raw distribution `strong:7, contextual:7, weak:21,
unavailable:1` → adjusted distribution `strong:1, contextual:4, weak:30,
unavailable:1`). `overstrong_count` remains 0.

### Authoring rules for new fixture cases

When extending the fixture further:

- Use short synthetic Korean text (1–3 sentences in `official_body_text`).
- Use `example.<ministry>.go.kr` URLs (e.g. `example.molit.go.kr`,
  `example.mosf.go.kr`, `example.fsc.go.kr`, `example.mois.go.kr`,
  `example.mss.go.kr`, `example.seoul.go.kr`). Never reference real
  individuals or copy real article text.
- For categories that map to a guardrail flag — `number_mismatch`,
  `date_mismatch`, `eligibility_mismatch`, `finality_mismatch`,
  `negation_or_refutation` — the `expected.risk_flags` list must include
  the corresponding flag name (`number_mismatch`, `date_mismatch`,
  `eligibility_mismatch`, `finality_mismatch`, `negation_mismatch`). The
  test suite enforces this (`FixtureShapeTests.test_mismatch_cases_declare_expected_risk_flags`).
- Keep `case_id` values descriptive and unique
  (`<category>_<scenario>` is the established convention). Uniqueness is
  enforced by `test_case_ids_are_unique`.
- At least 40% of cases must be mismatch / trap categories
  (`test_at_least_forty_percent_are_mismatch_traps`).

## A. Purpose

- Measure how often the semantic agent ranks the related official source
  above unrelated text.
- Identify false-positive patterns — cases where the agent reports
  `strong` semantic match even though the claim and source disagree on
  numbers, dates, eligibility, or status.
- Observe latency, cache, and provider behavior with the deterministic
  provider locally and (optionally) the real OpenAI provider when an
  operator chooses to pay for a live evaluation.
- Produce a repeatable scorecard so the same dataset can be re-run after
  threshold tuning or provider changes.

## B. What it does NOT do

- It does **not** verify claims or change verdicts. The evaluator only
  reads `semantic_evidence_summary`; `policy_decision`, `policy_scoring`,
  and `verification_card` ignore it entirely.
- It does **not** enable semantic matching in production. The Render
  defaults stay `SEMANTIC_MATCHING_ENABLED=false` /
  `EMBEDDING_PROVIDER=disabled`.
- It does **not** replace human review. Cases flagged as risky still
  belong in the reviewer queue.

## C. Deterministic local evaluation

```
python scripts/evaluate_semantic_calibration.py \
  --provider deterministic --show-failures \
  --markdown-out reports/semantic_calibration_deterministic.md
```

Runs against the bundled fixture
(`tests/fixtures/semantic_calibration_cases.json`) using the
deterministic hash-bigram provider. No network, no API key. The
`reports/` directory is gitignored, so generated reports stay local
unless you choose to commit them.

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--provider` | `deterministic` | `disabled`, `deterministic`, `openai`, `auto` |
| `--case-file` | `tests/fixtures/semantic_calibration_cases.json` | swap fixtures |
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

## D. OpenAI local evaluation

Opt-in. Triggers real embedding tokens — verify the model name on the
OpenAI dashboard first, and remember the cost adds up across cases.

PowerShell:

```powershell
$env:SEMANTIC_MATCHING_ENABLED = "true"
$env:EMBEDDING_PROVIDER = "openai"
$env:EMBEDDING_MODEL = "<currently-supported-embedding-model>"
$env:OPENAI_API_KEY = "<your-key>"
python scripts/evaluate_semantic_calibration.py `
  --provider openai --show-failures --show-matches `
  --markdown-out reports/semantic_calibration_openai.md
```

bash/zsh:

```bash
export SEMANTIC_MATCHING_ENABLED=true
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=<currently-supported-embedding-model>
export OPENAI_API_KEY=<your-key>
python scripts/evaluate_semantic_calibration.py \
  --provider openai --show-failures --show-matches \
  --markdown-out reports/semantic_calibration_openai.md
```

Pre-flight (M5.5 hardening still applies):

1. `EMBEDDING_MODEL` is **required**. Missing or empty makes the provider
   fail closed with `available=false`.
2. `OPENAI_API_KEY` is **required**. The provider never logs it.
3. The first run populates `embedding_cache` in the local `policy_ai.db`.
   Re-running against the same fixtures is free thereafter.
4. The evaluator only logs short structured lines — no full source
   bodies, no API keys, no full embedding vectors.

## E. How to disable

Default state. Drop the env vars or set:

```
SEMANTIC_MATCHING_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

Render keeps these defaults. Nothing in M5.6 modifies `render.yaml`.

## F. How to interpret results

- **`related_top1_rate`** — fraction of fixtures where the related
  official source ranked top-1. Closer to `1.0` is better. Anything
  below `0.8` is a red flag.
- **`overstrong_count`** — number of cases where the agent reported
  `strong` despite the fixture flagging the case as risky
  (number/date/eligibility/topic mismatch, contradiction-like). This
  should be **0** before considering production activation. Anything
  above 0 is the prime calibration signal — those are the cases where
  semantic similarity does not equal verification.
- **`support_cap_applied_count` / `total_critical_mismatches`** —
  emitted by the M5.7 critical-fact guardrails. `cap_applied` counts
  cases where the guardrail tightened the exposed `support_level`
  below the raw cosine label; `critical_mismatches` is the absolute
  count of disagreements detected across all top matches. See
  `docs/SEMANTIC_FACT_GUARDRAILS.md` for the cap semantics.
- **`raw_support_level_distribution`** — the distribution that would
  have been exposed *without* the guardrails. Comparing it against
  `support_level_distribution` shows how often the guardrails took
  effect for the run.
- **`support_level_distribution`** — informational. A heavy `weak`
  distribution on the deterministic provider is expected (the surrogate
  rarely crosses the strong threshold for non-trivial cases).
- **`average_runtime_ms`** — useful for latency budgeting before
  enabling on Render. Compare deterministic vs OpenAI runs.
- **`total_cache_hits` / `total_embedding_request_count`** — measure
  cache effectiveness. The first run on a fresh fixture has 0 cache
  hits; the second run should have many.

## G. Future activation gate

Before flipping `SEMANTIC_MATCHING_ENABLED=true` on Render:

1. Run the deterministic evaluator. Note current
   `related_top1_rate` and `overstrong_count` as the baseline.
2. Run the OpenAI evaluator locally on the same fixtures (and any
   additional historical cases you add).
3. **No risky case may be `strong`.** If `overstrong_count > 0` for the
   real provider, tune
   `SEMANTIC_MIN_SCORE_FOR_SUPPORT` upward (default `0.72`) and re-run.
4. Average runtime must fit the Render request budget (probe + main
   pipeline together).
5. Cache behavior must be observed on a repeat run — verify that
   `total_cache_hits` grows on the second run.
6. The existing smoke test (`scripts/smoke_async_job.py`) must still
   pass against a local server with the flags on. No verdict behavior
   should change.

Only when all six bullets clear should an operator consider a canary
rollout (e.g. `max_news=1` with monitoring).

## H. Future path

- Continue expanding the calibration fixture from anonymized historical
  cases — M6.0 grew the synthetic set to 36 cases, but a labeled
  real-claim set (still synthetic in URLs / personally identifying
  details) would let the OpenAI comparison surface domain-specific
  failure modes that a hand-authored fixture cannot. M6.2 added a
  separate 15-case anonymized real-claim-like batch under
  `tests/fixtures/semantic_real_claim_batch_sample.json`; see
  `docs/SEMANTIC_REAL_CLAIM_BATCH.md` for the dedicated evaluator and
  the activation-gate checklist that depends on it.
- Tune thresholds from the real-provider score distribution rather than
  the conservative defaults.
- Consider surfacing a small "임베딩 의미 매칭" card in the UI — only
  after the calibration scorecard is clean across providers.
- Migrate the cache to pgvector or Qdrant once volume justifies it (see
  `docs/SEMANTIC_MATCHING.md#future-path-to-pgvector--qdrant`).
- For multi-provider comparison (deterministic vs OpenAI) and
  activation-readiness recommendations, see
  `docs/SEMANTIC_PROVIDER_COMPARISON.md` (M5.8).

## Validation

```
python tests/test_semantic_calibration.py
python scripts/evaluate_semantic_calibration.py --provider deterministic --max-cases 3 --show-failures
python scripts/evaluate_semantic_calibration.py --provider openai --no-network --fail-on-unavailable
```

CI runs the first two on every push. None make a live API call.
