"""Tests for the M12.1 Postgres parity check CLI.

Run with: python tests/test_check_parity.py

No real Postgres is required. The neutralized check_parity module reads
its health block from ``postgres_storage.health_check`` (which we patch)
and the CLI's exit-code policy is pinned end-to-end. (M12.0e-6b-2:
repointed off the retired ``postgres_backfill.collect_status``.)
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
# Env-var scope helper — snapshot/restore the dual-write env vars.
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
    """Synthesize a ``postgres_storage.health_check`` payload (the health
    dict).

    M12.0e-6b-2: check_parity now reads ``postgres_storage.health_check()``
    directly (the retired ``postgres_backfill.collect_status`` wrapped the
    same health dict). This returns just that health dict — not the old
    ``{health, sqlite_counts, postgres_counts}`` wrapper. The count kwargs
    are accepted-but-ignored for call-site back-compat (check_parity's
    neutralized no-op never reads counts)."""
    return {
        "dual_write_enabled": enabled,
        "database_url_present": enabled,
        "engine_available": enabled and can_connect,
        "can_connect": enabled and can_connect,
        "tables_defined": sorted(_EXPECTED_TABLES),
        "error": None if can_connect else "connection refused",
    }


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class ModuleInvariantsTests(unittest.TestCase):
    def test_identity_columns_cover_every_mirror_table(self):
        """The IDENTITY_COLUMNS map must enumerate the 10 mirror tables.

        M12.0e-6b-2: dropped the ``postgres_backfill.get_backfill_specs``
        cross-check (postgres_backfill retired); the ``_EXPECTED_TABLES``
        invariant below preserves the same coverage guarantee."""
        from scripts import check_parity

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
# M12.0e-6b-3: FormatIdentityTests removed with the _format_identity /
# sampling helpers (get_connection retired).
# ---------------------------------------------------------------------------


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

    # M12.0e-6b-3: the 3 --sample tests (test_sample_downgrades_in_parity_
    # when_sets_diverge / test_sample_preview_capped / test_sample_false_
    # skips_set_probe) were removed with the SQLite sampling helpers
    # (get_connection retired). compute_parity_for_table is count-only.


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
# collect_parity_report — patched at the postgres_storage.health_check boundary
# ---------------------------------------------------------------------------


class CollectParityReportTests(unittest.TestCase):
    def test_no_op_pass_when_dual_write_disabled(self):
        """Disabled state is a clean pass: per_table is intentionally
        empty (zeros on the Postgres side would otherwise surface as
        bogus drift), summary reports zero tables checked, no drift."""
        from scripts import check_parity
        import postgres_storage

        # Realistic disabled-state shape: SQLite counts are real, the
        # Postgres counts are zero because no engine could be built.
        sqlite_counts = {n: 5 for n in _EXPECTED_TABLES}
        postgres_counts = {n: 0 for n in _EXPECTED_TABLES}
        with patch.object(
            postgres_storage, "health_check",
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

    def test_only_table_is_recorded_but_per_table_empty(self):
        """M12.0e-5b: neutralized no-op. The only_table arg is still
        echoed in the report, but per_table is always empty (no
        comparison) regardless of the patched counts."""
        from scripts import check_parity
        import postgres_storage

        with patch.object(
            postgres_storage, "health_check",
            return_value=_make_status(
                sqlite_counts={n: 5 for n in _EXPECTED_TABLES},
                postgres_counts={n: 5 for n in _EXPECTED_TABLES},
            ),
        ):
            report = check_parity.collect_parity_report(
                only_table="analysis_results",
            )

        self.assertEqual(report["per_table"], {})
        self.assertEqual(report["summary"]["tables_checked"], 0)
        self.assertFalse(report["summary"]["any_drift"])
        self.assertEqual(report["only_table"], "analysis_results")

    def test_count_drift_is_ignored_under_pg_only(self):
        """M12.0e-5b: even when the patched status shows mismatched
        counts, the neutralized report performs NO comparison —
        per_table stays empty and any_drift is False. SQLite is no
        longer written, so count drift is meaningless."""
        from scripts import check_parity
        import postgres_storage

        sqlite_counts = {n: 5 for n in _EXPECTED_TABLES}
        postgres_counts = dict(sqlite_counts)
        postgres_counts["analysis_results"] = 3  # would-be drift

        with patch.object(
            postgres_storage, "health_check",
            return_value=_make_status(
                sqlite_counts=sqlite_counts,
                postgres_counts=postgres_counts,
            ),
        ):
            report = check_parity.collect_parity_report()

        self.assertEqual(report["per_table"], {})
        self.assertFalse(report["summary"]["any_drift"])
        self.assertEqual(report["summary"]["drift_tables"], [])
        self.assertEqual(report["summary"]["total_delta_abs"], 0)


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
        import postgres_storage

        with _EnvScope():
            os.environ.pop("USE_POSTGRES_WRITE", None)
            os.environ.pop("DATABASE_URL", None)
            with patch.object(
                postgres_storage, "health_check",
                return_value=_make_status(enabled=False, can_connect=False),
            ):
                code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("Dual-Write Parity", stdout)
        self.assertIn("sole source of truth", stdout)

    def test_returns_0_when_parity_holds(self):
        import postgres_storage

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_storage, "health_check",
                return_value=_make_status(
                    sqlite_counts={n: 7 for n in _EXPECTED_TABLES},
                    postgres_counts={n: 7 for n in _EXPECTED_TABLES},
                ),
            ):
                code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("parity OK", stdout)

    def test_returns_0_when_enabled_but_unreachable(self):
        """M12.0e-5b: under PG-only the parity check is a no-op that
        always exits 0 — there is nothing to probe or compare, so an
        unreachable Postgres is irrelevant to the parity exit code.
        Replaces the pre-5b strict/non-strict + drift exit-1 tests,
        which exercised comparison paths that no longer run."""
        import postgres_storage

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_storage, "health_check",
                return_value=_make_status(
                    enabled=True, can_connect=False,
                ),
            ):
                code, stdout, _ = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn("parity OK", stdout)

    def test_strict_flag_is_accepted_but_still_exits_0(self):
        """M12.0e-5b: --strict is retained for CLI back-compat but has
        no effect — the neutralized no-op always exits 0."""
        import postgres_storage

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_storage, "health_check",
                return_value=_make_status(
                    enabled=True, can_connect=False,
                ),
            ):
                code, _, _ = self._run_main(["--strict"])
        self.assertEqual(code, 0)

    def test_json_output_is_valid_json_with_expected_keys(self):
        import postgres_storage

        with _EnvScope():
            os.environ["USE_POSTGRES_WRITE"] = "true"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://x/y"
            with patch.object(
                postgres_storage, "health_check",
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
        # M12.0e-5b: per_table is empty under the neutralized no-op; the
        # key is still present so consumers read the same shape.
        self.assertEqual(payload["per_table"], {})
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
        import postgres_storage
        from scripts import check_parity

        with patch.object(
            postgres_storage, "health_check",
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
