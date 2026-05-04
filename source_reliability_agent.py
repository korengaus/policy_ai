from urllib.parse import urlparse


VERY_HIGH_DOMAINS = [
    "go.kr",
    "korea.kr",
    "mof.go.kr",
    "molit.go.kr",
    "fsc.go.kr",
    "fss.or.kr",
    "bok.or.kr",
    "assembly.go.kr",
    "gov.kr",
]

HIGH_DOMAINS = [
    "kosis.kr",
    "re.kr",
    "ac.kr",
    "or.kr",
]

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
    return any(domain == pattern or domain.endswith("." + pattern) or pattern in domain for pattern in patterns)


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


def evaluate_source_candidate(source: dict) -> dict:
    enriched = dict(source or {})
    url = enriched.get("url") or ""
    domain = _domain(url)
    source_type = enriched.get("source_type") or "unknown"
    purpose = enriched.get("purpose") or ""
    retrieval_method = enriched.get("retrieval_method") or ""
    raw_text_available = bool(enriched.get("raw_text_available"))
    flags = []

    if not domain:
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

    if purpose in {"contradiction", "fact_check", "update"}:
        score = max(score - 5, 0)
    if "possible_redirect" in flags and source_type not in {"official_government", "public_institution"}:
        score = max(score - 8, 0)

    enriched["reliability_score"] = min(100, int(score))
    enriched["reliability_level"] = _level(enriched["reliability_score"])
    enriched["verification_role"] = _role(source_type, purpose, enriched["reliability_score"])
    enriched["source_risk_flags"] = sorted(set(flags))
    enriched["reliability_reason"] = reason
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
    top_source = max(
        eligible or candidates,
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
    average = round(
        sum(int(source.get("reliability_score") or 0) for source in candidates) / len(candidates)
    )
    return {
        "top_source_title": top_source.get("title") or top_source.get("url"),
        "top_source_url": top_source.get("url"),
        "top_source_reliability_score": top_source.get("reliability_score") or 0,
        "official_candidate_count": official_count,
        "raw_text_available_count": raw_text_count,
        "average_reliability_score": average,
        "official_detail_available": False,
        "official_mismatch": not bool(eligible),
        "official_mismatch_reasons": [] if eligible else ["no eligible fetched source for top evidence"],
    }
