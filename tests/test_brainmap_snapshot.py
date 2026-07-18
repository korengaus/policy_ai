# BRAINMAP-SNAPSHOT Slice 1 — offline tests for the growth-snapshot
# generator's pure functions (extraction + append-only semantics). No DB,
# no network — mirrors the weekly/faded generator test approach.
import json
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import snapshot_brainmap_growth as sbg  # noqa: E402


def _graph():
    return {
        "nodes": [
            {"id": 11, "cluster_id": 0, "title": "a"},
            {"id": 12, "cluster_id": 0, "title": "b"},
            {"id": 13, "cluster_id": 0, "title": "c"},
            {"id": 21, "cluster_id": 1, "title": "d"},
            {"id": 22, "cluster_id": 1, "title": "e"},
            {"id": 30, "cluster_id": None, "title": "noise"},
        ],
        "clusters": [
            {"cluster_id": 0, "stable_id": "aaa111aaa111", "outlet_count": 4,
             "size": 3},
            {"cluster_id": 1, "stable_id": "bbb222bbb222", "outlet_count": 2,
             "size": 2},
            {"cluster_id": 2, "stable_id": "", "outlet_count": 9, "size": 1},
        ],
    }


class ExtractionTests(unittest.TestCase):
    def test_extracts_stable_id_outlets_members(self):
        rows = sbg.build_snapshot_rows(_graph(), "2026-07-11", 7, "g-at")
        by_sid = {r["cluster_stable_id"]: r for r in rows}
        self.assertEqual(set(by_sid), {"aaa111aaa111", "bbb222bbb222"})
        self.assertEqual(by_sid["aaa111aaa111"]["outlet_count"], 4)
        self.assertEqual(by_sid["aaa111aaa111"]["member_count"], 3)
        self.assertEqual(by_sid["bbb222bbb222"]["outlet_count"], 2)
        self.assertEqual(by_sid["bbb222bbb222"]["member_count"], 2)

    def test_skips_clusters_without_stable_id(self):
        rows = sbg.build_snapshot_rows(_graph(), "2026-07-11", 7, "")
        self.assertEqual(len(rows), 2)  # the ""-stable_id cluster is dropped

    def test_row_shape_and_provenance(self):
        rows = sbg.build_snapshot_rows(_graph(), "2026-07-11", 7, "g-at")
        for row in rows:
            self.assertEqual(
                set(row),
                # STABLE-CLUSTER-ID: cluster_lineage_id joined the row shape.
                {"snapshot_date", "graph_ref", "graph_generated_at",
                 "cluster_stable_id", "outlet_count", "member_count",
                 "cluster_lineage_id"},
            )
            self.assertEqual(row["snapshot_date"], "2026-07-11")
            self.assertEqual(row["graph_ref"], 7)
            self.assertEqual(row["graph_generated_at"], "g-at")

    def test_lineage_passthrough_and_pre_lineage_null(self):
        # STABLE-CLUSTER-ID: pre-lineage graphs (no lineage_id key) snapshot
        # as None; post-lineage graphs pass the id through untouched.
        rows = sbg.build_snapshot_rows(_graph(), "2026-07-11", 7, "g-at")
        self.assertTrue(all(r["cluster_lineage_id"] is None for r in rows))
        graph = _graph()
        graph["clusters"][0]["lineage_id"] = "lin-000000aa"
        by_sid = {r["cluster_stable_id"]: r
                  for r in sbg.build_snapshot_rows(graph, "2026-07-11", 7, "g")}
        self.assertEqual(by_sid["aaa111aaa111"]["cluster_lineage_id"],
                         "lin-000000aa")
        self.assertIsNone(by_sid["bbb222bbb222"]["cluster_lineage_id"])

    def test_member_count_falls_back_to_size_without_nodes(self):
        bare = {"clusters": [{"cluster_id": 0, "stable_id": "ccc333ccc333",
                              "outlet_count": 1, "size": 5}]}
        rows = sbg.build_snapshot_rows(bare, "2026-07-11", 8, "")
        self.assertEqual(rows[0]["member_count"], 5)


class AppendOnlyTests(unittest.TestCase):
    """The core contract: runs ADD rows; prior rows are never touched."""

    def test_second_run_appends_and_prior_rows_untouched(self):
        day1 = sbg.build_snapshot_rows(_graph(), "2026-07-10", 6, "g1")
        store, wrote1 = sbg.append_batch([], day1)
        frozen = json.dumps(store, sort_keys=True)
        day2 = sbg.build_snapshot_rows(_graph(), "2026-07-11", 7, "g2")
        store2, wrote2 = sbg.append_batch(store, day2)
        self.assertTrue(wrote1)
        self.assertTrue(wrote2)
        self.assertEqual(len(store2), 4)
        self.assertEqual(json.dumps(store2[:2], sort_keys=True), frozen)

    def test_exact_duplicate_batch_skipped_unless_forced(self):
        day = sbg.build_snapshot_rows(_graph(), "2026-07-11", 7, "g")
        store, _ = sbg.append_batch([], day)
        again, wrote = sbg.append_batch(store, day)
        self.assertFalse(wrote)
        self.assertEqual(len(again), 2)
        forced, wrote_forced = sbg.append_batch(store, day, force=True)
        self.assertTrue(wrote_forced)
        self.assertEqual(len(forced), 4)

    def test_sql_is_append_only(self):
        # ALTER_ADD_LINEAGE_SQL is ADD COLUMN IF NOT EXISTS — schema-additive.
        for statement in (sbg.SELECT_NEWEST_GRAPH_SQL, sbg.CREATE_TABLE_SQL,
                          sbg.INSERT_SQL, sbg.SELECT_EXISTING_BATCH_SQL,
                          sbg.ALTER_ADD_LINEAGE_SQL):
            upper = statement.upper()
            self.assertNotIn("UPDATE", upper)
            self.assertNotIn("DELETE", upper)
            self.assertNotIn("ON CONFLICT", upper)


if __name__ == "__main__":
    unittest.main()
