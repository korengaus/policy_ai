from datetime import datetime, timezone
from urllib.parse import urlparse
import hashlib
import re


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
    "당국",
    "통해",
    "대한",
    "관련",
    "내용",
    "기사",
    "지난",
    "오늘",
    "있다",
    "한다",
    "했다",
    "나섰다",
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


def _keywords_from_claim(normalized_claim: dict) -> list[str]:
    claim_text = normalized_claim.get("claim_text") or ""
    seeds = [
        normalized_claim.get("actor") or "",
        normalized_claim.get("target") or "",
        normalized_claim.get("object") or "",
        normalized_claim.get("action") or "",
        normalized_claim.get("quantity") or "",
        normalized_claim.get("location") or "",
    ]
    words = []
    for seed in seeds:
        for token in re.findall(r"[가-힣A-Za-z0-9.%]+", seed):
            if len(token) >= 2 and token not in STOPWORDS and token != "unknown":
                words.append(token)

    for token in re.findall(r"[가-힣A-Za-z0-9.%]+", claim_text):
        if len(token) >= 3 and token not in STOPWORDS:
            words.append(token)

    deduped = []
    for word in words:
        if word not in deduped:
            deduped.append(word)
        if len(deduped) >= 6:
            break
    return deduped


def generate_source_queries(
    normalized_claims: list[dict],
    original_query: str = "",
) -> list[dict]:
    source_queries = []

    for index, claim in enumerate(normalized_claims or []):
        keywords = _keywords_from_claim(claim)
        compact_query = " ".join(keywords[:5]) or claim.get("claim_text") or original_query
        official_query = f"site:go.kr {compact_query}".strip()
        contradiction_base = " ".join(keywords[:4]) or compact_query

        query_specs = [
            ("original_query", original_query, "news_context"),
            ("claim_keyword_query", compact_query, "support"),
            ("official_query", official_query, "primary_source"),
            ("contradiction_query", f"{contradiction_base} 반박", "contradiction"),
            ("denial_query", f"{contradiction_base} 사실 아님", "fact_check"),
            ("official_explanation_query", f"{contradiction_base} 금융위 해명", "update"),
        ]

        for query_type, query, purpose in query_specs:
            if not query:
                continue
            source_queries.append(
                {
                    "claim_index": index,
                    "claim_text": claim.get("claim_text") or "",
                    "query_type": query_type,
                    "query": query[:120],
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
    return source_candidates


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
        "source_candidates": source_candidates,
    }
