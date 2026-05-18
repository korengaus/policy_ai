"""Tests for Phase 2 M1 Postgres dual-write plumbing.

Run with: python tests/test_postgres_dual_write.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import postgres as pg


SAMPLE_RESULT = {
    "title": "테스트 정책 뉴스",
    "original_url": "https://example.com/test-dual-write",
    "topic": "금융/정책",
    "claim_text": "테스트 주장",
    "verdict_label": "draft_likely_true",
    "verdict_confidence": 80,
    "ai_model": "gpt-4o-mini",
    "verification_card": {
        "claim_text": "테스트 주장",
        "verdict_label": "draft_likely_true",
        "verdict_confidence": 80,
        "last_checked_at": "2026-05-19T00:00:00+00:00",
    },
    "final_decision": {
        "policy_alert_level": "WATCH",
    },
    "normalized_claims": [{"normalized": "정상화된 테스트 주장"}],
}


class _EnvScope:
    """Context manager that snapshots and restores selected env vars."""

    KEYS = ("DATABASE_URL", "USE_POSTGRES_WRITE")

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        pg.reset_state_for_tests()


class FeatureFlagTests(unittest.TestCase):
    def test_disabled_when_database_url_missing(self):
        with _EnvScope():
            os.environ.pop("DATABASE_URL", None)
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()
            self.assertFalse(pg.is_postgres_enabled())
            self.assertFalse(pg.is_dual_write_enabled())

    def test_disabled_when_flag_false(self):
        with _EnvScope():
            os.environ["DATABASE_URL"] = "postgresql://example/test"
            os.environ["USE_POSTGRES_WRITE"] = "false"
            pg.reset_state_for_tests()
            self.assertTrue(pg.is_postgres_enabled())
            self.assertFalse(pg.is_dual_write_enabled())

    def test_enabled_when_both_present(self):
        with _EnvScope():
            os.environ["DATABASE_URL"] = "postgresql://example/test"
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()
            self.assertTrue(pg.is_dual_write_enabled())

    def test_dual_write_skipped_without_database_url(self):
        with _EnvScope():
            os.environ.pop("DATABASE_URL", None)
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()
            status = pg.postgres_dual_write(SAMPLE_RESULT, query="test")
            self.assertFalse(status["attempted"])
            self.assertFalse(status["ok"])
            self.assertIn("DATABASE_URL", status["skipped_reason"] or "")

    def test_dual_write_skipped_when_flag_false(self):
        with _EnvScope():
            os.environ["DATABASE_URL"] = "postgresql://example/test"
            os.environ["USE_POSTGRES_WRITE"] = "false"
            pg.reset_state_for_tests()
            status = pg.postgres_dual_write(SAMPLE_RESULT, query="test")
            self.assertFalse(status["attempted"])
            self.assertFalse(status["ok"])


class DualWriteFailureIsolationTests(unittest.TestCase):
    def test_failure_does_not_raise_to_caller(self):
        """Even if Postgres explodes, dual-write must return a status dict."""
        with _EnvScope():
            os.environ["DATABASE_URL"] = "postgresql://invalid:invalid@127.0.0.1:1/none"
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()

            with patch.object(pg, "get_session", return_value=None):
                status = pg.postgres_dual_write(SAMPLE_RESULT, query="test")
            self.assertTrue(status["attempted"])
            self.assertFalse(status["ok"])
            self.assertIsNotNone(status["error"])

    def test_session_exception_is_swallowed(self):
        """Session-level exceptions during dual-write must not propagate."""
        with _EnvScope():
            os.environ["DATABASE_URL"] = "postgresql://example/test"
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()

            class FakeSession:
                def execute(self, *args, **kwargs):
                    raise RuntimeError("simulated postgres failure")

                def rollback(self):
                    pass

                def commit(self):
                    pass

                def close(self):
                    pass

            with patch.object(pg, "get_session", return_value=FakeSession()):
                status = pg.postgres_dual_write(SAMPLE_RESULT, query="test")
            self.assertTrue(status["attempted"])
            self.assertFalse(status["ok"])
            self.assertIn("simulated postgres failure", status["error"])


class SqliteSavePathIntegrationTests(unittest.TestCase):
    """Confirm SQLite save path is unaffected by Postgres dual-write failures."""

    def test_sqlite_save_still_works_without_database_url(self):
        from text_utils import sanitize_data

        with _EnvScope():
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("USE_POSTGRES_WRITE", None)
            pg.reset_state_for_tests()

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "sqlite_test.db"
                with patch("database.DB_PATH", tmp_db):
                    import database

                    database.init_db()
                    save_status = database.save_analysis_result(
                        sanitize_data(SAMPLE_RESULT),
                        query="dual-write-test",
                    )
                    self.assertTrue(save_status["saved"])

                    # Dual-write should silently skip since DATABASE_URL is missing.
                    pg_status = pg.postgres_dual_write(SAMPLE_RESULT, query="dual-write-test")
                    self.assertFalse(pg_status["attempted"])
                    self.assertFalse(pg_status["ok"])

                    rows = database.get_recent_results(limit=5)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["original_url"], SAMPLE_RESULT["original_url"])

    def test_sqlite_save_unaffected_by_pg_exception(self):
        """If Postgres throws, SQLite row must still be intact."""
        from text_utils import sanitize_data

        with _EnvScope():
            os.environ["DATABASE_URL"] = "postgresql://example/test"
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                tmp_db = Path(tmp_dir) / "sqlite_test.db"
                with patch("database.DB_PATH", tmp_db):
                    import database

                    database.init_db()
                    save_status = database.save_analysis_result(
                        sanitize_data(SAMPLE_RESULT),
                        query="dual-write-test",
                    )
                    self.assertTrue(save_status["saved"])

                    # Simulate Postgres failure after the SQLite save.
                    with patch.object(pg, "get_session", side_effect=RuntimeError("boom")):
                        try:
                            pg_status = pg.postgres_dual_write(
                                SAMPLE_RESULT, query="dual-write-test"
                            )
                        except Exception as error:
                            self.fail(
                                f"postgres_dual_write must not raise: {error}"
                            )
                    self.assertTrue(pg_status["attempted"])
                    self.assertFalse(pg_status["ok"])

                    # SQLite remains source of truth and is unaffected.
                    rows = database.get_recent_results(limit=5)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["original_url"], SAMPLE_RESULT["original_url"])


if __name__ == "__main__":
    unittest.main()
