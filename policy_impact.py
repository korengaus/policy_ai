GROUP_RULES = {
    "homeowners": ["\uc720\uc8fc\ud0dd\uc790", "1\uc8fc\ud0dd\uc790", "\uc8fc\ud0dd\ubcf4\uc720", "\uc8fc\ub2f4\ub300", "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c"],
    "renters": ["\uc804\uc138", "\uc804\uc138\ub300\ucd9c", "\uc804\uc138\uc790\uae08", "\uc784\ucc28", "\ubcf4\uc99d\uae08", "\uc6d4\uc138", "\uc8fc\uac70\ube44"],
    "young_adults": ["\uccad\ub144", "\uc0ac\ud68c \ucd08\ub144\uc0dd", "\ucd08\uae30 \uc790\uae08"],
    "small_business_workers": ["\uc911\uc18c\uae30\uc5c5 \uc7ac\uc9c1", "\uc911\uc18c\uae30\uc5c5 \uadfc\ub85c\uc790", "\uadfc\ub85c\uc790", "\uc7ac\uc9c1\uc790"],
    "SMEs": ["\uc911\uc18c\uae30\uc5c5", "\uc18c\uc0c1\uacf5\uc778", "\uace8\ubaa9\uc0c1\uad8c", "\uc804\ud1b5\uc2dc\uc7a5"],
    "banks": ["\uc740\ud589", "\uae08\uc735\uad8c", "\uae08\uc735\ud68c\uc0ac", "\ub300\ucd9c\uae30\uad00"],
    "public_financial_institutions": ["\uae30\uc5c5\uc740\ud589", "IBK", "\uc8fc\ud0dd\ub3c4\uc2dc\uae30\uae08", "\uc815\ucc45\uae08\uc735"],
    "investors": ["\ud22c\uc790\uc790", "\uc790\ubcf8\uc2dc\uc7a5", "STO", "\uc99d\uad8c", "\uac00\uc0c1\uc790\uc0b0"],
    "general_consumers": ["\uc18c\ube44\uc790", "\uace0\uac1d", "\uad6d\ubbfc", "\uc77c\ubc18"],
}

SECTOR_RULES = {
    "housing": ["\uc8fc\uac70", "\uc8fc\ud0dd", "\uc804\uc138", "\uc6d4\uc138", "\uc8fc\uac70\ube44"],
    "household_finance": ["\uac00\uacc4\ubd80\ucc44", "\uc804\uc138\ub300\ucd9c", "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c", "\uc8fc\ub2f4\ub300", "\ub300\ucd9c"],
    "banking": ["\uc740\ud589", "\uae08\uc735\uad8c", "\uae30\uc5c5\uc740\ud589", "IBK"],
    "SME_finance": ["\uc911\uc18c\uae30\uc5c5", "\uc18c\uc0c1\uacf5\uc778", "\uadfc\ub85c\uc790\uc0dd\ud65c\uc548\uc815\uc790\uae08"],
    "real_estate": ["\ubd80\ub3d9\uc0b0", "\uc8fc\ud0dd", "\uc804\uc138\uc2dc\uc7a5", "\uaddc\uc81c\uc9c0\uc5ed"],
    "public_policy": ["\uc815\ucc45", "\uc81c\ub3c4", "\uc9c0\uc6d0", "\uaddc\uc81c", "\uc870\uc0ac \ucc29\uc218"],
    "capital_market": ["\uc790\ubcf8\uc2dc\uc7a5", "STO", "\uc99d\uad8c", "\uac00\uc0c1\uc790\uc0b0"],
    "consumer_finance": ["\uae08\ub9ac", "\uc774\uc790", "\ub300\ucd9c", "\uc18c\ube44\uc790", "\uc2e0\uc6a9\ub300\ucd9c"],
}

HIGH_IMPACT_KEYWORDS = [
    "\uaddc\uc81c",
    "\ucc28\ub2e8",
    "\uae08\uc9c0",
    "\uae08\ub9ac \uc778\ud558",
    "\uae08\ub9ac \uc0c1\uc2b9",
    "\uae08\ub9ac\uac10\uba74",
    "\ub300\ucd9c \uc81c\ud55c",
    "\uc9c0\uc6d0 \ucd95\uc18c",
    "\uc9c0\uc6d0 \ud655\ub300",
    "\ub9cc\uae30 \uc6d0\ucc9c \ucc28\ub2e8",
]
MEDIUM_IMPACT_KEYWORDS = [
    "\uc870\uc0ac \ucc29\uc218",
    "\uc81c\ub3c4 \uac80\ud1a0",
    "\uac80\ud1a0",
    "\uc2e4\ud6a8\uc131",
    "\uc2e4\ud589 \uac10\uc18c",
    "\ud604\ud669 \uc870\uc0ac",
]
LOW_IMPACT_KEYWORDS = ["\ud589\uc0ac", "\ubc1c\uc5b8", "\uc81c\uc5b8", "\uc804\ub9dd", "\uc124\uba85"]

