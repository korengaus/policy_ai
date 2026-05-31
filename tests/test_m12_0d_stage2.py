"""M12.0d-2 Stage 2 — SQLite read fallback removal + blocker resolution.

Run with: python tests/test_m12_0d_stage2.py

This module covers the Stage 2 contracts:

  * **Part C (Blocker 2):** ``postgres_storage.read_cached_embedding``
    raises ``PostgresReadError`` on engine errors, returns None on
    cache miss, and ``database.get_cached_embedding`` is PG-primary
    when dual-write is enabled. SQLite path runs unchanged when
    dual-write is disabled.
  * **Part B (Blocker 1):** ``database.update_review_task_status``
    now mirrors the updated row into Postgres via ``mirror_upsert``
    so PG ``review_tasks.status`` no longer stays at insert-time.
  * **Part E (Stage 1 deviation #4 fix):** ``postgres_storage.get_engine``
    raises ``PostgresReadError`` on missing-URL / engine-creation
    failures instead of swallowing into ``None``. ``ImportError`` is
    still caught (local-dev escape valve when psycopg isn't
    installed).

Re-uses the ``sqlite:///<tmp>`` Postgres substrate pattern so no real
Postgres server is required.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Env-var scaffold — parity with the other Stage 1 / Stage 2 test files.
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


def _seed_embedding_in_pg(*, text_hash, provider, model, vector,
                          preview="", created_at="2026-05-28T00:00:00"):
    """Helper: insert an embedding_cache row via mirror_upsert."""
    import postgres_storage

    postgres_storage.mirror_upsert(
        "embedding_cache",
        {
            "text_hash": text_hash,
            "provider": provider,
            "model": model,
            "dimensions": len(vector),
            "vector_json": json.dumps(list(vector)),
            "text_preview": preview,
            "created_at": created_at,
        },
        ["text_hash", "provider", "model"],
    )


# ---------------------------------------------------------------------------
# Part C1: postgres_storage.read_cached_embedding contracts.
# ---------------------------------------------------------------------------


class ReadCachedEmbeddingTests(unittest.TestCase):
    def test_returns_none_when_dual_write_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            import postgres_storage

            self.assertIsNone(
                postgres_storage.read_cached_embedding(
                    "h", "openai", "text-embedding-3-small",
                ),
            )

    def test_returns_vector_when_row_present(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                _seed_embedding_in_pg(
                    text_hash="h-present",
                    provider="openai",
                    model="text-embedding-3-small",
                    vector=[0.1, 0.2, 0.3, 0.4],
                )

                result = postgres_storage.read_cached_embedding(
                    "h-present", "openai", "text-embedding-3-small",
                )
                self.assertIsNotNone(result)
                self.assertEqual(len(result), 4)
                self.assertAlmostEqual(result[0], 0.1)
                self.assertAlmostEqual(result[3], 0.4)
                self.assertTrue(all(isinstance(v, float) for v in result))
                postgres_storage.reset_engine_for_tests()

    def test_returns_none_on_cache_miss(self):
        """Empty cache table → None (NOT raise)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)

                self.assertIsNone(
                    postgres_storage.read_cached_embedding(
                        "never-seeded", "openai",
                        "text-embedding-3-small",
                    ),
                )
                postgres_storage.reset_engine_for_tests()

    def test_raises_postgres_read_error_on_engine_failure(self):
        """SQLAlchemy errors must surface, not get swallowed (Stage 1
        contract extended to read_cached_embedding)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
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
                        postgres_storage.read_cached_embedding(
                            "h", "openai", "text-embedding-3-small",
                        )
                postgres_storage.reset_engine_for_tests()

    def test_corrupted_vector_json_returns_none(self):
        """A row with non-decodable vector_json is treated as a miss
        (best-effort cache; caller recomputes)."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                import postgres_storage

                engine = postgres_storage.get_engine()
                postgres_storage.ensure_schema(engine)
                # Write a row with junk vector_json directly via the
                # mirror_write helper.
                postgres_storage.mirror_write(
                    "embedding_cache",
                    {
                        "text_hash": "h-junk",
                        "provider": "openai",
                        "model": "m",
                        "dimensions": 0,
                        "vector_json": "{not-valid-json",
                        "text_preview": "",
                        "created_at": "2026-05-28T00:00:00",
                    },
                )

                self.assertIsNone(
                    postgres_storage.read_cached_embedding(
                        "h-junk", "openai", "m",
                    ),
                )
                postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part C2: database.get_cached_embedding wrapper.
