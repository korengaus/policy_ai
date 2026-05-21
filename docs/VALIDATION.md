# Validation

How to verify changes locally and what the GitHub Actions workflow does. This
layer is **only** for regression validation and smoke testing — it does not
change verification logic, methodology wording, or pipeline behavior.

## A. Local validation

Run the full offline suite (compile + Python tests + JS regression + slim-record
tests) from a single entry point:

```
python scripts/validate.py
```

Mirrors the CI workflow. Stops on the first failing command and exits with
that command's return code. Works in PowerShell and POSIX shells.

Equivalent manual sequence:

```
python -m compileall api_server.py database.py job_manager.py
python tests/test_jobs.py
python tests/test_postgres_dual_write.py
python tests/test_ai_reasoner_status.py
python tests/test_semantic_matching.py
python tests/test_semantic_activation.py
python tests/test_semantic_calibration.py
python tests/test_semantic_fact_guardrails.py
python tests/test_semantic_provider_comparison.py
python tests/test_semantic_real_claim_batch.py
python tests/test_historical_claim_batch_builder.py
python tests/test_semantic_canary_metrics.py
python tests/test_smoke_semantic_canary.py
python tests/test_operational_checks_runner.py    # includes M8.4 canary classification tests
python tests/test_review_workflow.py
python tests/test_review_api.py
python tests/test_review_workflow_smoke.py
python tests/test_operator_preflight.py   # M8.5: operator preflight helper
python tests/test_review_bundle.py        # M8.6: post-implementation review bundle helper
npm test   # runs regression.test.js + localstorage_slim.test.js + review_ui.test.js (M8.1 + M8.2 + M8.7)
```

**Shortcut**: instead of running each test individually, use the
operational runner's `quick` profile (M7.5):

```
python scripts/run_operational_checks.py --profile quick
```

For a focused reviewer-workflow smoke (M8.3, offline, no Render, no
OpenAI, dummy in-process token only):

```
python scripts/smoke_review_workflow.py --self-contained
python scripts/run_operational_checks.py --profile review-local
```

For the M8.7 reviewer/admin UI safety hardening (offline, no network,
no fetch — runs in a `vm` sandbox):

```
node tests/review_ui.test.js
```

This pins the admin-only wording (`관리자 전용`, `내부 검수`,
`사람 검토 필요`, `검수 큐 등록`, `게시가 아님`), the no-`/review/*`
auto-fetch on page initialization (even with a token already in
`sessionStorage`), the token-clear lockout message, the absence of
`published` / `corrected` UI labels, and the absence of any
`/publish` / `/correct` endpoint reference. `npm test` already
invokes this file, so `scripts/validate.py` covers it.

Before staging changes, the M8.5 preflight helper recommends a precise
`git add` command (never stages anything itself). It is exercised in
`scripts/validate.py` via `tests/test_operator_preflight.py`:

```
python scripts/operator_preflight.py
python scripts/operator_preflight.py --expected docs/REVIEW_WORKFLOW.md scripts/operator_preflight.py
python scripts/operator_preflight.py --expected ... --chatgpt-summary
python scripts/operator_preflight.py --expected ... --json
```

See `docs/OPERATIONAL_AUTOMATION.md` §F'' for the always-excluded
patterns and the rationale.

After Claude finishes implementing a milestone and before manually
running `git add`/`git commit`, the M8.6 review bundle helper packages
the change set into a ChatGPT-friendly summary (never stages anything
itself). It writes by default to `reports/review_bundle_<ts>.txt`,
which is gitignored and must **not** be committed:

```
python scripts/build_review_bundle.py --expected web/index.html docs/REVIEW_WORKFLOW.md
python scripts/build_review_bundle.py --expected ... --milestone "Phase 2 M8.6"
python scripts/build_review_bundle.py --expected ... --chatgpt-summary
python scripts/build_review_bundle.py --expected ... --include-diff
python scripts/build_review_bundle.py --expected ... --stdout
python scripts/build_review_bundle.py --expected ... --json
```

