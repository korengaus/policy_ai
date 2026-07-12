"""CLUSTER-SURFACE S-a — tests for GET /api/cluster/{result_id}/members.

Offline: the DB seam (_load_spread_indexes) is monkeypatched with synthetic
fixtures — no Postgres, no live DB, no network. Covers:
  * found case: sibling ids + titles from title_of, SELF excluded, min-id
    sorted, cluster meta carried, honesty note present,
  * cap at 10 siblings,
  * cluster_id 0 is a real cluster,
  * empty-title member -> "" (client falls back),
  * not-in-graph id / singleton cluster / no graph / stale cache without
    title_of / unexpected exception -> {"found": false, "members": []} 200,
  * honesty: no verdict column in the endpoint source, no truth vocabulary
    in the payload.
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


# Synthetic graph: cluster 0 (ids 1,2,3,4) + cluster 1 (ids 7,8) + a
# 13-member cluster 2 (ids 100..112, for the cap) + singleton id 9.
FAKE_GRAPH = {
    "nodes": (
        [{"id": 1, "cluster_id": 0, "title": "청년 지원금 첫 보도"},
         {"id": 2, "cluster_id": 0, "title": "청년 지원금 후속"},
         {"id": 3, "cluster_id": 0, "title": ""},
         {"id": 4, "cluster_id": 0, "title": "청년 지원금 심층"},
         {"id": 7, "cluster_id": 1, "title": "전세 대출 a"},
         {"id": 8, "cluster_id": 1, "title": "전세 대출 b"},
         {"id": 9, "cluster_id": None, "title": "singleton"}]
        + [{"id": 100 + i, "cluster_id": 2, "title": f"big-{i}"}
           for i in range(13)]
    ),
    "edges": [],
    "clusters": [
        {"cluster_id": 0, "stable_id": "abc123def456", "outlet_count": 3,
         "size": 4, "kind": "spread"},
        {"cluster_id": 1, "stable_id": "fedcba654321", "outlet_count": 2,
         "size": 2, "kind": "spread"},
        {"cluster_id": 2, "stable_id": "big000big000", "outlet_count": 13,
         "size": 13, "kind": "spread"},
    ],
}
FAKE_INDEXES = api_server._build_spread_indexes(FAKE_GRAPH)


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def _get(self, result_id, indexes=FAKE_INDEXES):
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=indexes):
            return self.client.get(f"/api/cluster/{result_id}/members")


class ClusterMembersFoundTests(_ClientMixin, unittest.TestCase):
    def test_siblings_exclude_self_sorted_with_titles(self):
        response = self._get(2)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["found"])
        self.assertEqual([m["analysis_id"] for m in body["members"]],
                         [1, 3, 4])  # self (2) excluded, min-id sorted
        self.assertEqual(body["members"][0]["title"], "청년 지원금 첫 보도")
        self.assertEqual(body["members"][1]["title"], "")  # empty stays ""
        self.assertEqual(body["cluster"], {"stable_id": "abc123def456",
                                           "outlet_count": 3})
        self.assertEqual(body["note"], "같은 주장을 다룬 다른 보도 — 검증이 아닙니다")

    def test_cluster_id_zero_is_a_real_cluster(self):
        self.assertTrue(self._get(1).json()["found"])

    def test_sibling_cap_at_10(self):
        body = self._get(100).json()
        self.assertEqual(len(body["members"]), 10)
        self.assertEqual(body["members"][0]["analysis_id"], 101)

    def test_cache_control_header(self):
        self.assertEqual(self._get(2).headers.get("cache-control"),
                         "max-age=300")


class ClusterMembersEmptyTests(_ClientMixin, unittest.TestCase):
    def _assert_empty_200(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False, "members": []})

    def test_id_not_in_graph(self):
        self._assert_empty_200(self._get(9999))

    def test_singleton_node(self):
        self._assert_empty_200(self._get(9))

    def test_two_member_cluster_still_returns_the_other(self):
        body = self._get(7).json()
        self.assertEqual([m["analysis_id"] for m in body["members"]], [8])

    def test_no_graph_returns_empty_200(self):
        self._assert_empty_200(self._get(2, indexes=None))

    def test_stale_cache_without_title_of_still_answers(self):
        # An in-process cache entry built before title_of existed.
        stale = {k: v for k, v in FAKE_INDEXES.items() if k != "title_of"}
        body = self._get(2, indexes=stale).json()
        self.assertTrue(body["found"])
        self.assertTrue(all(m["title"] == "" for m in body["members"]))

    def test_unexpected_exception_returns_empty_200_not_500(self):
        with patch.object(api_server, "_load_spread_indexes",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/cluster/2/members")
        self._assert_empty_200(response)


class ClusterMembersHonestyTests(_ClientMixin, unittest.TestCase):
    def test_no_truth_vocabulary_in_payload(self):
        text = self._get(2).text
        # The note itself says 검증이 아닙니다 — assert no verdict-shaped fields.
        for word in ("verdict", "confidence", "truth", "probability",
                     "score"):
            self.assertNotIn(word, text)

    def test_no_verdict_column_in_endpoint_source(self):
        source = inspect.getsource(api_server.cluster_members) + inspect.getsource(
            api_server._build_spread_indexes)
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "operator_review_required",
                       "has_genuine_official_support"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
