"""HOTTOPIC Phase 2 — unit tests for the hot_topics keyword selector.

The Anthropic client is MOCKED throughout (no live API). Tests pin the four
safeguards, the fail-safe (any error -> []), the flag-off byte-identical path,
and build_query_list dedup against the fixed queries.
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
# Fake Anthropic SDK message helpers (duck-typed; getattr-based access in
# hot_topics works on SimpleNamespace).
# ---------------------------------------------------------------------------
def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _search_use_block():
    return SimpleNamespace(type="server_tool_use", name="web_search")


def _search_result_block():
    return SimpleNamespace(type="web_search_tool_result")


def _usage(input_tokens=100, output_tokens=50, web_search_requests=2):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        server_tool_use=SimpleNamespace(web_search_requests=web_search_requests),
    )


def _message(blocks, usage=None):
    return SimpleNamespace(content=blocks, usage=usage or _usage())


def _fenced(items):
    """Wrap a list in a ```json fence exactly like the probe-observed output."""
    return "```json\n" + json.dumps(items, ensure_ascii=False) + "\n```"


def _enabled_env(**overrides):
    env = {
        "HOT_TOPIC_ENABLED": "true",
        "HOT_TOPIC_TOP_K": "3",
        "HOT_TOPIC_MAX_SEARCHES": "5",
        "ANTHROPIC_API_KEY": "test-key",
    }
    env.update(overrides)
    return env


class FlagOffTests(unittest.TestCase):
    def test_disabled_returns_empty_and_never_calls_api(self):
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "false"}, clear=False):
            with mock.patch.object(
                hot_topics, "_call_anthropic_web_search",
                side_effect=AssertionError("API must not be called when flag off"),
            ):
                self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_build_query_list_off_is_byte_identical(self):
        fixed = ["주택담보대출 규제", "복지 예산", "양도세 세제 개편"]
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "false"}, clear=False):
            result = hot_topics.build_query_list(fixed)
        self.assertEqual(result, fixed)


class WebSearchFiredSafeguardTests(unittest.TestCase):
    def test_happy_path_filters_dedups_and_truncates(self):
        items = [
            {"keyword": "햇살론 개편 서민금융", "source_url": "https://a.com/1"},
            {"keyword": "소상공인 세액공제 상가임대료", "source_url": "https://b.com/2"},
            {"keyword": "전세 대출 규제 완화", "source_url": "https://c.com/3"},
            {"keyword": "전세 대출 규제 완화", "source_url": "https://c.com/3b"},  # dup
            {"keyword": "주택 공급 대책", "source_url": "https://d.com/4"},  # past top_k=3
        ]
        msg = _message([_search_use_block(), _search_result_block(), _text_block(_fenced(items))])
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "_call_anthropic_web_search", return_value=msg):
                result = hot_topics.build_dynamic_queries()
        self.assertEqual(
            result,
            ["햇살론 개편 서민금융", "소상공인 세액공제 상가임대료", "전세 대출 규제 완화"],
        )

    def test_no_web_search_block_returns_empty(self):
        # Valid fenced JSON but NO server_tool_use / web_search_tool_result block.
        items = [{"keyword": "전세 대출 규제 완화", "source_url": "https://c.com/3"}]
        msg = _message([_text_block(_fenced(items))])
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "_call_anthropic_web_search", return_value=msg):
                self.assertEqual(hot_topics.build_dynamic_queries(), [])


class SourceUrlSafeguardTests(unittest.TestCase):
    def test_missing_empty_or_non_http_source_url_dropped(self):
        items = [
            {"keyword": "가계부채 DSR 규제", "source_url": ""},          # empty -> drop
            {"keyword": "청년 전세 지원금", "source_url": "ftp://x"},     # non-http -> drop
            {"keyword": "주담대 금리 인하"},                              # missing -> drop
            {"keyword": "부동산 양도세 개편", "source_url": "https://ok.com"},  # keep
        ]
        msg = _message([_search_use_block(), _text_block(_fenced(items))])
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "_call_anthropic_web_search", return_value=msg):
                result = hot_topics.build_dynamic_queries()
        self.assertEqual(result, ["부동산 양도세 개편"])


class DomainFilterSafeguardTests(unittest.TestCase):
    def test_offtopic_and_non_policy_keywords_dropped(self):
        items = [
            {"keyword": "코스피 증권 시황", "source_url": "https://x.com"},       # denylist
            {"keyword": "지방선거 공약", "source_url": "https://y.com"},          # denylist
            {"keyword": "아이돌 콘서트 일정", "source_url": "https://z.com"},     # denylist
            {"keyword": "손흥민 축구 국가대표", "source_url": "https://s.com"},   # denylist
            {"keyword": "오늘 날씨 정보", "source_url": "https://w.com"},         # no allowlist
            {"keyword": "미국 금리 인상 전망", "source_url": "https://m.com"},    # deny beats allow
            {"keyword": "부동산 양도세 개편", "source_url": "https://p.com"},     # keep
        ]
        msg = _message([_search_use_block(), _text_block(_fenced(items))])
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "_call_anthropic_web_search", return_value=msg):
                result = hot_topics.build_dynamic_queries()
        self.assertEqual(result, ["부동산 양도세 개편"])

    def test_obituary_marker_dropped(self):
        items = [
            {"keyword": "故 인물 정책 추모", "source_url": "https://o.com"},  # 故 obituary -> drop
            {"keyword": "소상공인 지원 대책", "source_url": "https://k.com"},  # keep
        ]
        msg = _message([_search_use_block(), _text_block(_fenced(items))])
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "_call_anthropic_web_search", return_value=msg):
                result = hot_topics.build_dynamic_queries()
        self.assertEqual(result, ["소상공인 지원 대책"])


class FailSafeTests(unittest.TestCase):
    def test_api_exception_returns_empty(self):
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(
                hot_topics, "_call_anthropic_web_search",
                side_effect=RuntimeError("network down"),
            ):
                self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_malformed_json_returns_empty(self):
        msg = _message([_search_use_block(), _text_block("```json\nnot valid json[\n```")])
        with mock.patch.dict(os.environ, _enabled_env(), clear=False):
            with mock.patch.object(hot_topics, "_call_anthropic_web_search", return_value=msg):
                self.assertEqual(hot_topics.build_dynamic_queries(), [])

    def test_missing_api_key_returns_empty(self):
        env = _enabled_env()
        env.pop("ANTHROPIC_API_KEY")
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with mock.patch.object(
                hot_topics, "_call_anthropic_web_search",
                side_effect=AssertionError("must not call API without key"),
            ):
                self.assertEqual(hot_topics.build_dynamic_queries(), [])


class BuildQueryListMergeTests(unittest.TestCase):
    def test_dedups_dynamic_equal_to_fixed(self):
        fixed = ["주택담보대출 규제", "복지 예산"]
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "true"}, clear=False):
            with mock.patch.object(
                hot_topics, "build_dynamic_queries",
                return_value=["주택담보대출 규제", "햇살론 개편 서민금융"],
            ):
                result = hot_topics.build_query_list(fixed)
        # Fixed first, dup removed, new dynamic appended.
        self.assertEqual(result, ["주택담보대출 규제", "복지 예산", "햇살론 개편 서민금융"])

    def test_fixed_order_preserved_and_dynamic_appended(self):
        fixed = ["a 대출", "b 복지"]
        with mock.patch.dict(os.environ, {"HOT_TOPIC_ENABLED": "true"}, clear=False):
            with mock.patch.object(
                hot_topics, "build_dynamic_queries", return_value=["c 전세 지원"],
            ):
                result = hot_topics.build_query_list(fixed)
        self.assertEqual(result, ["a 대출", "b 복지", "c 전세 지원"])


if __name__ == "__main__":
    unittest.main()
