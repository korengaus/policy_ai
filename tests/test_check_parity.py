"""Tests for the M12.1 Postgres parity check CLI.

Run with: python tests/test_check_parity.py

No real Postgres is required. The check_parity module reads counts from
``postgres_backfill.collect_status`` (which we patch) and identity
tuples from helper functions we exercise either directly or via patch.
The CLI's exit-code policy is also pinned end-to-end.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Env-var scope helper — same shape as test_postgres_backfill.py.
# ---------------------------------------------------------------------------


class _EnvScope:
    """Snapshot/restore the dual-write env vars + cached engine."""

    KEYS = ("USE_POSTGRES_WRITE", "DATABASE_URL")

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import postgres_storage

        postgres_storage.reset_engine_for_tests()


# Mirror tables — duplicated so a future drift in either side is caught.
_EXPECTED_TABLES = {
    "analysis_results",
    "jobs",
    "embedding_cache",
    "review_tasks",
    "review_decisions",
    "source_fetch_artifacts",
    "artifact_text_extractions",
    "artifact_evidence_candidates",
    "verdict_producer_comparisons",
    "verdict_label_attributions",
}


def _make_status(
    *,
    enabled: bool = True,
    can_connect: bool = True,
    sqlite_counts=None,
    postgres_counts=None,
) -> dict:
    """Synthesize a ``postgres_backfill.collect_status`` payload."""
    if sqlite_counts is None:
        sqlite_counts = {name: 0 for name in _EXPECTED_TABLES}
    if postgres_counts is None:
        postgres_counts = {name: 0 for name in _EXPECTED_TABLES}
    return {
        "health": {
            "dual_write_enabled": enabled,
            "database_url_present": enabled,
            "engine_available": enabled and can_connect,
            "can_connect": enabled and can_connect,
            "tables_defined": sorted(_EXPECTED_TABLES),
            "error": None if can_connect else "connection refused",
        },
        "sqlite_counts": dict(sqlite_counts),
        "postgres_counts": dict(postgres_counts),
    }


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class ModuleInvariantsTests(unittest.TestCase):
    def test_identity_columns_cover_every_mirror_table(self):
        """The IDENTITY_COLUMNS map must enumerate the same 10 tables
        that postgres_backfill.get_backfill_specs exposes — otherwise
        a future table addition would silently skip the --sample mode."""
        import postgres_backfill
        from scripts import check_parity

        spec_names = {
            s.table_name for s in postgres_backfill.get_backfill_specs()
        }
        self.assertSetEqual(
            set(check_parity._IDENTITY_COLUMNS.keys()), spec_names
        )
        self.assertSetEqual(
            set(check_parity._IDENTITY_COLUMNS.keys()), _EXPECTED_TABLES
        )

    def test_valid_table_names_matches_identity_map(self):
        from scripts import check_parity

        self.assertSetEqual(
            check_parity._valid_table_names(),
            set(check_parity._IDENTITY_COLUMNS.keys()),
        )

    def test_max_preview_per_side_is_bounded(self):
        """Sanity check on the bounded-preview constant — drift reports
        must never flood the terminal even when thousands of rows
        diverged."""
        from scripts import check_parity

        self.assertGreaterEqual(check_parity._MAX_PREVIEW_PER_SIDE, 5)
        self.assertLessEqual(check_parity._MAX_PREVIEW_PER_SIDE, 100)


# ---------------------------------------------------------------------------
# _format_identity
# ---------------------------------------------------------------------------


class FormatIdentityTests(unittest.TestCase):
    def test_format_identity_single_value(self):
        from scripts import check_parity

        self.assertEqual(
            check_parity._format_identity((42,), ["id"]), "42"
        )

    def test_format_identity_composite_key(self):
        from scripts import check_parity

        self.assertEqual(
            check_parity._format_identity(
                ("hash1", "openai", "text-embed"),
                ["text_hash", "provider", "model"],
            ),
            "hash1|openai|text-embed",
        )

    def test_format_identity_none_becomes_empty_string(self):
        from scripts import check_parity

        self.assertEqual(
            check_parity._format_identity((None, "openai"), ["a", "b"]),
            "|openai",
        )


# ---------------------------------------------------------------------------
# compute_parity_for_table
# ---------------------------------------------------------------------------


class ComputeParityTests(unittest.TestCase):
    def test_in_parity_when_counts_match(self):
        from scripts import check_parity

        record = check_parity.compute_parity_for_table(
            "analysis_results", 107, 107,
        )
        self.assertTrue(record["in_parity"])
        self.assertEqual(record["delta"], 0)
        self.assertFalse(record["sampled"])
        self.assertEqual(record["identity_columns"], ["id"])

    def test_drift_when_counts_differ(self):
        from scripts import check_parity

        record = check_parity.compute_parity_for_table(
            "embedding_cache", 700, 694,
        )
        self.assertFalse(record["in_parity"])
        self.assertEqual(record["delta"], 6)

    def test_delta_sign_is_sqlite_minus_postgres(self):
        """Positive delta means SQLite has more rows (Postgres behind);
        negative means Postgres has rows that SQLite does not."""
        from scripts import check_parity

        ahead = check_parity.compute_parity_for_table(
            "jobs", 100, 90,
        )
        self.assertEqual(ahead["delta"], 10)

        behind = check_parity.compute_parity_for_table(
            "jobs", 90, 100,
        )
        self.assertEqual(behind["delta"], -10)

    def test_sample_downgrades_in_parity_when_sets_diverge(self):
        """Same counts but different identities still counts as drift
        when --sample mode is active."""
        from scripts import check_parity

        with patch.object(
            check_parity, "_sample_sqlite_identities",
            return_value=[(1,), (2,), (3,)],
        ), patch.object(
            check_parity, "_sample_postgres_identities",
            return_value=[(1,), (2,), (99,)],
        ):
            record = check_parity.compute_parity_for_table(
                "analysis_results", 3, 3,
                engine=object(), sample=True, sample_limit=100,
            )
        self.assertFalse(record["in_parity"])
        self.assertEqual(record["sqlite_only_count"], 1)
        self.assertEqual(record["postgres_only_count"], 1)
        self.assertIn("3", record["sqlite_only_preview"])
        self.assertIn("99", record["postgres_only_preview"])

    def test_sample_preview_capped(self):
        """Drift previews never exceed _MAX_PREVIEW_PER_SIDE entries per
        side, regardless of how large the set difference is."""
        from scripts import check_parity

        sqlite_ids = [(i,) for i in range(200)]
        postgres_ids = []  # massive sqlite_only drift

        with patch.object(
            check_parity, "_sample_sqlite_identities",
            return_value=sqlite_ids,
        ), patch.object(
            check_parity, "_sample_postgres_identities",
            return_value=postgres_ids,
        ):
            record = check_parity.compute_parity_for_table(
                "analysis_results", 200, 0,
                engine=object(), sample=True, sample_limit=500,
            )
        self.assertEqual(record["sqlite_only_count"], 200)
        self.assertLessEqual(
            len(record["sqlite_only_preview"]),
            check_parity._MAX_PREVIEW_PER_SIDE,
        )

    def test_sample_false_skips_set_probe(self):
        """When sample=False, the function must not touch the identity
        helpers at all — keeps the default mode O(2 counts)."""
        from scripts import check_parity

        with patch.object(
            check_parity, "_sample_sqlite_identities",
        ) as sqlite_mock, patch.object(
            check_parity, "_sample_postgres_identities",
        ) as pg_mock:
            check_parity.compute_parity_for_table(
                "analysis_results", 5, 5, sample=False,
            )
        sqlite_mock.assert_not_called()
        pg_mock.assert_not_called()


# ---------------------------------------------------------------------------
# summarize_parity
# ---------------------------------------------------------------------------


class SummarizeParityTests(unittest.TestCase):
    def test_summarize_when_all_in_parity(self):
        from scripts import check_parity

        per_table = {
            "a": {"in_parity": True, "delta": 0},
            "b": {"in_parity": True, "delta": 0},
        }
        summary = check_parity.summarize_parity(per_table)
        self.assertEqual(summary["tables_checked"], 2)
        self.assertEqual(summary["tables_in_parity"], 2)
        self.assertEqual(summary["tables_with_drift"], 0)
        self.assertFalse(summary["any_drift"])
        self.assertEqual(summary["drift_tables"], [])
        self.assertEqual(summary["total_delta_abs"], 0)

    def test_summarize_with_mixed_drift(self):
        from scripts import check_parity

        per_table = {
            "z_table": {"in_parity": False, "delta": -3},
            "a_table": {"in_parity": False, "delta": 7},
            "m_table": {"in_parity": True, "delta": 0},
        }
        summary = check_parity.summarize_parity(per_table)
        self.assertTrue(summary["any_drift"])
        self.assertEqual(summary["tables_with_drift"], 2)
        # drift_tables is sorted for stable JSON output.
        self.assertEqual(summary["drift_tables"], ["a_table", "z_table"])
        self.assertEqual(summary["total_delta_abs"], 10)


# ---------------------------------------------------------------------------
# collect_parity_report — patched at the postgres_backfill boundary
# ---------------------------------------------------------------------------


class CollectParityReportTests(unittest.TestCase):
    def test_no_op_pass_when_dual_write_disabled(self):
        """Disabled state is a clean pass: per_table is intentionally
        empty (zeros on the Postgres side would otherwise surface as
        bogus drift), summary reports zero tables checked, no drift."""
        from scripts import check_parity
        import postgres_backfill

        # Realistic disabled-state shape: SQLite counts are real, the
        # Postgres counts are zero because no engine could be built.
        sqlite_counts = {n: 5 for n in _EXPECTED_TABLES}
        postgres_counts = {n: 0 for n in _EXPECTED_TABLES}
        with patch.object(
            postgres_backfill, "collect_status",
            return_value=_make_status(
                enabled=False, can_connect=False,
                sqlite_counts=sqlite_counts,
                postgres_counts=postgres_counts,
            ),
        ):
            report = check_parity.collect_parity_report()

        self.assertFalse(report["health"]["dual_write_enabled"])
        self.assertEqual(report["per_table"], {})
        self.assertEqual(report["summary"]["tables_checked"], 0)
        self.assertFalse(report["summary"]["any_drift"])

    def test_only_table_filter_restricts_report(self):
        from scripts import check_parity
        import postgres_backfill

        with patch.object(
            postgres_backfill, "collect_status",
            return_value=_make_status(
                sqlite_counts={n: 5 for n in _EXPECTED_TABLES},
                postgres_counts={n: 5 for n in _EXPECTED_TABLES},
            ),
        ):
            report = check_parity.collect_parity_report(
                only_table="analysis_results",
            )

        self.assertEqual(list(report["per_table"].keys()),
                         ["analysis_results"])
        self.assertEqual(report["summary"]["tables_checked"], 1)
        self.assertEqual(report["only_table"], "analysis_results")

    def test_drift_in_one_table_flags_summary(self):
        from scripts import check_parity
        import postgres_backfill

        sqlite_counts = {n: 5 for n in _EXPECTED_TABLES}
        postgres_counts = dict(sqlite_counts)
        postgres_counts["analysis_results"] = 3  # drift!

        with patch.object(
            postgres_backfill, "collect_status",
            return_value=_make_status(
                sqlite_counts=sqlite_counts,
                postgres_counts=postgres_counts,
            ),
        ):
            report = check_parity.collect_parity_report()

        self.assertTrue(report["summary"]["any_drift"])
        self.assertEqual(report["summary"]["drift_tables"],
                         ["analysis_results"])
        self.assertEqual(report["summary"]["total_delta_abs"], 2)


# ---------------------------------------------------------------------------
# main() — CLI exit policy
# ---------------------------------------------------------------------------


class MainExitPolicyTests(unittest.TestCase):
    def _run_main(self, argv):
        """Run main and return (exit_code, stdout, stderr)."""
        from scripts import check_parity

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = check_parity.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_returns_2_on_unknown_table(self):
        with _EnvScope():
            os.environ.pop("USE_POSTGRES_WRITE", None)
            os.environ.pop("DATABASE_URL", None)
            code, stdout, stderr = self._run_main(["--table", "no_such"])
        self.assertEqual(code, 2)
        self.assertIn("--table must be one of", stderr)

    def test_returns_2_on_negative_sample_limit(self):
        with _EnvScope():
            code, stdout, stderr = self._run_main(["--sample-limit", "-3"])
        self.assertEqual(code, 2)
        self.assertIn("sample-limit", stderr)

    def test_returns_0_when_dual_write_disabled(self):
        from scripts import check_parity
        import postgres_backfill

        with _EnvScope():
            os.environ.pop("USE_POSTGRES_WRITE", None)
            os.environ.pop("DATABASE_URL", None)
            with patch.object(
                postgres_backfill, "collect_status",
                return_value=_make_status(enabled=False, can_connect=False),
            ):
                code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("Dual-Write Parity", stdout)
        self.assertIn("sole source of truth", stdout)

    def test_returns_0_when_parity_holds(self):
        import postgres_backfill

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_backfill, "collect_status",
                return_value=_make_status(
                    sqlite_counts={n: 7 for n in _EXPECTED_TABLES},
                    postgres_counts={n: 7 for n in _EXPECTED_TABLES},
                ),
            ):
                code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("parity OK", stdout)

    def test_returns_1_when_drift_detected(self):
        import postgres_backfill

        sqlite_counts = {n: 7 for n in _EXPECTED_TABLES}
        postgres_counts = dict(sqlite_counts)
        postgres_counts["jobs"] = 3

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_backfill, "collect_status",
                return_value=_make_status(
                    sqlite_counts=sqlite_counts,
                    postgres_counts=postgres_counts,
                ),
            ):
                code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 1)
        self.assertIn("DRIFT detected", stdout)

    def test_strict_returns_1_when_enabled_but_unreachable(self):
        import postgres_backfill

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_backfill, "collect_status",
                return_value=_make_status(
                    enabled=True, can_connect=False,
                ),
            ):
                code, _, _ = self._run_main(["--strict"])
        self.assertEqual(code, 1)

    def test_non_strict_returns_0_when_enabled_but_unreachable(self):
        """Without --strict, unreachable Postgres is informational; the
        operator may run the script before bringing the DB online."""
        import postgres_backfill

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_backfill, "collect_status",
                return_value=_make_status(
                    enabled=True, can_connect=False,
                ),
            ):
                code, stdout, _ = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("SELECT 1 probe failed", stdout)

    def test_json_output_is_valid_json_with_expected_keys(self):
        import postgres_backfill

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_backfill, "collect_status",
                return_value=_make_status(
                    sqlite_counts={n: 1 for n in _EXPECTED_TABLES},
                    postgres_counts={n: 1 for n in _EXPECTED_TABLES},
                ),
            ):
                code, stdout, _ = self._run_main(["--json"])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertIn("health", payload)
        self.assertIn("summary", payload)
        self.assertIn("per_table", payload)
        self.assertIn("analysis_results", payload["per_table"])
        self.assertFalse(payload["summary"]["any_drift"])


# ---------------------------------------------------------------------------
# Read-only contract — exercised end-to-end against the real backfill
# status helper. The check_parity module must never call any helper that
# writes; pin that contract here.
# ---------------------------------------------------------------------------


class ReadOnlyContractTests(unittest.TestCase):
    def test_collect_parity_report_never_writes_to_postgres(self):
        """No mirror_write / mirror_upsert / ensure_schema call may
        happen during a parity check. We patch the writer surface and
        assert it stays untouched."""
        import postgres_backfill
        import postgres_storage
        from scripts import check_parity

        with patch.object(
            postgres_backfill, "collect_status",
            return_value=_make_status(),
        ), patch.object(
            postgres_storage, "mirror_write",
        ) as write_mock, patch.object(
            postgres_storage, "mirror_write_returning",
        ) as write_returning_mock, patch.object(
            postgres_storage, "mirror_upsert",
        ) as upsert_mock, patch.object(
            postgres_storage, "ensure_schema",
        ) as ensure_mock:
            check_parity.collect_parity_report()

        write_mock.assert_not_called()
        write_returning_mock.assert_not_called()
        upsert_mock.assert_not_called()
        ensure_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
