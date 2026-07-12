"""SPREAD-TIMELINE Slice 1 — tests for GET /api/spread/{analysis_id}.

Offline: the DB seams (_load_spread_indexes / _fetch_published_at) are
monkeypatched with synthetic fixtures — no Postgres, no live DB, no network.
Covers:
  * found case: shape + precomputed outlet_count + timeline aggregate,
  * not-found id -> {"found": false} (HTTP 200, never 500),
  * undated members excluded from the timeline but reported,
  * empty paths (no graph / PG off) and unexpected exceptions -> found:false,
  * pure index builder (membership from NODES, cluster_id 0 kept, singletons
    skipped),
  * honesty: no truth/검증 vocabulary in the payload.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402


# Synthetic graph fixture: cluster 0 (ids 1,2,3,4) + cluster 1 (ids 7,8) +
# singleton node id 9 (cluster_id null -> not spread-annotated).
FAKE_GRAPH = {
    "nodes": [
        {"id": 1, "cluster_id": 0},
        {"id": 2, "cluster_id": 0},
        {"id": 3, "cluster_id": 0},
        {"id": 4, "cluster_id": 0},
        {"id": 7, "cluster_id": 1},
        {"id": 8, "cluster_id": 1},
        {"id": 9, "cluster_id": None},
    ],
    "edges": [],
    "clusters": [
        {"cluster_id": 0, "stable_id": "abc123def456", "outlet_count": 3,
         "size": 4, "size_label": "3개 매체 보도 중", "kind": "spread"},
        {"cluster_id": 1, "stable_id": "fedcba654321", "outlet_count": 2,
         "size": 2, "size_label": "2개 매체 보도 중", "kind": "spread"},
    ],
}
FAKE_INDEXES = api_server._build_spread_indexes(FAKE_GRAPH)

# Cluster 0's member dates: two on 07-01, one on 07-03, one NULL (undated).
FAKE_PUBLISHED = [
    "2026-07-01T02:00:00+00:00",
    "2026-07-01T09:30:00+00:00",
    "2026-07-03T00:00:00+00:00",
    None,
]


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)


class SpreadFoundTests(_ClientMixin, unittest.TestCase):
    def _get(self, analysis_id):
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES), \
             patch.object(api_server, "_fetch_published_at",
                          return_value=list(FAKE_PUBLISHED)):
            return self.client.get(f"/api/spread/{analysis_id}")

    def test_found_shape_and_cluster_meta(self):
        response = self._get(2)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["found"])
        # outlet_count is the PRECOMPUTED graph value, never recomputed.
        self.assertEqual(body["cluster"]["outlet_count"], 3)
        self.assertEqual(body["cluster"]["stable_id"], "abc123def456")
        self.assertEqual(body["cluster"]["size"], 4)
        self.assertEqual(body["cluster"]["size_label"], "3개 매체 보도 중")

    def test_timeline_aggregate_and_undated_exclusion(self):
        body = self._get(2).json()
        timeline = body["timeline"]
        self.assertEqual(timeline["first_at"], "2026-07-01T02:00:00+00:00")
        self.assertEqual(timeline["last_at"], "2026-07-03T00:00:00+00:00")
        self.assertEqual(timeline["span_days"], 2)
        self.assertEqual(timeline["daily"], [
            {"date": "2026-07-01", "count": 2},
            {"date": "2026-07-03", "count": 1},
        ])
        # The NULL published_at member is OUT of the timeline but reported.
        self.assertEqual(timeline["dated_members"], 3)
        self.assertEqual(timeline["undated_members"], 1)

    def test_cluster_id_zero_is_a_real_cluster(self):
        # id 1 lives in cluster 0 — falsy cluster ids must not read as absent.
        body = self._get(1).json()
        self.assertTrue(body["found"])

    def test_cache_control_header(self):
        response = self._get(2)
        self.assertEqual(response.headers.get("cache-control"), "max-age=300")

    def test_no_truth_vocabulary_in_payload(self):
        text = self._get(2).text
        for word in ("검증", "confirmed", "verified", "truth", "probability"):
            self.assertNotIn(word, text)


class SpreadNotFoundTests(_ClientMixin, unittest.TestCase):
    def test_unclustered_id_returns_found_false(self):
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES):
            response = self.client.get("/api/spread/9999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_singleton_node_returns_found_false(self):
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES):
            response = self.client.get("/api/spread/9")
        self.assertEqual(response.json(), {"found": False})

    def test_no_graph_returns_found_false(self):
        # PG disabled / table missing / no row -> the loader returns None.
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=None):
            response = self.client.get("/api/spread/2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_unexpected_exception_returns_found_false_not_500(self):
        with patch.object(api_server, "_load_spread_indexes",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/spread/2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})


class SpreadPureHelperTests(unittest.TestCase):
    def test_build_spread_indexes_membership_from_nodes(self):
        indexes = api_server._build_spread_indexes(FAKE_GRAPH)
        self.assertEqual(indexes["cluster_of"][1], 0)
        self.assertEqual(indexes["cluster_of"][8], 1)
        self.assertNotIn(9, indexes["cluster_of"])  # singleton skipped
        self.assertEqual(indexes["members"][0], [1, 2, 3, 4])
        self.assertEqual(indexes["clusters"][1]["outlet_count"], 2)

    def test_build_spread_indexes_tolerates_empty_graph(self):
        indexes = api_server._build_spread_indexes({})
        self.assertEqual(indexes, {"clusters": {}, "members": {},
                                   "cluster_of": {}, "title_of": {}})

    def test_payload_all_undated_members(self):
        payload = api_server._build_spread_payload(
            {"stable_id": "x", "outlet_count": 2, "size": 2,
             "size_label": "2개 매체 보도 중"},
            [7, 8], [None, ""],
        )
        timeline = payload["timeline"]
        self.assertIsNone(timeline["first_at"])
        self.assertIsNone(timeline["span_days"])
        self.assertEqual(timeline["daily"], [])
        self.assertEqual(timeline["dated_members"], 0)
        self.assertEqual(timeline["undated_members"], 2)
        self.assertEqual(payload["cluster"]["outlet_count"], 2)

    def test_payload_json_stays_small(self):
        payload = api_server._build_spread_payload(
            FAKE_INDEXES["clusters"][0], [1, 2, 3, 4], list(FAKE_PUBLISHED),
        )
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 1024)


if __name__ == "__main__":
    unittest.main()
