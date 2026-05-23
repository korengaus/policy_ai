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

    def test_get_engine_returns_none_when_url_empty(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true", DATABASE_URL="")
            import postgres_storage

            self.assertIsNone(postgres_storage.get_engine())

    def test_get_engine_does_not_raise_on_invalid_url(self):
        with _EnvScope():
            # SQLAlchemy refuses to parse a meaningless dialect; the
            # helper must catch that and return None instead of letting
            # the exception escape.
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="not-a-real-url-dialect:///nowhere")
            import postgres_storage

            try:
                result = postgres_storage.get_engine()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"get_engine raised: {exc!r}")
            self.assertIsNone(result)

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
    """Confirms that even if postgres_storage's exported helpers raise
    unexpectedly, the SQLite write path in database.py still succeeds
    and returns its normal payload."""

    def _patched_mirror_write(self, *args, **kwargs):
        raise RuntimeError("simulated mirror_write failure")

    def _patched_mirror_upsert(self, *args, **kwargs):
        raise RuntimeError("simulated mirror_upsert failure")

    def test_save_analysis_result_isolated_from_mirror_write_failure(self):
        from text_utils import sanitize_data

        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "isolation_analysis.db"
                with patch("database.DB_PATH", tmp_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    sample = {
                        "title": "isolation test",
                        "original_url": "https://example.com/iso-1",
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
                    with patch.object(
                        postgres_storage, "mirror_write",
                        self._patched_mirror_write,
                    ):
                        status = database.save_analysis_result(
                            sanitize_data(sample), query="iso-test",
                        )
                    self.assertTrue(status["saved"])
                    self.assertEqual(
                        status["duplicate"], False,
                    )
                    rows = database.get_recent_results(limit=5)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        rows[0]["original_url"], sample["original_url"],
                    )

    def test_save_fetch_artifact_isolated_from_mirror_write_failure(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "isolation_fetch.db"
                with patch("database.DB_PATH", tmp_db):
                    import database
                    import postgres_storage

                    database.init_source_fetch_artifacts_table()
                    fetch_result = {
                        "source_id": "kr_law_open_data_candidate",
                        "url": "https://www.law.go.kr/x",
                        "fetch_timestamp": "2026-05-23T00:00:00",
                        "success": True,
                    }
                    with patch.object(
                        postgres_storage, "mirror_write",
                        self._patched_mirror_write,
                    ):
                        row_id = database.save_fetch_artifact(fetch_result)
                    self.assertGreater(row_id, 0)
                    artifacts = database.get_fetch_artifacts(
                        source_id="kr_law_open_data_candidate",
                    )
                    self.assertEqual(len(artifacts), 1)

    def test_record_review_decision_isolated_from_mirror_failure(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "isolation_decision.db"
                with patch("database.DB_PATH", tmp_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    # Need a parent task row first.
                    database.create_review_task(
                        task_id="t1", result_id="r1", job_id="j1",
                        item_index=0, status="open", query="q",
                        claim_text="c", title="t", url="u",
                        final_decision="WATCH", policy_confidence="60",
                        human_review_required=True,
                        snapshot={"k": "v"},
                        idempotency_key="idem-decision",
                        created_at="2026-05-23T00:00:00",
                        updated_at="2026-05-23T00:00:00",
                    )
                    with patch.object(
                        postgres_storage, "mirror_write",
                        self._patched_mirror_write,
                    ):
                        record = database.record_review_decision(
                            decision_id="d1", task_id="t1",
                            decision="approve",
                            created_at="2026-05-23T00:00:01",
                        )
                    self.assertTrue(record)
                    decisions = database.list_review_decisions("t1")
                    self.assertEqual(len(decisions), 1)


# ---------------------------------------------------------------------------
# Static-source checks for module-level imports and contracts the brief
# explicitly calls out.
# ---------------------------------------------------------------------------


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
