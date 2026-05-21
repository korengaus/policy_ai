# Server-Backed Reviewer Workflow (Phase 2 M8.0 + M8.1 + M8.2 + M8.3 + M8.7)

A backend-first foundation for the human-review layer. AI drafts and
summarizes evidence; humans approve, reject, or request more evidence.
**No publication path is enabled in M8.0, M8.1, M8.2, or M8.3** —
`published` and `corrected` are reserved status names that no
transition reaches.

M8.0 introduced the storage tables, API surface, and safety gate.
M8.1 wires those endpoints to a local/dev admin UI panel inside
`web/index.html`. M8.2 adds an analysis-to-review queue bridge — a
small admin-only button that posts the currently displayed analysis
result to `POST /review/tasks/from-result` so operators don't have to
hand-craft the payload. M8.3 adds a self-contained operational smoke
script (`scripts/smoke_review_workflow.py`) and a `review-local`
profile in the operational runner so the entire M8.0–M8.2 surface can
be exercised offline against a temp SQLite DB. Verdict logic and the
existing public-facing report are intentionally untouched.

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
| Analysis → queue bridge | ✓ "검수 큐에 등록" button calls `POST /review/tasks/from-result` (M8.2, token-gated, idempotent) |
| Offline operational smoke | ✓ `scripts/smoke_review_workflow.py --self-contained` + `--profile review-local` runner (M8.3, fully offline, dummy in-process token) |
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

## H'. Analysis-to-review queue bridge (M8.2)

The reviewer/admin panel exposes a single admin-only action that
registers the currently displayed analysis result into the server
review queue without having to compose the JSON body manually.

### Where it lives

Inside the existing "서버 검수 큐 (관리자 전용)" panel, between the
token panel and the queue list:

> **분석 결과 → 검수 큐 등록**
>
> 현재 화면에 표시 중인 분석 결과를 서버 검수 큐에 사람 검토 필요
> 상태로 등록합니다. 이 동작은 게시가 아니며, `final_decision` /
> `policy_confidence` / `verification_card` 값은 변경되지 않습니다.
> 같은 결과를 다시 등록하면 기존 검수 작업이 그대로 사용됩니다.

Button id: `serverReviewRegisterCurrentBtn`. Status banner id:
`serverReviewRegisterStatus`. A single pure helper,
`buildReviewTaskFromResultPayload(context, itemIndex)`, builds the
request body from `currentReportContext` without mutating it. The
helper is exposed via `window.__serverReviewHelpers.buildFromResultPayload`
for JS regression tests.

### What the button does

1. Reads the in-memory `currentReportContext` (set when the page
   renders an analysis response).
2. If there is no result on screen, shows the friendly Korean message:
   > 등록할 분석 결과가 없습니다. 먼저 분석을 실행하거나 기록에서
   > 결과를 선택하세요.
3. Builds a `/jobs/{id}/result`-style envelope from the current
   results (read-only — the original objects are untouched).
4. Calls `POST /review/tasks/from-result` with exactly one auth
   header: `X-Review-Token: <session-token>`. The token is never
   added to the URL, query string, request body, or any log line.
5. On `200 OK`:
   - If `idempotent: false`, shows:
     > 검수 큐 등록 완료. 사람 검토 대기 상태로 추가되었습니다.
   - If `idempotent: true`, shows:
     > 이미 검수 큐에 등록된 결과입니다. 기존 검수 작업을 표시합니다
     > (사람 검토 필요).
   - Refreshes the queue list and selects the returned `task_id`
     (whether newly created or already existing).
6. On `503`, surfaces the deterministic disabled-API message (same as
   M8.1):
   > 리뷰 API가 비활성화되어 있습니다. 로컬/운영 환경에서
   > `REVIEW_API_ENABLED` 설정이 필요합니다.
7. On `403`, shows the generic "토큰을 확인해 주세요" message — no
   token detail is exposed in any code path.

### Operator checklist

1. Set `REVIEW_API_ENABLED=true` and `REVIEW_API_TOKEN=<secret>` in
   the local/dev environment (Render env is **not** modified by M8.2).
2. Run the API:
   ```powershell
   $env:REVIEW_API_ENABLED = "true"
   $env:REVIEW_API_TOKEN   = "<your-local-dev-token>"
   python -m uvicorn api_server:app --reload --port 8000
   ```
3. Open `http://127.0.0.1:8000/`, run an analysis, expand
   "검수자 도구 보기" → "서버 검수 큐 (관리자 전용)", paste the
   token, press "토큰 적용".
4. With a current analysis result on screen, press
   **검수 큐에 등록**. The button posts to `/review/tasks/from-result`
   and the new task appears in the list below.
5. Re-pressing the button for the same result produces the same
   `task_id` — the M8.0 idempotency key is honored.

### Hard contracts (preserved)

