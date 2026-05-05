from datetime import datetime, timezone
from urllib.parse import urlparse
import hashlib
import re

from text_utils import sanitize_text


OFFICIAL_SOURCE_TYPES = {
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

FALLBACK_NEWS_SOURCES = {"naver_fallback", "daum_fallback"}

STOPWORDS = {
    "정부",
    "정책",
    "관련",
    "이번",
    "기사",
    "뉴스",
    "있는",
    "없는",
    "대한",
    "것으로",
    "한다고",
    "했다",
    "한다",
    "있다",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _publisher_from_url(url: str) -> str:
    netloc = urlparse(url or "").netloc
    return netloc.replace("www.", "")


def _news_source_type(news: dict, original_url: str) -> str:
    source = news.get("source") or ""
    if source in FALLBACK_NEWS_SOURCES:
        return "search_fallback_news"
    if _publisher_from_url(original_url):
        return "established_news"
    return "unknown"


def _official_source_type(source_type: str) -> str:
    if source_type in OFFICIAL_SOURCE_TYPES:
        return "official_government"
    if source_type in PUBLIC_INSTITUTION_TYPES:
        return "public_institution"
    return "unknown"


def _token_variants(token: str) -> set[str]:
    variants = {token}
    for suffix in ["으로", "에서", "에게", "까지", "부터", "보다", "처럼", "했다", "한다", "되는", "하고", "하며", "을", "를", "은", "는", "이", "가", "의", "와", "과", "도", "만", "로"]:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            variants.add(token[: -len(suffix)])
    return variants


def _keywords_from_claim(normalized_claim: dict) -> list[str]:
    fields = [
        normalized_claim.get("actor") or "",
        normalized_claim.get("target") or "",
        normalized_claim.get("object") or "",
        normalized_claim.get("action") or "",
        normalized_claim.get("quantity") or "",
        normalized_claim.get("location") or "",
        normalized_claim.get("claim_text") or "",
    ]
    words = []
    for field in fields:
        for raw in re.findall(r"[\uac00-\ud7a3A-Za-z0-9.%]+", sanitize_text(field)):
            for token in _token_variants(raw):
                if len(token) >= 2 and token not in STOPWORDS and token != "unknown":
                    words.append(token)

    deduped = []
    for word in words:
        if word not in deduped:
            deduped.append(word)
        if len(deduped) >= 8:
            break
    return deduped


def _numbers_from_claim(normalized_claim: dict) -> list[str]:
    text = " ".join(
        str(normalized_claim.get(key) or "")
        for key in ["claim_text", "quantity", "date_or_time"]
    )
    return list(
        dict.fromkeys(
            re.findall(
                r"\d+(?:\.\d+)?\s*(?:%p|%|조원|억원|만원|원|건|명|배)?|\d{4}년|\d{1,2}월|\d{1,2}일",
                text,
            )
        )
    )[:3]


def _compact_contradiction_query(claim: dict) -> str:
    keywords = _keywords_from_claim(claim)
    numbers = _numbers_from_claim(claim)
    institution_terms = [
        term
        for term in keywords
        if term in {"한국은행", "금융위", "금융위원회", "금감원", "금융감독원", "국토부", "국토교통부", "국세청", "정부"}
    ]
    action_terms = [
        term
        for term in keywords
        if term in {"금리", "인상", "인하", "동결", "규제", "제한", "차단", "지원", "감면", "조사", "시행", "확대", "축소"}
    ]
    core = []
    for term in [*institution_terms, *keywords[:5], *numbers, *action_terms]:
        if term and term not in core:
            core.append(term)
        if len(core) >= 7:
            break
    base = " ".join(core) or (claim.get("claim_text") or "")[:40]
    return f"{base} 반박 정정 사실 확인"[:90]


def generate_source_queries(
    normalized_claims: list[dict],
    original_query: str = "",
) -> list[dict]:
    source_queries = []

    for index, claim in enumerate(normalized_claims or []):
        keywords = _keywords_from_claim(claim)
        compact_query = " ".join(keywords[:5]) or claim.get("claim_text") or original_query
        official_query = f"site:go.kr {compact_query}".strip()
        contradiction_query = _compact_contradiction_query(claim)

        query_specs = [
            ("original_query", original_query, "news_context"),
            ("claim_keyword_query", compact_query, "support"),
            ("official_query", official_query, "primary_source"),
            ("contradiction_query", contradiction_query, "contradiction"),
            ("denial_query", contradiction_query.replace("반박 정정", "해명 부인"), "fact_check"),
            ("official_explanation_query", f"{compact_query} 공식 해명"[:90], "update"),
        ]

        for query_type, query, purpose in query_specs:
            if not query:
                continue
            source_queries.append(
                {
                    "claim_index": index,
                    "claim_text": claim.get("claim_text") or "",
                    "query_type": query_type,
                    "query": query[:100],
                    "purpose": purpose,
                }
            )

    print(f"[SourceRetrievalAgent] generated {len(source_queries)} source queries")
    return source_queries


def create_source_candidates(
    *,
    normalized_claims: list[dict],
    news: dict,
    original_url: str,
    original_query: str = "",
    article_body: str = "",
    official_source_candidates: list[dict] | None = None,
) -> list[dict]:
    retrieved_at = _now_iso()
    source_candidates = []
    publisher = news.get("publisher") or news.get("source_name") or _publisher_from_url(original_url)
    news_source_type = _news_source_type(news, original_url)

    for index, claim in enumerate(normalized_claims or []):
        source_candidates.append(
            {
                "source_id": _source_id(str(index), original_url, "news_context"),
                "claim_index": index,
                "title": news.get("title") or "",
                "url": original_url,
                "publisher": publisher,
                "source_type": news_source_type,
                "retrieval_method": "current_news_collection",
                "query_used": original_query,
                "purpose": "news_context",
                "raw_text_available": bool(article_body),
                "retrieved_at": retrieved_at,
            }
        )

        claim_query = " ".join(_keywords_from_claim(claim)[:5]) or original_query
        for source in (official_source_candidates or [])[:3]:
            source_candidates.append(
                {
                    "source_id": _source_id(
                        str(index),
                        source.get("source_name") or "",
                        source.get("official_search_url") or "",
                    ),
                    "claim_index": index,
                    "title": source.get("source_name") or "",
                    "url": source.get("official_search_url") or "",
                    "publisher": source.get("source_name") or "",
                    "source_type": _official_source_type(source.get("source_type") or ""),
                    "retrieval_method": "official_search_url_candidate",
                    "query_used": claim_query,
                    "purpose": "primary_source",
                    "raw_text_available": False,
                    "retrieved_at": retrieved_at,
                }
            )

    print(f"[SourceRetrievalAgent] created {len(source_candidates)} source candidates")
    return _stable_sort_source_candidates(source_candidates)


def _stable_sort_source_candidates(source_candidates: list[dict]) -> list[dict]:
    source_type_rank = {
        "official_government": 0,
        "public_institution": 1,
        "established_news": 2,
        "search_fallback_news": 3,
        "unknown": 4,
    }
    purpose_rank = {
        "primary_source": 0,
        "support": 1,
        "news_context": 2,
        "contradiction": 3,
        "fact_check": 4,
        "update": 5,
    }
    indexed = list(enumerate(source_candidates or []))
    indexed.sort(
        key=lambda pair: (
            int(pair[1].get("claim_index") or 0),
            source_type_rank.get(pair[1].get("source_type") or "unknown", 9),
            purpose_rank.get(pair[1].get("purpose") or "", 9),
            pair[1].get("publisher") or "",
            pair[1].get("title") or "",
            pair[1].get("url") or "",
            pair[0],
        )
    )
    return [item for _index, item in indexed]


def build_source_retrieval_context(
    *,
    normalized_claims: list[dict],
    news: dict,
    original_url: str,
    original_query: str = "",
    article_body: str = "",
    official_source_candidates: list[dict] | None = None,
) -> dict:
    source_queries = generate_source_queries(
        normalized_claims=normalized_claims,
        original_query=original_query,
    )
    source_candidates = create_source_candidates(
        normalized_claims=normalized_claims,
        news=news,
        original_url=original_url,
        original_query=original_query,
        article_body=article_body,
        official_source_candidates=official_source_candidates,
    )
    return {
        "source_queries": source_queries,
        "source_candidates": _stable_sort_source_candidates(source_candidates),
    }
