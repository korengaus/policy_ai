"""Phase 2 M5: cosine similarity + ranking for semantic evidence matching.

This module knows nothing about the verification pipeline. It just turns
(claim_text, chunks, provider) into a ranked match summary that the
``semantic_evidence_agent`` can attach to ``debug_summary``.

The output schema is intentionally close to the spec in the task brief so
the frontend (or future pgvector migration) can consume it directly.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, List, Optional, Sequence

import config
import database
import semantic_embeddings


logger = logging.getLogger(__name__)


def cosine_similarity(vec_a: Optional[Sequence[float]], vec_b: Optional[Sequence[float]]) -> float:
    """Cosine similarity bounded to ``[-1, 1]``. Returns 0.0 on any bad input."""
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) != len(vec_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        try:
            fa = float(a)
            fb = float(b)
        except (TypeError, ValueError):
            return 0.0
        dot += fa * fb
        norm_a += fa * fa
        norm_b += fb * fb
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def score_to_percent(score: float) -> int:
    """Map cosine score in ``[-1, 1]`` to a 0-100 percentage.

    The mapping is monotonic and conservative — a perfect 1.0 maps to 100,
    a perfect anti-correlation maps to 0, and 0 (orthogonal vectors)
    maps to 50. The agent never uses this number for verdict decisions,
    it's purely UI/debug.
    """
    try:
        bounded = max(-1.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return 0
    return int(round((bounded + 1.0) * 50.0))


def _embed_with_cache(
    text: str,
    provider: semantic_embeddings.EmbeddingProvider,
    *,
    cache_enabled: bool,
) -> tuple[Optional[List[float]], bool]:
    """Return (vector, cache_hit). Vector may be None if provider is disabled."""
    if not provider.available:
        return None, False
    canonical = (text or "").strip()
    if not canonical:
        return None, False
    text_hash = semantic_embeddings.hash_text_for_cache(canonical)
    if cache_enabled:
        cached = database.get_cached_embedding(text_hash, provider.name, provider.model)
        if cached is not None:
            return cached, True
    vector = provider.get_embedding(canonical)
    if vector is None:
        return None, False
    if cache_enabled:
        # Best-effort store; failures don't change behavior.
        database.save_cached_embedding(
            text_hash=text_hash,
            provider=provider.name,
            model=provider.model,
            vector=vector,
            text_preview=canonical[:200],
        )
    return vector, False


def _classify_support(top_score_percent: int) -> str:
    """Map cosine percent to one of strong/contextual/weak.

    Thresholds come from ``config`` so operators can tune without editing
    code. The labels here are SEMANTIC strength — never confuse with
    verification strength.
    """
    support_pct = score_to_percent(config.semantic_min_score_for_support())
    context_pct = score_to_percent(config.semantic_min_score_for_context())
    if top_score_percent >= support_pct:
        return "strong"
    if top_score_percent >= context_pct:
        return "contextual"
    return "weak"


def rank_semantic_matches(
    claim_text: str,
    chunks: Sequence[dict],
    provider: Optional[semantic_embeddings.EmbeddingProvider] = None,
    *,
    cache_enabled: Optional[bool] = None,
    top_k: int = 5,
) -> dict:
    """Embed claim+chunks and return the ranked match summary.

    The returned dict always includes ``enabled``, ``available``,
    ``support_level``, ``matches`` (possibly empty) and ``errors``, so
    callers never need to special-case missing keys.
    """
    provider = provider or semantic_embeddings.get_active_provider()
    enabled = config.semantic_matching_enabled()
    cache_enabled = config.embedding_cache_enabled() if cache_enabled is None else bool(cache_enabled)
    errors: List[str] = []
    base = {
        "enabled": enabled,
        "available": False,
        "provider": provider.name,
        "model": provider.model,
        "claim_text": (claim_text or "")[:400],
        "top_score": 0.0,
        "top_score_percent": 0,
        "support_level": "unavailable",
        "matches": [],
        "errors": errors,
        "cache_hits": 0,
        "chunk_count": len(chunks) if chunks else 0,
    }
    if not provider.available:
        if provider.error:
            errors.append(provider.error)
        return base
    if not isinstance(claim_text, str) or not claim_text.strip():
        errors.append("empty claim text")
        return base
    if not chunks:
        errors.append("no chunks provided")
        # Mark available=True so the caller can distinguish "we tried" from
        # "provider missing"; support_level stays unavailable.
        base["available"] = True
        return base

    claim_vector, claim_cache_hit = _embed_with_cache(
        claim_text, provider, cache_enabled=cache_enabled
    )
    if claim_vector is None:
        errors.append("failed to embed claim text")
        return base

    base["available"] = True
    cache_hits = 1 if claim_cache_hit else 0
    scored: List[dict] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_text = chunk.get("text") or ""
        if not chunk_text.strip():
            continue
        chunk_vector, chunk_cache_hit = _embed_with_cache(
            chunk_text, provider, cache_enabled=cache_enabled
        )
        if chunk_vector is None:
            continue
        if chunk_cache_hit:
            cache_hits += 1
        score = cosine_similarity(claim_vector, chunk_vector)
        scored.append({
            "chunk_id": chunk.get("chunk_id") or "",
            "score": float(score),
            "score_percent": score_to_percent(score),
            "text": chunk_text[:400],
            "char_start": int(chunk.get("char_start") or 0),
            "char_end": int(chunk.get("char_end") or 0),
            "source_id": chunk.get("source_id") or "",
            "source_title": chunk.get("source_title") or "",
            "source_url": chunk.get("source_url") or "",
        })

    scored.sort(key=lambda match: match["score"], reverse=True)
    top_k = max(1, int(top_k or 5))
    base["matches"] = scored[:top_k]
    base["cache_hits"] = cache_hits
    if scored:
        top = scored[0]
        base["top_score"] = float(top["score"])
        base["top_score_percent"] = int(top["score_percent"])
        base["support_level"] = _classify_support(top["score_percent"])
    else:
        # No chunk could be embedded; treat as weak/unavailable.
        base["support_level"] = "unavailable"
    return base