- **검수 큐 등록은 게시가 아니며, `final_decision` /
  `policy_confidence` / `verification_card` verdict를 변경하지 않는다.**
  The button only POSTs a snapshot; the server snapshot extractor
  reads verdict-side fields without writing to `analysis_results`.
- The transition matrix still refuses `published` / `corrected`.
- The token gate (`REVIEW_API_ENABLED` + `REVIEW_API_TOKEN` +
  `X-Review-Token` header) applies — disabled by default, no Render
  env changes, no public exposure.
- Token storage stays `sessionStorage`-only. M8.2 does **not**
  introduce a second token store.
- "사람 검토 필요" / "의미 매칭 근거 부족" wording on the public
  report is untouched.
- Semantic matching remains debug metadata only; the bridge never
  re-labels semantic signals as user-facing truth.

## H''. Local review-workflow smoke (M8.3)

`scripts/smoke_review_workflow.py` exercises the full M8.0–M8.2
review surface offline against a temporary SQLite database, using a
dummy in-process token that is never logged, persisted, or echoed.

### Exact command

```
python scripts/smoke_review_workflow.py --self-contained
```

The operational runner wires the same script behind a dedicated
profile:

```
python scripts/run_operational_checks.py --profile review-local
```

Both commands run end-to-end in a few seconds and exit `0` on pass,
`1` on a failed sub-check, `2` if `--self-contained` is omitted
(reserved for a future live mode).

### What it validates

| sub-check | what it asserts |
| --- | --- |
| `disabled_check` | With `REVIEW_API_ENABLED` unset, `GET /review/tasks` returns **503** and the detail mentions "disabled". |
| `token_check` | With env set: missing header → 403, wrong token → 403, correct token → 200. The token is sent only as `X-Review-Token`. |
| `task_creation_check` | `POST /review/tasks/from-result` with a synthetic Korean policy claim returns 200, `status=pending_review`, `human_review_required=true`, and the snapshot preserves `final_decision` / `policy_confidence` exactly. |
| `idempotency_check` | Two identical POSTs return the same `task_id` and the second carries `idempotent=true`. |
| `list_detail_check` | `GET /review/tasks` includes the created task; `GET /review/tasks/{id}` returns it with `status=pending_review`. |
| `decision_check` | Each allowed decision (`approve`, `reject`, `needs_more_evidence`, `comment`) is exercised against a **separate** synthetic task; each ends in the documented status. `comment` never changes status. |
| `verdict_isolation_check` | After `comment` + `approve`, the snapshot's `final_decision` / `policy_confidence` are byte-for-byte identical to creation time, and the original payload dict the script passed in is unchanged. |
| `publication_absent_check` | `POST /review/tasks/{id}/publish` → 404 / 405; submitting `{"decision": "published"}` or `{"decision": "corrected"}` → 400. |

### Token + environment safety

- The script uses a **dummy** in-process token literal (a fixed string,
  not a secret). It is **never** printed to stdout, stderr, or the
  JSON summary — verified by `tests/test_review_workflow_smoke.py`.
- The operator never has to paste a real `REVIEW_API_TOKEN`. The
  script sets `REVIEW_API_ENABLED=true` / `REVIEW_API_TOKEN=<dummy>`
  in-process only for the duration of the run and restores prior
  values (including unset) on exit, even on exception.
- The smoke does **not** enable the Render review API. `render.yaml`
  is not modified. `REVIEW_API_ENABLED` stays unset on Render.

### Hard contracts (preserved)

- **검수 큐 등록은 게시가 아니며, `final_decision` /
  `policy_confidence` / `verification_card` verdict를 변경하지
  않는다.** The smoke proves this via the verdict-isolation check.
- No publication path. The smoke asserts `/publish` is absent and the
  reserved statuses are unreachable.
- No real auth introduced. The temporary token gate remains the only
  fence.
- No OpenAI calls. No Render calls. No external network. Tests pin
  this (`test_review_workflow_smoke.py::SecretsAndIsolationTests`).
- Semantic matching activation is unchanged. The synthetic payload
  uses conservative Korean copy (`사람 검토 필요`, `moderate`) and
  never re-labels semantic signals as user-facing truth.

## H'''. Reviewer/admin UI safety hardening (M8.7)

M8.7 hardens the existing M8.0–M8.2 reviewer/admin panel in
`web/index.html` so the server-backed review workflow is unambiguously
internal, manual, token-gated, and **non-publication**. No backend
behavior, verdict logic, or semantic activation changes. No real auth.

### What changed in the UI

