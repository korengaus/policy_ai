"""PREDICTION-LOG B4 Phase 2a — tests for scripts/prediction_log_weekly.py.

Offline: no DB, no network. Mirrors the sibling script tests (pure helpers +
selftest) plus the two B4-specific pins:
  * HONESTY: the DDL is structurally verdict-free — every column name passes
    honesty_guard._is_truth_probability_key, and no verdict column is named
    in any SQL the script can execute.
  * TRENDING SYNC: compute_trending is a deliberate duplicate of the pure
    api_server._compute_trending (the cron child must not import the FastAPI
    app); a BEHAVIORAL pin runs both on the same fixtures and requires
    identical output, so drift fails CI instead of silently forking the
    rising-cluster signal.
"""

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import honesty_guard  # noqa: E402
import prediction_log_weekly as plw  # noqa: E402


class ContainmentMatcherTests(unittest.TestCase):
    def test_merge_matches_under_containment_not_jaccard(self):
        # A(10) absorbed into B(40): the most successful prediction outcome.
        predicted = list(range(1, 11))
        index = {"big": {"member_ids": set(range(1, 41)), "outlet_count": 12}}
        containment, jaccard = plw.containment_and_jaccard(
            predicted, index["big"]["member_ids"])
        self.assertEqual(containment, 1.0)
        self.assertAlmostEqual(jaccard, 0.25)
        self.assertLess(jaccard, 0.5)  # the churn trap Jaccard would hit
        match = plw.best_match(predicted, index)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "big")

    def test_split_is_unmeasurable(self):
        predicted = list(range(1, 11))
        index = {
            "shard1": {"member_ids": set(range(1, 6)), "outlet_count": 3},
            "shard2": {"member_ids": set(range(6, 11)), "outlet_count": 3},
        }
        self.assertIsNone(plw.best_match(predicted, index))
        fields = plw.score_prediction(json.dumps(predicted), 5, index)
        self.assertEqual(fields["outcome"], "unmeasurable")
        self.assertEqual(fields["matched_stable_id"], "")

    def test_vanished_is_unmeasurable(self):
        fields = plw.score_prediction(
            json.dumps([1, 2, 3]), 5,
            {"other": {"member_ids": {90, 91}, "outlet_count": 2}})
        self.assertEqual(fields["outcome"], "unmeasurable")

    def test_empty_graph_and_bad_json_are_unmeasurable(self):
        self.assertEqual(
            plw.score_prediction(json.dumps([1, 2]), 5, {})["outcome"],
            "unmeasurable")
        self.assertEqual(
            plw.score_prediction("not-json", 5,
                                 {"a": {"member_ids": {1}, "outlet_count": 1}}
                                 )["outcome"],
            "unmeasurable")

    def test_outcomes_by_outlet_comparison(self):
        predicted = [1, 2, 3]
        index = {"same": {"member_ids": {1, 2, 3}, "outlet_count": 9}}
        self.assertEqual(
            plw.score_prediction(json.dumps(predicted), 5, index)["outcome"],
            "grew")
        self.assertEqual(
            plw.score_prediction(json.dumps(predicted), 9, index)["outcome"],
            "held")
        self.assertEqual(
            plw.score_prediction(json.dumps(predicted), 12, index)["outcome"],
            "faded")

    def test_best_match_deterministic_tiebreak(self):
        predicted = [1, 2]
        index = {
            "bbb": {"member_ids": {1, 2}, "outlet_count": 3},
            "aaa": {"member_ids": {1, 2}, "outlet_count": 3},
        }
        # Identical overlap metrics -> highest stable_id wins the max() key
        # deterministically, every run.
        first = plw.best_match(predicted, index)
        second = plw.best_match(predicted, index)
        self.assertEqual(first, second)
        self.assertEqual(first[0], "bbb")

    def test_threshold_boundary(self):
        predicted = [1, 2, 3, 4, 5]
        # containment 2/5 = 0.4 < 0.6 -> no match.
        below = {"c": {"member_ids": {1, 2, 900}, "outlet_count": 2}}
        self.assertIsNone(plw.best_match(predicted, below))
        # containment 3/5 = 0.6 meets the >= threshold exactly.
        at = {"c": {"member_ids": {1, 2, 3}, "outlet_count": 2}}
        match = plw.best_match(predicted, at)
        self.assertIsNotNone(match)
        self.assertAlmostEqual(match[1], 0.6)


class BuildPredictionRowsTests(unittest.TestCase):
    def _entries(self):
        current = [("up", 8, 5), ("flat", 4, 3), ("new", 6, 4), ("down", 2, 2)]
        previous = [("up", 5, 4), ("flat", 4, 3), ("down", 3, 2)]
        return plw.compute_trending(current, previous, 10), {
            "up": {"member_ids": {10, 11}, "outlet_count": 8},
            "flat": {"member_ids": {20, 21}, "outlet_count": 4},
            "new": {"member_ids": {30, 31, 32}, "outlet_count": 6},
            "down": {"member_ids": {40}, "outlet_count": 2},
        }

    def test_growth_filter_ranking_and_constants(self):
        entries, index = self._entries()
        rows = plw.build_prediction_rows(entries, index, "2026-07-13", 42,
                                         "2026-07-13", top_n=5)
        self.assertEqual([r["cluster_stable_id"] for r in rows],
                         ["new", "up"])  # new growth 6 > up growth 3; no flat/down
        top = rows[0]
        self.assertEqual(top["is_new"], 1)
        self.assertEqual(top["graph_ref"], 42)
        self.assertEqual(top["predicted_direction"], "continue_spreading")
        self.assertEqual(top["framing"], plw.FRAMING_TEXT)
        self.assertEqual(top["horizon_days"], plw.HORIZON_DAYS)
        self.assertEqual(json.loads(top["member_ids_json"]), [30, 31, 32])
        self.assertEqual(top["member_count_at_prediction"], 3)

    def test_top_n_cap(self):
        entries, index = self._entries()
        rows = plw.build_prediction_rows(entries, index, "2026-07-13", 42,
                                         "2026-07-13", top_n=1)
        self.assertEqual(len(rows), 1)

    def test_unresolvable_member_set_skipped(self):
        entries, index = self._entries()
        del index["new"]
        rows = plw.build_prediction_rows(entries, index, "2026-07-13", 42,
                                         "2026-07-13", top_n=5)
        self.assertEqual([r["cluster_stable_id"] for r in rows], ["up"])


