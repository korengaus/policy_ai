"""M11.7a — pins for the two HIGH-priority Category 2 exception sites
flagged by docs/EXCEPTION_HANDLING_AUDIT.md and resolved with structured
warning logs:

  Site 1: memory_store.load_policy_memory
          (event: memory_store.load_corrupt_or_missing)
  Site 2: official_crawler.fetch_best_official_document outer wrapper
          (event: official_crawler.outer_wrapper_failure)

Both fixes are LOGGING-ONLY — the existing sentinel return values stay
byte-identical. These pins:

  (a) fire on the corrupt / failure path AND check the warning was
      emitted with the documented event name + the expected `extra`
      payload fields,
  (b) confirm the happy path emits NO warning (so legitimate "first
      run" / "empty file does not exist" / "successful fetch" cases
      don't get flagged as failures), and
  (c) confirm the return value on the failure path matches what the
      pre-M11.7a code produced (no control-flow change snuck in).

Uses ``unittest`` + ``logging.handlers.MemoryHandler`` since the rest
of the repo's test suite is unittest-based and that pattern matches
how ``tests/test_structured_logging.py`` captures emitted records.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _CapturingHandler(logging.Handler):
    """Records every emitted LogRecord so tests can inspect the exact
    log calls made during the unit under test. Filters by logger name
    prefix so unrelated chatter (e.g., from imports) does not leak in."""

    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name_prefix or record.name.startswith(
            self._name_prefix + "."
        ):
            self.records.append(record)


def _attach_capturing_handler(logger_name: str) -> _CapturingHandler:
    """Attach a capturing handler to the named logger AND make sure it
    will actually see the record. Uses propagate=False so we don't
    duplicate; ensures the handler is at DEBUG so WARNING records
    are caught."""
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler(logger_name)
    logger.addHandler(handler)
    # Make sure the level is permissive enough to see WARNING.
    if logger.level == logging.NOTSET or logger.level > logging.WARNING:
        logger.setLevel(logging.WARNING)
    return handler


def _detach_handler(logger_name: str, handler: logging.Handler) -> None:
    logging.getLogger(logger_name).removeHandler(handler)


def _records_with_event(
    records: list[logging.LogRecord], event_name: str
) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == event_name]


# ---------------------------------------------------------------------------
# Site 1 — memory_store.load_policy_memory
# ---------------------------------------------------------------------------


class MemoryStoreLoadPolicyMemoryWarningTests(unittest.TestCase):
    """When the memory file is corrupt JSON the function must emit
    `memory_store.load_corrupt_or_missing` at WARNING with a payload
    that names the path, the exception type, the exception message
    (truncated), and the fallback sentinel."""

    LOGGER_NAME = "memory_store"
    EVENT_NAME = "memory_store.load_corrupt_or_missing"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmppath = Path(self.tmpdir.name) / "memory.json"

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def _call_with_path(self, path: Path) -> dict:
        """Re-import memory_store with MEMORY_FILE pointed at ``path``
        so the function under test reads our synthetic file rather
        than the real one."""
        import memory_store

        with mock.patch.object(memory_store, "MEMORY_FILE", str(path)):
            return memory_store.load_policy_memory()

    def test_load_policy_memory_corrupt_file_emits_warning(self):
        # Write invalid JSON. The `try: json.load(...)` will raise
        # JSONDecodeError, which is caught by the broad except.
        self.tmppath.write_text("{this is not valid json", encoding="utf-8")

        result = self._call_with_path(self.tmppath)

        # Sentinel preserved.
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("topics"), {})
        self.assertEqual(result.get("articles"), [])
        self.assertIsNone(result.get("last_updated_at"))
        self.assertIn("created_at", result)

        # Warning emitted with the expected event and payload.
        matching = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(matching), 1,
            f"Expected exactly one '{self.EVENT_NAME}' record, got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = matching[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(getattr(record, "file_path"), str(self.tmppath))
        self.assertEqual(getattr(record, "exception_type"), "JSONDecodeError")
        self.assertIn("Expecting", getattr(record, "exception_message"))
        self.assertEqual(getattr(record, "fallback_returned"), "empty_dict")

    def test_load_policy_memory_missing_file_emits_no_warning(self):
        """When the file does not exist the function takes the early
        `if not os.path.exists` return — that path was already
        legitimate first-run behavior and must remain SILENT (no
        warning), otherwise every cold start would flag."""
        missing = Path(self.tmpdir.name) / "does_not_exist.json"
        self.assertFalse(missing.exists())

        result = self._call_with_path(missing)

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("topics"), {})
        self.assertEqual(result.get("articles"), [])

        matching = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(matching), 0,
            "Missing-file path must remain a silent first-run signal — "
            "warning should fire on PARSE failure, not on absence.",
        )

    def test_load_policy_memory_happy_path_emits_no_warning(self):
        """Valid JSON returns the parsed dict and emits NO warning."""
        payload = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_updated_at": "2026-05-01T12:00:00+00:00",
            "topics": {"finance": {"events": []}},
            "articles": [{"article_id": "abc"}],
        }
        self.tmppath.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        result = self._call_with_path(self.tmppath)

        self.assertEqual(result["topics"], payload["topics"])
        self.assertEqual(result["articles"], payload["articles"])

        matching = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(matching), 0,
            "Happy path must not emit the corrupt/missing warning.",
        )


# ---------------------------------------------------------------------------
# Site 2 — official_crawler outer wrapper
# ---------------------------------------------------------------------------


class OfficialCrawlerOuterWrapperWarningTests(unittest.TestCase):
    """When `fetch_best_official_document`'s outer except triggers,
    the function must emit `official_crawler.outer_wrapper_failure`
    at WARNING with source_name / site_key / URL / exception fields,
    and must STILL return the pre-built fallback `result` dict
    untouched in shape."""

    LOGGER_NAME = "official_crawler"
    EVENT_NAME = "official_crawler.outer_wrapper_failure"

    def setUp(self):
        self.handler = _attach_capturing_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def test_outer_wrapper_emits_warning_on_failure(self):
        """Force the try-block to raise by mocking an early-call
        helper (`_request_url`) to throw a RuntimeError. The except
        path should log the warning AND return the fallback result
        dict — control flow unchanged."""
        import official_crawler

        with mock.patch.object(
            official_crawler,
            "_request_url",
            side_effect=RuntimeError("simulated crawler explosion"),
        ):
            result = official_crawler.fetch_best_official_document(
                {
                    "source_name": "Financial Supervisory Service",
                    "source_type": "financial_regulator",
                    "search_query": "테스트 검색어",
                    "official_search_url": "https://www.fss.or.kr/search?q=test",
                }
            )

        # --- Behavioral pin: return shape is the fallback result dict.
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("usable"))
        self.assertFalse(result.get("weakly_usable"))
        self.assertFalse(result.get("fetched"))
        self.assertEqual(result.get("text_snippet"), "")
        self.assertIsNone(result.get("title"))
        self.assertIn("simulated crawler explosion", result.get("error") or "")
        attempts = result.get("search_attempt_results") or []
        self.assertGreaterEqual(len(attempts), 1)
        self.assertIn("simulated crawler explosion", attempts[0].get("error") or "")

        # --- Observability pin: exactly one structured warning emitted.
        matching = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(matching), 1,
            f"Expected exactly one '{self.EVENT_NAME}' record, got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = matching[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(
            getattr(record, "source_name"),
            "Financial Supervisory Service",
        )
        self.assertEqual(getattr(record, "exception_type"), "RuntimeError")
        self.assertIn(
            "simulated crawler explosion",
            getattr(record, "exception_message"),
        )
        self.assertEqual(
            getattr(record, "fallback_returned"),
            "unusable_result_dict",
        )

    def test_outer_wrapper_no_warning_when_no_search_url(self):
        """The function's early return path for missing search URLs
        (before the try-block) must NOT emit the warning — it's not
        a crawler failure, it's a degenerate input.

        Reaching that early return requires a candidate with no
        search_query AND no fallback URL fields, so that
        `_build_search_attempts` produces an attempt with `url=None`
        and the `if not search_url:` guard fires."""
        import official_crawler

        result = official_crawler.fetch_best_official_document({})

        self.assertIsInstance(result, dict)
        self.assertEqual(
            result.get("error"),
            "No official search URL found for candidate.",
        )

        matching = _records_with_event(self.handler.records, self.EVENT_NAME)
        self.assertEqual(
            len(matching), 0,
            "Early-return on missing search URL must not flag as a "
            "crawler outer-wrapper failure — it's degenerate input, "
            "not an exception.",
        )


if __name__ == "__main__":
    unittest.main()
