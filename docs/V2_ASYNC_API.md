# V2 Async API — M15.0b

**Status:** Phase 2 M15.0b SHIPPED. New opt-in `/v2/*` endpoints sit alongside the existing `/analyze` and `/jobs/*` flows. M15.0c will rewire the frontend to prefer the V2 flow.

## Why M15.0b exists

The existing `POST /analyze` is **synchronous** with a ~48-174s latency window. Users wait the full pipeline duration with no progress feedback. M15.0b lets the browser get a `job_id` in <100ms and stream progress via Server-Sent Events while the pipeline runs in a separate worker process.

This is the highest-user-impact backend milestone of Phase 2.

## Endpoint contracts

### `POST /v2/analyze`

Enqueues an analysis job. Returns immediately — the actual pipeline runs in a worker.

**Request:**
```json
{ "query": "전세사기", "max_news": 1 }
```

**Response (success — 202 Accepted):**
```json
{
  "job_id": "f4ca3dd1-a83f-4f5f-8159-c5a405b97ed6",
  "status": "queued",
  "created_at": "2026-05-25T00:16:52+00:00",
  "queue_name": "default"
}
```

**Response (Redis unavailable — 503):**
```json
{ "detail": "redis_unavailable: /v2/analyze requires a reachable REDIS_URL. The existing /analyze endpoint remains available as a synchronous fallback." }
```

**Validation errors:**
- 400 — empty `query` or `max_news <= 0`
- 422 — request body fails Pydantic validation

### `GET /v2/jobs/{job_id}`

Returns the current status of an enqueued job.

**Response (200):**
```json
{
  "job_id": "f4ca3dd1-a83f-4f5f-8159-c5a405b97ed6",
  "status": "queued | started | finished | failed | stopped | deferred | scheduled | canceled",
  "result": null,
  "error": null,
  "enqueued_at": "2026-05-25T00:16:52+00:00",
  "started_at": null,
  "ended_at": null,
  "progress_percent": 0,
  "current_step": null
}
```

When `status == "finished"`, `result` contains the summary payload built by `pipeline_worker._build_summary_payload`:

```json
{
  "status": "ok",
  "query": "전세사기",
  "total_news_count": 1,
  "saved_event_count": 1,
  "duplicate_count": 0,
  "saved_result_ids": [42],
  "ai_status_summary": { "ai_status": "ok", "ai_model": "gpt-test", "ai_available": true },
  "news_collection_debug": { "news_cache_hit": false }
}
```

The full per-news result rows are persisted to SQLite via the existing `save_analysis_result` path. Use `GET /history/{result_id}` (existing endpoint) to fetch the full row by id.

**Error responses:**
- 404 — `job_id` not found in Redis (job expired or never enqueued)
- 503 — Redis unavailable

### `GET /v2/jobs/{job_id}/stream`

Server-Sent Events stream of job progress. Auto-closes on terminal status or 600s timeout.

**Event types:**

| Event name | When | Data shape |
| --- | --- | --- |
| `status` | Initial status + every status transition | Full status dict (same shape as `GET /v2/jobs/{job_id}` response) |
| `progress` | Each `pipeline_worker.report_progress` call on the worker | `{stage, percent, detail, at, job_id}` |
| `completed` | Final event when job ends successfully | Full status dict |
| `failed` | Final event when job fails | Full status dict |
| `timeout` | Final event when stream's 600s timeout elapses | `{job_id, max_seconds}` |
| `unavailable` | Final event when Redis is unset/unreachable | `{job_id, reason}` |
| `not_found` | Final event when job_id is not in Redis | `{job_id}` |

**Example wire format:**

