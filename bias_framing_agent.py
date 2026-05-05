from datetime import datetime, timezone
import hashlib
import re


SENSATIONAL_TERMS = [
    "충격",
    "논란",
    "파장",
    "대란",
    "폭탄",
    "위기",
    "붕괴",
    "폭락",
    "급등",
    "불안",
    "공포",
    "초비상",
    "강타",
    "역대급",
    "심각",
    "위험",
    "최악",
    "긴급",
    "비상",
    "무너진다",
    "대혼란",
    "혼란",
    "충격파",
    "빨간불",
    "직격탄",
    "초읽기",
    "도미노",
    "먹구름",
    "경고등",
    "비상등",
    "요동",
    "흔들",
    "추락",
    "얼어붙",
    "불붙",
    "패닉",
    "벼랑",
    "고통",
    "압박",
    "쇼크",
    "초유",
    "역풍",
    "불똥",
    "대폭락",
    "역대급",
    "disaster",
    "crisis",
    "shock",
    "shocking",
    "collapse",
    "fear",
    "panic",
]

UNCERTAINTY_TERMS = [
    "가능성",
    "전망",
    "예상",
    "관측",
    "보인다",
    "분석된다",
    "우려",
    "추정",
    "전망된다",
    "예상된다",
    "관측된다",
    "가능성이 있다",
    "가능성이 제기",
    "현실화되나",
    "나설 수",
    "할 수 있다",
    "것으로 보인다",
    "것으로 예상",
    "것으로 전망",
    "검토",
    "추진",
    "논의",
    "could",
    "may",
    "might",
    "likely",
    "reportedly",
]

