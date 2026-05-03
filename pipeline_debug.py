def _count(items) -> int:
    return len(items) if isinstance(items, list) else 0


def _official_source_count(source_candidates: list[dict]) -> int:
    return sum(
        1
        for source in source_candidates or []
        if source.get("source_type") in {"official_government", "public_institution"}
    )


def _news_source_count(source_candidates: list[dict]) -> int:
    return sum(
        1
        for source in source_candidates or []
        if source.get("source_type") in {"established_news", "search_fallback_news"}
    )


def _direct_evidence_count(evidence_snippets: list[dict]) -> int:
    return sum(
        1
        for evidence in evidence_snippets or []
        if evidence.get("evidence_type") == "direct_support"
    )


def _framing_flags_count(bias_framing_analysis: list[dict]) -> int:
    return sum(
        1
        for item in bias_framing_analysis or []
        if item.get("framing_level") in {"medium", "high"}
        or item.get("emotional_language_detected")
        or item.get("uncertainty_language")
        or item.get("needs_editor_review")
    )


def build_pipeline_debug_summary(
    *,
    news: dict,
    original_url: str,
    claims: list[str],
    normalized_claims: list[dict],
    source_candidates: list[dict],
    official_source_candidates: list[dict],
    evidence_snippets: list[dict],
    contradiction_checks: list[dict],
    bias_framing_analysis: list[dict],
    verification_card: dict,
) -> dict:
    claims_count = _count(claims)
    normalized_count = _count(normalized_claims)
    source_count = _count(source_candidates)
    official_sources_count = _official_source_count(source_candidates)
    if not official_sources_count:
        official_sources_count = _count(official_source_candidates)
    news_sources_count = _news_source_count(source_candidates)
    evidence_count = _count(evidence_snippets)
    direct_count = _direct_evidence_count(evidence_snippets)
    contradiction_count = _count(contradiction_checks)
    bias_count = _count(bias_framing_analysis)
    framing_flags_count = _framing_flags_count(bias_framing_analysis)
    overall_verdict = verification_card.get("verdict_label") or ""

    intake_ok = bool(news.get("title") and original_url)
    claim_extraction_ok = claims_count > 0
    claim_normalization_ok = normalized_count > 0 and normalized_count >= claims_count
    source_retrieval_ok = source_count > 0
    evidence_matching_ok = evidence_count > 0
    contradiction_check_ok = contradiction_count > 0 and (
        not claims_count or contradiction_count >= claims_count
    )
    bias_framing_ok = bias_count > 0 and (not claims_count or bias_count >= claims_count)

    missing_steps = []
    if not intake_ok:
        missing_steps.append("intake")
    if not claim_extraction_ok:
        missing_steps.append("claim_extraction")
    if not claim_normalization_ok:
        missing_steps.append("claim_normalization")
    if not source_retrieval_ok:
        missing_steps.append("source_retrieval")
    if not evidence_matching_ok:
        missing_steps.append("evidence_matching")
    if not contradiction_check_ok:
        missing_steps.append("contradiction_check")
    if not bias_framing_ok:
        missing_steps.append("bias_framing")

    needs_human_review = bool(
        "review" in overall_verdict
        or overall_verdict in {
            "draft_needs_context",
            "draft_needs_official_confirmation",
            "draft_disputed",
        }
        or any(check.get("needs_human_review") for check in contradiction_checks or [])
        or any(item.get("needs_editor_review") for item in bias_framing_analysis or [])
    )

    summary = {
        "intake_ok": intake_ok,
        "claim_extraction_ok": claim_extraction_ok,
        "claim_normalization_ok": claim_normalization_ok,
        "source_retrieval_ok": source_retrieval_ok,
        "evidence_matching_ok": evidence_matching_ok,
        "contradiction_check_ok": contradiction_check_ok,
        "bias_framing_ok": bias_framing_ok,
        "claims_count": claims_count,
        "normalized_claims_count": normalized_count,
        "evidence_candidates_count": source_count,
        "official_sources_count": official_sources_count,
        "news_sources_count": news_sources_count,
        "direct_evidence_count": direct_count,
        "contradiction_checks_count": contradiction_count,
        "framing_flags_count": framing_flags_count,
        "overall_verdict": overall_verdict,
        "needs_human_review": needs_human_review,
        "missing_steps": missing_steps,
    }
    print(
        "[PipelineDebug] "
        f"intake_ok={str(intake_ok).lower()} "
        f"claim_count={claims_count} "
        f"evidence_count={evidence_count} "
        f"bias_framing_ok={str(bias_framing_ok).lower()}"
    )
    return summary
