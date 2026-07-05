
from structured_logging import get_logger

# audit \u00a71.5 #3 re-audit (2026-05-26): LOW_RISK_KEYWORDS is set-equal
# to policy_impact.LOW_IMPACT_KEYWORDS but with the trailing \uc124\uba85 \u2192
# \uc804\ub9dd order preserved here. Lifted to korean_constants.py to remove
# literal duplication while preserving each consumer's first-match
# behavior verbatim. HIGH_RISK_KEYWORDS / MEDIUM_RISK_KEYWORDS are
# INTENTIONALLY separate from the policy_impact HIGH/MEDIUM tuples \u2014
# they measure *risk signaling* (regulatory tightening, supervision)
# whereas HIGH/MEDIUM_IMPACT_KEYWORDS measure *impact magnitude*
# (specific market actions). Unifying would conflate two distinct
# scoring axes \u2014 see docs/KOREAN_CONSTANTS.md re-audit table.
from korean_constants import (
    LOW_RISK_KEYWORDS_POLICY_CONFIDENCE as LOW_RISK_KEYWORDS,
)
from official_evidence_resolution import _is_strong_primary_document_match

log = get_logger(__name__)
GRADE_SCORES = {
    "A": 100,
    "B": 80,
    "C": 60,
    "D": 30,
    "F": 0,
    None: 0,
}

# audit \u00a71.5 #3 re-audit (2026-05-26): intentionally separate from
# policy_impact.HIGH_IMPACT_KEYWORDS. 4 items overlap (\uaddc\uc81c / \ucc28\ub2e8 /
# \uae08\uc9c0 / \ub300\ucd9c \uc81c\ud55c) but the two lists score different axes.
HIGH_RISK_KEYWORDS = [
    "\uaddc\uc81c",
    "\ucc28\ub2e8",
    "\uae08\uc9c0",
    "\uc6d0\ucc9c \ucc28\ub2e8",
    "\ub300\ucd9c \uc81c\ud55c",
    "\uac10\ub3c5",
    "\uc870\uc0ac \ucc29\uc218",
]
# audit \u00a71.5 #3 re-audit (2026-05-26): intentionally separate from
# policy_impact.MEDIUM_IMPACT_KEYWORDS. Only 1 item overlaps (\uc2e4\ud589 \uac10\uc18c).
MEDIUM_RISK_KEYWORDS = [
    "\uae08\ub9ac \ubcc0\uacbd",
    "\uae08\ub9ac",
    "\uc9c0\uc6d0 \ucd95\uc18c",
    "\uc2e4\ud589 \uac10\uc18c",
    "\uc81c\ub3c4 \ubcc0\uacbd",
    "\ud61c\ud0dd \ubcc0\uacbd",
]


def _joined_policy_claims(policy_claims) -> str:
    parts = []

    for claim in policy_claims or []:
        if isinstance(claim, dict):
            parts.append(str(claim.get("sentence") or ""))
        else:
            parts.append(str(claim or ""))

    return " ".join(parts)


def _best_official_evidence(official_evidence_results: list[dict]) -> dict:
    candidates = [
        result
        for result in official_evidence_results or []
        if (result.get("usable") or result.get("weakly_usable"))
        and not result.get("should_exclude_from_verification")
        and result.get("evidence_grade") != "F"
    ]
    if not candidates:
        return {}

    def sort_key(result: dict) -> tuple:
        return (
            1 if result.get("usable") else 0,
            1 if result.get("weakly_usable") else 0,
            GRADE_SCORES.get(result.get("evidence_grade"), 0),
            result.get("document_relevance_score") or 0,
            result.get("reliability_score") or 0,
        )

    return sorted(candidates, key=sort_key, reverse=True)[0]


def _risk_level(text: str) -> tuple[str, str | None]:
    normalized = text or ""

    for keyword in HIGH_RISK_KEYWORDS:
        if keyword in normalized:
            return "high", keyword

    for keyword in MEDIUM_RISK_KEYWORDS:
        if keyword in normalized:
            return "medium", keyword

    for keyword in LOW_RISK_KEYWORDS:
        if keyword in normalized:
            return "low", keyword

    return "low", None


