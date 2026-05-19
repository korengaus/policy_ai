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
npm test
```

No external services required — `USE_POSTGRES_WRITE=false` keeps the dual-write
path mocked, and the JS tests run in an isolated `vm` sandbox with no network.

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
