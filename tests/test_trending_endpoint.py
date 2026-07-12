"""TRENDING-API Slice 1 — tests for GET /api/trending.

Offline: the DB seams (_fetch_snapshot_batches / _load_trending_display_index)
are monkeypatched with synthetic fixtures — no Postgres, no live DB, no
network. Covers:
  * two-batch growth diff: formula, ranking (growth desc, tie -> current
    outlet desc), is_new for a current-only cluster, dropped-out cluster
    excluded,
  * Top-N limit: default, ?limit=, cap at 20, garbage limit -> default (200),
  * representative-title resolution from a synthetic graph (label-title
    member, min-id fallback),
  * one-batch case (today's real §27c state) -> {"trending": [], note} 200,
  * zero batches / PG off / unexpected exception -> {"trending": []} 200,
  * honesty: no verdict field selected anywhere in the trending code, no
    truth/검증 vocabulary in the payload.
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


# Synthetic snapshot batches: rows are (stable_id, outlet_count, member_count).
# growth: ccc +9 (new), aaa +3, ddd +3 (new, tie with aaa -> lower current
# outlets loses), bbb +1, eee only in previous (dropped out -> excluded).
CURRENT_BATCH = {
    "snapshot_date": "2026-07-12",
    "graph_ref": 9,
    "rows": [
        ("aaa111aaa111", 5, 10),
        ("bbb222bbb222", 4, 6),
        ("ccc333ccc333", 9, 3),
        ("ddd444ddd444", 3, 2),
    ],
}
PREVIOUS_BATCH = {
    "snapshot_date": "2026-07-11",
    "graph_ref": 7,
    "rows": [
        ("aaa111aaa111", 2, 8),
        ("bbb222bbb222", 3, 6),
        ("eee555eee555", 6, 4),
    ],
}

# Synthetic graph for display resolution: cluster aaa's label_title matches
# member 12 (not the min id 11) -> representative 12; cluster ccc has no
# label-title member -> min-id fallback 31.
FAKE_GRAPH = {
    "nodes": [
        {"id": 11, "cluster_id": 0, "title": "다른 제목"},
        {"id": 12, "cluster_id": 0, "title": "청년 지원금 도입 검토"},
        {"id": 31, "cluster_id": 2, "title": "x"},
        {"id": 32, "cluster_id": 2, "title": "y"},
        {"id": 90, "cluster_id": None, "title": "noise"},
    ],
    "clusters": [
        {"cluster_id": 0, "stable_id": "aaa111aaa111",
         "label_title": "청년 지원금 도입 검토"},
        {"cluster_id": 2, "stable_id": "ccc333ccc333",
         "label_title": "전세 대출 발표"},
        {"cluster_id": 3, "stable_id": "", "label_title": "no-stable-id"},
    ],
}
FAKE_DISPLAY = api_server._build_trending_display_index(FAKE_GRAPH)


class _ClientMixin:
    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def _get(self, url="/api/trending", batches="default", display="default"):
        if batches == "default":
            batches = [CURRENT_BATCH, PREVIOUS_BATCH]
        if display == "default":
            display = FAKE_DISPLAY
        with patch.object(api_server, "_fetch_snapshot_batches",
                          return_value=batches), \
             patch.object(api_server, "_load_trending_display_index",
                          return_value=display):
            return self.client.get(url)


class TrendingRankingTests(_ClientMixin, unittest.TestCase):
    def test_growth_diff_and_ranking(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        order = [e["cluster_stable_id"] for e in body["trending"]]
        # ccc new(+9) > aaa(+3, 5 outlets) > ddd(new +3, 3 outlets) > bbb(+1).
        self.assertEqual(order, ["ccc333ccc333", "aaa111aaa111",
                                 "ddd444ddd444", "bbb222bbb222"])
        by_sid = {e["cluster_stable_id"]: e for e in body["trending"]}
        aaa = by_sid["aaa111aaa111"]
        self.assertEqual(aaa["growth"], 3)
        self.assertEqual(aaa["current_outlet_count"], 5)
        self.assertEqual(aaa["previous_outlet_count"], 2)
        self.assertEqual(aaa["member_count"], 10)
        self.assertFalse(aaa["is_new"])

    def test_new_cluster_is_new_full_growth(self):
        body = self._get().json()
        ccc = body["trending"][0]
        self.assertTrue(ccc["is_new"])
        self.assertEqual(ccc["growth"], 9)
        self.assertIsNone(ccc["previous_outlet_count"])

    def test_dropped_out_cluster_excluded(self):
        body = self._get().json()
        sids = {e["cluster_stable_id"] for e in body["trending"]}
        self.assertNotIn("eee555eee555", sids)

    def test_window_carries_batch_provenance(self):
        window = self._get().json()["window"]
        self.assertEqual(window, {"current_date": "2026-07-12",
                                  "previous_date": "2026-07-11",
                                  "graph_ref": 9})

    def test_representative_title_resolution(self):
        by_sid = {e["cluster_stable_id"]: e
                  for e in self._get().json()["trending"]}
        # label-title member (id 12) beats the min id (11).
        aaa = by_sid["aaa111aaa111"]
        self.assertEqual(aaa["title"], "청년 지원금 도입 검토")
        self.assertEqual(aaa["representative_analysis_id"], 12)
        # no member matches the label -> min-id fallback.
        ccc = by_sid["ccc333ccc333"]
        self.assertEqual(ccc["title"], "전세 대출 발표")
        self.assertEqual(ccc["representative_analysis_id"], 31)
        # cluster absent from the newest graph -> blank display, no crash.
        bbb = by_sid["bbb222bbb222"]
        self.assertEqual(bbb["title"], "")
        self.assertIsNone(bbb["representative_analysis_id"])

    def test_no_display_index_still_answers(self):
        body = self._get(display=None).json()
        self.assertEqual(len(body["trending"]), 4)
        self.assertTrue(all(e["title"] == "" for e in body["trending"]))

    def test_cache_control_header(self):
        self.assertEqual(self._get().headers.get("cache-control"),
                         "max-age=300")


class TrendingLimitTests(_ClientMixin, unittest.TestCase):
    def test_limit_param(self):
        body = self._get("/api/trending?limit=2").json()
        self.assertEqual(len(body["trending"]), 2)
        self.assertEqual(body["trending"][0]["cluster_stable_id"],
                         "ccc333ccc333")

    def test_limit_capped_at_20(self):
        many = {"snapshot_date": "2026-07-12", "graph_ref": 9,
                "rows": [("sid%02d" % i, i, 1) for i in range(1, 31)]}
        prev = {"snapshot_date": "2026-07-11", "graph_ref": 7, "rows": []}
        body = self._get("/api/trending?limit=50",
                         batches=[many, prev]).json()
        self.assertEqual(len(body["trending"]), 20)

    def test_garbage_limit_falls_back_to_default_200(self):
        response = self._get("/api/trending?limit=abc")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["trending"]), 4)


class TrendingEmptyTests(_ClientMixin, unittest.TestCase):
    def test_single_batch_returns_note_200(self):
        # Today's real state: exactly one §27c snapshot batch exists.
        response = self._get(batches=[CURRENT_BATCH])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trending"], [])
        self.assertEqual(body["note"], "insufficient snapshot history")
        self.assertEqual(body["window"]["current_date"], "2026-07-12")
        self.assertIsNone(body["window"]["previous_date"])

    def test_zero_batches_returns_empty_200(self):
        response = self._get(batches=[])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"trending": []})

    def test_pg_disabled_returns_empty_200(self):
        response = self._get(batches=None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"trending": []})

    def test_unexpected_exception_returns_empty_200_not_500(self):
        with patch.object(api_server, "_fetch_snapshot_batches",
                          side_effect=RuntimeError("boom")):
            response = self.client.get("/api/trending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"trending": []})


class TrendingPureHelperTests(unittest.TestCase):
    def test_compute_trending_duplicate_rows_collapse(self):
        # A --force re-append doubles a batch's rows — last one wins.
        entries = api_server._compute_trending(
            [("aaa", 2, 1), ("aaa", 5, 2)], [("aaa", 1, 1)], 10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["growth"], 4)

    def test_display_index_skips_blank_stable_id(self):
        self.assertNotIn("", FAKE_DISPLAY)
        self.assertEqual(set(FAKE_DISPLAY),
                         {"aaa111aaa111", "ccc333ccc333"})


class TrendingHonestyTests(_ClientMixin, unittest.TestCase):
    def test_no_truth_vocabulary_in_payload(self):
        text = self._get().text
        for word in ("검증", "confirmed", "verified", "truth", "probability",
                     "confidence"):
            self.assertNotIn(word, text)

    def test_no_verdict_column_in_trending_source(self):
        source = "".join(inspect.getsource(fn) for fn in (
            api_server._fetch_snapshot_batches,
            api_server._compute_trending,
            api_server._build_trending_display_index,
            api_server._load_trending_display_index,
            api_server.trending_growth,
        ))
        for column in ("verdict_label", "policy_confidence", "truth_claim",
                       "operator_review_required",
                       "has_genuine_official_support"):
            self.assertNotIn(column, source)


if __name__ == "__main__":
    unittest.main()
