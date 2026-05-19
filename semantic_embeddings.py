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
    """Base interface. Subclasses implement ``get_embedding`` and ``name``.

    Subclasses set:
        * ``available`` — True only when the provider can actually return a
          vector for non-empty text.
        * ``configured`` — True when all required env config is present
          (regardless of network reachability). Distinguishes "operator
          didn't set this up" from "we tried and it failed."
        * ``external_calls_possible`` — True when calling ``get_embedding``
          could result in a network request. False for disabled and
          deterministic providers.
        * ``reason`` — Short human-readable summary, JSON-safe.
    """

    name = "base"
    model = ""
    dimensions = 0
    available = False
    configured = False
    external_calls_possible = False
    error: Optional[str] = None
    reason: str = ""

    def get_embedding(self, text: str) -> Optional[List[float]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_embeddings(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        """Per-text dispatch; individual failures return ``None`` in place,
        never raise, never abort the batch."""
        results: List[Optional[List[float]]] = []
        for text in texts:
            try:
                results.append(self.get_embedding(text))
            except Exception as error:  # defensive: subclass bugs must not break callers
                logger.warning("embedding call raised unexpectedly: %s", error)
                results.append(None)
        return results

    def provider_status(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "dimensions": self.dimensions,
            "available": bool(self.available),
            "configured": bool(self.configured),
            "external_calls_possible": bool(self.external_calls_possible),
            "reason": self.reason or "",
            "error": self.error,
        }


class DisabledEmbeddingProvider(EmbeddingProvider):
    """Returned when SEMANTIC_MATCHING_ENABLED is false or provider=disabled.

    All methods are pure no-ops so callers never need to special-case the
    disabled state — they just observe ``available=False`` and skip ranking.
    """

    name = "disabled"
    configured = False
    external_calls_possible = False

    def __init__(self, reason: str = "semantic matching disabled") -> None:
        self.reason = reason
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
    configured = True
    external_calls_possible = False
    # Plain ASCII so the reason can be printed on Windows cp949 consoles
    # without forcing operators to set PYTHONUTF8 first.
    reason = "deterministic provider: no network, stable across runs"

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

    Initialization never raises. ``available`` is True only when every
    requirement is satisfied:
        * OpenAI SDK is importable
        * ``OPENAI_API_KEY`` is set (non-empty)
        * ``EMBEDDING_MODEL`` is set (non-empty) — M5.5 fail-closed change
        * The SDK client constructor succeeds

    ``configured`` is True when ``OPENAI_API_KEY`` AND ``EMBEDDING_MODEL`` are
    both present (regardless of SDK reachability), so operators can tell
    "missing setup" from "setup but couldn't initialize."

    Never logs API keys or raw input text. Errors are stored on the
    instance and exposed via ``provider_status()``.
    """

    name = "openai"
    external_calls_possible = True

    def __init__(self) -> None:
        self.model = config.embedding_model().strip()
        self._client = None
        self.error = None
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        # ``configured`` is independent of import/network success.
        self.configured = bool(api_key and self.model)

        if not api_key:
            self.available = False
            self.reason = "OPENAI_API_KEY missing"
            self.error = self.reason
            return
        if not self.model:
            # Fail-closed when EMBEDDING_MODEL is not provided. Previously we
            # silently defaulted to text-embedding-3-small, which made it too
            # easy to incur cost from a half-configured environment.
            self.available = False
            self.reason = "EMBEDDING_MODEL missing"
            self.error = self.reason
            return
        try:
            from openai import OpenAI  # local import — keeps app importable without the SDK
        except Exception as import_error:  # pragma: no cover - depends on env
            self.available = False
            self.reason = "openai sdk not importable"
            self.error = f"{self.reason}: {import_error}"
            logger.warning("OpenAIEmbeddingProvider unavailable: %s", self.reason)
            return
        try:
            self._client = OpenAI(api_key=api_key, timeout=config.embedding_timeout_seconds())
        except Exception as init_error:  # pragma: no cover - env-dependent
            self.available = False
            self.reason = "openai client init failed"
            self.error = f"{self.reason}: {init_error}"
            logger.warning("OpenAI client init failed: %s", init_error)
            return
        self.available = True
        self.reason = "openai client initialized; first embedding call will populate dimensions"
        # We can only learn dimensions after the first call; leave at 0 until then.

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
        truncated = self._truncate(text)
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=truncated,
            )
        except Exception as call_error:  # pragma: no cover - network-dependent
            # Log the exception type + short message only; never log the
            # API key (never in scope here) or the input text.
            self.error = f"embedding call failed: {type(call_error).__name__}"
            logger.warning(
                "OpenAI embedding call failed (text_len=%d, model=%s): %s",
                len(truncated), self.model, type(call_error).__name__,
            )
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
