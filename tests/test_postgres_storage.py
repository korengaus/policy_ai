"""Tests for the M12.0a Postgres dual-write foundation.

Run with: python tests/test_postgres_storage.py

These tests deliberately do NOT need a real Postgres server. Where an
integration check is required, a temporary SQLite file is used as the
SQLAlchemy backend (URL: ``sqlite:///<tmp>``). The mirror_write /
mirror_upsert helpers are dialect-aware: the Postgres ON CONFLICT path
is unreachable here, but the basic INSERT and the helper's
return-False-on-error contract are exercised end-to-end.
"""

from __future__ import annotations

import importlib
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
# Test scaffolding — env-var snapshot / restore so a misbehaving test
# cannot leak USE_POSTGRES_WRITE or DATABASE_URL into the next case.
# ---------------------------------------------------------------------------


class _EnvScope:
    """Context manager snapshot/restore for dual-write env vars."""

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
        # Reset module-level cached engine so the next test sees fresh state.
        import postgres_storage

        postgres_storage.reset_engine_for_tests()


def _set_env(**values):
    """Set env vars and reset the cached engine in one call."""
    import postgres_storage

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Feature-flag tests
# ---------------------------------------------------------------------------


class FeatureFlagTests(unittest.TestCase):
    def test_disabled_when_env_unset(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertFalse(postgres_storage.is_postgres_dual_write_enabled())

    def test_disabled_for_falsey_values(self):
        # The flag must require the exact string "true" (case-insensitive).
        # Any other value — including legacy truthy strings like "1" /
        # "yes" / "on" — must read as disabled. Reason: the brief's
        # explicit contract is "USE_POSTGRES_WRITE == 'true' (case-
        # insensitive)" and we want to surprise an operator who typed
        # "1" rather than enable dual-write silently.
        for value in ("false", "False", "FALSE", "no", "0", "",
                      "  ", "1", "yes", "on", "TRUE-ish"):
            with _EnvScope():
                _set_env(USE_POSTGRES_WRITE=value)
                import postgres_storage

                self.assertFalse(
                    postgres_storage.is_postgres_dual_write_enabled(),
                    msg=f"value={value!r} should not enable dual-write",
                )

    def test_enabled_for_case_insensitive_true(self):
        for value in ("true", "True", "TRUE", "  true  "):
            with _EnvScope():
                _set_env(USE_POSTGRES_WRITE=value)
                import postgres_storage

                self.assertTrue(
                    postgres_storage.is_postgres_dual_write_enabled(),
                    msg=f"value={value!r} should enable dual-write",
                )

    def test_database_url_helper_returns_none_when_unset_or_empty(self):
        for value in (None, "", "   "):
            with _EnvScope():
                _set_env(DATABASE_URL=value)
                import postgres_storage

                self.assertIsNone(postgres_storage.get_database_url())

    def test_database_url_helper_strips_whitespace(self):
        with _EnvScope():
            _set_env(DATABASE_URL="  postgresql://foo  ")
            import postgres_storage

            self.assertEqual(
                postgres_storage.get_database_url(), "postgresql://foo",
            )


# ---------------------------------------------------------------------------
# Engine lifecycle tests
# ---------------------------------------------------------------------------


class EngineLifecycleTests(unittest.TestCase):
    def test_get_engine_returns_none_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="false",
                     DATABASE_URL="postgresql://example/test")
            import postgres_storage

            self.assertIsNone(postgres_storage.get_engine())

    def test_get_engine_raises_when_url_empty(self):
        """M12.0d-2: missing DATABASE_URL when dual-write is enabled is
        a configuration error and surfaces as PostgresReadError instead
        of silently disabling dual-write (Stage 1 deviation #4 fix)."""
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true", DATABASE_URL="")
            import postgres_storage

            with self.assertRaises(postgres_storage.PostgresReadError):
                postgres_storage.get_engine()

    def test_get_engine_raises_on_invalid_url(self):
        """M12.0d-2: SQLAlchemy parse failures surface as
        PostgresReadError instead of being swallowed."""
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="not-a-real-url-dialect:///nowhere")
            import postgres_storage

            with self.assertRaises(postgres_storage.PostgresReadError):
                postgres_storage.get_engine()

    def test_reset_engine_for_tests_clears_cache(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "engine_cache.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine_first = postgres_storage.get_engine()
                self.assertIsNotNone(engine_first)
                # Same call should return the cached instance.
                self.assertIs(engine_first, postgres_storage.get_engine())
                postgres_storage.reset_engine_for_tests()
                # After reset, get_engine builds a fresh engine.
                engine_second = postgres_storage.get_engine()
                self.assertIsNotNone(engine_second)
                self.assertIsNot(engine_first, engine_second)
                # Dispose the engine before tempdir cleanup so Windows
                # can release the SQLite file handle.
                postgres_storage.reset_engine_for_tests()

    def test_no_global_engine_on_import(self):
        """Module import must not build an engine. Lazy init only."""
        with _EnvScope():
            # Set a valid URL so a non-lazy implementation WOULD build.
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "lazy.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                # Force re-import so we observe a fresh module state.
                sys.modules.pop("postgres_storage", None)
                import postgres_storage  # noqa: F401

                self.assertIsNone(postgres_storage._engine)


# ---------------------------------------------------------------------------
# mirror_write / mirror_upsert safety contracts (never raise)
# ---------------------------------------------------------------------------


class MirrorWriteSafetyTests(unittest.TestCase):
    def test_mirror_write_returns_false_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="false")
            import postgres_storage

            self.assertFalse(
                postgres_storage.mirror_write(
                    "analysis_results", {"query": "x"},
                )
            )

    def test_mirror_write_returns_false_for_unknown_table(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "unknown.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                postgres_storage.ensure_schema(postgres_storage.get_engine())
                self.assertFalse(
                    postgres_storage.mirror_write(
                        "no_such_table", {"id": 1},
                    )
                )

    def test_mirror_upsert_returns_false_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="false")
            import postgres_storage

            self.assertFalse(
                postgres_storage.mirror_upsert(
                    "review_tasks",
                    {"idempotency_key": "k"},
                    ["idempotency_key"],
                )
            )

    def test_mirror_upsert_returns_false_for_unknown_table(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "upsert_unknown.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                self.assertFalse(
                    postgres_storage.mirror_upsert(
                        "no_such_table", {"k": "v"}, ["k"],
                    )
                )

    def test_mirror_upsert_returns_false_with_empty_conflict_columns(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "empty_conflict.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                postgres_storage.ensure_schema(postgres_storage.get_engine())
                self.assertFalse(
                    postgres_storage.mirror_upsert(
                        "review_tasks",
                        {"task_id": "t1", "status": "open"},
                        [],
                    )
                )

    def test_mirror_write_returns_false_when_engine_invalid(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="postgresql+psycopg://"
                                  "u:p@127.0.0.1:1/none")
            import postgres_storage

            try:
                result = postgres_storage.mirror_write(
                    "analysis_results", {"query": "x"},
                )
            except Exception as exc:  # noqa: BLE001
                self.fail(f"mirror_write raised: {exc!r}")
            self.assertFalse(result)


# ---------------------------------------------------------------------------
# health_check shape & safety
# ---------------------------------------------------------------------------


class HealthCheckTests(unittest.TestCase):
    EXPECTED_KEYS = {
        "dual_write_enabled",
        "database_url_present",
        "engine_available",
        "can_connect",
        "error",
        "tables_defined",
    }

    def test_health_check_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            status = postgres_storage.health_check()
            self.assertEqual(set(status.keys()), self.EXPECTED_KEYS)
            self.assertFalse(status["dual_write_enabled"])
            self.assertFalse(status["database_url_present"])
            self.assertFalse(status["engine_available"])
            self.assertFalse(status["can_connect"])
            self.assertIsNone(status["error"])
            self.assertGreater(len(status["tables_defined"]), 0)

    def test_health_check_does_not_raise_on_invalid_url(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="postgresql+psycopg://"
                                  "u:p@127.0.0.1:1/none")
            import postgres_storage

            try:
                status = postgres_storage.health_check()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"health_check raised: {exc!r}")
            self.assertEqual(set(status.keys()), self.EXPECTED_KEYS)
            self.assertTrue(status["dual_write_enabled"])
            self.assertTrue(status["database_url_present"])
            self.assertFalse(status["can_connect"])


# ---------------------------------------------------------------------------
# Integration tests using sqlite-as-postgres-substitute.
# These hit a real SQLAlchemy engine but the dialect is SQLite, not PG,
# so the Postgres ON CONFLICT branch is not covered here — the SQLite-
# substitute branch of mirror_upsert is.
# ---------------------------------------------------------------------------


