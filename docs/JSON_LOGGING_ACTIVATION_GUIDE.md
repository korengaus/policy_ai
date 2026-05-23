# JSON Logging Activation Guide (M14.2)

## What you're activating

M14.0a built `structured_logging.py` with a JSON formatter.
M14.0b/c migrated all 251 `print()` calls in 13 legacy files to
logger calls.

With `LOG_FORMAT` unset (current Render config), logs appear as
plain text:

```
2026-05-23 14:32:01 INFO official_crawler: [official_crawler] Fetching ...
```

With `LOG_FORMAT=json`, each line becomes a structured JSON record:

```json
{"ts":"2026-05-23T14:32:01.234567+00:00","level":"INFO","module":"official_crawler","msg":"[official_crawler] Fetching ..."}
```

JSON records are searchable, filterable, and machine-parsable — useful
for debugging the upcoming M13.1b (LLM Judge API integration) and
M12.0c (Postgres activation) milestones.

## Pre-activation checklist

- [ ] Latest commit on `main` includes M14.0c (print migration complete).
- [ ] Latest CI run on `main` is green.
- [ ] M14.2 verification tooling is committed.
- [ ] You have ~5 minutes for activation and verification.
- [ ] You can roll back by removing the env var (no data migration
      needed).

## Step 1 — Local verification

Verify JSON logging works correctly in your local environment:

```
python scripts/check_json_logging.py --local
```

Expected output:

- 4 sample lines captured from a subprocess running with
  `LOG_FORMAT=json`.
- Every line is VALID JSON with `ts`, `level`, `module`, `msg`.
- The 4th line contains Korean text and is preserved as UTF-8
  (not `\uXXXX` ASCII-escaped).
- Final result: **PASS**.

If FAIL, **STOP** and investigate before touching Render. Likely
causes: an accidental change to `structured_logging.JsonFormatter`
since M14.0a, or a Python locale issue on the operator's machine.

## Step 2 — Render baseline (optional)

Before activation, run a baseline check to confirm Render is reachable
and to see what to look for in the log viewer:

```
python scripts/check_json_logging.py \
    --base-url https://policy-ai-q5ax.onrender.com \
    --skip-smoke
```

The script will:

- Confirm `/health` returns 200.
- Print the operator hints for the Render log viewer.
- NOT run `smoke_async_job` (use `--skip-smoke` to keep this fast).

With `LOG_FORMAT` unset on Render, the log viewer should still show
text-format lines.

## Step 3 — Activate on Render

Render dashboard → policy-ai service → Environment.

Add one environment variable (do NOT remove existing ones):

| Key          | Value  | Purpose                              |
|--------------|--------|--------------------------------------|
| `LOG_FORMAT` | `json` | Switch logging to structured JSON    |

Save. Render auto-redeploys (~2 minutes). Wait for "Live".

## Step 4 — Verify activation

Run the verification command WITHOUT `--skip-smoke` so a fresh
analysis populates the Render log stream:

```
python scripts/check_json_logging.py \
    --base-url https://policy-ai-q5ax.onrender.com
```

Then in Render dashboard → Logs:

1. Filter for the last ~2 minutes.
2. Each line should now start with `{"ts":...`.
3. Examples to search for:
   - `"module":"official_crawler"` — crawler events.
   - `"module":"verification_card"` — verification card construction.
   - `"module":"policy_decision"` — policy decision logic.
   - `"module":"http_cache"` — HTTP cache hits / misses.
   - `"level":"ERROR"` — any errors.
   - `"official_crawler_cache_event"` — HTTP cache decisions (the M13.3b structured event).

If lines are still text, the deploy may not have completed. Wait
1–2 more minutes and re-check.

## Step 5 — Useful grep patterns

In Render's log search:

| Goal                                           | Search pattern                          |
|------------------------------------------------|-----------------------------------------|
| All errors                                     | `"level":"ERROR"`                       |
| All HTTP cache hits                            | `"cache_hit":true`                      |
| All HTTP cache misses                          | `"cache_hit":false`                     |
| All policy decisions                           | `"module":"policy_decision"`            |
| All verification card builds                   | `"module":"verification_card"`          |
| Specific URL fetched                           | `"url":"https://www.fsc.go.kr/...`      |
| All scheduler / job events                     | `"module":"job_manager"`                |
| All news collection                            | `"module":"news_collector"`             |
| All article extractions                        | `"module":"article_extractor"`          |
| All Postgres dual-write events                 | `"module":"postgres_storage"`           |
| All LLM Judge events (when M13.1b lands)       | `"module":"llm_judge"`                  |

## Step 6 — Rollback (if needed)

To revert to text logging:

1. Render dashboard → Environment.
2. Delete `LOG_FORMAT` (or set to empty value, or set to `text`).
3. Save. Render auto-redeploys.

No data migration needed. Logs revert to plain text format on the
next request.

## What stays the same

- Verdict logic: unchanged.
- Cache behavior: unchanged.
- Analysis output: unchanged.
- API responses: unchanged.
- Render performance: ~no measurable difference (JSON formatter
  overhead is microseconds per log line).

## What changes

- Log line format only.
- Searchability in Render dashboard.
- Future debugging capability.

## Cost

No cost. JSON formatting is in-process. No external service.

## What this enables for the future

- **M13.1b (LLM Judge API connection)**: per-call cost and latency
  tracking via JSON `extra={...}` fields.
- **M12.0c (Postgres activation)**: query failure debugging via
  structured logs.
- **M14.3 (Request ID propagation)**: correlating events across
  modules via a `request_id` field.
- **M15.0 (External log shipping)**: forwarding JSON logs to
  Sentry / DataDog / etc.

## Reverting M14.2 tooling (not the production setting)

M14.2 adds tooling and docs only. The production setting is your
`LOG_FORMAT` env var on Render (Step 3). The tooling can be reverted
without touching Render by removing the new files:

```
rm scripts/check_json_logging.py
rm tests/test_check_json_logging.py
rm docs/JSON_LOGGING_ACTIVATION_GUIDE.md
# Then revert scripts/validate.py + scripts/run_operational_checks.py
# + docs/PRINT_MIGRATION.md to their pre-M14.2 states.
```

This is independent of whether you activated `LOG_FORMAT=json` on
Render. The activation lives in the Render dashboard, not in the
repo.