1. **Stronger admin-only labeling.** The `<details>` summary now reads
   **"내부 검수 도구 열기 (관리자 전용)"**. A red `role="note"`
   disclaimer renders at the top of the expanded panel:

   > **이 도구는 내부 운영자용입니다.** 검수 큐 등록은 게시가
   > 아니며, 기존 판정 결과(`final_decision` · `policy_confidence` ·
   > `verification_card`)를 변경하지 않습니다. 화면에 보이는 것은
   > 인증이 아니라 단순 표시이며, 실제 보호는 서버의
   > `REVIEW_API_ENABLED` 설정과 `X-Review-Token` 헤더로만
   > 이루어집니다. 여기서 발행되는 결과는 없습니다 (게시가 아님).

   The required Korean phrases — `관리자 전용`, `내부 검수`,
   `사람 검토 필요`, `검수 큐 등록`, `게시가 아님` — are pinned by
   `tests/review_ui.test.js::M87_REQUIRED_WORDING`.

2. **No `/review/*` auto-fetch on init.** Page initialization
   (`serverReviewBindEvents`) **never** calls `serverReviewLoadList()`
   automatically, even when a session token is already in
   `sessionStorage`. The init code only sets a non-loading banner that
   tells the operator to press **큐 새로고침**. The four explicit
   action sites are now the **only** call sites for `/review/*`:
   - **토큰 적용** (intentional explicit operator action — preserves M8.1 UX)
   - **큐 새로고침** (button click; gated on token presence)
   - **검수 큐에 등록** (button click; gated on token presence)
   - **판정 기록** submission (button click; gated on token presence)

   Pinned by `tests/review_ui.test.js` step 7 (the seeded-token
   re-init asserts zero `/review/*` fetches).

3. **Token clear lockout.** Clearing the token now resets the cached
   task list, hides the detail panel, blanks the register banner, and
   surfaces a deterministic Korean message:

   > 검수 토큰이 해제되었습니다. 서버 검수 작업을 보려면 다시 토큰을
   > 적용해 주세요.

   Every gated action (refresh / filter change / register / decision /
   detail load) re-checks the token and surfaces the same message
   when absent — no `/review/*` call can fire while the operator is
   locked out. Exposed for tests as
   `window.__serverReviewHelpers.tokenClearedMessage`.

4. **`published` / `corrected` removed from UI labels.** They remain
   *reserved* server statuses (the transition matrix still refuses
   them), but the UI carries no localized label for them, no decision
   value, no chip class entry. If the backend ever returns one, the
   chip falls through to the raw key — there is no copy that says
   "발행됨" / "정정됨" anywhere in `web/index.html`.

5. **Token handling unchanged but reinforced.** The token is still:
   - sent only as the `X-Review-Token` header
   - stored only in `sessionStorage` (`policy_ai_server_review_token`)
   - never placed in URLs, query strings, request bodies, logs, or
     visible status text
   - never hard-coded in committed source

   New static checks in `tests/review_ui.test.js` pin the absence of
   `?token=…` query strings, `Authorization: Bearer …` headers, and
   `"token": <value>` JSON body fields anywhere in the reviewer
   section.

### What did NOT change

- **No real auth.** UI visibility is not authentication. The only
  real protection remains `REVIEW_API_ENABLED=true` + a matching
  `REVIEW_API_TOKEN` + the `X-Review-Token` header sent by the client.
- **No publication path.** `published` / `corrected` are still
  reserved-and-unreachable server-side. No publish endpoint. No
  publish/correct UI affordance. The transition matrix refuses them.
- **No verdict mutation.** `buildReviewTaskFromResultPayload` still
  passes verdict-side fields verbatim and never mutates the source
  context (pinned since M8.2). Review decisions remain audit-only and
  never rewrite `final_decision` / `policy_confidence` /
  `verification_card`.
- **No semantic re-labeling.** Semantic matching remains debug
  metadata only. The reviewer UI surfaces no copy that would re-label
  semantic signals as user-facing truth.
- **No Render env change.** `REVIEW_API_ENABLED` stays unset on
  Render. `render.yaml` is unchanged. M8.7 is a frontend-only
  hardening pass.

### Why "UI visibility is not auth"

The `<details>` opt-in gate, the red admin warning, the disabled
controls when no token is present — all of these are *clarity*
mechanisms, not security mechanisms. A determined viewer can read the
HTML/JS. The actual security boundary is on the server:

1. `REVIEW_API_ENABLED` must be `true` (else every `/review/*` returns
   503 — pinned by `tests/test_review_api.py::SafetyGateTests`).
2. `REVIEW_API_TOKEN` must match what the client sends via
   `X-Review-Token` (else 403 — same test file).
3. Render keeps `REVIEW_API_ENABLED` unset by default.

If the operator wants a stricter UX in a future milestone, the right
path is real auth + an admin role, not stronger client-side hiding.

## I. Future work (post-M8.7)

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
python tests/test_review_workflow_smoke.py
python scripts/smoke_review_workflow.py --self-contained
python scripts/run_operational_checks.py --profile review-local
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
