"""Embedding provider abstraction for Phase 2 M5 semantic evidence matching.

Design contract:
    * Embedding calls NEVER happen at import time.
    * If the provider is disabled (default) or unavailable, callers receive
      ``None`` and a clear status dict; the pipeline must keep running.
    * Tests rely on the deterministic provider so they never need the network
      or an OpenAI key.

The provider here is intentionally not coupled to any vector database. It
exposes a tiny surface (``get_embedding``, ``get_embeddings``,
``provider_status``) so a future pgvector/Qdrant migration can replace the
storage layer without touching pipeline code.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from typing import Iterable, List, Optional, Sequence

import config


logger = logging.getLogger(__name__)


# Stable identifier for the deterministic provider's "model"; lets the cache
# distinguish hashed vectors from real-embedding vectors should real ones be
# stored later.
_DETERMINISTIC_MODEL = "deterministic-hash-v1"
_DETERMINISTIC_DIMENSIONS = 64


class EmbeddingProvider:
    """Base interface. Subclasses implement ``get_embedding`` and ``name``."""

    name = "base"
    model = ""
    dimensions = 0
    available = False
    error: Optional[str] = None

    def get_embedding(self, text: str) -> Optional[List[float]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_embeddings(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        return [self.get_embedding(text) for text in texts]

    def provider_status(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "dimensions": self.dimensions,
            "available": self.available,
            "error": self.error,
        }


class DisabledEmbeddingProvider(EmbeddingProvider):
    """Returned when SEMANTIC_MATCHING_ENABLED is false or provider=disabled.

    All methods are pure no-ops so callers never need to special-case the
    disabled state — they just observe ``available=False`` and skip ranking.
    """

    name = "disabled"

    def __init__(self, reason: str = "semantic matching disabled") -> None:
        self.error = reason

    def get_embedding(self, text: str) -> Optional[List[float]]:
        return None

    def get_embeddings(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        return [None for _ in texts]


class DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Stable vectors derived from token hashes — no network, no secrets.

    Used for tests and local development so the rest of the semantic stack
    (chunker, similarity, agent) can be exercised end-to-end without an
    external API. Produces vectors that capture rough lexical overlap: texts
    that share tokens score noticeably higher than unrelated texts, while
    stable identity (same text → same vector) is preserved.

    This is NOT a substitute for real embeddings in production — it's a test
    surrogate. The conservative verdict rules treat its scores the same way
    as real ones (i.e. semantic alone never upgrades a claim).
    """

    name = "deterministic-hash"
    model = _DETERMINISTIC_MODEL
    dimensions = _DETERMINISTIC_DIMENSIONS
    available = True

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        if not text:
            return []
        # Korean text often lacks whitespace word boundaries, so we mix
        # whitespace splits with character-bigrams to give the deterministic
        # vectors enough overlap signal for paraphrased-but-related text.
        normalized = text.strip().lower()
        tokens: List[str] = [tok for tok in normalized.split() if tok]
        if not normalized:
            return tokens
        bigrams = [normalized[i : i + 2] for i in range(len(normalized) - 1)]
        tokens.extend(bg for bg in bigrams if bg.strip())
        return tokens

    def get_embedding(self, text: str) -> Optional[List[float]]:
        if not isinstance(text, str) or not text.strip():
            # Returning a zero vector would make cosine_similarity div-by-zero;
            # the caller (rank_semantic_matches) skips None entries.
            return None
        vector = [0.0] * self.dimensions
        for token in self._tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            for i in range(self.dimensions):
                # Map each byte to {-1, +1} so similar tokens reinforce each
                # other in the same dimensions across documents.
                byte = digest[i % len(digest)]
                vector[i] += 1.0 if (byte & (1 << (i % 8))) else -1.0
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0:
            return None
        return [component / norm for component in vector]


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Optional real-embedding provider. Constructed lazily and only when
    SEMANTIC_MATCHING_ENABLED=true and EMBEDDING_PROVIDER=openai.

    Initialization never raises — if the OpenAI SDK isn't importable, or the
    API key is missing, the provider returns ``available=False`` and the
    caller falls back to disabled behavior. This guarantees the app starts
    even with the flag flipped on but the environment incomplete.
    """

    name = "openai"

    def __init__(self) -> None:
        self.model = config.embedding_model() or "text-embedding-3-small"
        self._client = None
        self.error = None
        try:
            from openai import OpenAI  # local import keeps app importable without the SDK
        except Exception as import_error:  # pragma: no cover - depends on env
            self.available = False
            self.error = f"openai sdk import failed: {import_error}"
            logger.warning("OpenAIEmbeddingProvider unavailable: %s", self.error)
            return
        api_key = os.getenv("OPENAI_API_KEY") or ""
        if not api_key:
            self.available = False
            self.error = "OPENAI_API_KEY missing"
            return
        try:
            self._client = OpenAI(api_key=api_key, timeout=config.embedding_timeout_seconds())
        except Exception as init_error:  # pragma: no cover - env-dependent
            self.available = False
            self.error = f"openai client init failed: {init_error}"
            return
        self.available = True
        # We can only know dimensions after the first call; leave at 0 until then.

    def _truncate(self, text: str) -> str:
        cap = config.embedding_max_text_chars()
        if cap and len(text) > cap:
            return text[:cap]
        return text

    def get_embedding(self, text: str) -> Optional[List[float]]:
        if not self.available or not self._client:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=self._truncate(text),
            )
        except Exception as call_error:  # pragma: no cover - network-dependent
            self.error = f"embedding call failed: {call_error}"
            logger.warning("OpenAI embedding call failed: %s", call_error)
            return None
        try:
            vector = list(response.data[0].embedding)
        except Exception:  # pragma: no cover - shape mismatch
            self.error = "unexpected embedding response shape"
            return None
        if not self.dimensions:
            self.dimensions = len(vector)
        return vector


def get_active_provider() -> EmbeddingProvider:
    """Return the provider matching the current environment. Never raises.

    Resolution order:
        1. If SEMANTIC_MATCHING_ENABLED is false → DisabledEmbeddingProvider.
        2. EMBEDDING_PROVIDER=deterministic → DeterministicHashEmbeddingProvider.
        3. EMBEDDING_PROVIDER=openai → OpenAIEmbeddingProvider (may end up
           ``available=False`` if the SDK or key is missing — caller treats
           that as disabled).
        4. Anything else → DisabledEmbeddingProvider.
    """
    if not config.semantic_matching_enabled():
        return DisabledEmbeddingProvider(reason="SEMANTIC_MATCHING_ENABLED=false")
    provider_name = config.embedding_provider()
    if provider_name == "deterministic":
        return DeterministicHashEmbeddingProvider()
    if provider_name == "openai":
        return OpenAIEmbeddingProvider()
    return DisabledEmbeddingProvider(reason=f"unsupported provider: {provider_name}")


def hash_text_for_cache(text: str) -> str:
    """Stable SHA-256 of the canonicalized text used as the cache lookup key."""
    canonical = (text or "").strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def iter_unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
