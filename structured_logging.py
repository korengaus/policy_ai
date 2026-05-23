"""Structured logging (M14.0a) — opt-in JSON output, coexists with print().

Behaviour
---------

* Setting env var ``LOG_FORMAT=json`` switches the root logger to emit
  JSON lines on stderr.
* Default (``LOG_FORMAT`` unset or ``"text"``) emits stdlib's familiar
  ``%(asctime)s %(levelname)s %(name)s: %(message)s`` text format.
* ``LOG_LEVEL`` env var controls the threshold (default ``INFO``).
  Invalid values fall back to ``INFO`` rather than crashing.
* ``print()`` statements throughout the codebase are NOT affected by
  this module. Stdout / file descriptor 1 stays untouched.
* Modules opt in by calling :func:`get_logger` instead of
  ``logging.getLogger``. The helper guarantees :func:`configure_logging`
  has run before returning, so the formatter is attached even when the
  module is imported before any explicit bootstrap.

JSON record shape (subject to future extension)::

    {
      "ts": "2026-05-23T12:34:56.789012+00:00",
      "level": "INFO",
      "module": "llm_judge",
      "msg": "Judge action: confirm",
      "extra": { ... if extra={"k": "v"} was passed ... }
    }

Safety
------

* No public function raises. Formatting errors fall back to a short
  text record; bootstrap errors are written to stderr and swallowed.
* ``configure_logging`` only adds handlers tagged ``_m14_managed`` and
  only removes its own tagged handlers — so it never breaks pytest's
  ``caplog`` or other test-infrastructure handlers.
* Korean text is preserved verbatim (``json.dumps`` is invoked with
  ``ensure_ascii=False``).
* Stdlib only. No new pip dependency. No external logging service.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------


_CONFIG_LOCK = threading.Lock()
_CONFIGURED = False


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def get_log_format() -> str:
    """Return ``"json"`` if env ``LOG_FORMAT`` equals ``"json"``
    (case-insensitive, leading/trailing spaces and tabs stripped).
    Anything else — including unset, empty, ``"text"``, ``"yaml"``,
    or values with embedded newlines / control chars — returns
    ``"text"``.

    Whitespace stripping is restricted to spaces and tabs so that
    weird control-character payloads (``"json\\n\\n"``,
    ``"json;DROP TABLE"``) do not coerce to the JSON code path —
    a defence-in-depth choice against env-injection surprises.
    """
    raw = os.environ.get("LOG_FORMAT", "").strip(" \t").lower()
    return "json" if raw == "json" else "text"


def is_json_logging_enabled() -> bool:
    return get_log_format() == "json"


def get_log_level() -> int:
    """Return the configured log level as an int. Invalid values
    fall back to ``logging.INFO`` rather than crashing. Same
    spaces-and-tabs-only stripping policy as :func:`get_log_format`."""
    raw = os.environ.get("LOG_LEVEL", "").strip(" \t").upper()
    if not raw:
        return logging.INFO
    candidate = getattr(logging, raw, None)
    if isinstance(candidate, int):
        return candidate
    return logging.INFO


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


# Standard ``LogRecord`` attributes — anything outside this set in
# ``record.__dict__`` is considered "extra" (i.e. passed via
# ``logger.info(..., extra={...})``) and serialised into the JSON
# record's ``extra`` field.
_STANDARD_LOG_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "message",
    "asctime", "taskName",
})


def _safe_json(value: Any) -> Any:
    """Best-effort JSON-safe conversion. NEVER raises."""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:  # noqa: BLE001 — any serialisation failure
        try:
            return repr(value)
        except Exception:  # noqa: BLE001
            return "<unrepresentable>"


class JsonFormatter(logging.Formatter):
    """Formats a ``LogRecord`` as a single-line UTF-8 JSON object.

    Falls back to a short text record on any formatting error so a
    misbehaving caller cannot crash the logger.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            ts = datetime.fromtimestamp(
                record.created, tz=timezone.utc,
            ).isoformat()
            payload = {
                "ts": ts,
                "level": record.levelname,
                "module": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            elif record.exc_text:
                payload["exc"] = record.exc_text

            extras = {}
            for key, value in record.__dict__.items():
                if key in _STANDARD_LOG_RECORD_ATTRS:
                    continue
                if key.startswith("_"):
                    continue
                extras[key] = _safe_json(value)
            if extras:
                payload["extra"] = extras
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001
                msg = "<message render failed>"
            return (
                f"{record.levelname} {record.name}: {msg} "
                f"[JsonFormatter error: {exc}]"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_TEXT_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(force: bool = False) -> None:
    """Bootstrap the root logger with the appropriate formatter.

    Idempotent — calling repeatedly is safe and a no-op after the
    first successful run, unless ``force=True`` is passed (used by
    tests after env-var changes).

    NEVER raises. Failure is written to stderr and swallowed.
    """
    global _CONFIGURED
    with _CONFIG_LOCK:
        if _CONFIGURED and not force:
            return
        try:
            root = logging.getLogger()
            # Remove ONLY our previously installed handlers. Other
            # handlers (pytest's caplog, ipython's, etc.) are left
            # intact.
            for handler in list(root.handlers):
                if getattr(handler, "_m14_managed", False):
                    root.removeHandler(handler)

            # Ensure stderr emits UTF-8 bytes regardless of platform.
            # Linux/macOS default to UTF-8 already; Windows defaults
            # to the operator's local codepage (e.g. cp949 on Korean
            # Windows), which would silently corrupt Korean text in
            # JSON output. ``reconfigure`` is available on Python 3.7+
            # and is a no-op on streams that don't support it.
            try:
                sys.stderr.reconfigure(  # type: ignore[attr-defined]
                    encoding="utf-8", errors="replace",
                )
            except Exception:  # noqa: BLE001
                pass

            handler = logging.StreamHandler(stream=sys.stderr)
            # Tag so future configure_logging calls and reset_for_tests
            # can identify their own handlers.
            handler._m14_managed = True  # type: ignore[attr-defined]

            if is_json_logging_enabled():
                handler.setFormatter(JsonFormatter())
            else:
                handler.setFormatter(
                    logging.Formatter(fmt=_TEXT_FMT, datefmt=_TEXT_DATEFMT)
                )

            root.addHandler(handler)
            root.setLevel(get_log_level())
            _CONFIGURED = True
        except Exception as exc:  # noqa: BLE001
            try:
                sys.stderr.write(
                    f"[structured_logging] configure_logging failed: "
                    f"{exc}\n"
                )
            except Exception:  # noqa: BLE001
                pass


def get_logger(name: str) -> logging.Logger:
    """Module convenience: ``configure_logging`` + ``logging.getLogger``.

    Idiomatic usage::

        from structured_logging import get_logger
        log = get_logger(__name__)

    Returns the same ``logging.Logger`` instance that
    ``logging.getLogger(name)`` would, so existing call sites
    (``log.info(...)`` etc.) continue to work without modification.
    """
    configure_logging()
    return logging.getLogger(name)


def reset_for_tests() -> None:
    """Test helper: clear configured state and remove ONLY our managed
    handlers. Safe to call when no handlers are installed."""
    global _CONFIGURED
    with _CONFIG_LOCK:
        try:
            root = logging.getLogger()
            for handler in list(root.handlers):
                if getattr(handler, "_m14_managed", False):
                    root.removeHandler(handler)
            _CONFIGURED = False
        except Exception as exc:  # noqa: BLE001
            try:
                sys.stderr.write(
                    f"[structured_logging] reset_for_tests failed: "
                    f"{exc}\n"
                )
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Diagnostic helper
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """Snapshot of the current logging config. Does not modify state.
    NEVER raises."""
    try:
        managed = sum(
            1 for h in logging.getLogger().handlers
            if getattr(h, "_m14_managed", False)
        )
        total = len(logging.getLogger().handlers)
    except Exception:  # noqa: BLE001
        managed = 0
        total = 0
    level_int = get_log_level()
    return {
        "log_format": get_log_format(),
        "log_level_name": logging.getLevelName(level_int),
        "log_level_int": level_int,
        "configured": _CONFIGURED,
        "managed_handler_count": managed,
        "total_handler_count": total,
    }
