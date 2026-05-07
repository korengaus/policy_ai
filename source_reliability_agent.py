from urllib.parse import urlparse

from official_metadata import (
    OFFICIAL_AUTHORITY_DOMAINS,
    PUBLIC_INSTITUTION_DOMAINS,
    canonical_official_domain,
    domain_matches,
    is_official_domain,
    name_implies_official,
    official_source_type_from_identity,
)


VERY_HIGH_DOMAINS = [
    "go.kr",
    "korea.kr",
    "mof.go.kr",
    "moef.go.kr",
    "mofa.go.kr",
    "molit.go.kr",
    "fsc.go.kr",
    "fss.or.kr",
    "bok.or.kr",
    "assembly.go.kr",
    "gov.kr",
    "nts.go.kr",
    "customs.go.kr",
    "stat.go.kr",
    "law.go.kr",
    "epeople.go.kr",
    "mss.go.kr",
    "msit.go.kr",
    "kif.re.kr",
    "hf.go.kr",
    "khug.or.kr",
    "lh.or.kr",
    "hfn.go.kr",
]
VERY_HIGH_DOMAINS = sorted(set(VERY_HIGH_DOMAINS) | OFFICIAL_AUTHORITY_DOMAINS)

HIGH_DOMAINS = [
    "kdi.re.kr",
    "kosis.kr",
    "hrdkorea.or.kr",
    "re.kr",
    "ac.kr",
    "or.kr",
]
HIGH_DOMAINS = sorted(set(HIGH_DOMAINS) | PUBLIC_INSTITUTION_DOMAINS)

NEWS_DOMAINS = [
    "yonhapnews.co.kr",
    "newsis.com",
    "mk.co.kr",
    "hankyung.com",
    "chosun.com",
    "joongang.co.kr",
    "donga.com",
    "sbs.co.kr",
    "mbc.co.kr",
    "kbs.co.kr",
    "daum.net",
    "naver.com",
]


def _domain(url: str) -> str:
    return urlparse(url or "").netloc.lower().replace("www.", "")


def _matches_domain(domain: str, patterns: list[str]) -> bool:
    return domain_matches(domain, patterns) or any(pattern in domain for pattern in patterns)


def _level(score: int) -> str:
    if score >= 90:
        return "very_high"
    if score >= 75:
        return "high"
    if score >= 45:
        return "medium"
    if score >= 25:
        return "low"
    return "unknown"


def _role(source_type: str, purpose: str, score: int) -> str:
    if purpose == "contradiction":
        return "contradiction_check"
    if source_type in {"official_government", "public_institution"} and score >= 75:
        return "primary_evidence"
    if source_type in {"official_government", "public_institution"}:
        return "context_only"
    if purpose == "support" and score >= 50:
        return "supporting_evidence"
    if purpose == "news_context":
        return "context_only"
    if score < 45:
        return "not_reliable_enough"
    return "supporting_evidence"


def _readable_reason(source: dict, fallback: str) -> str:
    source_type = source.get("source_type") or ""
    flags = set(source.get("source_risk_flags") or [])
    if source_type in {"official_government", "public_institution"}:
        if source.get("official_body_match"):
            return "공식기관 상세 본문이 수집됐고 핵심 주장과 직접 일치합니다."
        if source.get("official_body_fetched") or source.get("raw_text_available"):
            return "공식기관 본문은 수집됐지만 기사 핵심 주장과 직접 일치하지 않아 신뢰도를 낮게 반영했습니다."
        if "official_topic_mismatch" in flags or source.get("official_should_exclude_from_verification"):
            return "공식 출처가 기사 내용과 직접 일치하지 않아 신뢰도를 낮게 반영했습니다."
        if "official_search_only" in flags:
            return "공식기관 후보는 검색/목록 페이지까지만 확인되어 상세 본문 근거로 쓰기 어렵습니다."
        if "official_detail_missing" in flags or "official_detail_url_missing" in flags:
            return "공식기관 후보는 찾았지만 확인 가능한 상세 문서 URL이 부족합니다."
        if "official_pdf_only" in flags:
            return "공식기관 자료가 PDF 중심이라 현재 본문 검증에는 제한이 있습니다."
        return "공식기관 후보는 찾았지만 실제 상세 본문 확인은 아직 충분하지 않습니다."
    if source_type == "search_fallback_news":
        return "검색 fallback으로 확보한 뉴스 출처라 공식 출처보다 낮은 신뢰도로 반영했습니다."
    if source_type == "established_news":
        return "언론 보도 맥락 출처로 참고하되 공식 발표 여부는 별도 확인이 필요합니다."
    return fallback or "출처 신뢰도를 명확히 판단하기 어렵습니다."


