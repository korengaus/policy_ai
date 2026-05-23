# Structured Logging (M14.0)

## Why

The Phase 1 audit identified 251 `print()` call sites across 13 legacy
modules, with no structured logging. Production debugging is hard:

- Cannot filter Render logs by request ID
- Cannot correlate events across pipeline modules
- Cannot easily search for "all judge confirms in the last hour"
- Cannot extract per-domain HTTP cache hit/miss rates from text logs

M14.0 adds opt-in JSON logging that coexists with the existing
`print()` calls, then migrates legacy modules over multiple sub-phases.

## Three-phase rollout

| Phase   | Scope                                                              | Status     |
|---------|--------------------------------------------------------------------|------------|
| M14.0a  | Infrastructure + adopt in M13.x modules; `print()` untouched       | this PR    |
| M14.0b  | Migrate `print()` in legacy modules incrementally                  | future     |
| M14.0c  | Add request ID propagation; latency histograms                     | future     |

## What M14.0a adds

- `structured_logging.py` — stdlib-only module (no pip dependency
  added).
- `scripts/check_logging.py` — diagnostic CLI with `--status`,
  `--emit-sample`, `--emit-sample-with-extra`, and `--json` modes.
- `tests/test_structured_logging.py` — 35 tests, including
  `ModuleAdoptionPin` (10 M13.x modules adopt `get_logger`),
  `LegacyIsolationPin` (18 untouched files do not),
  `PrintsStillPresentPin` (representative legacy file still has
  `print()`).
- `LOG_FORMAT` env var (default `text`; set to `json` for JSON output).
- `LOG_LEVEL` env var (default `INFO`).
- Logger initialization swap in 10 M13.x modules (initialization line
  only; no behaviour change to log statements).

## What M14.0a does NOT do

- Does NOT replace any existing `print()` call.
- Does NOT modify legacy modules (`api_server`, `main`,
  `official_crawler`, `official_source_body`, `news_collector`,
  `article_extractor`, `verification_card`, `policy_*`, `database`,
  `ai_reasoner`, `job_manager`, `evidence_*`, `contradiction_agent`,
  `bias_framing_agent`).
- Does NOT add Sentry, DataDog, or any external service.
- Does NOT add any pip dependency.
- Does NOT change `render.yaml` or any Render env var.
- Does NOT affect verdict logic, frontend, or any user-visible output.

## Usage in code

```python
# Legacy modules (untouched in M14.0a):
import logging
log = logging.getLogger(__name__)

# Adopted M13.x modules (initialization swapped in M14.0a):
import logging  # kept — modules still use logging.INFO, logging.WARNING
from structured_logging import get_logger
log = get_logger(__name__)

# Both then call (unchanged):
log.info("Something happened")
log.warning("Watch out", extra={"analysis_id": 123})
```

## JSON output shape

```json
{
  "ts": "2026-05-23T12:34:56.789012+00:00",
  "level": "INFO",
  "module": "llm_judge",
  "msg": "Judge action: confirm",
  "extra": {"analysis_id": 105, "provider": "anthropic"}
}
```

Korean text is preserved verbatim (`ensure_ascii=False`), so a Render
log search for `의미 매칭` matches the actual text. Unserializable
extras (e.g. a raw object instance) fall back to `repr()` rather than
raising.

## Enabling JSON output

Locally:

```
LOG_FORMAT=json LOG_LEVEL=DEBUG python scripts/check_logging.py --emit-sample
```

On Render (when ready in M14.0c or later):

- Set `LOG_FORMAT=json` in Render env vars.
- Render captures stderr automatically; JSON lines appear in the log
  viewer.
- Filter via Render's log search.

M14.0a does NOT add `LOG_FORMAT` to `render.yaml`. The env var must be
set deliberately by the operator when the team is ready.

## Safety invariants

- `LOG_FORMAT` unset → behaviour identical to stdlib's text format
  (backward compatible).
- `configure_logging()` is idempotent.
- JSON formatter handles unserializable extras (falls back to `repr()`).
- Korean text preserved as UTF-8 (no `\u` escapes).
- Only removes its own handlers (tagged `_m14_managed`) — pytest's
  `caplog` and other test infrastructure handlers survive.
- Stderr is reconfigured to UTF-8 on Windows so JSON output bytes are
  always valid UTF-8 regardless of the operator's local codepage.
- `validate.py` clears `LOG_FORMAT` before running subprocesses so a
  deterministic text-mode run is the canonical CI state.

## 12 modules listed in the brief — 10 actually modified

The brief listed 12 modules to adopt the helper. Two had no
module-level logger initialization (just like the brief noted as a
permitted no-op):

- `source_registry.py` — no `logging.getLogger(...)` call. Untouched.
- `korean_constants.py` — no `logging.getLogger(...)` call. Untouched.

The 10 modules actually modified each had a single
`(log|logger) = logging.getLogger(__name__)` line, replaced with the
equivalent `get_logger(__name__)`. The variable name (`log` vs.
`logger`) was preserved per module.

## Rollback

To revert M14.0a:

1. In each of the 10 modified modules, replace
   `from structured_logging import get_logger` + `(log|logger) = get_logger(__name__)`
   with the original `(log|logger) = logging.getLogger(__name__)` line.
2. Delete `structured_logging.py`, `scripts/check_logging.py`,
   `tests/test_structured_logging.py`, `docs/STRUCTURED_LOGGING.md`.
3. Remove the `validate.py` / `run_operational_checks.py` additions.

No other code depends on these files. The 251 `print()` call sites
were never touched in M14.0a, so they continue to work exactly as
before.

## CI

`scripts/validate.py` runs `tests/test_structured_logging.py` plus
the `--help` and `--status` CLI smokes on every CI run. No actual
network call is made and no external logging service is involved.