class HorizonTests(unittest.TestCase):
    def test_horizon_math(self):
        self.assertTrue(plw.horizon_elapsed("2026-07-06", 7, "2026-07-13"))
        self.assertTrue(plw.horizon_elapsed("2026-07-01", 7, "2026-07-13"))
        self.assertFalse(plw.horizon_elapsed("2026-07-07", 7, "2026-07-13"))
        self.assertFalse(plw.horizon_elapsed("garbage", 7, "2026-07-13"))


class AppendOnlyTests(unittest.TestCase):
    SQL_CONSTANTS = (
        plw.SELECT_SNAPSHOT_KEYS_SQL, plw.SELECT_SNAPSHOT_ROWS_SQL,
        plw.SELECT_GRAPH_BY_ID_SQL, plw.SELECT_NEWEST_GRAPH_SQL,
        plw.SELECT_UNSCORED_SQL, plw.SELECT_EXISTING_BATCH_SQL,
        plw.CREATE_PREDICTION_LOG_SQL, plw.CREATE_PREDICTION_SCORES_SQL,
        plw.INSERT_PREDICTION_SQL, plw.INSERT_SCORE_SQL,
    )

    def test_no_update_delete_upsert_in_any_sql(self):
        for statement in self.SQL_CONSTANTS:
            for word in ("UPDATE", "DELETE", "ON CONFLICT"):
                self.assertNotIn(word, statement.upper())

    def test_module_issues_no_update_anywhere(self):
        import inspect
        source = inspect.getsource(plw)
        # Every executed statement comes from the SQL constants above; no
        # inline "UPDATE ..." string may exist as executable SQL.
        self.assertNotRegex(source, r'"\s*UPDATE\s')
        self.assertNotRegex(source, r"'\s*UPDATE\s")


class HonestySchemaTests(unittest.TestCase):
    def test_ddl_columns_cannot_hold_truth(self):
        columns = (plw.ddl_column_names(plw.CREATE_PREDICTION_LOG_SQL)
                   + plw.ddl_column_names(plw.CREATE_PREDICTION_SCORES_SQL))
        self.assertGreaterEqual(len(columns), 20)  # both tables parsed
        flagged = [c for c in columns
                   if honesty_guard._is_truth_probability_key(c)]
        self.assertEqual(flagged, [])

    def test_no_verdict_column_in_any_sql(self):
        for statement in AppendOnlyTests.SQL_CONSTANTS:
            for column in ("verdict_label", "truth_claim", "policy_confidence",
                           "operator_review_required",
                           "has_genuine_official_support"):
                self.assertNotIn(column, statement)

    def test_outcome_vocabulary_is_spread_only(self):
        outcomes = {"grew", "held", "faded", "unmeasurable"}
        for outcome in outcomes:
            for word in honesty_guard.FORBIDDEN_LABEL_VOCAB:
                self.assertNotIn(word, outcome)
        self.assertEqual(plw.PREDICTED_DIRECTION, "continue_spreading")

    def test_framing_reminder_for_future_exposure(self):
        # B4 stores rows only. If this framing is ever exposed via an API it
        # must FIRST join honesty_guard.FRAMING_WHITELIST — this test makes
        # the current state explicit so exposure work trips over it.
        self.assertNotIn(plw.FRAMING_TEXT, honesty_guard.FRAMING_WHITELIST)


class TrendingSignalSyncTests(unittest.TestCase):
    """Behavioral pin: the duplicated compute_trending must produce output
    identical to api_server._compute_trending on the same inputs."""

    FIXTURES = (
        # (current_rows, previous_rows, limit)
        ([("a", 5, 3), ("b", 2, 1)], [("a", 1, 1)], 10),          # growth + new
        ([("a", 5, 3), ("a", 9, 4)], [("a", 1, 1)], 10),           # dupes collapse
        ([("a", 3, 1), ("b", 3, 1), ("c", 1, 1)], [], 2),          # ties + limit
        ([("", 9, 9), ("a", 2, 1)], [("", 1, 1), ("a", 2, 1)], 10),  # blank sid dropped
        ([], [("a", 5, 2)], 10),                                    # dropouts ignored
        ([("x", 4, 2)], [("x", 7, 3)], 10),                        # negative growth kept by signal
    )

    def test_identical_output_on_all_fixtures(self):
        import api_server
        for current, previous, limit in self.FIXTURES:
            self.assertEqual(
                plw.compute_trending(current, previous, limit),
                api_server._compute_trending(current, previous, limit),
                "compute_trending diverged from api_server._compute_trending "
                "on fixture %r" % (current,))


class SelftestTests(unittest.TestCase):
    def test_selftest_passes(self):
        self.assertEqual(plw.run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()
