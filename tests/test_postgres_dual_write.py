"""Feature-flag tests for the db.postgres connection helpers.

Historically this module exercised the ``postgres_dual_write`` audit_log
INSERT path. That dual-write was removed in M12.0d Stage 3a (zero readers
and the table never existed in production); the canonical dual-write now
flows through :mod:`postgres_storage`. The remaining tests cover the
small surface that survives in ``db.postgres``:

    * ``is_postgres_enabled`` / ``is_dual_write_enabled`` feature-flag
      gating off ``DATABASE_URL`` + ``USE_POSTGRES_WRITE``.
    * ``reset_state_for_tests`` for env-toggle test scaffolding.

Run with: python tests/test_postgres_dual_write.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import postgres as pg


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


if __name__ == "__main__":
    unittest.main()
