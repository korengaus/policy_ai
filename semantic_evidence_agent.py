"""Phase 2 M5: semantic evidence agent.

Coordinates the embedding provider, chunker, and similarity ranker to
produce a ``semantic_evidence_summary`` for the pipeline. Strictly
additive — this module never modifies claims, snippets, source candidates,
verdict labels, or any other pipeline state. It returns a dict; the caller
decides where (if anywhere) to attach it.

Guarantees:
    * If semantic matching is disabled or unavailable, returns a summary
      with ``semantic_matching_available=False`` and an empty matches list.
      The pipeline keeps running unchanged.
    * Prefers official body text over news/article text when ranking.
    * Never fabricates quotes — the ``top_matches`` list only includes
      retrieved chunk text.
    * No claim is ever upgraded to verified here; the support label
      ("strong" / "contextual" / "weak") describes semantic match strength
      and is consumed downstream as debug metadata only.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

import config
import semantic_chunker
import semantic_embeddings
import semantic_similarity


logger = logging.getLogger(__name__)


def _coerce_claims(normalized_claims: object, claim_text_fallback: str) -> List[dict]:
    """Return a list of ``{"claim_index", "claim_text"}`` dicts.

    Tolerates the heterogeneous shapes the pipeline emits: lists of strings,
    lists of ``{"claim_text": ...}``, lists of ``{"text": ...}``, or a single
    fallback string when nothing else is available.
    """
    claims: List[dict] = []
    if isinstance(normalized_claims, list):
        for index, item in enumerate(normalized_claims):
            text = ""
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get("claim_text") or item.get("text") or item.get("claim") or ""
            text = (text or "").strip()
            if text:
                claims.append({"claim_index": index, "claim_text": text})
    if not claims and isinstance(claim_text_fallback, str) and claim_text_fallback.strip():
        claims.append({"claim_index": 0, "claim_text": claim_text_fallback.strip()})
    return claims


def _build_source_chunks(
    source_candidates: Optional[Sequence[dict]],
    evidence_snippets: Optional[Sequence[dict]],
    *,
    max_chunks_per_source: int,
    max_chars_per_chunk: int = 480,
) -> tuple[List[dict], int]:
    """Produce a flat list of chunks across all sources, preferring official body text.

    Returns ``(chunks, sources_used)`` where ``sources_used`` is the count of
    distinct sources contributing at least one chunk.
    """
    chunks: List[dict] = []
    sources_used = 0

    if isinstance(source_candidates, Sequence):
        for source in source_candidates:
            if not isinstance(source, dict):
                continue
            body = (
                source.get("official_body_text")
                or source.get("body_text")
                or ""
            )
            if not isinstance(body, str) or not body.strip():
                continue
            source_id = (
                source.get("url")
                or source.get("official_detail_url")
                or source.get("source_url")
                or source.get("title")
                or "source"
            )
            new_chunks = semantic_chunker.chunk_text_for_semantic_matching(
                body,
                max_chunks=max_chunks_per_source,
                max_chars_per_chunk=max_chars_per_chunk,
                source_kind="official_body_text",
                source_id=str(source_id),
            )
            if not new_chunks:
                continue
            sources_used += 1
            source_title = source.get("title") or source.get("source_title") or ""
            source_url = source.get("url") or source.get("source_url") or ""
            for chunk in new_chunks:
                chunk["source_title"] = source_title
                chunk["source_url"] = source_url
            chunks.extend(new_chunks)

    # Evidence snippets are added as secondary context; they're typically
    # already short, so we surface them whole rather than re-chunking.
    if isinstance(evidence_snippets, Sequence):
        snippet_counter = 0
        for snippet in evidence_snippets:
            if not isinstance(snippet, dict):
                continue
            text = snippet.get("evidence_text") or snippet.get("text") or ""
            if not isinstance(text, str) or not text.strip():
                continue
            source_url = snippet.get("source_url") or snippet.get("url") or ""
            source_title = snippet.get("source_title") or snippet.get("title") or ""
            chunks.append({
                "chunk_id": f"evidence_snippet:{snippet_counter}",
                "text": text[:max_chars_per_chunk],
                "char_start": 0,
                "char_end": min(len(text), max_chars_per_chunk),
                "source": "evidence_snippet",
                "source_id": snippet.get("snippet_id") or f"snippet-{snippet_counter}",
                "source_title": source_title,
                "source_url": source_url,
            })
            snippet_counter += 1
        if snippet_counter > 0:
            sources_used += 1

    return chunks, sources_used


def _aggregate_support_level(per_claim: Iterable[dict]) -> str:
    """Reduce per-claim support levels to a single ``best_support_level``.

    Priority order — ``strong`` > ``contextual`` > ``weak`` > ``unavailable``.
    """
    seen = {claim.get("support_level") for claim in per_claim}
    for level in ("strong", "contextual", "weak"):
        if level in seen:
            return level
    return "unavailable"


def compute_semantic_evidence_summary(
    *,
    normalized_claims: Optional[Sequence] = None,
    claim_text: str = "",
    source_candidates: Optional[Sequence[dict]] = None,
    evidence_snippets: Optional[Sequence[dict]] = None,
    provider: Optional[semantic_embeddings.EmbeddingProvider] = None,
) -> dict:
    """Top-level entry point. Returns a ``semantic_evidence_summary`` dict.

    Safe to call even when semantic matching is disabled — that path
    short-circuits with ``semantic_matching_available=False`` and no
    embedding calls.
    """
    enabled = config.semantic_matching_enabled()
    provider = provider or semantic_embeddings.get_active_provider()
    available = bool(provider.available)

    summary = {
        "semantic_matching_enabled": enabled,
        "semantic_matching_available": available,
        "provider": provider.name,
        "model": provider.model,
        "dimensions": provider.dimensions,
        "claim_count": 0,
        "source_count": 0,
        "chunk_count": 0,
        "best_overall_score": 0.0,
        "best_overall_score_percent": 0,
        "best_support_level": "unavailable",
        "claim_matches": [],
        "limitations": [],
        "errors": [],
    }

    if not enabled:
        summary["limitations"].append("semantic matching disabled via configuration")
        return summary
    if not available:
        reason = provider.error or "embedding provider unavailable"
        summary["errors"].append(reason)
        summary["limitations"].append(reason)
        return summary

    claims = _coerce_claims(normalized_claims, claim_text)
    summary["claim_count"] = len(claims)
    if not claims:
        summary["limitations"].append("no claims available for semantic matching")
        return summary

    max_chunks = config.semantic_max_chunks_per_source()
    chunks, sources_used = _build_source_chunks(
        source_candidates,
        evidence_snippets,
        max_chunks_per_source=max_chunks,
    )
    summary["source_count"] = sources_used
    summary["chunk_count"] = len(chunks)

    if not chunks:
        summary["limitations"].append(
            "no official body text available — semantic matching cannot evaluate this claim"
        )
        return summary

    cache_enabled = config.embedding_cache_enabled()
    claim_matches: List[dict] = []
    cache_hits = 0
    best_score = 0.0
    best_percent = 0

    for claim in claims:
        ranked = semantic_similarity.rank_semantic_matches(
            claim_text=claim["claim_text"],
            chunks=chunks,
            provider=provider,
            cache_enabled=cache_enabled,
        )
        cache_hits += int(ranked.get("cache_hits") or 0)
        if ranked.get("errors"):
            summary["errors"].extend(ranked["errors"])
        claim_matches.append({
            "claim_index": claim["claim_index"],
            "claim_text": claim["claim_text"][:400],
            "best_score": float(ranked.get("top_score") or 0.0),
            "best_score_percent": int(ranked.get("top_score_percent") or 0),
            "support_level": ranked.get("support_level") or "unavailable",
            "top_matches": ranked.get("matches") or [],
        })
        if (ranked.get("top_score") or 0.0) > best_score:
            best_score = float(ranked["top_score"])
            best_percent = int(ranked.get("top_score_percent") or 0)

    summary["claim_matches"] = claim_matches
    summary["best_overall_score"] = best_score
    summary["best_overall_score_percent"] = best_percent
    summary["best_support_level"] = _aggregate_support_level(claim_matches)
    summary["cache_hits"] = cache_hits

    # Hard conservative guardrail — even if semantic match is "strong", the
    # surrounding pipeline must not treat this as standalone verification.
    # We bake the disclaimer into the summary so downstream consumers (UI,
    # exports, future analysts) always see it.
    summary["limitations"].append(
        "semantic match strength is metadata only; rule-based verification "
        "and official body matching remain authoritative"
    )
    return summary
