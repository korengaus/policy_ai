# Server-Backed Reviewer Workflow (Phase 2 M8.0)

A backend-first foundation for the human-review layer. AI drafts and
summarizes evidence; humans approve, reject, or request more evidence.
**No publication path is enabled in M8.0** — `published` and `corrected`
are reserved status names that no transition reaches.

## A. Purpose

- Persist review tasks and reviewer decisions in SQLite alongside the
  existing `analysis_results` table.
- Expose a small, **token-protected** API surface so a reviewer client
  (frontend wiring is M8.1) can list tasks, view detail, create tasks
  from an analysis result, and record decisions.
- Keep verdict-side fields (`final_decision`, `policy_confidence`,
  `verification_card`) read-only from the review layer. The review
  endpoints never mutate analysis results.
- Stay disabled by default so a public Render deploy cannot
  accidentally expose reviewer endpoints.

## B. Current status

| layer | M8.0 status |
| --- | --- |
| Storage (SQLite) | ✓ `review_tasks` + `review_decisions` tables created idempotently by `init_db()` |
| API endpoints | ✓ Five endpoints, all gated by `review_auth.require_review_token` |
| Safety gate | ✓ `REVIEW_API_ENABLED` + `REVIEW_API_TOKEN` + `X-Review-Token` header |
| Frontend wiring | **deferred to M8.1** |
| Publication | **not implemented**; transitions into `published` / `corrected` are refused |
| Verdict mutation | **disabled by contract**, pinned by `tests/test_review_api.py::VerdictIsolationTests` |
| Postgres dual-write | **deferred** — SQLite remains source of truth for M8.0 |

## C. Safety gate

The review endpoints are off by default. Two env vars + a header are
required:

| variable | purpose | default |
| --- | --- | --- |
| `REVIEW_API_ENABLED` | Master kill-switch | unset (= disabled, HTTP 503) |
| `REVIEW_API_TOKEN` | Shared secret the reviewer client must present | unset (= 503 when enabled) |
| `X-Review-Token` header | Sent on every request, must match the token | absent (= 403) |

Behavior table:

| `REVIEW_API_ENABLED` | `REVIEW_API_TOKEN` | header | result |
| --- | --- | --- | --- |
| unset / false | – | – | **503** disabled |
| true | unset | any | **503** misconfigured |
| true | set | missing | **403** |
| true | set | wrong | **403** |
| true | set | matches | request proceeds |

Important:

- The token value is **never logged or printed** by `review_auth.py`.
- Render keeps `REVIEW_API_ENABLED` unset by default. Activation is a
  manual operator action via the Render dashboard, same pattern as the
  semantic canary (M7.4).
- This is **not** a real auth system. It's a fence until proper auth +
  admin lands in a future milestone.

## D. Endpoints

All endpoints require the safety gate (`X-Review-Token` header).

### `GET /review/tasks`

Query parameters:

| param | default | description |
| --- | --- | --- |
| `status` | – | Filter by exact status (`pending_review` / `needs_more_evidence` / `approved` / `rejected`). Unknown status → 400. |
| `limit` | `50` | Page size, clamped to `[1, 100]`. |
| `offset` | `0` | Pagination offset. |

Returns `{"tasks": [...], "count": N, "status_filter": "..."}`.

### `GET /review/tasks/{task_id}`

Returns `{"task": {...}, "decisions": [...]}`. The task object includes
the stored snapshot so the reviewer UI can render the original claim,
sources, and verdict signals.

### `POST /review/tasks/from-result`

Body:

```json
{
  "result_id": "42",
  "job_id": "job-abc",
  "item_index": 0,
  "result_payload": { "result": { "results": [...] } },
  "query": "전세사기"
}
```

The server resolves the analysis payload via, in priority order:

1. `result_payload` (full body — easiest for the reviewer client)
2. `job_id` (in-process job cache, when the job is still warm)
3. `result_id` (stored history row)

