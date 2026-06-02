"""Tests for M20-3 — promoting the Naver SearchProvider to PRIMARY (Option B1).

Run with: python tests/test_m20_3_naver_primary.py

B1: when NAVER_SEARCH_ENABLED is true, Naver is tried FIRST (above the Google
RSS ladder); if it returns on-topic items (M17b filter, sort=date) it wins and
the RSS ladder is skipped; otherwise ``selected`` stays empty and the RSS
ladder + existing fallbacks take over. Disabled → byte-identical to today.

Covers:
(a) Flag OFF  -> RSS primary, provider never called, no naver_api_count.
(b) Flag ON + RSS has results + Naver on-topic -> Naver WINS (promotion proof).
(c) On-topic guarantee -> off-topic Naver item dropped by M17b, RSS wins; the
    off-topic article never appears.
(d) sort=date -> provider.search called with sort="date".
(e) Dedup -> duplicate Naver URLs/titles collapsed; google_link==original_url.
(f) Cache segmentation -> disabled base key, enabled +"-nv", no contamination.

NO real API call is ever made — get_search_provider is patched.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import news_collector  # noqa: E402
from providers.naver_search import MockNaverSearchProvider  # noqa: E402


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
# (a) Flag OFF — RSS primary, provider never called.
# ---------------------------------------------------------------------------


class FlagOffTests(unittest.TestCase):
    def test_flag_off_rss_primary_no_provider(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="false")
            feed = _FakeFeed([_rss_entry("전세대출 규제 발표", recent=True)])
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=feed), \
                 patch("providers.get_search_provider") as mock_get_provider:
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            mock_get_provider.assert_not_called()
            debug = out["debug"]
            self.assertEqual(debug["collection_source"], "google_rss")
            self.assertNotIn("naver_api_count", debug)
            self.assertEqual(len(out["results"]), 1)


# ---------------------------------------------------------------------------
# (b) Flag ON + RSS has results + Naver on-topic -> Naver WINS.
# ---------------------------------------------------------------------------


class NaverPrimaryWinsTests(unittest.TestCase):
    def test_naver_wins_even_when_rss_has_results(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            feed = _FakeFeed([_rss_entry("전세대출 금리 인하", recent=True)])
            provider = MockNaverSearchProvider(
                items=[_naver_raw("전세대출 규제 강화", "https://press.example.com/n/1")]
            )
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=feed), \
                 patch("providers.get_search_provider", return_value=provider) as mock_get_provider:
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            mock_get_provider.assert_called_once_with("naver")
            debug = out["debug"]
            self.assertEqual(debug["news_collection_mode"], "naver_api")
            self.assertEqual(debug["collection_source"], "naver_api")
            self.assertEqual(debug["naver_api_count"], 1)
            self.assertEqual(out["results"][0]["original_url"], "https://press.example.com/n/1")
            self.assertEqual(out["results"][0]["source"], "naver_api")


# ---------------------------------------------------------------------------
# (c) On-topic guarantee — off-topic Naver item dropped, RSS wins.
# ---------------------------------------------------------------------------


class OnTopicGuaranteeTests(unittest.TestCase):
    def test_offtopic_naver_item_never_promoted(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            # Naver returns an UNRELATED murder-case article (sim-style noise):
            # its title shares no token with "전세사기" -> dropped by M17b.
            provider = MockNaverSearchProvider(
                items=[_naver_raw("강남 오피스텔 살인 사건 용의자 검거", "https://press.example.com/crime/7")]
            )
            # RSS returns an on-topic article that should become the primary card.
            feed = _FakeFeed([_rss_entry("전세사기 피해자 지원 대책 발표", recent=True)])
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=feed), \
                 patch.object(news_collector, "search_naver_news_fallback", return_value=([], None)), \
                 patch.object(news_collector, "search_daum_news_fallback", return_value=([], None)), \
                 patch("providers.get_search_provider", return_value=provider) as mock_get_provider:
                out = news_collector.search_google_news_rss_with_meta("전세사기", max_results=3)

            mock_get_provider.assert_called_once_with("naver")
            debug = out["debug"]
            # Naver was tried but yielded nothing on-topic -> RSS takes over.
            self.assertEqual(debug["collection_source"], "google_rss")
            self.assertNotIn("naver_api_count", debug)
            titles = [r.get("title", "") for r in out["results"]]
            self.assertTrue(all("살인" not in t for t in titles))
            self.assertTrue(any("전세사기" in t for t in titles))


# ---------------------------------------------------------------------------
# (d) sort=date is requested for the primary call.
# ---------------------------------------------------------------------------


class SortParamTests(unittest.TestCase):
    def test_primary_call_uses_sort_date(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            provider = MockNaverSearchProvider(
                items=[_naver_raw("전세대출 규제", "https://press.example.com/s/1")]
            )
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=_FakeFeed([])), \
                 patch.object(provider, "search", wraps=provider.search) as spy, \
                 patch("providers.get_search_provider", return_value=provider):
                news_collector.search_google_news_rss_with_meta("전세대출", max_results=3)

            spy.assert_called_once()
            self.assertEqual(spy.call_args.kwargs.get("sort"), "date")
            self.assertEqual(spy.call_args.kwargs.get("limit"), 3)


# ---------------------------------------------------------------------------
# (e) Dedup across Naver items.
# ---------------------------------------------------------------------------


class DedupTests(unittest.TestCase):
    def test_duplicate_naver_items_collapsed(self):
        with _EnvScope():
            _set_env(NAVER_SEARCH_ENABLED="true")
            items = [
                _naver_raw("전세대출 규제 강화", "https://press.example.com/dup/1"),
                _naver_raw("전세대출 규제 강화", "https://press.example.com/dup/1"),
                _naver_raw("전세대출 한도 확대", "https://press.example.com/other/2"),
            ]
            provider = MockNaverSearchProvider(items=items)
            with patch.object(news_collector, "_cached_news_response", return_value=None), \
                 patch.object(news_collector, "_store_news_response"), \
                 patch.object(news_collector, "_parse_google_news_rss", return_value=_FakeFeed([])), \
                 patch("providers.get_search_provider", return_value=provider):
                out = news_collector.search_google_news_rss_with_meta("전세대출", max_results=5)

            urls = [r["original_url"] for r in out["results"]]
            self.assertEqual(len(urls), len(set(urls)))
            self.assertEqual(len(out["results"]), 2)
            for r in out["results"]:
                self.assertEqual(r["google_link"], r["original_url"])


# ---------------------------------------------------------------------------
# (f) Cache segmentation (reconfirm under primary semantics).
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
