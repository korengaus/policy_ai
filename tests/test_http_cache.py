"""Tests for the M13.3a HTTP cache module + CLI.

Run with: python tests/test_http_cache.py

No real HTTP traffic. Synthetic URLs (``https://example.gov.kr/...``)
are used throughout. Tests instantiate fresh :class:`HttpCache`
objects so the singleton's state cannot pollute across cases; the few
tests that exercise the singleton call ``reset_default_cache_for_tests``
in setUp/tearDown.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import threading
import time
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import http_cache  # noqa: E402


# ---------------------------------------------------------------------------
# Env-scope helper — identical pattern to test_postgres_storage.py
# ---------------------------------------------------------------------------


class _EnvScope:
    KEYS = (
        "HTTP_CACHE_ENABLED",
        "HTTP_CACHE_DEFAULT_TTL_SECONDS",
        "HTTP_CACHE_MAX_ENTRIES",
    )

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _set_env(**values):
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _enabled_cache(**kwargs) -> http_cache.HttpCache:
    """Construct a fresh HttpCache. Tests must wrap in _EnvScope and
    set HTTP_CACHE_ENABLED=true before calling get/put."""
    return http_cache.HttpCache(**kwargs)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class FeatureFlagTests(unittest.TestCase):
    def test_disabled_when_env_unset(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_ENABLED=None)
            self.assertFalse(http_cache.is_http_cache_enabled())

    def test_disabled_for_falsey_values(self):
        for value in (
            "", "false", "False", "FALSE", "0", "no", "off",
            "yes", "1", "TRUE-ish", "  ",
        ):
            with _EnvScope():
                _set_env(HTTP_CACHE_ENABLED=value)
                self.assertFalse(
                    http_cache.is_http_cache_enabled(),
                    msg=f"value={value!r} should not enable the cache",
                )

    def test_enabled_for_case_insensitive_true(self):
        for value in ("true", "True", "TRUE", "  true  "):
            with _EnvScope():
                _set_env(HTTP_CACHE_ENABLED=value)
                self.assertTrue(
                    http_cache.is_http_cache_enabled(),
                    msg=f"value={value!r} should enable the cache",
                )

    def test_default_ttl_seconds_default(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_DEFAULT_TTL_SECONDS=None)
            self.assertEqual(http_cache.get_default_ttl_seconds(), 3600)

    def test_default_ttl_seconds_from_env(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_DEFAULT_TTL_SECONDS="120")
            self.assertEqual(http_cache.get_default_ttl_seconds(), 120)

    def test_default_ttl_invalid_env_falls_back(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_DEFAULT_TTL_SECONDS="not-a-number")
            self.assertEqual(http_cache.get_default_ttl_seconds(), 3600)

    def test_default_ttl_negative_env_falls_back(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_DEFAULT_TTL_SECONDS="-5")
            self.assertEqual(http_cache.get_default_ttl_seconds(), 3600)

    def test_max_entries_default(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_MAX_ENTRIES=None)
            self.assertEqual(http_cache.get_max_entries(), 500)

    def test_max_entries_from_env(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_MAX_ENTRIES="10")
            self.assertEqual(http_cache.get_max_entries(), 10)


# ---------------------------------------------------------------------------
# URL normalization + cache key
# ---------------------------------------------------------------------------


class UrlNormalizationTests(unittest.TestCase):
    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            http_cache._normalize_url("HTTPS://Example.com/"),
            "https://example.com/",
        )

    def test_root_path_preserved(self):
        self.assertEqual(
            http_cache._normalize_url("https://example.com/"),
            "https://example.com/",
        )

    def test_trailing_slash_stripped_for_non_root(self):
        self.assertEqual(
            http_cache._normalize_url("https://example.com/foo/"),
            "https://example.com/foo",
        )

    def test_fragment_dropped(self):
        self.assertEqual(
            http_cache._normalize_url("https://example.com/foo?a=1#anchor"),
            "https://example.com/foo?a=1",
        )

    def test_query_preserved(self):
        self.assertEqual(
            http_cache._normalize_url("https://example.com/foo?b=2&a=1"),
            "https://example.com/foo?b=2&a=1",
        )

    def test_empty_url_returns_empty(self):
        self.assertEqual(http_cache._normalize_url(""), "")
        self.assertEqual(http_cache._normalize_url(None), "")  # type: ignore[arg-type]


class CanonicalHeadersTests(unittest.TestCase):
    def test_empty_headers_returns_empty_string(self):
        self.assertEqual(http_cache._canonical_headers(None), "")
        self.assertEqual(http_cache._canonical_headers({}), "")

    def test_only_content_affecting_headers_folded(self):
        headers = {
            "Accept": "text/html",
            "Authorization": "Bearer secret",
            "User-Agent": "test/1.0",
            "X-Custom": "ignored",
        }
        canonical = http_cache._canonical_headers(headers)
        self.assertIn("accept", canonical)
        self.assertIn("user-agent", canonical)
        self.assertNotIn("authorization", canonical.lower())
        self.assertNotIn("x-custom", canonical.lower())

    def test_lowercased_keys_and_sorted(self):
        headers = {
            "USER-AGENT": "a",
            "Accept": "text/html",
            "ACCEPT-LANGUAGE": "ko",
        }
        canonical = http_cache._canonical_headers(headers)
        # JSON dict iteration order via sort_keys=True
        parsed = json.loads(canonical)
        self.assertEqual(
            list(parsed.keys()),
            sorted(parsed.keys()),
        )
        self.assertEqual(set(parsed.keys()),
                         {"accept", "accept-language", "user-agent"})


class CacheKeyStabilityTests(unittest.TestCase):
    def test_same_url_same_key(self):
        a = http_cache.compute_cache_key("https://example.com/x")
        b = http_cache.compute_cache_key("https://example.com/x")
        self.assertEqual(a, b)

    def test_different_url_different_key(self):
        a = http_cache.compute_cache_key("https://example.com/a")
        b = http_cache.compute_cache_key("https://example.com/b")
        self.assertNotEqual(a, b)

    def test_relevant_header_changes_key(self):
        a = http_cache.compute_cache_key(
            "https://example.com/x", {"accept-language": "ko"},
        )
        b = http_cache.compute_cache_key(
            "https://example.com/x", {"accept-language": "en"},
        )
        self.assertNotEqual(a, b)

    def test_irrelevant_header_does_not_change_key(self):
        a = http_cache.compute_cache_key(
            "https://example.com/x", {"x-trace-id": "abc"},
        )
        b = http_cache.compute_cache_key(
            "https://example.com/x", {"x-trace-id": "def"},
        )
        self.assertEqual(a, b)

    def test_extract_domain(self):
        self.assertEqual(
            http_cache.extract_domain("https://Example.com:443/x"),
            "example.com",
        )
        self.assertEqual(http_cache.extract_domain(""), "")
        self.assertEqual(http_cache.extract_domain("not-a-url"), "")


# ---------------------------------------------------------------------------
# Cache-Control parsing
# ---------------------------------------------------------------------------


class CacheControlParsingTests(unittest.TestCase):
    def test_no_store(self):
        cc = http_cache.parse_cache_control("no-store")
        self.assertTrue(cc.no_store)
        self.assertFalse(cc.no_cache)
        self.assertIsNone(cc.max_age_seconds)

    def test_no_cache_with_max_age(self):
        cc = http_cache.parse_cache_control("no-cache, max-age=600")
        self.assertTrue(cc.no_cache)
        self.assertEqual(cc.max_age_seconds, 600)

    def test_private_with_max_age(self):
        cc = http_cache.parse_cache_control("private, max-age=300")
        self.assertTrue(cc.private)
        self.assertEqual(cc.max_age_seconds, 300)

    def test_invalid_max_age_returns_none(self):
        cc = http_cache.parse_cache_control("max-age=NaN")
        self.assertIsNone(cc.max_age_seconds)
        # max-age= without value is also tolerated.
        cc = http_cache.parse_cache_control("max-age=")
        self.assertIsNone(cc.max_age_seconds)

    def test_empty_header(self):
        cc = http_cache.parse_cache_control("")
        self.assertFalse(cc.no_store)
        self.assertFalse(cc.no_cache)
        self.assertFalse(cc.private)
        self.assertIsNone(cc.max_age_seconds)

    def test_none_header(self):
        cc = http_cache.parse_cache_control(None)
        self.assertFalse(cc.no_store)

    def test_case_insensitive(self):
        cc = http_cache.parse_cache_control("No-Store, MAX-AGE=120")
        self.assertTrue(cc.no_store)
        self.assertEqual(cc.max_age_seconds, 120)


# ---------------------------------------------------------------------------
# HttpCache disabled / enabled basic behaviour
# ---------------------------------------------------------------------------


class HttpCacheDisabledTests(unittest.TestCase):
    def test_get_returns_none_when_disabled(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_ENABLED=None)
            cache = _enabled_cache()
            self.assertIsNone(cache.get("https://example.gov.kr/x"))
            self.assertEqual(cache.stats.disabled_calls, 1)
            self.assertEqual(cache.stats.misses, 0)

    def test_put_returns_false_when_disabled(self):
        with _EnvScope():
            _set_env(HTTP_CACHE_ENABLED=None)
            cache = _enabled_cache()
            self.assertFalse(
                cache.put("https://example.gov.kr/x", b"data"),
            )
            self.assertEqual(cache.stats.disabled_calls, 1)
            self.assertEqual(cache.stats.stored, 0)
            self.assertEqual(cache.size(), 0)


class HttpCacheStoreRetrieveTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_basic_put_then_get(self):
        cache = _enabled_cache()
        url = "https://example.gov.kr/data"
        body = b"<html>hi</html>"
        ok = cache.put(
            url, body, status_code=200,
            headers={"Content-Type": "text/html"},
        )
        self.assertTrue(ok)
        entry = cache.get(url)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.body, body)
        self.assertEqual(entry.status_code, 200)
        self.assertEqual(entry.headers.get("Content-Type"), "text/html")
        self.assertEqual(cache.stats.hits, 1)
        self.assertEqual(cache.stats.misses, 0)
        self.assertEqual(cache.stats.stored, 1)

    def test_miss_increments_misses(self):
        cache = _enabled_cache()
        result = cache.get("https://example.gov.kr/no-such-url")
        self.assertIsNone(result)
        self.assertEqual(cache.stats.misses, 1)
        self.assertEqual(cache.stats.hits, 0)

    def test_irrelevant_request_header_hits_same_entry(self):
        cache = _enabled_cache()
        url = "https://example.gov.kr/x"
        cache.put(
            url, b"payload",
            request_headers={"x-trace-id": "abc"},
        )
        # Different x-trace-id must still hit (not a content-affecting header).
        entry = cache.get(url, headers={"x-trace-id": "different"})
        self.assertIsNotNone(entry)

    def test_relevant_request_header_misses(self):
        cache = _enabled_cache()
        url = "https://example.gov.kr/x"
        cache.put(
            url, b"payload",
            request_headers={"accept-language": "ko"},
        )
        entry = cache.get(url, headers={"accept-language": "en"})
        self.assertIsNone(entry)
        self.assertEqual(cache.stats.misses, 1)


# ---------------------------------------------------------------------------
# TTL behaviour
# ---------------------------------------------------------------------------


class TtlBehaviourTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_immediate_get_hits(self):
        cache = _enabled_cache()
        cache.put(
            "https://example.gov.kr/x", b"body", ttl_seconds=60,
        )
        self.assertIsNotNone(cache.get("https://example.gov.kr/x"))

    def test_expired_entry_returns_none(self):
        cache = _enabled_cache()
        url = "https://example.gov.kr/expiring"
        cache.put(url, b"body", ttl_seconds=10)
        # Force the entry into the past instead of sleeping.
        with cache._lock:  # noqa: SLF001 — testing internal state
            for entry in cache._store.values():
                entry.expires_at = time.time() - 1.0
        self.assertIsNone(cache.get(url))
        self.assertEqual(cache.stats.expired, 1)

    def test_is_expired_method(self):
        entry = http_cache.CacheEntry(
            key="k", url="u", body=b"",
            status_code=200, headers={},
            fetched_at=time.time(), expires_at=time.time() + 100,
        )
        self.assertFalse(entry.is_expired())
        entry.expires_at = time.time() - 1.0
        self.assertTrue(entry.is_expired())

    def test_ttl_precedence_explicit_arg(self):
        cache = _enabled_cache(default_ttl_seconds=10)
        url = "https://example.gov.kr/x"
        # Explicit arg=1; Cache-Control says max-age=999; default=10.
        # Explicit arg wins.
        cache.put(
            url, b"body",
            ttl_seconds=1,
            headers={"Cache-Control": "max-age=999"},
        )
        with cache._lock:  # noqa: SLF001
            for entry in cache._store.values():
                expected_expiry_window = (
                    time.time() - entry.fetched_at - 1
                )
                self.assertLessEqual(
                    abs((entry.expires_at - entry.fetched_at) - 1.0),
                    0.01,
                    msg="explicit ttl_seconds=1 should have won",
                )

    def test_ttl_precedence_cache_control_max_age(self):
        cache = _enabled_cache(default_ttl_seconds=10)
        url = "https://example.gov.kr/x"
        cache.put(
            url, b"body",
            headers={"Cache-Control": "max-age=120"},
        )
        with cache._lock:  # noqa: SLF001
            for entry in cache._store.values():
                self.assertAlmostEqual(
                    entry.expires_at - entry.fetched_at, 120,
                    delta=0.01,
                )

    def test_ttl_precedence_default(self):
        cache = _enabled_cache(default_ttl_seconds=42)
        url = "https://example.gov.kr/x"
        cache.put(url, b"body")
        with cache._lock:  # noqa: SLF001
            for entry in cache._store.values():
                self.assertAlmostEqual(
                    entry.expires_at - entry.fetched_at, 42,
                    delta=0.01,
                )

    def test_zero_ttl_refused(self):
        cache = _enabled_cache()
        self.assertFalse(
            cache.put(
                "https://example.gov.kr/x", b"body",
                ttl_seconds=0,
            )
        )
        self.assertEqual(cache.stats.refused_by_cache_control, 1)
        self.assertEqual(cache.stats.stored, 0)


# ---------------------------------------------------------------------------
# Cache-Control refusal
# ---------------------------------------------------------------------------


class CacheControlRefusalTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_no_store_refused(self):
        cache = _enabled_cache()
        ok = cache.put(
            "https://example.gov.kr/x", b"body",
            headers={"Cache-Control": "no-store"},
        )
        self.assertFalse(ok)
        self.assertEqual(cache.stats.refused_by_cache_control, 1)
        self.assertEqual(cache.size(), 0)

    def test_no_cache_refused(self):
        cache = _enabled_cache()
        ok = cache.put(
            "https://example.gov.kr/x", b"body",
            headers={"Cache-Control": "no-cache"},
        )
        self.assertFalse(ok)
        self.assertEqual(cache.stats.refused_by_cache_control, 1)

    def test_private_refused(self):
        cache = _enabled_cache()
        ok = cache.put(
            "https://example.gov.kr/x", b"body",
            headers={"Cache-Control": "private, max-age=600"},
        )
        self.assertFalse(ok)
        self.assertEqual(cache.stats.refused_by_cache_control, 1)


# ---------------------------------------------------------------------------
# Domain allow / deny
# ---------------------------------------------------------------------------


class DomainAllowDenyTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_allow_list_restricts_storage(self):
        cache = _enabled_cache(allowed_domains={"allowed.gov.kr"})
        ok = cache.put(
            "https://other.gov.kr/x", b"body",
        )
        self.assertFalse(ok)
        self.assertEqual(cache.stats.refused_by_domain, 1)
        ok = cache.put(
            "https://allowed.gov.kr/x", b"body",
        )
        self.assertTrue(ok)

    def test_deny_list_blocks_storage(self):
        cache = _enabled_cache(denied_domains={"denied.gov.kr"})
        ok = cache.put(
            "https://denied.gov.kr/x", b"body",
        )
        self.assertFalse(ok)
        self.assertEqual(cache.stats.refused_by_domain, 1)

    def test_deny_wins_over_allow(self):
        cache = _enabled_cache(
            allowed_domains={"both.gov.kr"},
            denied_domains={"both.gov.kr"},
        )
        ok = cache.put("https://both.gov.kr/x", b"body")
        self.assertFalse(ok)
        self.assertEqual(cache.stats.refused_by_domain, 1)

    def test_empty_allow_and_deny_allows_all(self):
        cache = _enabled_cache()
        for url in (
            "https://a.com/x",
            "https://b.example.gov.kr/y",
            "https://anything.invalid/z",
        ):
            self.assertTrue(cache.put(url, b"body"))


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class LruEvictionTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_eviction_at_capacity(self):
        cache = _enabled_cache(max_entries=3)
        urls = [
            f"https://example.gov.kr/{i}" for i in range(4)
        ]
        for url in urls:
            self.assertTrue(cache.put(url, b"body"))
        # The first put should have been evicted.
        self.assertEqual(cache.stats.evicted, 1)
        self.assertIsNone(cache.get(urls[0]))
        for url in urls[1:]:
            self.assertIsNotNone(cache.get(url))

    def test_get_touches_lru(self):
        cache = _enabled_cache(max_entries=3)
        url_a = "https://example.gov.kr/a"
        url_b = "https://example.gov.kr/b"
        url_c = "https://example.gov.kr/c"
        url_d = "https://example.gov.kr/d"
        cache.put(url_a, b"a")
        cache.put(url_b, b"b")
        cache.put(url_c, b"c")
        # Touch A so it becomes most-recent; B should now be oldest.
        self.assertIsNotNone(cache.get(url_a))
        cache.put(url_d, b"d")
        self.assertEqual(cache.stats.evicted, 1)
        self.assertIsNone(cache.get(url_b))
        self.assertIsNotNone(cache.get(url_a))
        self.assertIsNotNone(cache.get(url_c))
        self.assertIsNotNone(cache.get(url_d))

    def test_replacement_does_not_evict(self):
        cache = _enabled_cache(max_entries=3)
        for i in range(3):
            cache.put(f"https://example.gov.kr/{i}", b"body")
        # Replacing an existing key must not trigger eviction.
        cache.put("https://example.gov.kr/0", b"updated")
        self.assertEqual(cache.stats.evicted, 0)
        entry = cache.get("https://example.gov.kr/0")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.body, b"updated")


# ---------------------------------------------------------------------------
# Thread safety + counter sanity
# ---------------------------------------------------------------------------


class ThreadSafetyTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_concurrent_put_get_no_exceptions(self):
        cache = _enabled_cache(max_entries=200)
        errors = []

        def worker(worker_id):
            try:
                for i in range(100):
                    url = f"https://example.gov.kr/{worker_id}/{i}"
                    cache.put(url, b"body")
                    cache.get(url)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(wid,))
            for wid in range(10)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors, msg=f"thread errors: {errors}")
        # Counter sanity — never negative.
        for field_name in (
            "hits", "misses", "expired",
            "refused_by_domain", "refused_by_cache_control",
            "evicted", "stored", "disabled_calls",
        ):
            self.assertGreaterEqual(
                getattr(cache.stats, field_name), 0,
                msg=f"{field_name} went negative",
            )


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"

    def tearDown(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_clear_returns_count_and_empties(self):
        cache = _enabled_cache()
        for i in range(5):
            cache.put(f"https://example.gov.kr/{i}", b"body")
        self.assertEqual(cache.clear(), 5)
        self.assertEqual(cache.size(), 0)

    def test_snapshot_keys(self):
        cache = _enabled_cache()
        cache.put("https://example.gov.kr/x", b"body")
        snap = cache.snapshot()
        expected = {
            "enabled", "max_entries", "default_ttl_seconds",
            "allowed_domains", "denied_domains",
            "current_size", "stats", "entries_preview",
        }
        self.assertSetEqual(set(snap.keys()), expected)

    def test_snapshot_preview_capped_at_20(self):
        cache = _enabled_cache(max_entries=100)
        for i in range(30):
            cache.put(f"https://example.gov.kr/{i}", b"body")
        snap = cache.snapshot()
        self.assertLessEqual(len(snap["entries_preview"]), 20)


class SingletonTests(unittest.TestCase):
    def test_singleton_is_stable(self):
        http_cache.reset_default_cache_for_tests()
        a = http_cache.get_default_cache()
        b = http_cache.get_default_cache()
        self.assertIs(a, b)

    def test_reset_for_tests_creates_fresh_instance(self):
        first = http_cache.get_default_cache()
        http_cache.reset_default_cache_for_tests()
        second = http_cache.get_default_cache()
        self.assertIsNot(first, second)


# ---------------------------------------------------------------------------
# Pipeline isolation pin — the contract that M13.3a is dormant.
# ---------------------------------------------------------------------------


class PipelineIsolationPin(unittest.TestCase):
    """Static-source scan. If any production module imports
    ``http_cache``, M13.3a's "infrastructure only" claim is broken
    and this test fails."""

    FORBIDDEN_IMPORTERS = (
        "official_crawler.py",
        "official_source_body.py",
        "news_collector.py",
        "article_extractor.py",
        "main.py",
        "api_server.py",
        "job_manager.py",
        "scheduler.py",  # may not exist
        "verification_card.py",
        "policy_decision.py",
        "policy_scoring.py",
        "policy_confidence.py",
        "database.py",
        "postgres_storage.py",
        "postgres_backfill.py",
        "llm_judge.py",
    )

    def test_no_production_module_imports_http_cache(self):
        forbidden = re.compile(
            r"^(?:from\s+http_cache\b|import\s+http_cache\b)",
            re.MULTILINE,
        )
        offenders = []
        for filename in self.FORBIDDEN_IMPORTERS:
            path = _PROJECT_ROOT / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if forbidden.search(text):
                offenders.append(filename)
        self.assertFalse(
            offenders,
            msg=(
                "The following production files import http_cache in "
                "M13.3a, breaking the 'infrastructure only' "
                f"contract: {offenders}"
            ),
        )


# ---------------------------------------------------------------------------
# Module-level static checks
# ---------------------------------------------------------------------------


class ModuleLevelStaticChecks(unittest.TestCase):
    def setUp(self):
        self.module_path = _PROJECT_ROOT / "http_cache.py"
        self.source = self.module_path.read_text(encoding="utf-8")

    def test_no_third_party_imports(self):
        forbidden = (
            "requests", "httpx", "urllib3",
            "openai", "anthropic",
            "fastapi", "sqlalchemy",
        )
        for name in forbidden:
            pattern = re.compile(
                rf"^(?:from\s+{name}\b|import\s+{name}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"http_cache.py must not import {name}",
            )

    def test_no_pipeline_imports(self):
        for needle in (
            "official_crawler", "official_source_body",
            "news_collector", "article_extractor",
            "main", "api_server", "database",
            "postgres_storage", "postgres_backfill", "llm_judge",
        ):
            pattern = re.compile(
                rf"^(?:from\s+{re.escape(needle)}\b|import\s+{re.escape(needle)}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"http_cache.py must not import {needle}",
            )


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "check_http_cache_cli",
        str(_PROJECT_ROOT / "scripts" / "check_http_cache.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliTests(unittest.TestCase):
    def _run_cli(self, argv):
        module = _load_cli_module()
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = out, err
            rc = module.main(argv)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        # Force fresh singleton for every CLI test.
        http_cache.reset_default_cache_for_tests()

    def tearDown(self):
        http_cache.reset_default_cache_for_tests()
        os.environ.pop("HTTP_CACHE_ENABLED", None)

    def test_help_exits_zero(self):
        rc, out, _ = self._run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("check_http_cache", out)
        self.assertIn("Exit codes", out)

    def test_status_disabled_exits_zero(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        rc, out, _ = self._run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("Enabled:                False", out)
        self.assertIn("M13.3a infrastructure only", out)

    def test_status_json_disabled_includes_safety(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        rc, out, _ = self._run_cli(["--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertFalse(data["enabled"])
        self.assertFalse(data["safety"]["integrated_with_pipeline"])
        self.assertFalse(data["safety"]["makes_real_http_calls"])

    def test_simulate_hit_requires_enabled(self):
        os.environ.pop("HTTP_CACHE_ENABLED", None)
        rc, _, err = self._run_cli(["--simulate-hit"])
        self.assertEqual(rc, 1)
        self.assertIn("HTTP_CACHE_ENABLED=true", err)

    def test_simulate_hit_enabled_succeeds(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"
        rc, out, _ = self._run_cli(["--simulate-hit"])
        self.assertEqual(rc, 0)
        self.assertIn("Step 1", out)
        self.assertIn("hit", out)

    def test_simulate_deny_enabled_succeeds(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"
        rc, out, _ = self._run_cli(["--simulate-deny"])
        self.assertEqual(rc, 0)
        self.assertIn("stored=False", out)

    def test_simulate_expired_enabled_succeeds(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"
        rc, out, _ = self._run_cli(["--simulate-expired"])
        self.assertEqual(rc, 0)
        self.assertIn("expired", out.lower())

    def test_simulate_hit_json(self):
        os.environ["HTTP_CACHE_ENABLED"] = "true"
        rc, out, _ = self._run_cli(["--simulate-hit", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["simulation"], "hit")
        self.assertTrue(data["success"])


if __name__ == "__main__":
    unittest.main()
