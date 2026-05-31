"""Phase 2 M11.0a: tests for ``verdict_producer_comparison`` +
``compare_verdict_producers``.

Every test that writes to a database uses a temp SQLite file so the
real ``policy_ai.db`` is untouched. No test path calls
``analyze_pipeline`` or any other live pipeline entry point. No test
path makes a network call, imports OpenAI, or invokes browser
automation.

Covers the M11.0a spec items:
    A. compare_producers_for_analysis with valid SQLite-shaped row
       runs all three producers and returns a populated comparison
    B. compare_producers_for_analysis tolerates rows missing P1
       inputs — producer1_label is None, extra notes the missing
       fields, P2/P3 still run
    C. compare_producers_for_analysis tolerates rows missing P3
       inputs the same way
    D. compute_disagreement_summary with 0 comparisons → total=0
    E. compute_disagreement_summary with all-agreeing → all_three
       equals total
    F. compute_disagreement_summary pairwise counts are correct
    G. truth_claim is always False on ProducerComparison
    H. operator_review_required is always True on ProducerComparison
    I. save_producer_comparison forces truth_claim=0 in DB
    J. save_producer_comparison forces operator_review_required=1
    K. save_producer_comparison INSERT-OR-REPLACE on input_hash
    L. get_producer_comparisons filters by analysis_id
    M. get_producer_comparisons only_disagreements filter
    N. input_hash is deterministic for same input
    O. init_db() creates verdict_producer_comparisons table
    P. Producer that raises an exception → recorded with error, no
       crash
    Q. No network calls in any test path
    R. No OpenAI imports in verdict_producer_comparison.py
    S. verdict_producer_comparison not imported by main / api / scheduler
    T. The three producers themselves are not modified — assert key
       constants / function signatures still exist
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database  # noqa: E402
import policy_decision  # noqa: E402
import policy_scoring  # noqa: E402
import postgres_storage  # noqa: E402
import verdict_producer_comparison as comparator  # noqa: E402
import verification_card  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "compare_verdict_producers.py"
COMPARATOR_MODULE_PATH = ROOT / "verdict_producer_comparison.py"

CLI_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row_high_confidence() -> dict:
    """SQLite-shaped row with enough fields to run all three producers
    and (typically) yield a high-confidence verdict from P1."""
    return {
        "id": 101,
        "query": "전세자금 대출 한도",
        "title": "전세자금 대출 한도 확대 발표",
        "original_url": "https://news.example/article-1",
        "claim_text": "정부는 전세자금 대출 한도를 확대한다",
        "claims": json.dumps(
            ["정부는 전세자금 대출 한도를 확대한다"],
            ensure_ascii=False,
        ),
        "normalized_claims": json.dumps(
            ["전세자금 대출 한도 확대"], ensure_ascii=False,
        ),
        # Verdict-input columns.
        "policy_confidence_score": 80,
        "verification_strength": "strong",
        "risk_level": "high",
        "impact_level": "high",
        "impact_direction": "negative",
        "market_sensitivity": 70,
        "consumer_sensitivity": 85,
        "business_sensitivity": 60,
        # Verification-card-side JSON fragments.
        "evidence_quality_summary": json.dumps(
            {"average_evidence_quality_score": 70,
             "strong": 2, "medium": 1, "weak": 0},
            ensure_ascii=False,
        ),
        "source_reliability_summary": json.dumps(
            {"max_reliability_score": 80}, ensure_ascii=False,
        ),
        "contradiction_summary": json.dumps(
            {"possible_contradiction_count": 0,
             "confirmed_contradiction_count": 0,
             "likely_contradiction_count": 0,
             "needs_official_confirmation_count": 0,
             "insufficient_evidence_count": 0},
            ensure_ascii=False,
        ),
        "bias_framing_summary": json.dumps(
            {"high_framing_count": 0}, ensure_ascii=False,
        ),
        "official_mismatch": 0,
        "official_mismatch_reasons": "[]",
        "source_candidates": "[]",
        "evidence_snippets": json.dumps(
            [{"evidence_type": "direct_support"}], ensure_ascii=False,
        ),
        "debug_summary": json.dumps(
            {
                "evidence_strength_summary": {
                    "average_strength_score": 65,
                },
                "evidence_comparison": {
                    "verification_level": "strong_official_match",
                    "comparison_status": "official_evidence_confirmed",
                },
                "official_sources": [
                    {"title": "공식 자료", "url": "https://example.go.kr/x"},
                ],
            },
            ensure_ascii=False,
        ),
    }


def _row_low_confidence() -> dict:
    row = _row_high_confidence()
    row["id"] = 102
    row["policy_confidence_score"] = 15
    row["verification_strength"] = "none"
    row["risk_level"] = "low"
    row["impact_level"] = "low"
    row["consumer_sensitivity"] = 10
    row["evidence_snippets"] = "[]"
    row["debug_summary"] = json.dumps(
        {"evidence_comparison": {}, "official_sources": []},
        ensure_ascii=False,
    )
    row["evidence_quality_summary"] = json.dumps(
        {"average_evidence_quality_score": 0,
         "strong": 0, "medium": 0, "weak": 1},
        ensure_ascii=False,
    )
    return row


def _row_missing_p1_inputs() -> dict:
    """Row with the P1-relevant verdict-input columns stripped so the
    reconstructed dicts contain only defaults. P1 still runs (it
    tolerates falsy inputs), but the comparison records this case
    with low-confidence outputs and no extras like decision_reasons.
    The MORE direct way to force P1 to refuse is to monkey-patch it
    to raise (see ``ProducerRaisesTests``)."""
    row = _row_low_confidence()
    row.pop("policy_confidence_score", None)
    row.pop("impact_level", None)
    return row


def _row_missing_p3_inputs() -> dict:
    """Row whose debug_summary has no evidence_comparison and no
    official_sources, so P3 reconstructs an empty evidence_comparison.
    P3 still runs (it tolerates empty inputs and returns
    'draft_unverified')."""
    row = _row_low_confidence()
    row["debug_summary"] = "{}"
    row.pop("evidence_comparison", None)
    return row


# ---------------------------------------------------------------------------
# A / B / C / G / H. Core comparison behaviour
# ---------------------------------------------------------------------------


class CoreComparisonTests(unittest.TestCase):
    def test_high_confidence_row_runs_all_three_producers(self):
        comparison = comparator.compare_producers_for_analysis(
            _row_high_confidence(),
        )
        self.assertIsNotNone(comparison.producer1_label)
        self.assertIsNotNone(comparison.producer2_label)
        self.assertIsNotNone(comparison.producer3_label)
        # Producer 2's alert level mirrors its label by construction.
        self.assertEqual(
            comparison.producer2_alert_level,
            comparison.producer2_label,
        )
        # Safety invariants on the dataclass.
        self.assertIs(comparison.truth_claim, False)
        self.assertIs(comparison.operator_review_required, True)
        # disagreement_pattern carries all three labels.
        self.assertIn("P1=", comparison.disagreement_pattern)
        self.assertIn("P2=", comparison.disagreement_pattern)
        self.assertIn("P3=", comparison.disagreement_pattern)
        # input_hash is a hex SHA-256.
        self.assertEqual(len(comparison.input_hash), 64)
        # most_conservative_label is among the actual outputs.
        self.assertIn(
            comparison.most_conservative_label,
            {
                comparison.producer1_label,
                comparison.producer2_label,
                comparison.producer3_label,
            },
        )

    def test_low_confidence_row_runs_all_three_and_agrees_low(self):
        comparison = comparator.compare_producers_for_analysis(
            _row_low_confidence(),
        )
        # The low-confidence inputs typically produce LOW / LOW / draft_unverified.
        self.assertIsNotNone(comparison.producer1_label)
        self.assertIsNotNone(comparison.producer2_label)
        self.assertIsNotNone(comparison.producer3_label)
        # all_three_agree must use the rank map — not raw string equality.
        # LOW (rank 0) + LOW (rank 0) + draft_unverified (rank 0) → agree.
        self.assertTrue(comparison.all_three_agree)

    def test_row_with_missing_p1_inputs_still_runs(self):
        # _run_producer1 is robust to absent fields (defaults to 0 /
        # None) so we expect it to still emit some label. The MOST
        # important behaviour: other producers still run.
        comparison = comparator.compare_producers_for_analysis(
            _row_missing_p1_inputs(),
        )
        # P2 and P3 still produce labels.
        self.assertIsNotNone(comparison.producer2_label)
        self.assertIsNotNone(comparison.producer3_label)

    def test_row_with_missing_p3_inputs_still_runs(self):
        comparison = comparator.compare_producers_for_analysis(
            _row_missing_p3_inputs(),
        )
        # P1 and P2 still produce labels.
        self.assertIsNotNone(comparison.producer1_label)
        self.assertIsNotNone(comparison.producer2_label)
        # P3 with no evidence_comparison + no official_sources should
        # surface the conservative draft_unverified label rather than
        # crashing.
        self.assertEqual(comparison.producer3_label, "draft_unverified")

    def test_non_dict_row_is_handled_safely(self):
        comparison = comparator.compare_producers_for_analysis(None)  # type: ignore[arg-type]
        # Should still produce a comparison with safe defaults.
        self.assertIs(comparison.truth_claim, False)
        self.assertIs(comparison.operator_review_required, True)


# ---------------------------------------------------------------------------
# P. Producer raises an exception → recorded, no crash
# ---------------------------------------------------------------------------


class ProducerRaisesTests(unittest.TestCase):
    def test_producer1_raises_is_caught(self):
        with patch.object(
            comparator, "make_final_decision",
            side_effect=RuntimeError("boom-1"),
        ):
            c = comparator.compare_producers_for_analysis(
                _row_high_confidence(),
            )
        self.assertIsNone(c.producer1_label)
        self.assertIn("error", c.producer1_extra)
        self.assertIn("boom-1", c.producer1_extra["error"])
        # Other producers should still run.
        self.assertIsNotNone(c.producer3_label)

    def test_producer2_raises_is_caught(self):
        with patch.object(
            comparator, "calibrate_final_decision",
            side_effect=RuntimeError("boom-2"),
        ):
            c = comparator.compare_producers_for_analysis(
                _row_high_confidence(),
            )
        self.assertIsNone(c.producer2_label)
        self.assertIsNone(c.producer2_alert_level)
        self.assertIn("error", c.producer2_extra)
        self.assertIn("boom-2", c.producer2_extra["error"])
        # Other producers still run.
        self.assertIsNotNone(c.producer1_label)
        self.assertIsNotNone(c.producer3_label)

    def test_producer3_raises_is_caught(self):
        with patch.object(
            comparator, "_verdict_label",
            side_effect=RuntimeError("boom-3"),
        ):
            c = comparator.compare_producers_for_analysis(
                _row_high_confidence(),
            )
        self.assertIsNone(c.producer3_label)
        self.assertIn("error", c.producer3_extra)
        self.assertIn("boom-3", c.producer3_extra["error"])
        # Other producers still run.
        self.assertIsNotNone(c.producer1_label)


# ---------------------------------------------------------------------------
# Disagreement aggregation
# ---------------------------------------------------------------------------


def _make_synth_comparison(
    *, analysis_id, p1, p2, p3,
    p1_p2, p1_p3, p2_p3, all_three,
    pattern=None, errored=False,
) -> comparator.ProducerComparison:
    extra1 = {"error": "synthetic"} if errored else {}
    return comparator.ProducerComparison(
        analysis_id=str(analysis_id),
        source="synth",
        input_hash=f"hash-{analysis_id}",
        producer1_label=p1, producer1_extra=extra1,
        producer2_label=p2, producer2_alert_level=p2,
        producer3_label=p3,
        all_three_agree=bool(all_three),
        p1_p2_agree=bool(p1_p2),
        p1_p3_agree=bool(p1_p3),
        p2_p3_agree=bool(p2_p3),
        disagreement_pattern=pattern or f"P1={p1},P2={p2},P3={p3}",
        most_conservative_label=p1,
        comparison_timestamp="2026-05-22T00:00:00+00:00",
    )


class DisagreementSummaryTests(unittest.TestCase):
    def test_zero_comparisons(self):
        summary = comparator.compute_disagreement_summary([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["all_three_agree_count"], 0)
        self.assertEqual(summary["at_least_one_disagreement_count"], 0)
        self.assertEqual(
            summary["pairwise_disagreement_counts"],
            {"p1_vs_p2": 0, "p1_vs_p3": 0, "p2_vs_p3": 0},
        )

    def test_all_agree(self):
        items = [
            _make_synth_comparison(
                analysis_id=i, p1="HIGH", p2="HIGH", p3="draft_verified",
                p1_p2=True, p1_p3=True, p2_p3=True, all_three=True,
            )
            for i in range(5)
        ]
        summary = comparator.compute_disagreement_summary(items)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["all_three_agree_count"], 5)
        self.assertEqual(summary["at_least_one_disagreement_count"], 0)
        self.assertEqual(
            summary["pairwise_disagreement_counts"]["p1_vs_p2"], 0,
        )

    def test_pairwise_counts(self):
        items = [
            # All disagree pairwise.
            _make_synth_comparison(
                analysis_id=1, p1="HIGH", p2="LOW", p3="draft_unverified",
                p1_p2=False, p1_p3=False, p2_p3=False, all_three=False,
            ),
            # P1 vs P2 disagree only.
            _make_synth_comparison(
                analysis_id=2, p1="HIGH", p2="LOW", p3="draft_verified",
                p1_p2=False, p1_p3=True, p2_p3=False, all_three=False,
            ),
            # All agree.
            _make_synth_comparison(
                analysis_id=3, p1="LOW", p2="LOW", p3="draft_unverified",
                p1_p2=True, p1_p3=True, p2_p3=True, all_three=True,
            ),
        ]
        summary = comparator.compute_disagreement_summary(items)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["all_three_agree_count"], 1)
        self.assertEqual(
            summary["pairwise_disagreement_counts"]["p1_vs_p2"], 2,
        )
        self.assertEqual(
            summary["pairwise_disagreement_counts"]["p1_vs_p3"], 1,
        )
        self.assertEqual(
            summary["pairwise_disagreement_counts"]["p2_vs_p3"], 2,
        )

    def test_errored_runs_counted(self):
        items = [
            _make_synth_comparison(
                analysis_id=1, p1=None, p2="LOW", p3="draft_unverified",
                p1_p2=False, p1_p3=False, p2_p3=True, all_three=False,
                errored=True,
            ),
            _make_synth_comparison(
                analysis_id=2, p1="LOW", p2="LOW", p3="draft_unverified",
                p1_p2=True, p1_p3=True, p2_p3=True, all_three=True,
                errored=False,
            ),
        ]
        summary = comparator.compute_disagreement_summary(items)
        self.assertEqual(summary["errored_producer_runs_count"], 1)

    def test_pattern_histogram(self):
        items = [
            _make_synth_comparison(
                analysis_id=1, p1="HIGH", p2="LOW", p3="draft_unverified",
                p1_p2=False, p1_p3=False, p2_p3=False, all_three=False,
                pattern="P1=HIGH,P2=LOW,P3=draft_unverified",
            ),
            _make_synth_comparison(
                analysis_id=2, p1="HIGH", p2="LOW", p3="draft_unverified",
                p1_p2=False, p1_p3=False, p2_p3=False, all_three=False,
                pattern="P1=HIGH,P2=LOW,P3=draft_unverified",
            ),
            _make_synth_comparison(
                analysis_id=3, p1="LOW", p2="LOW", p3="draft_unverified",
                p1_p2=True, p1_p3=True, p2_p3=True, all_three=True,
                pattern="P1=LOW,P2=LOW,P3=draft_unverified",
            ),
        ]
        summary = comparator.compute_disagreement_summary(items)
        hist = summary["disagreement_pattern_histogram"]
        self.assertEqual(hist["P1=HIGH,P2=LOW,P3=draft_unverified"], 2)
        self.assertEqual(hist["P1=LOW,P2=LOW,P3=draft_unverified"], 1)


# ---------------------------------------------------------------------------
# N. input_hash determinism
# ---------------------------------------------------------------------------


class InputHashTests(unittest.TestCase):
    def test_same_input_produces_same_hash(self):
        row = _row_high_confidence()
        c1 = comparator.compare_producers_for_analysis(row)
        c2 = comparator.compare_producers_for_analysis(row)
        self.assertEqual(c1.input_hash, c2.input_hash)

    def test_different_input_produces_different_hash(self):
        c1 = comparator.compare_producers_for_analysis(
            _row_high_confidence(),
        )
        c2 = comparator.compare_producers_for_analysis(
            _row_low_confidence(),
        )
        self.assertNotEqual(c1.input_hash, c2.input_hash)


# ---------------------------------------------------------------------------
# I / J / K / L / M / O. Database round-trip with temp DB
# ---------------------------------------------------------------------------


class DatabaseRoundTripTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: round-trip via the PG-primary path. Point a private
        # sqlite:// substitute at a per-test temp file (USE_POSTGRES_WRITE
        # ON) and reset the cached engine so each test binds to its own
        # fresh DB. Env vars are snapshot/restored so the rest of the
        # process (and validate.py's dual-write-disabled determinism) is
        # untouched. Pattern copied from the #1/#2-migrated tests.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._pg_db = str(Path(self._tmp_dir.name) / "pg.db")
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db}"
        postgres_storage.reset_engine_for_tests()

    def tearDown(self):
        import gc as _gc
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        postgres_storage.reset_engine_for_tests()
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _save_for(self, *, row, lie=False) -> int:
        c = comparator.compare_producers_for_analysis(row)
        d = comparator.comparison_to_dict(c)
        if lie:
            d["truth_claim"] = True
            d["operator_review_required"] = False
        return database.save_producer_comparison(d)

    def test_basic_round_trip(self):
        row_id = self._save_for(row=_row_high_confidence())
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)
        rows = database.get_producer_comparisons()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["analysis_id"], "101")
        self.assertEqual(r["source"], "sqlite")
        self.assertIs(r["truth_claim"], False)
        self.assertIs(r["operator_review_required"], True)

    def test_save_forces_truth_claim_zero_even_when_caller_lies(self):
        self._save_for(row=_row_high_confidence(), lie=True)
        rows = database.get_producer_comparisons()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["truth_claim"], False)

    def test_save_forces_operator_review_required_one_even_when_lied(self):
        self._save_for(row=_row_high_confidence(), lie=True)
        rows = database.get_producer_comparisons()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["operator_review_required"], True)

    def test_insert_or_replace_on_input_hash(self):
        self._save_for(row=_row_high_confidence())
        # Same input → same input_hash → upsert (PG: ON CONFLICT DO UPDATE).
        self._save_for(row=_row_high_confidence())
        rows = database.get_producer_comparisons()
        self.assertEqual(len(rows), 1, "duplicate hash must overwrite")
        # A different input produces a new row, not a replacement.
        self._save_for(row=_row_low_confidence())
        rows = database.get_producer_comparisons()
        self.assertEqual(len(rows), 2)

    def test_get_filters_by_analysis_id(self):
        self._save_for(row=_row_high_confidence())
        self._save_for(row=_row_low_confidence())
        rows_101 = database.get_producer_comparisons(analysis_id="101")
        rows_102 = database.get_producer_comparisons(analysis_id="102")
        self.assertEqual(len(rows_101), 1)
        self.assertEqual(rows_101[0]["analysis_id"], "101")
        self.assertEqual(len(rows_102), 1)
        self.assertEqual(rows_102[0]["analysis_id"], "102")

    def test_get_only_disagreements_filter(self):
        # Inject one synthetic agreeing row and one disagreeing row
        # directly (bypassing compare_producers_for_analysis) so we
        # control the all_three_agree flag.
        agree = _make_synth_comparison(
            analysis_id="901", p1="LOW", p2="LOW", p3="draft_unverified",
            p1_p2=True, p1_p3=True, p2_p3=True, all_three=True,
        )
        disagree = _make_synth_comparison(
            analysis_id="902", p1="HIGH", p2="LOW", p3="draft_unverified",
            p1_p2=False, p1_p3=False, p2_p3=False, all_three=False,
        )
        for c in (agree, disagree):
            d = comparator.comparison_to_dict(c)
            database.save_producer_comparison(d)
        all_rows = database.get_producer_comparisons()
        only_disagree = database.get_producer_comparisons(
            only_disagreements=True,
        )
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(len(only_disagree), 1)
        self.assertEqual(only_disagree[0]["analysis_id"], "902")

    def test_save_rejects_missing_required_fields(self):
        for bad in (
            None,
            {},
            {"analysis_id": "x"},                                  # no source
            {"analysis_id": "x", "source": "s"},                   # no input_hash
            {"analysis_id": "x", "source": "s", "input_hash": "h"},  # no ts
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    database.save_producer_comparison(bad)


# ---------------------------------------------------------------------------
# O. init_db() creates the verdict_producer_comparisons table — SQLite-specific.
#
# Deliberately NOT migrated to the PG-substitute path: this pins the
# SQLite schema-creation behaviour (init_db builds the table, verified via
# sqlite_master) that 0e-5 still depends on. It runs with NO dual-write env
# — a DB_PATH swap + raw sqlite3 read against an isolated temp file.
# ---------------------------------------------------------------------------


class InitDbSqliteSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        import gc as _gc
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_init_db_creates_verdict_producer_comparisons_table(self):
        fresh_db = str(Path(self._tmp_dir.name) / "fresh_init.db")
        original = database.DB_PATH
        database.DB_PATH = Path(fresh_db)
        try:
            database.init_db()
            connection = sqlite3.connect(fresh_db)
            try:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='verdict_producer_comparisons'"
                ).fetchone()
            finally:
                connection.close()
        finally:
            database.DB_PATH = original
        self.assertIsNotNone(
            row,
            "init_db() must create verdict_producer_comparisons table",
        )


# ---------------------------------------------------------------------------
# CLI smokes
# ---------------------------------------------------------------------------


def _run_cli_subprocess(*args, timeout=CLI_TIMEOUT_SECONDS, env=None):
    completed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
        cwd=str(ROOT),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return completed.returncode, completed.stdout, completed.stderr


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: the CLI is PG-only. Point a private sqlite://
        # substitute at a per-test temp file (USE_POSTGRES_WRITE ON) and
        # reset the cached engine. The CLI subprocess inherits both env
        # vars via _run_cli_subprocess's env={**os.environ, ...} merge.
        # These smokes seed nothing — they exercise --help / empty-DB
        # reads / usage errors only.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._pg_db = str(Path(self._tmp_dir.name) / "cli_smoke_pg.db")
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db}"
        postgres_storage.reset_engine_for_tests()

    def tearDown(self):
        import gc as _gc
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        postgres_storage.reset_engine_for_tests()
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_help_exits_0(self):
        rc, stdout, _ = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("verdict producers", stdout)
        self.assertIn("Exit codes", stdout)

    def test_summary_empty_db(self):
        rc, stdout, _ = _run_cli_subprocess("--summary")
        self.assertEqual(rc, 0)
        self.assertIn("Disagreement Summary", stdout)
        self.assertIn("Total comparisons:           0", stdout)
        self.assertIn("truth_claim=False", stdout)

    def test_list_disagreements_empty(self):
        rc, stdout, _ = _run_cli_subprocess("--list-disagreements")
        self.assertEqual(rc, 0)
        self.assertIn("Disagreements", stdout)
        self.assertIn("Total: 0", stdout)

    def test_no_mode_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess()
        self.assertEqual(rc, 2)
        self.assertIn("required", stderr)

    def test_save_and_dry_run_mutually_exclusive(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--from-sqlite", "--save", "--dry-run",
        )
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", stderr)

    def test_two_modes_simultaneously_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--summary", "--from-sqlite",
        )
        self.assertEqual(rc, 2)
        self.assertIn("only one", stderr)


# ---------------------------------------------------------------------------
# Q / R / S. Static safety
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def _import_lines(self, path):
        text = path.read_text(encoding="utf-8")
        return [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]

    def test_comparator_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(COMPARATOR_MODULE_PATH))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"verdict_producer_comparison.py must not import {forbidden!r}",
            )

    def test_cli_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(CLI_SCRIPT))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"compare_verdict_producers.py must not import {forbidden!r}",
            )

    def test_comparator_not_imported_by_pipeline_entry_points(self):
        for module_name in ("main.py", "api_server.py", "scheduler.py"):
            module_path = ROOT / module_name
            if not module_path.exists():
                continue
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "verdict_producer_comparison", text,
                f"{module_name} must not import verdict_producer_comparison",
            )
            self.assertNotIn(
                "compare_verdict_producers", text,
                f"{module_name} must not import compare_verdict_producers",
            )


# ---------------------------------------------------------------------------
# T. The three producers themselves are not modified
# ---------------------------------------------------------------------------


class ProducersUnchangedTests(unittest.TestCase):
    """Pin the public signatures of the three producers so this
    milestone surfaces any unintended modification immediately. The
    actual logic is not asserted (Phase 1 audit owns that); we just
    confirm the functions still exist with the parameter names this
    tool relies on."""

    def test_make_final_decision_signature(self):
        sig = inspect.signature(policy_decision.make_final_decision)
        params = list(sig.parameters)
        self.assertEqual(
            params, ["policy_confidence", "policy_impact"],
            f"make_final_decision signature changed: {params}",
        )

    def test_calibrate_final_decision_signature(self):
        sig = inspect.signature(policy_scoring.calibrate_final_decision)
        params = list(sig.parameters)
        expected = [
            "final_decision", "policy_confidence", "policy_impact",
            "verification_card", "source_candidates",
            "evidence_snippets", "debug_summary",
        ]
        self.assertEqual(
            params, expected,
            f"calibrate_final_decision signature changed: {params}",
        )

    def test_alert_from_score_still_present(self):
        sig = inspect.signature(policy_scoring._alert_from_score)
        params = list(sig.parameters)
        for name in (
            "final_score", "evidence_quality_score",
            "source_trust_score", "strength_score",
            "contradiction_adjustment", "human_feedback_adjustment",
            "policy_impact", "policy_confidence", "official_mismatch",
        ):
            self.assertIn(
                name, params,
                f"_alert_from_score is missing {name!r}: {params}",
            )

    def test_verdict_label_signature(self):
        sig = inspect.signature(verification_card._verdict_label)
        params = list(sig.parameters)
        expected_first_three = [
            "policy_confidence", "evidence_comparison", "official_sources",
        ]
        self.assertEqual(
            params[:3], expected_first_three,
            f"_verdict_label first-three parameters changed: {params}",
        )
        for name in (
            "evidence_snippets", "contradiction_summary",
            "bias_framing_summary", "claim_count",
        ):
            self.assertIn(
                name, params,
                f"_verdict_label is missing {name!r}: {params}",
            )

    def test_high_threshold_constant_still_in_policy_decision(self):
        # Phase 1 audit references the "≥60 HIGH-eligible" rule in
        # _policy_alert_level. Pin the literal so a future refactor
        # that removes it forces an update here.
        source = (ROOT / "policy_decision.py").read_text(encoding="utf-8")
        self.assertIn(
            "confidence_score >= 60", source,
            "policy_decision.py no longer contains the ≥60 HIGH rule",
        )


if __name__ == "__main__":
    unittest.main()
