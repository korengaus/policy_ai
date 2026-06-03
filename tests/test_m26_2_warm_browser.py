"""M26.2 — persistent warm Chromium via a dedicated render thread.

Mock-driven: a fake ``playwright.sync_api`` module is injected into
``sys.modules`` so NO live Chromium is launched and NO network is required.
The fake supports both shapes the code uses:

  * cold path  — ``with sync_playwright() as pw: pw.chromium.launch()``
  * warm path  — ``sync_playwright().start()`` (persistent) then reuse

Covers: gate-off cold (holder never created), gate-on warm parity with cold,
single-worker executor (LESSON 1), launch-failure fallback to cold, fork
PID-guard recreation, disconnect relaunch, and per-render context/page
finally-close (2GB leak guard).
"""

import os
import sys
import types
import unittest
from unittest import mock


# --------------------------------------------------------------------------
# Fake Playwright surface (shared recorder so tests can flip flags mid-run).
# --------------------------------------------------------------------------


class _FakePWError(Exception):
    pass


class _FakePWTimeout(_FakePWError):
    pass


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status


class _FakeLocator:
    def inner_text(self, timeout=None):
        return "FAKE BODY TEXT"


class _FakePage:
    def __init__(self, rec):
        self._rec = rec

    def goto(self, url, wait_until=None, timeout=None):
        self._rec["goto_calls"].append(url)
        err = self._rec.get("goto_error")
        if err is not None:
            raise err
        return _FakeResponse(200)

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "FAKE TITLE"

    def content(self):
        return "<html><body>FAKE CONTENT</body></html>"

    def locator(self, selector):
        return _FakeLocator()

    def evaluate(self, js):
        return [{"href": "https://www.fsc.go.kr/no010101/100", "text": "보도자료 1"}]

    def close(self):
        self._rec["page_closed"] += 1


class _FakeContext:
    def __init__(self, rec):
        self._rec = rec

    def new_page(self):
        page = _FakePage(self._rec)
        self._rec["pages"].append(page)
        return page

    def close(self):
        self._rec["context_closed"] += 1


class _FakeChromium:
    def __init__(self, rec):
        self._rec = rec

    def launch(self, headless=True):
        self._rec["launch_calls"] += 1
        return _FakeBrowser(self._rec)


class _FakeBrowser:
    def __init__(self, rec):
        self._rec = rec

    def new_context(self, **kwargs):
        self._rec["context_kwargs"].append(kwargs)
        ctx = _FakeContext(self._rec)
        self._rec["contexts"].append(ctx)
        return ctx

    def is_connected(self):
        return self._rec.get("connected", True)

    def close(self):
        self._rec["browser_closed"] += 1


class _FakePlaywright:
    def __init__(self, rec):
        self._rec = rec
        self.chromium = _FakeChromium(rec)

    # cold path: `with sync_playwright() as pw`
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # warm path: sync_playwright().start()
    def start(self):
        self._rec["start_calls"] += 1
        return self

    def stop(self):
        self._rec["stop_calls"] += 1


def _new_recorder():
    return {
        "launch_calls": 0,
        "start_calls": 0,
        "stop_calls": 0,
        "browser_closed": 0,
        "context_closed": 0,
        "page_closed": 0,
        "goto_calls": [],
        "pages": [],
        "contexts": [],
        "context_kwargs": [],
        "connected": True,
        "goto_error": None,
    }


