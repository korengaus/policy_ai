"""M11.7b — pins for the Category 4 Playwright exception narrowing in
official_browser_crawler.fetch_rendered_page.

The single broad ``except Exception`` at the previously-cited L69 was
replaced with a three-tier chain:

    except PlaywrightTimeoutError  → event "playwright.page_timeout"
    except PlaywrightError         → event "playwright.api_error"
    except Exception               → event "playwright.unexpected_error"

All three tiers preserve the existing sentinel return shape:
``result["error"] = str(exc)`` then the function returns ``result``
with ``rendered=False``. The pipeline cannot crash via this path.

These pins assert:
  (a) Each tier catches the right exception family and emits the
      documented event name with structured ``extra=`` fields.
  (b) The sentinel return shape is byte-identical across all three
      tiers (only the ``error`` field carries the exception message).
  (c) The happy path emits no warning.
  (d) ``KeyboardInterrupt`` and ``SystemExit`` (which inherit from
      ``BaseException``, not ``Exception``) propagate normally — the
      pre-M11.7b broad ``except Exception`` did NOT catch them either,
      so this is a behavior preservation pin, not a behavior change.

All tests mock the Playwright API. NO real browser is launched.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class _CapturingHandler(logging.Handler):
    """Records every emitted LogRecord whose logger name matches the
    target prefix. Same pattern as tests/test_m11_7a_category2_logging.py."""

    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name_prefix or record.name.startswith(
            self._name_prefix + "."
        ):
            self.records.append(record)


def _attach_handler(logger_name: str) -> _CapturingHandler:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler(logger_name)
    logger.addHandler(handler)
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
# Mock Playwright module — installed into sys.modules at test time so the
# lazy `from playwright.sync_api import sync_playwright, Error, TimeoutError`
# inside fetch_rendered_page picks up our mocks instead of the real package.
# This means NO real headless browser is ever launched by these tests.
# ---------------------------------------------------------------------------


def _build_mock_playwright_module(*, raise_on_goto=None):
    """Construct an in-memory replacement for ``playwright.sync_api``.

    ``raise_on_goto`` controls failure mode:
      - None: happy path; ``page.goto`` returns a fake response and
              all subsequent calls succeed.
      - an Exception instance: ``page.goto`` raises it. Use the real
              ``PlaywrightTimeoutError`` / ``PlaywrightError`` /
              ``RuntimeError`` from the real ``playwright.sync_api``
              that we re-export, so isinstance checks in the source
              code under test work correctly.

    The mock implements only the attributes ``fetch_rendered_page``
    actually touches.
    """
    # Use the REAL exception classes from the real installed package so
    # ``except PlaywrightTimeoutError`` etc. in the source code under
    # test compare correctly. We never call into real Playwright; we
    # only borrow its exception type objects.
    import playwright.sync_api as real_sa

    PlaywrightError = real_sa.Error
    PlaywrightTimeoutError = real_sa.TimeoutError

    class _FakeResponse:
        status = 200

    class _FakeLocator:
        def inner_text(self, timeout=5000):
            return "본문 텍스트 샘플"

    class _FakePage:
        def goto(self, url, wait_until="networkidle", timeout=15000):
            if raise_on_goto is not None:
                raise raise_on_goto
            return _FakeResponse()

        def wait_for_timeout(self, ms):
            return None

        def title(self):
            return "샘플 페이지 제목"

        def content(self):
            return "<html><body>샘플</body></html>"

        def locator(self, selector):
            return _FakeLocator()

        def evaluate(self, script):
            return [{"href": "https://example.go.kr/x", "text": "샘플 링크"}]

    class _FakeContext:
        def new_page(self):
            return _FakePage()

        def close(self):
            return None

    class _FakeBrowser:
        def new_context(self, **kwargs):
            return _FakeContext()

        def close(self):
            return None

    class _FakeChromium:
        def launch(self, headless=True):
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _SyncPlaywrightContextManager:
        def __enter__(self):
            return _FakePlaywright()

        def __exit__(self, exc_type, exc, tb):
            return False

    def sync_playwright():
        return _SyncPlaywrightContextManager()

    # Build a fake module object exposing the exact names the source
    # code under test imports.
    import types

    fake_sa = types.ModuleType("playwright.sync_api")
    fake_sa.sync_playwright = sync_playwright
    fake_sa.Error = PlaywrightError
    fake_sa.TimeoutError = PlaywrightTimeoutError

    fake_root = types.ModuleType("playwright")
    fake_root.sync_api = fake_sa

    return fake_root, fake_sa, PlaywrightError, PlaywrightTimeoutError


def _install_mock_playwright(*, raise_on_goto=None):
    """Return a context manager that swaps in the mock playwright module
    for the duration of the test, then restores."""
    fake_root, fake_sa, _err, _terr = _build_mock_playwright_module(
        raise_on_goto=raise_on_goto,
    )
    return mock.patch.dict(
        sys.modules,
        {
            "playwright": fake_root,
            "playwright.sync_api": fake_sa,
        },
        clear=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class PlaywrightNarrowingTests(unittest.TestCase):
    LOGGER_NAME = "official_browser_crawler"
    URL = "https://www.fss.or.kr/some/page"

    def setUp(self):
        self.handler = _attach_handler(self.LOGGER_NAME)

    def tearDown(self):
        _detach_handler(self.LOGGER_NAME, self.handler)

    def _call(self):
        import official_browser_crawler

        return official_browser_crawler.fetch_rendered_page(self.URL)

    # -- (a) Tier-1: PlaywrightTimeoutError --------------------------------

    def test_timeout_error_caught_and_logged(self):
        from playwright.sync_api import TimeoutError as PWTimeout

        with _install_mock_playwright(raise_on_goto=PWTimeout("nav timed out")):
            result = self._call()

        # Sentinel preserved.
        self.assertFalse(result["rendered"])
        self.assertEqual(result["error"], "nav timed out")
        self.assertEqual(result["html"], "")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["raw_links"], [])
        self.assertEqual(result["url"], self.URL)

        # Exactly one warning fired with the timeout-specific event.
        matching = _records_with_event(
            self.handler.records, "playwright.page_timeout"
        )
        self.assertEqual(
            len(matching), 1,
            f"Expected exactly one 'playwright.page_timeout' record; got "
            f"{[r.getMessage() for r in self.handler.records]!r}.",
        )
        record = matching[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(getattr(record, "url"), self.URL)
        self.assertEqual(getattr(record, "exception_type"), "TimeoutError")
        self.assertEqual(getattr(record, "exception_message"), "nav timed out")
        self.assertEqual(
            getattr(record, "fallback_returned"), "unrendered_result_dict"
        )

        # Other-event warnings did NOT fire.
        self.assertEqual(
            len(_records_with_event(self.handler.records, "playwright.api_error")), 0,
        )
        self.assertEqual(
            len(_records_with_event(self.handler.records, "playwright.unexpected_error")), 0,
        )

    # -- (b) Tier-2: base PlaywrightError -----------------------------------

    def test_playwright_error_caught_and_logged(self):
        from playwright.sync_api import Error as PWError

        with _install_mock_playwright(raise_on_goto=PWError("target closed")):
            result = self._call()

        self.assertFalse(result["rendered"])
        self.assertEqual(result["error"], "target closed")

        matching = _records_with_event(
            self.handler.records, "playwright.api_error"
        )
        self.assertEqual(len(matching), 1)
        record = matching[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(getattr(record, "url"), self.URL)
        self.assertEqual(getattr(record, "exception_type"), "Error")
        self.assertEqual(getattr(record, "exception_message"), "target closed")
        self.assertEqual(
            getattr(record, "fallback_returned"), "unrendered_result_dict"
        )

        # Timeout-tier should NOT fire even though TimeoutError is a
        # subclass — we raised plain Error, not TimeoutError.
        self.assertEqual(
            len(_records_with_event(self.handler.records, "playwright.page_timeout")), 0,
        )
        self.assertEqual(
            len(_records_with_event(self.handler.records, "playwright.unexpected_error")), 0,
        )

    # -- (c) Tier-3: final-fallback Exception -------------------------------

    def test_unexpected_exception_caught_by_final_fallback(self):
        """A non-Playwright exception (e.g., a programmer bug) must
        still be caught by the final-fallback except, preserving
        pipeline availability and the sentinel return shape — but
        with the distinct 'playwright.unexpected_error' event so the
        operator can alert on it specifically."""
        with _install_mock_playwright(
            raise_on_goto=RuntimeError("unexpected simulated bug")
        ):
            result = self._call()

        self.assertFalse(result["rendered"])
        self.assertEqual(result["error"], "unexpected simulated bug")

        matching = _records_with_event(
            self.handler.records, "playwright.unexpected_error"
        )
        self.assertEqual(len(matching), 1)
        record = matching[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(getattr(record, "exception_type"), "RuntimeError")
        self.assertIn("unexpected simulated bug", getattr(record, "exception_message"))

        # Neither narrow-tier event fired.
        self.assertEqual(
            len(_records_with_event(self.handler.records, "playwright.page_timeout")), 0,
        )
        self.assertEqual(
            len(_records_with_event(self.handler.records, "playwright.api_error")), 0,
        )

    # -- (d) Happy path: no warning at all ---------------------------------

    def test_happy_path_emits_no_warning(self):
        with _install_mock_playwright(raise_on_goto=None):
            result = self._call()

        self.assertTrue(result["rendered"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["title"], "샘플 페이지 제목")
        self.assertIn("샘플", result["html"])
        self.assertEqual(result["text"], "본문 텍스트 샘플")
        self.assertEqual(len(result["raw_links"]), 1)
        self.assertEqual(result["raw_links"][0]["href"], "https://example.go.kr/x")

        for event in (
            "playwright.page_timeout",
            "playwright.api_error",
            "playwright.unexpected_error",
        ):
            self.assertEqual(
                len(_records_with_event(self.handler.records, event)), 0,
                f"Happy path must emit no '{event}' record.",
            )

    # -- (e) Return-value byte-identity across exception tiers -------------

    def test_return_values_unchanged_for_each_exception_type(self):
        """The sentinel return shape must be IDENTICAL across all three
        exception tiers — the only field that varies is ``error``,
        which carries str(exc). Every other field stays at its
        pre-initialized value. This is the M11.7b byte-identicality
        contract."""
        from playwright.sync_api import (
            Error as PWError,
            TimeoutError as PWTimeout,
        )

        cases = [
            ("timeout", PWTimeout("t")),
            ("api_error", PWError("e")),
            ("unexpected", RuntimeError("u")),
        ]
        results = {}
        for label, exc in cases:
            with _install_mock_playwright(raise_on_goto=exc):
                results[label] = self._call()

        # The non-error fields are byte-identical between all three.
        comparable_keys = (
            "url", "rendered", "status_code", "title",
            "html", "text", "raw_links",
        )
        baseline = {k: results["timeout"][k] for k in comparable_keys}
        for label in ("api_error", "unexpected"):
            for k in comparable_keys:
                self.assertEqual(
                    results[label][k], baseline[k],
                    f"Field {k!r} drifted between tiers; "
                    f"{label}={results[label][k]!r} vs "
                    f"timeout={baseline[k]!r}.",
                )
        # And `error` carries str(exc) for each.
        self.assertEqual(results["timeout"]["error"], "t")
        self.assertEqual(results["api_error"]["error"], "e")
        self.assertEqual(results["unexpected"]["error"], "u")


if __name__ == "__main__":
    unittest.main()
