# Semantic Provider Comparison (Phase 2 M5.8)

Tooling to compare semantic-matching calibration across providers —
`deterministic`, optional `openai`, and a `disabled` sanity check — and
produce a conservative activation-readiness recommendation. This phase
is **evaluation-only**: nothing in M5.8 enables production embeddings,
changes verdict logic, or alters Render configuration.

## A. Purpose

- Compare the deterministic baseline against real OpenAI embeddings on
  the same calibration fixture.
- Measure ranking quality (`related_top1_rate`), false-positive risk
  (`overstrong_count`), guardrail load (`support_cap_applied_count`,
  `total_critical_mismatches`), latency (`average_runtime_ms`), and
  cache behavior in a single side-by-side report.
- Apply `semantic_thresholds.recommend_thresholds` to surface an
  activation-readiness label that operators can review before any canary
  decision.

## B. What this does NOT do

- It does **not** enable production embeddings. Render keeps
  `SEMANTIC_MATCHING_ENABLED=false` / `EMBEDDING_PROVIDER=disabled`.
- It does **not** change verdicts. `policy_decision`, `policy_scoring`,
  and `verification_card` do not import the new module or read its
  output — pinned by `tests/test_semantic_provider_comparison.py:VerdictIsolationTests`.
- It does **not** verify claims. Every report includes the conservative
  disclaimer: *semantic match strength is metadata only*.
- It does **not** replace human review. Cases the guardrails capped to
  `weak` / `contextual` still belong in the reviewer queue.
- It does **not** introduce pgvector, Qdrant, Redis, or Celery — those
  decisions belong to a later phase.

## C. Deterministic comparison (offline)

```
python scripts/compare_semantic_providers.py \
  --providers deterministic,disabled \
  --no-network \
  --markdown-out reports/semantic_provider_comparison_deterministic.md
```

Runs the bundled fixture (`tests/fixtures/semantic_calibration_cases.json`)
through the deterministic hash provider plus the disabled sanity-check
path. No network, no API key. `reports/` is gitignored, so generated
reports stay local unless explicitly committed.

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--providers` | `deterministic` | Comma-separated, any of `deterministic,openai,disabled` |
| `--case-file` | `tests/fixtures/semantic_calibration_cases.json` | Swap fixtures |
| `--max-cases` | – | Cap evaluated cases |
| `--no-network` | off | Block any live OpenAI call regardless of env |
| `--require-live-confirmation` | on | Refuse live OpenAI calls without an explicit token (default) |
| `--no-require-live-confirmation` | – | Disable the live-confirmation gate (not recommended) |
| `--live-confirm-token` | – | Pass `LIVE_OPENAI_OK` to authorize a live OpenAI call |
| `--show-failures` / `--show-matches` | off | Verbose case-level diagnostics |
| `--json-out` / `--markdown-out` | – | Persist the report |

## D. OpenAI comparison (local / manual, opt-in)

A live OpenAI call is gated behind two things at once:

1. `--live-confirm-token LIVE_OPENAI_OK` on the command line, AND
2. A fully configured environment (`SEMANTIC_MATCHING_ENABLED=true`,
   `EMBEDDING_PROVIDER=openai`, `EMBEDDING_MODEL`, `OPENAI_API_KEY`).

Missing the token exits with code 3. Token correct but env missing exits
with code 2. The script never logs the API key and never prints raw
source bodies.

PowerShell:

```powershell
$env:SEMANTIC_MATCHING_ENABLED = "true"
$env:EMBEDDING_PROVIDER = "openai"
$env:EMBEDDING_MODEL = "<currently-supported-embedding-model>"
$env:OPENAI_API_KEY = "<your-key>"
python scripts/compare_semantic_providers.py `
  --providers deterministic,openai `
  --live-confirm-token LIVE_OPENAI_OK `
  --show-failures --show-matches `
  --markdown-out reports/semantic_provider_comparison_openai.md
```

bash/zsh:

```bash
export SEMANTIC_MATCHING_ENABLED=true
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=<currently-supported-embedding-model>
export OPENAI_API_KEY=<your-key>
python scripts/compare_semantic_providers.py \
  --providers deterministic,openai \
  --live-confirm-token LIVE_OPENAI_OK \
  --show-failures --show-matches \
  --markdown-out reports/semantic_provider_comparison_openai.md