POSITIVE_KEYWORDS = ["\uae08\ub9ac\uac10\uba74", "\uae08\ub9ac \uac10\uba74", "\uc9c0\uc6d0 \ud655\ub300", "\uc9c0\uc6d0", "\ud61c\ud0dd", "\uc778\ud558", "\uc644\ud654"]
NEGATIVE_KEYWORDS = ["\uc81c\ud55c", "\ucc28\ub2e8", "\uae08\uc9c0", "\ucd95\uc18c", "\uac10\uc18c", "\uaddc\uc81c", "\ubd80\ub2f4", "\uc5b4\ub824\uc6cc"]
UNCERTAIN_KEYWORDS = ["\uc870\uc0ac", "\uac80\ud1a0", "\uc804\ub9dd", "\uac00\ub2a5\uc131", "\uc9c0\uc801"]

CONSUMER_HOUSING_FINANCE_KEYWORDS = [
    "\uc804\uc138\ub300\ucd9c",
    "\uc8fc\ub2f4\ub300",
    "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
    "\uc6d4\uc138",
    "\uc8fc\uac70\ube44",
    "\ub300\ucd9c \uc81c\ud55c",
    "\ucc28\ub2e8",
    "\uaddc\uc81c",
]
MARKET_KEYWORDS = ["\ub300\ucd9c", "\uae08\ub9ac", "\uc740\ud589", "\ubd80\ub3d9\uc0b0", "\uc8fc\ud0dd", "\uae08\uc735\uad8c"]
BUSINESS_KEYWORDS = ["\uc911\uc18c\uae30\uc5c5", "\uadfc\ub85c\uc790", "\uc740\ud589", "\uae30\uc5c5\uc740\ud589", "\uae30\uc5c5", "\uc0ac\uc5c5\uc790", "\uc18c\uc0c1\uacf5\uc778", "IBK"]


def _policy_claims_text(policy_claims) -> str:
    parts = []

    for claim in policy_claims or []:
        if isinstance(claim, dict):
            parts.append(str(claim.get("sentence") or ""))
        else:
            parts.append(str(claim or ""))

    return " ".join(parts)