def evaluate_source_candidate(source: dict) -> dict:
    enriched = dict(source or {})
    url = enriched.get("url") or ""
    domain = _domain(url) or canonical_official_domain(enriched.get("publisher") or enriched.get("title") or "", url)
    source_type = enriched.get("source_type") or "unknown"
    inferred_type = official_source_type_from_identity(enriched.get("publisher") or enriched.get("title") or "", url)
    if inferred_type and source_type == "unknown":
        source_type = inferred_type
        enriched["source_type"] = source_type
    purpose = enriched.get("purpose") or ""
    retrieval_method = enriched.get("retrieval_method") or ""
    raw_text_available = bool(enriched.get("raw_text_available"))
    flags = []

    if not domain and not name_implies_official(enriched.get("publisher") or enriched.get("title") or ""):
        flags.append("unknown_publisher")
    if any(token in url.lower() for token in ["redirect", "url=", "news.google.com", "search?"]):
        flags.append("possible_redirect")
    if source_type == "search_fallback_news":
        flags.append("search_fallback_only")
    if not raw_text_available:
        flags.append("no_body_text")
    if source_type == "unknown":
        flags.append("unofficial_source")

    if source_type == "official_government" or _matches_domain(domain, VERY_HIGH_DOMAINS):
        score = 95
        reason = "공식 정부/금융당국/공공기관 도메인 기반 출처 후보입니다."
    elif source_type == "public_institution" or _matches_domain(domain, HIGH_DOMAINS):
        score = 85
        reason = "공공기관/통계/학술 성격의 출처 후보입니다."
    elif source_type == "established_news" or _matches_domain(domain, NEWS_DOMAINS):
        score = 68
        reason = "주요 언론 또는 포털 뉴스 맥락 출처입니다."
    elif source_type == "search_fallback_news":
        score = 52
        reason = "검색 fallback으로 확보한 뉴스 출처입니다."
    else:
        score = 30
        reason = "출처 유형 또는 발행처 신뢰도를 명확히 판단하기 어렵습니다."

    if retrieval_method == "official_search_url_candidate" and not raw_text_available:
        flags.append("official_candidate_not_fetched")
        flags.append("official_detail_not_verified")
        reason += " 공식기관 후보이지만 실제 상세 문서 본문은 아직 수집되지 않았습니다."
        score = min(score, 70)

    if source_type in {"official_government", "public_institution"} and enriched.get("official_body_failure_reason"):
        flags.append(enriched.get("official_body_failure_reason"))

    if source_type in {"official_government", "public_institution"} and raw_text_available:
        if enriched.get("official_body_match"):
            reason += " 공식기관 상세 본문을 수집했고 해당 주장과 핵심 용어가 일치합니다."
            score = max(score, 92)
        else:
            flags.append("official_body_mismatch")
            reason += " 공식기관 본문은 수집됐지만 핵심 주장과의 직접 일치가 부족합니다."
            score = min(score, 70)
    elif source_type in {"official_government", "public_institution"} and not raw_text_available:
        flags.append("official_detail_not_verified")
        score = min(score, 70)

    if purpose in {"contradiction", "fact_check", "update"}:
        score = max(score - 5, 0)
    if "possible_redirect" in flags and source_type not in {"official_government", "public_institution"}:
        score = max(score - 8, 0)

    enriched["reliability_score"] = min(100, int(score))
    enriched["reliability_level"] = _level(enriched["reliability_score"])
    enriched["verification_role"] = _role(source_type, purpose, enriched["reliability_score"])
    enriched["source_risk_flags"] = sorted(set(flags))
    enriched["reliability_reason"] = _readable_reason(enriched, reason)
    return enriched


