"""M12.0d-1 Stage 1 — raise-on-PG-failure regression tests.

Run with: python tests/test_m12_0d_stage1_raise.py

These tests pin the new Stage 1 contract:

  * postgres_storage read helpers RAISE ``PostgresReadError`` on
    SQLAlchemy / engine errors instead of swallowing into a None
    return.
  * The 15 PG-primary read functions in database.py + job_manager.py
    log.error + re-raise on PG read failure instead of silently
    falling back to SQLite.
  * The import failure path is also fatal — a broken
    ``postgres_storage`` module surfaces instead of silently leaking
    a stale SQLite row.

Reuses the same ``sqlite:///<tmp>`` substrate as
``tests/test_postgres_storage.py`` so no real Postgres is required.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, SQLAlchemyError


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Env-var scaffold — keep parity with tests/test_postgres_storage.py so
# a misbehaving test cannot leak USE_POSTGRES_WRITE / DATABASE_URL.
# ---------------------------------------------------------------------------


class _EnvScope:
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


def _set_env(**kwargs):
    for key, value in kwargs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


def _enable_pg(pg_url: str) -> None:
    _set_env(USE_POSTGRES_WRITE="true", DATABASE_URL=pg_url)


# ---------------------------------------------------------------------------
# Part A — postgres_storage helpers RAISE on engine error.
#
# We exercise the error path by mocking ``engine.connect`` to raise
# OperationalError. The helper must propagate as PostgresReadError
# (Stage 1 contract) instead of swallowing into None.
# ---------------------------------------------------------------------------


# Helpers and their call signatures keyed by name, so we can parametrize
# the raise / no-raise tests without 30 near-identical methods.
_SINGLE_ROW_HELPERS = (
    ("read_analysis_result_by_id", (1,), {}),
    ("read_review_task_by_task_id", ("t",), {}),
    ("read_review_task_by_idempotency_key", ("idem",), {}),
    ("read_review_decision_by_id", ("d",), {}),
    ("read_job_by_id", ("j",), {}),
    ("read_analysis_result_id_by_url", ("https://x",), {}),
)


_LIST_HELPERS = (
    ("read_recent_analysis_results", (), {"limit": 5}),
    ("read_review_tasks", (), {}),
    ("read_review_decisions_for_task", ("t",), {}),
    ("read_fetch_artifacts", (), {}),
    ("read_extraction_results", (), {}),
    ("read_evidence_candidates", (), {}),
    ("read_producer_comparisons", (), {}),
    ("read_verdict_label_attributions", (), {}),
)


_BOOL_HELPERS = (
    ("read_analysis_result_exists_by_url", ("https://x",), {}),
)


_ALL_HELPERS = _SINGLE_ROW_HELPERS + _LIST_HELPERS + _BOOL_HELPERS


class HelperRaisesOnEngineErrorTests(unittest.TestCase):
    """Mock engine.connect to raise SQLAlchemyError; assert each helper
    raises PostgresReadError instead of returning None."""

    def _assert_helper_raises(self, helper_name, args, kwargs):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                self.assertIsNotNone(engine)
                postgres_storage.ensure_schema(engine)

                helper = getattr(postgres_storage, helper_name)

                # Force the next engine.connect() to raise. Use the
                # real cached engine so the helper's code path matches
                # production (engine returned, .connect raises inside
                # the try block).
                def _boom(*a, **kw):
                    raise OperationalError(
                        "simulated", params=None, orig=Exception("boom"),
                    )

                with patch.object(engine, "connect", side_effect=_boom):
                    with self.assertRaises(
                        postgres_storage.PostgresReadError,
                        msg=f"{helper_name} should raise PostgresReadError",
                    ) as cm:
                        helper(*args, **kwargs)
                    # Cause chained from OperationalError per `raise X from exc`.
                    self.assertIsInstance(cm.exception.__cause__, SQLAlchemyError)

                postgres_storage.reset_engine_for_tests()

    def test_all_helpers_raise_on_engine_error(self):
        for helper_name, args, kwargs in _ALL_HELPERS:
            with self.subTest(helper=helper_name):
                self._assert_helper_raises(helper_name, args, kwargs)


class HelperReturnsNoneOnLegitimateEmptyTests(unittest.TestCase):
    """Empty-but-functional PG substrate — helpers must return None
    (single-row) or [] (list) without raising. This pins the Stage 1
    contract that None / [] mean 'no row' only, not 'engine error'."""

    def test_single_row_helpers_return_none_when_empty(self):
        for helper_name, args, kwargs in _SINGLE_ROW_HELPERS:
            with self.subTest(helper=helper_name):
                with _EnvScope():
                    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                        pg_db = Path(tmp_dir) / "pg.db"
                        _enable_pg(f"sqlite:///{pg_db}")
                        import postgres_storage

                        engine = postgres_storage.get_engine()
                        postgres_storage.ensure_schema(engine)
                        result = getattr(postgres_storage, helper_name)(
                            *args, **kwargs,
                        )
                        self.assertIsNone(result)
                        postgres_storage.reset_engine_for_tests()

    def test_list_helpers_return_empty_list_when_empty(self):
        for helper_name, args, kwargs in _LIST_HELPERS:
            with self.subTest(helper=helper_name):
                with _EnvScope():
                    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                        pg_db = Path(tmp_dir) / "pg.db"
                        _enable_pg(f"sqlite:///{pg_db}")
                        import postgres_storage

                        engine = postgres_storage.get_engine()
                        postgres_storage.ensure_schema(engine)
                        result = getattr(postgres_storage, helper_name)(
                            *args, **kwargs,
                        )
                        self.assertEqual(result, [])
                        postgres_storage.reset_engine_for_tests()

    def test_exists_helper_returns_false_when_url_absent(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                self.assertEqual(
                    postgres_storage.read_analysis_result_exists_by_url(
                        "https://never-seeded.example",
                    ),
                    False,
                )
                postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part B — database.py / job_manager.py wrapper functions propagate.
#
# Mock the underlying postgres_storage helper with side_effect raising
# PostgresReadError; assert the database.py wrapper re-raises (any
# Exception subclass — the wrapper uses bare except Exception + raise).
# ---------------------------------------------------------------------------


# (database_fn_path, postgres_storage_helper_name, callable_args)
_DB_FN_CASES = (
    ("database.result_exists_by_url",
     "read_analysis_result_exists_by_url",
     ("https://example.com/x",)),
    ("database.get_result_id_by_url",
     "read_analysis_result_id_by_url",
     ("https://example.com/x",)),
    ("database.get_recent_results",
     "read_recent_analysis_results",
     ()),
    ("database.get_result_by_id",
     "read_analysis_result_by_id",
     (1,)),
    ("database.get_review_task_by_idempotency_key",
     "read_review_task_by_idempotency_key",
     ("idem",)),
    ("database.get_review_task",
     "read_review_task_by_task_id",
     ("t",)),
    ("database.list_review_tasks",
     "read_review_tasks",
     ()),
    ("database.get_review_decision",
     "read_review_decision_by_id",
     ("d",)),
    ("database.list_review_decisions",
     "read_review_decisions_for_task",
     ("t",)),
    ("database.get_fetch_artifacts",
     "read_fetch_artifacts",
     ()),
    ("database.get_extraction_results",
     "read_extraction_results",
     ()),
    ("database.get_evidence_candidates",
     "read_evidence_candidates",
     ()),
    ("database.get_producer_comparisons",
     "read_producer_comparisons",
     ()),
    ("database.get_verdict_label_attributions",
     "read_verdict_label_attributions",
     ()),
    ("job_manager.get_job_status",
     "read_job_by_id",
     ("j",)),
)


def _resolve_callable(dotted: str):
    module_name, attr = dotted.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, attr)


class WrapperPropagatesRaiseTests(unittest.TestCase):
    """Each of the 15 wrappers re-raises when the PG helper raises."""

    def test_wrappers_propagate_postgres_read_error(self):
        for fn_path, helper_name, call_args in _DB_FN_CASES:
            with self.subTest(fn=fn_path):
                with _EnvScope():
                    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                        pg_db = Path(tmp_dir) / "pg.db"
                        _enable_pg(f"sqlite:///{pg_db}")
                        import postgres_storage

                        # Some wrappers also need the SQLite schema in
                        # place (review tables). Set them up to keep
                        # the test pure to the raise behaviour.
                        engine = postgres_storage.get_engine()
                        postgres_storage.ensure_schema(engine)

                        def _boom(*a, **kw):
                            raise postgres_storage.PostgresReadError(
                                f"simulated {helper_name}",
                            )

                        wrapper = _resolve_callable(fn_path)
                        with patch.object(
                            postgres_storage, helper_name,
                            side_effect=_boom,
                        ):
                            with self.assertRaises(Exception) as cm:
                                wrapper(*call_args)
                            self.assertIsInstance(
                                cm.exception,
                                postgres_storage.PostgresReadError,
                            )
                        postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part C — log.error emission on PG failure.
#
# Confirms the new error log includes ``exc_info=True`` (so traceback is
# captured) and at least one identifying field. Uses caplog-equivalent
# via unittest.TestCase.assertLogs.
# ---------------------------------------------------------------------------


class LogErrorEmissionTests(unittest.TestCase):
    def test_get_result_by_id_emits_log_error_on_pg_failure(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import database
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                def _boom(*a, **kw):
                    raise postgres_storage.PostgresReadError("simulated")

                with patch.object(
                    postgres_storage, "read_analysis_result_by_id",
                    side_effect=_boom,
                ):
                    with self.assertLogs("database", level="ERROR") as cap:
                        with self.assertRaises(
                            postgres_storage.PostgresReadError,
                        ):
                            database.get_result_by_id(42)
                # At least one record at ERROR with traceback attached.
                error_records = [
                    r for r in cap.records if r.levelno == logging.ERROR
                ]
                self.assertTrue(error_records)
                rec = error_records[0]
                self.assertIn("PG read failed", rec.getMessage())
                # exc_info=True → exc_info attribute is a tuple, not None.
                self.assertIsNotNone(rec.exc_info)
                postgres_storage.reset_engine_for_tests()

    def test_get_job_status_emits_log_error_on_pg_failure(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import job_manager
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                def _boom(*a, **kw):
                    raise postgres_storage.PostgresReadError("simulated")

                with patch.object(
                    postgres_storage, "read_job_by_id",
                    side_effect=_boom,
                ):
                    with self.assertLogs(
                        "policy_ai.job_manager", level="ERROR",
                    ) as cap:
                        with self.assertRaises(
                            postgres_storage.PostgresReadError,
                        ):
                            job_manager.get_job_status("abc")
                error_records = [
                    r for r in cap.records if r.levelno == logging.ERROR
                ]
                self.assertTrue(error_records)
                self.assertIn(
                    "get_job_status PG read failed",
                    error_records[0].getMessage(),
                )
                self.assertIsNotNone(error_records[0].exc_info)
                postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part D — Import failure propagation.
#
# Simulate a broken postgres_storage module load. The wrapper's first
# try/except (around the import) must log + raise.
# ---------------------------------------------------------------------------


class ImportFailurePropagatesTests(unittest.TestCase):
    def test_get_result_by_id_raises_when_postgres_storage_import_fails(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                _enable_pg(f"sqlite:///{Path(tmp_dir) / 'pg.db'}")
                # Remove cached module so the next ``from
                # postgres_storage import ...`` triggers a fresh import.
                # Inject a broken stub that raises ImportError on import.
                import database

                # Clear any cached engine state first.
                if "postgres_storage" in sys.modules:
                    sys.modules.pop("postgres_storage")

                class _RaisingFinder:
                    def find_module(self, name, path=None):
                        if name == "postgres_storage":
                            return self
                        return None

                    def find_spec(self, name, path=None, target=None):
                        if name == "postgres_storage":
                            raise ImportError("simulated import failure")
                        return None

                    def load_module(self, name):
                        raise ImportError("simulated import failure")

                finder = _RaisingFinder()
                sys.meta_path.insert(0, finder)
                try:
                    with patch("database.DB_PATH", sqlite_db):
                        with self.assertRaises(ImportError):
                            database.get_result_by_id(1)
                finally:
                    sys.meta_path.remove(finder)
                    sys.modules.pop("postgres_storage", None)
                    # Re-import so subsequent tests see the real module.
                    import postgres_storage  # noqa: F401

                    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part E — Disabled dual-write keeps SQLite path silent.
#
# Sanity check that the SQLite fallback still runs when
# USE_POSTGRES_WRITE is unset / "false". This is the load-bearing
# invariant for local dev and CI without a Postgres substrate.
# ---------------------------------------------------------------------------


class DisabledDualWriteKeepsSQLitePathTests(unittest.TestCase):
    def test_get_result_by_id_uses_sqlite_when_disabled(self):
        from text_utils import sanitize_data

        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite.db"
                with patch("database.DB_PATH", sqlite_db):
                    import database

                    database.init_db()
                    sample = {
                        "title": "from sqlite (dual-write disabled)",
                        "original_url": "https://example.com/disabled",
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
                        sanitize_data(sample), query="disabled-path",
                    )
                    self.assertTrue(status["saved"])

                    row = database.get_result_by_id(status["id"])
                    self.assertIsNotNone(row)
                    self.assertEqual(
                        row["title"], "from sqlite (dual-write disabled)",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
