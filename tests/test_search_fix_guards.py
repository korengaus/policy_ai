"""SEARCH-FIX Slice B — tests for the two junk guards.

1. Save-quality gate (database.save_analysis_result): SERP-fallback-shaped
   results (forced_search_fallback source, or original_url on a search-engine
   host) are NOT persisted; normal results are. DB seams are monkeypatched —
   no live DB.
2. Pre-analysis CJK guard (api_server): an all-Latin query gets HTTP 400 on
   all three analyze endpoints BEFORE the pipeline; a Korean query passes
   validation. The rate limiter is dependency-overridden so test requests
   never 429.
Both guards are verdict-isolated: the tests assert the result object's
verdict fields are untouched.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402
import database  # noqa: E402


def _result(original_url, collection_source):
    return {
        "title": "테스트 기사",
        "original_url": original_url,
        "topic": "금융",
        "verdict_label": "부분 사실",
        "verification_card": {
            "claim_text": "테스트 주장",
            "verdict_label": "부분 사실",
            "debug_summary": {"collection_source": collection_source},
        },
    }


class SaveQualityGateTests(unittest.TestCase):
    def _save(self, result):
        with patch.object(database, "result_exists_by_url",
                          return_value=False), \
             patch.object(database, "_mirror_write_returning_safe",
                          return_value=123) as writer:
            status = database.save_analysis_result(result, query="테스트")
        return status, writer

    def test_forced_search_fallback_source_not_persisted(self):
        result = _result("https://news.example.com/a1",
                         "forced_search_fallback")
        status, writer = self._save(result)
        self.assertEqual(status, {"saved": False, "duplicate": False,
                                  "id": None, "skipped_low_quality": True})
        writer.assert_not_called()

    def test_naver_serp_host_not_persisted(self):
        result = _result(
            "https://search.naver.com/search.naver?where=news&query=x",
            "naver_api",
        )
        status, writer = self._save(result)
        self.assertTrue(status["skipped_low_quality"])
        writer.assert_not_called()

    def test_daum_serp_host_not_persisted(self):
        result = _result("https://search.daum.net/search?w=news&q=x",
                         "daum_fallback")
        status, writer = self._save(result)
        self.assertTrue(status["skipped_low_quality"])
        writer.assert_not_called()

    def test_normal_result_is_persisted(self):
        result = _result("https://news.example.com/real-article",
                         "naver_api")
        status, writer = self._save(result)
        self.assertEqual(status, {"saved": True, "duplicate": False,
                                  "id": 123})
        writer.assert_called_once()

    def test_gate_is_verdict_isolated(self):
        # The refused result object keeps its verdict fields byte-identical —
        # the gate only refuses persistence.
        result = _result("https://search.naver.com/search.naver?query=x",
                         "forced_search_fallback")
        self._save(result)
        self.assertEqual(result["verdict_label"], "부분 사실")
        self.assertEqual(result["verification_card"]["verdict_label"],
                         "부분 사실")
        self.assertEqual(
            result["verification_card"]["debug_summary"]["collection_source"],
            "forced_search_fallback",
        )

    def test_detector_shapes(self):
        self.assertTrue(database._is_serp_fallback_result(
            {"original_url": "https://www.search.naver.com/x"}))
        self.assertFalse(database._is_serp_fallback_result(
            {"original_url": "https://n.news.naver.com/article/001/1"}))
        self.assertFalse(database._is_serp_fallback_result({}))


class CjkQueryGuardTests(unittest.TestCase):
    def setUp(self):
        api_server.app.dependency_overrides[api_server.analyze_rate_limiter] = (
            lambda: None
        )

    def tearDown(self):
        api_server.app.dependency_overrides.pop(
            api_server.analyze_rate_limiter, None
        )

    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def test_all_latin_query_rejected_on_all_three_endpoints(self):
        for path in ("/analyze", "/jobs/analyze", "/v2/analyze"):
            response = self.client.post(
                path, json={"query": "asdfqwer1234", "max_news": 1},
            )
            self.assertEqual(response.status_code, 400, path)
            self.assertEqual(response.json()["detail"],
                             "정책 뉴스와 관련된 검색어를 입력해주세요.")

    def test_korean_query_passes_validation(self):
        with patch.object(api_server, "analyze_pipeline",
                          return_value={"news_results": []}) as pipeline:
            response = self.client.post(
                "/analyze", json={"query": "청년도약계좌", "max_news": 1},
            )
        self.assertEqual(response.status_code, 200)
        pipeline.assert_called_once()

    def test_guard_runs_before_pipeline(self):
        with patch.object(api_server, "analyze_pipeline") as pipeline:
            self.client.post(
                "/analyze", json={"query": "asdfqwer1234", "max_news": 1},
            )
        pipeline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
