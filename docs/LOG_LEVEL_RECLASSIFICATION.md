# Log Level Reclassification (M14.4)

## What this fixed

After M14.2 (JSON logging activation) and M14.3a (request_id propagation),
Render log inspection revealed that the `level` field on several lines
was wrong: status-field reports were tagged `"level":"ERROR"` even though
no error had occurred.

Examples found in production logs:

```json
{"level":"ERROR","msg":"  rendered_error: None"}                                  // No rendering error — normal state report
{"level":"ERROR","msg":"  error_page_detected: False"}                            // This is NOT an error page
{"level":"ERROR","msg":"  error_page_reason: None"}                               // No error page reason
{"level":"ERROR","msg":"  error: Best official document relevance below threshold: 0"}  // Filtering decision
{"level":"ERROR","msg":"[OfficialBody] candidates=3 fetched=3 usable=3 matched=2 failures={}"}  // Summary
```

The root cause was M14.0b/c's migration rule, which mapped any message
containing the substring `"error"` to `log.error`. That caught field
names like `rendered_error:`, `error_page_detected:`, `error_page_reason:`
that just *happen* to have "error" in the key name.

## What M14.4 changed

Five `log.error` calls were reclassified to `log.info`:

| File                      | Line (pre-M14.4) | log.error → log.info |
|---------------------------|------------------|----------------------|
| official_crawler.py       | 1490             | `  rendered_error: ...`        |
| official_crawler.py       | 1518             | `  error_page_detected: ...`   |
| official_crawler.py       | 1519             | `  error_page_reason: ...`     |
| official_crawler.py       | 1522             | `  error: ...`                 |
| official_source_body.py   | 578              | `[OfficialBody] candidates=... fetched=... usable=... matched=... failures=...` |
| **Total**                 |                  | **5**                          |

All five live inside status-reporter functions
(`print_official_evidence_results`, the summary at the end of
`enrich_with_official_bodies`); none are inside an `except` block; none
of their messages contain a real-failure keyword.

## Rules applied

KEEP as `log.error`:

- Call is inside an `except` block (the except-block rule wins over any
  text classification).
- Message contains a real-error keyword: `"fail" / "failed"`,
  `"exception"`, `"timeout"`, `"abort"`, `"connection reset"`,
  `"connection refused"`, `"connection aborted"`, `"traceback"`,
  Korean: `"실패"`, `"예외"`, `"오류 발생"`.
- Message starts with `Error:` (Python exception repr convention).

CHANGE to `log.info`:

- Status field names: `rendered_error: None`, `error_page_detected: False`,
  `error_page_reason: None`.
- Bare `error: <value>` where the value is a state/threshold reason
  (e.g., `"Best official document relevance below threshold"`) and the
  surrounding context is a status-reporter, not an exception handler.
- Summary lines that count items including a `failures={...}` field
  where the field is just a counter dict (may be empty).

Edge case: if a `log.error` call is inside an `except` block *and* its
message reads like a status field, KEEP it as `log.error`. The except
block context wins. This is why `article_extractor.py` lines 372-377
remained `log.error` — they live inside `except Exception as error:`
and report the values that the function fell back to when the
exception fired.

## What real errors were preserved

Every `log.error` inside an `except` block was left untouched:

- `main.py`: `[AnalysisCache] read failed`, `[AnalysisCache] write failed`
- `news_collector.py`: `[NewsCollector] Cache read failed`,
  `[NewsCollector] Cache write failed`, `원문 URL 변환 실패`
- `news_collector.py`: `[NewsCollector] Google RSS failed, trying Naver fallback`
  (not inside `except`, but contains the "failed" verb)
- `article_extractor.py`: all 6 fallback-reporting lines inside the
  outer `except Exception as error:`

These are pinned by
`tests/test_log_level_reclassification.py::PreservedRealErrorsStillErrorPin`.

## Detection pin

`tests/test_log_level_reclassification.py` ships five pins:

1. **NoFalsePositiveErrorsPin** — every `log.error` in the 13 migrated
   files must be inside an `except` block, or its message must contain
   a strong-error keyword, or start with `Error:`. This catches future
   migration mistakes that re-introduce field-name `log.error` calls.
2. **FieldNameErrorPatternPin** — no `log.error` may have a message
   matching the regex `^\s*(\w+_error|error_\w+)\s*[:=]`. This is a
   stricter, targeted guard against the exact Render log examples that
   motivated M14.4.
3. **ReclassifiedPatternsArInfoNow** — each of the five M14.4
   reclassifications must currently be `log.info`. Protects against
   accidental reverts.
4. **PreservedRealErrorsStillErrorPin** — each known real-error pattern
   must still be `log.error`. Protects against an over-broad future
   "silence the noise" pass.
5. **TotalLogCallCountInvariant** — total log-call count across the
   13 files is exactly 254 (unchanged from M14.0c). M14.4 only shifts
   levels, never adds or removes a call.
6. **ExceptBlockErrorsPreserved** — counts of `log.error` inside
   `except` blocks per file are pinned exactly: main.py=2,
   news_collector.py=3, article_extractor.py=6.

## What stays the same

- Total log call count: 254 (unchanged).
- All message text: unchanged. M14.4 only changed the method name
  (`log.error` → `log.info`); no message string was touched.
- All real errors still appear as `"level":"ERROR"` in JSON logs.
- request_id, JSON formatting, all other M14.x features unchanged.
- Verdict logic untouched. All verdict tests pass.

## Render impact (expected after deploy)

Before M14.4, an analysis run emitted roughly **30+ `"level":"ERROR"`
lines per request**, most of them field-name reports. After M14.4, the
same run should emit only a handful — the genuine errors from
`except` blocks (if any fired) and the few `"failed"`-prefixed lines.

To verify after deploy:

```bash
# In Render's log search:
"level":"ERROR"
```

The match count per analysis should drop dramatically. Lines like
`rendered_error: None`, `error_page_detected: False`,
`error_page_reason: None`, and the `[OfficialBody]` summary should now
appear with `"level":"INFO"` and the same message text as before.

Real connection errors, if they happen, will still appear as
`"level":"ERROR"` — they were not touched by this change.

## Rollback

To revert M14.4:

1. Revert the M14.4 commit.
2. `python scripts/validate.py` + `npm test` to confirm green.
3. No data migration needed.

The reclassification is in-source only; no env vars, no schema, no
client-visible API changes.

## Future direction

M14.4 is a hotfix, not a structural change. The next planned milestone
is **M14.3b** — propagating `request_id` through `job_manager`
background workers so async-job log lines inherit the originating
request's ID. The M14.4 detection pin will continue to guard against
future migration mistakes regardless of what comes next.
