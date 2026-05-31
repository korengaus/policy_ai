"""Tests for the M12.0b Postgres backfill module + CLI.

Run with: python tests/test_postgres_backfill.py

No real Postgres is required. Integration tests use ``sqlite:///<tmp>``
SQLAlchemy URLs as a substrate for the postgres_storage helpers; the
SQLite-as-Postgres branch of ``mirror_upsert`` exercises the upsert
contract closely enough to verify backfill idempotency and overwrite
semantics. SQLite source rows live in a separate temp file so the
real ``policy_ai.db`` is never touched.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Env-var scope helper — identical pattern to test_postgres_storage.py.
# ---------------------------------------------------------------------------


class _EnvScope:
    """Snapshot/restore USE_POSTGRES_WRITE and DATABASE_URL."""

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


def _set_env(**values):
    import postgres_storage

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Spec catalogue tests
# ---------------------------------------------------------------------------


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


class SpecCatalogTests(unittest.TestCase):
    def test_get_backfill_specs_returns_ten_specs(self):
        import postgres_backfill

        specs = postgres_backfill.get_backfill_specs()
        self.assertEqual(len(specs), 10)

    def test_specs_cover_every_mirror_table(self):
        import postgres_backfill

        names = {s.table_name for s in postgres_backfill.get_backfill_specs()}
        self.assertSetEqual(names, _EXPECTED_TABLES)

    def test_every_spec_has_callable_reader(self):
        import postgres_backfill

        for spec in postgres_backfill.get_backfill_specs():
            self.assertTrue(
                callable(spec.sqlite_reader),
                msg=f"{spec.table_name} has non-callable sqlite_reader",
            )

    def test_every_spec_uses_documented_strategy(self):
        import postgres_backfill

        for spec in postgres_backfill.get_backfill_specs():
            self.assertIn(
                spec.idempotency_strategy,
                postgres_backfill.IDEMPOTENCY_STRATEGIES,
                msg=(
                    f"{spec.table_name} has unknown strategy "
                    f"{spec.idempotency_strategy!r}"
                ),
            )

    def test_upsert_specs_have_conflict_columns(self):
        import postgres_backfill

        for spec in postgres_backfill.get_backfill_specs():
            if spec.idempotency_strategy == "upsert_by_columns":
                self.assertTrue(
                    spec.conflict_columns,
                    msg=(
                        f"{spec.table_name} uses upsert_by_columns "
                        "but has empty conflict_columns"
                    ),
                )

    def test_specs_match_documented_constraints(self):
        """Pin the exact strategy + conflict columns for tables with
        UNIQUE constraints — protects against accidental strategy drift."""
        import postgres_backfill

        by_name = {
            s.table_name: s for s in postgres_backfill.get_backfill_specs()
        }
        self.assertEqual(
            by_name["embedding_cache"].idempotency_strategy,
            "upsert_by_columns",
        )
        self.assertEqual(
            sorted(by_name["embedding_cache"].conflict_columns),
            sorted(["text_hash", "provider", "model"]),
        )
        self.assertEqual(
            by_name["review_tasks"].idempotency_strategy,
            "upsert_by_columns",
        )
        self.assertEqual(
            by_name["review_tasks"].conflict_columns, ["idempotency_key"],
        )
        self.assertEqual(
            by_name["verdict_producer_comparisons"].idempotency_strategy,
            "upsert_by_columns",
        )
        self.assertEqual(
            by_name["verdict_producer_comparisons"].conflict_columns,
            ["input_hash"],
        )
        self.assertEqual(
            by_name["verdict_label_attributions"].idempotency_strategy,
            "upsert_by_columns",
        )
        self.assertEqual(
            by_name["verdict_label_attributions"].conflict_columns,
            ["analysis_id"],
        )


# ---------------------------------------------------------------------------
# backfill_table behaviour
# ---------------------------------------------------------------------------


class BackfillTableDisabledStateTests(unittest.TestCase):
    """When dual-write is disabled, backfill_table returns a clean
    BackfillResult with an error message and zero counts."""

    def test_returns_error_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_backfill

            spec = postgres_backfill.get_backfill_specs()[0]
            result = postgres_backfill.backfill_table(spec, dry_run=True)
            self.assertEqual(result.rows_read, 0)
            self.assertEqual(result.rows_inserted, 0)
            self.assertTrue(
                any("USE_POSTGRES_WRITE" in e for e in result.errors),
                msg=f"errors did not mention USE_POSTGRES_WRITE: {result.errors}",
            )

    def test_returns_error_when_database_url_missing(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true", DATABASE_URL="")
            import postgres_backfill

            spec = postgres_backfill.get_backfill_specs()[0]
            result = postgres_backfill.backfill_table(spec, dry_run=False)
            self.assertEqual(result.rows_inserted, 0)
            self.assertTrue(
                any("engine" in e.lower() or "USE_POSTGRES_WRITE" in e
                    for e in result.errors),
                msg=f"unexpected errors: {result.errors}",
            )


class BackfillIntegrationTests(unittest.TestCase):
    """End-to-end backfill against a temp SQLite source and a separate
    sqlite:///<tmp> Postgres substitute."""

    def _seed_sqlite(self, db_path: Path):
        """Seed an isolated SQLite DB with a handful of rows across
        the mirror-table set.

        Seeding is performed with USE_POSTGRES_WRITE temporarily
        disabled so the M12.0a dual-write hooks inside database.py
        do not fire against a Postgres substrate whose schema does
        not exist yet (each test sets up the Postgres substrate
        AFTER seeding). The env-scope outside this helper handles
        the broader env restoration.
        """
        # Snapshot current env values and disable dual-write for
        # the duration of the seed. The caller's _EnvScope restores
        # the original values on exit.
        prior_flag = os.environ.get("USE_POSTGRES_WRITE")
        prior_url = os.environ.get("DATABASE_URL")
        os.environ.pop("USE_POSTGRES_WRITE", None)
        os.environ.pop("DATABASE_URL", None)
        import postgres_storage

        postgres_storage.reset_engine_for_tests()
        with patch("database.DB_PATH", db_path):
            import database

            database.init_db()
            database.init_review_tables()
            database.init_source_fetch_artifacts_table()
            database.init_artifact_text_extractions_table()
            database.init_artifact_evidence_candidates_table()
            database.init_verdict_producer_comparisons_table()
            database.init_verdict_label_attributions_table()

            # 3 analysis_results rows.
            # M12.0e-5a: save_analysis_result is PG-only (the SQLite write
            # fallback was removed), so seed the SQLite source the backfill
            # tool reads via raw SQL — same pattern as the review_tasks /
            # review_decisions seed below. Columns mirror what the old
            # save_analysis_result stored for this sample (the rest NULL).
            with database.get_connection() as connection:
                for index in range(3):
                    connection.execute(
                        """
                        INSERT INTO analysis_results (
                            query, title, original_url, topic,
                            claim_text, verdict_label, verdict_confidence,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"q-{index}", f"row-{index}",
                            f"https://example.com/r{index}", "test",
                            "주장", "draft_likely_true", 70,
                            "2026-05-23T00:00:00",
                        ),
                    )
                connection.commit()

            # 2 review_tasks rows with distinct idempotency keys.
            # M12.0d Stage 3c-2: database.create_review_task no longer
            # writes to SQLite (PG-only), so seed via raw SQL against the
            # local SQLite source the backfill tool reads. The backfill
            # tool itself still supports these tables — we just bypass
            # the production write path for test seeding.
            with database.get_connection() as connection:
                for index in range(2):
                    connection.execute(
                        """
                        INSERT INTO review_tasks (
                            task_id, result_id, job_id, item_index, status,
                            query, claim_text, title, url,
                            final_decision, policy_confidence,
                            human_review_required, snapshot_json,
                            created_at, updated_at, idempotency_key
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"t-{index}", str(index + 1), f"j-{index}", 0,
                            "open", f"q-{index}", "주장", f"row-{index}",
                            f"https://example.com/r{index}",
                            "WATCH", "60", 1, '{"k": "v"}',
                            "2026-05-23T00:00:00", "2026-05-23T00:00:00",
                            f"idem-{index}",
                        ),
                    )

                # 1 review_decision row.
                connection.execute(
                    """
                    INSERT INTO review_decisions (
                        decision_id, task_id, decision,
                        created_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("d-1", "t-0", "approve", "2026-05-23T00:00:01", "{}"),
                )
                connection.commit()

            # 1 source_fetch_artifact row.
            # M12.0e-5a: save_fetch_artifact is PG-only; seed the SQLite
            # source via raw SQL (same precedent). truth_claim /
            # official_source_candidate forced to 0 as the old save did.
            with database.get_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO source_fetch_artifacts (
                        source_id, url, fetch_timestamp, success,
                        truth_claim, official_source_candidate, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "kr_law_open_data_candidate",
                        "https://www.law.go.kr/test",
                        "2026-05-23T00:00:00", 1, 0, 0,
                        "2026-05-23T00:00:00",
                    ),
                )
                connection.commit()

        # Restore the env values the test set up before seeding so
        # the caller can rely on them when invoking backfill.
        if prior_flag is None:
            os.environ.pop("USE_POSTGRES_WRITE", None)
        else:
            os.environ["USE_POSTGRES_WRITE"] = prior_flag
        if prior_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_url
        postgres_storage.reset_engine_for_tests()

    def test_dry_run_writes_nothing_but_reports_counts(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)
                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    results = postgres_backfill.backfill_all_tables(
                        dry_run=True,
                    )
                    analysis_result = next(
                        r for r in results
                        if r.table_name == "analysis_results"
                    )
                    self.assertEqual(analysis_result.rows_read, 3)
                    self.assertEqual(analysis_result.rows_inserted, 3)
                    self.assertEqual(
                        analysis_result.rows_errored, 0,
                    )

                    # Confirm Postgres substrate is still empty.
                    engine = postgres_storage.get_engine()
                    with engine.connect() as conn:
                        count = conn.execute(
                            sa.text("SELECT COUNT(*) FROM analysis_results")
                        ).scalar()
                    self.assertEqual(count, 0)
                    postgres_storage.reset_engine_for_tests()

    def test_execute_mode_writes_rows(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)
                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    results = postgres_backfill.backfill_all_tables(
                        dry_run=False,
                    )
                    by_name = {r.table_name: r for r in results}
                    self.assertEqual(
                        by_name["analysis_results"].rows_inserted, 3,
                    )
                    self.assertEqual(
                        by_name["review_tasks"].rows_inserted, 2,
                    )
                    self.assertEqual(
                        by_name["review_decisions"].rows_inserted, 1,
                    )
                    self.assertEqual(
                        by_name["source_fetch_artifacts"].rows_inserted, 1,
                    )

                    engine = postgres_storage.get_engine()
                    with engine.connect() as conn:
                        ar = conn.execute(
                            sa.text("SELECT COUNT(*) FROM analysis_results")
                        ).scalar()
                        rt = conn.execute(
                            sa.text("SELECT COUNT(*) FROM review_tasks")
                        ).scalar()
                        rd = conn.execute(
                            sa.text("SELECT COUNT(*) FROM review_decisions")
                        ).scalar()
                        fa = conn.execute(
                            sa.text("SELECT COUNT(*) FROM source_fetch_artifacts")
                        ).scalar()
                    self.assertEqual(ar, 3)
                    self.assertEqual(rt, 2)
                    self.assertEqual(rd, 1)
                    self.assertEqual(fa, 1)
                    postgres_storage.reset_engine_for_tests()

    def test_idempotent_rerun_skips_existing_rows(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)
                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    postgres_backfill.backfill_all_tables(dry_run=False)
                    # Second run — every row is already present.
                    second = postgres_backfill.backfill_all_tables(
                        dry_run=False,
                    )
                    by_name = {r.table_name: r for r in second}
                    self.assertEqual(
                        by_name["analysis_results"].rows_inserted, 0,
                    )
                    self.assertEqual(
                        by_name["analysis_results"].rows_skipped_existing, 3,
                    )
                    # Upsert tables touch existing rows on re-run, but the
                    # data is byte-identical so the final state is the
                    # same row count.
                    engine = postgres_storage.get_engine()
                    with engine.connect() as conn:
                        ar = conn.execute(
                            sa.text("SELECT COUNT(*) FROM analysis_results")
                        ).scalar()
                        rt = conn.execute(
                            sa.text("SELECT COUNT(*) FROM review_tasks")
                        ).scalar()
                    self.assertEqual(ar, 3)
                    self.assertEqual(rt, 2)
                    postgres_storage.reset_engine_for_tests()

    def test_upsert_updates_changed_row(self):
        """Modify a SQLite review_tasks row's claim_text and re-run
        backfill. The mirror row should reflect the updated value."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)
                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    postgres_backfill.backfill_all_tables(dry_run=False)

                    # Mutate the SQLite source directly. The backfill
                    # is read-only against SQLite at the module level
                    # but the test is allowed to seed mutations.
                    conn = sqlite3.connect(src_db)
                    try:
                        conn.execute(
                            "UPDATE review_tasks SET claim_text = ? "
                            "WHERE idempotency_key = ?",
                            ("updated-claim", "idem-0"),
                        )
                        conn.commit()
                    finally:
                        conn.close()

                    postgres_backfill.backfill_all_tables(dry_run=False)

                    engine = postgres_storage.get_engine()
                    with engine.connect() as conn:
                        row = conn.execute(
                            sa.text(
                                "SELECT claim_text FROM review_tasks "
                                "WHERE idempotency_key = :k"
                            ),
                            {"k": "idem-0"},
                        ).fetchone()
                    self.assertEqual(row[0], "updated-claim")
                    postgres_storage.reset_engine_for_tests()

    def test_only_table_filter_limits_specs(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)
                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    results = postgres_backfill.backfill_all_tables(
                        dry_run=True,
                        only_table="review_tasks",
                    )
                    self.assertEqual(len(results), 1)
                    self.assertEqual(
                        results[0].table_name, "review_tasks",
                    )
                    self.assertEqual(results[0].rows_inserted, 2)
                    postgres_storage.reset_engine_for_tests()

    def test_backfill_never_modifies_sqlite(self):
        """Pin the read-only contract — row counts in SQLite must be
        identical before and after a backfill run."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)
                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    before = {
                        name: postgres_backfill._count_rows(name)
                        for name in postgres_backfill.TABLE_READERS
                    }
                    postgres_backfill.backfill_all_tables(dry_run=False)
                    after = {
                        name: postgres_backfill._count_rows(name)
                        for name in postgres_backfill.TABLE_READERS
                    }
                    self.assertEqual(before, after)
                    postgres_storage.reset_engine_for_tests()

    def test_per_row_error_does_not_abort_run(self):
        """A row that fails the transformer must be captured in
        ``errors`` without stopping the loop."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                src_db = Path(tmp_dir) / "src.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                self._seed_sqlite(src_db)

                def broken_transformer(row):
                    if row.get("id") == 2:
                        raise RuntimeError("boom on id=2")
                    return row

                with patch("database.DB_PATH", src_db):
                    import postgres_backfill
                    import postgres_storage

                    spec = next(
                        s for s in postgres_backfill.get_backfill_specs()
                        if s.table_name == "analysis_results"
                    )
                    spec.row_transformer = broken_transformer
                    try:
                        result = postgres_backfill.backfill_table(
                            spec, dry_run=False,
                        )
                    finally:
                        spec.row_transformer = None
                    self.assertEqual(result.rows_read, 3)
                    self.assertEqual(result.rows_errored, 1)
                    self.assertEqual(result.rows_inserted, 2)
                    self.assertTrue(
                        any("boom on id=2" in e for e in result.errors),
                        msg=f"unexpected errors: {result.errors}",
                    )
                    postgres_storage.reset_engine_for_tests()


class SummarizeResultsTests(unittest.TestCase):
    EXPECTED_KEYS = {
        "total_tables",
        "total_rows_read",
        "total_rows_inserted",
        "total_rows_skipped_existing",
        "total_rows_errored",
        "tables_with_errors",
        "per_table",
    }

    def test_summary_shape(self):
        import postgres_backfill

        sample = [
            postgres_backfill.BackfillResult(
                table_name="a", rows_read=10, rows_inserted=10,
                rows_skipped_existing=0, rows_errored=0,
                duration_seconds=0.1, dry_run=True,
            ),
            postgres_backfill.BackfillResult(
                table_name="b", rows_read=5, rows_inserted=2,
                rows_skipped_existing=3, rows_errored=0,
                duration_seconds=0.05, dry_run=True,
            ),
        ]
        summary = postgres_backfill.summarize_results(sample)
        self.assertEqual(set(summary.keys()), self.EXPECTED_KEYS)
        self.assertEqual(summary["total_tables"], 2)
        self.assertEqual(summary["total_rows_read"], 15)
        self.assertEqual(summary["total_rows_inserted"], 12)
        self.assertEqual(summary["total_rows_skipped_existing"], 3)
        self.assertEqual(summary["total_rows_errored"], 0)
        self.assertEqual(len(summary["per_table"]), 2)


# ---------------------------------------------------------------------------
# CLI behaviour — invoke main() directly with crafted argv.
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def _run_cli(self, argv, *, stdin_isatty=True, stdin_text=""):
        """Invoke the CLI's main() with stdin/stdout captured.

        ``stdin_isatty`` controls the value returned by
        ``sys.stdin.isatty()`` so we can simulate both interactive and
        non-interactive shells. ``stdin_text`` becomes the readline
        content when the CLI prompts interactively.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_postgres_backfill_cli",
            str(_PROJECT_ROOT / "scripts" / "run_postgres_backfill.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        fake_stdin = io.StringIO(stdin_text)
        fake_stdin.isatty = lambda: stdin_isatty  # type: ignore[assignment]
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()

        old_stdin, old_stdout, old_stderr = (
            sys.stdin, sys.stdout, sys.stderr,
        )
        try:
            sys.stdin = fake_stdin
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture
            rc = module.main(argv)
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        return rc, stdout_capture.getvalue(), stderr_capture.getvalue()

    def test_status_disabled_exits_zero(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            rc, out, _ = self._run_cli(["--status"])
            self.assertEqual(rc, 0)
            self.assertIn("Postgres dual-write enabled: False", out)
            self.assertIn("Backfill cannot run", out)
            self.assertIn(
                "SQLite remains the source of truth", out,
            )

    def test_status_enabled_exits_zero(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                rc, out, _ = self._run_cli(["--status"])
                self.assertEqual(rc, 0)
                self.assertIn("Postgres dual-write enabled: True", out)
                self.assertIn("Can connect:", out)
                import postgres_storage

                postgres_storage.reset_engine_for_tests()

    def test_dry_run_disabled_exits_zero_with_message(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            rc, out, _ = self._run_cli(["--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("dual-write is disabled", out.lower())

    def test_execute_disabled_exits_one(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            rc, _, _ = self._run_cli(["--execute", "--yes"])
            self.assertEqual(rc, 1)

    def test_execute_non_tty_without_yes_exits_one(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                rc, _, err = self._run_cli(
                    ["--execute"], stdin_isatty=False,
                )
                self.assertEqual(rc, 1)
                self.assertIn("--yes", err)
                import postgres_storage

                postgres_storage.reset_engine_for_tests()

    def test_invalid_table_name_exits_two(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            rc, _, err = self._run_cli(
                ["--dry-run", "--table", "not_a_real_table"],
            )
            self.assertEqual(rc, 2)
            self.assertIn("--table", err)

    def test_help_exits_zero(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            rc, out, _ = self._run_cli(["--help"])
            self.assertEqual(rc, 0)
            self.assertIn("Exit codes", out)
            self.assertIn("run_postgres_backfill", out)

    def test_status_json_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            rc, out, _ = self._run_cli(["--status", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertIn("health", data)
            self.assertIn("sqlite_counts", data)
            self.assertIn("postgres_counts", data)
            self.assertEqual(data["health"]["dual_write_enabled"], False)


# ---------------------------------------------------------------------------
# Static checks on postgres_backfill.py — no network, no OpenAI, no
# top-level dependency on main/api_server/scheduler.
# ---------------------------------------------------------------------------


class ModuleLevelStaticChecks(unittest.TestCase):
    def setUp(self):
        self.module_path = _PROJECT_ROOT / "postgres_backfill.py"
        self.source = self.module_path.read_text(encoding="utf-8")

    def test_does_not_import_main_api_or_scheduler(self):
        forbidden = (
            r"^(?:from\s+main\b|import\s+main\b)",
            r"^(?:from\s+api_server\b|import\s+api_server\b)",
            r"^(?:from\s+scheduler\b|import\s+scheduler\b)",
            r"^(?:from\s+job_manager\b|import\s+job_manager\b)",
        )
        for pattern in forbidden:
            self.assertIsNone(
                re.search(pattern, self.source, re.MULTILINE),
                msg=f"postgres_backfill.py must not match {pattern!r}",
            )

    def test_does_not_import_openai_or_anthropic(self):
        for needle in ("openai", "anthropic"):
            self.assertNotIn(
                needle, self.source,
                msg=f"postgres_backfill.py must not reference {needle}",
            )

    def test_does_not_import_db_postgres_m1_module(self):
        """M12.0b backfill must not touch the M1 normalized-schema
        dual-write at ``db/postgres.py``. They coexist independently."""
        forbidden = re.compile(
            r"^(?:from\s+db\.postgres\b|from\s+db\s+import\s+postgres)",
            re.MULTILINE,
        )
        self.assertIsNone(
            forbidden.search(self.source),
            msg=(
                "postgres_backfill.py must not import db.postgres "
                "(M1 system is independent)"
            ),
        )

    def test_does_not_import_requests_or_urllib_for_external_io(self):
        """Network I/O is forbidden in backfill — only SQLAlchemy
        engine connections are allowed, and those are mediated through
        postgres_storage helpers."""
        for needle in ("requests", "urllib", "httpx"):
            # Allow them being mentioned in comments / strings? No —
            # the brief says "no network I/O imports". An import line
            # like ``import requests`` would trip this; a comment
            # mentioning the word "urllib" elsewhere will not, because
            # we only scan for the import-line shape.
            pattern = re.compile(
                rf"^(?:from\s+{needle}\b|import\s+{needle}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=(
                    f"postgres_backfill.py must not import {needle}"
                ),
            )


if __name__ == "__main__":
    unittest.main()
