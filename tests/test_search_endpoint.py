"""SEARCH-TO-ANALYZE Slice 1 — tests for GET /api/search.

Offline: the DB seam (_search_corpus_rows) is monkeypatched — no Postgres,
no live DB, no network, and the analyze pipeline is never invoked. Covers:
hit shape, no-match/empty-q/short-q -> {results: []}, never 500 on error,
ILIKE wildcard escaping (a bare "%" must not act as match-all), snippet
truncation, and the 200-char query cap.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402

FAKE_ROWS = [
    (2053, "청년도약계좌 확대 발표", "정부가 청년도약계좌 지원 대상을 확대한다는 보도",
     "2026-07-03T01:00:00+00:00", "draft"),
    (14, "금리 인하 검토", "한국은행이 기준금리 인하를 검토한다는 주장",
     None, "draft"),
]


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)


class SearchEndpointTests(_ClientMixin, unittest.TestCase):
    def test_hit_returns_slim_rows(self):
        with patch.object(api_server, "_search_corpus_rows",
                          return_value=list(FAKE_ROWS)) as seam:
            response = self.client.get("/api/search", params={"q": "청년도약"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["results"]), 2)
        first = body["results"][0]
        self.assertEqual(first["result_id"], 2053)
        self.assertEqual(first["title"], "청년도약계좌 확대 발표")
        self.assertIn("청년도약계좌", first["snippet"])
        self.assertEqual(first["published_at"], "2026-07-03T01:00:00+00:00")
        self.assertEqual(first["review_status"], "draft")
        seam.assert_called_once_with("청년도약")

    def test_no_match_returns_empty(self):
        with patch.object(api_server, "_search_corpus_rows", return_value=[]):
            response = self.client.get("/api/search", params={"q": "없는검색어"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    def test_empty_and_short_q_skip_db_entirely(self):
        with patch.object(api_server, "_search_corpus_rows") as seam:
            for q in ("", "   ", "a"):
                response = self.client.get("/api/search", params={"q": q})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"results": []})
        seam.assert_not_called()

    def test_error_returns_empty_not_500(self):
        with patch.object(api_server, "_search_corpus_rows",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/search", params={"q": "청년"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    def test_query_is_trimmed_and_capped_at_200_chars(self):
        long_q = "가" * 300
        with patch.object(api_server, "_search_corpus_rows",
                          return_value=[]) as seam:
            self.client.get("/api/search", params={"q": "  " + long_q + "  "})
        passed = seam.call_args[0][0]
        self.assertEqual(len(passed), 200)
        self.assertEqual(passed, "가" * 200)

    def test_snippet_truncated_with_ellipsis(self):
        row = (7, "제목", "긴" * 300, None, "draft")
        with patch.object(api_server, "_search_corpus_rows",
                          return_value=[row]):
            body = self.client.get("/api/search", params={"q": "제목"}).json()
        snippet = body["results"][0]["snippet"]
        self.assertEqual(len(snippet), 121)  # 120 chars + ellipsis
        self.assertTrue(snippet.endswith("…"))

    def test_no_store_cache_header(self):
        with patch.object(api_server, "_search_corpus_rows", return_value=[]):
            response = self.client.get("/api/search", params={"q": "청년"})
        self.assertEqual(response.headers.get("cache-control"), "no-store")


class WildcardEscapingTests(unittest.TestCase):
    """q is parameterized (bound as :pattern) and its LIKE metacharacters are
    escaped, so user-typed wildcards are literals."""

    def test_percent_is_escaped_not_match_all(self):
        self.assertEqual(api_server._build_search_pattern("%"), "%\\%%")

    def test_underscore_and_backslash_escaped(self):
        self.assertEqual(api_server._build_search_pattern("a_b"), "%a\\_b%")
        self.assertEqual(api_server._build_search_pattern("a\\b"), "%a\\\\b%")

    def test_plain_text_wrapped_for_substring_match(self):
        self.assertEqual(api_server._build_search_pattern("청년도약"), "%청년도약%")

    def test_mixed_metacharacters(self):
        self.assertEqual(api_server._build_search_pattern("50%_\\"),
                         "%50\\%\\_\\\\%")

    def test_statement_uses_bound_pattern_not_interpolation(self):
        # The SQL text must reference the bind params and ESCAPE clause;
        # the user string itself never appears in the statement.
        import inspect

        src = inspect.getsource(api_server._search_corpus_rows)
        self.assertIn(":pattern", src)
        self.assertIn("ESCAPE", src)
        self.assertIn(":row_limit", src)
        self.assertNotIn("f\"SELECT", src)
        self.assertNotIn("% q", src)
        self.assertNotIn(".format(", src)


if __name__ == "__main__":
    unittest.main()
