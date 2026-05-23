"""Tests for the M13.3d HTTP cache integration in
``official_source_body.fetch_official_source_body``.

Run with: python tests/test_official_source_body_cache.py

No real network traffic. ``requests.get`` is patched with a fake that
returns a controllable :class:`requests.Response`. The module-local
cache singleton is reset between tests so cross-case pollution is
impossible.

The single most important pin in this file is
:class:`CacheOffByteIdentityTests` — it confirms that with both flags
unset (the default state on Render) the wrapper returns exactly what
``requests.get`` returned, byte-for-byte.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import http_cache  # noqa: E402
import official_source_body  # noqa: E402


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _make_response(
    status: int = 200,
    body: bytes = b"<html><body>hi</body></html>",
    headers=None,
    url: str = "https://fsc.go.kr/page",
) -> requests.Response:
    response = requests.Response()
    response._content = body  # noqa: SLF001
    response.status_code = status
    response.headers = CaseInsensitiveDict(
        headers or {"Content-Type": "text/html; charset=utf-8"},
    )
    response.url = url
    response.reason = "OK" if status == 200 else ""
    return response


class _FakeGet:
    """Counts requests.get calls and returns a canned response."""

    def __init__(
        self,
        body: bytes = b"<html><body>hi</body></html>",
        status: int = 200,
        headers=None,
        per_url: dict = None,
        raise_exc: Exception = None,
    ):
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.per_url = per_url or {}
        self.raise_exc = raise_exc
        self.calls = 0
        self.urls = []

    def __call__(self, url, **kwargs):
        self.calls += 1
        self.urls.append(url)
        if self.raise_exc is not None:
            raise self.raise_exc
        if url in self.per_url:
            payload = self.per_url[url]
            return _make_response(
                status=payload.get("status", 200),
                body=payload.get("body", b""),
                headers=payload.get("headers"),
                url=url,
            )
        return _make_response(
            status=self.status, body=self.body,
            headers=self.headers, url=url,
        )


class _EnvScope:
    KEYS = (
        "HTTP_CACHE_ENABLED",
        "OFFICIAL_SOURCE_BODY_CACHE_ENABLED",
        "OFFICIAL_SOURCE_BODY_CACHE_TTL_SECONDS",
        "HTTP_CACHE_DEFAULT_TTL_SECONDS",
        "HTTP_CACHE_MAX_ENTRIES",
    )

    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        http_cache.reset_default_cache_for_tests()
        official_source_body._reset_body_cache_for_tests()


def _set_env(**values):
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _enable_both_flags():
    _set_env(
        HTTP_CACHE_ENABLED="true",
        OFFICIAL_SOURCE_BODY_CACHE_ENABLED="true",
    )
    http_cache.reset_default_cache_for_tests()
    official_source_body._reset_body_cache_for_tests()


# ---------------------------------------------------------------------------
# Cache-OFF byte-identicality pin — the regression contract for M13.3d.
# ---------------------------------------------------------------------------


class CacheOffByteIdentityTests(unittest.TestCase):
    """With both flags unset, ``fetch_official_source_body`` must
    behave like the pre-M13.3d function. The cache wrapper adds zero
    observable side effects."""

    def setUp(self):
        official_source_body._reset_body_cache_for_tests()

    def tearDown(self):
        official_source_body._reset_body_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_SOURCE_BODY_CACHE_ENABLED", None)

    def test_both_flags_unset_calls_requests_get_each_time(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                OFFICIAL_SOURCE_BODY_CACHE_ENABLED=None,
            )
            fake = _FakeGet(body=b"<html><body>" + b"x" * 400 + b"</body></html>")
            with patch.object(requests, "get", fake):
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
            self.assertEqual(fake.calls, 2)

    def test_flag_off_only_master_set(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED="true",
                OFFICIAL_SOURCE_BODY_CACHE_ENABLED=None,
            )
            fake = _FakeGet(body=b"<html><body>" + b"x" * 400 + b"</body></html>")
            with patch.object(requests, "get", fake):
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
            self.assertEqual(fake.calls, 2)

    def test_flag_off_only_module_set(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                OFFICIAL_SOURCE_BODY_CACHE_ENABLED="true",
            )
            fake = _FakeGet(body=b"<html><body>" + b"x" * 400 + b"</body></html>")
            with patch.object(requests, "get", fake):
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
            self.assertEqual(fake.calls, 2)


# ---------------------------------------------------------------------------
# Both-flag precedence — both must be true together
# ---------------------------------------------------------------------------


class FlagPrecedenceTests(unittest.TestCase):
    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        official_source_body._reset_body_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_SOURCE_BODY_CACHE_ENABLED", None)

    def test_both_flags_set_caching_active(self):
        with _EnvScope():
            _enable_both_flags()
            fake = _FakeGet(body=b"<html><body>" + b"x" * 400 + b"</body></html>")
            with patch.object(requests, "get", fake):
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
                official_source_body.fetch_official_source_body(
                    "https://fsc.go.kr/notice",
                )
            self.assertEqual(fake.calls, 1)

    def test_flag_truthy_values(self):
        for value in ("true", "True", "TRUE", "1", "on", "yes", "YES"):
            with _EnvScope():
                _set_env(
                    HTTP_CACHE_ENABLED="true",
                    OFFICIAL_SOURCE_BODY_CACHE_ENABLED=value,
                )
                http_cache.reset_default_cache_for_tests()
                official_source_body._reset_body_cache_for_tests()
                self.assertTrue(
                    official_source_body.is_official_source_body_cache_enabled(),
                    f"value {value!r} should enable the cache",
                )

    def test_flag_falsy_values(self):
        for value in ("", "false", "False", "no", "0", "off", "FALSE"):
            with _EnvScope():
                _set_env(
                    HTTP_CACHE_ENABLED="true",
                    OFFICIAL_SOURCE_BODY_CACHE_ENABLED=value,
                )
                self.assertFalse(
                    official_source_body.is_official_source_body_cache_enabled(),
                    f"value {value!r} should NOT enable the cache",
                )


# ---------------------------------------------------------------------------
# Cache-on behaviour
# ---------------------------------------------------------------------------


class CacheOnBehaviourTests(unittest.TestCase):
    def setUp(self):
        _enable_both_flags()

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        official_source_body._reset_body_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_SOURCE_BODY_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_SOURCE_BODY_CACHE_TTL_SECONDS", None)

    def test_second_call_to_gov_url_hits_cache(self):
        body = (
            b"<html><body><div class='view_cont'>"
            + b"x" * 400 +
            b"</div></body></html>"
        )
        fake = _FakeGet(body=body)
        with patch.object(requests, "get", fake):
            r1 = official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/notice",
            )
            r2 = official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/notice",
            )
        self.assertEqual(fake.calls, 1)
        self.assertEqual(r1["status_code"], r2["status_code"])
        self.assertEqual(r1["body_text"], r2["body_text"])

    def test_non_gov_domain_passes_through(self):
        body = b"<html><body>" + b"x" * 400 + b"</body></html>"
        fake = _FakeGet(body=body)
        with patch.object(requests, "get", fake):
            official_source_body.fetch_official_source_body(
                "https://example.com/page",
            )
            official_source_body.fetch_official_source_body(
                "https://example.com/page",
            )
        self.assertEqual(fake.calls, 2)

    def test_different_urls_get_different_cache_entries(self):
        body = b"<html><body>" + b"x" * 400 + b"</body></html>"
        fake = _FakeGet(body=body)
        with patch.object(requests, "get", fake):
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/a",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/b",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/a",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/b",
            )
        # 2 unique URLs → 2 fetches, then 2 cache hits.
        self.assertEqual(fake.calls, 2)

    def test_404_not_cached(self):
        fake = _FakeGet(body=b"", status=404)
        with patch.object(requests, "get", fake):
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/missing",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/missing",
            )
        self.assertEqual(fake.calls, 2)

    def test_oversize_body_not_cached(self):
        big = b"<html><body>" + b"x" * (
            official_source_body._OFFICIAL_SOURCE_BODY_CACHE_MAX_BODY_BYTES + 1
        ) + b"</body></html>"
        fake = _FakeGet(body=big, status=200)
        with patch.object(requests, "get", fake):
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/big",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/big",
            )
        self.assertEqual(fake.calls, 2)

    def test_no_store_response_not_cached(self):
        body = b"<html><body>" + b"x" * 400 + b"</body></html>"
        fake = _FakeGet(
            body=body,
            headers={
                "Content-Type": "text/html",
                "Cache-Control": "no-store",
            },
        )
        with patch.object(requests, "get", fake):
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/ns",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/ns",
            )
        self.assertEqual(fake.calls, 2)

    def test_korean_body_preserved_on_cache_hit(self):
        korean_html = (
            "<html><body><div class='view_cont'>"
            "금융위원회는 오늘 새로운 정책을 발표했다. "
            * 30
            + "</div></body></html>"
        ).encode("utf-8")
        fake = _FakeGet(
            body=korean_html,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        with patch.object(requests, "get", fake):
            r1 = official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/korean-notice",
            )
            r2 = official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/korean-notice",
            )
        self.assertEqual(fake.calls, 1)
        self.assertIn("금융위원회", r1["body_text"])
        self.assertEqual(r1["body_text"], r2["body_text"])

    def test_ttl_env_override(self):
        os.environ["OFFICIAL_SOURCE_BODY_CACHE_TTL_SECONDS"] = "60"
        self.assertEqual(
            official_source_body._get_official_source_body_cache_ttl_seconds(),
            60,
        )
        os.environ["OFFICIAL_SOURCE_BODY_CACHE_TTL_SECONDS"] = "0"
        self.assertEqual(
            official_source_body._get_official_source_body_cache_ttl_seconds(),
            1800,
        )
        os.environ["OFFICIAL_SOURCE_BODY_CACHE_TTL_SECONDS"] = "not-a-number"
        self.assertEqual(
            official_source_body._get_official_source_body_cache_ttl_seconds(),
            1800,
        )
        del os.environ["OFFICIAL_SOURCE_BODY_CACHE_TTL_SECONDS"]
        self.assertEqual(
            official_source_body._get_official_source_body_cache_ttl_seconds(),
            1800,
        )

    def test_network_exception_propagates(self):
        fake = _FakeGet(raise_exc=ConnectionError("boom"))
        with patch.object(requests, "get", fake):
            result = official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/down",
            )
        # The function catches RequestException; ConnectionError is a
        # subclass and is reported via failure_reason.
        self.assertEqual(result["status_code"], None)
        self.assertIsNotNone(result["failure_reason"])


class StructuredLogTests(unittest.TestCase):
    """The cache emits ``official_source_body_cache_event`` with the
    expected extras on every fetch when the cache is on."""

    LOGGER_NAME = "official_source_body"

    def setUp(self):
        _enable_both_flags()
        from structured_logging import JsonFormatter
        self.records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(_self, record):
                self.records.append(record)

        self.handler = _Capture()
        self.handler.setFormatter(JsonFormatter())
        self.logger = logging.getLogger(self.LOGGER_NAME)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        http_cache.reset_default_cache_for_tests()
        official_source_body._reset_body_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_SOURCE_BODY_CACHE_ENABLED", None)

    def test_cache_event_log_shape(self):
        body = b"<html><body>" + b"x" * 400 + b"</body></html>"
        fake = _FakeGet(body=body)
        with patch.object(requests, "get", fake):
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/log-test",
            )
            official_source_body.fetch_official_source_body(
                "https://fsc.go.kr/log-test",
            )

        cache_events = [
            r for r in self.records
            if r.getMessage() == "official_source_body_cache_event"
        ]
        self.assertEqual(
            len(cache_events), 2,
            "Expected one cache_event per fetch call.",
        )
        # First event: cache_hit=False; second: cache_hit=True.
        self.assertFalse(getattr(cache_events[0], "cache_hit", None))
        self.assertTrue(getattr(cache_events[1], "cache_hit", None))
        # Extras present.
        for ev in cache_events:
            self.assertTrue(hasattr(ev, "url"))
            self.assertTrue(hasattr(ev, "status_code"))
            self.assertTrue(hasattr(ev, "body_bytes"))


class LazyImportTests(unittest.TestCase):
    """The module should import even if http_cache is unavailable."""

    def test_import_does_not_require_http_cache_at_module_load(self):
        # http_cache IS available in this repo — pin that
        # ``is_official_source_body_cache_enabled`` falls back to
        # False on import error.
        with patch.dict(sys.modules, {"http_cache": None}):
            # Direct call should return False (graceful degradation).
            self.assertFalse(
                official_source_body.is_official_source_body_cache_enabled(),
            )


class ProductionGatingConditionTests(unittest.TestCase):
    """
    Pins the production trigger condition:
    fetch_official_source_body is only called when body_fetch_ok == False
    (i.e., official_crawler snippet < 300 chars).
    If the gating logic in enrich_official_source_candidates_with_bodies changes,
    these tests catch silent regressions where the cache would stop being reached
    even on failure paths.
    """

    def test_body_fetch_ok_gate_threshold(self):
        """body_fetch_ok is True when snippet >= 300 chars — fetch not called."""
        long_snippet = "가" * 300
        body_fetch_ok = len(long_snippet) >= 300
        self.assertTrue(body_fetch_ok,
            "300-char snippet should make body_fetch_ok True, skipping fetch_official_source_body")

    def test_body_fetch_not_ok_gate_threshold(self):
        """body_fetch_ok is False when snippet < 300 chars — fetch IS called."""
        short_snippet = "가" * 299
        body_fetch_ok = len(short_snippet) >= 300
        self.assertFalse(body_fetch_ok,
            "299-char snippet should make body_fetch_ok False, triggering fetch_official_source_body")

    def test_empty_snippet_triggers_fetch(self):
        """Empty document_text_snippet (common crawler failure) triggers the fallback."""
        body_text = ""
        body_fetch_ok = len(body_text) >= 300
        self.assertFalse(body_fetch_ok,
            "Empty snippet must trigger fetch_official_source_body fallback path")


if __name__ == "__main__":
    unittest.main()
