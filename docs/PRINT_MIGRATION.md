# print() → Structured Logging Migration (M14.0b)

## Scope

M14.0a built the structured logging infrastructure but did NOT touch
any `print()` call. M14.0b migrates the top 5 files (189 of 251
prints = 75%):

| File                       | Prints migrated | Status         |
|----------------------------|-----------------|----------------|
| `main.py`                  | 62              | this milestone |
| `official_crawler.py`      | 57              | this milestone |
| `verification_card.py`     | 27              | this milestone |
| `news_collector.py`        | 26              | this milestone |
| `article_extractor.py`     | 17              | this milestone |
| **Total**                  | **189**         | done           |

Deferred to M14.0c:

| File                             | Prints | Status |
|----------------------------------|--------|--------|
| `evidence_comparator.py`         | 14     | future |
| `policy_decision.py`             | 11     | future |
| `policy_confidence.py`           | 11     | future |
| `policy_impact.py`               | 10     | future |
| `bias_framing_agent.py`          | 6      | future |
| `evidence_extraction_agent.py`   | 5      | future |
| `contradiction_agent.py`         | 4      | future |
| `official_source_body.py`        | 1      | future |
| **Total**                        | **62** | future |

## Migration rules applied

Each `print()` was replaced with the appropriate log level, determined
mechanically from the message content + whether the call was inside an
`except` block:

- Message contains `"error"` / `"fail"` / `"exception"` / `"traceback"`
  / `"crash"` / `"abort"`, or Korean `"오류"` / `"에러"` / `"실패"` /
  `"예외"` / `"⚠"` / `"❌"` / `"✗"` → `log.error`
- Message contains `"warn"` / `"warning"` / `"caution"` /
  `"deprecated"`, or Korean `"경고"` / `"주의"` → `log.warning`
- Message contains `"debug"` / `"[debug]"` → `log.debug`
- Inside an `except` block with no other indicator → `log.error`
- Otherwise → `log.info`

The function name token (`print` → `log.<level>`) was the only edit
at each call site. Argument text and quoting were preserved byte-for-byte.

## Level distribution per file

| File | info | warning | error | debug |
|---|---|---|---|---|
| main.py | 60 | 0 | 2 | 0 |
| official_crawler.py | 55 | 1 | 4 | 0 |
| verification_card.py | 26 | 0 | 0 | 1 |
| news_collector.py | 22 | 0 | 4 | 0 |
| article_extractor.py | 11 | 0 | 6 | 0 |
| **Total migrated** | **174** | **1** | **16** | **1** |

Note: official_crawler.py's `warning` count includes the existing
M13.3b `official_crawler_cache_put_failed` log; the migration added
zero new warnings to that file. The `error` count for main.py
reflects two pre-migration prints whose text included the keyword
"error".

## Output behaviour

With `LOG_FORMAT` unset (current Render config), the output is
visually identical to pre-M14.0b — same text, same Korean characters,
same banners, same emission order. The only routing difference is
that `print(...)` wrote to stdout while `logger.info(...)` writes to
stderr via the structured logger. Render captures both streams and
displays them in chronological order, so the operator-visible log
viewer is unchanged.

With `LOG_FORMAT=json`, each migrated print now produces a JSON
record:

```json
{
  "ts": "2026-05-23T12:34:56.789012+00:00",
  "level": "INFO",
  "module": "main",
  "msg": "[main] analyze_pipeline took 124.3s"
}
```

## What M14.0b does NOT do

- Does NOT add `extra={...}` structured fields. A future milestone
  may add these case-by-case for high-value events.
- Does NOT add request IDs (M14.1).
- Does NOT enable JSON logging on Render (operator decision).
- Does NOT remove or change the text content of any print's message.
- Does NOT migrate the remaining 8 files (M14.0c).
- Does NOT add new log statements — the only edit was the function
  name token at each existing print site.
- Does NOT change verdict logic, control flow, or return values.

## Safety verification

`tests/test_print_migration.py` (13 cases) pins:

- **Zero remaining print() calls** in the 5 target files (AST Call
  node count + tokeniser scan for `print(` patterns).
- `from structured_logging import get_logger` present in each
  migrated file.
