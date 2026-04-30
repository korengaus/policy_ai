GRADE_SCORES = {
    "A": 100,
    "B": 80,
    "C": 60,
    "D": 30,
    "F": 0,
    None: 0,
}

HIGH_RISK_KEYWORDS = [
    "\uaddc\uc81c",
    "\ucc28\ub2e8",
    "\uae08\uc9c0",
    "\uc6d0\ucc9c \ucc28\ub2e8",
    "\ub300\ucd9c \uc81c\ud55c",
    "\uac10\ub3c5",
    "\uc870\uc0ac \ucc29\uc218",
]
MEDIUM_RISK_KEYWORDS = [
    "\uae08\ub9ac \ubcc0\uacbd",
    "\uae08\ub9ac",
    "\uc9c0\uc6d0 \ucd95\uc18c",
    "\uc2e4\ud589 \uac10\uc18c",
    "\uc81c\ub3c4 \ubcc0\uacbd",
    "\ud61c\ud0dd \ubcc0\uacbd",
]
LOW_RISK_KEYWORDS = [
    "\ud589\uc0ac",
    "\ubc1c\uc5b8",
    "\uc81c\uc5b8",
    "\uc124\uba85",
    "\uc804\ub9dd",
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


def calculate_policy_confidence(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
    official_evidence_results: list[dict],
    evidence_comparison: dict,
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
    if not official_usable:
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
    print("\n----- Policy confidence -----")
    print("policy_confidence_score:", policy_confidence.get("policy_confidence_score"))
    print("verification_strength:", policy_confidence.get("verification_strength"))
    print("risk_level:", policy_confidence.get("risk_level"))
    print("action_priority:", policy_confidence.get("action_priority"))
    print("confidence_evidence_source:", policy_confidence.get("confidence_evidence_source"))
    print("confidence_evidence_title:", policy_confidence.get("confidence_evidence_title"))
    print("confidence_evidence_url:", policy_confidence.get("confidence_evidence_url"))
    print("confidence_evidence_grade:", policy_confidence.get("confidence_evidence_grade"))
    print("confidence_reasons:")
    for reason in policy_confidence.get("confidence_reasons") or []:
        print("-", reason)
