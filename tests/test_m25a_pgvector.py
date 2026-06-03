"""M25a — pgvector storage infrastructure tests (mock-driven, NO live DB/OpenAI).

Proves the storage routing is gated and behavior-identical:
  * gate ON  → typed embedding_vectors preferred, JSON embedding_cache fallback,
               dual write.
  * gate OFF → embedding_cache (JSON) ONLY; vector store never touched
               (byte-identical to pre-M25a).
  * JSON-vs-typed parity (cosine identical).
  * CREATE EXTENSION permission failure degrades gracefully (no crash).
  * pgvector package absent → builder returns None → graceful no-op.

No real Postgres and no OpenAI key are required: the typed read/write functions
are monkeypatched with an in-memory store, and the schema hook is exercised with
a fake engine.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config  # noqa: E402
import database  # noqa: E402
import postgres_storage as ps  # noqa: E402
import semantic_similarity  # noqa: E402


_VEC = [0.1, -0.2, 0.3, 0.4, -0.5]


class RoutingTests(unittest.TestCase):
    """database.get/save_cached_embedding routing under the gate."""

    def test_roundtrip_via_embedding_vectors_when_enabled(self):
        store: dict = {}

        def fake_upsert(*, text_hash, provider, model, dimensions, embedding, text_preview="", created_at=""):
            store[(text_hash, provider, model)] = [float(v) for v in embedding]
            return True

        def fake_read_vector(text_hash, provider, model):
            return store.get((text_hash, provider, model))

        with patch.object(config, "pgvector_enabled", lambda: True), \
             patch.object(ps, "is_postgres_dual_write_enabled", lambda: True), \
             patch.object(ps, "upsert_embedding_vector", fake_upsert), \
             patch.object(ps, "read_cached_embedding_vector", fake_read_vector), \
             patch.object(ps, "read_cached_embedding", lambda *a, **k: None), \
             patch.object(database, "_mirror_upsert_safe", lambda *a, **k: True):
            ok = database.save_cached_embedding("h1", "openai", "m1", _VEC, "preview")
            self.assertTrue(ok)
            got = database.get_cached_embedding("h1", "openai", "m1")
        self.assertEqual(got, _VEC)

    def test_disabled_path_touches_only_embedding_cache(self):
        vec_read = MagicMock(return_value=None)
        vec_write = MagicMock(return_value=False)
        cache_write = MagicMock(return_value=True)
        sentinel = [9.0, 8.0]

        with patch.object(config, "pgvector_enabled", lambda: False), \
             patch.object(ps, "is_postgres_dual_write_enabled", lambda: True), \
             patch.object(ps, "read_cached_embedding_vector", vec_read), \
             patch.object(ps, "upsert_embedding_vector", vec_write), \
             patch.object(ps, "read_cached_embedding", lambda *a, **k: sentinel), \
             patch.object(database, "_mirror_upsert_safe", cache_write):
            database.save_cached_embedding("h2", "openai", "m1", _VEC, "p")
            got = database.get_cached_embedding("h2", "openai", "m1")

        # Vector store NEVER touched when the gate is off.
        vec_read.assert_not_called()
        vec_write.assert_not_called()
        # JSON cache used exactly as before.
        cache_write.assert_called_once()
        self.assertEqual(got, sentinel)

    def test_falls_back_to_json_cache_on_vector_miss(self):
        sentinel = [1.0, 2.0, 3.0]
        with patch.object(config, "pgvector_enabled", lambda: True), \
             patch.object(ps, "is_postgres_dual_write_enabled", lambda: True), \
             patch.object(ps, "read_cached_embedding_vector", lambda *a, **k: None), \
             patch.object(ps, "read_cached_embedding", lambda *a, **k: sentinel):
            got = database.get_cached_embedding("h3", "openai", "m1")
        self.assertEqual(got, sentinel)

    def test_dual_write_when_enabled(self):
        vec_write = MagicMock(return_value=True)
        cache_write = MagicMock(return_value=True)
        with patch.object(config, "pgvector_enabled", lambda: True), \
             patch.object(ps, "upsert_embedding_vector", vec_write), \
             patch.object(database, "_mirror_upsert_safe", cache_write):
            database.save_cached_embedding("h4", "openai", "m1", _VEC, "p")
        cache_write.assert_called_once()        # JSON cache (durable fallback)
        vec_write.assert_called_once()          # typed store (additive)


class ParityTests(unittest.TestCase):
    def test_json_roundtrip_cosine_identical(self):
        roundtripped = json.loads(json.dumps(_VEC))
        score = semantic_similarity.cosine_similarity(_VEC, roundtripped)
        self.assertAlmostEqual(score, 1.0, places=12)


class GateGuardTests(unittest.TestCase):
    """The typed helpers no-op (no engine needed) when the gate is off."""

    def test_read_vector_returns_none_when_disabled(self):
        with patch.object(config, "pgvector_enabled", lambda: False):
            self.assertIsNone(ps.read_cached_embedding_vector("h", "openai", "m"))

    def test_upsert_vector_returns_false_when_disabled(self):
        with patch.object(config, "pgvector_enabled", lambda: False):
            self.assertFalse(ps.upsert_embedding_vector(
                text_hash="h", provider="openai", model="m",
                dimensions=5, embedding=_VEC))


class ExtensionHookTests(unittest.TestCase):
    def setUp(self):
        # Reset the lazily-built table cache between tests.
        ps._embedding_vectors_table = None

    def tearDown(self):
        ps._embedding_vectors_table = None

    def test_pgvector_package_absent_returns_false_gracefully(self):
        with patch.object(ps, "_Vector", None):
            ps._embedding_vectors_table = None
            self.assertIsNone(ps._build_embedding_vectors_table())
            self.assertFalse(ps._ensure_pgvector(MagicMock()))  # no raise

    def test_create_extension_permission_failure_is_graceful(self):
        # Builder returns a table (patched), but the engine raises on the
        # CREATE EXTENSION — _ensure_pgvector must catch it and return False.
        fake_engine = MagicMock()
        fake_engine.begin.side_effect = Exception("permission denied for CREATE EXTENSION")
        with patch.object(ps, "_build_embedding_vectors_table", lambda: MagicMock()):
            result = ps._ensure_pgvector(fake_engine)  # must not raise
        self.assertFalse(result)

    def test_none_engine_returns_false(self):
        self.assertFalse(ps._ensure_pgvector(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
