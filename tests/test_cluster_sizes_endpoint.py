"""CLUSTER-SURFACE S-b — tests for GET /api/cluster-sizes?ids=...

Offline: the DB seam (_load_spread_indexes) is monkeypatched with synthetic
fixtures — no Postgres, no live DB, no network. Covers:
  * batch map {id: outlet_count} for in-cluster ids in ONE call,
  * ids not in the graph omitted from the map,
  * clusters below 2 outlets omitted (mirrors the spread >=2 gate),
  * malformed / negative tokens ignored,
  * id count capped at 60,
  * cluster_id 0 is a real cluster,
  * empty ids / no graph / unexpected exception -> {"sizes": {}} 200,
  * honesty: no verdict column in the endpoint source.
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
# >=2 gate -> omitted); singleton id 9.
FAKE_GRAPH = {
    "nodes": [
        {"id": 1, "cluster_id": 0, "title": "a"},
        {"id": 2, "cluster_id": 0, "title": "b"},
        {"id": 7, "cluster_id": 1, "title": "c"},
        {"id": 8, "cluster_id": 1, "title": "d"},
        {"id": 9, "cluster_id": None, "title": "singleton"},
    ],
    "edges": [],
    "clusters": [
        {"cluster_id": 0, "stable_id": "abc123def456", "outlet_count": 3,
         "size": 2, "kind": "spread"},
        {"cluster_id": 1, "stable_id": "fedcba654321", "outlet_count": 1,
         "size": 2, "kind": "spread"},
    ],
}
FAKE_INDEXES = api_server._build_spread_indexes(FAKE_GRAPH)


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def _get(self, query, indexes=FAKE_INDEXES):
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=indexes):
            return self.client.get(f"/api/cluster-sizes{query}")


class ClusterSizesBatchTests(_ClientMixin, unittest.TestCase):
    def test_batch_map_for_in_cluster_ids(self):
        response = self._get("?ids=1,2,9,9999")
        self.assertEqual(response.status_code, 200)
        # ids 1,2 -> cluster 0 (3 outlets); 9 singleton + 9999 unknown omitted.
        self.assertEqual(response.json(), {"sizes": {"1": 3, "2": 3}})

    def test_below_two_outlets_omitted(self):
        # Cluster 1 has outlet_count 1 -> no chip-worthy size returned.
        self.assertEqual(self._get("?ids=7,8").json(), {"sizes": {}})

    def test_cluster_id_zero_is_a_real_cluster(self):
        self.assertEqual(self._get("?ids=1").json()["sizes"], {"1": 3})

    def test_malformed_tokens_ignored(self):
        response = self._get("?ids=1,abc,-5,,2.5, 2 ")
        self.assertEqual(response.json(), {"sizes": {"1": 3, "2": 3}})

    def test_id_count_capped_at_60(self):
        # 70 unknown ids then id 1: the cap drops everything past 60, so id 1
        # (position 71) is never looked up.
        query = "?ids=" + ",".join(str(10000 + i) for i in range(70)) + ",1"
        self.assertEqual(self._get(query).json(), {"sizes": {}})

    def test_cache_control_header(self):
        self.assertEqual(self._get("?ids=1").headers.get("cache-control"),
                         "max-age=300")


class ClusterSizesEmptyTests(_ClientMixin, unittest.TestCase):
    def test_no_ids_param(self):
        response = self._get("")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sizes": {}})

    def test_empty_ids_param(self):
        self.assertEqual(self._get("?ids=").json(), {"sizes": {}})

    def test_no_graph_returns_empty_200(self):
        response = self._get("?ids=1,2", indexes=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sizes": {}})

    def test_unexpected_exception_returns_empty_200_not_500(self):
        with patch.object(api_server, "_load_spread_indexes",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/cluster-sizes?ids=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"sizes": {}})


class ClusterSizesHonestyTests(unittest.TestCase):
    def test_no_verdict_column_in_endpoint_source(self):
        source = inspect.getsource(api_server.cluster_sizes)
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "operator_review_required",
                       "has_genuine_official_support"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
