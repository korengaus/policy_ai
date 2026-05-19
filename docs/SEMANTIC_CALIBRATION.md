# Semantic Calibration (Phase 2 M5.6)

Calibration tooling that measures semantic matching quality against
fixture cases **before** real embeddings are turned on in production.

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

- Build a labeled calibration set from anonymized historical cases.
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
