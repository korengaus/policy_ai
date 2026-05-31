"""M12.0e sub-stage 0e-2 — PG-schema-creation invariant pin (TEST ONLY).

Run with: python tests/test_m12_0e_pg_schema_startup_invariant.py

Context
-------
The Postgres mirror schema is created via the lazy
``postgres_storage.get_engine() -> ensure_schema`` path
(``postgres_storage.py:173-175``): the first successful engine build
runs ``ensure_schema(_engine)`` once (guarded by ``_schema_ensured``)
*before returning the engine*. Because every read AND write helper
obtains its engine through ``get_engine()``, the mirror tables are
guaranteed to exist before the first SELECT or INSERT on a fresh
database — schema creation is bound to **engine construction**, not to
a write. This is independent of ``database.init_db()``, which is pure
SQLite (``database.py:114-148``) and never touches Postgres.

This module locks that invariant so a future regression that removes
the lazy ``ensure_schema`` call (or otherwise recouples PG schema
creation to ``init_db``) fails loudly — BEFORE a later sub-stage
removes / no-ops ``init_db``. It changes NO production behaviour.

Re-uses the ``sqlite:///<tmp>`` Postgres-substitute pattern (and the
``reset_engine_for_tests`` helper) from ``tests/test_m12_0d_stage2.py``
and ``tests/test_postgres_storage.py`` so no real Postgres server is
required.
"""

from __future__ import annotations

import os
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
# Env-var scaffold — parity with the M12.0d Stage 1 / Stage 2 test files.
# Snapshots + restores the two PG env vars and resets the cached engine /
# the one-shot ``_schema_ensured`` guard on exit so tests stay isolated.
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
# Invariant 1 — schema is bound to engine construction.
#
# Calling get_engine() (and nothing else — no explicit ensure_schema, no
# prior write, no init_db) must leave the mirror tables queryable. This
# proves ensure_schema ran as a side-effect of the engine build.
# ---------------------------------------------------------------------------


class SchemaBoundToEngineBuildTests(unittest.TestCase):
    def test_get_engine_alone_creates_mirror_tables(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                pg_db = Path(tmp_dir) / "fresh_pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                # Start from a clean module state so the engine is built
                # fresh inside this scope (mirrors how a process starts).
                postgres_storage.reset_engine_for_tests()

                # The ONLY call that touches PG: build the engine. We do
                # NOT call ensure_schema explicitly and we do NOT write.
                engine = postgres_storage.get_engine()
                self.assertIsNotNone(
                    engine,
                    "dual-write is ON, so get_engine() must build an engine",
                )

                # Representative INT-PK table (embedding_cache, PG-primary
                # since 0e-1) and TEXT-PK table (review_tasks) must both be
                # queryable with zero prior writes — i.e. ensure_schema ran
                # as a side-effect of the engine build.
                for table_name in ("embedding_cache", "review_tasks"):
                    with self.subTest(table=table_name):
                        with engine.connect() as conn:
                            count = conn.execute(
                                sa.text(
                                    f"SELECT COUNT(*) FROM {table_name}"
                                )
                            ).scalar()
                        self.assertEqual(
                            count,
                            0,
                            f"{table_name} must exist and be empty on a "
                            f"fresh DB after get_engine() alone",
                        )

    def test_every_mirror_table_exists_after_engine_build(self):
        # Stronger form of the invariant: ALL registered mirror tables —
        # not just the two representatives — are created by the engine
        # build alone. Guards against a partial create_all regression.
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                pg_db = Path(tmp_dir) / "fresh_pg_all.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                postgres_storage.reset_engine_for_tests()
                engine = postgres_storage.get_engine()
                self.assertIsNotNone(engine)

                self.assertTrue(
                    postgres_storage.MIRROR_TABLE_NAMES,
                    "expected a non-empty mirror-table registry",
                )
                for table_name in postgres_storage.MIRROR_TABLE_NAMES:
                    with self.subTest(table=table_name):
                        with engine.connect() as conn:
                            count = conn.execute(
                                sa.text(
                                    f"SELECT COUNT(*) FROM {table_name}"
                                )
                            ).scalar()
                        self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# Invariant 2 — PG schema creation is independent of database.init_db().
#
# init_db() is pure SQLite and is never on the PG-schema-creation path.
# Patch it with a mock, build the engine, confirm the mirror tables are
# queryable, and assert init_db was never invoked. This is the precise
# pin that protects a later sub-stage's removal of init_db: removing it
# must not affect PG schema creation.
# ---------------------------------------------------------------------------


class SchemaIndependentOfInitDbTests(unittest.TestCase):
    def test_get_engine_does_not_call_init_db(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                pg_db = Path(tmp_dir) / "no_init_db_pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import database
                import postgres_storage

                postgres_storage.reset_engine_for_tests()

                with patch.object(database, "init_db") as init_db_mock:
                    engine = postgres_storage.get_engine()
                    self.assertIsNotNone(engine)

                    # Mirror tables are usable even though init_db was
                    # never called.
                    with engine.connect() as conn:
                        count = conn.execute(
                            sa.text("SELECT COUNT(*) FROM embedding_cache")
                        ).scalar()
                    self.assertEqual(count, 0)

                init_db_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Invariant 3 — dual-write OFF is a safe no-op.
#
# With the flag off, get_engine() returns None and ensure_schema(None)
# returns False without raising. Confirms an explicit-or-lazy schema
# call is harmless in the SQLite-only configuration.
# ---------------------------------------------------------------------------


class DualWriteOffSafetyTests(unittest.TestCase):
    def test_engine_none_and_ensure_schema_noop_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            postgres_storage.reset_engine_for_tests()

            self.assertIsNone(
                postgres_storage.get_engine(),
                "dual-write OFF must yield no engine",
            )
            # ensure_schema(None) must be a safe no-op returning False and
            # must never raise.
            self.assertFalse(postgres_storage.ensure_schema(None))


if __name__ == "__main__":
    unittest.main()
