"""Phase 2 M5: semantic evidence matching test suite.

Strict CI-safety contract:
    * No OpenAI key required.
    * No network calls.
    * No Postgres required (uses a temp SQLite file for cache tests).
    * Disabled-by-default behavior is verified explicitly.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

# Allow `python tests/test_semantic_matching.py` to import the project modules
# the same way the other tests do.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import database
import semantic_chunker
import semantic_embeddings
import semantic_evidence_agent
import semantic_similarity


@contextmanager
def _env(**overrides: str):
    """Temporarily set environment variables; restores prior state."""
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _temporary_sqlite_db():
    """Point ``database.DB_PATH`` at a fresh sqlite file and init it.

    Cleanup is best-effort: on Windows, sqlite3 connections sometimes hold a
    file handle open until garbage collection, which makes ``unlink`` raise
    ``PermissionError``. The tempdir is cleared on next boot anyway.
    """
    import gc

    fd, raw_path = tempfile.mkstemp(suffix=".db", prefix="semantic_test_")
    os.close(fd)
    new_path = Path(raw_path)
    original = database.DB_PATH
    database.DB_PATH = new_path
    try:
        database.init_db()
        yield new_path
    finally:
        database.DB_PATH = original
        # Encourage any lingering sqlite3.Connection objects to be finalized
        # before we attempt to delete the file (helps on Windows).
        gc.collect()
        try:
            new_path.unlink()
        except (FileNotFoundError, PermissionError, OSError):
            pass


class DisabledProviderTests(unittest.TestCase):
    def test_disabled_provider_returns_unavailable(self):
        with _env(SEMANTIC_MATCHING_ENABLED=None, EMBEDDING_PROVIDER=None):
            provider = semantic_embeddings.get_active_provider()
            status = provider.provider_status()
            self.assertEqual(status["provider"], "disabled")
            self.assertFalse(status["available"])
            self.assertIsNone(provider.get_embedding("test"))
            self.assertEqual(provider.get_embeddings(["a", "b"]), [None, None])

    def test_pipeline_summary_when_disabled_is_safe(self):
        with _env(SEMANTIC_MATCHING_ENABLED=None):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "공식 발표"}],
                source_candidates=[{
                    "official_body_text": "공식 발표 본문",
                    "url": "https://www.fsc.go.kr/example",
                    "title": "공식 자료",
                }],
                evidence_snippets=[],
            )
            self.assertFalse(summary["semantic_matching_enabled"])
            self.assertFalse(summary["semantic_matching_available"])
            self.assertEqual(summary["best_support_level"], "unavailable")
            self.assertEqual(summary["claim_matches"], [])
            self.assertIn("disabled", " ".join(summary["limitations"]))


class DeterministicProviderTests(unittest.TestCase):
    def test_same_text_produces_same_vector(self):
        provider = semantic_embeddings.DeterministicHashEmbeddingProvider()
        vec1 = provider.get_embedding("금융위원회 공식 발표")
        vec2 = provider.get_embedding("금융위원회 공식 발표")
        self.assertIsNotNone(vec1)
        self.assertEqual(vec1, vec2)
        self.assertEqual(len(vec1), provider.dimensions)

    def test_empty_text_returns_none(self):
        provider = semantic_embeddings.DeterministicHashEmbeddingProvider()
        self.assertIsNone(provider.get_embedding(""))
        self.assertIsNone(provider.get_embedding("   "))
        self.assertIsNone(provider.get_embedding(None))  # type: ignore[arg-type]

    def test_cosine_similarity_of_identical_vectors_is_one(self):
        provider = semantic_embeddings.DeterministicHashEmbeddingProvider()
        vec = provider.get_embedding("동일한 텍스트")
        self.assertAlmostEqual(semantic_similarity.cosine_similarity(vec, vec), 1.0, places=5)

    def test_score_to_percent_bounds(self):
        self.assertEqual(semantic_similarity.score_to_percent(1.0), 100)
        self.assertEqual(semantic_similarity.score_to_percent(0.0), 50)
        self.assertEqual(semantic_similarity.score_to_percent(-1.0), 0)
        self.assertEqual(semantic_similarity.score_to_percent("bad"), 0)


class CacheTests(unittest.TestCase):
    def test_cache_round_trip(self):
        with _temporary_sqlite_db():
            text_hash = semantic_embeddings.hash_text_for_cache("hello")
            self.assertIsNone(database.get_cached_embedding(text_hash, "p", "m"))
            saved = database.save_cached_embedding(
                text_hash=text_hash,
                provider="p",
                model="m",
                vector=[0.1, 0.2, 0.3],
                text_preview="hello",
            )
            self.assertTrue(saved)
            cached = database.get_cached_embedding(text_hash, "p", "m")
            self.assertEqual(cached, [0.1, 0.2, 0.3])

    def test_cache_ignores_bad_vectors(self):
        with _temporary_sqlite_db():
            self.assertFalse(database.save_cached_embedding("h", "p", "m", []))
            self.assertFalse(database.save_cached_embedding("h", "p", "m", "not a list"))  # type: ignore[arg-type]
            self.assertFalse(database.save_cached_embedding("", "p", "m", [0.1]))

    def test_cache_hit_path_via_similarity(self):
        with _temporary_sqlite_db(), _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="deterministic",
            EMBEDDING_CACHE_ENABLED="true",
        ):
            provider = semantic_embeddings.get_active_provider()
            chunks = semantic_chunker.chunk_text_for_semantic_matching(
                "전세사기 피해 지원 공식 발표 본문",
                max_chunks=4,
            )
            first = semantic_similarity.rank_semantic_matches("전세사기 피해 지원", chunks, provider)
            # Second call should hit the cache for both claim and chunks.
            second = semantic_similarity.rank_semantic_matches("전세사기 피해 지원", chunks, provider)
            self.assertGreaterEqual(second["cache_hits"], first["cache_hits"])
            self.assertGreater(second["cache_hits"], 0)


class ChunkingTests(unittest.TestCase):
    def test_korean_text_splits_into_sentences(self):
        text = (
            "금융위원회는 전세사기 피해자 지원 방안을 발표했다. "
            "이번 발표에는 대출 만기 연장과 이자 감면이 포함된다. "
            "구체적인 신청 절차는 추후 공지할 예정이다."
        )
        chunks = semantic_chunker.chunk_text_for_semantic_matching(text, max_chunks=10)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertTrue(chunk["text"].strip())
            self.assertGreaterEqual(chunk["char_end"], chunk["char_start"])
            self.assertIn("chunk_id", chunk)

    def test_long_text_is_capped(self):
        text = "공식 발표. " * 5000  # absurdly long
        chunks = semantic_chunker.chunk_text_for_semantic_matching(
            text, max_chunks=5, max_chars_per_chunk=120
        )
        self.assertEqual(len(chunks), 5)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"]), 120)

    def test_bad_input_does_not_throw(self):
        self.assertEqual(semantic_chunker.chunk_text_for_semantic_matching(None), [])
        self.assertEqual(semantic_chunker.chunk_text_for_semantic_matching(""), [])
        self.assertEqual(semantic_chunker.chunk_text_for_semantic_matching("   "), [])
        # Negative caps coerce to safe minimums rather than crashing.
        self.assertEqual(
            semantic_chunker.chunk_text_for_semantic_matching("text", max_chunks=-5),
            semantic_chunker.chunk_text_for_semantic_matching("text", max_chunks=1),
        )


class RankingTests(unittest.TestCase):
    def test_related_chunk_outranks_unrelated_chunk(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            chunks = [
                {
                    "chunk_id": "related",
                    "text": "금융위원회 전세사기 피해 지원 공식 발표 본문",
                    "char_start": 0,
                    "char_end": 30,
                    "source": "official_body_text",
                    "source_id": "src1",
                },
                {
                    "chunk_id": "unrelated",
                    "text": "오늘 점심 메뉴는 비빔밥과 김치찌개입니다.",
                    "char_start": 0,
                    "char_end": 30,
                    "source": "official_body_text",
                    "source_id": "src2",
                },
            ]
            ranked = semantic_similarity.rank_semantic_matches(
                "금융위원회 전세사기 피해 지원 발표", chunks, provider, cache_enabled=False
            )
            self.assertTrue(ranked["available"])
            self.assertEqual(ranked["matches"][0]["chunk_id"], "related")
            self.assertGreater(
                ranked["matches"][0]["score"],
                ranked["matches"][1]["score"],
            )

    def test_top_matches_are_sorted_descending(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            chunks = [
                {"chunk_id": str(i), "text": f"문장 {i} 전세사기", "char_start": 0, "char_end": 0,
                 "source": "official_body_text", "source_id": "s"}
                for i in range(6)
            ]
            ranked = semantic_similarity.rank_semantic_matches(
                "전세사기 피해 지원", chunks, provider, cache_enabled=False
            )
            scores = [m["score"] for m in ranked["matches"]]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_chunks_marks_available_but_no_support(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            ranked = semantic_similarity.rank_semantic_matches(
                "공식 발표", [], provider, cache_enabled=False
            )
            self.assertTrue(ranked["available"])
            self.assertEqual(ranked["support_level"], "unavailable")
            self.assertEqual(ranked["matches"], [])


class IntegrationTests(unittest.TestCase):
    def test_summary_appears_in_pipeline_output_when_enabled(self):
        """When semantic matching is on with deterministic provider, the
        agent produces a usable summary; when off, it returns unavailable.
        Neither variant changes the verdict caller path — that is verified by
        the existing regression suite which still passes alongside this one.
        """
        with _temporary_sqlite_db(), _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="deterministic",
            EMBEDDING_CACHE_ENABLED="true",
        ):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[
                    {"claim_text": "금융위원회 전세사기 피해 지원 발표"},
                    {"claim_text": "주거지원 강화 계획"},
                ],
                source_candidates=[{
                    "official_body_text": (
                        "금융위원회는 전세사기 피해자 지원 방안을 발표했다. "
                        "대출 만기 연장과 이자 감면이 핵심 내용이다."
                    ),
                    "url": "https://www.fsc.go.kr/example",
                    "title": "공식 발표",
                }],
                evidence_snippets=[{
                    "evidence_text": "정부는 추가 주거지원 방안을 마련했다.",
                    "source_url": "https://news.example.com/a",
                    "source_title": "주거지원 보도",
                }],
            )
            self.assertTrue(summary["semantic_matching_enabled"])
            self.assertTrue(summary["semantic_matching_available"])
            self.assertEqual(summary["claim_count"], 2)
            self.assertGreater(summary["chunk_count"], 0)
            self.assertGreater(summary["best_overall_score"], 0.0)
            self.assertIn(summary["best_support_level"], {"strong", "contextual", "weak"})
            # Claim matches preserve ordering and carry top matches.
            self.assertEqual(len(summary["claim_matches"]), 2)
            for claim in summary["claim_matches"]:
                self.assertIn("top_matches", claim)
                self.assertIn(claim["support_level"], {"strong", "contextual", "weak", "unavailable"})
            # The conservative disclaimer must always appear.
            self.assertTrue(any("authoritative" in line for line in summary["limitations"]))

    def test_no_official_body_text_marks_unavailable(self):
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "어떤 주장"}],
                source_candidates=[{"url": "https://example.com", "title": "no body"}],
                evidence_snippets=[],
            )
            self.assertTrue(summary["semantic_matching_enabled"])
            self.assertTrue(summary["semantic_matching_available"])
            self.assertEqual(summary["best_support_level"], "unavailable")
            self.assertTrue(any("no official body text" in line for line in summary["limitations"]))

    def test_high_semantic_score_does_not_imply_verified(self):
        """The semantic summary's support_level lives in a separate namespace
        from verification_strength/verdict_label. Asserting structurally:
        nothing in the summary names a verdict, only semantic-match terms.
        """
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "공식 발표"}],
                source_candidates=[{
                    "official_body_text": "공식 발표 공식 발표 공식 발표",
                    "url": "https://www.fsc.go.kr/x",
                    "title": "공식",
                }],
                evidence_snippets=[],
            )
            self.assertNotIn("verdict", summary)
            self.assertNotIn("verified", str(summary).lower())
            self.assertIn("metadata only", " ".join(summary["limitations"]))

    def test_openai_provider_without_key_falls_back_safely(self):
        """If someone flips the OpenAI provider on without an API key, the
        provider must report unavailable instead of crashing. The agent must
        then produce a disabled-style summary.
        """
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            OPENAI_API_KEY=None,
        ):
            provider = semantic_embeddings.get_active_provider()
            self.assertEqual(provider.name, "openai")
            self.assertFalse(provider.available)
            self.assertIsNotNone(provider.error)
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": "공식 발표"}],
                source_candidates=[{"official_body_text": "본문", "url": "x", "title": "y"}],
                evidence_snippets=[],
                provider=provider,
            )
            self.assertFalse(summary["semantic_matching_available"])
            self.assertEqual(summary["best_support_level"], "unavailable")


class CISafetyTests(unittest.TestCase):
    def test_no_openai_module_calls_at_import(self):
        """Importing the M5 modules must not instantiate any client. We
        already imported them at the top of this file — if a real client
        had been created we'd have to scrub it. Just verify the provider
        constructor isn't running on import.
        """
        # If the OpenAI SDK happened to be installed, ensure that simply
        # importing our module did NOT call it. We can verify by mocking the
        # constructor and re-running ``get_active_provider`` in the disabled
        # default state.
        with _env(SEMANTIC_MATCHING_ENABLED=None):
            with mock.patch("semantic_embeddings.OpenAIEmbeddingProvider") as fake:
                provider = semantic_embeddings.get_active_provider()
                fake.assert_not_called()
                self.assertEqual(provider.name, "disabled")

    def test_provider_status_serializable(self):
        """provider_status must be JSON-safe so it can be embedded in
        debug_summary and persisted to SQLite."""
        import json

        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            json.dumps(provider.provider_status())


if __name__ == "__main__":
    unittest.main()
