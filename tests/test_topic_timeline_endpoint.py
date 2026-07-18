# TEMPORAL-MAP v1 — offline tests for GET /api/topic-timeline/{analysis_id}.
# Mirrors test_trending_endpoint.py's seam-mock style: the DB seams
# (_load_spread_indexes / _resolve_cluster_lineage / _fetch_lineage_trajectory)
# are patched, so no DB and no network. Measurement only — the payload carries
# dates + counts + lineage hex, never verdict vocabulary.
import contextlib
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402


# Analysis id 42 lives in cluster 0 (11 outlets, lineage carried by graph);
# id 77 is a singleton (not in cluster_of); cluster 1 fails the outlet>=2 gate.
FAKE_INDEXES = {
    "clusters": {
        0: {"cluster_id": 0, "stable_id": "newsid111111",
            "lineage_id": "lin-aaa11111", "outlet_count": 11},
        1: {"cluster_id": 1, "stable_id": "one1one1one1",
            "lineage_id": "lin-bbb22222", "outlet_count": 1},
    },
    "members": {0: [42, 43], 1: [55]},
    "cluster_of": {42: 0, 43: 0, 55: 1},
    "title_of": {42: "t", 43: "u", 55: "v"},
}

TRAJECTORY = [
    ("2026-07-06", 3, 3, 4),
    ("2026-07-09", 5, 7, 9),
    ("2026-07-13", 7, 11, 14),
]


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def _get(self, analysis_id=42, indexes="default", trajectory="default",
             lineage="default"):
        if indexes == "default":
            indexes = FAKE_INDEXES
        if trajectory == "default":
            trajectory = TRAJECTORY
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(
                api_server, "_load_spread_indexes", return_value=indexes))
            stack.enter_context(patch.object(
                api_server, "_fetch_lineage_trajectory",
                return_value=trajectory))
            if lineage != "default":
                stack.enter_context(patch.object(
                    api_server, "_resolve_cluster_lineage",
                    return_value=lineage))
            return self.client.get(f"/api/topic-timeline/{analysis_id}")


class TopicTimelineShapeTests(_ClientMixin, unittest.TestCase):
    def test_trajectory_payload_shape(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["found"])
        self.assertEqual(body["lineage_id"], "lin-aaa11111")
        self.assertEqual(len(body["points"]), 3)
        self.assertEqual(body["points"][0],
                         {"date": "2026-07-06", "graph_ref": 3,
                          "outlets": 3, "members": 4})
        self.assertEqual(body["first_seen"], "2026-07-06")
        self.assertEqual(body["peak_outlets"], 11)

    def test_latest_delta_is_last_two_point_difference(self):
        body = self._get().json()
        self.assertEqual(body["latest_delta"], 4)  # 11 - 7

    def test_single_point_delta_zero(self):
        body = self._get(trajectory=[("2026-07-13", 7, 11, 14)]).json()
        self.assertTrue(body["found"])
        self.assertEqual(body["latest_delta"], 0)
        self.assertEqual(body["peak_outlets"], 11)

    def test_lineage_falls_back_to_snapshots_when_graph_lacks_it(self):
        # The live graph row may PREDATE the lineage change: cluster meta has
        # no lineage_id, but the backfilled snapshots do.
        indexes = {
            "clusters": {0: {"cluster_id": 0, "stable_id": "newsid111111",
                             "outlet_count": 11}},
            "members": {0: [42]}, "cluster_of": {42: 0}, "title_of": {42: "t"},
        }
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=indexes), \
             patch.object(api_server, "_resolve_cluster_lineage",
                          return_value="lin-from-snap") as resolver, \
             patch.object(api_server, "_fetch_lineage_trajectory",
                          return_value=TRAJECTORY):
            body = self.client.get("/api/topic-timeline/42").json()
        self.assertTrue(body["found"])
        self.assertEqual(body["lineage_id"], "lin-from-snap")
        resolver.assert_called_once()

    def test_cache_control_header(self):
        self.assertEqual(self._get().headers.get("cache-control"),
                         "max-age=300")


class TopicTimelineGateTests(_ClientMixin, unittest.TestCase):
    def test_singleton_not_in_cluster_returns_found_false(self):
        response = self._get(analysis_id=77)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})

    def test_outlet_gate_below_two_returns_found_false(self):
        self.assertEqual(self._get(analysis_id=55).json(), {"found": False})

    def test_no_lineage_returns_found_false(self):
        self.assertEqual(self._get(lineage=None).json(), {"found": False})

    def test_empty_trajectory_returns_found_false(self):
        self.assertEqual(self._get(trajectory=[]).json(), {"found": False})

    def test_no_indexes_returns_found_false(self):
        self.assertEqual(self._get(indexes=None).json(), {"found": False})

    def test_unexpected_exception_returns_found_false_200_not_500(self):
        with patch.object(api_server, "_load_spread_indexes",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/topic-timeline/42")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"found": False})


class TopicTimelineHonestyTests(_ClientMixin, unittest.TestCase):
    def test_no_verdict_vocabulary_in_payload(self):
        text = self._get().text
        for word in ("검증", "confirmed", "verified", "truth", "probability",
                     "confidence", "여론", "sentiment", "반박"):
            self.assertNotIn(word, text)

    def test_no_verdict_column_in_timeline_source(self):
        source = "".join(inspect.getsource(fn) for fn in (
            api_server._fetch_lineage_trajectory,
            api_server._resolve_cluster_lineage,
            api_server._build_topic_timeline_payload,
            api_server.topic_timeline,
        ))
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "policy_alert_level", "final_decision"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
