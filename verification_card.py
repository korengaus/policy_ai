from datetime import datetime, timezone
from urllib.parse import urlparse
import re

from source_reliability_agent import summarize_source_reliability
from evidence_extraction_agent import (
    summarize_claim_evidence_quality,
    summarize_evidence_snippets,
)
from contradiction_agent import summarize_contradiction_checks
from bias_framing_agent import summarize_bias_framing


OFFICIAL_GOVERNMENT_TYPES = {
    "central_government",
    "financial_regulator",
    "central_bank",
    "legislature",
    "local_government",
}

PUBLIC_INSTITUTION_TYPES = {
    "public_service",
    "public_financial_institution",
}

EXCLUDED_TOP_SOURCE_TYPES = {
    "search_page",
    "index_page",
    "menu_or_index_page",
    "service_index_page",
    "generic_list_page",
    "service_page",
    "faq_or_guide",
    "error_page",
    "attachment_only",
}

HOUSING_QUERY_TERMS = {
    "부동산",
    "주거",
    "주택",
    "전세",
    "월세",
    "임대",
    "양도세",
    "공급",
    "재건축",
    "재개발",
}

HOUSING_DOCUMENT_TERMS = {
    "부동산",
    "주거",
    "주택",
    "전세",
    "월세",
    "임대",
    "양도세",
    "공급",
    "재건축",
    "재개발",
    "보증금",
    "세입자",
}

MATERIAL_OFFICIAL_CONCEPTS = {
    "rental_loan",
    "mortgage_loan",
    "interest_rate",
    "regulation",
    "financial_product_notice",
}

FALLBACK_NEWS_SOURCES = {
    "naver_fallback",
    "daum_fallback",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


POLICY_ACTION_KEYWORDS = [
    "검토",
    "추진",
    "발표",
    "조사",
    "착수",
    "시행",
    "확대",
    "축소",
    "제한",
    "차단",
    "금지",
    "지원",
    "감면",
    "인하",
    "인상",
    "대출",
    "금리",
    "규제",
    "정책",
]


def _split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?。])\s+|(?<=[다요죠음임함됨])\.\s*|(?<=다)\s+", normalized)
    sentences = []
    for part in parts:
        sentence = part.strip(" -•·\t\r\n")
        if len(sentence) >= 25:
            sentences.append(sentence)
    return sentences


def _sentence_score(sentence: str) -> int:
    score = min(len(sentence), 160)
    if any(keyword in sentence for keyword in POLICY_ACTION_KEYWORDS):
        score += 60
    if re.search(r"\d", sentence):
        score += 25
    if any(actor in sentence for actor in ["정부", "금융당국", "국토부", "금융위", "금감원", "한국은행", "은행", "기업은행"]):
        score += 30
    if any(target in sentence for target in ["전세대출", "주택담보대출", "주담대", "금리", "부동산", "청년", "중소기업", "대출"]):
        score += 30
    return score


def _content_based_claim(article_body: str) -> str:
    if not article_body or len(article_body) < 300:
        return ""

    sentences = _split_sentences(article_body)
    if not sentences:
        return ""

    ranked = sorted(sentences[:20], key=_sentence_score, reverse=True)
    chosen = []
    for sentence in ranked:
        if sentence not in chosen:
            chosen.append(sentence)
        if len(chosen) >= 2:
            break

    claim = " ".join(chosen).strip()
    if len(claim) > 450:
        claim = claim[:447].rstrip() + "..."
    return claim


def _first_policy_claim(policy_claims: list[dict], news_title: str, news_summary: str, article_body: str = "") -> str:
    content_claim = _content_based_claim(article_body)
    if content_claim:
        return content_claim

    if policy_claims:
        first = policy_claims[0] or {}
        sentence = first.get("sentence")
        if sentence:
            return sentence
    return news_summary or news_title or ""


def _source_type_for_official(source_type: str) -> str:
    if source_type in OFFICIAL_GOVERNMENT_TYPES:
        return "official_government"
    if source_type in PUBLIC_INSTITUTION_TYPES:
        return "public_institution"
    return "unknown"


def _source_type_for_news(news_source: str, original_url: str) -> str:
    if news_source in FALLBACK_NEWS_SOURCES:
        return "search_fallback_news"
    parsed = urlparse(original_url or "")
    if parsed.netloc:
        return "established_news"
    return "unknown"


