"""FEED-SPREAD (12-APPLY) — tests for GET /api/cluster-spreads?ids=...

Offline: the DB seams (_load_spread_indexes, _fetch_published_at_by_id) are
monkeypatched with synthetic fixtures — no Postgres, no live DB, no network.
Mirrors tests/test_cluster_sizes_endpoint.py, including its honesty
source-scan. Covers:
  * batch map {id: spread facts} for in-cluster ids in ONE call,
  * ids not in the graph omitted; clusters below 2 outlets omitted,
  * timeline derived from members' published_at (NULLs excluded from the
    timeline but not from outlet_count/size),
  * ONE combined published_at query for all matched clusters,
  * daily capped at 14 buckets with an EXPLICIT daily_truncated flag while
    first_at/last_at keep the full range (truncation is never silent),
  * stable_id carried per id (feed fold key),
  * malformed / negative tokens ignored; id count capped at 60,
  * cluster_id 0 is a real cluster,
  * failed published_at read -> null timeline fields, counts still ship,
  * empty ids / no graph / unexpected exception -> {"spreads": {}} 200,
  * honesty: no verdict/score/confidence token in the endpoint source.
"""

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402


# Cluster 0 (ids 1,2) outlets=3; cluster 1 (ids 7,8) outlets=1 (below the
# >=2 gate -> omitted); singleton id 9; cluster 2 (ids 20,21,22) outlets=2
# with one NULL published_at member and a multi-day spread.
FAKE_GRAPH = {
    "nodes": [
        {"id": 1, "cluster_id": 0, "title": "a"},
        {"id": 2, "cluster_id": 0, "title": "b"},
        {"id": 7, "cluster_id": 1, "title": "c"},
        {"id": 8, "cluster_id": 1, "title": "d"},
        {"id": 9, "cluster_id": None, "title": "singleton"},
        {"id": 20, "cluster_id": 2, "title": "e"},
        {"id": 21, "cluster_id": 2, "title": "f"},
        {"id": 22, "cluster_id": 2, "title": "g"},
    ],
    "edges": [],
    "clusters": [
        {"cluster_id": 0, "stable_id": "abc123def456", "outlet_count": 3,
         "size": 2, "kind": "spread"},
        {"cluster_id": 1, "stable_id": "fedcba654321", "outlet_count": 1,
         "size": 2, "kind": "spread"},
        {"cluster_id": 2, "stable_id": "22cafe22beef", "outlet_count": 2,
         "size": 3, "kind": "spread"},
    ],
}
FAKE_INDEXES = api_server._build_spread_indexes(FAKE_GRAPH)

FAKE_PUBLISHED = {
    1: "2026-08-01T09:00:00",
    2: "2026-08-03T10:00:00",
    20: "2026-08-01T08:00:00",
    21: "2026-08-01T12:00:00",
    22: None,  # dateless member: excluded from timeline, kept in counts
}


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def _get(self, query, indexes=FAKE_INDEXES, published=FAKE_PUBLISHED):
        def fake_fetch(member_ids):
            return {mid: published.get(mid) for mid in member_ids}

        with patch.object(api_server, "_load_spread_indexes",
                          return_value=indexes), \
             patch.object(api_server, "_fetch_published_at_by_id",
                          side_effect=fake_fetch):
            return self.client.get(f"/api/cluster-spreads{query}")