def evaluate_source_candidates(source_candidates: list[dict]) -> list[dict]:
    evaluated = [evaluate_source_candidate(source) for source in (source_candidates or [])]
    evaluated.sort(
        key=lambda source: (
            int(source.get("claim_index") or 0),
            -(int(source.get("reliability_score") or 0)),
            source.get("source_type") or "",
            source.get("publisher") or "",
            source.get("title") or "",
            source.get("url") or "",
            source.get("source_id") or "",
        )
    )
    official_count = sum(
        1
        for source in evaluated
        if source.get("source_type") in {"official_government", "public_institution"}
    )
    top_source = max(
        evaluated,
        key=lambda source: (
            source.get("reliability_score") or 0,
            source.get("title") or "",
            source.get("url") or "",
        ),
        default={},
    )
    print(f"[SourceReliabilityAgent] evaluated {len(evaluated)} sources")
    print(f"[SourceReliabilityAgent] top source: {top_source.get('title') or top_source.get('url') or 'None'}")
    print(f"[SourceReliabilityAgent] official candidates: {official_count}")
    return evaluated


def _is_top_source_eligible(source: dict) -> bool:
    flags = set(source.get("source_risk_flags") or [])
    if source.get("source_type") in {"official_government", "public_institution"}:
        return bool(
            source.get("raw_text_available")
            and source.get("official_body_match")
            and "official_body_mismatch" not in flags
        )
    if "official_candidate_not_fetched" in flags or "official_detail_not_verified" in flags:
        return False
    if "no_body_text" in flags:
        return False
    if source.get("verification_role") == "not_reliable_enough":
        return False
    return True


def summarize_source_reliability(source_candidates: list[dict]) -> dict:
    candidates = source_candidates or []
    if not candidates:
        return {
            "top_source_title": None,
            "top_source_url": None,
            "top_source_reliability_score": 0,
            "official_candidate_count": 0,
            "raw_text_available_count": 0,
            "average_reliability_score": 0,
            "official_detail_available": False,
            "official_mismatch": True,
            "official_mismatch_reasons": ["no source candidates"],
        }

    eligible = [source for source in candidates if _is_top_source_eligible(source)]
    fallback_eligible = [
        source
        for source in candidates
        if source.get("source_type") in {"established_news", "search_fallback_news"}
        and source.get("raw_text_available")
        and "possible_redirect" not in set(source.get("source_risk_flags") or [])
    ]
    top_source = max(
        eligible or fallback_eligible or candidates,
        key=lambda source: (
            source.get("reliability_score") or 0,
            source.get("title") or "",
            source.get("url") or "",
        ),
    )
    official_count = sum(
        1
        for source in candidates
        if source.get("source_type") in {"official_government", "public_institution"}
    )
    raw_text_count = sum(1 for source in candidates if source.get("raw_text_available"))
    official_body_matches = [
        source
        for source in candidates
        if source.get("source_type") in {"official_government", "public_institution"}
        and source.get("raw_text_available")
        and source.get("official_body_match")
    ]
    official_failure_reasons = {}
    for source in candidates:
        if source.get("source_type") not in {"official_government", "public_institution"}:
            continue
        reason = source.get("official_body_failure_reason")
        if reason:
            official_failure_reasons[reason] = official_failure_reasons.get(reason, 0) + 1
    average = round(
        sum(int(source.get("reliability_score") or 0) for source in candidates) / len(candidates)
    )
    mismatch_reasons = []
    if not official_body_matches:
        if official_failure_reasons:
            mismatch_reasons.extend(
                f"{reason}: {count}" for reason, count in sorted(official_failure_reasons.items())
            )
        else:
            mismatch_reasons.append("no eligible fetched official body for top evidence")
    return {
        "top_source_title": top_source.get("title") or top_source.get("url"),
        "top_source_url": top_source.get("url"),
        "top_source_reliability_score": top_source.get("reliability_score") or 0,
        "official_candidate_count": official_count,
        "raw_text_available_count": raw_text_count,
        "average_reliability_score": average,
        "official_detail_available": bool(official_body_matches),
        "official_body_match_count": len(official_body_matches),
        "official_detail_pages_fetched_count": sum(
            1
            for source in candidates
            if source.get("source_type") in {"official_government", "public_institution"}
            and source.get("official_body_fetched")
        ),
        "official_body_success_count": len(official_body_matches),
        "official_body_fail_count": sum(official_failure_reasons.values()),
        "official_failure_reasons": official_failure_reasons,
        "selected_primary_source": top_source.get("title") or top_source.get("url"),
        "official_source_used_in_final_scoring": bool(official_body_matches),
        "official_mismatch": not bool(official_body_matches),
        "official_mismatch_reasons": [] if official_body_matches else mismatch_reasons,
    }
