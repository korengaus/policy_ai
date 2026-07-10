"""FADED-CLAIMS Slice 1 — tests for the detection generator's pure logic
(scripts/generate_faded_candidates.py). Offline: synthetic graph + fixed
'today'; no DB, no network. The script's own --selftest covers the same
matrix; these run it under pytest so CI gates it too.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_faded_candidates as gfc  # noqa: E402

TODAY = date(2026, 7, 11)

GRAPH = {
    "nodes": [
        {"id": 1, "cluster_id": 0}, {"id": 2, "cluster_id": 0},
        {"id": 4, "cluster_id": 1}, {"id": 5, "cluster_id": 1},
        {"id": 6, "cluster_id": 2}, {"id": 7, "cluster_id": 2},
        {"id": 8, "cluster_id": 3}, {"id": 9, "cluster_id": 3},
    ],
    "clusters": [
        # kept: wide, 40d silent, forward-looking marker.
        {"cluster_id": 0, "stable_id": "aaa", "outlet_count": 8,
         "label_title": "청년 지원금 도입 검토"},
        # excluded: recent (5d).
        {"cluster_id": 1, "stable_id": "bbb", "outlet_count": 9,
         "label_title": "전세 대출 발표"},
        # excluded: narrow (3 outlets).
        {"cluster_id": 2, "stable_id": "ccc", "outlet_count": 3,
         "label_title": "복지 계획 발표"},
        # kept: wide, 60d silent, NO marker (marker never gates).
        {"cluster_id": 3, "stable_id": "ddd", "outlet_count": 6,
         "label_title": "전세 대출 급증"},
    ],
}
ROWS = {
    1: ("청년 지원금 도입 검토", "2026-06-01T00:00:00+00:00", "https://a.kr/1"),
    2: ("기타", "2026-05-20T00:00:00+00:00", "https://b.kr/2"),
    4: ("전세 대출 발표", "2026-07-06T00:00:00+00:00", "https://a.kr/4"),
    5: ("기타3", "2026-07-01T00:00:00+00:00", "https://b.kr/5"),
    6: ("복지 계획 발표", "2026-05-01T00:00:00+00:00", "https://a.kr/6"),
    7: ("기타4", "2026-04-01T00:00:00+00:00", "https://b.kr/7"),
    8: ("전세 대출 급증", "2026-05-12T00:00:00+00:00", "https://a.kr/8"),
    9: ("기타5", "2026-04-20T00:00:00+00:00", "https://b.kr/9"),
}


def _shortlist(**kwargs):
    defaults = dict(min_outlets=5, min_silence_days=21, top_n=10)
    defaults.update(kwargs)
    return gfc.build_candidates(GRAPH, ROWS, TODAY, **defaults)


class FilterAndRankingTests(unittest.TestCase):
    def test_filtering(self):
        sids = [c["cluster_stable_id"] for c in _shortlist()]
        self.assertIn("aaa", sids)
        self.assertIn("ddd", sids)
        self.assertNotIn("bbb", sids)  # too recent
        self.assertNotIn("ccc", sids)  # too narrow

    def test_ranking_and_marker_boost(self):
        shortlist = _shortlist()
        self.assertEqual([c["cluster_stable_id"] for c in shortlist],
                         ["aaa", "ddd"])
        by_sid = {c["cluster_stable_id"]: c for c in shortlist}
        self.assertTrue(by_sid["aaa"]["marker_hit"])
        self.assertFalse(by_sid["ddd"]["marker_hit"])
        self.assertEqual([c["rank"] for c in shortlist], [1, 2])

    def test_dates_and_representative(self):
        by_sid = {c["cluster_stable_id"]: c for c in _shortlist()}
        self.assertEqual(by_sid["aaa"]["silence_days"], 40)
        self.assertEqual(by_sid["aaa"]["representative_analysis_id"], 1)
        self.assertEqual(by_sid["ddd"]["silence_days"], 60)

    def test_marker_detection(self):
        self.assertTrue(gfc.marker_hit("전세 대출 시행 예정"))
        self.assertFalse(gfc.marker_hit("전세 대출 급증"))
        self.assertTrue(gfc.marker_hit("연금 확대", markers=("확대",)))

    def test_top_n_truncation(self):
        shortlist = _shortlist(top_n=1)
        self.assertEqual(len(shortlist), 1)
        self.assertEqual(shortlist[0]["cluster_stable_id"], "aaa")

    def test_verdict_free_payload(self):
        import json

        blob = json.dumps(_shortlist(), ensure_ascii=False)
        for needle in ("verdict", "confidence", "truth", "policy_alert"):
            self.assertNotIn(needle, blob)


class UpsertPreservationTests(unittest.TestCase):
    def test_new_cluster_inserts_pending(self):
        plan = gfc.plan_upsert(None, {"cluster_stable_id": "x"})
        self.assertEqual(plan, {"action": "insert", "status": "pending",
                                "reviewed_at": None})

    def test_approved_status_survives_rerun(self):
        plan = gfc.plan_upsert(
            {"status": "approved", "reviewed_at": "2026-07-01T00:00:00+00:00"},
            {"cluster_stable_id": "x"},
        )
        self.assertEqual(plan["action"], "update")
        self.assertEqual(plan["status"], "approved")
        self.assertEqual(plan["reviewed_at"], "2026-07-01T00:00:00+00:00")

    def test_dismissed_status_survives_rerun(self):
        plan = gfc.plan_upsert({"status": "dismissed", "reviewed_at": "t"},
                               {"cluster_stable_id": "x"})
        self.assertEqual(plan["status"], "dismissed")

    def test_update_sql_never_touches_status(self):
        # Belt-and-braces: the UPDATE statement must not name the operator
        # columns at all.
        self.assertNotIn("status", gfc.UPDATE_SQL)
        self.assertNotIn("reviewed_at", gfc.UPDATE_SQL)

    def test_selftest_passes(self):
        self.assertEqual(gfc.run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()
