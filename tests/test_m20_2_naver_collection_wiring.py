"""Tests for M20-2 — wiring the Naver news SearchProvider into news collection.

Run with: python tests/test_m20_2_naver_collection_wiring.py

Option A design: Naver API is a fallback tier that fires ONLY when the Google
RSS ladder selected nothing AND NAVER_SEARCH_ENABLED is true. Covers:

(a) Flag OFF  -> byte-identical control flow + debug dict; provider never
    constructed (get_search_provider assert_not_called); no naver_api_count key.
(b) Flag ON + RSS empty  -> Naver mock items selected; mode/collection_source
    == "naver_api"; naver_api_count present.
(c) Flag ON + RSS works  -> Naver provider NOT invoked; common path unchanged.
(d) Dedup  -> overlapping Naver URLs/titles collapsed via _dedupe_news_items +
    M17b; google_link == original_url so the M15 main.py pass behaves.
(e) Cache segmentation  -> disabled key byte-identical to pre-M20-2; enabled
    key gets "-nv"; toggling serves no cross-contaminated entry.

NO real API call is ever made — get_search_provider / requests are patched.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import news_collector  # noqa: E402
from providers.naver_search import MockNaverSearchProvider  # noqa: E402


# ---------------------------------------------------------------------------
# Env scope helper — mirrors test_m20_naver_search_provider._EnvScope.
# ---------------------------------------------------------------------------


class _EnvScope:
    KEYS = ("NAVER_SEARCH_ENABLED", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")

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


class _FakeFeed:
    """Minimal feedparser-result stand-in: only ``.entries`` is read."""

    def __init__(self, entries):
        self.entries = entries


def _rss_entry(title: str, *, recent: bool = True) -> dict:
    published = news_collector._utc_now_rfc2822() if recent else "Mon, 01 Jan 2001 00:00:00 GMT"
    return {
        "title": title,
        "summary": f"{title} 요약",
        "link": f"https://news.google.com/rss/articles/{abs(hash(title)) % 100000}",
        "published": published,
    }


def _naver_raw(title: str, originallink: str) -> dict:
    return {
        "title": title,
        "originallink": originallink,
        "link": "https://n.news.naver.com/mnews/article/001/0000000999",
        "description": f"{title} 설명",
        "pubDate": "Mon, 02 Jun 2025 09:30:00 +0900",
    }


# ---------------------------------------------------------------------------
# (a) Flag OFF — byte-identical control flow, provider never constructed.
# ---------------------------------------------------------------------------


class FlagOffTests(unittest.TestCase):
    def test_flag_off_no_provider_no_naver_key(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="false")
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=_FakeFeed([])), \
                 patch.object(news_collector, "search_naver_news_fallback", return_value=([], None)), \
                 patch.object(news_collector, "search_daum_news_fallback", return_value=([], None)), \
                 patch("providers.get_search_provider") as mock_get_provider:
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            mock_get_provider.assert_not_called()
            debug = out["debug"]
            # Disabled path: no Naver tier, falls through to emergency fallback
            # exactly as pre-M20-2.
            self.assertNotIn("naver_api_count", debug)
            self.assertEqual(debug["news_collection_mode"], "forced_search_fallback")
            self.assertEqual(debug["collection_source"], "forced_search_fallback")
            # Disabled cache key is byte-identical to the pre-M20-2 sha1 (no -nv).
            self.assertNotIn("-nv", debug["news_cache_key"])


# ---------------------------------------------------------------------------
# (b) Flag ON + RSS empty — Naver tier fires.
# ---------------------------------------------------------------------------


class FlagOnRssEmptyTests(unittest.TestCase):
    def test_naver_tier_selected_when_rss_empty(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            mock_provider = MockNaverSearchProvider(items=[_naver_raw("전세대출 규제 강화", "https://press.example.com/a/1")])
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=_FakeFeed([])), \
                 patch.object(news_collector, "search_naver_news_fallback", return_value=([], None)), \
                 patch.object(news_collector, "search_daum_news_fallback", return_value=([], None)), \
                 patch("providers.get_search_provider", return_value=mock_provider) as mock_get_provider:
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            mock_get_provider.assert_called_once_with("naver")
            debug = out["debug"]
            self.assertEqual(debug["news_collection_mode"], "naver_api")
            self.assertEqual(debug["collection_source"], "naver_api")
            self.assertEqual(debug["naver_api_count"], 1)
            self.assertEqual(len(out["results"]), 1)
            hit = out["results"][0]
            self.assertEqual(hit["source"], "naver_api")
            self.assertEqual(hit["original_url"], "https://press.example.com/a/1")
            self.assertEqual(hit["google_link"], hit["original_url"])


# ---------------------------------------------------------------------------
# (c) Flag ON + RSS works — Naver NOT invoked.
# ---------------------------------------------------------------------------


class FlagOnRssWorksTests(unittest.TestCase):
    def test_naver_not_invoked_when_rss_has_results(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            feed = _FakeFeed([_rss_entry("전세대출 금리 인하 발표", recent=True)])
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=feed), \
                 patch("providers.get_search_provider") as mock_get_provider:
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            mock_get_provider.assert_not_called()
            debug = out["debug"]
            self.assertEqual(debug["collection_source"], "google_rss")
            self.assertNotIn("naver_api_count", debug)
            self.assertGreaterEqual(len(out["results"]), 1)


# ---------------------------------------------------------------------------
# (d) Dedup — overlapping Naver URLs/titles collapsed.
# ---------------------------------------------------------------------------


class DedupTests(unittest.TestCase):
    def test_duplicate_naver_items_collapsed(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            items = [
                _naver_raw("전세대출 규제 강화", "https://press.example.com/dup/1"),
                _naver_raw("전세대출 규제 강화", "https://press.example.com/dup/1"),  # exact dup URL
                _naver_raw("전세대출 한도 확대", "https://press.example.com/other/2"),
            ]
            mock_provider = MockNaverSearchProvider(items=items)
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=_FakeFeed([])), \
                 patch.object(news_collector, "search_naver_news_fallback", return_value=([], None)), \
                 patch.object(news_collector, "search_daum_news_fallback", return_value=([], None)), \
                 patch("providers.get_search_provider", return_value=mock_provider):
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=5)

            urls = [r["original_url"] for r in out["results"]]
            # The exact-duplicate URL is collapsed by _dedupe_news_items.
            self.assertEqual(len(urls), len(set(urls)))
            self.assertEqual(len(out["results"]), 2)
            for r in out["results"]:
                self.assertEqual(r["google_link"], r["original_url"])

    def test_m17b_filters_zero_overlap_naver_items(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            # Title shares NO token with the query -> dropped by M17b.
            items = [_naver_raw("날씨 맑음 주말 나들이", "https://press.example.com/weather/9")]
            mock_provider = MockNaverSearchProvider(items=items)
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=_FakeFeed([])), \
                 patch.object(news_collector, "search_naver_news_fallback", return_value=([], None)), \
                 patch.object(news_collector, "search_daum_news_fallback", return_value=([], None)), \
                 patch("providers.get_search_provider", return_value=mock_provider):
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            # No Naver item survived M17b -> tier yields nothing -> emergency fallback.
            self.assertEqual(out["debug"]["news_collection_mode"], "forced_search_fallback")


# ---------------------------------------------------------------------------
# (e) Cache key segmentation.
# ---------------------------------------------------------------------------


class CacheSegmentationTests(unittest.TestCase):
    def _base_key(self, query: str, max_results: int) -> str:
        raw = f"{news_collector._normalize_query(query)}|{int(max_results or 0)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def test_disabled_key_byte_identical(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="false")
            self.assertEqual(news_collector._cache_key("전세대출", 3), self._base_key("전세대출", 3))

    def test_enabled_key_has_suffix(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            self.assertEqual(
                news_collector._cache_key("전세대출", 3), self._base_key("전세대출", 3) + "-nv",
            )

    def test_no_cross_contamination_on_toggle(self):
        # Entry stored under the disabled (base) key must NOT be served once
        # the flag is enabled (which looks up base+"-nv").
        base = self._base_key("전세대출", 3)
        fresh_entry = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "results": [{"title": "stale"}],
            "debug": {},
        }
        with _EnvScope():
            with patch.object(news_collector, "_load_news_cache", return_value={base: fresh_entry}):
                _set_env(NAVER_SEARCH_ENABLED="false")
                self.assertIsNotNone(news_collector._cached_news_response("전세대출", 3))
                _set_env(NAVER_SEARCH_ENABLED="true")
                self.assertIsNone(news_collector._cached_news_response("전세대출", 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