```
event: status
data: {"job_id":"f4ca3dd1-...","status":"queued","progress_percent":0,...}

event: progress
data: {"stage":"pipeline_started","percent":10,"detail":"query=전세사기","at":"2026-05-25T...","job_id":"f4ca3dd1-..."}

event: progress
data: {"stage":"saving_results","percent":85,"detail":"persisting per-news results","at":"2026-05-25T...","job_id":"f4ca3dd1-..."}

event: completed
data: {"job_id":"f4ca3dd1-...","status":"finished","result":{...},...}
```

**Resilience:**

- The generator both subscribes to `job:{job_id}:progress` (Redis pub/sub) AND polls `get_job_status` every ~1s. Race conditions where the subscriber missed a published event are caught by the polling loop.
- If pub/sub subscription fails, the generator falls back to polling-only.
- If Redis is unavailable at all, the generator emits a single `unavailable` event and closes the stream.

## Architecture

```
Browser                api_server.py                    Redis              Worker (separate process)
   │
   ├─ POST /v2/analyze ───────►  v2_analyze
   │                              ├─ job_queue.enqueue(...)─────►  RQ "default" queue
   │                              └─ return {job_id} ◄──────────┐
   │                                                            │
   │ ◄── 202 + job_id ────────────────────────────────────────────┘
   │
   ├─ GET /v2/jobs/{id}/stream ──►  v2_job_stream
   │                                ├─ pubsub.subscribe(...)─────►   ◄──── publish progress ◄── worker.py
   │                                ├─ get_job_status(id)─────────►   ◄──── job.return_value ◄── pipeline_worker.run_analyze_pipeline_job
   │                                └─ yield SSE events
   │ ◄── SSE event: status ──────────┘
   │ ◄── SSE event: progress ────────┘                          (worker calls main.analyze_pipeline,
   │ ◄── SSE event: completed ───────┘                           saves results to SQLite, publishes
   │                                                             progress to job:{id}:progress)
```