def _verification_strength(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 50:
        return "medium"
    if score >= 25:
        return "low"
    return "none"


def _action_priority(risk_level: str, verification_strength: str) -> str:
    if risk_level == "high" and verification_strength in {"high", "medium"}:
        return "high"
    if risk_level == "medium" or verification_strength == "medium":
        return "medium"
    return "low"


# M22-1 — Lane B (Policy-Briefing) conservative confidence ceiling. A genuine
# strong Lane-B official body match raises confidence to a FIXED 70 (never
# higher) with verification_strength forced to "low". 70 < 85 blocks the
# draft_verified gate; "low" ∉ _STRONG_VERIFICATION_STRENGTHS blocks the
# snippet draft_verified gate (both in verification_card._verdict_label). Max
# achievable label is therefore draft_likely_true — "likely true, human still
# confirms".
LANE_B_STRONG_CONFIDENCE = 70


def calculate_policy_confidence(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
    official_evidence_results: list[dict],
    evidence_comparison: dict,
    *,
    primary_document_match: dict | None = None,
) -> dict:
    best_evidence = _best_official_evidence(official_evidence_results)
    official_usable = bool(best_evidence)
    semantic_support_score = int(evidence_comparison.get("semantic_support_score") or 0)
    document_relevance_score = int(best_evidence.get("document_relevance_score") or 0) if official_usable else 0
    evidence_grade = best_evidence.get("evidence_grade")
    evidence_grade_score = GRADE_SCORES.get(evidence_grade, 0)
    reliability_score = int(best_evidence.get("reliability_score") or 0) if official_usable else 0
    source_reliability_score = min(100, max(0, reliability_score * 20))
    has_conflict = bool(evidence_comparison.get("conflict_signals") or evidence_comparison.get("semantic_conflict_signals"))

    no_conflict_score = 0 if has_conflict else 100
    raw_score = (
        semantic_support_score * 0.35
        + document_relevance_score * 0.20
        + evidence_grade_score * 0.20
        + source_reliability_score * 0.15
        + no_conflict_score * 0.10
    )

    policy_confidence_score = max(0, min(100, round(raw_score)))
    lane_b_raised = False
    if not official_usable:
        # M22-1 — Lane A↔B join: when Lane A has no usable official doc but Lane
        # B carries a GENUINE strong Policy-Briefing official_body_match, raise
        # to a fixed conservative ceiling instead of clamping to 20. Gated on
        # not-official_usable so any Lane-A-usable case is byte-identical, and on
        # _is_strong_primary_document_match (Policy-Briefing marker + real
        # body-match + strong + score>=75), so existing fixtures with no Lane-B
        # match stay byte-identical. official_body_match is read-only here
        # (M19-3 guard); never set/faked.
        if _is_strong_primary_document_match(primary_document_match):
            policy_confidence_score = LANE_B_STRONG_CONFIDENCE
            verification_strength = "low"
            lane_b_raised = True
        else:
            # audit §1.5 #5 (2026-05-26): no-official-doc confidence clamp.
            # The 20 ceiling forces verification_strength = "none" via the
            # _verification_strength boundaries below (>= 25 = "low"). This
            # cascades into many P2 paths — see docs/MAGIC_THRESHOLDS.md §4.
            # Symmetric with the `unknown` tier value in
            # _source_confidence_score (line ~153).
            policy_confidence_score = min(20, policy_confidence_score)
            verification_strength = "none"
    else:
        verification_strength = _verification_strength(policy_confidence_score)

    risk_text = " ".join(
        [
            news_title or "",
            news_summary or "",
            (article_body or "")[:1500],
            _joined_policy_claims(policy_claims),
        ]
    )
    risk_level, risk_keyword = _risk_level(risk_text)
    action_priority = _action_priority(risk_level, verification_strength)

    if official_usable:
        reasons = [
            f"semantic support {semantic_support_score}",
            f"document relevance {document_relevance_score}",
            f"evidence grade {evidence_grade or 'None'}",
            f"source reliability {reliability_score}/5",
        ]
        reasons.append("official document usable")
    else:
        reasons = [
            f"semantic support {semantic_support_score}",
            "no usable official document",
        ]
        if lane_b_raised:
            reasons.append(
                "raised by strong Policy Briefing official body match (Lane B)"
            )

    if has_conflict:
        reasons.append("conflict signals detected")
    else:
        reasons.append("no conflict signals")

    if risk_keyword:
        reasons.append(f"{risk_level} risk keyword detected: {risk_keyword}")
    else:
        reasons.append("no explicit high/medium risk keyword detected")

    return {
        "policy_confidence_score": policy_confidence_score,
        "verification_strength": verification_strength,
        "risk_level": risk_level,
        "action_priority": action_priority,
        "confidence_evidence_source": best_evidence.get("source_name") if official_usable else None,
        "confidence_evidence_title": best_evidence.get("document_title") if official_usable else None,
        "confidence_evidence_url": best_evidence.get("selected_document_url") if official_usable else None,
        "confidence_evidence_grade": evidence_grade if official_usable else None,
        "confidence_reasons": reasons,
    }


def print_policy_confidence(policy_confidence: dict):
    log.info("\n----- Policy confidence -----")
    log.info(f"policy_confidence_score: {policy_confidence.get('policy_confidence_score')}")
    log.info(f"verification_strength: {policy_confidence.get('verification_strength')}")
    log.info(f"risk_level: {policy_confidence.get('risk_level')}")
    log.info(f"action_priority: {policy_confidence.get('action_priority')}")
    log.info(f"confidence_evidence_source: {policy_confidence.get('confidence_evidence_source')}")
    log.info(f"confidence_evidence_title: {policy_confidence.get('confidence_evidence_title')}")
    log.info(f"confidence_evidence_url: {policy_confidence.get('confidence_evidence_url')}")
    log.info(f"confidence_evidence_grade: {policy_confidence.get('confidence_evidence_grade')}")
    log.info("confidence_reasons:")
    for reason in policy_confidence.get("confidence_reasons") or []:
        log.info(f'- {reason}')
