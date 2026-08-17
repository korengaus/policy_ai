"""HERO-PICK (70-APPLY) — tests for GET /api/hero-pick.

Offline: the DB seam (_load_hero_pick_candidates) is monkeypatched — no
Postgres, no live DB, no network (the test_cluster_sizes_endpoint pattern).
Covers:
  * ranked candidates: outlet_count DESC, stable_id tiebreak, capped at 5,
  * representative chosen by the server's own display rule
    (_build_trending_display_index: lowest-id member titled label_title,
    else min member id),
  * clusters below 2 outlets / without a stable_id / without a real
    cluster_id omitted (mirrors the sizes/spread >=2 gate),
  * representative content_nature == market_commercial excluded,
  * cache-control mirrors the spread endpoints,
  * no candidates / no graph / unexpected exception -> {"candidates": []} 200,
  * honesty: no verdict/score/confidence/label column in the endpoint source
    (mirrors ClusterSizesHonestyTests.test_no_verdict_column_in_endpoint_source).
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


# Cluster 0: 3 outlets, label matches node 2 -> representative 2.
# Cluster 1: 5 outlets but its representative (7) is market_commercial -> excluded.
# Cluster 2: 4 outlets, no label match -> representative = min member id (11).
# Cluster 3: 1 outlet -> below the >=2 gate.
# Cluster 4: no stable_id -> omitted. Node 9 is a singleton.
FAKE_GRAPH = {
    "nodes": [
        {"id": 1, "cluster_id": 0, "title": "a", "content_nature": "policy_news"},
        {"id": 2, "cluster_id": 0, "title": "big story", "content_nature": "policy_news"},
        {"id": 7, "cluster_id": 1, "title": "market thing", "content_nature": "market_commercial"},
        {"id": 8, "cluster_id": 1, "title": "market thing 2", "content_nature": "market_commercial"},
        {"id": 12, "cluster_id": 2, "title": "mid story", "content_nature": "policy_news"},
        {"id": 11, "cluster_id": 2, "title": "mid story b", "content_nature": "policy_news"},
        {"id": 20, "cluster_id": 3, "title": "small", "content_nature": "policy_news"},
        {"id": 30, "cluster_id": 4, "title": "no stable", "content_nature": "policy_news"},
        {"id": 9, "cluster_id": None, "title": "singleton"},
    ],
    "edges": [],
    "clusters": [
        {"cluster_id": 0, "stable_id": "aaa111", "label_title": "big story",
         "outlet_count": 3, "kind": "spread"},
        {"cluster_id": 1, "stable_id": "bbb222", "label_title": "market thing",
         "outlet_count": 5, "kind": "spread"},
        {"cluster_id": 2, "stable_id": "ccc333", "label_title": "unmatched label",
         "outlet_count": 4, "kind": "spread"},
        {"cluster_id": 3, "stable_id": "ddd444", "label_title": "small",
         "outlet_count": 1, "kind": "spread"},
        {"cluster_id": 4, "stable_id": "", "label_title": "no stable",
         "outlet_count": 6, "kind": "spread"},
    ],
}
FAKE_DISPLAY = api_server._build_trending_display_index(FAKE_GRAPH)
FAKE_CANDIDATES = api_server._build_hero_pick_candidates(FAKE_GRAPH, FAKE_DISPLAY)


class HeroPickBuilderTests(unittest.TestCase):
    def test_ranked_desc_with_market_and_gates_applied(self):
        # market cluster (5 outlets) excluded; <2 gate and empty stable_id
        # omitted; remaining ranked outlet_count DESC.
        self.assertEqual(FAKE_CANDIDATES, [
            {"stable_id": "ccc333", "representative_analysis_id": 11,
             "outlet_count": 4},
            {"stable_id": "aaa111", "representative_analysis_id": 2,
             "outlet_count": 3},
        ])

    def test_representative_follows_display_rule(self):
        # Label match -> the matching member (2), not min id (1);
        # no match -> min member id (11), not first-listed (12).
        by_stable = {c["stable_id"]: c for c in FAKE_CANDIDATES}
        self.assertEqual(by_stable["aaa111"]["representative_analysis_id"], 2)
        self.assertEqual(by_stable["ccc333"]["representative_analysis_id"], 11)

    def test_stable_id_tiebreak_and_cap(self):
        graph = {
            "nodes": [
                {"id": i, "cluster_id": i, "title": "t%d" % i,
                 "content_nature": "policy_news"}
                for i in range(1, 8)
            ],
            "edges": [],
            "clusters": [
                {"cluster_id": i, "stable_id": "sid%d" % i,
                 "label_title": "t%d" % i, "outlet_count": 2, "kind": "spread"}
                for i in range(1, 8)
            ],
        }
        display = api_server._build_trending_display_index(graph)
        ranked = api_server._build_hero_pick_candidates(graph, display)
        # Equal counts -> stable_id ASC, capped at _HERO_PICK_LIMIT (5).
        self.assertEqual([c["stable_id"] for c in ranked],
                         ["sid1", "sid2", "sid3", "sid4", "sid5"])

    def test_circulation_fields_only(self):
        for candidate in FAKE_CANDIDATES:
            self.assertEqual(
                sorted(candidate),
                ["outlet_count", "representative_analysis_id", "stable_id"])


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def _get(self, candidates=FAKE_CANDIDATES):
        with patch.object(api_server, "_load_hero_pick_candidates",
                          return_value=candidates):
            return self.client.get("/api/hero-pick")


class HeroPickEndpointTests(_ClientMixin, unittest.TestCase):
    def test_candidates_payload(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"candidates": FAKE_CANDIDATES})

    def test_cache_control_header(self):
        self.assertEqual(self._get().headers.get("cache-control"),
                         "max-age=300")

    def test_no_candidates_returns_empty_200(self):
        response = self._get(candidates=[])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"candidates": []})

    def test_no_graph_returns_empty_200(self):
        response = self._get(candidates=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"candidates": []})

    def test_unexpected_exception_returns_empty_200_not_500(self):
        with patch.object(api_server, "_load_hero_pick_candidates",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/hero-pick")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"candidates": []})


class HeroPickHonestyTests(unittest.TestCase):
    def test_no_verdict_column_in_endpoint_source(self):
        # Mirrors ClusterSizesHonestyTests: the handler AND its helpers must
        # never touch a verdict/score/confidence/label column.
        source = "".join(inspect.getsource(fn) for fn in (
            api_server.hero_pick,
            api_server._load_hero_pick_candidates,
            api_server._build_hero_pick_candidates,
        ))
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "operator_review_required",
                       "has_genuine_official_support",
                       "verdict_confidence", "policy_alert_level",
                       "risk_level", "review_status"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
