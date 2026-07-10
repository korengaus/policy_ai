"""WEEKLY-REPORT Slice 1 — tests for GET /api/weekly-report[/{week_start}]
and the generator's pure ranking logic (scripts/generate_weekly_report.py).

Offline: the DB seam (_load_weekly_report_row) is monkeypatched; the
generator tests call build_report directly with a synthetic graph.
No Postgres, no live DB, no network.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import api_server  # noqa: E402
import generate_weekly_report as gwr  # noqa: E402

FAKE_PAYLOAD = {
    "week_start": "2026-07-04",
    "week_end": "2026-07-10",
    "framing": "확산 규모 기준 · 사실 검증 아님",
    "kind": "spread",
    "total_clusters_considered": 4,
    "qualifying_clusters": 2,
    "top": [
        {"rank": 1, "stable_id": "bbb", "title": "B-대표제목",
         "representative_analysis_id": 4, "outlet_count": 9,
         "window_member_count": 2, "first_at": "2026-07-05T00:00:00+00:00",
         "last_at": "2026-07-08T09:00:00+00:00"},
    ],
}


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)


class WeeklyReportEndpointTests(_ClientMixin, unittest.TestCase):
    def test_latest_returns_stored_payload(self):
        stored = json.dumps(FAKE_PAYLOAD, ensure_ascii=False)
        with patch.object(api_server, "_load_weekly_report_row",
                          return_value=stored) as loader:
            response = self.client.get("/api/weekly-report")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["found"])
        self.assertEqual(body["report"], FAKE_PAYLOAD)
        loader.assert_called_once_with(None)

    def test_week_param_is_passed_through(self):
        stored = json.dumps(FAKE_PAYLOAD, ensure_ascii=False)
        with patch.object(api_server, "_load_weekly_report_row",
                          return_value=stored) as loader:
            response = self.client.get("/api/weekly-report/2026-07-04")
        self.assertTrue(response.json()["found"])
        loader.assert_called_once_with("2026-07-04")

    def test_empty_returns_found_false_not_500(self):
        with patch.object(api_server, "_load_weekly_report_row",
                          return_value=None):
            response = self.client.get("/api/weekly-report")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_bad_stored_json_returns_found_false(self):
        with patch.object(api_server, "_load_weekly_report_row",
                          return_value="not json {"):
            response = self.client.get("/api/weekly-report")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_unexpected_exception_returns_found_false_not_500(self):
        with patch.object(api_server, "_load_weekly_report_row",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/weekly-report")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_cache_control_header(self):
        with patch.object(api_server, "_load_weekly_report_row",
                          return_value=None):
            response = self.client.get("/api/weekly-report")
        self.assertEqual(response.headers.get("cache-control"), "max-age=300")


class GeneratorRankingTests(unittest.TestCase):
    """The generator's --selftest covers the full matrix; here the pure
    build_report is exercised so pytest gates it too."""

    GRAPH = {
        "nodes": [
            {"id": 1, "cluster_id": 0, "title": "A-대표"},
            {"id": 2, "cluster_id": 0, "title": "A-기타"},
            {"id": 3, "cluster_id": 1, "title": "B-대표"},
            {"id": 4, "cluster_id": 1, "title": "B-기타"},
            {"id": 5, "cluster_id": 2, "title": "C-옛날"},
        ],
        "clusters": [
            {"cluster_id": 0, "stable_id": "aaa", "outlet_count": 2,
             "label_title": "A-대표", "size_label": "2개 매체 보도 중"},
            {"cluster_id": 1, "stable_id": "bbb", "outlet_count": 7,
             "label_title": "B-대표", "size_label": "7개 매체 보도 중"},
            {"cluster_id": 2, "stable_id": "ccc", "outlet_count": 4,
             "label_title": "C-옛날", "size_label": "4개 매체 보도 중"},
        ],
    }
    PUBLISHED = {
        1: "2026-07-05T00:00:00+00:00", 2: None,
        3: "2026-07-06T00:00:00+00:00", 4: "2026-07-09T00:00:00+00:00",
        5: "2026-02-01T00:00:00+00:00",
    }

    def _build(self):
        return gwr.build_report(self.GRAPH, self.PUBLISHED,
                                "2026-07-04", "2026-07-10", top_n=10)

    def test_ranking_by_outlet_count_and_window_filter(self):
        payload = self._build()
        self.assertEqual([e["stable_id"] for e in payload["top"]],
                         ["bbb", "aaa"])  # ccc out of window
        self.assertEqual([e["rank"] for e in payload["top"]], [1, 2])
        self.assertEqual(payload["qualifying_clusters"], 2)
        self.assertEqual(payload["total_clusters_considered"], 3)

    def test_representative_is_label_member(self):
        payload = self._build()
        by_sid = {e["stable_id"]: e for e in payload["top"]}
        self.assertEqual(by_sid["bbb"]["representative_analysis_id"], 3)
        self.assertEqual(by_sid["aaa"]["representative_analysis_id"], 1)

    def test_verdict_free_and_framing(self):
        payload = self._build()
        # "검증" may appear ONLY inside the framing disclaimer's negation
        # ("사실 검증 아님") — everywhere else the payload must be free of
        # verdict vocabulary and verdict-shaped keys.
        without_framing = {k: v for k, v in payload.items() if k != "framing"}
        blob = json.dumps(without_framing, ensure_ascii=False)
        for needle in ("verdict", "confidence", "truth_claim", "score",
                       "검증", "confirmed", "verified"):
            self.assertNotIn(needle, blob)
        self.assertEqual(payload["framing"], "확산 규모 기준 · 사실 검증 아님")
        self.assertTrue(gwr.honesty_guard_ok(payload))

    def test_top_n_truncation(self):
        payload = gwr.build_report(self.GRAPH, self.PUBLISHED,
                                   "2026-07-04", "2026-07-10", top_n=1)
        self.assertEqual(len(payload["top"]), 1)
        self.assertEqual(payload["top"][0]["stable_id"], "bbb")
        self.assertEqual(payload["qualifying_clusters"], 2)


if __name__ == "__main__":
    unittest.main()
