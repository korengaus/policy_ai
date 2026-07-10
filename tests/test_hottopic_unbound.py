"""HOTTOPIC-UNBOUND — tests for the domain-positive filter tightening and the
seedless Google News section-feed fetch/merge (default OFF).

Offline: RSS parsing and the seed search are monkeypatched — no network.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import hot_topics  # noqa: E402
import news_collector  # noqa: E402


def _fake_search(items):
    """Stand-in for search_google_news_rss_with_meta (ignores the query)."""
    def _search(query, max_results=3):
        return {"results": list(items), "debug": {"collection_source": "google_rss"}}
    return _search


_SEED_ITEMS = [
    {"title": "전세 대출 규제 강화", "summary": "s", "google_link": "https://g/1",
     "source": "google_rss", "published": "Fri, 10 Jul 2026 01:00:00 GMT"},
]
_SECTION_ITEMS = [
    {"title": "반도체 공급 확대 발표", "summary": "s2", "google_link": "https://g/2",
     "source": "google_rss", "published": "Fri, 10 Jul 2026 02:00:00 GMT"},
    # Duplicate of a seed title — the pool dedup must collapse it.
    {"title": "전세 대출 규제 강화", "summary": "dup", "google_link": "https://g/3",
     "source": "google_rss", "published": ""},
]


class DomainPositiveFilterTests(unittest.TestCase):
    def test_generic_verb_alone_now_fails(self):
        # "공급 확대" / "정책 개편" carry only generic verbs — the
        # HOTTOPIC-UNBOUND tightening drops them (no verifiable domain).
        self.assertFalse(hot_topics._passes_domain_filter("공급 확대"))
        self.assertFalse(hot_topics._passes_domain_filter("정책 개편"))

    def test_domain_keywords_still_pass(self):
        self.assertTrue(hot_topics._passes_domain_filter("전세 대출 지원"))
        self.assertTrue(hot_topics._passes_domain_filter("부동산 공급 대책"))
        self.assertTrue(hot_topics._passes_domain_filter("소상공인 지원금"))

    def test_denylist_still_wins(self):
        self.assertFalse(hot_topics._passes_domain_filter("코스피 금융 정책"))
        self.assertFalse(hot_topics._passes_domain_filter("이재명 부동산 대책"))

    def test_allowlist_recategorized_not_removed(self):
        self.assertEqual(
            set(hot_topics._ALLOWLIST),
            set(hot_topics._ALLOWLIST_DOMAIN) | set(hot_topics._ALLOWLIST_GENERIC),
        )
        self.assertIn("공급", hot_topics._ALLOWLIST_GENERIC)
        self.assertIn("전세", hot_topics._ALLOWLIST_DOMAIN)


class SectionFeedFetchTests(unittest.TestCase):
    def test_builds_seedless_section_url_and_returns_titles(self):
        captured = {}

        def _fake_parse(rss_url):
            captured["url"] = rss_url
            return SimpleNamespace(entries=[
                {"title": "제목 하나", "summary": "요약", "link": "https://g/a",
                 "published": "Fri, 10 Jul 2026 01:00:00 GMT"},
                {"title": "", "summary": "", "link": "", "published": ""},
            ])

        with mock.patch.object(news_collector, "_parse_google_news_rss",
                               new=_fake_parse):
            items = news_collector.fetch_google_news_topic_titles("business", 5)
        self.assertEqual(
            captured["url"],
            "https://news.google.com/rss/headlines/section/topic/"
            "BUSINESS?hl=ko&gl=KR&ceid=KR:ko",
        )
        # Seedless: no query anywhere in the URL.
        self.assertNotIn("q=", captured["url"])
        self.assertEqual(len(items), 1)  # blank-title entry dropped
        self.assertEqual(items[0]["title"], "제목 하나")
        self.assertEqual(items[0]["source"], "google_rss")

    def test_topic_is_sanitized(self):
        captured = {}
        with mock.patch.object(
            news_collector, "_parse_google_news_rss",
            new=lambda url: captured.update(url=url) or SimpleNamespace(entries=[]),
        ):
            news_collector.fetch_google_news_topic_titles("bad/../topic!", 5)
        self.assertIn("/topic/BADTOPIC?", captured["url"])

    def test_error_returns_empty(self):
        with mock.patch.object(news_collector, "_parse_google_news_rss",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(
                news_collector.fetch_google_news_topic_titles("BUSINESS", 5), [])

    def test_empty_topic_returns_empty_without_fetch(self):
        with mock.patch.object(news_collector, "_parse_google_news_rss") as parse:
            self.assertEqual(news_collector.fetch_google_news_topic_titles("", 5), [])
        parse.assert_not_called()


class SectionMergeGatingTests(unittest.TestCase):
    def _pool(self, section_env, section_fetch):
        env = {"HOT_TOPIC_ENABLED": "true"}
        env.update(section_env)
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta",
                                   new=_fake_search(_SEED_ITEMS)):
                with mock.patch.object(hot_topics, "fetch_google_news_topic_titles",
                                       new=section_fetch):
                    return hot_topics._fetch_candidate_titles()

    def test_default_off_section_not_fetched(self):
        fetch = mock.Mock(return_value=list(_SECTION_ITEMS))
        pooled = self._pool({"HOT_TOPIC_SECTION_ENABLED": ""}, fetch)
        fetch.assert_not_called()
        self.assertEqual([p["title"] for p in pooled], ["전세 대출 규제 강화"])

    def test_enabled_merges_and_dedups(self):
        fetch = mock.Mock(return_value=list(_SECTION_ITEMS))
        pooled = self._pool({"HOT_TOPIC_SECTION_ENABLED": "true"}, fetch)
        self.assertTrue(fetch.called)
        titles = [p["title"] for p in pooled]
        self.assertIn("반도체 공급 확대 발표", titles)
        # Dedup: the duplicated seed title appears exactly once.
        self.assertEqual(titles.count("전세 대출 규제 강화"), 1)
        # Default topics: BUSINESS + NATION -> two fetch calls.
        self.assertEqual(fetch.call_count, 2)

    def test_section_failure_is_failsoft(self):
        fetch = mock.Mock(side_effect=RuntimeError("feed down"))
        pooled = self._pool({"HOT_TOPIC_SECTION_ENABLED": "true"}, fetch)
        # Per-seed results survive a section-feed failure.
        self.assertEqual([p["title"] for p in pooled], ["전세 대출 규제 강화"])


class SectionConfigTests(unittest.TestCase):
    def test_default_disabled_and_default_topics(self):
        with mock.patch.dict(os.environ, {"HOT_TOPIC_SECTION_ENABLED": "",
                                          "HOT_TOPIC_SECTION_TOPICS": ""},
                             clear=False):
            self.assertFalse(config.hot_topic_section_enabled())
            self.assertEqual(config.hot_topic_section_topics(),
                             ["BUSINESS", "NATION"])

    def test_env_overrides(self):
        with mock.patch.dict(os.environ,
                             {"HOT_TOPIC_SECTION_ENABLED": "true",
                              "HOT_TOPIC_SECTION_TOPICS": "nation, politics"},
                             clear=False):
            self.assertTrue(config.hot_topic_section_enabled())
            self.assertEqual(config.hot_topic_section_topics(),
                             ["NATION", "POLITICS"])


if __name__ == "__main__":
    unittest.main()