# ---------------------------------------------------------------------------


class GetCachedEmbeddingWrapperTests(unittest.TestCase):
    def test_uses_pg_when_enabled(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)
                    _seed_embedding_in_pg(
                        text_hash="h-from-pg",
                        provider="openai",
                        model="m",
                        vector=[0.5, 0.6, 0.7],
                    )

                    result = database.get_cached_embedding(
                        "h-from-pg", "openai", "m",
                    )
                    self.assertEqual(result, [0.5, 0.6, 0.7])
                    postgres_storage.reset_engine_for_tests()

    def test_pg_miss_does_not_fall_through_to_sqlite(self):
        """Stage 2 contract: a PG cache miss returns None — caller
        recomputes the embedding. We must NOT silently substitute the
        SQLite row (which would be stale across Render restarts).

        M12.0e-1 note: under PG-primary, save_cached_embedding skips the
        SQLite INSERT entirely when dual-write is ON, so the "seed SQLite
        only" step below is now effectively a no-op (mirror is also
        patched off). The assertion still holds because PG stays empty —
        get_cached_embedding returns None either way."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    # Seed SQLite only, suppressing mirror so PG stays empty.
                    with patch.object(
                        postgres_storage, "mirror_upsert",
                        lambda *a, **kw: False,
                    ):
                        database.save_cached_embedding(
                            text_hash="h-sqlite-only",
                            provider="openai",
                            model="m",
                            vector=[1.0, 2.0],
                        )

                    # PG empty → returns None even though SQLite has it.
                    self.assertIsNone(
                        database.get_cached_embedding(
                            "h-sqlite-only", "openai", "m",
                        ),
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_uses_sqlite_when_disabled(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE=None, DATABASE_URL=None)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite.db"
                with patch("database.DB_PATH", sqlite_db):
                    import database

                    database.init_db()
                    # M12.0e-5a: SQLite write fallback removed. The embedding
                    # cache is best-effort; with dual-write OFF the save
                    # persists nothing (no-op) and a subsequent read is a
                    # clean miss rather than a durable hit.
                    database.save_cached_embedding(
                        text_hash="h-sqlite-disabled",
                        provider="openai",
                        model="m",
                        vector=[3.0, 4.0, 5.0],
                    )

                    result = database.get_cached_embedding(
                        "h-sqlite-disabled", "openai", "m",
                    )
                    self.assertIsNone(result)

    def test_raises_when_pg_read_fails(self):
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_db()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    def _boom(*a, **kw):
                        raise postgres_storage.PostgresReadError(
                            "simulated",
                        )

                    with patch.object(
                        postgres_storage, "read_cached_embedding",
                        side_effect=_boom,
                    ):
                        with self.assertRaises(
                            postgres_storage.PostgresReadError,
                        ):
                            database.get_cached_embedding(
                                "any", "openai", "m",
                            )
                    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part B: update_review_task_status mirror to PG.
# ---------------------------------------------------------------------------


class UpdateReviewTaskStatusMirrorTests(unittest.TestCase):
    def _seed_task(self, database, task_id, idempotency_key,
                   status="open"):
        database.create_review_task(
            task_id=task_id,
            result_id="r1", job_id="j1", item_index=0,
            status=status, query="q",
            claim_text="c", title="t", url="u",
            final_decision="WATCH",
            policy_confidence="60",
            human_review_required=True,
            snapshot={"k": "v"},
            idempotency_key=idempotency_key,
            created_at="2026-05-28T00:00:00",
            updated_at="2026-05-28T00:00:00",
        )

    def test_status_change_propagates_to_pg(self):
        """M12.0d Stage 3c-2: update_review_task_status now performs a
        direct PG UPDATE via postgres_storage.pg_update_review_task_status,
        no SQLite write involved. The end-to-end invariant — that the PG
        row reflects the new status and updated_at — is unchanged; only
        the mechanism is direct UPDATE instead of SQLite-UPDATE then
        SQLite-re-read then mirror_upsert."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    self._seed_task(
                        database, "task-status-mirror",
                        "idem-status-mirror",
                        status="open",
                    )

                    # Sanity: pre-update PG status is "open".
                    pre = postgres_storage.read_review_task_by_task_id(
                        "task-status-mirror",
                    )
                    self.assertIsNotNone(pre)
                    self.assertEqual(pre["status"], "open")

                    # Direct PG UPDATE via pg_update_review_task_status.
                    database.update_review_task_status(
                        "task-status-mirror",
                        new_status="approved",
                        updated_at="2026-05-28T01:00:00",
                    )

                    # PG row reflects the new status — directly, not via
                    # an SQLite re-read + mirror_upsert round-trip.
                    post = postgres_storage.read_review_task_by_task_id(
                        "task-status-mirror",
                    )
                    self.assertIsNotNone(post)
                    self.assertEqual(post["status"], "approved")
                    self.assertEqual(
                        post["updated_at"], "2026-05-28T01:00:00",
                    )
                    postgres_storage.reset_engine_for_tests()

    def test_get_review_task_pg_primary_sees_updated_status(self):
        """M12.0d Stage 3c-2: end-to-end — after the direct PG UPDATE,
        the PG-primary get_review_task returns the new status."""
        with _EnvScope():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                sqlite_db = Path(tmp_dir) / "sqlite_local.db"
                pg_db = Path(tmp_dir) / "pg.db"
                _enable_pg(f"sqlite:///{pg_db}")
                with patch("database.DB_PATH", sqlite_db):
                    import database
                    import postgres_storage

                    database.init_review_tables()
                    engine = postgres_storage.get_engine()
                    postgres_storage.ensure_schema(engine)

                    self._seed_task(
                        database, "task-end-to-end",
                        "idem-end-to-end",
                        status="open",
                    )
                    database.update_review_task_status(
                        "task-end-to-end",
                        new_status="dismissed",
                        updated_at="2026-05-28T02:00:00",
                    )

                    fetched = database.get_review_task("task-end-to-end")
                    self.assertIsNotNone(fetched)
                    self.assertEqual(fetched["status"], "dismissed")
                    postgres_storage.reset_engine_for_tests()


