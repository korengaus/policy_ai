# Job Queue Infrastructure — M15.0a

**Status:** Phase 2 M15.0a SHIPPED — **infrastructure only**, no behaviour change. The new RQ-based queue sits alongside the existing process-local `job_manager` lifecycle table. M15.0b through M15.0e will progressively wire the existing `/analyze` and `/jobs/*` endpoints onto this queue.

## Why this exists

`claude_audit_phase1.md §1.6` identified three async-orchestration gaps:

1. **Synchronous OpenAI call inside a sync request** — 5–20s in the request path.
2. **Sequential per-news-item pipeline** — no parallelism across news items.
3. **No real-time / async pipeline** — every `/analyze` request runs the full chain synchronously (~174s baseline).

M15.0a lays the foundation. M15.0b will add the SSE progress endpoint; M15.0c the frontend; M15.0d the parallel news collection; M15.0e the Playwright pool.

## What ships in M15.0a

| Artifact | Purpose |
| --- | --- |
| `job_queue.py` | RQ wrapper with graceful degradation. Public API: `get_redis_connection()`, `get_queue(name)`, `enqueue_job(func, *args)`, `get_job_status(job_id)`, `get_queue_health()`. All functions return `None` or a documented sentinel dict when Redis is unavailable — never raise. |
| `worker.py` | Standalone RQ worker entry point. Opt-in: no Render config references it. Operator decides whether to provision a Background Worker. Exits 1 if `REDIS_URL` is unset / unreachable, exits 2 if `rq`/`redis` packages are missing. |
| `GET /health/queue` (in `api_server.py`) | Returns `{redis_connected, queue_depth, workers_count, queue_name, redis_url_set}`. Always 200, even when Redis is unset. The existing `/health` (liveness probe) is byte-identical to pre-M15.0a. |
| `tests/test_job_queue.py` | 23 tests. Fully offline using `fakeredis.FakeServer` shared across calls. Covers graceful-degradation contracts, public-API shapes, the `/health/queue` endpoint, and structural no-LLM-imports invariants. |
| `scripts/check_job_queue.py` | Read-only diagnostic CLI. Probes Redis via `PING` + queue-depth + worker-count. Exit codes: 0 = informational (Redis reachable OR degraded), 1 = `REDIS_URL` set but unreachable, 2 = CLI usage error. |
| `scripts/run_operational_checks.py --profile job-queue` | Runs the diagnostic CLI + the test suite. Fully offline. |
| `requirements.txt` | Adds `rq>=2.0.0`, `redis>=5.0.0`, `fakeredis>=2.20.0` (test-time). |

## What M15.0a does NOT touch

- `/analyze` endpoint behaviour — still synchronous (~174s).
- `main.analyze_pipeline` — unchanged.
- The existing `/jobs/analyze`, `/jobs/{job_id}`, `/jobs/{job_id}/result` endpoints — still backed by the process-local `job_manager` (`asyncio.to_thread`), exactly as documented at `api_server.py:395-400` (the pre-existing M2/M3 comment that explicitly said "Redis/Celery later").
- Any verdict-producing code (`policy_decision`, `policy_scoring`, `verification_card`, `policy_confidence`, `policy_impact`).
- Any M11.0d artifact.
- `tests/regression.test.js`, `render.yaml`, `frontend/`, `web/index.html`.

## Graceful degradation contract

The application MUST NOT crash when `REDIS_URL` is unset or Redis is unreachable. Pinned by:

- `tests/test_job_queue.py::GetRedisConnectionTests::test_returns_none_when_url_unset`
- `tests/test_job_queue.py::GetRedisConnectionTests::test_returns_none_when_url_blank`
- `tests/test_job_queue.py::GetRedisConnectionTests::test_returns_none_on_connection_failure`
- `tests/test_job_queue.py::GetQueueTests::test_returns_none_when_redis_unavailable`
- `tests/test_job_queue.py::EnqueueJobTests::test_returns_none_when_redis_unavailable`
- `tests/test_job_queue.py::GetJobStatusTests::test_returns_unavailable_when_redis_unset`
- `tests/test_job_queue.py::GetQueueHealthTests::test_health_when_redis_unset`
- `tests/test_job_queue.py::HealthQueueEndpointTests::test_health_queue_returns_degraded_when_redis_unset`

When `REDIS_URL` is unset:

- `get_redis_connection()` → `None`
- `get_queue(...)` → `None`
- `enqueue_job(...)` → `None` (logged at WARNING)
- `get_job_status(...)` → `{"status": "unavailable", "error": "redis_unavailable", ...}`
- `get_queue_health()` → `{"redis_connected": False, "queue_depth": 0, "workers_count": 0, ...}`
- `GET /health/queue` → 200 with the degraded payload.

## Worker provisioning (opt-in)

To run the RQ worker locally:

```powershell
$env:REDIS_URL = "redis://localhost:6379/0"
python worker.py
```

To provision a Render Background Worker (operator decision; nothing in this repo auto-creates it):

- Service type: **Background Worker**.
- Start command: `python worker.py`.
- Environment: `REDIS_URL` (and any other env vars the future jobs need — TBD in M15.0b).
- Region: same as the web service and the Key Value service.

## Render verification after deploy

After pushing M15.0a, run these two profiles from a local checkout:

```powershell
python scripts/run_operational_checks.py --profile render-baseline --base-url https://policy-ai-q5ax.onrender.com
python scripts/run_operational_checks.py --profile job-queue --base-url https://policy-ai-q5ax.onrender.com
```

The `render-baseline` profile confirms the existing endpoints still respond (no regression). The `job-queue` profile is fully offline — it runs the same checks that ran during CI but against the local checkout, so it does not depend on Render env vars.

To verify Render's `REDIS_URL` is wired correctly, hit the new endpoint directly:

```
curl https://policy-ai-q5ax.onrender.com/health/queue
```

Expected response shape:

```json
{
  "redis_connected": true,
  "queue_depth": 0,
  "workers_count": 0,
  "queue_name": "default",
  "redis_url_set": true
}
```

If `redis_connected: false` or `redis_url_set: false`, check the Render web-service env var dashboard.

## Roadmap — what comes next

- **M15.0b** — SSE progress endpoint. `/jobs/analyze` enqueues the pipeline as an RQ job; a new SSE endpoint streams per-stage progress to the browser.
- **M15.0c** — Frontend wires the SSE stream to a per-stage progress bar.
- **M15.0d** — Parallel news collection inside the worker (current pipeline runs news items sequentially; convert the loop to `asyncio.gather` or RQ sub-jobs).
- **M15.0e** — Playwright browser pool (currently every per-news rendered fetch launches its own browser).

Each milestone preserves the M11.0d-3b contract: P2 remains authoritative; the `disagreement_signal` continues to be emitted on every analysis.

## Architecture sketch

```
Browser POST /jobs/analyze
        │
        ▼
api_server.jobs_analyze ─── enqueue_job(_run_pipeline_for_job, query, max_news)
        │                       │
        ▼                       ▼
   returns job_id           RQ Queue ("default")
                                │
                                ▼
                          worker.py (separate process)
                                │
                                ▼
                          main.analyze_pipeline
                                │
                                ▼
                          M11.0d-3a disagreement_signal logged
                          M11.0d-3b P2 authoritative alert
                                │
                                ▼
                          job.return_value stored in Redis
                                │
                                ▼
Browser GET /jobs/{job_id}/result
```

The diagram above is the **target** after M15.0b/c/d/e. M15.0a only ships the queue + worker + health endpoint.