```

Use the placeholder `<currently-supported-embedding-model>` and verify
the model name on the OpenAI dashboard before running. Do not hardcode
a model in checked-in scripts. Do **not** commit generated reports — the
`reports/` directory is gitignored on purpose.

Live OpenAI comparison is intentionally **not** part of CI. CI only runs
the deterministic + disabled offline smoke.

## E. How to interpret results

- **`related_top1_rate`** — fraction of fixtures where the related
  official source ranked top-1. ≥ 0.80 is the canary candidate band; <
  0.60 is "not ready"; in between is "local-only."
- **`overstrong_count`** — number of cases where the agent reported
  `strong` despite the fixture flagging the case as risky. Must be **0**
  before any activation. Anything > 0 forces `not_ready`.
- **`support_cap_applied_count`** — number of cases where the M5.7
  guardrails tightened the exposed `support_level` below the raw cosine
  label. A high value (≥ 50% of cases) means embeddings alone are
  insufficient and the guardrails are doing meaningful safety work.
- **`total_critical_mismatches`** — total number of disagreements the
  guardrails detected across all top matches. Surfaces in the safety
  notes.
- **`average_runtime_ms`** — useful for latency budgeting. ≥ 1500 ms
  triggers a warning; ≥ 4000 ms forces `not_ready`.
- **`total_cache_hits` / `total_embedding_request_count`** — cache
  effectiveness. Repeat runs should approach 100% cache hits.
- **`activation_readiness`** — one of `not_ready`, `local_only`, or
  `debug_canary_candidate`. The deterministic provider alone can never
  exceed `local_only` — real-embedding measurement is required for any
  canary recommendation.

## F. Activation gates

Before considering a Render canary (still **not** in this milestone):

1. OpenAI comparison completed locally on the bundled fixture **and**
   at least one expanded historical case set.
2. `overstrong_count = 0` for the OpenAI provider.
3. Every mismatch case capped by guardrails — no number/date/eligibility
   disagreement leaves the agent with `support_level=strong`.
4. `related_top1_rate ≥ 0.80` for the OpenAI provider.
5. `average_runtime_ms` within the Render request budget (probe + main
   pipeline combined).
6. No verdict-side change. `final_decision`, `policy_confidence`, and
   `verification_card` continue to ignore the semantic summary.
7. Render smoke test (`scripts/smoke_async_job.py`) still passes against
   the deployed instance with the current Render env.
8. Human-review language preserved verbatim
   (`사람 검토 필요`, `의미 매칭 근거 부족`, `공식 출처 확인 필요`).

Only when all eight clear should an operator consider a debug-only
canary (e.g. `max_news=1`, internal-facing endpoint, no UI surface).

## G. M6.0 update — expanded fixture

The bundled fixture grew from 8 seed cases to 36 in M6.0. See
`docs/SEMANTIC_CALIBRATION.md#m60-update--expanded-real-policy-fixture`
for the full category breakdown. M6.0 implications for the comparison
script:

- The deterministic baseline now reports a richer scorecard
  (~10 capped cases out of 36 ≈ 28% cap ratio, well-distributed risk
  flags) so the activation-readiness recommendation has more signal to
  work with.
- The next local OpenAI comparison (still gated by `RUN_LIVE_OPENAI_EVAL`)
  runs against the broader fixture; cap-ratio, raw-vs-adjusted strong
  distribution, and `related_top1_rate` are all expected to shift
  compared to the M5.9 run on 8 cases.
- The `compare_semantic_providers.py` CLI did not change. No new env
  vars, no new flags, no live-call behavior change.

## H. Future path

- Continue expanding the calibration set from anonymized historical
  cases — M6.0 is still synthetic-but-realistic, not labeled real-claim
  data.
- Tune `SEMANTIC_MIN_SCORE_FOR_SUPPORT` / `SEMANTIC_MIN_SCORE_FOR_CONTEXT`
  from the OpenAI score distribution. M5.8 deliberately leaves
  `recommended_thresholds.support` / `context` as `null` because tuning
  before a real-provider comparison would be premature.
- Consider surfacing a small `semantic_evidence_summary` card in the
  reviewer UI — only after the comparison scorecard is clean across
  providers.
- Migrate the cache to pgvector or Qdrant once production volume
  justifies it (see `docs/SEMANTIC_MATCHING.md#future-path-to-pgvector--qdrant`).

## I. Validation

```
python tests/test_semantic_provider_comparison.py
python scripts/compare_semantic_providers.py --providers deterministic,disabled --max-cases 3 --no-network --show-failures
python scripts/compare_semantic_providers.py --providers openai --no-network
```

CI runs the first two on every push. No live OpenAI call happens in CI.