def _reliability_reason(source_type: str, grade: str | None = None) -> str:
    if source_type == "official_government":
        return "정부/금융당국/국회 등 공식 출처 후보입니다."
    if source_type == "public_institution":
        return "공공서비스 또는 공적 금융기관 출처입니다."
    if source_type == "established_news":
        return "뉴스 검색 결과에서 확보한 언론 기사입니다."
    if source_type == "search_fallback_news":
        return "Google RSS 실패 시 검색 HTML fallback으로 확보한 기사입니다."
    if grade:
        return f"출처 신뢰도 등급 {grade} 기준으로 평가했습니다."
    return "출처 유형을 명확히 분류하지 못했습니다."


def _official_evidence_sources(official_evidence_results: list[dict]) -> list[dict]:
    sources = []
    for evidence in official_evidence_results or []:
        if evidence.get("should_exclude_from_verification") or evidence.get("evidence_grade") == "F":
            continue
        if not (evidence.get("usable") or evidence.get("weakly_usable")):
            continue

        source_type = _source_type_for_official(evidence.get("source_type") or "")
        reliability_score = evidence.get("reliability_score") or 0
        sources.append(
            {
                "title": evidence.get("document_title") or evidence.get("source_name") or "",
                "url": evidence.get("selected_document_url") or evidence.get("search_url") or "",
                "source_type": source_type,
                "reliability_score": reliability_score,
                "reliability_reason": _reliability_reason(source_type),
                "evidence_grade": evidence.get("evidence_grade"),
            }
        )
    return sources


def _split_csv_like(value) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def _official_topic_mismatch_reason(item: dict) -> str:
    query_text = " ".join(
        [
            str(item.get("search_query_used") or ""),
            " ".join(str(part) for part in item.get("search_query_variants") or []),
        ]
    )
    document_text = " ".join(
        [
            str(item.get("document_title") or ""),
            str(item.get("document_text_snippet") or "")[:800],
        ]
    )
    title_text = str(item.get("document_title") or "")
    site_key = str(item.get("site_key") or "").lower()
    query_has_housing = any(term in query_text for term in HOUSING_QUERY_TERMS)
    document_has_housing = any(term in document_text for term in HOUSING_DOCUMENT_TERMS)
    title_has_housing = any(term in title_text for term in HOUSING_DOCUMENT_TERMS)
    if query_has_housing and site_key in {"fsc", "fss", "ibk", "bok"} and not title_has_housing:
        return "official document topic mismatch: financial-agency title lacks housing/real-estate terms"
    if query_has_housing and not document_has_housing:
        return "official document topic mismatch: housing/real-estate query without matching document terms"

    concepts = _split_csv_like(item.get("matched_concepts"))
    query_has_housing_finance = any(term in query_text for term in {"전세대출", "주담대", "주택담보대출", "금리", "규제"})
    if query_has_housing_finance and not (concepts & MATERIAL_OFFICIAL_CONCEPTS):
        return "official document topic mismatch: missing material policy concepts"

    return ""


def _is_usable_official_detail(item: dict) -> bool:
    if not (item.get("usable") or item.get("weakly_usable")):
        return False
    if item.get("should_exclude_from_verification"):
        return False
    if item.get("evidence_grade") not in {"A", "B", "C"}:
        return False
    if item.get("document_type") in EXCLUDED_TOP_SOURCE_TYPES:
        return False
    if not item.get("selected_document_url"):
        return False
    return not _official_topic_mismatch_reason(item)