class WarmBrowserTests(unittest.TestCase):
    def setUp(self):
        # Inject a fake playwright module so neither cold nor warm needs a
        # real browser. One shared recorder per test.
        self.rec = _new_recorder()
        fake_mod = types.ModuleType("playwright.sync_api")
        fake_mod.sync_playwright = lambda: _FakePlaywright(self.rec)
        fake_mod.Error = _FakePWError
        fake_mod.TimeoutError = _FakePWTimeout
        self._saved_modules = {
            "playwright": sys.modules.get("playwright"),
            "playwright.sync_api": sys.modules.get("playwright.sync_api"),
        }
        sys.modules["playwright"] = types.ModuleType("playwright")
        sys.modules["playwright.sync_api"] = fake_mod

        import official_browser_crawler as obc

        self.obc = obc
        # Fresh holder per test (the module singleton would leak state).
        self._saved_holder = obc._WARM_BROWSER
        obc._WARM_BROWSER = obc._WarmBrowserHolder()

    def tearDown(self):
        try:
            self.obc._WARM_BROWSER.shutdown()
        except Exception:
            pass
        self.obc._WARM_BROWSER = self._saved_holder
        for name, mod in self._saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    # ---- gate off ----

    def test_gate_off_uses_cold_and_holder_never_created(self):
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "false"}):
            result = self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        self.assertTrue(result["rendered"])
        self.assertEqual(result["title"], "FAKE TITLE")
        # Warm holder must not have been created/used.
        self.assertIsNone(self.obc._WARM_BROWSER._executor)
        self.assertEqual(self.rec["start_calls"], 0)  # warm-only persistent start

    # ---- parity ----

    def test_gate_on_warm_parity_with_cold(self):
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "false"}):
            cold = self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        # Fresh recorder + holder for the warm run.
        self.rec = _new_recorder()
        sys.modules["playwright.sync_api"].sync_playwright = lambda: _FakePlaywright(self.rec)
        self.obc._WARM_BROWSER = self.obc._WarmBrowserHolder()
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "true"}):
            warm = self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        self.assertEqual(warm, cold)
        self.assertGreaterEqual(self.rec["start_calls"], 1)  # persistent driver started

    # ---- single render thread (LESSON 1) ----

    def test_render_executor_is_single_worker(self):
        self.assertEqual(self.obc._RENDER_EXECUTOR_MAX_WORKERS, 1)
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "true"}):
            self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        executor = self.obc._WARM_BROWSER._executor
        self.assertIsNotNone(executor)
        self.assertEqual(executor._max_workers, 1)

    def test_max_parallel_playwright_default_is_one(self):
        # LESSON 1 footgun fix: in-code default hardened to 1.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_PARALLEL_PLAYWRIGHT", None)
            self.assertEqual(self.obc._max_parallel_playwright(), 1)

    # ---- failure fallback ----

    def test_warm_launch_failure_falls_back_to_cold(self):
        sentinel = {"rendered": "COLD_SENTINEL"}
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "true"}), \
                mock.patch.object(self.obc, "_start_playwright", side_effect=RuntimeError("no chromium")), \
                mock.patch.object(self.obc, "_fetch_rendered_page_cold", return_value=sentinel) as cold_mock:
            result = self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        self.assertEqual(result, sentinel)
        cold_mock.assert_called_once()

    # ---- fork PID guard ----

    def test_pid_guard_recreates_executor(self):
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "true"}):
            self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
            e1 = self.obc._WARM_BROWSER._executor
            self.assertIsNotNone(e1)
            # Simulate a fork: stale creator PID forces a reset on next render.
            self.obc._WARM_BROWSER._creator_pid = -1
            self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
            e2 = self.obc._WARM_BROWSER._executor
        self.assertIsNotNone(e2)
        self.assertIsNot(e2, e1)

    # ---- disconnect relaunch ----

    def test_disconnect_triggers_single_relaunch(self):
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "true"}):
            self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
            self.assertEqual(self.rec["launch_calls"], 1)
            # Browser reports disconnected -> next render must relaunch once.
            self.rec["connected"] = False
            self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        self.assertEqual(self.rec["launch_calls"], 2)
        self.assertGreaterEqual(self.rec["browser_closed"], 1)

    # ---- leak guard ----

    def test_context_and_page_closed_even_when_goto_raises(self):
        self.rec["goto_error"] = _FakePWTimeout("navigation timeout")
        with mock.patch.dict(os.environ, {"WARM_BROWSER_ENABLED": "true"}):
            result = self.obc.fetch_rendered_page("https://www.fsc.go.kr/no010101")
        self.assertFalse(result["rendered"])
        self.assertTrue(result["error"])
        self.assertGreaterEqual(self.rec["page_closed"], 1)
        self.assertGreaterEqual(self.rec["context_closed"], 1)


if __name__ == "__main__":
    unittest.main()
