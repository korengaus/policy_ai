"""HOTTOPIC Phase 2b — unit tests for the news_collector-titles keyword selector.

news_collector and the Anthropic client are MOCKED throughout (no live API, no
live RSS). Tests pin the four safeguards, the fail-safe (any error -> []), the
flag-off byte-identical path, build_query_list dedup, and the robust JSON parse.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hot_topics


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _news_item(title, summary="", source="google_rss", link="https://news/x"):
    return {
        "title": title,
        "summary": summary,
        "google_link": link,
        "source": source,
        "published": "Tue, 09 Jun 2026 08:00:00 GMT",
    }


def _search_return(items, collection_source="google_rss"):
    return {"results": list(items), "debug": {"collection_source": collection_source}}


def _fake_search(items, collection_source="google_rss"):
    """Return a stand-in for search_google_news_rss_with_meta that ignores the
    seed and always returns the same pool (dedup collapses the 5 seed calls)."""
    def _inner(seed, max_results=8):
        return _search_return(items, collection_source)
    return _inner


def _pick_message(json_text, input_tokens=120, output_tokens=40):
    block = SimpleNamespace(type="text", text=json_text)
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=[block], usage=usage)


def _enabled_env(**overrides):
    env = {
        "HOT_TOPIC_ENABLED": "true",
        "HOT_TOPIC_TOP_K": "3",
        "ANTHROPIC_API_KEY": "test-key",
    }
    env.update(overrides)
    return env


# A clean 3-title policy pool used by several tests (index order preserved).
_POOL = [
    _news_item("부동산 세제 개편 실거주 원칙 시동", link="https://n/0"),
    _news_item("소상공인 지원 재원 확대 촉구", link="https://n/1"),
    _news_item("국민연금 보험료율 인상 논의 본격화", link="https://n/2"),
]


class FlagOffTests(unittest.TestCase):
    def test_disabled_returns_empty_and_never_fetches(self):
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "false"}, clear=False):
            with mock.patch.object(
                hot_topics, "search_google_news_rss_with_meta",
                side_effect=AssertionError("must not fetch when flag off"),
            ):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    side_effect=AssertionError("must not call LLM when flag off"),
                ):
                    self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_build_query_list_off_is_byte_identical(self):
        fixed = ["주택담보대출 규제", "복지 예산", "양도세 세제 개편"]
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "false"}, clear=False):
            self.assertEqual(hot_topics.build_query_list(fixed), fixed)


class HappyPathTests(unittest.TestCase):
    def test_filters_dedups_and_truncates(self):
        picks = [
            {"keyword": "부동산 세제 개편", "title_index": 0},
            {"keyword": "국민연금 보험료율 인상", "title_index": 2},
            {"keyword": "부동산 세제 개편", "title_index": 0},      # dup -> dropped
            {"keyword": "소상공인 지원 대책", "title_index": 1},
            {"keyword": "주택 공급 대책", "title_index": 0},          # past top_k=3
        ]
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_fake_search(_POOL)):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    return_value=_pick_message(json.dumps(picks, ensure_ascii=False)),
                ):
                    result = hot_topics.build_dynamic_queries()
        self.assertEqual(result, ["부동산 세제 개편", "국민연금 보험료율 인상", "소상공인 지원 대책"])


class SearchActuallyHappenedSafeguardTests(unittest.TestCase):
    def test_empty_titles_returns_empty_before_llm(self):
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_fake_search([])):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    side_effect=AssertionError("LLM must not be called with no titles"),
                ):
                    self.assertEqual(hot_topics.build_dynamic_queries(), [])


class ProvenanceSafeguardTests(unittest.TestCase):
    def test_out_of_range_or_missing_title_index_dropped(self):
        picks = [
            {"keyword": "부동산 세제 개편", "title_index": 99},   # out of range -> drop
            {"keyword": "소상공인 지원 대책"},                    # missing index -> drop
            {"keyword": "국민연금 보험료율 인상", "title_index": 2},  # valid -> keep
        ]
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_fake_search(_POOL)):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    return_value=_pick_message(json.dumps(picks, ensure_ascii=False)),
                ):
                    result = hot_topics.build_dynamic_queries()
        self.assertEqual(result, ["국민연금 보험료율 인상"])


class DomainFilterSafeguardTests(unittest.TestCase):
    def test_offtopic_and_non_policy_keywords_dropped(self):
        picks = [
            {"keyword": "코스피 증권 시황", "title_index": 0},     # denylist
            {"keyword": "오늘 날씨 정보", "title_index": 1},        # no allowlist term
            {"keyword": "미국 금리 인상 전망", "title_index": 2},  # deny beats allow
            {"keyword": "故 인물 추모 정책", "title_index": 0},    # obituary marker
            {"keyword": "부동산 세제 개편", "title_index": 0},     # keep
        ]
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_fake_search(_POOL)):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    return_value=_pick_message(json.dumps(picks, ensure_ascii=False)),
                ):
                    result = hot_topics.build_dynamic_queries()
        self.assertEqual(result, ["부동산 세제 개편"])


class EmergencyFallbackExclusionTests(unittest.TestCase):
    def test_seed_resolved_to_emergency_fallback_excluded(self):
        # collection_source == forced_search_fallback -> whole seed skipped.
        emerg = [_news_item("금융 정책 뉴스 검색 결과", source="forced_search_fallback")]
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(
                hot_topics, "search_google_news_rss_with_meta",
                new=_fake_search(emerg, collection_source="forced_search_fallback"),
            ):
                self.assertEqual(hot_topics._fetch_candidate_titles(), [])

    def test_per_item_emergency_source_excluded(self):
        mixed = [
            _news_item("부동산 세제 개편 실거주", source="google_rss", link="https://n/a"),
            _news_item("소상공인 지원 뉴스 검색 결과", source="forced_search_fallback"),
        ]
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(
                hot_topics, "search_google_news_rss_with_meta",
                new=_fake_search(mixed, collection_source="google_rss"),
            ):
                pooled = hot_topics._fetch_candidate_titles()
        titles = [p["title"] for p in pooled]
        self.assertEqual(titles, ["부동산 세제 개편 실거주"])


class FailSafeTests(unittest.TestCase):
    def test_llm_exception_returns_empty(self):
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_fake_search(_POOL)):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    side_effect=RuntimeError("anthropic down"),
                ):
                    self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_all_seed_fetches_fail_returns_empty(self):
        def _boom(seed, max_results=8):
            raise RuntimeError("rss down")
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_boom):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    side_effect=AssertionError("LLM must not be called with no titles"),
                ):
                    self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_malformed_json_returns_empty(self):
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "search_google_news_rss_with_meta", new=_fake_search(_POOL)):
                with mock.patch.object(
                    hot_topics, "_call_anthropic_pick",
                    return_value=_pick_message("not valid json ["),
                ):
                    self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_missing_api_key_returns_empty(self):
        env = _enabled_env()
        env.pop("ANTHROPIC_API_KEY")
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with mock.patch.object(
                hot_topics, "search_google_news_rss_with_meta",
                side_effect=AssertionError("must not fetch without key"),
            ):
                self.assertEqual(hot_topics.build_dynamic_queries(), [])


class BuildQueryListMergeTests(unittest.TestCase):
    def test_dedups_dynamic_equal_to_fixed(self):
        fixed = ["주택담보대출 규제", "복지 예산"]
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "true"}, clear=False):
            with mock.patch.object(
                hot_topics, "build_dynamic_queries",
                return_value=["주택담보대출 규제", "부동산 세제 개편"],
            ):
                result = hot_topics.build_query_list(fixed)
        self.assertEqual(result, ["주택담보대출 규제", "복지 예산", "부동산 세제 개편"])

    def test_fixed_order_preserved_and_dynamic_appended(self):
        fixed = ["a 대출", "b 복지"]
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "true"}, clear=False):
            with mock.patch.object(
                hot_topics, "build_dynamic_queries", return_value=["c 전세 지원"],
            ):
                result = hot_topics.build_query_list(fixed)
        self.assertEqual(result, ["a 대출", "b 복지", "c 전세 지원"])


class RobustJsonParseTests(unittest.TestCase):
    """_extract_json_array must handle fence/bare/raw/prose shapes identically."""

    _ITEMS = [{"keyword": "부동산 세제 개편", "title_index": 0}]

    def test_fenced_json_block(self):
        text = "```json\n" + json.dumps(self._ITEMS, ensure_ascii=False) + "\n```"
        self.assertEqual(hot_topics._extract_json_array(text), self._ITEMS)

    def test_bare_fence_block(self):
        text = "```\n" + json.dumps(self._ITEMS, ensure_ascii=False) + "\n```"
        self.assertEqual(hot_topics._extract_json_array(text), self._ITEMS)

    def test_raw_json_no_fence(self):
        text = json.dumps(self._ITEMS, ensure_ascii=False)
        self.assertEqual(hot_topics._extract_json_array(text), self._ITEMS)

    def test_json_embedded_in_prose(self):
        text = "결과: " + json.dumps(self._ITEMS, ensure_ascii=False) + " 입니다."
        self.assertEqual(hot_topics._extract_json_array(text), self._ITEMS)


if __name__ == "__main__":
    unittest.main()
