def _clamp(value: int | float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, round(float(value or 0))))


def _strength_score(strength: dict) -> int:
    strong = int(strength.get("strong") or 0)
    medium = int(strength.get("medium") or 0)
    weak = int(strength.get("weak") or 0)
    total = strong + medium + weak
    if not total:
        return 0
    return _clamp(((strong * 3) + (medium * 2) + weak) / (total * 3) * 100)


def _source_trust_score(summary: dict, source_candidates: list[dict]) -> int:
    official_detail = bool(summary.get("official_detail_available"))
    official_body_matches = int(summary.get("official_body_matches") or summary.get("official_body_match_count") or 0)
    official_resolution_direct = int(summary.get("official_resolution_direct_matches") or 0)
    official_resolution_contextual = int(summary.get("official_resolution_contextual_matches") or 0)
    official_resolution_top_score = int(summary.get("official_resolution_top_score") or summary.get("official_direct_match_score") or 0)
    official_usable = int(summary.get("official_bodies_usable") or 0)
    official_candidates = int(summary.get("official_body_candidates") or summary.get("official_candidate_count") or 0)
    average_reliability = int(summary.get("average_reliability_score") or 0)
    fetched_official = [
        source
        for source in source_candidates or []
        if source.get("source_type") in {"official_government", "public_institution"}
        and source.get("raw_text_available")
    ]

    score = 20
    if official_detail:
        score += 25
    if official_body_matches:
        score += min(30, official_body_matches * 15)
    elif official_usable:
        score += 12
    elif official_candidates:
        score += 5

    if official_resolution_direct:
        score += min(25, official_resolution_direct * 18)
    elif official_resolution_contextual:
        score += min(15, official_resolution_contextual * 10)

    if official_resolution_top_score >= 75:
        score += 10
    elif official_resolution_top_score >= 55:
        score += 6

    if fetched_official:
        score += 10
    score += min(15, average_reliability // 5)

    fallback_only = bool(source_candidates) and all(
        source.get("source_type") == "search_fallback_news"
        for source in source_candidates or []
    )
    if fallback_only:
        score = min(score, 45)

    if summary.get("official_mismatch") and not official_body_matches:
        score = min(score, 35)
    return _clamp(score)


def _human_feedback_adjustment(debug_summary: dict) -> int:
    if debug_summary.get("approved_boost"):
        return 15
    if debug_summary.get("rejected_penalty"):
        return -30
    if debug_summary.get("review_feedback_status") == "needs_more_info":
        return -10
    return 0


def _contradiction_adjustment(contradiction_summary: dict) -> int:
    confirmed = int(
        contradiction_summary.get("confirmed_contradiction_count")
        or contradiction_summary.get("confirmed_contradictions")
        or contradiction_summary.get("likely_contradiction_count")
        or 0
    )
    possible = int(
        contradiction_summary.get("possible_contradiction_count")
        or contradiction_summary.get("possible_contradictions")
        or 0
    )
    if confirmed:
        return -35
    if possible:
        return -12
    return 0


def _impact_gate(policy_impact: dict, policy_confidence: dict) -> int:
    impact_level = policy_impact.get("impact_level")
    risk_level = policy_confidence.get("risk_level")
    consumer = int(policy_impact.get("consumer_sensitivity") or 0)
    market = int(policy_impact.get("market_sensitivity") or 0)
    business = int(policy_impact.get("business_sensitivity") or 0)

    score = 0
    if impact_level == "high":
        score += 18
    elif impact_level == "medium":
        score += 10
    if risk_level == "high":
        score += 12
    elif risk_level == "medium":
        score += 6
    score += min(10, max(consumer, market, business) // 10)
    return _clamp(score, 0, 35)


def _alert_from_score(
    *,
    final_score: int,
    evidence_quality_score: int,
    source_trust_score: int,
    strength_score: int,
    contradiction_adjustment: int,
    human_feedback_adjustment: int,
    policy_impact: dict,
    policy_confidence: dict,
    official_mismatch: bool,
) -> str:
    impact_level = policy_impact.get("impact_level")
    risk_level = policy_confidence.get("risk_level")

    if human_feedback_adjustment >= 15 and final_score >= 65:
        return "HIGH" if impact_level == "high" else "WATCH"
    if contradiction_adjustment <= -35:
        return "WATCH"
    if official_mismatch and source_trust_score < 45:
        return "WATCH" if impact_level == "high" or risk_level == "high" else "LOW"
    if (
        final_score >= 75
        and evidence_quality_score >= 65
        and source_trust_score >= 55
        and strength_score >= 55
        and contradiction_adjustment == 0
        and impact_level == "high"
    ):
        return "HIGH"
    if final_score >= 45 or impact_level == "high" or risk_level == "high":
        return "WATCH"
    return "LOW"


def calibrate_final_decision(
    *,
    final_decision: dict,
    policy_confidence: dict,
    policy_impact: dict,
    verification_card: dict,
    source_candidates: list[dict],
    evidence_snippets: list[dict],
    debug_summary: dict,
) -> tuple[dict, dict]:
    debug = dict(debug_summary or {})
    strength = debug.get("evidence_strength_summary") or {}
    quality = debug.get("evidence_quality_summary") or verification_card.get("evidence_quality_summary") or {}
    source_summary = verification_card.get("source_reliability_summary") or {}
    contradiction_summary = verification_card.get("contradiction_summary") or {}

    strength_component = _strength_score(strength)
    evidence_quality_score = int(quality.get("average_evidence_quality_score") or 0)
    source_trust = _source_trust_score({**source_summary, **debug}, source_candidates)
    confidence_score = int(policy_confidence.get("policy_confidence_score") or 0)
    human_adjustment = _human_feedback_adjustment(debug)
    contradiction_adjustment = _contradiction_adjustment(contradiction_summary)
    impact_component = _impact_gate(policy_impact, policy_confidence)

    base_score = (
        strength_component * 0.25
        + evidence_quality_score * 0.25
        + source_trust * 0.25
        + confidence_score * 0.15
        + impact_component * 0.10
    )
    final_score = _clamp(base_score + human_adjustment + contradiction_adjustment)
    calibrated_alert = _alert_from_score(
        final_score=final_score,
        evidence_quality_score=evidence_quality_score,
        source_trust_score=source_trust,
        strength_score=strength_component,
        contradiction_adjustment=contradiction_adjustment,
        human_feedback_adjustment=human_adjustment,
        policy_impact=policy_impact,
        policy_confidence=policy_confidence,
        official_mismatch=bool(verification_card.get("official_mismatch")),
    )

    calibrated = dict(final_decision or {})
    previous_alert = calibrated.get("policy_alert_level")
    calibrated["policy_alert_level"] = calibrated_alert
    calibrated["final_score"] = final_score
    calibrated["source_trust_score"] = source_trust
    calibrated["human_feedback_adjustment"] = human_adjustment
    calibrated["contradiction_adjustment"] = contradiction_adjustment
    calibrated["evidence_weighted_score"] = strength_component
    calibrated["evidence_quality_score"] = evidence_quality_score
    calibrated["calibration_reasons"] = [
        f"weighted evidence strength {strength_component}",
        f"average evidence quality {evidence_quality_score}",
        f"source trust {source_trust}",
        f"policy confidence {confidence_score}",
        f"impact gate {impact_component}",
        f"human feedback adjustment {human_adjustment}",
        f"contradiction adjustment {contradiction_adjustment}",
    ]
    decision_reasons = list(calibrated.get("decision_reasons") or [])
    if previous_alert != calibrated_alert:
        decision_reasons.append(f"calibrated alert {previous_alert} -> {calibrated_alert}")
    decision_reasons.extend(calibrated["calibration_reasons"])
    calibrated["decision_reasons"] = list(dict.fromkeys(decision_reasons))

    debug.update(
        {
            "final_score": final_score,
            "source_trust_score": source_trust,
            "human_feedback_adjustment": human_adjustment,
            "contradiction_adjustment": contradiction_adjustment,
            "evidence_weighted_score": strength_component,
            "calibrated_policy_alert_level": calibrated_alert,
        }
    )
    return calibrated, debug
