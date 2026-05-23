"""Tests for the M13.3b HTTP cache integration in
``official_crawler._request_url``.

Run with: python tests/test_official_crawler_cache.py

No real network traffic. ``requests.get`` is patched with a fake that
returns a controllable :class:`requests.Response`. The cache singleton
is reset between tests so cross-case pollution is impossible.

The single most important pin in this file is
:class:`CacheOffByteIdentityTests` — it confirms that with both flags
unset (the default state on Render in M13.3b) ``_request_url`` returns
exactly what ``requests.get`` returned, byte-for-byte.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import http_cache  # noqa: E402
import official_crawler  # noqa: E402


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _make_response(
    status: int = 200,
    body: bytes = b"<html>hi</html>",
    headers=None,
    url: str = "https://fsc.go.kr/test",
) -> requests.Response:
    """Build a real ``requests.Response`` with the supplied payload."""
    response = requests.Response()
    response._content = body  # noqa: SLF001
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {"Content-Type": "text/html"})
    response.url = url
    response.reason = "OK" if status == 200 else ""
    return response


class _FakeGet:
    """Counts requests.get calls and returns a canned response.

    Configurable to raise (simulating ConnectionError) on any call.
    """

    def __init__(
        self,
        responses=None,
        raise_exc: Exception = None,
        body: bytes = b"<html>hi</html>",
        status: int = 200,
        headers=None,
    ):
        self.responses = responses
        self.raise_exc = raise_exc
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "text/html"}
        self.calls = 0

    def __call__(self, url, headers=None, timeout=None, **kwargs):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.responses is not None:
            idx = min(self.calls - 1, len(self.responses) - 1)
            return self.responses[idx]
        return _make_response(
            status=self.status,
            body=self.body,
            headers=self.headers,
            url=url,
        )


class _EnvScope:
    KEYS = (
        "HTTP_CACHE_ENABLED",
        "OFFICIAL_CRAWLER_CACHE_ENABLED",
        "OFFICIAL_CRAWLER_CACHE_TTL_SECONDS",
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


def _set_env(**values):
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _enable_both_flags(env: _EnvScope):
    _set_env(
        HTTP_CACHE_ENABLED="true",
        OFFICIAL_CRAWLER_CACHE_ENABLED="true",
    )
    http_cache.reset_default_cache_for_tests()


# ---------------------------------------------------------------------------
# Cache-OFF byte-identicality pin — the regression contract for M13.3b.
# ---------------------------------------------------------------------------


class CacheOffByteIdentityTests(unittest.TestCase):
    """With both flags unset, _request_url MUST behave like the
    pre-M13.3b function. The cache wrapper must add zero observable
    side effects."""

    def setUp(self):
        http_cache.reset_default_cache_for_tests()

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)

    def test_both_flags_unset_calls_requests_get_each_time(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                OFFICIAL_CRAWLER_CACHE_ENABLED=None,
            )
            fake = _FakeGet(body=b"payload-A", status=200)
            with patch.object(requests, "get", fake):
                r1 = official_crawler._request_url(
                    "https://fsc.go.kr/page",
                )
                r2 = official_crawler._request_url(
                    "https://fsc.go.kr/page",
                )
            self.assertEqual(fake.calls, 2)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.content, b"payload-A")
            self.assertEqual(r2.content, b"payload-A")

    def test_both_flags_unset_return_object_identical_to_requests_get(self):
        """The wrapper must return the *exact* Response object the
        underlying requests.get produced — same instance, not a copy."""
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                OFFICIAL_CRAWLER_CACHE_ENABLED=None,
            )
            sentinel = _make_response(
                status=200, body=b"sentinel-body",
                headers={"X-Trace": "abc"},
                url="https://fsc.go.kr/sentinel",
            )
            fake = _FakeGet(responses=[sentinel])
            with patch.object(requests, "get", fake):
                returned = official_crawler._request_url(
                    "https://fsc.go.kr/sentinel",
                )
            # The wrapper returns the original Response instance
            # unchanged when the cache is off — pin via `is`.
            self.assertIs(returned, sentinel)


# ---------------------------------------------------------------------------
# Both-flag precedence — both must be true together
# ---------------------------------------------------------------------------


class FlagPrecedenceTests(unittest.TestCase):
    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)

    def test_only_master_flag_set_no_caching(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED="true",
                OFFICIAL_CRAWLER_CACHE_ENABLED=None,
            )
            http_cache.reset_default_cache_for_tests()
            fake = _FakeGet(body=b"x")
            with patch.object(requests, "get", fake):
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
            self.assertEqual(fake.calls, 2)

    def test_only_official_flag_set_no_caching(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                OFFICIAL_CRAWLER_CACHE_ENABLED="true",
            )
            http_cache.reset_default_cache_for_tests()
            fake = _FakeGet(body=b"x")
            with patch.object(requests, "get", fake):
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
            self.assertEqual(fake.calls, 2)

    def test_both_flags_set_caching_active(self):
        with _EnvScope():
            _enable_both_flags(_EnvScope())
            fake = _FakeGet(body=b"x")
            with patch.object(requests, "get", fake):
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
            self.assertEqual(fake.calls, 1)


# ---------------------------------------------------------------------------
# Cache-on behaviour
# ---------------------------------------------------------------------------


class CacheOnBehaviourTests(unittest.TestCase):
    def setUp(self):
        _enable_both_flags(_EnvScope())

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_TTL_SECONDS", None)

    def test_second_call_to_gov_url_hits_cache(self):
        fake = _FakeGet(body=b"gov-page")
        with patch.object(requests, "get", fake):
            r1 = official_crawler._request_url(
                "https://fsc.go.kr/page",
            )
            r2 = official_crawler._request_url(
                "https://fsc.go.kr/page",
            )
        self.assertEqual(fake.calls, 1)
        self.assertEqual(r1.content, b"gov-page")
        self.assertEqual(r2.content, b"gov-page")
        self.assertEqual(r2.status_code, 200)

    def test_non_gov_domain_passes_through(self):
        fake = _FakeGet(body=b"non-gov")
        with patch.object(requests, "get", fake):
            official_crawler._request_url("https://example.com/page")
            official_crawler._request_url("https://example.com/page")
        self.assertEqual(fake.calls, 2)

    def test_404_not_cached(self):
        fake = _FakeGet(body=b"", status=404)
        with patch.object(requests, "get", fake):
            official_crawler._request_url(
                "https://fsc.go.kr/missing",
            )
            official_crawler._request_url(
                "https://fsc.go.kr/missing",
            )
        self.assertEqual(fake.calls, 2)

    def test_oversize_body_not_cached(self):
        big = b"x" * (
            official_crawler._OFFICIAL_CRAWLER_CACHE_MAX_BODY_BYTES + 1
        )
        fake = _FakeGet(body=big, status=200)
        with patch.object(requests, "get", fake):
            official_crawler._request_url(
                "https://fsc.go.kr/big",
            )
            official_crawler._request_url(
                "https://fsc.go.kr/big",
            )
        self.assertEqual(fake.calls, 2)

    def test_no_store_response_not_cached(self):
        fake = _FakeGet(
            body=b"ns",
            headers={"Cache-Control": "no-store"},
        )
        with patch.object(requests, "get", fake):
            official_crawler._request_url(
                "https://fsc.go.kr/ns",
            )
            official_crawler._request_url(
                "https://fsc.go.kr/ns",
            )
        self.assertEqual(fake.calls, 2)

    def test_network_exception_propagates(self):
        fake = _FakeGet(raise_exc=ConnectionError("boom"))
        with patch.object(requests, "get", fake):
            with self.assertRaises(ConnectionError):
                official_crawler._request_url(
                    "https://fsc.go.kr/down",
                )
        # The retry loop in _do_request_url_raw attempts twice and
        # then re-raises, so 2 calls is correct.
        self.assertEqual(fake.calls, 2)


# ---------------------------------------------------------------------------
# Synthetic Response shape on cache hit
# ---------------------------------------------------------------------------


class ResponseShapeOnHitTests(unittest.TestCase):
    def setUp(self):
        _enable_both_flags(_EnvScope())

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)

    def test_synthetic_response_supports_caller_attributes(self):
        """All four call sites use ``.status_code``,
        ``.raise_for_status()``, ``.content``, plus the response is
        passed to ``_response_text`` which reads ``.encoding`` and
        ``.apparent_encoding``."""
        body = "<html><body>한국어 정책</body></html>".encode("utf-8")
        fake = _FakeGet(
            body=body,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        with patch.object(requests, "get", fake):
            official_crawler._request_url("https://fsc.go.kr/k")
            cached = official_crawler._request_url(
                "https://fsc.go.kr/k",
            )
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(cached.content, body)
        # Headers preserved (CaseInsensitiveDict so case-blind lookup).
        self.assertEqual(
            cached.headers.get("content-type"),
            "text/html; charset=utf-8",
        )
        # raise_for_status() is a no-op for 200 — must not raise.
        cached.raise_for_status()
        # apparent_encoding works on synthetic responses (chardet
        # reads .content). Truthy string is enough.
        self.assertTrue(cached.apparent_encoding)
        # .text decodes content via apparent_encoding when .encoding
        # is None.
        self.assertIn("한국어", cached.text)


# ---------------------------------------------------------------------------
# TTL env var override
# ---------------------------------------------------------------------------


class TtlEnvOverrideTests(unittest.TestCase):
    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_TTL_SECONDS", None)

    def test_custom_ttl_used(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED="true",
                OFFICIAL_CRAWLER_CACHE_ENABLED="true",
                OFFICIAL_CRAWLER_CACHE_TTL_SECONDS="1",
            )
            http_cache.reset_default_cache_for_tests()
            fake = _FakeGet(body=b"x")
            with patch.object(requests, "get", fake):
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
                # Force expiry without sleeping.
                cache = http_cache.get_default_cache()
                with cache._lock:  # noqa: SLF001
                    for entry in cache._store.values():
                        entry.expires_at = time.time() - 1.0
                official_crawler._request_url(
                    "https://fsc.go.kr/x",
                )
            self.assertEqual(fake.calls, 2)

    def test_default_ttl_when_env_unset(self):
        with _EnvScope():
            _set_env(OFFICIAL_CRAWLER_CACHE_TTL_SECONDS=None)
            self.assertEqual(
                official_crawler._get_official_crawler_cache_ttl_seconds(),
                600,
            )

    def test_invalid_ttl_falls_back(self):
        with _EnvScope():
            _set_env(OFFICIAL_CRAWLER_CACHE_TTL_SECONDS="not-a-number")
            self.assertEqual(
                official_crawler._get_official_crawler_cache_ttl_seconds(),
                600,
            )
            _set_env(OFFICIAL_CRAWLER_CACHE_TTL_SECONDS="-5")
            self.assertEqual(
                official_crawler._get_official_crawler_cache_ttl_seconds(),
                600,
            )


# ---------------------------------------------------------------------------
# Multi-URL independence + concurrency
# ---------------------------------------------------------------------------


class MultiUrlAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        _enable_both_flags(_EnvScope())

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)

    def test_different_urls_independently_cached(self):
        fake = _FakeGet(body=b"x")
        with patch.object(requests, "get", fake):
            for url in (
                "https://fsc.go.kr/a",
                "https://fsc.go.kr/b",
                "https://fsc.go.kr/c",
            ):
                official_crawler._request_url(url)
                official_crawler._request_url(url)
        # 3 unique URLs => 3 real fetches, 3 cache hits.
        self.assertEqual(fake.calls, 3)

    def test_concurrent_calls_do_not_raise(self):
        fake = _FakeGet(body=b"x")
        errors = []

        def worker():
            try:
                for _ in range(20):
                    official_crawler._request_url(
                        "https://fsc.go.kr/concurrent",
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with patch.object(requests, "get", fake):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertFalse(errors, msg=f"thread errors: {errors}")
        # At most a few calls may race past the first hit, but never
        # more than the number of threads.
        self.assertLessEqual(fake.calls, 5)


# ---------------------------------------------------------------------------
# Allow-list contract
# ---------------------------------------------------------------------------


class AllowListContractTests(unittest.TestCase):
    def test_contains_only_lowercase_kr_government_domains(self):
        # The conservative subset is restricted to Korean government /
        # agency hosts. Most end in .go.kr or .or.kr, but a few of
        # the top-level government portals (gov.kr, korea.kr) live
        # directly under .kr.
        for domain in official_crawler.GOV_CACHE_ALLOWED_DOMAINS:
            self.assertEqual(
                domain, domain.lower(),
                msg=f"domain {domain!r} not lowercased",
            )
            self.assertTrue(
                domain.endswith(".kr"),
                msg=(
                    f"domain {domain!r} not a Korean government domain"
                ),
            )

    def test_count_matches_documented_size(self):
        # Per the M13.3b brief: 20 domains in the conservative subset.
        self.assertEqual(
            len(official_crawler.GOV_CACHE_ALLOWED_DOMAINS), 20,
        )


# ---------------------------------------------------------------------------
# Structured log emission
# ---------------------------------------------------------------------------


class StructuredLogTests(unittest.TestCase):
    def setUp(self):
        _enable_both_flags(_EnvScope())

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("OFFICIAL_CRAWLER_CACHE_ENABLED", None)

    def test_cache_hit_emits_structured_log(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(stream=buf)
        log = logging.getLogger("official_crawler")
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            fake = _FakeGet(body=b"x")
            with patch.object(requests, "get", fake):
                official_crawler._request_url(
                    "https://fsc.go.kr/log",
                )
                official_crawler._request_url(
                    "https://fsc.go.kr/log",
                )
        finally:
            log.removeHandler(handler)
        text = buf.getvalue()
        self.assertIn("official_crawler_cache_event", text)


# ---------------------------------------------------------------------------
# Static check — official_crawler.py must not import postgres / verdict modules
# ---------------------------------------------------------------------------


class PipelineIsolationStaticCheck(unittest.TestCase):
    """The M13.3b change is scoped to HTTP caching. The wrapper must
    not have pulled in any verdict, storage, or pipeline module."""

    def setUp(self):
        self.source = (
            _PROJECT_ROOT / "official_crawler.py"
        ).read_text(encoding="utf-8")

    def test_no_verdict_or_storage_module_imported(self):
        import re

        for needle in (
            "verification_card", "policy_decision", "policy_scoring",
            "policy_confidence", "policy_impact",
            "database", "postgres_storage", "postgres_backfill",
            "llm_judge",
        ):
            pattern = re.compile(
                rf"^(?:from\s+{re.escape(needle)}\b|import\s+{re.escape(needle)}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=(
                    f"official_crawler.py must not import {needle} "
                    "after M13.3b"
                ),
            )


if __name__ == "__main__":
    unittest.main()
