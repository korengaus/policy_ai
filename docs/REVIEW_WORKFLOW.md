# Server-Backed Reviewer Workflow (Phase 2 M8.0 + M8.1)

A backend-first foundation for the human-review layer. AI drafts and
summarizes evidence; humans approve, reject, or request more evidence.
**No publication path is enabled in M8.0 or M8.1** — `published` and
`corrected` are reserved status names that no transition reaches.

M8.0 introduced the storage tables, API surface, and safety gate.
M8.1 wires those endpoints to a local/dev admin UI panel inside
`web/index.html`. Verdict logic and the existing public-facing report
are intentionally untouched.

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

| layer | status |
| --- | --- |
| Storage (SQLite) | ✓ `review_tasks` + `review_decisions` tables created idempotently by `init_db()` (M8.0) |
| API endpoints | ✓ Five endpoints, all gated by `review_auth.require_review_token` (M8.0) |
| Safety gate | ✓ `REVIEW_API_ENABLED` + `REVIEW_API_TOKEN` + `X-Review-Token` header (M8.0) |
| Frontend wiring | ✓ Local/dev admin panel in `web/index.html` (M8.1, token-gated) |
| Publication | **not implemented**; transitions into `published` / `corrected` are refused |
| Verdict mutation | **disabled by contract**, pinned by `tests/test_review_api.py::VerdictIsolationTests` |
| Postgres dual-write | **deferred** — SQLite remains source of truth |

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

## H. Local/dev reviewer UI (M8.1)

`web/index.html` now exposes a token-gated admin panel titled
**"서버 검수 큐 (관리자 전용)"** that calls the M8.0 endpoints. Open
the page in a browser, expand the panel, paste the local reviewer token,
and the panel hydrates from `/review/tasks`.

What the UI lets a reviewer do:

- **Apply / clear a session token** — the token is held only in
  `sessionStorage` (per-browser, per-tab, cleared on close). The page
  never writes the token to `localStorage`, never logs it, and never
  echoes it back to the screen after "토큰 적용" is pressed (the input
  is cleared immediately).
- **List review tasks** — newest first, with a status dropdown filter
  (`전체` / `pending_review` / `needs_more_evidence` / `approved` /
  `rejected`). Filter values match the backend vocabulary exactly.
- **View task detail** — claim text, draft verdict, policy-confidence
  label, original URL, timestamps, plus the prior decision history.
  The original analysis payload is **not** mutated; the UI only reads.
- **Record decisions** — `approve`, `reject`, `needs_more_evidence`, or
  `comment`. Optional reviewer ID, comment, and public note fields are
  POSTed as-is to `/review/tasks/{id}/decision`. Disallowed transitions
  (e.g. re-approving an already-approved task) surface the backend's
  409 message; the UI does not bypass the transition matrix.
- **Refresh the queue** after a decision so the row's status chip
  reflects the new state.

Token-header behavior:

- Every request sets exactly one auth header:
  `X-Review-Token: <session-token>`. No other auth header is sent. The
  value is never embedded in URLs, query strings, request bodies, or
  log statements.
- When no token is applied, no request is fired — the UI shows a
  benign "토큰을 적용하면 서버 검수 큐를 불러옵니다." prompt.
- When the backend returns 503 (`REVIEW_API_ENABLED` not set, or
  `REVIEW_API_TOKEN` missing) the UI shows exactly:
  > **리뷰 API가 비활성화되어 있습니다. 로컬/운영 환경에서
  > `REVIEW_API_ENABLED` 설정이 필요합니다.**
- When the backend returns 403 (missing/wrong token) the UI shows a
  generic message — no token detail, no value comparison, no hint
  about server-side configuration. The reviewer simply re-pastes the
  correct token.

This panel is **local/dev admin workflow only**:

- It does not publish anything (no publish endpoint exists; the
  transition matrix refuses `published` / `corrected`).
- It does not mutate `final_decision`, `policy_confidence`,
  `verification_card`, or any verdict field on the original
  `analysis_results` row.
- It does not weaken the "사람 검토 필요" or "의미 매칭 근거 부족"
  wording anywhere on the public page.
- It does not change the existing localStorage-based "검수자용 리뷰 큐"
  (the older reviewer notes), which remains untouched.
- `REVIEW_API_ENABLED` is **not** flipped automatically. Render env is
  not modified by M8.1.

Running it locally:

```powershell
$env:REVIEW_API_ENABLED = "true"
$env:REVIEW_API_TOKEN   = "<your-local-dev-token>"
python -m uvicorn api_server:app --reload --port 8000
```

Open `http://127.0.0.1:8000/`, expand "검수자 도구 보기" then
"서버 검수 큐 (관리자 전용)", paste the same token, press "토큰 적용".
The token must never appear in source control, logs, or chat history.

## I. Future work (post-M8.1)

- Proper auth + admin layer to replace the temporary token gate.
- Postgres dual-write for review tables to match the existing pattern
  for `analysis_results`.
- Publication path with explicit reviewer privilege check + correction
  workflow (`published` / `corrected` become reachable).
- Server-side hook that auto-creates a review task whenever a pipeline
  result is flagged `human_review_required=true`.

## J. Validation

```
python tests/test_review_workflow.py
python tests/test_review_api.py
node tests/review_ui.test.js
python scripts/validate.py
```

CI runs the full set on every push. The review API tests use FastAPI
TestClient + a temporary SQLite DB; the JS reviewer-UI test runs in a
`vm` sandbox with no network. No live server, no OpenAI key, no Render
call.

To exercise the API + UI locally, set the env vars as in section H and
run uvicorn; then from another shell, with the same token, you can
also call the endpoints directly:

```powershell
curl -H "X-Review-Token: <your-local-dev-token>" http://127.0.0.1:8000/review/tasks
```

The token must never appear in source control, logs, or chat history.