Creates the task in `pending_review` with `human_review_required=true`.
**Idempotent** on `(result_id, job_id, item_index, claim_text)` — a
repeat POST returns the same `task_id` and `"idempotent": true`.

### `POST /review/tasks/{task_id}/decision`

Body:

```json
{
  "decision": "approve",
  "reviewer_id": "local_reviewer",
  "comment": "evidence verified against source body",
  "public_note": "(optional reviewer-facing note)"
}
```

Decisions:

| decision | from | new status |
| --- | --- | --- |
| `approve` | `pending_review` / `needs_more_evidence` | `approved` |
| `reject` | `pending_review` / `needs_more_evidence` | `rejected` |
| `needs_more_evidence` | `pending_review` / `needs_more_evidence` | `needs_more_evidence` |
| `comment` | any status | (unchanged) |

Disallowed transitions return **409 Conflict** (e.g. re-approving an
already-approved task). Comment-only decisions on approved / rejected
tasks are accepted and recorded for audit.

### `GET /review/tasks/{task_id}/decisions`

Returns `{"task_id": "...", "decisions": [...], "count": N}` —
decisions in append order, oldest first. Decisions are append-only;
there's no delete / update endpoint.

## E. Statuses

| status | description |
| --- | --- |
| `pending_review` | Default for newly-created tasks. Awaiting human review. |
| `needs_more_evidence` | Reviewer requested additional evidence before deciding. |
| `approved` | Reviewer approved the AI-drafted finding. No publication path. |
| `rejected` | Reviewer rejected the AI-drafted finding. |
| `published` | **Reserved** — not reachable in M8.0. |
| `corrected` | **Reserved** — not reachable in M8.0. |

## F. Decisions

`approve`, `reject`, `needs_more_evidence`, `comment`. See the
transition table above. The full vocabulary is pinned by
`tests/test_review_workflow.py::TransitionMatrixTests`.

## G. What this does NOT do

- **Does not publish anything.** No `published` transition. No publish
  endpoint. The transition matrix refuses any move into `published` /
  `corrected`.
- **Does not change verdict logic.** `policy_decision`,
  `policy_scoring`, and `verification_card` are not imported by
  `review_workflow.py` / `review_auth.py` / the new endpoints — pinned
  by `tests/test_review_api.py::VerdictIsolationTests` and
  `tests/test_review_workflow.py::IsolationTests`.
- **Does not change confidence labels or wording.**
- **Does not expose a UI yet.** Frontend wiring is M8.1.
- **Does not replace real auth.** The token gate is a temporary fence.
- **Does not modify Render env.** Operator must enable manually via
  Render dashboard.
- **Does not touch `analysis_results`.** All writes go to the new
  `review_tasks` / `review_decisions` tables.

## H. Future work (M8.1+)

- Wire the existing reviewer dashboard UI to the new endpoints
  (M8.1 candidate). Local reviewer-token setup, list / detail screens,
  approve / reject / needs-more-evidence buttons, decision history.
- Proper auth + admin layer to replace the temporary token gate.
- Postgres dual-write for review tables to match the existing pattern
  for `analysis_results`.
- Publication path with explicit reviewer privilege check + correction
  workflow (`published` / `corrected` become reachable).

## I. Validation

```
python tests/test_review_workflow.py
python tests/test_review_api.py
python scripts/validate.py
```

CI runs all three on every push. The review API tests use FastAPI
TestClient + a temporary SQLite DB; no live server, no OpenAI key, no
Render call.

To exercise the API locally:

```powershell
$env:REVIEW_API_ENABLED = "true"
$env:REVIEW_API_TOKEN   = "<your-local-dev-token>"
python -m uvicorn api_server:app --reload --port 8000
```

Then from another shell, with the same token, hit the endpoints:

```powershell
curl -H "X-Review-Token: <your-local-dev-token>" http://127.0.0.1:8000/review/tasks
```

The token must never appear in source control, logs, or chat history.