def _official_verification_summary(
    official_evidence_results: list[dict],
    fallback_summary: dict,
) -> dict:
    results = official_evidence_results or []
    usable = [item for item in results if _is_usable_official_detail(item)]
    mismatch_results = [
        item
        for item in results
        if item.get("should_exclude_from_verification")
        or item.get("evidence_grade") == "F"
        or item.get("document_type") in EXCLUDED_TOP_SOURCE_TYPES
        or _official_topic_mismatch_reason(item)
        or (item.get("document_relevance_score") is not None and int(item.get("document_relevance_score") or 0) < 40)
    ]
    mismatch_reasons = []
    for item in mismatch_results[:4]:
        reason = (
            _official_topic_mismatch_reason(item)
            or item.get("error")
            or item.get("selected_document_reason")
            or item.get("document_type")
            or "official evidence mismatch"
        )
        if reason not in mismatch_reasons:
            mismatch_reasons.append(reason)

    summary = dict(fallback_summary or {})
    summary["official_detail_available"] = bool(usable)
    summary["official_mismatch"] = not bool(usable)
    summary["official_mismatch_count"] = len(mismatch_results)
    summary["official_mismatch_reasons"] = mismatch_reasons

    if usable:
        top = max(
            usable,
            key=lambda item: (
                int(item.get("document_relevance_score") or 0),
                {"A": 3, "B": 2, "C": 1}.get(item.get("evidence_grade"), 0),
            ),
        )
        summary.update(
            {
                "top_source_title": top.get("document_title") or top.get("source_name"),
                "top_source_url": top.get("selected_document_url") or top.get("search_url"),
                "top_source_reliability_score": top.get("reliability_score") or 5,
                "top_source_evidence_grade": top.get("evidence_grade"),
                "top_source_document_type": top.get("document_type"),
                "top_source_relevance_score": top.get("document_relevance_score") or 0,
                "top_source_note": "usable official detail document",
            }
        )
    else:
        summary.update(
            {
                "top_source_title": "공식 상세 근거 부족",
                "top_source_url": "",
                "top_source_reliability_score": 0,
                "top_source_evidence_grade": None,
                "top_source_document_type": None,
                "top_source_relevance_score": 0,
                "top_source_note": "usable official detail document not found",
            }
        )
    return summary


def _official_adjusted_evidence_quality(
    quality_summary: dict,
    source_reliability_summary: dict,
) -> dict:
    adjusted = dict(quality_summary or {})
    if not source_reliability_summary.get("official_mismatch"):
        return adjusted

    strong_count = int(adjusted.get("strong") or 0)
    medium_count = int(adjusted.get("medium") or 0)
    weak_count = int(adjusted.get("weak") or 0)
    total_count = strong_count + medium_count + weak_count
    adjusted["strong"] = 0
    adjusted["medium"] = 0
    adjusted["weak"] = total_count
    adjusted["average_evidence_quality_score"] = min(
        int(adjusted.get("average_evidence_quality_score") or 0),
        35 if total_count else 0,
    )
    adjusted["evidence_quality_overall_label"] = "weak"
    adjusted["official_quality_note"] = "official detail evidence missing or mismatched"
    return adjusted


def _news_source(news: dict, original_url: str) -> dict:
    source_type = _source_type_for_news(news.get("source") or "", original_url)
    reliability_score = 3 if source_type == "established_news" else 2

    return {
        "title": news.get("title") or "",
        "url": original_url,
        "source_type": source_type,
        "reliability_score": reliability_score,
        "reliability_reason": _reliability_reason(source_type),
    }


