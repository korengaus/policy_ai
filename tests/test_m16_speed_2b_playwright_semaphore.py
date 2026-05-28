"""M16-speed-2b — pins for the bounded Playwright semaphore.

M16-speed-2a wrapped ``fetch_rendered_page`` with a module-level
``threading.Lock`` so the 512MB Starter tier could not OOM under concurrent
Chromium browsers. The Worker is now Standard (2GB); M16-speed-2b replaces
that ``Lock`` with a bounded ``threading.Semaphore`` whose capacity is set
by the ``MAX_PARALLEL_PLAYWRIGHT`` env var (default 3, clamped >= 1).

These pins assert:

* ``_max_parallel_playwright()`` env-helper contract: default, invalid env,
  zero-clamp, respects-env.
* ``Semaphore(1)`` is behaviourally equivalent to the previous ``Lock`` —
  two concurrent ``fetch_rendered_page`` calls execute strictly
  sequentially (no overlap; second start AFTER first end).
* ``Semaphore(3)`` allows real concurrency — three concurrent
  ``fetch_rendered_page`` calls overlap (at least 2 starts before the
  first end).

Test approach for the concurrency tests:
* Playwright is mocked at ``sys.modules`` (same pattern as
  ``tests/test_m11_7b_playwright_narrowing.py``). The mock's
  ``page.goto`` sleeps for a controllable delay so concurrent calls
  produce observable overlap.
* The semaphore is constructed from the env var at MODULE IMPORT time.
  To exercise different capacities without re-importing, we replace
  ``official_browser_crawler._PLAYWRIGHT_SEMAPHORE`` directly with a
  fresh ``Semaphore(N)`` inside the test (direct-patch approach). The
  original is restored in ``tearDown``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import official_browser_crawler  # noqa: E402


# ---------------------------------------------------------------------------
# Mock Playwright module — delay-stub variant. Records (event, thread_id,
# monotonic_ts) so the test can verify whether concurrent calls overlap.
# ---------------------------------------------------------------------------


def _build_mock_playwright_module(*, delay_seconds: float, event_log: list):
    """Return (fake_root_module, fake_sa_module). The mock's ``page.goto``
    appends a ``("goto_start", tid, ts)`` event, sleeps for
    ``delay_seconds``, then appends a ``("goto_end", tid, ts)`` event.
    This bracket sits INSIDE the semaphore-guarded region of
    ``fetch_rendered_page`` so the recorded timings reflect when the
    semaphore permit was held.

    Uses the REAL ``playwright.sync_api`` exception classes so the
    ``except PlaywrightTimeoutError`` / ``except PlaywrightError`` arms
    in ``fetch_rendered_page`` still type-match (irrelevant for the
    happy path but cheap to keep symmetric with M11.7b).
    """
    import playwright.sync_api as real_sa

    PlaywrightError = real_sa.Error
    PlaywrightTimeoutError = real_sa.TimeoutError

    class _FakeResponse:
        status = 200

    class _FakeLocator:
        def inner_text(self, timeout=5000):
            return "본문"

    class _FakePage:
        def goto(self, url, wait_until="networkidle", timeout=15000):
            tid = threading.get_ident()
            event_log.append(("goto_start", tid, time.monotonic()))
            time.sleep(delay_seconds)
            event_log.append(("goto_end", tid, time.monotonic()))
            return _FakeResponse()

        def wait_for_timeout(self, ms):
            return None

        def title(self):
            return "샘플"

        def content(self):
            return "<html><body>샘플</body></html>"

        def locator(self, selector):
            return _FakeLocator()

        def evaluate(self, script):
            return []

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

    fake_sa = types.ModuleType("playwright.sync_api")
    fake_sa.sync_playwright = sync_playwright
    fake_sa.Error = PlaywrightError
    fake_sa.TimeoutError = PlaywrightTimeoutError

    fake_root = types.ModuleType("playwright")
    fake_root.sync_api = fake_sa

    return fake_root, fake_sa


def _install_mock_playwright(*, delay_seconds: float, event_log: list):
    fake_root, fake_sa = _build_mock_playwright_module(
        delay_seconds=delay_seconds, event_log=event_log,
    )
    return mock.patch.dict(
        sys.modules,
        {"playwright": fake_root, "playwright.sync_api": fake_sa},
        clear=False,
    )


# ---------------------------------------------------------------------------
# 1. Env helper — default, invalid, zero-clamp, respects-env
# ---------------------------------------------------------------------------


class MaxParallelPlaywrightHelperTests(unittest.TestCase):
    def test_max_parallel_playwright_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_PARALLEL_PLAYWRIGHT", None)
            self.assertEqual(official_browser_crawler._max_parallel_playwright(), 3)

    def test_max_parallel_playwright_invalid_env(self):
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_PLAYWRIGHT": "garbage"}, clear=False,
        ):
            self.assertEqual(official_browser_crawler._max_parallel_playwright(), 3)

    def test_max_parallel_playwright_zero_clamped(self):
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_PLAYWRIGHT": "0"}, clear=False,
        ):
            self.assertEqual(official_browser_crawler._max_parallel_playwright(), 1)

    def test_max_parallel_playwright_respects_env(self):
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_PLAYWRIGHT": "2"}, clear=False,
        ):
            self.assertEqual(official_browser_crawler._max_parallel_playwright(), 2)


# ---------------------------------------------------------------------------
# 2. Concurrency behaviour — Semaphore(1) serializes, Semaphore(3) overlaps
# ---------------------------------------------------------------------------


class SemaphoreConcurrencyTests(unittest.TestCase):
    URL = "https://www.fss.or.kr/some/page"

    def setUp(self):
        # Save the production semaphore so tearDown can restore it.
        self._original_semaphore = official_browser_crawler._PLAYWRIGHT_SEMAPHORE

    def tearDown(self):
        official_browser_crawler._PLAYWRIGHT_SEMAPHORE = self._original_semaphore

    def _run_concurrent_fetches(self, n_threads: int, delay_seconds: float) -> list:
        event_log: list = []

        def _worker():
            official_browser_crawler.fetch_rendered_page(self.URL)

        with _install_mock_playwright(
            delay_seconds=delay_seconds, event_log=event_log,
        ):
            threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        return event_log

    def test_semaphore_1_serializes(self):
        """Semaphore(1) reproduces the pre-M16-speed-2b Lock behaviour.

        Two concurrent ``fetch_rendered_page`` calls must execute strictly
        sequentially — the second ``goto_start`` must come AFTER the first
        ``goto_end`` in event-log order. Equivalent: no overlap window.
        """
        # Direct-patch the semaphore to capacity 1.
        official_browser_crawler._PLAYWRIGHT_SEMAPHORE = threading.Semaphore(1)

        event_log = self._run_concurrent_fetches(n_threads=2, delay_seconds=0.10)

        starts = [e for e in event_log if e[0] == "goto_start"]
        ends = [e for e in event_log if e[0] == "goto_end"]
        self.assertEqual(len(starts), 2, f"expected 2 starts, got {event_log!r}")
        self.assertEqual(len(ends), 2, f"expected 2 ends, got {event_log!r}")

        # Order check: in event-log order, the events must be
        # [start, end, start, end] — no [start, start, ...] pattern.
        kinds = [e[0] for e in event_log]
        self.assertEqual(
            kinds, ["goto_start", "goto_end", "goto_start", "goto_end"],
            f"Semaphore(1) must serialize; got event order {kinds!r}",
        )

        # Two distinct threads ran (i.e. the test really spawned two).
        thread_ids = {e[1] for e in event_log}
        self.assertEqual(len(thread_ids), 2)

    def test_semaphore_3_allows_concurrency(self):
        """Semaphore(3) admits three concurrent ``fetch_rendered_page``
        invocations — at least two ``goto_start`` events must occur
        BEFORE the first ``goto_end``. This is the operational
        contract the M16-speed-2b speedup depends on.
        """
        official_browser_crawler._PLAYWRIGHT_SEMAPHORE = threading.Semaphore(3)

        event_log = self._run_concurrent_fetches(n_threads=3, delay_seconds=0.20)

        starts = [e for e in event_log if e[0] == "goto_start"]
        ends = [e for e in event_log if e[0] == "goto_end"]
        self.assertEqual(len(starts), 3, f"expected 3 starts, got {event_log!r}")
        self.assertEqual(len(ends), 3, f"expected 3 ends, got {event_log!r}")

        # Find index of the first goto_end in event_log; count
        # goto_start events BEFORE it.
        first_end_index = next(
            i for i, e in enumerate(event_log) if e[0] == "goto_end"
        )
        starts_before_first_end = sum(
            1 for e in event_log[:first_end_index] if e[0] == "goto_start"
        )
        self.assertGreaterEqual(
            starts_before_first_end, 2,
            "Semaphore(3) must allow at least 2 concurrent Playwright "
            "calls (>=2 starts before first end); "
            f"saw {starts_before_first_end} starts before first end. "
            f"Event log: {event_log!r}",
        )

        # All three threads were distinct.
        thread_ids = {e[1] for e in event_log}
        self.assertEqual(len(thread_ids), 3)


if __name__ == "__main__":
    unittest.main()
