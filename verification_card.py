from datetime import datetime, timezone
from urllib.parse import urlparse
import re

from source_reliability_agent import summarize_source_reliability


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

    return {
        "claim_text": claim_text,
        "claims": claim_list or [claim_text],
        "normalized_claims": normalized_claims or [],
        "source_queries": source_queries or [],
        "source_candidates": source_candidates or [],
        "source_reliability_summary": summarize_source_reliability(source_candidates or []),
        "verdict_label": _verdict_label(policy_confidence, evidence_comparison, official_sources),
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