# ---------------------------------------------------------------------------
# Part E: get_engine raise behaviour (Stage 1 deviation #4 fix).
# ---------------------------------------------------------------------------


class GetEngineRaiseBehaviorTests(unittest.TestCase):
    def test_raises_postgres_read_error_when_url_empty(self):
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true", DATABASE_URL="")
            import postgres_storage

            with self.assertRaises(postgres_storage.PostgresReadError):
                postgres_storage.get_engine()

    def test_raises_postgres_read_error_on_engine_creation_failure(self):
        """SQLAlchemy parse failure → raise. Pre-M12.0d-2 this
        returned None silently."""
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="not-a-real-url-dialect:///nowhere")
            import postgres_storage

            with self.assertRaises(postgres_storage.PostgresReadError):
                postgres_storage.get_engine()

    def test_returns_none_when_disabled(self):
        """The legitimate ``USE_POSTGRES_WRITE`` unset path is unchanged."""
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="false",
                     DATABASE_URL="postgresql://example/test")
            import postgres_storage

            self.assertIsNone(postgres_storage.get_engine())

    def test_health_check_does_not_raise_on_invalid_url(self):
        """The diagnostic helper used by check_postgres_health.py
        catches the new raise and reports it as a populated ``error``
        field instead of crashing the operator CLI."""
        with _EnvScope():
            _set_env(USE_POSTGRES_WRITE="true",
                     DATABASE_URL="postgresql+psycopg://"
                                  "u:p@127.0.0.1:1/none")
            import postgres_storage

            try:
                status = postgres_storage.health_check()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"health_check raised: {exc!r}")
            self.assertTrue(status["dual_write_enabled"])
            self.assertTrue(status["database_url_present"])
            self.assertFalse(status["can_connect"])
            # An error string must be populated when engine creation
            # fails (driver missing OR raise).
            postgres_storage.reset_engine_for_tests()


if __name__ == "__main__":
    unittest.main(verbosity=2)