def _verdict_label(
    policy_confidence: dict,
    evidence_comparison: dict,
    official_sources: list[dict],
    evidence_snippets: list[dict] | None = None,
    contradiction_summary: dict | None = None,
    bias_framing_summary: dict | None = None,
    claim_count: int = 0,
) -> str:
    confidence_score = int(policy_confidence.get("policy_confidence_score") or 0)
    verification_strength = policy_confidence.get("verification_strength")
    comparison_status = evidence_comparison.get("comparison_status")
    verification_level = evidence_comparison.get("verification_level")
    has_conflict = bool(
        evidence_comparison.get("conflict_signals")
        or evidence_comparison.get("semantic_conflict_signals")
    )

    if has_conflict or comparison_status == "official_conflict_possible":
        return "draft_disputed"

    contradiction = contradiction_summary or {}
    bias = bias_framing_summary or {}
    possible_count = int(contradiction.get("possible_contradiction_count") or 0)
    likely_count = int(contradiction.get("likely_contradiction_count") or 0)
    high_framing_count = int(bias.get("high_framing_count") or 0)
    official_confirmation_count = int(
        contradiction.get("needs_official_confirmation_count") or 0
    )
    insufficient_claim_count = int(contradiction.get("insufficient_evidence_count") or 0)
    if high_framing_count and (possible_count or likely_count):
        return "draft_high_risk_review"
    if high_framing_count:
        return "draft_needs_review"
    if possible_count or likely_count:
        return "draft_needs_review"
    if claim_count and official_confirmation_count >= max(1, claim_count // 2):
        return "draft_needs_official_confirmation"
    if claim_count and insufficient_claim_count >= max(1, claim_count // 2):
        return "draft_needs_context"

    snippets = evidence_snippets or []
    direct_support_count = sum(1 for item in snippets if item.get("evidence_type") == "direct_support")
    official_reference_count = sum(1 for item in snippets if item.get("evidence_type") == "official_reference")
    insufficient_count = sum(1 for item in snippets if item.get("evidence_type") == "insufficient_evidence")
    if claim_count and direct_support_count >= claim_count:
        return "draft_verified"
    if official_reference_count > 0 and direct_support_count == 0:
        return "draft_needs_official_confirmation"
    if insufficient_count > 0:
        return "draft_needs_context"

    if comparison_status == "official_evidence_missing" and verification_level == "excluded_non_policy_page":
        return "draft_needs_context"
    if not official_sources or verification_strength == "none":
        return "draft_unverified"
    if confidence_score >= 85 and verification_level == "strong_official_match":
        return "draft_verified"
    if confidence_score >= 60 and verification_level in {"strong_official_match", "medium_official_match"}:
        return "draft_likely_true"
    if confidence_score >= 35:
        return "draft_needs_context"
    return "draft_unverified"


def _missing_context(
    official_sources: list[dict],
    evidence_comparison: dict,
    official_evidence_results: list[dict],
) -> list[str]:
    missing = []
    if not official_sources:
        missing.append("검증에 사용할 수 있는 공식 상세문서가 부족합니다.")
    if evidence_comparison.get("verification_level") in {"weak_official_match", "low_confidence_match"}:
        missing.append("공식문서와 뉴스 주장 사이의 정책명/대상/시행일 일치 여부 확인이 필요합니다.")
    if evidence_comparison.get("verification_level") == "excluded_non_policy_page":
        missing.append("수집된 공식문서가 목록/안내/무관 문서로 제외되었습니다.")
    if any(result.get("error") for result in official_evidence_results or []):
        missing.append("일부 공식기관 검색 또는 상세문서 접근에 실패했습니다.")
    if not missing:
        missing.append("최종 공개 전 사람 검토와 원문 재확인이 필요합니다.")
    return missing


def _evidence_summary(evidence_comparison: dict, official_sources: list[dict]) -> str:
    summary = evidence_comparison.get("comparison_summary") or ""
    if official_sources:
        source_titles = ", ".join(
            source.get("title") or source.get("url") or "공식 출처"
            for source in official_sources[:2]
        )
        return f"{summary} 주요 근거: {source_titles}".strip()
    return summary or "사용 가능한 공식 근거가 아직 충분하지 않습니다."


def build_verification_card(
    *,
    news: dict,
    original_url: str,
    policy_claims: list[dict],
    official_evidence_results: list[dict],
    evidence_comparison: dict,
    policy_confidence: dict,
    article_body: str = "",
    claims: list[str] | None = None,
    normalized_claims: list[dict] | None = None,
    source_queries: list[dict] | None = None,
    source_candidates: list[dict] | None = None,
    evidence_snippets: list[dict] | None = None,
    claim_evidence_map: dict | None = None,
    contradiction_checks: list[dict] | None = None,
    contradiction_summary: dict | None = None,
    bias_framing_analysis: list[dict] | None = None,
    bias_framing_summary: dict | None = None,
) -> dict:
    official_sources = _official_evidence_sources(official_evidence_results)
    evidence_sources = official_sources + [_news_source(news, original_url)]
    best_source = official_sources[0] if official_sources else evidence_sources[0]
    verdict_confidence = int(policy_confidence.get("policy_confidence_score") or 0)
    claim_list = [claim for claim in (claims or []) if claim]
    claim_text = (
        claim_list[0]
        if claim_list
            else _first_policy_claim(
            policy_claims,
            news.get("title") or "",
            news.get("summary") or "",
            article_body,
        )
    )
    final_contradiction_summary = (
        contradiction_summary
        or summarize_contradiction_checks(contradiction_checks or [])
    )
    final_bias_summary = (
        bias_framing_summary
        or summarize_bias_framing(bias_framing_analysis or [])
    )
    evidence_extraction_summary = summarize_evidence_snippets(evidence_snippets or [])
    claim_quality_summary = summarize_claim_evidence_quality(
        claim_list or [claim_text],
        evidence_snippets or [],
    )
    source_reliability_summary = _official_verification_summary(
        official_evidence_results,
        summarize_source_reliability(source_candidates or []),
    )
    evidence_quality_summary = _official_adjusted_evidence_quality(
        evidence_extraction_summary.get("evidence_quality_summary") or {},
        source_reliability_summary,
    )
    evidence_extraction_summary = dict(evidence_extraction_summary)
    evidence_extraction_summary["evidence_quality_summary"] = evidence_quality_summary
    evidence_extraction_summary["total_strong_evidence"] = evidence_quality_summary.get("strong", 0)
    evidence_extraction_summary["total_medium_evidence"] = evidence_quality_summary.get("medium", 0)
    evidence_extraction_summary["total_weak_evidence"] = evidence_quality_summary.get("weak", 0)
    evidence_extraction_summary["average_evidence_quality_score"] = evidence_quality_summary.get(
        "average_evidence_quality_score",
        0,
    )
    evidence_extraction_summary["evidence_quality_overall_label"] = evidence_quality_summary.get(
        "evidence_quality_overall_label",
        "weak",
    )

    return {
        "claim_text": claim_text,
        "claims": claim_list or [claim_text],
        "normalized_claims": normalized_claims or [],
        "source_queries": source_queries or [],
        "source_candidates": source_candidates or [],
        "source_reliability_summary": source_reliability_summary,
        "official_mismatch": source_reliability_summary.get("official_mismatch"),
        "official_mismatch_reasons": source_reliability_summary.get("official_mismatch_reasons") or [],
        "official_detail_available": source_reliability_summary.get("official_detail_available"),
        "evidence_snippets": evidence_snippets or [],
        "claim_evidence_map": claim_evidence_map or {},
        "claim_evidence_quality_summary": claim_quality_summary,
        "evidence_quality_summary": evidence_quality_summary,
        "evidence_extraction_summary": evidence_extraction_summary,
        "contradiction_checks": contradiction_checks or [],
        "contradiction_summary": final_contradiction_summary,
        "bias_framing_analysis": bias_framing_analysis or [],
        "bias_framing_summary": final_bias_summary,
        "verdict_label": _verdict_label(
            policy_confidence,
            evidence_comparison,
            official_sources,
            evidence_snippets=evidence_snippets or [],
            contradiction_summary=final_contradiction_summary,
            bias_framing_summary=final_bias_summary,
            claim_count=len(claim_list or [claim_text]),
        ),
        "verdict_confidence": verdict_confidence,
        "evidence_sources": evidence_sources,
        "source_reliability_score": best_source.get("reliability_score") or 0,
        "source_reliability_reason": best_source.get("reliability_reason") or "",
        "evidence_summary": _evidence_summary(evidence_comparison, official_sources),
        "missing_context": _missing_context(
            official_sources,
            evidence_comparison,
            official_evidence_results,
        ),
        "last_checked_at": _now_iso(),
        "review_status": "ai_draft_pending_human_review",
    }


def print_verification_card(card: dict):
    print("\n----- Verification card -----")
    print("claim_text:", card.get("claim_text"))
    print("claims:")
    for claim in card.get("claims") or []:
        print("-", claim)
    print("normalized_claims:")
    for claim in card.get("normalized_claims") or []:
        print(
            "-",
            claim.get("actor"),
            "|",
            claim.get("action"),
            "|",
            claim.get("target"),
            "|",
            claim.get("status"),
            "|",
            claim.get("claim_type"),
            "|",
            claim.get("uncertainty_level"),
        )
    print("source_queries:", len(card.get("source_queries") or []))
    print("source_candidates:", len(card.get("source_candidates") or []))
    print("source_reliability_summary:", card.get("source_reliability_summary"))
    print("evidence_snippets:", len(card.get("evidence_snippets") or []))
    print("claim_evidence_quality_summary:", card.get("claim_evidence_quality_summary"))
    print("evidence_quality_summary:", card.get("evidence_quality_summary"))
    print("evidence_extraction_summary:", card.get("evidence_extraction_summary"))
    print("contradiction_checks:", len(card.get("contradiction_checks") or []))
    print("contradiction_summary:", card.get("contradiction_summary"))
    print("bias_framing_analysis:", len(card.get("bias_framing_analysis") or []))
    print("bias_framing_summary:", card.get("bias_framing_summary"))
    print("debug_summary:", card.get("debug_summary"))
    print("verdict_label:", card.get("verdict_label"))
    print("verdict_confidence:", card.get("verdict_confidence"))
    print("source_reliability_score:", card.get("source_reliability_score"))
    print("source_reliability_reason:", card.get("source_reliability_reason"))
    print("evidence_summary:", card.get("evidence_summary"))
    print("missing_context:")
    for item in card.get("missing_context") or []:
        print("-", item)
    print("last_checked_at:", card.get("last_checked_at"))
    print("review_status:", card.get("review_status"))
