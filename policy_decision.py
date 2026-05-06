def _has_any(values: list[str], targets: set[str]) -> bool:
    return bool(set(values or []) & targets)


def _joined_reasons(policy_confidence: dict, policy_impact: dict) -> str:
    return " ".join(
        [
            " ".join(policy_confidence.get("confidence_reasons") or []),
            " ".join(policy_impact.get("impact_reasons") or []),
        ]
    )


def _market_signal(policy_confidence: dict, policy_impact: dict) -> tuple[list[str], list[str]]:
    groups = policy_impact.get("affected_groups") or []
    sectors = policy_impact.get("affected_sectors") or []
    direction = policy_impact.get("impact_direction")
    risk_level = policy_confidence.get("risk_level")
    reason_text = _joined_reasons(policy_confidence, policy_impact)
    reasons = []
    signals = []

    tightening_keywords = [
        "규제",
        "제한",
        "차단",
        "원천 차단",
        "대출 제한",
        "유주택자",
    ]
    support_pressure_keywords = [
        "지원 감소",
        "지원 부족",
        "실행 감소",
        "감소",
        "청년 지원 약화",
        "주거 사다리",
        "실효성",
        "버팀목",
    ]
    has_tightening_keyword = any(keyword in reason_text for keyword in tightening_keywords)
    has_support_pressure_keyword = any(keyword in reason_text for keyword in support_pressure_keywords)

    if (
        _has_any(sectors, {"housing", "household_finance", "real_estate"})
        and direction == "negative"
        and risk_level == "high"
        and has_tightening_keyword
    ):
        signals.append("housing_tightening_risk")
        reasons.append("housing finance restriction risk detected")

    if (
        _has_any(groups, {"young_adults", "renters"})
        and _has_any(sectors, {"housing", "household_finance"})
        and direction in {"negative", "uncertain"}
        and has_support_pressure_keyword
        and not ("housing_tightening_risk" in signals and has_tightening_keyword)
    ):
        signals.append("housing_support_pressure")
        reasons.append("housing support pressure for renters or young adults")

    if _has_any(sectors, {"banking", "household_finance"}) and direction in {"positive", "mixed"}:
        signals.append("consumer_credit_relief")
        reasons.append("positive consumer credit relief signal")

    if _has_any(groups, {"SMEs", "small_business_workers"}) and direction in {"positive", "mixed"}:
        signals.append("sme_finance_support")
        reasons.append("positive SME finance support signal")

    if _has_any(sectors, {"banking"}) and direction in {"positive", "mixed"}:
        signals.append("bank_margin_pressure")
        reasons.append("bank margin or product pricing pressure possible")

    if not signals and policy_confidence.get("verification_strength") == "none":
        signals.append("policy_uncertainty")
        reasons.append("official verification is insufficient")

    if not signals:
        signals.append("no_clear_signal")
        reasons.append("no clear market signal detected")

    return signals, reasons


def _policy_alert_level(policy_confidence: dict, policy_impact: dict) -> tuple[str, list[str]]:
    confidence_score = int(policy_confidence.get("policy_confidence_score") or 0)
    verification_strength = policy_confidence.get("verification_strength")
    risk_level = policy_confidence.get("risk_level")
    impact_level = policy_impact.get("impact_level")
    consumer_sensitivity = int(policy_impact.get("consumer_sensitivity") or 0)
    reasons = []

    if impact_level == "high" and (
        risk_level == "high" or consumer_sensitivity >= 80
    ):
        reasons.append("alert based on high impact")
        if verification_strength == "none":
            reasons.append("high impact but no usable official evidence")
            return "WATCH", reasons
        return "HIGH", reasons

    if confidence_score >= 60 and impact_level in {"high", "medium"}:
        reasons.append("medium alert based on verified confidence and material impact")
        return "MEDIUM", reasons

    if verification_strength == "none" and risk_level == "high":
        reasons.append("watch due to high risk with insufficient verification")
        return "WATCH", reasons

    if impact_level == "high" and verification_strength in {"none", "low"}:
        reasons.append("watch due to high impact with limited verification")
        return "WATCH", reasons

    if confidence_score < 25 and impact_level == "low":
        reasons.append("low alert because confidence and impact are low")
        return "LOW", reasons

    reasons.append("low alert because no high-confidence policy action signal was found")
    return "LOW", reasons