class ClusterSpreadsBatchTests(_ClientMixin, unittest.TestCase):
    def test_batch_map_for_in_cluster_ids(self):
        response = self._get("?ids=1,2,9,9999")
        self.assertEqual(response.status_code, 200)
        spreads = response.json()["spreads"]
        # ids 1,2 -> cluster 0; 9 singleton + 9999 unknown omitted.
        self.assertEqual(sorted(spreads.keys()), ["1", "2"])
        self.assertEqual(spreads["1"], {
            "outlet_count": 3,
            "size": 2,
            "first_at": "2026-08-01T09:00:00",
            "last_at": "2026-08-03T10:00:00",
            "span_days": 2,
            "daily": [{"date": "2026-08-01", "count": 1},
                      {"date": "2026-08-03", "count": 1}],
            "daily_truncated": False,
            "stable_id": "abc123def456",
        })

    def test_below_two_outlets_omitted(self):
        self.assertEqual(self._get("?ids=7,8").json(), {"spreads": {}})

    def test_cluster_id_zero_is_a_real_cluster(self):
        self.assertIn("1", self._get("?ids=1").json()["spreads"])

    def test_null_published_member_excluded_from_timeline_not_counts(self):
        spread = self._get("?ids=20").json()["spreads"]["20"]
        self.assertEqual(spread["outlet_count"], 2)
        self.assertEqual(spread["size"], 3)  # id 22 (NULL date) still counted
        self.assertEqual(spread["span_days"], 0)
        self.assertEqual(spread["daily"], [{"date": "2026-08-01", "count": 2}])

    def test_one_combined_published_query_for_all_clusters(self):
        calls = []

        def spy(member_ids):
            calls.append(sorted(member_ids))
            return {mid: FAKE_PUBLISHED.get(mid) for mid in member_ids}

        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES), \
             patch.object(api_server, "_fetch_published_at_by_id",
                          side_effect=spy):
            self.client.get("/api/cluster-spreads?ids=1,20")
        # ONE call, pooling BOTH clusters' members.
        self.assertEqual(calls, [[1, 2, 20, 21, 22]])

    def test_daily_capped_with_explicit_truncation_flag(self):
        published = {1: "2026-06-01T00:00:00"}
        # 20 distinct days -> 20 buckets; member 2 carries the newest date so
        # first_at/last_at span the FULL range while daily keeps 14 buckets.
        graph = {
            "nodes": [{"id": i, "cluster_id": 0, "title": "n"}
                      for i in range(1, 21)],
            "edges": [],
            "clusters": [{"cluster_id": 0, "stable_id": "cap", "outlet_count": 2,
                          "size": 20}],
        }
        for i in range(2, 21):
            published[i] = f"2026-06-{i:02d}T00:00:00"
        spread = self._get("?ids=1", indexes=api_server._build_spread_indexes(graph),
                           published=published).json()["spreads"]["1"]
        self.assertEqual(len(spread["daily"]), 14)
        self.assertTrue(spread["daily_truncated"])
        # full range preserved: the series visibly does not span it
        self.assertEqual(spread["first_at"], "2026-06-01T00:00:00")
        self.assertEqual(spread["last_at"], "2026-06-20T00:00:00")
        self.assertEqual(spread["daily"][0]["date"], "2026-06-07")  # most recent 14

    def test_failed_published_read_degrades_to_null_timeline(self):
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES), \
             patch.object(api_server, "_fetch_published_at_by_id",
                          return_value={}):
            spread = self.client.get(
                "/api/cluster-spreads?ids=1").json()["spreads"]["1"]
        self.assertEqual(spread["outlet_count"], 3)
        self.assertIsNone(spread["first_at"])
        self.assertIsNone(spread["last_at"])
        self.assertIsNone(spread["span_days"])
        self.assertEqual(spread["daily"], [])
        self.assertFalse(spread["daily_truncated"])

    def test_malformed_tokens_ignored(self):
        response = self._get("?ids=1,abc,-5,,2.5, 2 ")
        self.assertEqual(sorted(response.json()["spreads"].keys()), ["1", "2"])

    def test_id_count_capped_at_60(self):
        query = "?ids=" + ",".join(str(10000 + i) for i in range(70)) + ",1"
        self.assertEqual(self._get(query).json(), {"spreads": {}})

    def test_cache_control_header(self):
        self.assertEqual(self._get("?ids=1").headers.get("cache-control"),
                         "max-age=300")


class ClusterSpreadsEmptyTests(_ClientMixin, unittest.TestCase):
    def test_no_ids_param(self):
        response = self._get("")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"spreads": {}})

    def test_empty_ids_param(self):
        self.assertEqual(self._get("?ids=").json(), {"spreads": {}})

    def test_no_graph_returns_empty_200(self):
        response = self._get("?ids=1,2", indexes=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"spreads": {}})

    def test_unexpected_exception_returns_empty_200_not_500(self):
        with patch.object(api_server, "_load_spread_indexes",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/cluster-spreads?ids=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"spreads": {}})


class ClusterSpreadsHonestyTests(unittest.TestCase):
    def test_no_verdict_column_in_endpoint_source(self):
        # Mirrors ClusterSizesHonestyTests: circulation only, no judgement
        # axis anywhere in the handler or its batch helper.
        source = (inspect.getsource(api_server.cluster_spreads)
                  + inspect.getsource(api_server._fetch_published_at_by_id))
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "operator_review_required",
                       "has_genuine_official_support"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