class SqliteSubstituteIntegrationTests(unittest.TestCase):
    def test_mirror_write_inserts_row(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "mirror_insert.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                self.assertIsNotNone(engine)
                postgres_storage.ensure_schema(engine)
                ok = postgres_storage.mirror_write(
                    "analysis_results",
                    {
                        "id": 1,
                        "query": "test",
                        "title": "test title",
                        "original_url": "https://example.com/x",
                        "created_at": "2026-05-23T00:00:00+00:00",
                    },
                )
                self.assertTrue(ok)
                with engine.connect() as conn:
                    row = conn.execute(
                        sa.text(
                            "SELECT query, title, original_url "
                            "FROM analysis_results WHERE id = 1"
                        )
                    ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "test")
                self.assertEqual(row[1], "test title")
                self.assertEqual(row[2], "https://example.com/x")
                postgres_storage.reset_engine_for_tests()

    def test_mirror_upsert_replaces_on_conflict(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "mirror_upsert.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                base_row = {
                    "task_id": "t1",
                    "status": "open",
                    "snapshot_json": "{}",
                    "created_at": "2026-05-23T00:00:00",
                    "updated_at": "2026-05-23T00:00:00",
                    "idempotency_key": "idem-1",
                    "claim_text": "first",
                }
                self.assertTrue(
                    postgres_storage.mirror_upsert(
                        "review_tasks", base_row, ["idempotency_key"],
                    )
                )
                updated_row = dict(base_row, claim_text="second")
                self.assertTrue(
                    postgres_storage.mirror_upsert(
                        "review_tasks", updated_row, ["idempotency_key"],
                    )
                )
                with engine.connect() as conn:
                    row = conn.execute(
                        sa.text(
                            "SELECT claim_text FROM review_tasks "
                            "WHERE idempotency_key = :k"
                        ),
                        {"k": "idem-1"},
                    ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "second")
                postgres_storage.reset_engine_for_tests()

    def test_mirror_upsert_returning_inserts_then_updates_same_id(self):
        """M12.0d Stage 3c-3: mirror_upsert_returning returns the row id on
        insert and the SAME id on a conflicting re-upsert (which updates),
        exercised against the SQLite-as-Postgres substitute (the id is
        recovered via the follow-up SELECT-by-conflict path)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "upsert_returning.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                base_row = {
                    "analysis_id": "ana-1",
                    "source": "s",
                    "input_hash": "h-1",
                    "producer1_label": "first",
                    "comparison_timestamp": "2026-05-27T00:00:00",
                    "truth_claim": 0,
                    "operator_review_required": 1,
                    "created_at": "2026-05-27T00:00:00+00:00",
                }
                first_id = postgres_storage.mirror_upsert_returning(
                    "verdict_producer_comparisons", base_row, ["input_hash"],
                )
                self.assertIsInstance(first_id, int)
                self.assertGreater(first_id, 0)

                updated_row = dict(base_row, producer1_label="second")
                second_id = postgres_storage.mirror_upsert_returning(
                    "verdict_producer_comparisons", updated_row,
                    ["input_hash"],
                )
                # Same conflict key → same row id, value updated.
                self.assertEqual(second_id, first_id)
                with engine.connect() as conn:
                    row = conn.execute(
                        sa.text(
                            "SELECT producer1_label FROM "
                            "verdict_producer_comparisons "
                            "WHERE input_hash = :h"
                        ),
                        {"h": "h-1"},
                    ).fetchone()
                self.assertEqual(row[0], "second")
                postgres_storage.reset_engine_for_tests()

    def test_mirror_write_with_invalid_url_returns_false(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="postgresql+psycopg://"
                                  "u:p@127.0.0.1:1/none")
            import postgres_storage

            try:
                ok = postgres_storage.mirror_write(
                    "analysis_results", {"query": "x"},
                )
            except Exception as exc:  # noqa: BLE001
                self.fail(f"mirror_write raised: {exc!r}")
            self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Schema parity: every SQLite table from database.py must appear in
# postgres_storage._metadata with the same column NAMES.
# ---------------------------------------------------------------------------


def _sqlite_table_columns(connection, table_name: str) -> set:
    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()
    return {row[1] for row in rows}  # row[1] is the column name


class SchemaParityTests(unittest.TestCase):
    EXPECTED_TABLES = {
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

    def test_metadata_contains_every_sqlite_table(self):
        import postgres_storage

        defined = set(postgres_storage._metadata.tables.keys())
        missing = self.EXPECTED_TABLES - defined
        self.assertSetEqual(
            missing, set(),
            msg=f"Postgres mirror missing tables: {missing}",
        )

    def test_postgres_columns_match_sqlite_columns(self):
        """For every mirrored table, the Postgres column NAMES must
        match the SQLite column NAMES exactly. Types differ (SQLite is
        permissive; Postgres uses Text/Integer/Float) — we only enforce
        the column-name contract because that's what the dual-write
        path relies on for ``_filter_row``."""
        import database
        import postgres_storage

        # Build a fresh SQLite DB in a temp location so we don't touch
        # the real policy_ai.db.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            tmp_db = Path(tmp_dir) / "schema_parity.db"
            with patch("database.DB_PATH", tmp_db):
                database.init_db()
                # Some tables need their own initializer.
                database.init_source_fetch_artifacts_table()
                database.init_artifact_text_extractions_table()
                database.init_artifact_evidence_candidates_table()
                database.init_verdict_producer_comparisons_table()
                database.init_verdict_label_attributions_table()

                connection = sqlite3.connect(tmp_db)
                try:
                    for table_name in self.EXPECTED_TABLES:
                        sqlite_cols = _sqlite_table_columns(
                            connection, table_name,
                        )
                        pg_table = postgres_storage._metadata.tables[
                            table_name
                        ]
                        pg_cols = {c.name for c in pg_table.columns}
                        self.assertSetEqual(
                            sqlite_cols, pg_cols,
                            msg=(
                                f"Column mismatch for {table_name}: "
                                f"sqlite={sqlite_cols}, pg={pg_cols}"
                            ),
                        )
                finally:
                    connection.close()


# ---------------------------------------------------------------------------
# Database integration: SQLite write must succeed even when the mirror
# write raises (the outer try/except in database.py must swallow).
# ---------------------------------------------------------------------------


class DatabaseDualWriteIsolationTests(unittest.TestCase):
    """M12.0d Stage 3c-3: exercises the PG-only write contract for the
    integer-PK tables (analysis_results, source_fetch_artifacts) under the
    SQLite-as-Postgres substitute — the PG-assigned id is returned on
    success, and a failed PG write surfaces an explicit failure (no phantom
    id) rather than silently falling back to SQLite."""

    def test_save_analysis_result_pg_only_returns_pg_assigned_id(self):
        """M12.0d Stage 3c-3: with dual-write enabled, save_analysis_result
        writes ONLY to Postgres and returns the PG-assigned (SERIAL) id.
        Exercised against the SQLite-as-Postgres substitute. Replaces the
        pre-3c-3 ``..._isolated_from_mirror_write_failure`` test, whose
        SQLite-survives-mirror-failure contract no longer applies now that
        the dual-write-enabled path never touches SQLite."""
        from text_utils import sanitize_data

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_pg_local.db"
                pg_db = Path(tmp_dir) / "iso_pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    sample = {
                        "title": "pg-only write",
                        "original_url": "https://example.com/pg-only-1",
                        "topic": "정책",
                        "claim_text": "주장",
                        "verdict_label": "draft_likely_true",
                        "verdict_confidence": 70,
                        "verification_card": {
                            "claim_text": "주장",
                            "verdict_label": "draft_likely_true",
                            "verdict_confidence": 70,
                        },
                    }
                    status = database.save_analysis_result(
                        sanitize_data(sample), query="pg-only-test",
                    )
                    self.assertTrue(status["saved"])
                    self.assertFalse(status["duplicate"])
                    self.assertIsInstance(status["id"], int)

                    # The id is PG-assigned; the row must be readable from
                    # the PG substitute (get_recent_results is PG-primary).
                    rows = database.get_recent_results(limit=5)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        rows[0]["original_url"], sample["original_url"],
                    )
                    self.assertEqual(rows[0]["id"], status["id"])
                    postgres_storage.reset_engine_for_tests()

    def test_save_analysis_result_pg_write_failure_reports_not_saved(self):
        """M12.0d Stage 3c-3 (Q1 decision): when the PG write returns no
        id and the SQLite fallback is gone, save_analysis_result reports an
        explicit failure (saved=False, id=None, error='pg_write_failed')
        rather than a phantom save with a fabricated id. Guards against the
        3c-1 data-loss class."""
        from text_utils import sanitize_data

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_fail_local.db"
                pg_db = Path(tmp_dir) / "iso_fail_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    sample = {
                        "title": "pg write fails",
                        "original_url": "https://example.com/pg-fail-1",
                        "topic": "정책",
                        "claim_text": "주장",
                        "verdict_label": "draft_likely_true",
                        "verdict_confidence": 70,
                        "verification_card": {
                            "claim_text": "주장",
                            "verdict_label": "draft_likely_true",
                            "verdict_confidence": 70,
                        },
                    }
                    # mirror_write_returning → None simulates a PG insert
                    # that assigned no id (driver/SQL failure swallowed by
                    # _mirror_write_returning_safe).
                    with patch.object(
                        postgres_storage, "mirror_write_returning",
                        lambda *a, **kw: None,
                    ):
                        status = database.save_analysis_result(
                            sanitize_data(sample), query="pg-fail-test",
                        )
                    self.assertFalse(status["saved"])
                    self.assertIsNone(status["id"])
                    self.assertEqual(status.get("error"), "pg_write_failed")

                    # Nothing persisted to PG, and the SQLite write path is
                    # NOT taken under dual-write — so PG stays empty.
                    self.assertEqual(database.get_recent_results(limit=5), [])
                    postgres_storage.reset_engine_for_tests()

    def test_save_fetch_artifact_pg_only_returns_pg_assigned_id(self):
        """M12.0d Stage 3c-3: with dual-write enabled, save_fetch_artifact
        writes ONLY to Postgres and returns the PG-assigned id (the value
        the operator CLI prints as saved_row_id). Replaces the pre-3c-3
        ``..._isolated_from_mirror_write_failure`` test."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_fetch_local.db"
                pg_db = Path(tmp_dir) / "iso_fetch_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_source_fetch_artifacts_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    fetch_result = {
                        "source_id": "kr_law_open_data_candidate",
                        "url": "https://www.law.go.kr/x",
                        "fetch_timestamp": "2026-05-23T00:00:00",
                        "success": True,
                    }
                    row_id = database.save_fetch_artifact(fetch_result)
                    self.assertIsInstance(row_id, int)
                    self.assertGreater(row_id, 0)

                    artifacts = database.get_fetch_artifacts(
                        source_id="kr_law_open_data_candidate",
                    )
                    self.assertEqual(len(artifacts), 1)
                    self.assertEqual(artifacts[0]["id"], row_id)
                    postgres_storage.reset_engine_for_tests()

    def test_save_fetch_artifact_pg_write_failure_returns_sentinel(self):
        """M12.0d Stage 3c-3 (Q1 decision, int-return variant): when the
        PG write returns no id and the SQLite fallback is gone,
        save_fetch_artifact returns the sentinel -1 (never a real row id)
        rather than a phantom positive id. Guards the 3c-1 data-loss class
        while preserving the ``-> int`` contract."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_fetch_fail_local.db"
                pg_db = Path(tmp_dir) / "iso_fetch_fail_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_source_fetch_artifacts_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    fetch_result = {
                        "source_id": "kr_law_open_data_candidate",
                        "url": "https://www.law.go.kr/x",
                        "fetch_timestamp": "2026-05-23T00:00:00",
                        "success": True,
                    }
                    with patch.object(
                        postgres_storage, "mirror_write_returning",
                        lambda *a, **kw: None,
                    ):
                        row_id = database.save_fetch_artifact(fetch_result)
                    self.assertEqual(row_id, -1)

                    # Nothing persisted to PG; SQLite write path NOT taken.
                    self.assertEqual(database.get_fetch_artifacts(), [])
                    postgres_storage.reset_engine_for_tests()

    def test_save_producer_comparison_pg_only_returns_pg_assigned_id(self):
        """M12.0d Stage 3c-3: with dual-write enabled and no db_path,
        save_producer_comparison upserts ONLY to Postgres and returns the
        PG-assigned id."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_pc_local.db"
                pg_db = Path(tmp_dir) / "iso_pc_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    row_id = database.save_producer_comparison({
                        "analysis_id": "ana-pc",
                        "source": "s",
                        "input_hash": "h-pc",
                        "comparison_timestamp": "2026-05-27T00:00:00",
                    })
                    self.assertIsInstance(row_id, int)
                    self.assertGreater(row_id, 0)

                    rows = database.get_producer_comparisons(
                        analysis_id="ana-pc",
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["id"], row_id)
                    postgres_storage.reset_engine_for_tests()

    def test_save_producer_comparison_pg_write_failure_returns_sentinel(self):
        """M12.0d Stage 3c-3 (Q1 decision, int-return variant): on PG write
        failure save_producer_comparison returns the sentinel -1 rather than
        a phantom positive id, with the SQLite write path NOT taken."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_pc_fail_local.db"
                pg_db = Path(tmp_dir) / "iso_pc_fail_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    with patch.object(
                        postgres_storage, "mirror_upsert_returning",
                        lambda *a, **kw: None,
                    ):
                        row_id = database.save_producer_comparison({
                            "analysis_id": "ana-pc-fail",
                            "source": "s",
                            "input_hash": "h-pc-fail",
                            "comparison_timestamp": "2026-05-27T00:00:00",
                        })
                    self.assertEqual(row_id, -1)
                    self.assertEqual(database.get_producer_comparisons(), [])
                    postgres_storage.reset_engine_for_tests()

    def test_save_verdict_label_attribution_pg_only_returns_pg_assigned_id(self):
        """M12.0d Stage 3c-3: with dual-write enabled and no db_path,
        save_verdict_label_attribution upserts ONLY to Postgres and returns
        the PG-assigned id."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_vla_local.db"
                pg_db = Path(tmp_dir) / "iso_vla_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    row_id = database.save_verdict_label_attribution({
                        "analysis_id": "ana-vla",
                        "diagnostic_timestamp": "2026-05-27T00:00:00",
                    })
                    self.assertIsInstance(row_id, int)
                    self.assertGreater(row_id, 0)

                    rows = database.get_verdict_label_attributions(
                        analysis_id="ana-vla",
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["id"], row_id)
                    postgres_storage.reset_engine_for_tests()

    def test_save_verdict_label_attribution_pg_write_failure_returns_sentinel(self):
        """M12.0d Stage 3c-3 (Q1 decision, int-return variant): on PG write
        failure save_verdict_label_attribution returns the sentinel -1
        rather than a phantom positive id, with the SQLite write path NOT
        taken."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "iso_vla_fail_local.db"
                pg_db = Path(tmp_dir) / "iso_vla_fail_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    with patch.object(
                        postgres_storage, "mirror_upsert_returning",
                        lambda *a, **kw: None,
                    ):
                        row_id = database.save_verdict_label_attribution({
                            "analysis_id": "ana-vla-fail",
                            "diagnostic_timestamp": "2026-05-27T00:00:00",
                        })
                    self.assertEqual(row_id, -1)
                    self.assertEqual(
                        database.get_verdict_label_attributions(), [],
                    )
                    postgres_storage.reset_engine_for_tests()

    # M12.0d Stage 3c-2/3c-3: review_decisions (3c-2), analysis_results,
    # source_fetch_artifacts, verdict_producer_comparisons and
    # verdict_label_attributions (3c-3) writes are all PG-only when
    # dual-write is enabled; the SQLite-as-fallback isolation contract no
    # longer applies. The pre-3c-3 ``..._isolated_from_mirror_write_failure``
    # tests were replaced by the PG-only happy-path + pg_write_failed /
    # sentinel failure-path tests above.


# ---------------------------------------------------------------------------
# Static-source checks for module-level imports and contracts the brief
# explicitly calls out.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# M12.0c-minimal: Postgres read helpers + database.py fallback semantics.
#
# These tests exercise the new ``read_analysis_result_by_id`` /
# ``read_recent_analysis_results`` helpers using the same SQLite-as-
# Postgres substitute the dual-write tests already use, so no real
# Postgres server is required. The integration class verifies that
# ``database.get_result_by_id`` / ``get_recent_results`` prefer the
# Postgres row when enabled and fall back to SQLite when not.
# ---------------------------------------------------------------------------


class ReadAnalysisResultByIdTests(unittest.TestCase):
    """Direct tests of ``postgres_storage.read_analysis_result_by_id``."""

    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(postgres_storage.read_analysis_result_by_id(1))

    def test_returns_dict_when_row_present(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_by_id_present.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                self.assertIsNotNone(engine)
                postgres_storage.ensure_schema(engine)
                postgres_storage.mirror_write(
                    "analysis_results",
                    {
                        "id": 42,
                        "query": "read by id present",
                        "title": "from postgres",
                        "original_url": "https://example.com/pg-42",
                        "created_at": "2026-05-27T00:00:00+00:00",
                    },
                )

                row = postgres_storage.read_analysis_result_by_id(42)
                self.assertIsNotNone(row)
                self.assertIsInstance(row, dict)
                self.assertEqual(row["id"], 42)
                self.assertEqual(row["title"], "from postgres")
                self.assertEqual(
                    row["original_url"], "https://example.com/pg-42",
                )
                postgres_storage.reset_engine_for_tests()

    def test_returns_none_when_row_missing(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_by_id_missing.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                self.assertIsNone(
                    postgres_storage.read_analysis_result_by_id(9999),
                )
                postgres_storage.reset_engine_for_tests()

    def test_raises_postgres_read_error_on_engine_failure(self):
        """M12.0d-1: real engine / SQL errors raise PostgresReadError
        (Stage 1 contract). Pre-M12.0d-1 the helper swallowed and
        returned None; that masked PG outages."""
        from sqlalchemy.exc import OperationalError

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "engine_err.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                def _boom(*a, **kw):
                    raise OperationalError(
                        "simulated", params=None, orig=Exception("boom"),
                    )

                with patch.object(engine, "connect", side_effect=_boom):
                    with self.assertRaises(
                        postgres_storage.PostgresReadError,
                    ):
                        postgres_storage.read_analysis_result_by_id(1)
                postgres_storage.reset_engine_for_tests()


class ReadRecentAnalysisResultsTests(unittest.TestCase):
    """Direct tests of ``postgres_storage.read_recent_analysis_results``.

    The empty-list semantics is the load-bearing invariant: when
    Postgres knows there are 0 rows, the helper returns ``[]`` (not
    None) so the database.py caller treats it as authoritative and
    does NOT fall back to SQLite. The disabled / error paths return
    None so the caller does fall back."""

    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_recent_analysis_results(limit=5),
            )

    def test_returns_empty_list_when_no_rows(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_recent_empty.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                result = postgres_storage.read_recent_analysis_results(
                    limit=5,
                )
                # The crucial distinction: [] (PG authoritative zero),
                # not None (PG unavailable). database.py uses this to
                # decide whether to fall back to SQLite.
                self.assertEqual(result, [])
                self.assertIsNotNone(result)
                postgres_storage.reset_engine_for_tests()

    def test_returns_rows_newest_first(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_recent_order.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                for row_id, title in [
                    (1, "oldest"), (2, "middle"), (3, "newest"),
                ]:
                    postgres_storage.mirror_write(
                        "analysis_results",
                        {
                            "id": row_id,
                            "query": "order test",
                            "title": title,
                            "original_url": f"https://example.com/o-{row_id}",
                            "created_at": (
                                f"2026-05-27T00:00:0{row_id}+00:00"
                            ),
                        },
                    )

                rows = postgres_storage.read_recent_analysis_results(
                    limit=10,
                )
                self.assertEqual(len(rows), 3)
                self.assertEqual(rows[0]["id"], 3)  # newest first
                self.assertEqual(rows[0]["title"], "newest")
                self.assertEqual(rows[2]["id"], 1)  # oldest last
                postgres_storage.reset_engine_for_tests()

    def test_respects_limit_clamp(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_recent_limit.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                for row_id in range(1, 6):  # 5 rows
                    postgres_storage.mirror_write(
                        "analysis_results",
                        {
                            "id": row_id,
                            "query": "limit test",
                            "title": f"row {row_id}",
                            "original_url": f"https://example.com/l-{row_id}",
                            "created_at": "2026-05-27T00:00:00+00:00",
                        },
                    )

                # limit=2 → exactly 2 rows (LIMIT honored).
                rows_two = postgres_storage.read_recent_analysis_results(
                    limit=2,
                )
                self.assertEqual(len(rows_two), 2)
                # limit=-5 → clamped to 1 by max(1, ...). A literal 0
                # would NOT trigger the min clamp because the clamp
                # expression is ``max(1, min(int(limit or 20), 100))``
                # and ``0 or 20 → 20`` short-circuits past the floor.
                # Negative inputs are the only way to hit the floor;
                # this also matches the SQLite helper's behaviour, so
                # the contract stays identical between paths.
                rows_negative = postgres_storage.read_recent_analysis_results(
                    limit=-5,
                )
                self.assertEqual(len(rows_negative), 1)
                # limit=999 → clamped to 100 (max clamp); but only 5
                # rows exist, so the visible count caps at 5. The clamp
                # behaviour is asserted indirectly by the absence of
                # any exception and the matching count.
                rows_large = postgres_storage.read_recent_analysis_results(
                    limit=999,
                )
                self.assertEqual(len(rows_large), 5)
                postgres_storage.reset_engine_for_tests()


class DatabaseReadFallbackIntegrationTests(unittest.TestCase):
    """Integration tests for the database.py side of M12.0c-minimal.

    Two SQLite files are used per case: one as the local "SQLite
    source of truth" (``database.DB_PATH``) and one as the
    "Postgres" substitute behind ``DATABASE_URL``. The matrix
    confirms that the read helpers prefer Postgres when enabled and
    fall back to SQLite when Postgres is disabled or returns None —
    and that an authoritative empty list from Postgres is NOT
    overridden by SQLite rows."""

    def test_get_result_by_id_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()  # SQLite schema only; no rows.
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    postgres_storage.mirror_write(
                        "analysis_results",
                        {
                            "id": 7,
                            "query": "prefer pg",
                            "title": "from postgres",
                            "original_url": "https://example.com/pg-7",
                            "created_at": "2026-05-27T00:00:00+00:00",
                        },
                    )

                    row = database.get_result_by_id(7)
                    self.assertIsNotNone(row)
                    self.assertEqual(row["title"], "from postgres")
                    self.assertEqual(
                        row["original_url"], "https://example.com/pg-7",
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_get_result_by_id_falls_back_to_sqlite_when_disabled(self):
        from text_utils import sanitize_data

        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_only.db"
                with patch("database.DB_PATH", sqlite_db):
                    import database

                    database.init_db()
                    sample = {
                        "title": "from sqlite",
                        "original_url": "https://example.com/sqlite-1",
                        "topic": "정책",
                        "claim_text": "주장",
                        "verdict_label": "draft_likely_true",
                        "verdict_confidence": 70,
                        "verification_card": {
                            "claim_text": "주장",
                            "verdict_label": "draft_likely_true",
                            "verdict_confidence": 70,
                        },
                    }
                    status = database.save_analysis_result(
                        sanitize_data(sample), query="fallback-disabled",
                    )
                    self.assertTrue(status["saved"])

                    row = database.get_result_by_id(status["id"])
                    self.assertIsNotNone(row)
                    self.assertEqual(row["title"], "from sqlite")
                    self.assertEqual(
                        row["original_url"], "https://example.com/sqlite-1",
                    )

    def test_get_result_by_id_returns_none_when_pg_empty(self):
        """M12.0d-1: PG is enabled and reachable but has no matching row.
        Stage 1 contract: function returns None (PG is authoritative for
        'not found'); the SQLite fallback is unreachable when dual-write
        is enabled, even if SQLite has the row."""
        from text_utils import sanitize_data

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    # Set up PG substitute schema but insert nothing —
                    # so read_analysis_result_by_id will return None.
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed a STALE row directly into
                    # SQLite via raw SQL. Under dual-write,
                    # save_analysis_result no longer writes SQLite, so we
                    # bypass it to reproduce the "stale SQLite, empty PG"
                    # scenario this test pins (PG-empty must win).
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO analysis_results "
                            "(id, query, title, original_url, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                42, "fallback-miss", "from sqlite only",
                                "https://example.com/sqlite-only",
                                "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    # Stage 1: PG empty + dual-write enabled → None, even
                    # though SQLite holds id=42.
                    row = database.get_result_by_id(42)
                    self.assertIsNone(row)
                    postgres_storage.reset_engine_for_tests()

    def test_get_recent_results_prefers_postgres_empty_over_sqlite_rows(self):
        """Load-bearing invariant: when Postgres returns ``[]`` (PG
        authoritative zero), the caller MUST trust it and NOT fall
        back to SQLite, even if SQLite holds rows. Without this,
        operators migrating to Postgres would see stale SQLite data
        leaking through."""
        from text_utils import sanitize_data

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed the stale SQLite row via raw
                    # SQL (save_analysis_result no longer writes SQLite
                    # under dual-write). PG stays empty; the stale row must
                    # NOT leak through.
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO analysis_results "
                            "(id, query, title, original_url, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                7, "empty-pg", "stale sqlite row",
                                "https://example.com/stale",
                                "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    rows = database.get_recent_results(limit=5)
                    # PG has 0 rows → [] returned authoritatively.
                    # SQLite has 1 stale row but must be IGNORED.
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# M12.0c-2: reviewer dashboard read helpers + database.py fallback.
#
# Mirrors the M12.0c-minimal test structure for the five new helpers:
#
#   * read_review_task_by_task_id
#   * read_review_task_by_idempotency_key
#   * read_review_tasks (status filter + pagination, [] = PG truth)
#   * read_review_decision_by_id
#   * read_review_decisions_for_task ([] = PG truth)
#
# Each helper gets 2 unit tests, paired with 2 database.py integration
# tests, for 4 per function × 5 functions = 20 new tests in total.
# ---------------------------------------------------------------------------


def _seed_review_task_in_pg(*, task_id, idempotency_key, status="open",
                            claim_text="claim", created_at="2026-05-27T00:00:00",
                            updated_at="2026-05-27T00:00:00",
                            human_review_required=1, item_index=0,
                            snapshot_json="{}"):
    """Helper: write a review_tasks row directly through the PG mirror
    so the read helpers have something to find. Assumes the engine is
    already built and the schema is in place."""
    import postgres_storage

    postgres_storage.mirror_upsert(
        "review_tasks",
        {
            "task_id": task_id,
            "status": status,
            "claim_text": claim_text,
            "snapshot_json": snapshot_json,
            "created_at": created_at,
            "updated_at": updated_at,
            "idempotency_key": idempotency_key,
            "human_review_required": human_review_required,
            "item_index": item_index,
        },
        ["idempotency_key"],
    )


def _seed_review_decision_in_pg(*, decision_id, task_id,
                                 decision="approve",
                                 created_at="2026-05-27T00:00:00",
                                 metadata_json="{}"):
    """Helper: write a review_decisions row through the PG mirror.
    review_decisions is append-only, so this uses mirror_write (insert)."""
    import postgres_storage

    postgres_storage.mirror_write(
        "review_decisions",
        {
            "decision_id": decision_id,
            "task_id": task_id,
            "decision": decision,
            "created_at": created_at,
            "metadata_json": metadata_json,
        },
    )


class ReadReviewTaskByTaskIdTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_review_task_by_task_id("t1"),
            )

    def test_returns_dict_when_row_present(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_task_by_id.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                self.assertIsNotNone(engine)
                postgres_storage.ensure_schema(engine)
                _seed_review_task_in_pg(
                    task_id="task-1", idempotency_key="idem-task-1",
                    claim_text="from pg",
                )

                row = postgres_storage.read_review_task_by_task_id("task-1")
                self.assertIsNotNone(row)
                self.assertIsInstance(row, dict)
                self.assertEqual(row["task_id"], "task-1")
                self.assertEqual(row["claim_text"], "from pg")
                # RAW dict contract: snapshot_json present (TEXT), not
                # the database.py-normalized ``snapshot`` key.
                self.assertIn("snapshot_json", row)
                self.assertNotIn("snapshot", row)
                postgres_storage.reset_engine_for_tests()


class ReadReviewTaskByIdempotencyKeyTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_review_task_by_idempotency_key(
                    "idem-x",
                ),
            )

    def test_returns_dict_when_row_present(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_task_by_idem.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_review_task_in_pg(
                    task_id="task-idem", idempotency_key="my-idem-key",
                    claim_text="found by idem",
                )

                row = postgres_storage.read_review_task_by_idempotency_key(
                    "my-idem-key",
                )
                self.assertIsNotNone(row)
                self.assertEqual(row["task_id"], "task-idem")
                self.assertEqual(row["idempotency_key"], "my-idem-key")
                self.assertEqual(row["claim_text"], "found by idem")
                postgres_storage.reset_engine_for_tests()


class ReadReviewTasksTests(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(postgres_storage.read_review_tasks())

    def test_status_filter_pagination_and_order(self):
        """Single test covers three contracts at once: status filter,
        newest-first ordering (created_at DESC, task_id DESC), and the
        clamped limit/offset window. Also asserts that an empty result
        is the empty list ``[]`` (PG authoritative zero), not None."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_tasks_filter.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                # Three open + one closed task. created_at increments
                # so order should be: t-open-3, t-open-2, t-open-1
                # for status="open".
                _seed_review_task_in_pg(
                    task_id="t-open-1", idempotency_key="i1",
                    status="open", created_at="2026-05-27T00:00:01",
                )
                _seed_review_task_in_pg(
                    task_id="t-open-2", idempotency_key="i2",
                    status="open", created_at="2026-05-27T00:00:02",
                )
                _seed_review_task_in_pg(
                    task_id="t-open-3", idempotency_key="i3",
                    status="open", created_at="2026-05-27T00:00:03",
                )
                _seed_review_task_in_pg(
                    task_id="t-closed-1", idempotency_key="i4",
                    status="closed", created_at="2026-05-27T00:00:04",
                )

                # status filter narrows to 3 open tasks, newest first.
                open_rows = postgres_storage.read_review_tasks(
                    status="open", limit=10,
                )
                self.assertEqual(len(open_rows), 3)
                self.assertEqual(open_rows[0]["task_id"], "t-open-3")
                self.assertEqual(open_rows[2]["task_id"], "t-open-1")

                # limit + offset pagination on the same filter.
                page = postgres_storage.read_review_tasks(
                    status="open", limit=1, offset=1,
                )
                self.assertEqual(len(page), 1)
                self.assertEqual(page[0]["task_id"], "t-open-2")

                # No filter → 4 rows total, newest first across statuses.
                all_rows = postgres_storage.read_review_tasks(limit=10)
                self.assertEqual(len(all_rows), 4)
                self.assertEqual(all_rows[0]["task_id"], "t-closed-1")

                # status with no matches → authoritative [] (NOT None).
                empty = postgres_storage.read_review_tasks(
                    status="nonexistent",
                )
                self.assertEqual(empty, [])
                self.assertIsNotNone(empty)
                postgres_storage.reset_engine_for_tests()