def _action_recommendation(
    alert_level: str,
    market_signals: list[str],
    policy_confidence: dict,
    policy_impact: dict,
) -> str:
    verification_strength = policy_confidence.get("verification_strength")

    if "housing_tightening_risk" in market_signals and verification_strength == "none":
        return "Monitor official FSC/FSS follow-up before treating as confirmed policy."
    if "housing_tightening_risk" in market_signals:
        return "Track official housing finance restrictions and lender implementation guidance."
    if "housing_support_pressure" in market_signals:
        return "Track youth housing finance support measures and budget changes."
    if "sme_finance_support" in market_signals and "consumer_credit_relief" in market_signals:
        return "Treat as verified product-level support and monitor borrower adoption."
    if "sme_finance_support" in market_signals:
        return "Monitor SME finance support terms and uptake by eligible workers."
    if "consumer_credit_relief" in market_signals:
        return "Monitor product-level rate relief and consumer eligibility conditions."
    if alert_level == "WATCH":
        return "Keep on watchlist until usable official evidence is available."
    return "No immediate action beyond routine monitoring."


def _decision_summary(
    alert_level: str,
    market_signals: list[str],
    policy_confidence: dict,
    policy_impact: dict,
) -> str:
    verification_strength = policy_confidence.get("verification_strength")
    impact_level = policy_impact.get("impact_level")
    consumer_sensitivity = int(policy_impact.get("consumer_sensitivity") or 0)

    if "housing_tightening_risk" in market_signals and verification_strength == "none":
        return "공식근거는 부족하지만 소비자 영향이 큰 주거금융 규제 가능성으로 WATCH가 필요합니다."
    if "housing_support_pressure" in market_signals:
        return "청년·임차인 주거금융 지원 압력이 확인되어 정책 후속 조치를 추적할 필요가 있습니다."
    if "sme_finance_support" in market_signals and "consumer_credit_relief" in market_signals:
        return "공식 근거가 있는 금융상품 지원 신호로 중소기업 근로자와 대출 소비자 영향을 함께 봐야 합니다."
    if alert_level == "HIGH":
        return f"공식 검증과 {impact_level} 영향이 결합되어 높은 수준의 정책 알림이 필요합니다."
    if alert_level == "WATCH" and consumer_sensitivity >= 80:
        return "공식 검증은 제한적이지만 소비자 민감도가 높아 관찰이 필요합니다."
    if alert_level == "MEDIUM":
        return "정책 신뢰도와 영향도가 중간 이상으로 확인되어 후속 모니터링이 필요합니다."
    return "현재 단계에서는 명확한 정책 실행 신호가 낮아 일반 모니터링 대상으로 판단됩니다."


def make_final_decision(policy_confidence: dict, policy_impact: dict) -> dict:
    alert_level, alert_reasons = _policy_alert_level(policy_confidence, policy_impact)
    market_signals, signal_reasons = _market_signal(policy_confidence, policy_impact)
    recommendation = _action_recommendation(
        alert_level,
        market_signals,
        policy_confidence,
        policy_impact,
    )
    summary = _decision_summary(
        alert_level,
        market_signals,
        policy_confidence,
        policy_impact,
    )

    reasons = []
    reasons.extend(alert_reasons)
    reasons.extend(signal_reasons)

    verification_strength = policy_confidence.get("verification_strength")
    evidence_grade = policy_confidence.get("confidence_evidence_grade")
    consumer_sensitivity = int(policy_impact.get("consumer_sensitivity") or 0)
    business_sensitivity = int(policy_impact.get("business_sensitivity") or 0)

    if verification_strength == "none":
        reasons.append("no usable official evidence")
    else:
        reasons.append(f"verification strength {verification_strength}")

    if evidence_grade:
        reasons.append(f"verified official evidence grade {evidence_grade}")

    if consumer_sensitivity >= 80:
        reasons.append("high consumer sensitivity")

    if business_sensitivity >= 70:
        reasons.append("high business sensitivity")

    return {
        "policy_alert_level": alert_level,
        "market_signal": market_signals,
        "action_recommendation": recommendation,
        "decision_summary": summary,
        "decision_reasons": reasons,
    }


def print_final_decision(final_decision: dict):
    print("\n----- Final decision -----")
    print("policy_alert_level:", final_decision.get("policy_alert_level"))
    print("final_score:", final_decision.get("final_score"))
    print("source_trust_score:", final_decision.get("source_trust_score"))
    print("human_feedback_adjustment:", final_decision.get("human_feedback_adjustment"))
    print("contradiction_adjustment:", final_decision.get("contradiction_adjustment"))
    print("market_signal:", ", ".join(final_decision.get("market_signal") or []))
    print("action_recommendation:", final_decision.get("action_recommendation"))
    print("decision_summary:", final_decision.get("decision_summary"))
    print("decision_reasons:")
    for reason in final_decision.get("decision_reasons") or []:
        print("-", reason)
