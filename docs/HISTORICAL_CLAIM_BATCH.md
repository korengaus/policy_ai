# Historical Claim Batch Builder (Phase 2 M7.0)

A local-only utility that scans existing analysis artifacts — the
`reports/policy_analysis_*.json` files and the SQLite `analysis_results`
table — and assembles an anonymized semantic evaluation batch with the
same schema as `tests/fixtures/semantic_real_claim_batch_sample.json`.
The generated batch is meant to feed
`scripts/evaluate_real_claim_batch.py` locally **before** any Render
debug canary decision.

## A. Purpose

- Bridge the gap between the synthetic real-claim batch (M6.4, 72
  hand-authored cases) and a Render-side activation by running
  semantic evaluation on claim/source shapes that came from real local
  analysis runs.
- Provide a repeatable way to grow the evaluation set as more local
  artifacts accumulate — re-running the builder on the same inputs
  produces the same case IDs (deterministic hashing) so diffs are
  meaningful.
- Surface mismatch patterns the synthetic fixtures may have missed —
  the deterministic M5.7 / M6.6 guardrails infer per-case categories so
  the operator can spot trends (e.g., "the historical batch is 60%
  partial_support, the synthetic batch is 0%").

## B. What this does NOT do

- It does **not** enable production embeddings. Render keeps
  `SEMANTIC_MATCHING_ENABLED=false` / `EMBEDDING_PROVIDER=disabled`.
- It does **not** change verdicts. `policy_decision`, `policy_scoring`,
  and `verification_card` do not import the new module or read its
  output — pinned by
  `tests/test_historical_claim_batch_builder.py::VerdictIsolationTests`.
- It does **not** call OpenAI. The builder is pure-stdlib + the M5.7 /
  M6.6 guardrails, no embedding network calls.
- It does **not** publish anything. Generated output goes under
  `reports/` which is gitignored.
- It does **not** commit generated output. The summary markdown is also
  gitignored. Operators must explicitly stage anything they want to
  preserve, which is intentionally not the default workflow.

## C. Dry-run

```
python scripts/build_historical_claim_batch.py --dry-run --max-cases 100
```

Scans `reports/` and `policy_ai.db` (whichever exist), runs the full
extraction + anonymization + category-inference pipeline, prints a
single-line scorecard plus the category and risk-flag distributions,
and writes **nothing**. Use this to preview what the builder would emit
before committing to a generated file.

Example output line:

```
[build-historical] reports_scanned=471 sqlite_rows=105 candidates=739 \
  emitted=200 skipped=0 elapsed=1.33s anonymized=True
  category_distribution={'finality_mismatch': 3, 'no_body': 40, \
                         'partial_support': 41, 'same_topic_wrong_policy': 8, \
                         'unknown_historical': 108}
  risk_flag_distribution={'finality_mismatch': 3, \
                          'heuristic_unknown_historical': 108, \
                          'missing_critical_fact': 52, \
                          'official_body_missing': 40, \
                          'policy_scope_mismatch': 8}
```

## D. Generate a batch

```
python scripts/build_historical_claim_batch.py \
  --output reports/semantic_historical_claim_batch.generated.json \
  --max-cases 100 \
  --overwrite
```

Writes the JSON batch and a markdown summary. The summary path defaults
to `reports/semantic_historical_claim_batch.summary.md`. Both files are
gitignored. Re-running the builder is safe — `--overwrite` is required
to replace an existing output file (exit code 2 without it).

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--reports-dir` | `reports/` | Where to scan for `policy_analysis_*.json` |
| `--sqlite-db` | `policy_ai.db` | SQLite DB to scan `analysis_results` from |
| `--output` | `reports/semantic_historical_claim_batch.generated.json` | Output JSON path |
| `--summary-out` | `reports/semantic_historical_claim_batch.summary.md` | Summary markdown |
| `--max-cases` | `100` | Cap emitted cases |
| `--min-cases` | `10` | Floor for `--strict` mode |
| `--source` | `both` | `reports` / `sqlite` / `both` |
| `--include-debug` | off | Embed `metadata` block per case |
| `--overwrite` | off | Replace existing output |
| `--strict` | off | Exit 3 if fewer than `--min-cases` cases emit |
| `--dry-run` | off | Print summary, write nothing |
| `--seed` | – | Seed for shuffling; default ordering is stable by case_id |

## E. Evaluate deterministic (offline, CI-safe)

```
python scripts/evaluate_real_claim_batch.py \
  --case-file reports/semantic_historical_claim_batch.generated.json \
  --provider deterministic --no-network \
  --show-failures --show-matches
```

The generated batch uses the same schema as
`tests/fixtures/semantic_real_claim_batch_sample.json`, so the M6.2 /
M6.6 evaluator works without modification. Pass `--fail-on-regression`
when re-running to detect new failure modes.

## F. Evaluate live OpenAI (opt-in, local-only)

Live OpenAI on the generated batch is gated by the same two-factor
authorization as every other live milestone: an in-shell env, **plus**
the `--live-confirm-token LIVE_OPENAI_OK` CLI flag. CI never runs this
path.

PowerShell:

```powershell
$env:SEMANTIC_MATCHING_ENABLED = "true"
$env:EMBEDDING_PROVIDER = "openai"
$env:EMBEDDING_MODEL = "text-embedding-3-small"
$env:OPENAI_API_KEY = "<your-key>"
python scripts/evaluate_real_claim_batch.py `
  --case-file reports/semantic_historical_claim_batch.generated.json `
  --provider openai `
  --live-confirm-token LIVE_OPENAI_OK `
  --show-failures --show-matches `
  --json-out reports/semantic_historical_claim_batch_openai.json `
  --markdown-out reports/semantic_historical_claim_batch_openai.md
```

bash/zsh:

```bash
export SEMANTIC_MATCHING_ENABLED=true
export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=text-embedding-3-small
export OPENAI_API_KEY=<your-key>
python scripts/evaluate_real_claim_batch.py \
  --case-file reports/semantic_historical_claim_batch.generated.json \
  --provider openai \
  --live-confirm-token LIVE_OPENAI_OK \
  --show-failures --show-matches \
  --json-out reports/semantic_historical_claim_batch_openai.json \
  --markdown-out reports/semantic_historical_claim_batch_openai.md
```

## G. Safety

- **Never paste the API key into chat.** The shell env is the only
  trustworthy carrier.
- **Never commit generated reports** — the `reports/` directory is
  gitignored on purpose. If you want to share a sample, anonymize it
  again and place it under `tests/fixtures/` only after a manual
  review.
- **Review the generated batch before live evaluation.** Open the JSON,
  scan claim_text and source URLs, confirm anonymization survived the
  round-trip. The builder is conservative but the synthetic-name
  redaction (only 3-syllable + 씨) is a coarse heuristic, not a full
  PII scrubber.
- **Treat generated categories as heuristic, not gold labels.** The
  M5.7 / M6.6 guardrails are deterministic over text — they're useful
  for surfacing *kinds* of mismatch, but they can't tell you whether
  the analysis run that produced the artifact was actually correct.
- **Do not feed the generated batch into the verdict pipeline.** It's
  evaluation data, not training data. The pipeline's reviewer-side
  artifacts must continue to come from the existing official-evidence
  retrieval, not from this batch.

## H. Anonymization rules

The builder anonymizes by default (`--anonymize` is on). The
transformations applied:

| field | rule |
| --- | --- |
| URL | host replaced with `example.generated.<kind>` (`go.kr`, `seoul.go.kr`, `busan.go.kr`, `gg.go.kr`, etc.); query string and fragment dropped; path replaced with `/source/<sha256 prefix>`. |
| title | truncated to 160 chars; PII patterns scrubbed. |
| publisher | replaced with `Example Source (<hash>)` so different publishers don't collapse but no real name leaks. |
| body text | truncated to 1000 chars; emails, phones, resident IDs, long numeric IDs, and `<3-syllable>씨` honorific names redacted to placeholders (`[이메일]`, `[전화번호]`, `[주민번호]`, `[식별자]`, `[이름]씨`). |
| claim text | truncated to 300 chars; same PII rules. |
| Korean policy vocabulary | passes through unchanged (`정부`, `전세사기`, `청년`, `보조금`, `지원금`, `대출`, `바우처`, `시행`, `검토 중`, etc.). |

## I. Category / risk inference

For each (claim, source) pair, the builder runs
`semantic_fact_guardrails.compare_critical_facts(claim, body)` and maps
the highest-priority emitted flag to a fixture category:

| guardrail flag | inferred category |
| --- | --- |
| `number_mismatch` | `number_mismatch` |
| `date_mismatch` | `date_mismatch` |
| `eligibility_mismatch` | `eligibility_mismatch` |
| `finality_mismatch` | `finality_mismatch` |
| `negation_mismatch` | `negation_or_refutation` |
| `policy_scope_mismatch` (M6.6) | `same_topic_wrong_policy` |
| `actor_scope_mismatch` / `local_vs_central` (M6.6) | `local_vs_central_authority` |
| `missing_critical_fact` only | `partial_support` |
| no flag, no source body | `no_body` |
| no flag, body present | `unknown_historical` (documentation-only flag `heuristic_unknown_historical`) |

`expected.should_not_be_strong` defaults to `True` for every category
except `unknown_historical` and clean direct-support patterns. The
fixture's expected `support_level` is always `any` — the builder does
not invent ground-truth labels.

## J. Activation gate (cumulative with prior milestones)

Before any Render debug canary:

1. Synthetic calibration fixture (`tests/fixtures/semantic_calibration_cases.json`, 36 cases) passes deterministic + live OpenAI with `overstrong_count = 0`. ✓ (M6.0 / M6.1)
2. Synthetic real-claim batch (`tests/fixtures/semantic_real_claim_batch_sample.json`, 72 cases) passes deterministic + live OpenAI with `overstrong_count = 0`. ✓ (M6.4 / M6.5 / M6.6)
3. **Generated historical batch has at least 50 usable cases** with a category mix that exercises every M5.7 / M6.6 guardrail flag.
4. **Deterministic evaluation on the generated batch passes with `overstrong_count = 0`.**
5. **Live OpenAI evaluation on the generated batch passes with `overstrong_count = 0`.**
6. Actor / scope / wrong-policy cases stay safe (either cosine < 0.72 or a guardrail flag fires and caps).
7. Runtime within Render's request budget.
8. No verdict-side change. `final_decision`, `policy_confidence`, and `verification_card` continue to ignore the semantic summary.
9. Render smoke test (`scripts/smoke_async_job.py`) still passes against the deployed instance with the current Render env.
10. Human-review language preserved verbatim (`사람 검토 필요`, `의미 매칭 근거 부족`, `공식 출처 확인 필요`).

Only when all ten clear should an operator consider a debug-only canary
(e.g. `max_news=1`, internal-facing endpoint, no UI surface).

## K. Validation

```
python tests/test_historical_claim_batch_builder.py
python scripts/build_historical_claim_batch.py --dry-run --max-cases 100
```

CI runs the first on every push with synthetic policy_analysis fixtures
under a tempdir. The builder never runs against the real `reports/` or
`policy_ai.db` in CI.
