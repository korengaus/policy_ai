"""Tests for the M13.3d HTTP cache integration on the Google News RSS
fetch path in ``news_collector.py``.

Run with: python tests/test_news_collector_cache.py

No real network traffic. Both ``feedparser.parse`` and ``requests.get``
are patched. Naver / Daum fallback paths must NEVER hit the cache;
those tests assert call-through on every invocation.

The most important pins:
* :class:`CacheOffByteIdentityTests` — both flags unset = byte-identical
  ``feedparser.parse(rss_url)`` behavior.
* :class:`NaverDaumFallbacksNotCached` — fallbacks bypass the cache
  even with the flag on.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import requests
from requests.structures import CaseInsensitiveDict


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import http_cache  # noqa: E402
import news_collector  # noqa: E402


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


_GOOGLE_NEWS_RSS_BYTES = (
    b'<?xml version="1.0"?>\n'
    b'<rss version="2.0">\n'
    b'  <channel><title>Google News</title>\n'
    b'    <item><title>News A</title>'
    b'<link>https://example.com/a</link>'
    b'<pubDate>Mon, 24 May 2026 10:00:00 +0000</pubDate></item>\n'
    b'  </channel>\n'
    b'</rss>\n'
)


def _make_response(
    status: int = 200,
    body: bytes = _GOOGLE_NEWS_RSS_BYTES,
    headers=None,
    url: str = "https://news.google.com/rss/search?q=test",
) -> requests.Response:
    response = requests.Response()
    response._content = body  # noqa: SLF001
    response.status_code = status
    response.headers = CaseInsensitiveDict(
        headers or {"Content-Type": "application/rss+xml"},
    )
    response.url = url
    response.reason = "OK" if status == 200 else ""
    return response


class _FakeFeedparserParse:
    """Counts ``feedparser.parse`` calls, distinguishing URL-arg vs
    bytes-arg invocations. Returns a minimal feed-like object."""

    def __init__(self, body: bytes = _GOOGLE_NEWS_RSS_BYTES):
        self.body = body
        self.calls = 0
        self.url_calls = 0
        self.bytes_calls = 0
        self.last_arg = None

    def __call__(self, source):
        self.calls += 1
        self.last_arg = source
        if isinstance(source, (bytes, bytearray)):
            self.bytes_calls += 1
        elif isinstance(source, str):
            self.url_calls += 1
        return MagicMock(entries=[])


class _FakeGet:
    """Counts requests.get calls and returns a canned RSS response."""

    def __init__(
        self, body: bytes = _GOOGLE_NEWS_RSS_BYTES, status: int = 200,
        headers=None, raise_exc: Exception = None,
    ):
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/rss+xml"}
        self.raise_exc = raise_exc
        self.calls = 0
        self.urls = []

    def __call__(self, url, **kwargs):
        self.calls += 1
        self.urls.append(url)
        if self.raise_exc is not None:
            raise self.raise_exc
        return _make_response(
            status=self.status, body=self.body,
            headers=self.headers, url=url,
        )


class _EnvScope:
    KEYS = (
        "HTTP_CACHE_ENABLED",
        "NEWS_COLLECTOR_CACHE_ENABLED",
        "NEWS_COLLECTOR_CACHE_TTL_SECONDS",
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
        news_collector._reset_rss_cache_for_tests()


def _set_env(**values):
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _enable_both_flags():
    _set_env(
        HTTP_CACHE_ENABLED="true",
        NEWS_COLLECTOR_CACHE_ENABLED="true",
    )
    http_cache.reset_default_cache_for_tests()
    news_collector._reset_rss_cache_for_tests()


_GOOGLE_RSS_URL = (
    "https://news.google.com/rss/search?q=test&hl=ko&gl=KR&ceid=KR:ko"
)
_GOOGLE_RSS_URL_OTHER_QUERY = (
    "https://news.google.com/rss/search?q=other&hl=ko&gl=KR&ceid=KR:ko"
)


# ---------------------------------------------------------------------------
# Cache-OFF byte-identicality pin
# ---------------------------------------------------------------------------


class CacheOffByteIdentityTests(unittest.TestCase):
    """With both flags unset, ``_parse_google_news_rss`` MUST call
    ``feedparser.parse(rss_url)`` exactly like the pre-M13.3d code.
    """

    def setUp(self):
        news_collector._reset_rss_cache_for_tests()

    def tearDown(self):
        news_collector._reset_rss_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_ENABLED", None)

    def test_both_flags_unset_uses_feedparser_url_arg(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                NEWS_COLLECTOR_CACHE_ENABLED=None,
            )
            fake_fp = _FakeFeedparserParse()
            fake_get = _FakeGet()
            with patch.object(news_collector.feedparser, "parse", fake_fp), \
                 patch.object(requests, "get", fake_get):
                news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
                news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            # Both calls used URL-arg form.
            self.assertEqual(fake_fp.url_calls, 2)
            self.assertEqual(fake_fp.bytes_calls, 0)
            # requests.get was NEVER called on the cache-off path.
            self.assertEqual(fake_get.calls, 0)

    def test_flag_off_only_master_set(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED="true",
                NEWS_COLLECTOR_CACHE_ENABLED=None,
            )
            fake_fp = _FakeFeedparserParse()
            with patch.object(news_collector.feedparser, "parse", fake_fp):
                news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
                news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            self.assertEqual(fake_fp.url_calls, 2)

    def test_flag_off_only_module_set(self):
        with _EnvScope():
            _set_env(
                HTTP_CACHE_ENABLED=None,
                NEWS_COLLECTOR_CACHE_ENABLED="true",
            )
            fake_fp = _FakeFeedparserParse()
            with patch.object(news_collector.feedparser, "parse", fake_fp):
                news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
                news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            self.assertEqual(fake_fp.url_calls, 2)


# ---------------------------------------------------------------------------
# Cache-on behaviour for the Google News RSS path
# ---------------------------------------------------------------------------


class CacheOnBehaviourTests(unittest.TestCase):
    def setUp(self):
        _enable_both_flags()

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        news_collector._reset_rss_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_TTL_SECONDS", None)

    def test_first_call_fetches_bytes_then_parses(self):
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
        self.assertEqual(fake_get.calls, 1)
        # feedparser was called with bytes, not the URL string.
        self.assertEqual(fake_fp.bytes_calls, 1)
        self.assertEqual(fake_fp.url_calls, 0)

    def test_second_call_same_url_hits_cache(self):
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
        # Only one network fetch.
        self.assertEqual(fake_get.calls, 1)
        # Both feedparser calls were bytes-arg (one from miss, one from hit).
        self.assertEqual(fake_fp.bytes_calls, 2)

    def test_different_queries_are_separate_cache_entries(self):
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL_OTHER_QUERY)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL_OTHER_QUERY)
        # 2 unique URLs → 2 fetches.
        self.assertEqual(fake_get.calls, 2)

    def test_non_google_news_url_is_passthrough(self):
        """A URL that is not news.google.com must NOT be cached."""
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(
                "https://other.example.com/rss?q=foo",
            )
            news_collector._parse_google_news_rss(
                "https://other.example.com/rss?q=foo",
            )
        # Cache bypassed → feedparser.parse(url) called both times.
        self.assertEqual(fake_get.calls, 0)
        self.assertEqual(fake_fp.url_calls, 2)
        self.assertEqual(fake_fp.bytes_calls, 0)

    def test_non_200_response_not_cached(self):
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet(body=b"", status=500)
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
        self.assertEqual(fake_get.calls, 2)

    def test_network_exception_falls_back_to_url_feedparser(self):
        """If ``requests.get`` raises, fall back to feedparser's own
        fetch so the pipeline never breaks because of cache plumbing."""
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet(raise_exc=ConnectionError("boom"))
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
        self.assertEqual(fake_get.calls, 1)
        # Fell back to the URL-arg form.
        self.assertEqual(fake_fp.url_calls, 1)
        self.assertEqual(fake_fp.bytes_calls, 0)

    def test_oversize_body_not_cached(self):
        big = b"<rss>" + b"x" * (
            news_collector._NEWS_COLLECTOR_CACHE_MAX_BODY_BYTES + 1
        ) + b"</rss>"
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet(body=big, status=200)
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
        self.assertEqual(fake_get.calls, 2)

    def test_korean_query_url_is_cached(self):
        from urllib.parse import quote
        korean_query = quote("전세사기")
        url = (
            f"https://news.google.com/rss/search?"
            f"q={korean_query}&hl=ko&gl=KR&ceid=KR:ko"
        )
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(url)
            news_collector._parse_google_news_rss(url)
        self.assertEqual(fake_get.calls, 1)
        self.assertEqual(fake_fp.bytes_calls, 2)


# ---------------------------------------------------------------------------
# Naver / Daum fallback paths must NOT be cached
# ---------------------------------------------------------------------------


class NaverDaumFallbacksNotCached(unittest.TestCase):
    """The Naver and Daum fallback functions live outside the
    ``_parse_google_news_rss`` wrapper. Even with the flag on, they
    must hit the network on every invocation."""

    def setUp(self):
        _enable_both_flags()

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        news_collector._reset_rss_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_ENABLED", None)

    def test_naver_fallback_url_is_not_routed_through_cache(self):
        """``_parse_google_news_rss`` only caches news.google.com.
        A naver URL (which the Naver fallback function would fetch
        directly via requests.get, not via this wrapper) is rejected
        by the domain gate."""
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        naver_url = "https://search.naver.com/search.naver?query=test"
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(naver_url)
            news_collector._parse_google_news_rss(naver_url)
        # No requests.get from the cache wrapper; feedparser fell
        # through to URL-arg form (which is the cache-bypass branch).
        self.assertEqual(fake_get.calls, 0)
        self.assertEqual(fake_fp.url_calls, 2)

    def test_daum_url_is_not_routed_through_cache(self):
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        daum_url = "https://search.daum.net/search?w=news&q=test"
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(daum_url)
            news_collector._parse_google_news_rss(daum_url)
        self.assertEqual(fake_get.calls, 0)
        self.assertEqual(fake_fp.url_calls, 2)


# ---------------------------------------------------------------------------
# TTL helpers + flag-parsing
# ---------------------------------------------------------------------------


class TtlAndFlagTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_TTL_SECONDS", None)
        http_cache.reset_default_cache_for_tests()
        news_collector._reset_rss_cache_for_tests()

    def test_default_ttl_300(self):
        self.assertEqual(
            news_collector._get_rss_cache_ttl_seconds(), 300,
        )

    def test_ttl_env_override(self):
        os.environ["NEWS_COLLECTOR_CACHE_TTL_SECONDS"] = "60"
        self.assertEqual(news_collector._get_rss_cache_ttl_seconds(), 60)
        os.environ["NEWS_COLLECTOR_CACHE_TTL_SECONDS"] = "not-a-number"
        self.assertEqual(news_collector._get_rss_cache_ttl_seconds(), 300)
        os.environ["NEWS_COLLECTOR_CACHE_TTL_SECONDS"] = "-5"
        self.assertEqual(news_collector._get_rss_cache_ttl_seconds(), 300)

    def test_flag_truthy_values(self):
        for value in ("true", "True", "TRUE", "1", "on", "yes", "YES"):
            with _EnvScope():
                _set_env(
                    HTTP_CACHE_ENABLED="true",
                    NEWS_COLLECTOR_CACHE_ENABLED=value,
                )
                self.assertTrue(
                    news_collector.is_news_collector_cache_enabled(),
                    f"value {value!r} should enable the cache",
                )

    def test_flag_falsy_values(self):
        for value in ("", "false", "False", "no", "0", "off", "FALSE"):
            with _EnvScope():
                _set_env(
                    HTTP_CACHE_ENABLED="true",
                    NEWS_COLLECTOR_CACHE_ENABLED=value,
                )
                self.assertFalse(
                    news_collector.is_news_collector_cache_enabled(),
                    f"value {value!r} should NOT enable the cache",
                )


# ---------------------------------------------------------------------------
# Structured log shape
# ---------------------------------------------------------------------------


class StructuredLogTests(unittest.TestCase):
    LOGGER_NAME = "news_collector"

    def setUp(self):
        _enable_both_flags()
        self.records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(_self, record):
                self.records.append(record)

        self.handler = _Capture()
        self.logger = logging.getLogger(self.LOGGER_NAME)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        http_cache.reset_default_cache_for_tests()
        news_collector._reset_rss_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        os.environ.pop("NEWS_COLLECTOR_CACHE_ENABLED", None)

    def test_news_collector_cache_event_emitted(self):
        fake_fp = _FakeFeedparserParse()
        fake_get = _FakeGet()
        with patch.object(news_collector.feedparser, "parse", fake_fp), \
             patch.object(requests, "get", fake_get):
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
            news_collector._parse_google_news_rss(_GOOGLE_RSS_URL)
        events = [
            r for r in self.records
            if r.getMessage() == "news_collector_cache_event"
        ]
        self.assertEqual(len(events), 2)
        self.assertFalse(getattr(events[0], "cache_hit", None))
        self.assertTrue(getattr(events[1], "cache_hit", None))
        for ev in events:
            self.assertTrue(hasattr(ev, "url"))
            self.assertTrue(hasattr(ev, "status_code"))
            self.assertTrue(hasattr(ev, "body_bytes"))


class LazyImportTests(unittest.TestCase):
    def test_is_enabled_returns_false_when_http_cache_unavailable(self):
        with patch.dict(sys.modules, {"http_cache": None}):
            self.assertFalse(
                news_collector.is_news_collector_cache_enabled(),
            )


if __name__ == "__main__":
    unittest.main()