def _detect_by_rules(text: str, rules: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    detected = []
    reasons = []

    for label, keywords in rules.items():
        matched = [keyword for keyword in keywords if keyword and keyword in text]
        if matched:
            detected.append(label)
            reasons.append(f"detected {label}: {matched[0]}")

    return detected, reasons


def _impact_level(text: str) -> tuple[str, str | None]:
    for keyword in HIGH_IMPACT_KEYWORDS:
        if keyword in text:
            return "high", keyword
    for keyword in MEDIUM_IMPACT_KEYWORDS:
        if keyword in text:
            return "medium", keyword
    for keyword in LOW_IMPACT_KEYWORDS:
        if keyword in text:
            return "low", keyword
    return "low", None


def _impact_direction(text: str) -> tuple[str, list[str]]:
    positive = [keyword for keyword in POSITIVE_KEYWORDS if keyword in text]
    negative = [keyword for keyword in NEGATIVE_KEYWORDS if keyword in text]
    uncertain = [keyword for keyword in UNCERTAIN_KEYWORDS if keyword in text]

    if positive and negative:
        return "mixed", [f"positive signal: {positive[0]}", f"negative signal: {negative[0]}"]
    if negative:
        return "negative", [f"negative impact from {negative[0]}"]
    if positive:
        return "positive", [f"positive impact from {positive[0]}"]
    if uncertain:
        return "uncertain", [f"uncertain impact from {uncertain[0]}"]
    return "uncertain", ["impact direction is uncertain"]


def _score_sensitivity(text: str, keywords: list[str], base: int = 10) -> int:
    score = base
    hits = 0

    for keyword in keywords:
        if keyword in text:
            hits += 1

    score += min(80, hits * 15)
    return max(0, min(100, score))


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def analyze_policy_impact(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
) -> dict:
    text = " ".join(
        [
            news_title or "",
            news_summary or "",
            (article_body or "")[:2500],
            _policy_claims_text(policy_claims),
        ]
    )

    affected_groups, group_reasons = _detect_by_rules(text, GROUP_RULES)
    affected_sectors, sector_reasons = _detect_by_rules(text, SECTOR_RULES)
    impact_level, impact_keyword = _impact_level(text)
    impact_direction, direction_reasons = _impact_direction(text)

    market_sensitivity = _score_sensitivity(
        text,
        ["\uae08\uc735\uc2dc\uc7a5", "\uae08\uc735\uad8c", "\uc740\ud589", "\ub300\ucd9c", "\uae08\ub9ac", "\ubd80\ub3d9\uc0b0", "\uc8fc\ud0dd", "\uc804\uc138"],
        base=15,
    )
    consumer_sensitivity = _score_sensitivity(
        text,
        ["\uccad\ub144", "\uc804\uc138", "\uc8fc\uac70\ube44", "\uc6d4\uc138", "\ub300\ucd9c\uc790", "\uc18c\ube44\uc790", "\ubcf4\uc99d\uae08", "\uc8fc\uac70"],
        base=10,
    )
    business_sensitivity = _score_sensitivity(
        text,
        ["\uc911\uc18c\uae30\uc5c5", "\uae30\uc5c5\uc740\ud589", "IBK", "\uadfc\ub85c\uc790", "\uc0ac\uc5c5\uc790", "\uc740\ud589", "\uc18c\uc0c1\uacf5\uc778"],
        base=10,
    )

    has_consumer_housing_group = bool({"renters", "homeowners"} & set(affected_groups))
    has_consumer_housing_sector = bool({"housing", "household_finance"} & set(affected_sectors))
    has_consumer_housing_keyword = _has_any(text, CONSUMER_HOUSING_FINANCE_KEYWORDS)

    if has_consumer_housing_group and has_consumer_housing_sector and has_consumer_housing_keyword:
        consumer_sensitivity = max(consumer_sensitivity, 70)

    if (
        impact_level == "high"
        and impact_direction == "negative"
        and has_consumer_housing_group
        and has_consumer_housing_keyword
    ):
        consumer_sensitivity = max(consumer_sensitivity, 85)

    if _has_any(text, MARKET_KEYWORDS):
        market_sensitivity = max(market_sensitivity, 60)

    if not _has_any(text, BUSINESS_KEYWORDS):
        business_sensitivity = min(business_sensitivity, 35)

    reasons = []
    reasons.extend(group_reasons)
    reasons.extend(sector_reasons[:3])
    if impact_keyword:
        reasons.append(f"{impact_level} impact keyword detected: {impact_keyword}")
    reasons.extend(direction_reasons)

    if ("housing" in affected_sectors or "renters" in affected_groups) and consumer_sensitivity >= 70:
        reasons.append("high consumer sensitivity due to housing cost")
    if "banking" in affected_sectors or "banks" in affected_groups:
        reasons.append("market sensitivity due to banking/loan exposure")
    if "SMEs" in affected_groups or "small_business_workers" in affected_groups:
        reasons.append("business sensitivity due to SME/workforce exposure")

    return {
        "affected_groups": affected_groups,
        "affected_sectors": affected_sectors,
        "impact_level": impact_level,
        "impact_direction": impact_direction,
        "impact_reasons": reasons,
        "market_sensitivity": market_sensitivity,
        "consumer_sensitivity": consumer_sensitivity,
        "business_sensitivity": business_sensitivity,
    }


def print_policy_impact(policy_impact: dict):
    print("\n----- Policy impact -----")
    print("affected_groups:", ", ".join(policy_impact.get("affected_groups") or []))
    print("affected_sectors:", ", ".join(policy_impact.get("affected_sectors") or []))
    print("impact_level:", policy_impact.get("impact_level"))
    print("impact_direction:", policy_impact.get("impact_direction"))
    print("market_sensitivity:", policy_impact.get("market_sensitivity"))
    print("consumer_sensitivity:", policy_impact.get("consumer_sensitivity"))
    print("business_sensitivity:", policy_impact.get("business_sensitivity"))
    print("impact_reasons:")
    for reason in policy_impact.get("impact_reasons") or []:
        print("-", reason)
