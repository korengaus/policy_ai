"""M16-speed-1a — pins for the gnewsdecoder URL cache (Part H) and
ai_reasoner OpenAI client timeout (Part F1).

Part H — gnewsdecoder cache
---------------------------
The cache lives in ``news_collector.py`` and wraps the
``gnewsdecoder()`` call inside ``resolve_google_news_url``. It is
always-on (no env flag), disk-backed at ``.cache/gnewsdecoder_cache.json``,
keyed by ``sha1(google_url)[:16]``, TTL 24h. Failed decodes are NOT
cached (a transient decoder error must not pin the fallback for 24h).
The non-Google short-circuit at ``news_collector.py:919`` must remain
BEFORE the cache lookup so the existing
``test_non_google_url_short_circuit_no_error`` contract holds.

Tests in this file:
  * test_gnewsdecoder_cache_hit_skips_decoder
  * test_gnewsdecoder_cache_miss_calls_decoder_and_stores
  * test_gnewsdecoder_cache_expiry_re_decodes
  * test_gnewsdecoder_failure_not_cached
  * test_non_google_url_bypasses_cache

Part F1 — ai_reasoner timeout
-----------------------------
The OpenAI client in ``ai_reasoner.get_openai_client()`` must be
constructed with ``timeout=20.0`` so a wedged LLM call fails fast
(20s) instead of hanging up to the SDK default of 600s. Matches
``llm_judge.py``'s ``_OPENAI_TIMEOUT_SECONDS = 15.0`` convention
(the reasoning prompt is 2-4x the judge prompt size, hence 20 not 15).

Test in this file:
  * test_ai_reasoner_client_has_timeout
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import news_collector  # noqa: E402


# ---------------------------------------------------------------------------
# Part H — gnewsdecoder cache
# ---------------------------------------------------------------------------


class GnewsdecoderCacheTests(unittest.TestCase):
    """End-to-end behavior of the cache wrapper in
    ``resolve_google_news_url``."""

    GOOGLE_URL = (
        "https://news.google.com/rss/articles/cache-test-001?oc=5"
    )
    DECODED_URL = "https://www.example.kr/news/2026/05/28/policy-update.html"

    def setUp(self):
        news_collector._reset_gnewsdecoder_cache_for_tests()

    def tearDown(self):
        news_collector._reset_gnewsdecoder_cache_for_tests()

    # ---- Test 1: cache hit skips decoder ----

    def test_gnewsdecoder_cache_hit_skips_decoder(self):
        """Second call to ``resolve_google_news_url`` with the same
        URL must use the cache; the decoder mock must NOT be called
        on the second invocation."""
        decoded = {"status": True, "decoded_url": self.DECODED_URL}
        with mock.patch.object(
            news_collector, "gnewsdecoder", return_value=decoded,
        ) as mocked:
            first = news_collector.resolve_google_news_url(self.GOOGLE_URL)
            second = news_collector.resolve_google_news_url(self.GOOGLE_URL)

        self.assertEqual(first, self.DECODED_URL)
        self.assertEqual(second, self.DECODED_URL)
        self.assertEqual(
            mocked.call_count, 1,
            "Second call must hit the cache, not the decoder. "
            f"call_count={mocked.call_count}",
        )

    # ---- Test 2: cache miss calls decoder AND stores ----

    def test_gnewsdecoder_cache_miss_calls_decoder_and_stores(self):
        """Fresh URL on cold cache: decoder is called AND the entry
        is written to disk."""
        decoded = {"status": True, "decoded_url": self.DECODED_URL}
        with mock.patch.object(
            news_collector, "gnewsdecoder", return_value=decoded,
        ) as mocked:
            result = news_collector.resolve_google_news_url(self.GOOGLE_URL)

        self.assertEqual(result, self.DECODED_URL)
        self.assertEqual(mocked.call_count, 1)

        # Cache file written; entry round-trips.
        self.assertTrue(news_collector.GNEWSDECODER_CACHE_PATH.exists())
        cache = json.loads(
            news_collector.GNEWSDECODER_CACHE_PATH.read_text(encoding="utf-8")
        )
        key = news_collector._gnewsdecoder_cache_key(self.GOOGLE_URL)
        self.assertIn(key, cache)
        self.assertEqual(cache[key].get("decoded_url"), self.DECODED_URL)
        self.assertEqual(cache[key].get("google_news_url"), self.GOOGLE_URL)
        self.assertIn("cached_at", cache[key])

    # ---- Test 3: expiry causes re-decode ----

    def test_gnewsdecoder_cache_expiry_re_decodes(self):
        """Past-TTL entries are NOT served; decoder is re-called."""
        # Seed cache with an expired entry by writing directly.
        key = news_collector._gnewsdecoder_cache_key(self.GOOGLE_URL)
        expired_at = (
            datetime.now(timezone.utc)
            - timedelta(
                seconds=news_collector.GNEWSDECODER_CACHE_TTL_SECONDS + 10
            )
        )
        news_collector.GNEWSDECODER_CACHE_PATH.parent.mkdir(
            parents=True, exist_ok=True,
        )
        news_collector.GNEWSDECODER_CACHE_PATH.write_text(
            json.dumps({
                key: {
                    "cached_at": expired_at.isoformat(),
                    "google_news_url": self.GOOGLE_URL,
                    "decoded_url": "https://stale.example.kr/old-url",
                },
            }),
            encoding="utf-8",
        )

        decoded = {"status": True, "decoded_url": self.DECODED_URL}
        with mock.patch.object(
            news_collector, "gnewsdecoder", return_value=decoded,
        ) as mocked:
            result = news_collector.resolve_google_news_url(self.GOOGLE_URL)

        # Decoder MUST have been called — expired entry is ignored.
        self.assertEqual(mocked.call_count, 1)
        # Returned the fresh decoded value, not the stale one.
        self.assertEqual(result, self.DECODED_URL)
        self.assertNotEqual(result, "https://stale.example.kr/old-url")

    # ---- Test 4: decoder failure NOT cached ----

    def test_gnewsdecoder_failure_not_cached(self):
        """A decoder error must NOT write to the cache; the next call
        must retry the decoder (not serve a fallback)."""
        with mock.patch.object(
            news_collector,
            "gnewsdecoder",
            side_effect=ValueError("transient decode failure"),
        ) as mocked:
            first = news_collector.resolve_google_news_url(self.GOOGLE_URL)
            second = news_collector.resolve_google_news_url(self.GOOGLE_URL)

        # Both calls fall back to the original URL.
        self.assertEqual(first, self.GOOGLE_URL)
        self.assertEqual(second, self.GOOGLE_URL)
        # And the decoder was called twice — failure was not cached.
        self.assertEqual(
            mocked.call_count, 2,
            "Decoder failure must not be cached; second call must "
            f"re-attempt. call_count={mocked.call_count}",
        )
        # Cache file should either not exist OR not contain this key.
        if news_collector.GNEWSDECODER_CACHE_PATH.exists():
            cache = json.loads(
                news_collector.GNEWSDECODER_CACHE_PATH.read_text(
                    encoding="utf-8",
                )
            )
            key = news_collector._gnewsdecoder_cache_key(self.GOOGLE_URL)
            self.assertNotIn(
                key, cache,
                "Decoder failure must not produce a cache entry.",
            )

    # ---- Test 5: non-Google URL bypasses cache (short-circuit) ----

    def test_non_google_url_bypasses_cache(self):
        """The non-Google URL short-circuit MUST sit BEFORE the cache
        lookup. The decoder mock must not be invoked at all, AND the
        cache file must not be created."""
        non_google_url = "https://www.fss.or.kr/notice/12345"

        with mock.patch.object(news_collector, "gnewsdecoder") as mocked:
            result = news_collector.resolve_google_news_url(non_google_url)

        self.assertEqual(result, non_google_url)
        mocked.assert_not_called()
        # Cache file should not have been touched by a non-Google URL.
        # (The cache file might exist from prior tests in this class's
        # setUp/tearDown lifecycle; what matters is no entry was added
        # for this non-Google URL.)
        if news_collector.GNEWSDECODER_CACHE_PATH.exists():
            cache = json.loads(
                news_collector.GNEWSDECODER_CACHE_PATH.read_text(
                    encoding="utf-8",
                )
            )
            key = news_collector._gnewsdecoder_cache_key(non_google_url)
            self.assertNotIn(key, cache)


class GnewsdecoderCacheKeyShapeTests(unittest.TestCase):
    """Pin the cache key shape so a future refactor cannot silently
    drop the sha1(url)[:16] convention shared with _cache_key at
    news_collector.py:143."""

    def test_key_is_16_char_hex(self):
        key = news_collector._gnewsdecoder_cache_key(
            "https://news.google.com/rss/articles/abc",
        )
        self.assertEqual(len(key), 16)
        # All hex characters.
        int(key, 16)

    def test_key_is_deterministic(self):
        url = "https://news.google.com/rss/articles/deterministic"
        self.assertEqual(
            news_collector._gnewsdecoder_cache_key(url),
            news_collector._gnewsdecoder_cache_key(url),
        )

    def test_key_differs_for_different_urls(self):
        a = news_collector._gnewsdecoder_cache_key(
            "https://news.google.com/rss/articles/AAA",
        )
        b = news_collector._gnewsdecoder_cache_key(
            "https://news.google.com/rss/articles/BBB",
        )
        self.assertNotEqual(a, b)


class GnewsdecoderCacheNoOpDecodeNotStoredTests(unittest.TestCase):
    """When the decoder returns the input URL unchanged (no-op
    decode), the cache must NOT be written — otherwise the cache
    would store entries that provide no speedup AND would mask a
    transient decoder degradation behind an "I see no benefit"
    fallback."""

    def setUp(self):
        news_collector._reset_gnewsdecoder_cache_for_tests()

    def tearDown(self):
        news_collector._reset_gnewsdecoder_cache_for_tests()

    def test_noop_decode_not_cached(self):
        google_url = "https://news.google.com/rss/articles/noop"
        decoded_noop = {"status": True, "decoded_url": google_url}

        with mock.patch.object(
            news_collector, "gnewsdecoder", return_value=decoded_noop,
        ):
            result = news_collector.resolve_google_news_url(google_url)

        self.assertEqual(result, google_url)
        # No cache file (or no entry) because the decode was a no-op.
        if news_collector.GNEWSDECODER_CACHE_PATH.exists():
            cache = json.loads(
                news_collector.GNEWSDECODER_CACHE_PATH.read_text(
                    encoding="utf-8",
                )
            )
            key = news_collector._gnewsdecoder_cache_key(google_url)
            self.assertNotIn(key, cache)


# ---------------------------------------------------------------------------
# Part F1 — ai_reasoner OpenAI client timeout
# ---------------------------------------------------------------------------


class AiReasonerOpenAITimeoutTests(unittest.TestCase):
    """Pin that ``ai_reasoner.get_openai_client`` constructs the
    OpenAI client with ``timeout=20.0``. Matches the llm_judge.py
    convention (constructor-level timeout, 15.0s there; 20.0s here
    because the reasoning prompt is 2-4x the judge prompt size)."""

    def setUp(self):
        import os
        self._saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key-m16-speed-1a"

    def tearDown(self):
        import os
        if self._saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved_key

    def test_ai_reasoner_client_has_timeout(self):
        import ai_reasoner

        # Module-level constant must be 20.0 (the documented value).
        self.assertEqual(
            ai_reasoner._AI_REASONER_TIMEOUT_SECONDS, 20.0,
            "Module constant for the OpenAI client timeout must be "
            "20.0 per M16-speed-1a Part F1. Update both the constant "
            "and the docstring if this is intentionally changed.",
        )

        # Constructor receives the timeout kwarg.
        fake_openai = mock.MagicMock()
        with mock.patch.object(ai_reasoner, "OpenAI", fake_openai):
            client, reason = ai_reasoner.get_openai_client()

        self.assertIsNone(reason)
        self.assertIs(client, fake_openai.return_value)
        # OpenAI(api_key=..., timeout=20.0) — match by kwargs.
        _args, kwargs = fake_openai.call_args
        self.assertEqual(
            kwargs.get("timeout"), 20.0,
            "OpenAI client must be constructed with timeout=20.0. "
            f"Got kwargs={kwargs!r}.",
        )
        self.assertEqual(kwargs.get("api_key"), "test-key-m16-speed-1a")


if __name__ == "__main__":
    unittest.main()
