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
import time
from typing import Iterable, List, Optional, Sequence

import config
import semantic_chunker
import semantic_embeddings
import semantic_fact_guardrails
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


def _apply_guardrails_to_claim_match(
    *,
    claim_text: str,
    raw_support_level: str,
    top_matches: List[dict],
) -> dict:
    """Run the critical-fact guardrails against each top match for a claim.

    Returns a dict describing how the claim's exposed support_level should
    differ from the raw semantic support level. Always non-destructive: the
    raw level is preserved on the claim_match for diagnostics, and each top
    match gets a ``critical_fact_check`` attached.
    """
    aggregated_risk_flags: List[str] = []
    aggregated_mismatches: List[dict] = []
    tightest_cap = "strong"  # ranking: strong > contextual > weak

    for match in top_matches:
        match_text = match.get("text") or ""
        check = semantic_fact_guardrails.compare_critical_facts(claim_text, match_text)
        match["critical_fact_check"] = check
        for flag in check.get("risk_flags") or []:
            if flag not in aggregated_risk_flags:
                aggregated_risk_flags.append(flag)
        aggregated_mismatches.extend(check.get("mismatches") or [])
        tightest_cap = semantic_fact_guardrails.cap_support_level(
            tightest_cap, check.get("support_cap") or "strong"
        )

    if not top_matches:
        # Nothing to compare; raw level (likely "unavailable") stands.
        return {
            "raw_support_level": raw_support_level,
            "guardrail_adjusted_support_level": raw_support_level,
            "semantic_risk_flags": [],
            "critical_mismatches": [],
            "support_cap_applied": False,
            "support_cap_reason": "no matches to evaluate",
        }

    adjusted = semantic_fact_guardrails.cap_support_level(raw_support_level, tightest_cap)
    cap_applied = adjusted != raw_support_level
    return {
        "raw_support_level": raw_support_level,
        "guardrail_adjusted_support_level": adjusted,
        "semantic_risk_flags": aggregated_risk_flags,
        "critical_mismatches": aggregated_mismatches,
        "support_cap_applied": cap_applied,
        "support_cap_reason": (
            f"capped to {tightest_cap} by guardrails" if cap_applied else "no cap applied"
        ),
    }


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
    embedding calls. M5.5 adds runtime metadata (``runtime_ms``,
    ``embedding_request_count``, ``provider_status``) so operators can
    measure latency and provider state without enabling matching globally.
    """
    started_at = time.perf_counter()
    enabled = config.semantic_matching_enabled()
    provider = provider or semantic_embeddings.get_active_provider()
    available = bool(provider.available)
    provider_status = provider.provider_status()

    summary = {
        "semantic_matching_enabled": enabled,
        "semantic_matching_available": available,
        "provider": provider.name,
        "model": provider.model,
        "dimensions": provider.dimensions,
        "provider_status": provider_status,
        "claim_count": 0,
        "source_count": 0,
        "chunk_count": 0,
        "best_overall_score": 0.0,
        "best_overall_score_percent": 0,
        "best_support_level": "unavailable",
        "best_raw_support_level": "unavailable",
        "claim_matches": [],
        "limitations": [],
        "errors": [],
        "cache_hits": 0,
        "embedding_request_count": 0,
        "runtime_ms": 0,
        # Phase 2 M5.7: critical-fact guardrails. ``semantic_guardrails_enabled``
        # is always True when this agent runs — they're deterministic, fast,
        # and have no external dependency. The counts below summarize how
        # often the guardrail capped the raw semantic label.
        "semantic_guardrails_enabled": True,
        "semantic_risk_flags": [],
        "critical_mismatch_count": 0,
        "support_cap_applied_count": 0,
    }

    def _finalize(out: dict) -> dict:
        out["runtime_ms"] = int(round((time.perf_counter() - started_at) * 1000))
        _emit_debug_log(out)
        return out

    if not enabled:
        summary["limitations"].append("semantic matching disabled via configuration")
        return _finalize(summary)
    if not available:
        reason = provider.error or "embedding provider unavailable"
        summary["errors"].append(reason)
        summary["limitations"].append(reason)
        return _finalize(summary)

    claims = _coerce_claims(normalized_claims, claim_text)
    summary["claim_count"] = len(claims)
    if not claims:
        summary["limitations"].append("no claims available for semantic matching")
        return _finalize(summary)

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
        return _finalize(summary)

    cache_enabled = config.embedding_cache_enabled()
    claim_matches: List[dict] = []
    cache_hits = 0
    best_score = 0.0
    best_percent = 0
    # Each claim embedding is one request + one per chunk it ranks against;
    # cache hits don't count as requests. The similarity ranker reports
    # cache_hits per call, so non-hits == requests.
    embedding_request_count = 0

    aggregated_risk_flags: List[str] = []
    critical_mismatch_count = 0
    support_cap_applied_count = 0
    raw_levels_for_aggregate: List[dict] = []

    for claim in claims:
        ranked = semantic_similarity.rank_semantic_matches(
            claim_text=claim["claim_text"],
            chunks=chunks,
            provider=provider,
            cache_enabled=cache_enabled,
        )
        per_call_cache_hits = int(ranked.get("cache_hits") or 0)
        cache_hits += per_call_cache_hits
        # 1 claim embed + one per chunk in the call, minus cache hits.
        call_total_embeds = 1 + (ranked.get("chunk_count") or 0)
        embedding_request_count += max(0, call_total_embeds - per_call_cache_hits)
        if ranked.get("errors"):
            summary["errors"].extend(ranked["errors"])

        raw_support_level = ranked.get("support_level") or "unavailable"
        top_matches = ranked.get("matches") or []
        guardrail_result = _apply_guardrails_to_claim_match(
            claim_text=claim["claim_text"],
            raw_support_level=raw_support_level,
            top_matches=top_matches,
        )

        # Aggregate guardrail metrics across all claims.
        for flag in guardrail_result["semantic_risk_flags"]:
            if flag not in aggregated_risk_flags:
                aggregated_risk_flags.append(flag)
        critical_mismatch_count += len(guardrail_result["critical_mismatches"])
        if guardrail_result["support_cap_applied"]:
            support_cap_applied_count += 1

        adjusted_level = guardrail_result["guardrail_adjusted_support_level"]
        claim_matches.append({
            "claim_index": claim["claim_index"],
            "claim_text": claim["claim_text"][:400],
            "best_score": float(ranked.get("top_score") or 0.0),
            "best_score_percent": int(ranked.get("top_score_percent") or 0),
            # ``support_level`` exposes the guardrail-adjusted label so
            # downstream consumers (calibration evaluator, UI, exports) see
            # the conservative value by default. ``raw_support_level`` is
            # preserved for diagnostics and threshold tuning.
            "support_level": adjusted_level,
            "raw_support_level": guardrail_result["raw_support_level"],
            "guardrail_adjusted_support_level": adjusted_level,
            "semantic_risk_flags": guardrail_result["semantic_risk_flags"],
            "critical_mismatches": guardrail_result["critical_mismatches"],
            "support_cap_applied": guardrail_result["support_cap_applied"],
            "support_cap_reason": guardrail_result["support_cap_reason"],
            "top_matches": top_matches,
        })
        raw_levels_for_aggregate.append({"support_level": raw_support_level})

        if (ranked.get("top_score") or 0.0) > best_score:
            best_score = float(ranked["top_score"])
            best_percent = int(ranked.get("top_score_percent") or 0)

    summary["claim_matches"] = claim_matches
    summary["best_overall_score"] = best_score
    summary["best_overall_score_percent"] = best_percent
    # ``best_support_level`` reflects the guardrail-adjusted view, while
    # ``best_raw_support_level`` keeps the un-capped score visible.
    summary["best_support_level"] = _aggregate_support_level(claim_matches)
    summary["best_raw_support_level"] = _aggregate_support_level(raw_levels_for_aggregate)
    summary["cache_hits"] = cache_hits
    summary["embedding_request_count"] = embedding_request_count
    summary["semantic_risk_flags"] = aggregated_risk_flags
    summary["critical_mismatch_count"] = critical_mismatch_count
    summary["support_cap_applied_count"] = support_cap_applied_count

    # Hard conservative guardrail — even if semantic match is "strong", the
    # surrounding pipeline must not treat this as standalone verification.
    # We bake the disclaimer into the summary so downstream consumers (UI,
    # exports, future analysts) always see it.
    summary["limitations"].append(
        "semantic match strength is metadata only; rule-based verification "
        "and official body matching remain authoritative"
    )
    return _finalize(summary)


def _emit_debug_log(summary: dict) -> None:
    """Short single-line summary at INFO level — no raw bodies, no keys.

    Only fires when matching is enabled AND the provider is available, so
    the default disabled path stays completely silent.
    """
    if not summary.get("semantic_matching_enabled"):
        return
    if not summary.get("semantic_matching_available"):
        return
    try:
        logger.info(
            "semantic matching summary: provider=%s model=%s available=%s "
            "best_support_level=%s best_score_percent=%s claims=%s chunks=%s "
            "cache_hits=%s embedding_requests=%s runtime_ms=%s",
            summary.get("provider"),
            summary.get("model") or "(unset)",
            summary.get("semantic_matching_available"),
            summary.get("best_support_level"),
            summary.get("best_overall_score_percent"),
            summary.get("claim_count"),
            summary.get("chunk_count"),
            summary.get("cache_hits"),
            summary.get("embedding_request_count"),
            summary.get("runtime_ms"),
        )
    except Exception:
        # Logging must never break the pipeline.
        logger.debug("failed to emit semantic matching debug log", exc_info=True)