See `docs/OPERATIONAL_AUTOMATION.md` §F''' for the full flag list,
diff-handling rules, and the always-excluded patterns. The
helper is exercised in `scripts/validate.py` via
`tests/test_review_bundle.py`.

It calls `scripts/validate.py` and writes a consolidated report under
`reports/operational_check_<timestamp>.{json,md}` (gitignored). See
`docs/OPERATIONAL_AUTOMATION.md` for profiles and CI guidance.

No external services required — `USE_POSTGRES_WRITE=false` keeps the dual-write
path mocked, the semantic tests use the deterministic embedding provider
(no network, no OpenAI key), and the JS tests run in an isolated `vm` sandbox
with no network. See `docs/SEMANTIC_MATCHING.md` for the M5 semantic flow,
`docs/SEMANTIC_ACTIVATION.md` for the M5.5 probe tooling, and
`docs/SEMANTIC_CALIBRATION.md` for the M5.6 evaluator.

Optional manual probe / evaluator runs (not in default CI):

```
python scripts/probe_semantic_matching.py --provider deterministic --show-matches --max-cases 3
python scripts/probe_semantic_matching.py --provider openai --no-network --fail-on-unavailable
python scripts/evaluate_semantic_calibration.py --provider deterministic --show-failures
python scripts/evaluate_semantic_calibration.py --provider openai --no-network --fail-on-unavailable
python scripts/compare_semantic_providers.py --providers deterministic,disabled --no-network --show-failures
python scripts/compare_semantic_providers.py --providers openai --no-network
python scripts/evaluate_real_claim_batch.py --provider deterministic --no-network --show-failures
python scripts/evaluate_real_claim_batch.py --provider openai --no-network --fail-on-unavailable
```

See `docs/SEMANTIC_PROVIDER_COMPARISON.md` for the M5.8 driver and
`docs/SEMANTIC_REAL_CLAIM_BATCH.md` for the M6.2 real-claim evaluator.
Live OpenAI runs against either fixture require `--live-confirm-token
LIVE_OPENAI_OK` plus a fully configured env and are intentionally **not**
part of CI.

## B. Manual local smoke test

Exercise the async-job flow end-to-end against a local uvicorn server.

Terminal 1 — start the API:

```
python -m uvicorn api_server:app --reload --port 8000
```

Terminal 2 — run the smoke test:

```
python scripts/smoke_async_job.py --base-url http://127.0.0.1:8000 --query 전세사기 --max-news 1
```

The script:
1. `GET /health`
2. `POST /jobs/analyze`
3. Polls `GET /jobs/{job_id}` until the job reaches `completed | failed | timeout`
4. On `completed`, fetches `GET /jobs/{job_id}/result` and asserts a usable payload

Exit code is `0` on success, non-zero on any failure (HTTP error, polling
timeout, completed-but-unavailable, etc.). Uses only the Python stdlib —
no new dependencies.

CLI flags:

| flag | default |
| --- | --- |
| `--base-url` | `http://127.0.0.1:8000` |
| `--query` | `전세사기` |
| `--max-news` | `1` |
| `--timeout-seconds` | `300` |
| `--poll-interval` | `2` |

## C. Render smoke test

Same script, pointed at the deployed instance:

```
python scripts/smoke_async_job.py --base-url https://YOUR-RENDER-APP.onrender.com --query 전세사기 --max-news 1
```

This exercises the real pipeline on Render (live news fetch, external sites).
Use sparingly and outside business hours if you're worried about API quotas
on upstream news providers.

## D. GitHub Actions

`.github/workflows/ci.yml`:

- **Push / pull_request** → runs compile + the four test suites listed above.
  No external network calls; no live crawling.
- **workflow_dispatch** → same offline suite plus an optional smoke step.
  When triggered manually, you can supply the `smoke_base_url` input; the
  workflow will run `scripts/smoke_async_job.py` against that URL once the
  offline suite passes. Leave `smoke_base_url` empty to skip the smoke step.

Environment used by the workflow:

```
CI=true
PYTHONUTF8=1
USE_POSTGRES_WRITE=false
```

No `OPENAI_API_KEY` or `DATABASE_URL` is needed — Python tests mock those
dependencies. If you want the smoke step to hit a Render deployment that
requires authentication, add the credentials as repository secrets and extend
the workflow at that point (not done here to keep M4 minimal).

## E. Known limitations

- **Smoke test runs the real pipeline.** It depends on the target server's
  network reachability to upstream news sources. A failure does not always
  mean the deployment is broken — transient upstream failures show up here.
- **Normal CI avoids live crawling by default.** The push/PR run only
  exercises the compile + offline test surface.
- **Jobs are process-local.** `/jobs/analyze` schedules work on the same
  uvicorn worker that received the request. A smoke test that hits a
  multi-worker deployment may not see the same worker on follow-up polls
  until Phase 3 introduces Redis/Celery.
- **Favicon 404 is not a validation blocker.** Browsers request
  `/favicon.ico`; the API does not serve one. Ignore that line in server logs.
- **`.claude/settings.local.json` must not be committed.** It is now in
  `.gitignore` for new clones, but the file is already tracked in this repo;
  exclude it from commits explicitly when staging changes.
- **`reports/review_bundle_*.txt` must not be committed.** They live under
  the gitignored `reports/` directory and contain transient review artifacts
  for ChatGPT/operator inspection only.