PRO_POLICY_TERMS = ["지원", "완화", "혜택", "감면", "확대", "안정", "보호"]
ANTI_POLICY_TERMS = ["규제", "강화", "제한", "억제", "차단", "금지", "불허", "축소", "부담"]
PRO_MARKET_TERMS = ["상승", "호황", "회복", "활성화", "투자", "성장"]
ANTI_MARKET_TERMS = ["하락", "침체", "위기", "폭락", "불안", "위험", "대란"]
PRO_GOVERNMENT_TERMS = ["정부가 지원", "당국이 지원", "공식 발표", "대책 마련"]
ANTI_GOVERNMENT_TERMS = ["실패", "논란", "비판", "졸속", "혼선"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bias_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _contains_terms(text: str, terms: list[str]) -> list[str]:
    lowered = (text or "").lower()
    found = []
    for term in terms:
        if term.lower() in lowered and term not in found:
            found.append(term)
    return found


def _sentence_has_uncertain_but_definitive(text: str, uncertainty_terms: list[str]) -> bool:
    if not uncertainty_terms:
        return False
    definitive_terms = ["확정", "반드시", "무조건", "원천 차단", "된다", "했다", "시행"]
    return any(term in (text or "") for term in definitive_terms)


def _claim_snippets(
    claim_index: int,
    evidence_snippets: list[dict],
    claim_evidence_map: dict,
) -> list[dict]:
    mapped_ids = set(claim_evidence_map.get(str(claim_index)) or claim_evidence_map.get(claim_index) or [])
    if mapped_ids:
        return [item for item in evidence_snippets or [] if item.get("evidence_id") in mapped_ids]
    return [
        item
        for item in evidence_snippets or []
        if int(item.get("claim_index") or -1) == claim_index
    ]


def _bias_direction(text: str) -> str:
    pro_policy = len(_contains_terms(text, PRO_POLICY_TERMS))
    anti_policy = len(_contains_terms(text, ANTI_POLICY_TERMS))
    pro_market = len(_contains_terms(text, PRO_MARKET_TERMS))
    anti_market = len(_contains_terms(text, ANTI_MARKET_TERMS))
    pro_government = len(_contains_terms(text, PRO_GOVERNMENT_TERMS))
    anti_government = len(_contains_terms(text, ANTI_GOVERNMENT_TERMS))

    scores = {
        "pro_policy": pro_policy,
        "anti_policy": anti_policy,
        "pro_market": pro_market,
        "anti_market": anti_market,
        "pro_government": pro_government,
        "anti_government": anti_government,
    }
    best, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score == 0:
        return "neutral"
    tied = [name for name, score in scores.items() if score == best_score]
    return best if len(tied) == 1 else "unclear"


def _level(score: int) -> str:
    if score >= 51:
        return "high"
    if score >= 21:
        return "medium"
    return "low"


def _source_context_for_claim(claim_index: int, source_candidates: list[dict]) -> dict:
    related = [
        source
        for source in source_candidates or []
        if int(source.get("claim_index") or -1) == claim_index
    ]
    official_count = sum(
        1
        for source in related
        if source.get("source_type") in {"official_government", "public_institution"}
    )
    raw_count = sum(1 for source in related if source.get("raw_text_available"))
    fallback_only = bool(related) and not official_count and all(
        source.get("source_type") == "search_fallback_news" for source in related
    )
    return {
        "official_count": official_count,
        "raw_count": raw_count,
        "fallback_only": fallback_only,
    }


def _contradiction_for_claim(claim_index: int, contradiction_checks: list[dict]) -> dict:
    for check in contradiction_checks or []:
        if int(check.get("claim_index") or -1) == claim_index:
            return check
    return {}


def analyze_bias_framing(
    *,
    normalized_claims: list[dict],
    news_title: str = "",
    news_summary: str = "",
    article_body: str = "",
    source_candidates: list[dict] | None = None,
    evidence_snippets: list[dict] | None = None,
    claim_evidence_map: dict | None = None,
    contradiction_checks: list[dict] | None = None,
) -> dict:
    checked_at = _now_iso()
    analyses = []
    title_terms = _contains_terms(news_title, SENSATIONAL_TERMS)
    total_sensational_count = 0
    total_uncertainty_count = 0
    final_scores = []

    for index, claim in enumerate(normalized_claims or []):
        claim_text = claim.get("claim_text") or ""
        combined_text = " ".join(
            [
                news_title or "",
                news_summary or "",
                article_body or "",
                claim_text,
            ]
        )
        sensational = _contains_terms(combined_text, SENSATIONAL_TERMS)
        uncertainty = _contains_terms(combined_text, UNCERTAINTY_TERMS)
        loaded_terms = list(dict.fromkeys(sensational + uncertainty))
        snippets = _claim_snippets(index, evidence_snippets or [], claim_evidence_map or {})
        source_context = _source_context_for_claim(index, source_candidates or [])
        contradiction = _contradiction_for_claim(index, contradiction_checks or [])

        score = 0
        reasons = []
        if title_terms:
            score += 20
            reasons.append("기사 제목에 강한 감정 표현이 포함됨")
        body_sensational = [term for term in sensational if term not in title_terms]
        if body_sensational:
            score += min(50, len(body_sensational) * 10)
            reasons.append("감정적/자극적 표현이 포함됨")
        if uncertainty:
            score += min(25, len(uncertainty) * 5)
            reasons.append("불확실성 표현이 포함됨")
        if _sentence_has_uncertain_but_definitive(combined_text, uncertainty):
            score += 15
            reasons.append("불확실 표현과 확정적 결론 표현이 함께 나타남")

        contradiction_status = contradiction.get("contradiction_status") or ""
        if contradiction_status == "possible_contradiction":
            score += 20
            reasons.append("반박 가능성 신호가 있어 표현 검토가 필요함")
        elif contradiction_status in {"likely_contradiction", "confirmed_contradiction"}:
            score += 30
            reasons.append("강한 모순 가능성 신호가 있어 표현 검토가 필요함")

        low_confidence_evidence = [
            snippet
            for snippet in snippets
            if snippet.get("extraction_confidence") == "low"
            or snippet.get("supports_claim") in {"unclear", "not_enough_info"}
        ]
        if low_confidence_evidence:
            score += 15
            reasons.append("근거 신뢰도가 낮거나 불명확한 근거가 포함됨")
        if source_context["fallback_only"]:
            score += 15
            reasons.append("공식 출처 없이 검색 fallback 뉴스만 확보됨")

        score = max(0, min(score, 100))
        total_sensational_count += len(sensational)
        total_uncertainty_count += len(uncertainty)
        final_scores.append(score)
        framing_level = _level(score)
        needs_editor_review = (
            framing_level == "high"
            or bool(low_confidence_evidence)
            or source_context["fallback_only"]
            or contradiction_status in {"possible_contradiction", "likely_contradiction", "confirmed_contradiction"}
        )
        if not reasons:
            reasons.append("자극적 표현이나 뚜렷한 편향 신호가 낮음")

        analyses.append(
            {
                "bias_id": _bias_id(str(index), claim_text, checked_at),
                "claim_index": index,
                "claim_text": claim_text,
                "framing_score": score,
                "framing_level": framing_level,
                "bias_direction": _bias_direction(combined_text),
                "emotional_language_detected": bool(sensational),
                "loaded_terms": loaded_terms,
                "uncertainty_language": uncertainty,
                "sensational_phrases": sensational,
                "framing_reason": "; ".join(reasons),
                "needs_editor_review": needs_editor_review,
                "checked_at": checked_at,
            }
        )

    summary = summarize_bias_framing(analyses)
    editor_review_count = sum(1 for item in analyses if item.get("needs_editor_review"))
    print(f"[BiasFramingAgent] checked {len(analyses)} claims")
    print(f"[BiasFramingAgent] detected sensational: {total_sensational_count}")
    print(f"[BiasFramingAgent] detected uncertainty: {total_uncertainty_count}")
    print(f"[BiasFramingAgent] final score: {max(final_scores) if final_scores else 0}")
    print(f"[BiasFramingAgent] high framing count: {summary.get('high_framing_count', 0)}")
    print(f"[BiasFramingAgent] editor review needed: {editor_review_count}")
    return {
        "bias_framing_analysis": analyses,
        "bias_framing_summary": summary,
    }


def summarize_bias_framing(bias_framing_analysis: list[dict]) -> dict:
    analyses = bias_framing_analysis or []
    high_count = sum(1 for item in analyses if item.get("framing_level") == "high")
    medium_count = sum(1 for item in analyses if item.get("framing_level") == "medium")
    low_count = sum(1 for item in analyses if item.get("framing_level") == "low")
    emotional_count = sum(1 for item in analyses if item.get("emotional_language_detected"))
    uncertainty_count = sum(1 for item in analyses if item.get("uncertainty_language"))
    editor_review_count = sum(1 for item in analyses if item.get("needs_editor_review"))

    if high_count:
        risk = "high"
    elif medium_count:
        risk = "medium"
    elif editor_review_count:
        risk = "watch"
    else:
        risk = "low"

    return {
        "total_claims_checked": len(analyses),
        "high_framing_count": high_count,
        "medium_framing_count": medium_count,
        "low_framing_count": low_count,
        "emotional_language_count": emotional_count,
        "uncertainty_language_count": uncertainty_count,
        "editor_review_needed_count": editor_review_count,
        "overall_framing_risk": risk,
    }