class ReadReviewDecisionByIdTests(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_review_decision_by_id("d1"),
            )

    def test_returns_dict_when_present(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_decision_by_id.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_review_decision_in_pg(
                    decision_id="dec-1", task_id="task-x",
                    decision="approve", metadata_json='{"k":"v"}',
                )

                row = postgres_storage.read_review_decision_by_id("dec-1")
                self.assertIsNotNone(row)
                self.assertEqual(row["decision_id"], "dec-1")
                self.assertEqual(row["task_id"], "task-x")
                self.assertEqual(row["decision"], "approve")
                # RAW dict contract: metadata_json present (TEXT), not
                # the database.py-normalized ``metadata`` key.
                self.assertIn("metadata_json", row)
                self.assertNotIn("metadata", row)
                postgres_storage.reset_engine_for_tests()


class ReadReviewDecisionsForTaskTests(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_review_decisions_for_task("task-1"),
            )

    def test_returns_rows_oldest_first(self):
        """Append-only history reads in occurrence order
        (created_at ASC, decision_id ASC). Also asserts the empty case
        returns ``[]`` (PG authoritative zero)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "read_decisions_for_task.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                # Three decisions on one task, plus one on a different
                # task that must NOT appear in the result.
                _seed_review_decision_in_pg(
                    decision_id="d-a", task_id="task-hist",
                    created_at="2026-05-27T00:00:01",
                )
                _seed_review_decision_in_pg(
                    decision_id="d-b", task_id="task-hist",
                    created_at="2026-05-27T00:00:02",
                )
                _seed_review_decision_in_pg(
                    decision_id="d-c", task_id="task-hist",
                    created_at="2026-05-27T00:00:03",
                )
                _seed_review_decision_in_pg(
                    decision_id="other", task_id="task-OTHER",
                    created_at="2026-05-27T00:00:00",
                )

                rows = postgres_storage.read_review_decisions_for_task(
                    "task-hist",
                )
                self.assertEqual(len(rows), 3)
                # Oldest first (asc).
                self.assertEqual(rows[0]["decision_id"], "d-a")
                self.assertEqual(rows[2]["decision_id"], "d-c")
                # Foreign-task row must be excluded.
                for r in rows:
                    self.assertEqual(r["task_id"], "task-hist")

                # No matches → authoritative [], not None.
                empty = postgres_storage.read_review_decisions_for_task(
                    "task-DOES-NOT-EXIST",
                )
                self.assertEqual(empty, [])
                self.assertIsNotNone(empty)
                postgres_storage.reset_engine_for_tests()


class DatabaseReviewFallbackIntegrationTests(unittest.TestCase):
    """Integration tests for the database.py side of M12.0c-2.

    Each case sets up two SQLite files: one is the local SQLite source
    of truth (``database.DB_PATH``) and one is the Postgres substitute
    behind ``DATABASE_URL``. We assert that the database.py read
    functions prefer the Postgres row when enabled, fall back to
    SQLite when Postgres is disabled or returns None for a single-row
    lookup, and trust an authoritative empty list from Postgres over
    any stale SQLite rows for list-shaped lookups.
    """

    # --- get_review_task ---------------------------------------------

    def test_get_review_task_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()  # SQLite-only schema.
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_review_task_in_pg(
                        task_id="task-pg", idempotency_key="idem-pg",
                        claim_text="from postgres",
                    )

                    task = database.get_review_task("task-pg")
                    self.assertIsNotNone(task)
                    self.assertEqual(task["claim_text"], "from postgres")
                    # _row_to_review_task transformation applied.
                    self.assertIn("snapshot", task)
                    self.assertNotIn("snapshot_json", task)
                    self.assertIsInstance(
                        task["human_review_required"], bool,
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_get_review_task_returns_none_when_pg_empty(self):
        """M12.0d-1: PG enabled + empty → None. SQLite fallback is
        unreachable when dual-write is enabled."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # Seed SQLite only; suppress the PG mirror so PG
                    # stays empty and read_review_task_by_task_id
                    # returns None for the lookup. Pre-M12.0d-1 this
                    # leaked the SQLite row through; Stage 1 hides it.
                    with patch.object(
                        postgres_storage, "mirror_upsert",
                        lambda *a, **kw: False,
                    ):
                        database.create_review_task(
                            task_id="task-sqlite",
                            result_id="r1", job_id="j1", item_index=0,
                            status="open", query="q",
                            claim_text="sqlite-only", title="t", url="u",
                            final_decision="WATCH",
                            policy_confidence="60",
                            human_review_required=True,
                            snapshot={"k": "v"},
                            idempotency_key="idem-sqlite",
                            created_at="2026-05-27T00:00:00",
                            updated_at="2026-05-27T00:00:00",
                        )

                    task = database.get_review_task("task-sqlite")
                    self.assertIsNone(task)
                    postgres_storage.reset_engine_for_tests()

    # --- get_review_task_by_idempotency_key --------------------------

    def test_get_review_task_by_idempotency_key_prefers_postgres(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_review_task_in_pg(
                        task_id="t-pg-idem",
                        idempotency_key="idem-from-pg",
                        claim_text="pg idem hit",
                    )

                    task = database.get_review_task_by_idempotency_key(
                        "idem-from-pg",
                    )
                    self.assertIsNotNone(task)
                    self.assertEqual(task["task_id"], "t-pg-idem")
                    self.assertEqual(task["claim_text"], "pg idem hit")
                    postgres_storage.reset_engine_for_tests()

    def test_get_review_task_by_idempotency_key_returns_none_when_pg_empty(self):
        """M12.0d-1: PG enabled + empty → None."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    with patch.object(
                        postgres_storage, "mirror_upsert",
                        lambda *a, **kw: False,
                    ):
                        database.create_review_task(
                            task_id="t-only-sqlite",
                            result_id="r1", job_id="j1", item_index=0,
                            status="open", query="q",
                            claim_text="sqlite idem", title="t", url="u",
                            final_decision="WATCH",
                            policy_confidence="60",
                            human_review_required=True,
                            snapshot={"k": "v"},
                            idempotency_key="idem-sqlite-only",
                            created_at="2026-05-27T00:00:00",
                            updated_at="2026-05-27T00:00:00",
                        )

                    task = database.get_review_task_by_idempotency_key(
                        "idem-sqlite-only",
                    )
                    self.assertIsNone(task)
                    postgres_storage.reset_engine_for_tests()

    # --- list_review_tasks -------------------------------------------

    def test_list_review_tasks_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_review_task_in_pg(
                        task_id="t-pg-a", idempotency_key="i-a",
                        created_at="2026-05-27T00:00:01",
                    )
                    _seed_review_task_in_pg(
                        task_id="t-pg-b", idempotency_key="i-b",
                        created_at="2026-05-27T00:00:02",
                    )

                    rows = database.list_review_tasks(limit=10)
                    self.assertEqual(len(rows), 2)
                    # Newest first.
                    self.assertEqual(rows[0]["task_id"], "t-pg-b")
                    self.assertEqual(rows[1]["task_id"], "t-pg-a")
                    postgres_storage.reset_engine_for_tests()

    def test_list_review_tasks_pg_empty_list_authoritative(self):
        """Load-bearing invariant: PG ``[]`` overrides any SQLite rows."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # Seed a SQLite row that must NOT leak through.
                    with patch.object(
                        postgres_storage, "mirror_upsert",
                        lambda *a, **kw: False,
                    ):
                        database.create_review_task(
                            task_id="stale-sqlite",
                            result_id="r1", job_id="j1", item_index=0,
                            status="open", query="q",
                            claim_text="stale", title="t", url="u",
                            final_decision="WATCH",
                            policy_confidence="60",
                            human_review_required=True,
                            snapshot={"k": "v"},
                            idempotency_key="idem-stale",
                            created_at="2026-05-27T00:00:00",
                            updated_at="2026-05-27T00:00:00",
                        )

                    rows = database.list_review_tasks(limit=10)
                    # PG has 0 rows → [] authoritative; SQLite row hidden.
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()

    # --- get_review_decision -----------------------------------------

    def test_get_review_decision_prefers_postgres(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_review_decision_in_pg(
                        decision_id="d-pg", task_id="t-pg",
                        decision="approve", metadata_json='{"src":"pg"}',
                    )

                    rec = database.get_review_decision("d-pg")
                    self.assertIsNotNone(rec)
                    self.assertEqual(rec["decision_id"], "d-pg")
                    self.assertEqual(rec["decision"], "approve")
                    # _row_to_review_decision transformation applied.
                    self.assertIn("metadata", rec)
                    self.assertNotIn("metadata_json", rec)
                    self.assertEqual(rec["metadata"], {"src": "pg"})
                    postgres_storage.reset_engine_for_tests()

    def test_get_review_decision_returns_none_when_pg_empty(self):
        """M12.0d-1: PG enabled + empty → None."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # Parent task so the FK-implicit relationship is sane.
                    with patch.object(
                        postgres_storage, "mirror_upsert",
                        lambda *a, **kw: False,
                    ):
                        database.create_review_task(
                            task_id="t-decis", result_id="r1", job_id="j1",
                            item_index=0, status="open", query="q",
                            claim_text="c", title="t", url="u",
                            final_decision="WATCH",
                            policy_confidence="60",
                            human_review_required=True,
                            snapshot={"k": "v"},
                            idempotency_key="idem-decis",
                            created_at="2026-05-27T00:00:00",
                            updated_at="2026-05-27T00:00:00",
                        )
                    with patch.object(
                        postgres_storage, "mirror_write",
                        lambda *a, **kw: False,
                    ):
                        database.record_review_decision(
                            decision_id="d-sqlite", task_id="t-decis",
                            decision="approve",
                            created_at="2026-05-27T00:00:01",
                        )

                    rec = database.get_review_decision("d-sqlite")
                    self.assertIsNone(rec)
                    postgres_storage.reset_engine_for_tests()

    # --- list_review_decisions ---------------------------------------

    def test_list_review_decisions_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_review_decision_in_pg(
                        decision_id="d-old", task_id="task-list",
                        created_at="2026-05-27T00:00:01",
                    )
                    _seed_review_decision_in_pg(
                        decision_id="d-new", task_id="task-list",
                        created_at="2026-05-27T00:00:02",
                    )

                    rows = database.list_review_decisions("task-list")
                    self.assertEqual(len(rows), 2)
                    # Oldest first (asc).
                    self.assertEqual(rows[0]["decision_id"], "d-old")
                    self.assertEqual(rows[1]["decision_id"], "d-new")
                    # metadata key from _row_to_review_decision.
                    for r in rows:
                        self.assertIn("metadata", r)
                        self.assertNotIn("metadata_json", r)
                    postgres_storage.reset_engine_for_tests()

    def test_list_review_decisions_pg_empty_list_authoritative(self):
        """PG empty → [] returned, SQLite decisions hidden."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # Seed SQLite-only parent + decision.
                    with patch.object(
                        postgres_storage, "mirror_upsert",
                        lambda *a, **kw: False,
                    ):
                        database.create_review_task(
                            task_id="t-empty", result_id="r1", job_id="j1",
                            item_index=0, status="open", query="q",
                            claim_text="c", title="t", url="u",
                            final_decision="WATCH",
                            policy_confidence="60",
                            human_review_required=True,
                            snapshot={"k": "v"},
                            idempotency_key="idem-empty",
                            created_at="2026-05-27T00:00:00",
                            updated_at="2026-05-27T00:00:00",
                        )
                    with patch.object(
                        postgres_storage, "mirror_write",
                        lambda *a, **kw: False,
                    ):
                        database.record_review_decision(
                            decision_id="d-stale", task_id="t-empty",
                            decision="approve",
                            created_at="2026-05-27T00:00:01",
                        )

                    rows = database.list_review_decisions("t-empty")
                    # PG has 0 rows for this task → [] authoritative.
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# M12.0c-3: duplicate INSERT prevention helpers + database.py fallback.
#
# Two helpers cover the analysis_results duplicate-detection path:
#
#   * read_analysis_result_exists_by_url — Optional[bool]; True AND
#     False are PG-authoritative, only None triggers SQLite fallback.
#   * read_analysis_result_id_by_url — Optional[int]; standard
#     M12.0c-minimal single-id-lookup pattern (None → SQLite fallback).
#
# 4 tests per function × 2 functions = 8 new tests.
# ---------------------------------------------------------------------------


class ReadAnalysisResultExistsByUrlTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_analysis_result_exists_by_url(
                    "https://example.com/x",
                ),
            )

    def test_true_when_present_false_when_missing(self):
        """Combined check: True/False are BOTH authoritative when the
        engine is reachable — the load-bearing semantic that lets the
        caller skip SQLite fallback on a PG ``False``."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "exists_by_url.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                self.assertIsNotNone(engine)
                postgres_storage.ensure_schema(engine)
                postgres_storage.mirror_write(
                    "analysis_results",
                    {
                        "id": 1,
                        "query": "exists test",
                        "title": "row present",
                        "original_url": "https://example.com/present",
                        "created_at": "2026-05-27T00:00:00+00:00",
                    },
                )

                # Present URL → True (authoritative).
                self.assertEqual(
                    postgres_storage.read_analysis_result_exists_by_url(
                        "https://example.com/present",
                    ),
                    True,
                )
                # Missing URL → False (authoritative — NOT None).
                result = postgres_storage.read_analysis_result_exists_by_url(
                    "https://example.com/never-saved",
                )
                self.assertEqual(result, False)
                self.assertIsNotNone(result)
                postgres_storage.reset_engine_for_tests()


class ReadAnalysisResultIdByUrlTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_analysis_result_id_by_url(
                    "https://example.com/x",
                ),
            )

    def test_returns_latest_id_or_none(self):
        """Latest-id behaviour (``ORDER BY id DESC LIMIT 1``) plus the
        missing-row → None contract."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "id_by_url.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                # Two rows with the same URL — different ids. Helper
                # must return the newest (highest) id.
                postgres_storage.mirror_write(
                    "analysis_results",
                    {
                        "id": 10,
                        "query": "first",
                        "title": "older",
                        "original_url": "https://example.com/dup",
                        "created_at": "2026-05-27T00:00:00+00:00",
                    },
                )
                postgres_storage.mirror_write(
                    "analysis_results",
                    {
                        "id": 11,
                        "query": "second",
                        "title": "newer",
                        "original_url": "https://example.com/dup",
                        "created_at": "2026-05-27T00:01:00+00:00",
                    },
                )

                self.assertEqual(
                    postgres_storage.read_analysis_result_id_by_url(
                        "https://example.com/dup",
                    ),
                    11,
                )
                # Missing URL → None.
                self.assertIsNone(
                    postgres_storage.read_analysis_result_id_by_url(
                        "https://example.com/never",
                    ),
                )
                postgres_storage.reset_engine_for_tests()


class DatabaseDuplicateDetectionFallbackTests(unittest.TestCase):
    """Integration tests for the database.py side of M12.0c-3.

    Each case sets up two SQLite files: one is the local SQLite source
    of truth (``database.DB_PATH``) and one is the Postgres substitute
    behind ``DATABASE_URL``. Confirms that:

      * ``result_exists_by_url`` prefers Postgres when enabled and that
        PG ``False`` is authoritative over any stale SQLite row.
      * ``get_result_id_by_url`` prefers Postgres when enabled and
        falls back to SQLite when Postgres returns None.
    """

    def test_result_exists_by_url_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()  # SQLite schema only; no rows.
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # Seed PG only; SQLite stays empty.
                    postgres_storage.mirror_write(
                        "analysis_results",
                        {
                            "id": 5,
                            "query": "exists from pg",
                            "title": "in pg",
                            "original_url": "https://example.com/pg-exists",
                            "created_at": "2026-05-27T00:00:00+00:00",
                        },
                    )

                    self.assertTrue(
                        database.result_exists_by_url(
                            "https://example.com/pg-exists",
                        )
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_result_exists_by_url_pg_false_authoritative_over_stale_sqlite(self):
        """Load-bearing invariant: when PG says no row (False), the
        wrapper trusts PG even if SQLite has a stale row. Without this,
        duplicate detection would leak through and the caller would
        skip a legitimate save."""
        from text_utils import sanitize_data

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed the stale SQLite row via raw
                    # SQL (save_analysis_result no longer writes SQLite
                    # under dual-write). PG stays empty for this URL.
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO analysis_results "
                            "(id, query, title, original_url, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                1, "stale-test", "stale sqlite row",
                                "https://example.com/stale-only",
                                "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    # PG has 0 rows for this URL → False authoritative.
                    # SQLite has 1 stale row but must be IGNORED.
                    self.assertFalse(
                        database.result_exists_by_url(
                            "https://example.com/stale-only",
                        )
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_get_result_id_by_url_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    postgres_storage.mirror_write(
                        "analysis_results",
                        {
                            "id": 99,
                            "query": "id from pg",
                            "title": "pg row",
                            "original_url": "https://example.com/pg-id",
                            "created_at": "2026-05-27T00:00:00+00:00",
                        },
                    )

                    self.assertEqual(
                        database.get_result_id_by_url(
                            "https://example.com/pg-id",
                        ),
                        99,
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_get_result_id_by_url_returns_none_when_pg_empty(self):
        """M12.0d-1: PG enabled + empty → None. SQLite fallback is
        unreachable when dual-write is enabled."""
        from text_utils import sanitize_data

        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg_substitute.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed SQLite only via raw SQL
                    # (save_analysis_result no longer writes SQLite under
                    # dual-write). PG stays empty for this URL.
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO analysis_results "
                            "(id, query, title, original_url, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                3, "sqlite-id-test", "sqlite-only id source",
                                "https://example.com/sqlite-id-only",
                                "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    # PG returns None for this URL → function returns
                    # None (SQLite row is ignored under Stage 1).
                    self.assertIsNone(
                        database.get_result_id_by_url(
                            "https://example.com/sqlite-id-only",
                        ),
                    )
                    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# M12.0c-4: operator CLI table read helpers + database.py fallback.
#
# Five helpers cover the operator-CLI tables:
#
#   * read_fetch_artifacts             (source_fetch_artifacts)
#   * read_extraction_results          (artifact_text_extractions)
#   * read_evidence_candidates         (artifact_evidence_candidates)
#   * read_producer_comparisons        (verdict_producer_comparisons)
#   * read_verdict_label_attributions  (verdict_label_attributions)
#
# Test layout: 2 unit + 2 integration per function (20) + 2 db_path-skip
# tests (extraction + producer_comparisons) = 22 new tests.
# ---------------------------------------------------------------------------


def _seed_fetch_artifact_in_pg(*, source_id, url, fetch_timestamp,
                                created_at="2026-05-27T00:00:00",
                                success=1):
    import postgres_storage

    postgres_storage.mirror_write(
        "source_fetch_artifacts",
        {
            "source_id": source_id, "url": url,
            "fetch_timestamp": fetch_timestamp,
            "success": success, "truth_claim": 0,
            "official_source_candidate": 0,
            "created_at": created_at,
        },
    )


def _seed_extraction_result_in_pg(*, artifact_id, source_id, url,
                                   extraction_timestamp,
                                   created_at="2026-05-27T00:00:00",
                                   success=1):
    import postgres_storage

    postgres_storage.mirror_write(
        "artifact_text_extractions",
        {
            "artifact_id": artifact_id, "source_id": source_id,
            "url": url, "extraction_timestamp": extraction_timestamp,
            "success": success, "truth_claim": 0,
            "official_source_candidate": 0,
            "created_at": created_at,
        },
    )


def _seed_evidence_candidate_in_pg(*, extraction_id, source_id, url,
                                    analysis_id, claim_text,
                                    candidate_timestamp,
                                    created_at="2026-05-27T00:00:00",
                                    match_score=0.5):
    import postgres_storage

    postgres_storage.mirror_write(
        "artifact_evidence_candidates",
        {
            "extraction_id": extraction_id, "source_id": source_id,
            "url": url, "analysis_id": analysis_id,
            "claim_text": claim_text, "match_score": match_score,
            "candidate_timestamp": candidate_timestamp,
            "truth_claim": 0, "official_source_candidate": 0,
            "operator_review_required": 1,
            "created_at": created_at,
        },
    )


def _seed_producer_comparison_in_pg(*, analysis_id, source, input_hash,
                                     comparison_timestamp,
                                     created_at="2026-05-27T00:00:00",
                                     all_three_agree=1,
                                     disagreement_pattern=None):
    import postgres_storage

    postgres_storage.mirror_upsert(
        "verdict_producer_comparisons",
        {
            "analysis_id": analysis_id, "source": source,
            "input_hash": input_hash,
            "comparison_timestamp": comparison_timestamp,
            "all_three_agree": all_three_agree,
            "p1_p2_agree": all_three_agree,
            "p1_p3_agree": all_three_agree,
            "p2_p3_agree": all_three_agree,
            "disagreement_pattern": disagreement_pattern,
            "truth_claim": 0, "operator_review_required": 1,
            "created_at": created_at,
        },
        ["input_hash"],
    )


def _seed_verdict_label_attribution_in_pg(*, analysis_id,
                                           diagnostic_timestamp,
                                           created_at="2026-05-27T00:00:00",
                                           attributed_branch_id=None,
                                           is_weak_evidence_verified=0):
    import postgres_storage

    postgres_storage.mirror_upsert(
        "verdict_label_attributions",
        {
            "analysis_id": analysis_id,
            "diagnostic_timestamp": diagnostic_timestamp,
            "attributed_branch_id": attributed_branch_id,
            "is_weak_evidence_verified": is_weak_evidence_verified,
            "truth_claim": 0, "operator_review_required": 1,
            "created_at": created_at,
        },
        ["analysis_id"],
    )


# -- Unit tests (10): 2 per helper ------------------------------------


class ReadFetchArtifactsTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(postgres_storage.read_fetch_artifacts())

    def test_filters_and_pagination(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rfa.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_fetch_artifact_in_pg(
                    source_id="src-a", url="https://a/1",
                    fetch_timestamp="2026-05-27T00:00:01",
                )
                _seed_fetch_artifact_in_pg(
                    source_id="src-a", url="https://a/2",
                    fetch_timestamp="2026-05-27T00:00:02",
                )
                _seed_fetch_artifact_in_pg(
                    source_id="src-b", url="https://b/1",
                    fetch_timestamp="2026-05-27T00:00:03",
                )

                # source filter narrows + newest-first.
                rows = postgres_storage.read_fetch_artifacts(
                    source_id="src-a", limit=10,
                )
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["url"], "https://a/2")
                self.assertEqual(rows[1]["url"], "https://a/1")
                # No-match filter → [] authoritative (NOT None).
                empty = postgres_storage.read_fetch_artifacts(
                    source_id="nonexistent",
                )
                self.assertEqual(empty, [])
                self.assertIsNotNone(empty)
                postgres_storage.reset_engine_for_tests()


class ReadExtractionResultsTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(postgres_storage.read_extraction_results())

    def test_filters_by_source_and_artifact_id(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rer.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_extraction_result_in_pg(
                    artifact_id=10, source_id="src-x", url="u1",
                    extraction_timestamp="2026-05-27T00:00:01",
                )
                _seed_extraction_result_in_pg(
                    artifact_id=11, source_id="src-x", url="u2",
                    extraction_timestamp="2026-05-27T00:00:02",
                )
                _seed_extraction_result_in_pg(
                    artifact_id=12, source_id="src-y", url="u3",
                    extraction_timestamp="2026-05-27T00:00:03",
                )

                # source filter
                rows = postgres_storage.read_extraction_results(
                    source_id="src-x",
                )
                self.assertEqual(len(rows), 2)
                # artifact_id filter
                one = postgres_storage.read_extraction_results(
                    artifact_id=11,
                )
                self.assertEqual(len(one), 1)
                self.assertEqual(one[0]["url"], "u2")
                # Combined filter
                none_match = postgres_storage.read_extraction_results(
                    source_id="src-x", artifact_id=12,
                )
                self.assertEqual(none_match, [])
                postgres_storage.reset_engine_for_tests()


class ReadEvidenceCandidatesTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_evidence_candidates(),
            )

    def test_filters_by_analysis_source_extraction(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rec.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_evidence_candidate_in_pg(
                    extraction_id=1, source_id="src-a", url="u1",
                    analysis_id="ana-1", claim_text="c1",
                    candidate_timestamp="2026-05-27T00:00:01",
                )
                _seed_evidence_candidate_in_pg(
                    extraction_id=2, source_id="src-a", url="u2",
                    analysis_id="ana-1", claim_text="c2",
                    candidate_timestamp="2026-05-27T00:00:02",
                )
                _seed_evidence_candidate_in_pg(
                    extraction_id=3, source_id="src-b", url="u3",
                    analysis_id="ana-2", claim_text="c3",
                    candidate_timestamp="2026-05-27T00:00:03",
                )

                # analysis_id filter
                ana1 = postgres_storage.read_evidence_candidates(
                    analysis_id="ana-1",
                )
                self.assertEqual(len(ana1), 2)
                # source_id filter
                srcb = postgres_storage.read_evidence_candidates(
                    source_id="src-b",
                )
                self.assertEqual(len(srcb), 1)
                self.assertEqual(srcb[0]["analysis_id"], "ana-2")
                # extraction_id filter
                ext2 = postgres_storage.read_evidence_candidates(
                    extraction_id=2,
                )
                self.assertEqual(len(ext2), 1)
                # Empty-string analysis_id MUST NOT inject WHERE.
                # (truthy guard parity with SQLite-side.)
                all_rows = postgres_storage.read_evidence_candidates(
                    analysis_id="",
                )
                self.assertEqual(len(all_rows), 3)
                postgres_storage.reset_engine_for_tests()


class ReadProducerComparisonsTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_producer_comparisons(),
            )

    def test_only_disagreements_and_pattern_filter(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rpc.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_producer_comparison_in_pg(
                    analysis_id="ana-1", source="s1",
                    input_hash="h-agree",
                    comparison_timestamp="2026-05-27T00:00:01",
                    all_three_agree=1, disagreement_pattern=None,
                )
                _seed_producer_comparison_in_pg(
                    analysis_id="ana-2", source="s2",
                    input_hash="h-disagree-1",
                    comparison_timestamp="2026-05-27T00:00:02",
                    all_three_agree=0,
                    disagreement_pattern="p1_only",
                )
                _seed_producer_comparison_in_pg(
                    analysis_id="ana-3", source="s3",
                    input_hash="h-disagree-2",
                    comparison_timestamp="2026-05-27T00:00:03",
                    all_three_agree=0,
                    disagreement_pattern="p2_only",
                )

                # only_disagreements maps to all_three_agree == 0.
                disagree = postgres_storage.read_producer_comparisons(
                    only_disagreements=True,
                )
                self.assertEqual(len(disagree), 2)
                self.assertTrue(
                    all(r["all_three_agree"] == 0 for r in disagree)
                )
                # Pattern filter narrows further.
                p1 = postgres_storage.read_producer_comparisons(
                    disagreement_pattern="p1_only",
                )
                self.assertEqual(len(p1), 1)
                self.assertEqual(p1[0]["analysis_id"], "ana-2")
                postgres_storage.reset_engine_for_tests()


class ReadVerdictLabelAttributionsTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_verdict_label_attributions(),
            )

    def test_only_weak_and_branch_filter(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rvla.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_verdict_label_attribution_in_pg(
                    analysis_id="ana-1",
                    diagnostic_timestamp="2026-05-27T00:00:01",
                    attributed_branch_id="branch_A",
                    is_weak_evidence_verified=0,
                )
                _seed_verdict_label_attribution_in_pg(
                    analysis_id="ana-2",
                    diagnostic_timestamp="2026-05-27T00:00:02",
                    attributed_branch_id="branch_B",
                    is_weak_evidence_verified=1,
                )
                _seed_verdict_label_attribution_in_pg(
                    analysis_id="ana-3",
                    diagnostic_timestamp="2026-05-27T00:00:03",
                    attributed_branch_id="branch_A",
                    is_weak_evidence_verified=1,
                )

                # only_weak_evidence_verified → is_weak_evidence_verified=1.
                weak = postgres_storage.read_verdict_label_attributions(
                    only_weak_evidence_verified=True,
                )
                self.assertEqual(len(weak), 2)
                # Branch filter.
                branch_a = (
                    postgres_storage.read_verdict_label_attributions(
                        attributed_branch_id="branch_A",
                    )
                )
                self.assertEqual(len(branch_a), 2)
                # Combined filter.
                weak_a = (
                    postgres_storage.read_verdict_label_attributions(
                        attributed_branch_id="branch_A",
                        only_weak_evidence_verified=True,
                    )
                )
                self.assertEqual(len(weak_a), 1)
                self.assertEqual(weak_a[0]["analysis_id"], "ana-3")
                postgres_storage.reset_engine_for_tests()


# -- Integration tests (10): 2 per wrapper + db_path skip (2) ---------


class DatabaseOperatorCliFallbackTests(unittest.TestCase):
    """Integration tests for the database.py side of M12.0c-4."""

    # --- get_fetch_artifacts -----------------------------------------

    def test_get_fetch_artifacts_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_source_fetch_artifacts_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_fetch_artifact_in_pg(
                        source_id="pg-src", url="https://pg/x",
                        fetch_timestamp="2026-05-27T00:00:01",
                    )

                    rows = database.get_fetch_artifacts()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["source_id"], "pg-src")
                    # _row_to_fetch_artifact transformation applied.
                    self.assertIsInstance(rows[0]["success"], bool)
                    self.assertIsInstance(rows[0]["truth_claim"], bool)
                    postgres_storage.reset_engine_for_tests()

    def test_get_fetch_artifacts_pg_empty_list_authoritative(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_source_fetch_artifacts_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed a STALE row directly into
                    # SQLite via raw SQL. Under dual-write, save_fetch_artifact
                    # no longer writes SQLite (PG-only), so we bypass it to
                    # reproduce the "stale SQLite, empty PG" scenario this
                    # test pins (PG-empty [] must win).
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO source_fetch_artifacts "
                            "(source_id, url, fetch_timestamp, success, "
                            "truth_claim, official_source_candidate, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                "sqlite-only", "https://stale/x",
                                "2026-05-27T00:00:00", 1, 0, 0,
                                "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    rows = database.get_fetch_artifacts()
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()

    # --- get_extraction_results --------------------------------------

    def test_get_extraction_results_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_artifact_text_extractions_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_extraction_result_in_pg(
                        artifact_id=42, source_id="pg-src",
                        url="https://pg/e",
                        extraction_timestamp="2026-05-27T00:00:01",
                    )

                    rows = database.get_extraction_results()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["artifact_id"], 42)
                    self.assertIsInstance(rows[0]["success"], bool)
                    postgres_storage.reset_engine_for_tests()

    def test_get_extraction_results_pg_empty_list_authoritative(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_artifact_text_extractions_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    with patch.object(
                        postgres_storage, "mirror_write",
                        lambda *a, **kw: False,
                    ):
                        database.save_extraction_result({
                            "artifact_id": 1,
                            "source_id": "stale",
                            "url": "https://stale/y",
                            "extraction_timestamp": "2026-05-27T00:00:00",
                            "success": True,
                        })

                    rows = database.get_extraction_results()
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()

    # --- get_evidence_candidates -------------------------------------

    def test_get_evidence_candidates_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_artifact_evidence_candidates_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_evidence_candidate_in_pg(
                        extraction_id=5, source_id="pg-src",
                        url="https://pg/c", analysis_id="ana-pg",
                        claim_text="claim from pg",
                        candidate_timestamp="2026-05-27T00:00:01",
                    )

                    rows = database.get_evidence_candidates(
                        analysis_id="ana-pg",
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["claim_text"], "claim from pg")
                    # bool conversions applied.
                    self.assertIsInstance(
                        rows[0]["operator_review_required"], bool,
                    )
                    self.assertIsInstance(rows[0]["match_score"], float)
                    postgres_storage.reset_engine_for_tests()

    def test_get_evidence_candidates_pg_empty_list_authoritative(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_artifact_evidence_candidates_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    with patch.object(
                        postgres_storage, "mirror_write",
                        lambda *a, **kw: False,
                    ):
                        database.save_evidence_candidate({
                            "extraction_id": 1, "source_id": "stale",
                            "url": "u", "analysis_id": "ana-stale",
                            "claim_text": "c",
                            "candidate_timestamp": "2026-05-27T00:00:00",
                        })

                    rows = database.get_evidence_candidates()
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()

    # --- get_producer_comparisons ------------------------------------

    def test_get_producer_comparisons_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_verdict_producer_comparisons_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_producer_comparison_in_pg(
                        analysis_id="ana-pc", source="pc-src",
                        input_hash="h-pc-1",
                        comparison_timestamp="2026-05-27T00:00:01",
                    )

                    rows = database.get_producer_comparisons(
                        analysis_id="ana-pc",
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["source"], "pc-src")
                    self.assertIsInstance(rows[0]["all_three_agree"], bool)
                    postgres_storage.reset_engine_for_tests()

    def test_get_producer_comparisons_pg_empty_list_authoritative(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_verdict_producer_comparisons_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed a STALE row directly into
                    # SQLite via raw SQL. Under dual-write, save_producer_
                    # comparison no longer writes SQLite (PG-only), so we
                    # bypass it to reproduce the "stale SQLite, empty PG"
                    # scenario this test pins (PG-empty [] must win).
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO verdict_producer_comparisons "
                            "(analysis_id, source, input_hash, "
                            "comparison_timestamp, truth_claim, "
                            "operator_review_required, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                "ana-stale", "s", "h-stale",
                                "2026-05-27T00:00:00", 0, 1,
                                "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    rows = database.get_producer_comparisons()
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()

    # --- get_verdict_label_attributions ------------------------------

    def test_get_verdict_label_attributions_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_verdict_label_attributions_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_verdict_label_attribution_in_pg(
                        analysis_id="ana-vla",
                        diagnostic_timestamp="2026-05-27T00:00:01",
                        attributed_branch_id="branch_X",
                        is_weak_evidence_verified=1,
                    )

                    rows = database.get_verdict_label_attributions(
                        analysis_id="ana-vla",
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        rows[0]["attributed_branch_id"], "branch_X",
                    )
                    self.assertIsInstance(
                        rows[0]["is_weak_evidence_verified"], bool,
                    )
                    self.assertTrue(rows[0]["is_weak_evidence_verified"])
                    postgres_storage.reset_engine_for_tests()

    def test_get_verdict_label_attributions_pg_empty_list_authoritative(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_verdict_label_attributions_table()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # M12.0d Stage 3c-3: seed a STALE row directly into
                    # SQLite via raw SQL. Under dual-write, save_verdict_
                    # label_attribution no longer writes SQLite (PG-only),
                    # so we bypass it to reproduce the "stale SQLite, empty
                    # PG" scenario this test pins (PG-empty [] must win).
                    with database.get_connection() as conn:
                        conn.execute(
                            "INSERT INTO verdict_label_attributions "
                            "(analysis_id, diagnostic_timestamp, "
                            "is_weak_evidence_verified, truth_claim, "
                            "operator_review_required, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                "ana-stale-vla", "2026-05-27T00:00:00",
                                0, 0, 1, "2026-05-27T00:00:00+00:00",
                            ),
                        )
                        conn.commit()

                    rows = database.get_verdict_label_attributions()
                    self.assertEqual(rows, [])
                    postgres_storage.reset_engine_for_tests()

    # --- db_path skip (2) --------------------------------------------

    def test_get_extraction_results_with_db_path_skips_postgres(self):
        """When the caller passes an explicit ``db_path``, PG MUST be
        skipped — the caller is opting into a specific SQLite file
        (CLI's ``--db-path`` flag / isolated tests)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                explicit_db = Path(tmp_dir) / "explicit.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                import database
                import postgres_storage

                # PG has a row that would be visible if we did not
                # skip — but the caller passed db_path, so PG MUST be
                # ignored.
                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_extraction_result_in_pg(
                    artifact_id=999, source_id="pg-src",
                    url="https://pg/should-not-appear",
                    extraction_timestamp="2026-05-27T00:00:01",
                )
                # explicit_db is empty — initialise its schema directly.
                with patch("database.DB_PATH", explicit_db):
                    database.init_artifact_text_extractions_table()

                rows = database.get_extraction_results(db_path=explicit_db)
                # PG had a row but db_path was passed → SQLite only.
                self.assertEqual(rows, [])
                postgres_storage.reset_engine_for_tests()

    def test_get_producer_comparisons_with_db_path_skips_postgres(self):
        """db_path skip with a different filter shape
        (``only_disagreements`` bool flag)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                explicit_db = Path(tmp_dir) / "explicit.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                import database
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_producer_comparison_in_pg(
                    analysis_id="ana-pg-skip", source="s",
                    input_hash="h-pg-skip",
                    comparison_timestamp="2026-05-27T00:00:01",
                    all_three_agree=0,
                    disagreement_pattern="p1_only",
                )
                with patch("database.DB_PATH", explicit_db):
                    database.init_verdict_producer_comparisons_table()

                rows = database.get_producer_comparisons(
                    only_disagreements=True, db_path=explicit_db,
                )
                self.assertEqual(rows, [])
                postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# M12.0c-jobs: jobs table write+read mirroring + database.py fallback.
#
# Unlike the earlier M12.0c sub-milestones (which only added read
# fallback on top of pre-existing M12.0a mirror_writes), this milestone
# is a PAIRED write+read migration: nothing was previously mirroring
# the jobs table into postgres_storage.jobs_table — the schema existed
# but no caller wrote to it. So tests cover BOTH the new dual-write
# path inside job_manager AND the new read fallback in get_job_status.
#
# 7 write tests + 5 read tests + 1 parity test = 13 new tests.
# ---------------------------------------------------------------------------


def _seed_job_in_pg(*, job_id, status="queued", query="q", max_news=5,
                    progress_percent=0, current_stage="queued",
                    result_id=None, error_message=None,
                    created_at="2026-05-27T00:00:00",
                    started_at=None, completed_at=None,
                    pipeline_version="phase2-m2"):
    """Helper: write a jobs row directly into the PG mirror so read
    helpers have something to find. Assumes engine is built and the
    schema is in place."""
    import postgres_storage

    postgres_storage.mirror_upsert(
        "jobs",
        {
            "id": job_id, "status": status, "query": query,
            "max_news": max_news,
            "progress_percent": progress_percent,
            "current_stage": current_stage,
            "result_id": result_id,
            "error_message": error_message,
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "pipeline_version": pipeline_version,
        },
        ["id"],
    )


def _pg_row_for_job(engine, job_id):
    """Direct PG read for assertions inside dual-write tests."""
    import postgres_storage

    with engine.connect() as conn:
        row = conn.execute(
            sa.select(postgres_storage.jobs_table).where(
                postgres_storage.jobs_table.c.id == job_id,
            )
        ).first()
    return dict(row._mapping) if row is not None else None


# -- Write tests (5 + 2 isolation) ------------------------------------


class JobsMirrorWriteTests(unittest.TestCase):
    def test_create_job_mirrors_full_row_to_postgres(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    record = job_manager.create_job(
                        query="hello", max_news=7,
                    )
                    job_id = record["id"]

                    pg_row = _pg_row_for_job(engine, job_id)
                    self.assertIsNotNone(pg_row)
                    self.assertEqual(pg_row["id"], job_id)
                    self.assertEqual(pg_row["status"], "queued")
                    self.assertEqual(pg_row["query"], "hello")
                    self.assertEqual(pg_row["max_news"], 7)
                    self.assertEqual(pg_row["progress_percent"], 0)
                    self.assertEqual(pg_row["current_stage"], "queued")
                    self.assertIsNone(pg_row["result_id"])
                    self.assertIsNone(pg_row["error_message"])
                    postgres_storage.reset_engine_for_tests()

    def test_start_job_updates_postgres_mirror(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    record = job_manager.create_job(
                        query="q", max_news=1,
                    )
                    job_id = record["id"]
                    job_manager.start_job(job_id)

                    pg_row = _pg_row_for_job(engine, job_id)
                    self.assertIsNotNone(pg_row)
                    self.assertEqual(pg_row["status"], "running")
                    self.assertEqual(pg_row["current_stage"], "running")
                    self.assertEqual(pg_row["progress_percent"], 5)
                    self.assertIsNotNone(pg_row["started_at"])
                    postgres_storage.reset_engine_for_tests()

    def test_update_progress_mirrors_to_postgres(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    record = job_manager.create_job(query="q", max_news=1)
                    job_id = record["id"]
                    job_manager.start_job(job_id)
                    job_manager.update_progress(
                        job_id, "news_collecting", 35,
                    )

                    pg_row = _pg_row_for_job(engine, job_id)
                    self.assertEqual(
                        pg_row["current_stage"], "news_collecting",
                    )
                    self.assertEqual(pg_row["progress_percent"], 35)
                    # Status stays 'running' through progress updates.
                    self.assertEqual(pg_row["status"], "running")
                    postgres_storage.reset_engine_for_tests()

    def test_complete_job_mirrors_terminal_state(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    record = job_manager.create_job(query="q", max_news=1)
                    job_id = record["id"]
                    job_manager.start_job(job_id)
                    job_manager.complete_job(job_id, result_id=42)

                    pg_row = _pg_row_for_job(engine, job_id)
                    self.assertEqual(pg_row["status"], "completed")
                    self.assertEqual(pg_row["current_stage"], "completed")
                    self.assertEqual(pg_row["progress_percent"], 100)
                    self.assertEqual(pg_row["result_id"], 42)
                    self.assertIsNone(pg_row["error_message"])
                    self.assertIsNotNone(pg_row["completed_at"])
                    postgres_storage.reset_engine_for_tests()

    def test_fail_job_mirrors_error_message(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    record = job_manager.create_job(query="q", max_news=1)
                    job_id = record["id"]
                    job_manager.start_job(job_id)
                    job_manager.fail_job(job_id, "boom: openai 500")

                    pg_row = _pg_row_for_job(engine, job_id)
                    self.assertEqual(pg_row["status"], "failed")
                    self.assertEqual(
                        pg_row["error_message"], "boom: openai 500",
                    )
                    self.assertIsNotNone(pg_row["completed_at"])
                    postgres_storage.reset_engine_for_tests()


# M12.0d Stage 3c-2: JobsMirrorIsolationTests (the previous "SQLite
# survives PG mirror failure" contract for jobs writes) was removed
# because that contract no longer exists. After 3c-2 jobs writes go
# to PG only — there is no SQLite fallback to test. The equivalent
# class PostgresIsolationTests in tests/test_jobs.py was deleted for
# the same reason.


# -- Read tests (3 unit + 2 integration) -------------------------------


class ReadJobByIdTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(postgres_storage.read_job_by_id("any"))

    def test_returns_dict_when_present(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rjid.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_job_in_pg(
                    job_id="abc-123", status="running",
                    query="seeded", max_news=3,
                    progress_percent=42,
                    current_stage="news_collecting",
                )

                row = postgres_storage.read_job_by_id("abc-123")
                self.assertIsNotNone(row)
                self.assertEqual(row["id"], "abc-123")
                self.assertEqual(row["status"], "running")
                self.assertEqual(row["query"], "seeded")
                self.assertEqual(row["progress_percent"], 42)
                postgres_storage.reset_engine_for_tests()

    def test_returns_none_when_missing(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "rjid_miss.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{tmp_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                self.assertIsNone(
                    postgres_storage.read_job_by_id("never-seeded"),
                )
                postgres_storage.reset_engine_for_tests()


class JobManagerGetJobStatusFallbackTests(unittest.TestCase):
    def test_get_job_status_prefers_postgres_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_job_in_pg(
                        job_id="from-pg", status="running",
                        query="pg-only-query",
                        progress_percent=60,
                    )
                    # SQLite is empty for this job_id.

                    status = job_manager.get_job_status("from-pg")
                    self.assertIsNotNone(status)
                    self.assertEqual(status["id"], "from-pg")
                    self.assertEqual(status["job_id"], "from-pg")  # alias
                    self.assertEqual(status["status"], "running")
                    self.assertEqual(status["query"], "pg-only-query")
                    self.assertEqual(status["progress_percent"], 60)
                    postgres_storage.reset_engine_for_tests()

    def test_get_job_status_returns_none_when_pg_empty(self):
        """M12.0d-1: PG enabled + empty → None. SQLite fallback is
        unreachable when dual-write is enabled."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _set_env(USE_POSTGRES_WRITE="true",
                         DATABASE_URL=f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import job_manager
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    # Suppress PG mirror so create_job writes SQLite only.
                    # Pre-M12.0d-1 the SQLite row leaked through; Stage 1
                    # hides it under the PG-authoritative contract.
                    with patch.object(
                        postgres_storage, "mirror_write",
                        lambda *a, **kw: False,
                    ):
                        record = job_manager.create_job(
                            query="sqlite-only", max_news=2,
                        )

                    status = job_manager.get_job_status(record["id"])
                    self.assertIsNone(status)
                    postgres_storage.reset_engine_for_tests()


# -- Parity (1) -------------------------------------------------------


# M12.0d Stage 3c-2: JobsDualWriteParityTests (the previous "SQLite ==
# PG mirror byte-identical across all 12 columns" invariant for jobs)
# was removed because that contract no longer exists. After 3c-2 the
# SQLite jobs table is never written to, so a row-by-row comparison is
# meaningless. The helper job_manager._read_jobs_row_full was also
# removed in the same change.


class ModuleLevelStaticChecks(unittest.TestCase):
    """Pure source-text inspection. Cheap and stable; catches the
    invariant the brief asks for without needing to wrangle Python's
    import machinery."""

    def setUp(self):
        self.module_path = _PROJECT_ROOT / "postgres_storage.py"
        self.source = self.module_path.read_text(encoding="utf-8")

    def test_does_not_import_database_module(self):
        """A circular import database <-> postgres_storage would
        break ``from postgres_storage import mirror_write`` inside
        database.py. The brief explicitly forbids this direction."""
        forbidden = re.compile(
            r"^(?:from\s+database\b|import\s+database\b)",
            re.MULTILINE,
        )
        self.assertIsNone(
            forbidden.search(self.source),
            msg="postgres_storage.py must not import database",
        )

    def test_does_not_import_openai_or_anthropic(self):
        for needle in ("openai", "anthropic"):
            self.assertNotIn(
                needle, self.source,
                msg=f"postgres_storage.py must not reference {needle}",
            )

    def test_main_api_scheduler_do_not_import_postgres_storage(self):
        """Per the brief: only database.py imports postgres_storage,
        and only inside try blocks. main.py / api_server.py /
        scheduler.py must not touch it at module load."""
        targets = ("main.py", "api_server.py", "scheduler.py")
        forbidden = re.compile(
            r"^(?:from\s+postgres_storage\b|import\s+postgres_storage\b)",
            re.MULTILINE,
        )
        for filename in targets:
            path = _PROJECT_ROOT / filename
            if not path.exists():
                continue  # scheduler.py may be optional
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                forbidden.search(text),
                msg=(
                    f"{filename} must not import postgres_storage at "
                    "module level"
                ),
            )

    def test_database_uses_lazy_import_inside_helpers(self):
        """database.py must import postgres_storage only inside the
        try blocks so a missing dependency at boot does not crash
        analyze_pipeline."""
        db_text = (_PROJECT_ROOT / "database.py").read_text(encoding="utf-8")
        # The module must not have a top-level postgres_storage import.
        top_level = re.search(
            r"^(?:from\s+postgres_storage\b|import\s+postgres_storage\b)",
            db_text, re.MULTILINE,
        )
        self.assertIsNone(
            top_level,
            msg=(
                "database.py imports postgres_storage at module level — "
                "import must be inside the try block in "
                "_mirror_write_safe / _mirror_upsert_safe."
            ),
        )
        # And it must reference postgres_storage somewhere (otherwise
        # the dual-write hooks are missing entirely).
        self.assertIn(
            "from postgres_storage import", db_text,
            msg=(
                "database.py does not import postgres_storage at all — "
                "dual-write hooks are missing."
            ),
        )


if __name__ == "__main__":
    unittest.main()
