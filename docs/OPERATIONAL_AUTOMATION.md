# Operational Automation Runner (Phase 2 M7.5)

A single CLI that bundles the standard post-change and canary-monitoring
checks so the operator stops typing the same five commands after every
milestone. The runner only orchestrates existing scripts
(`scripts/validate.py`, `scripts/smoke_async_job.py`,
`scripts/smoke_semantic_canary.py`,
`scripts/build_historical_claim_batch.py`,
`scripts/evaluate_real_claim_batch.py`); it makes no production
decisions and never modifies Render env.

## A. Purpose

- Reduce manual validation / smoke / canary repetition.
- Produce a consolidated JSON + Markdown report per run with parsed
  pass / warn / fail status per step.
- Keep the door open for future specialized AI agents (testing,
  deployment, monitoring, reviewer) to consume these reports — without
  building autonomous code-editing agents yet.

## B. What this does NOT do

- **Not autonomous coding.** It runs existing scripts; it does not edit
  source files.
- **Not multi-agent orchestration.** Single-process, single-CLI.
- **Not a Render env modifier.** Activating / rolling back the semantic
  canary still requires manual operator action in the Render dashboard.
- **Not Celery / Redis / pgvector / Qdrant.** Pure stdlib + existing
  scripts.
- **Not a verdict change.** `policy_decision`, `policy_scoring`, and
  `verification_card` are never imported. Pinned by
  `tests/test_operational_checks_runner.py::VerdictIsolationTests`.

## C. Profiles

| profile | what it runs | hits Render? | may trigger OpenAI? | typical use |
| --- | --- | --- | --- | --- |
| `quick` | `validate.py` | No | No | pre-commit local check |
| `post-commit` | `validate` + legacy Render smoke | Yes (smoke) | No (smoke script never calls OpenAI; Render server may if semantic is on) | after `git push` |
| `render-baseline` | legacy smoke + semantic canary (no `expect-enabled`) | Yes | Indirectly via Render if semantic on | inspect current Render semantic state |
| `render-canary` | semantic canary with `--expect-semantic-enabled --expect-provider openai --fail-on-semantic-unavailable` + legacy smoke | Yes | **Yes — Render will issue OpenAI requests** | monitor active semantic canary |
| `historical` | historical builder dry-run + deterministic eval (if file exists) | No | No | check builder output / regenerate batch evaluation |
| `review-local` (M8.3) | offline reviewer-workflow smoke — `scripts/smoke_review_workflow.py --self-contained` | No | No | exercise M8.0–M8.2 reviewer surface against a temp SQLite DB with a dummy in-process token |
| `full` | `validate` + `render-canary` + `historical` | Yes | Indirectly via Render | nightly / weekly comprehensive check |

## D. Common usage

Before commit (offline, fast):

```
python scripts/run_operational_checks.py --profile quick
```

After push / Render redeploy (legacy smoke):

```
python scripts/run_operational_checks.py --profile post-commit \
  --base-url https://policy-ai-q5ax.onrender.com
```

Inspect current Render semantic state (baseline):

```
python scripts/run_operational_checks.py --profile render-baseline \
  --base-url https://policy-ai-q5ax.onrender.com
```

Monitor active Render semantic canary (primary + secondary query):

```
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query
```

Historical dry-run + deterministic eval (no network):

```
python scripts/run_operational_checks.py --profile historical
```

Reviewer-workflow smoke (offline, no Render, no OpenAI, dummy
in-process token only):

```
python scripts/run_operational_checks.py --profile review-local
```

Pass/warn/fail interpretation for `review-local`:

| smoke result | runner status | meaning |
| --- | --- | --- |
| every sub-check `passed=true`, exit 0 | `pass` | M8.0–M8.2 review surface intact: disabled-by-default, token gate, from-result, idempotency, list/detail, every allowed decision, verdict isolation, and the absent publication path are all working. |
| any sub-check `passed=false`, exit 1 | `fail` | At least one reviewer-workflow contract regressed. The runner summary names the failing sub-check; inspect the smoke's JSON tail in the report. Do **not** roll forward until the failing sub-check is restored. |
| CLI misuse (e.g. `--self-contained` missing), exit 2 | `fail` | Treat as a hard fail; the smoke did not run any contract check. |

`review-local` is fully local/offline: it does **not** call OpenAI, does
**not** call Render, does **not** require `REVIEW_API_TOKEN` from the
operator, and does **not** modify Render env / `render.yaml`.

Full check (validate + canary + historical):

```
python scripts/run_operational_checks.py --profile full \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query
```

Dry-run (print commands, write a dry-run report, execute nothing):

```
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query --dry-run
```

Useful flags:

| flag | default | purpose |
| --- | --- | --- |
| `--base-url` | `https://policy-ai-q5ax.onrender.com` | Local or Render endpoint |
| `--query` | `전세사기` | Primary query |
| `--secondary-query` | `청년 월세` | Used with `--include-secondary-query` |
| `--max-news` | `1` | Smoke parameter |
| `--timeout-seconds` | `300` | Per-job timeout for smoke calls |
| `--poll-interval` | `2` | Smoke poll interval |
| `--skip-validate` / `--skip-render` / `--skip-semantic-canary` / `--skip-historical` | off | Drop steps without changing profile |
| `--include-secondary-query` | off | Run canary smoke twice per profile |
| `--json-out` / `--markdown-out` | auto-timestamped under `reports/` | Override default report paths |
| `--no-default-reports` | off | Suppress auto reports; explicit `--*-out` still honored |
| `--fail-on-warn` | off | Exit code 2 when any step returns `warn` |
| `--dry-run` | off | Print commands + write dry-run report; execute nothing |
| `--no-openai-note` | off | Suppress the render-canary OpenAI note |

Exit codes:

| code | meaning |
| --- | --- |
| 0 | all steps passed (or warn-only when `--fail-on-warn` not set) |
| 1 | at least one step failed (run stopped at first fail) |
| 2 | at least one step warned and `--fail-on-warn` was set |
| 130 | operator interrupt (Ctrl-C) |

## E. Safety

- **May hit Render** for profiles that include smoke / canary. The
  `--base-url` flag controls the target — point it at a local
  uvicorn for offline testing if Render shouldn't be touched.
- **`render-canary` may indirectly trigger OpenAI** if Render's
  `SEMANTIC_MATCHING_ENABLED` is currently `true`. The script never
  calls OpenAI itself; the Render service does, server-side. A note
  prints before the run unless `--no-openai-note` is set.
- **Never prints the API key.** The runner never reads
  `OPENAI_API_KEY` from the environment and never writes it to disk.
- **Never modifies Render env.** Activation / rollback stays with the
  operator in the Render dashboard.
- **Generated reports are gitignored.** `reports/operational_check_*.json`
  and `.md` live under `reports/` which is in `.gitignore` at line 5.
  Do not commit them. Reports older than a few days can be deleted
  freely.

## F. Interpretation

**`pass`** when every step passes. Semantic canary steps report
`provider_errors=0`, `overstrong_like=0`, semantic available where
expected, and the legacy async smoke also passes.

**`warn`** when one or more steps return `warn`. Common causes:

- Cold-start runtime on Render's first OpenAI call pushes
  `runtime_p95` above the 1500 ms threshold.
- Small-sample math drives `cap_ratio` above 0.70 on n=1 to n=3 claim
  payloads (the historical 100-case run sat at 0.0).
- Low cache hit rate on a fresh embedding cache.

These are usually self-correcting after a few canary runs as caches
warm. If the warn pattern persists across multiple runs, that's a real
operational signal worth investigating.

**`fail`** / consider rollback when:

- Any step exits non-zero.
- Semantic was expected enabled but unavailable
  (`smoke_semantic_canary` exit 2).
- Provider errors appear (server-side OpenAI failures).
- `overstrong_like_count > 0` (the M6.5-style failure mode — a
  critical mismatch was detected but the support label remained
  strong).
- The job timed out.
- The result endpoint returned an unusable payload.
- The legacy `smoke_async_job.py` regressed against the same
  semantic-enabled server.

Rollback path (operator action in Render dashboard):

```
SEMANTIC_MATCHING_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

Then re-run `--profile post-commit` to confirm the legacy verdict path
is unchanged.

## G. Relationship to future AI agents automation

This is the first automation layer. It produces structured JSON
reports that future specialized agents can consume:

- **testing agent** — interpret `validate` step output, suggest fixes
- **deployment agent** — read `post-commit` reports, gate on `pass`
- **monitoring agent** — chart `render-canary` runtime_p95 / cap_ratio
  over time
- **reviewer agent** — flag `unknown_historical` cases in the
  historical batch that need labeling

The runner deliberately stops at orchestration. **Autonomous
code-editing agents are not in scope** for this milestone — the policy
domain still requires human review for substantive changes, and the
current `claude-code` interactive flow is the right granularity.

## H. Validation

```
python tests/test_operational_checks_runner.py
python scripts/run_operational_checks.py --help
python scripts/run_operational_checks.py --profile quick --no-default-reports
python scripts/run_operational_checks.py --profile render-canary \
  --base-url https://policy-ai-q5ax.onrender.com \
  --include-secondary-query --dry-run --no-default-reports
```

CI runs the first one on every push with a fake subprocess runner — no
real Render call, no real OpenAI call. Live `render-canary` runs are
intentionally not part of CI.