**Worker process:** `python worker.py` (the entry point M15.0a shipped). The worker imports `pipeline_worker.run_analyze_pipeline_job` and `main.analyze_pipeline` in its own process. Without a worker provisioned, jobs queue successfully but never execute — `/v2/jobs/{id}` returns `status="queued"` forever (until RQ's default expiry).

## What M15.0b does NOT do

- Does NOT modify `POST /analyze` (still synchronous, ~174s baseline, all current clients unaffected)
- Does NOT modify `POST /jobs/analyze` or `GET /jobs/{job_id}*` (the pre-existing process-local `job_manager` system stays exactly as it was)
- Does NOT modify `main.analyze_pipeline` body (wrapped, not changed)
- Does NOT modify any verdict-producing code (`policy_decision`, `policy_scoring`, `verification_card`, `policy_confidence`, `policy_impact`)
- Does NOT modify any M11.0d artifact
- Does NOT modify the frontend (M15.0c does this)
- Does NOT parallelize news items within a single analysis (M15.0d)
- Does NOT add a Playwright pool (M15.0e)
- Does NOT auto-provision a Render Background Worker — operator decides separately (see `docs/JOB_QUEUE.md`)

## Graceful degradation

| Scenario | `/analyze` | `/jobs/analyze` | `/v2/analyze` | `/v2/jobs/{id}/stream` |
| --- | --- | --- | --- | --- |
| Normal | 200 (~174s) | 200 + asyncio task | 202 + job_id | SSE stream |
| Redis unset | 200 (~174s) | 200 + asyncio task | **503** | single `unavailable` event |
| Redis up, no worker | 200 (~174s) | 200 + asyncio task | 202 + job_id (stays "queued") | initial `status` event, then polls until 600s timeout |
| Worker crashes mid-job | 200 (~174s) | 200 + asyncio task | 202 + job_id (stays "started" then RQ marks "failed") | `failed` event when RQ marks the job dead |

## Render verification after deploy

```powershell
python scripts/run_operational_checks.py --profile render-baseline --base-url https://policy-ai-q5ax.onrender.com
python scripts/run_operational_checks.py --profile job-queue --base-url https://policy-ai-q5ax.onrender.com
```

Plus manual smoke tests:

```powershell
# Enqueue a job (returns immediately):
curl -X POST https://policy-ai-q5ax.onrender.com/v2/analyze `
     -H "Content-Type: application/json" `
     -d '{"query": "전세사기", "max_news": 1}'

# Poll status (no worker → status stays "queued"):
curl https://policy-ai-q5ax.onrender.com/v2/jobs/<job_id_from_above>

# Stream progress (no worker → polls until 600s, no terminal event):
curl -N https://policy-ai-q5ax.onrender.com/v2/jobs/<job_id_from_above>/stream
```

Without a Background Worker provisioned, the job will sit in "queued" state. This is **expected behaviour for M15.0b**; the worker provisioning is an explicit operator decision (see `docs/JOB_QUEUE.md` for the checklist).

## Roadmap

- **M15.0c** — Frontend wires the SSE stream to a per-stage progress bar. The polling-fallback already implemented in M15.0b lets the operator decide whether to provision the worker before or after the frontend update.
- **M15.0d** — Parallel news collection inside the worker (current pipeline runs news items sequentially).
- **M15.0e** — Playwright browser pool (currently every per-news rendered fetch launches its own browser).

Each milestone preserves the M11.0d-3b contract: P2 remains authoritative; `disagreement_signal` continues to be emitted on every analysis.

---

## Frontend integration — M15.0c

**Status:** SHIPPED in M15.0c. The vanilla-JS frontend now tries V2 first, falls back to V1-async, then to sync `/analyze`.

### V2 client location

The V2 client functions (`v2PostAnalyze`, `v2StreamProgress`, `v2InflateResults`, `requestPolicyAnalysisV2`, etc.) live as a delimited section INSIDE `frontend/scripts/main.js`, marked by:

```js
// =====================================================================
// M15.0c — V2 client (begin)
...
// M15.0c — V2 client (end)
// =====================================================================
```

**Why inline, not a separate `frontend/scripts/v2_client.js` file?** The M11.5 / M13.2a / M13.2b build pipeline (`frontend/build_index.py`) injects exactly ONE `<script>` block and one `<style>` block into the template; adding a second source file would change the build contract and force updates to `tests/test_frontend_build.py`'s `JS_PATH` pin. M15.0c kept the build pipeline untouched and used a delimited section instead. Re-promoting to a separate physical file is a small follow-up that would require extending `build_index.py` to accept a list of JS sources (concatenated in order) and updating the test fixture accordingly.

### Three-tier fallback chain

`requestPolicyAnalysis({query, maxNews}, onProgress)` in `main.js`:

1. **V2** (`requestPolicyAnalysisV2`) — POST `/v2/analyze` → EventSource on `/v2/jobs/{id}/stream`. Shows the progress UI. If EventSource is unavailable OR no events arrive in 10s, falls back to 2s polling of `/v2/jobs/{id}`.
2. **V1 async** (`requestPolicyAnalysisAsync`, the pre-existing pattern) — POST `/jobs/analyze` + poll `/jobs/{id}` / `/jobs/{id}/result`. Uses the process-local `job_manager` system. Fires when V2 returns 503 (Redis unavailable) or any network error.
3. **Sync** (`requestPolicyAnalysisLegacy`) — POST `/analyze` synchronously. Always works; blocks for 48-174s. Final safety net.

Each tier rejects to the next on any exception. The user sees error UI only if all three fail.

### V2 result inflation

`/v2/jobs/{job_id}`'s `result` field carries `saved_result_ids` (list of integers) instead of the full per-news result rows. The frontend uses the existing `mapHistoryRowToResult` adapter (already defined in `main.js:2538`) and the existing `GET /history/{id}` endpoint to inflate each id back to the AnalyzeResult shape that `renderResults()` expects. Pattern (in pseudocode):

```js
const summary = sseCompletedPayload.result;
const ids = summary.saved_result_ids || [];
const rows = await Promise.all(ids.map((id) => fetch(`/history/${id}`).then(r => r.json())));
const inflated = {
  status: "ok",
  results: rows.map(r => mapHistoryRowToResult(r.result)),
  news_collection_debug: summary.news_collection_debug || {},
  ai_status: summary.ai_status_summary || {},
};
renderAnalysisResponse(query, maxNews, stabilizeAnalysisResponseForRender(inflated, ...));
```

This means the frontend issues `N+1` requests on completion (1 status + N history fetches). For typical `max_news=1-3`, that's 2-4 fetches — acceptable. If this becomes a bottleneck, a future `/v2/jobs/{id}/result` endpoint could do the inflation server-side.

### Progress UI

The UI elements live in `frontend/template.html`:

```html
<div id="v2ProgressWrap" class="v2-progress-wrap" role="status" aria-live="polite" hidden>
  <div class="v2-progress-track" role="progressbar" ...>
    <div id="v2ProgressBar" class="v2-progress-bar" style="width:0%"></div>
  </div>
  <div id="v2ProgressText" class="v2-progress-text"></div>
</div>
```

CSS in `frontend/styles/main.css` uses existing design tokens (`--blue`, `--blue-dark`, `--border`, `--muted`, `--surface-soft`). Hidden by default via the `hidden` attribute; shown only when `requestPolicyAnalysisV2` is active. Always reset in the `analyze()` handler's `finally` block so it disappears on success, error, and fallback alike.

### Stage → Korean label table

`V2_STAGE_LABELS_KO` in `main.js` translates pipeline stages to Korean strings. The first 5 are emitted by `pipeline_worker.report_progress` today (M15.0b); the rest are aspirational for M15.0d/e and pre-translated so a future per-step event can render without a frontend code change.

| Stage | Korean |
| --- | --- |
| `queued` | 대기열에 등록됨 |
| `pipeline_started` | 검증 파이프라인 실행 중 |
| `saving_results` | 결과 저장 중 |
| `completed` | 완료 |
| `failed` | 실패 |
| `news_collection` (M15.0d) | 뉴스 수집 중 |
| `article_extraction` (M15.0d) | 기사 본문 추출 중 |
| `claim_extraction` (M15.0d) | 주장 추출 중 |
| `official_source_search` (M15.0d) | 공식 출처 검색 중 |
| `evidence_extraction` (M15.0d) | 증거 추출 중 |
| `verification_card` (M15.0d) | 검증 카드 구성 중 |
| `ai_reasoning` (M15.0d) | AI 추론 중 |
| `calibration` (M15.0d) | 최종 보정 중 |

Unknown stage names fall through to `"진행 중"`.

### Pins

- `tests/test_frontend_v2_client.test.js` (Node) — extracts the V2 client section from the built `web/index.html` and asserts:
  - Section delimiters present and unique
  - All 7 M15.0b SSE event types are wired (`progress` / `status` / `completed` / `failed` / `timeout` / `unavailable` / `not_found`)
  - The three-tier fallback chain is wired (V2 → V1-async → sync), in order
  - All M15.0b stage labels translated to Korean
  - `analyze()` handler resets the progress UI in `finally`
  - All regression-pinned methodology phrases survived
- `tests/test_v2_endpoints_e2e.py` (Python) — uses RQ's `SimpleWorker` in burst mode (in-process, no separate worker subprocess) to run a mocked pipeline through the full enqueue → execute → status-finished chain, and verifies the `/history/{id}` inflation path. The real `analyze_pipeline` is mocked; the worker really executes via RQ's actual machinery.
- `tests/test_frontend_build.py` (existing) — confirms `web/index.html` matches a fresh `frontend/build_index.py` build.
- `tests/regression.test.js` (existing, byte-identical) — confirms Korean methodology phrases unchanged and `buildReportText` output is consistent.