- Module-level `log = get_logger(__name__)` (or `logger = …` per the
  file's existing convention) present in each migrated file.
- `log.X` call count ≥ pre-migration print count + pre-existing log
  calls (per file).
- No `file=` / `end=` / `flush=` / `sep=` kwargs leaked onto any
  `log.X(...)` call (those are print-only kwargs and would crash at
  runtime).
- The 8 deferred files still have their original print counts (14,
  11, 11, 10, 6, 5, 4, 1).
- The 8 deferred files do NOT import structured_logging (would
  imply someone partially migrated them outside this milestone).
- Korean text probes (정책, 검증, 공식, 분석, 한국) still appear in
  the files that contained Korean prints — sanity check that the
  migration did not strip Korean characters.

`tests/test_structured_logging.py`'s `LegacyIsolationPin` was updated
to drop the 5 migrated files from its allow-list of files that must
NOT import structured_logging. The list now has 13 entries (the 8
M14.0c-deferred files plus 5 still-untouched pipeline / storage /
verdict modules).

## Rollback

To revert M14.0b:

1. Revert the M14.0b commit.
2. Verify with `python scripts/validate.py` + `npm test`.
3. No data migration is needed — logging is stateless.

The 8 untouched files (M14.0c scope) are unaffected by M14.0b's
rollback.

## Render behaviour

No Render env var changes in M14.0b. With `LOG_FORMAT` unset
(default text), the Render log viewer shows the same lines as before
— just routed via Python's logging framework instead of `print`.

To later enable JSON logs on Render (separate operator decision):

1. Render dashboard → Environment → Add `LOG_FORMAT=json`.
2. Save → auto-redeploy.
3. Render log viewer now shows JSON per line, searchable by level,
   module, msg, or any future extras.

See `docs/STRUCTURED_LOGGING.md` for the broader logging architecture
and `docs/CACHE_ACTIVATION_GUIDE.md` for how the same env-var flow
works for `HTTP_CACHE_ENABLED`.

## M14.0c — Migration completion (this PR)

M14.0c migrated the remaining 8 files (62 prints), bringing the
total to:

| Phase    | Files | Prints | Status         |
|----------|-------|--------|----------------|
| M14.0a   | 0     | 0      | infrastructure |
| M14.0b   | 5     | 189    | done           |
| M14.0c   | 8     | 62     | this PR        |
| **Total** | **13** | **251** | **complete**   |

### Files migrated in M14.0c

| File                            | Prints | Notes |
|---------------------------------|--------|-------|
| `evidence_comparator.py`        | 14     | All `log.info` |
| `policy_decision.py`            | 11     | VERDICT LOGIC — all `log.info`, no business logic touched |
| `policy_confidence.py`          | 11     | VERDICT LOGIC — all `log.info`, no business logic touched |
| `policy_impact.py`              | 10     | VERDICT LOGIC — all `log.info`, no business logic touched |
| `bias_framing_agent.py`         | 6      | All `log.info` |
| `evidence_extraction_agent.py`  | 5      | All `log.info` |
| `contradiction_agent.py`        | 4      | All `log.info` |
| `official_source_body.py`       | 1      | 1 `log.error` (inside an except block) |

**M14.0c level totals:** 61 `info`, 0 `warning`, 1 `error`, 0 `debug`.

### Verdict invariance verified

The three verdict-logic modules' migrations were proven
non-functional by `tests/test_print_migration_m14_0c.py::M14_0C_VerdictInvariancePin`,
which subprocess-invokes the existing verdict test suites and
asserts each exits 0:

- `tests/test_verdict_label_b08_fix.py` — 24 cases, PASS
- `tests/test_verdict_label_diagnostic.py` — 42 cases, PASS
- `tests/test_verdict_producer_comparison.py` — 37 cases, PASS
- `tests/test_artifact_evidence_linker.py` — 42 cases, PASS (exercises `evidence_comparator.py`)

If any verdict assertion broke after the migration, one of these
would fail. They all pass, so verdict invariance is established.

A second structural pin (`M14_0C_VerdictModuleStructuralPin`)
checks each verdict module's top-level AST nodes: exactly one new
`ImportFrom structured_logging` + exactly one new `Assign log = …`
was added. No new class, function, or unrelated statement was
introduced.

### What's complete

Every `print()` across the 13 originally-listed legacy modules is
now routed through `structured_logging.get_logger`. With
`LOG_FORMAT` unset (default), output is visually identical to
pre-M14.0a. With `LOG_FORMAT=json` (Render env var, operator
decision), every log line is a searchable JSON record.

The pre-M14.0a inventory:

```
main.py                     62 prints
official_crawler.py         57
verification_card.py        27
news_collector.py           26
article_extractor.py        17
evidence_comparator.py      14
policy_decision.py          11
policy_confidence.py        11
policy_impact.py            10
bias_framing_agent.py        6
evidence_extraction_agent.py 5
contradiction_agent.py       4
official_source_body.py      1
Total                      251 prints across 13 files
```

After M14.0c: **0 prints, 0 prints, 0 prints (all 13 files).**

### What's NOT done (future milestones)

- Adding `extra={...}` structured fields to log calls (case-by-case,
  future milestones).
- Request ID propagation across calls (M14.1).
- Enabling `LOG_FORMAT=json` on Render (operator decision).
- Migrating the few `print()` calls that may exist in test scripts
  themselves (out of scope — tests can legitimately use `print` for
  output the runner captures).

### Render activation note

Until the operator sets `LOG_FORMAT=json` on Render, the platform
serves the same operator-visible log lines as pre-migration. No
behaviour change is observable. The migration is fully reversible
via `git revert` for any single milestone (M14.0b or M14.0c).
