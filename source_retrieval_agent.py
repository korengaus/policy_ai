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

OFFICIAL_DOMAIN_QUERY_HINTS = {
    "\uae08\uc735\uc704": "site:fsc.go.kr",
    "\uae08\uc735\uc704\uc6d0\ud68c": "site:fsc.go.kr",
    "\uae08\uac10\uc6d0": "site:fss.or.kr",
    "\uae08\uc735\uac10\ub3c5\uc6d0": "site:fss.or.kr",
    "\uad6d\ud1a0\ubd80": "site:molit.go.kr",
    "\uad6d\ud1a0\uad50\ud1b5\ubd80": "site:molit.go.kr",
    "\uae30\uc7ac\ubd80": "site:moef.go.kr",
    "\uae30\ud68d\uc7ac\uc815\ubd80": "site:moef.go.kr",
    "\ud55c\uad6d\uc740\ud589": "site:bok.or.kr",
    "\ud55c\uc740": "site:bok.or.kr",
    "\uad6d\uc138\uccad": "site:nts.go.kr",
    "\uacf5\uc815\uc704": "site:ftc.go.kr",
    "\uacf5\uc815\uac70\ub798\uc704\uc6d0\ud68c": "site:ftc.go.kr",
    "\ubc95\ubb34\ubd80": "site:moj.go.kr",
    "\uacbd\ucc30\uccad": "site:police.go.kr",
    "\uc591\ub3c4\uc138": "site:nts.go.kr",
    "\uc591\ub3c4\uc18c\ub4dd\uc138": "site:nts.go.kr",
    "\uc138\ubb34\uc870\uc0ac": "site:nts.go.kr",
    "\uc804\uc138\uc0ac\uae30": "site:molit.go.kr OR site:khug.or.kr OR site:moj.go.kr",
    "\uc804\uc138\ubcf4\uc99d": "site:khug.or.kr",
    "\uacf5\uacf5\uc8fc\ud0dd": "site:lh.or.kr",
}

POLICY_PROGRAM_TERMS = [
    "\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1",
    "\uc0ac\ud68c\uc5f0\ub300\uae08\uc735\ud611\uc758\ud68c",
    "\uccad\ub144 \ubc84\ud300\ubaa9",
    "\ubc84\ud300\ubaa9 \uc804\uc138\ub300\ucd9c",
    "\uc8fc\ud0dd\ub3c4\uc2dc\uae30\uae08",
    "\uc548\uc2ec\uc804\uc138",
    "DSR",
    "\uc2a4\ud2b8\ub808\uc2a4 DSR",
    "\ubd80\ub3d9\uc0b0 PF",
    "\uae30\uc900\uae08\ub9ac",
    "\ud3ec\uc6a9\uae08\uc735",
    "\uc2e0\uc6a9\ud3c9\uac00",
    "\uc5ec\uc2e0\uc2dc\uc2a4\ud15c",
    "\uc591\ub3c4\uc18c\ub4dd\uc138",
    "\uc138\ubb34\uc870\uc0ac",
    "\ud0c8\ub8e8",
]

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


def _program_terms_from_text(text: str, limit: int = 4) -> list[str]:
    found = []
    clean_text = sanitize_text(text or "")
    for term in POLICY_PROGRAM_TERMS:
        if term in clean_text and term not in found:
            found.append(term)
        if len(found) >= limit:
            break
    return found


def _official_site_query(core_query: str, context_text: str) -> str:
    for keyword, site in OFFICIAL_DOMAIN_QUERY_HINTS.items():
        if keyword in context_text:
            return f"{site} {core_query}".strip()[:100]
    if any(term in context_text for term in ["\ub300\ucd9c", "\uae08\uc735", "\uac00\uacc4\ubd80\ucc44", "DSR"]):
        return f"site:fsc.go.kr OR site:fss.or.kr {core_query}".strip()[:100]
    if any(term in context_text for term in ["\uc591\ub3c4\uc138", "\uc591\ub3c4\uc18c\ub4dd\uc138", "\uc138\ubb34\uc870\uc0ac", "\ud0c8\ub8e8", "\uad6d\uc138\uccad"]):
        return f"site:nts.go.kr {core_query}".strip()[:100]
    if any(term in context_text for term in ["\ubd80\ub3d9\uc0b0", "\uc8fc\ud0dd", "\uc804\uc138", "\uc784\ub300"]):
        return f"site:molit.go.kr OR site:moef.go.kr {core_query}".strip()[:100]
    if "\uae08\ub9ac" in context_text:
        return f"site:bok.or.kr {core_query}".strip()[:100]
    return f"site:go.kr {core_query}".strip()[:100]


def _clean_title_query(article_title: str) -> str:
    title = sanitize_text(article_title or "")
    title = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", title)
    title = re.sub(r"\s*[-|]\s*[^-|]{2,20}$", " ", title)
    tokens = [
        token
        for token in re.findall(r"[\uac00-\ud7a3A-Za-z0-9][\uac00-\ud7a3A-Za-z0-9.%.-]{1,}", title)
        if token not in STOPWORDS and not token.isdigit()
    ]
    return " ".join(tokens[:8])[:90]


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
    article_title: str = "",
) -> list[dict]:
    source_queries = []

    for index, claim in enumerate(normalized_claims or []):
        keywords = _keywords_from_claim(claim)
        numbers = _numbers_from_claim(claim)
        context_text = " ".join([original_query, article_title, claim.get("claim_text") or ""])
        programs = _program_terms_from_text(context_text)
        compact_parts = []
        for term in [*programs, *keywords[:6], *numbers[:2]]:
            if term and term not in compact_parts:
                compact_parts.append(term)
        compact_query = " ".join(compact_parts[:8]) or claim.get("claim_text") or original_query
        official_query = _official_site_query(compact_query, context_text)
        title_query = _clean_title_query(article_title)
        contradiction_query = _compact_contradiction_query(claim)

        query_specs = [
            ("original_query", original_query, "news_context"),
            ("claim_keyword_query", compact_query, "support"),
            ("official_query", official_query, "primary_source"),
            ("official_exact_title_query", _official_site_query(title_query, context_text), "primary_source"),
            ("official_press_query", _official_site_query(f"{compact_query} 보도자료", context_text), "primary_source"),
            ("official_explanation_material_query", _official_site_query(f"{compact_query} 설명자료 해명자료", context_text), "primary_source"),
            ("official_notice_query", _official_site_query(f"{compact_query} 고시 공고", context_text), "primary_source"),
            ("official_title_query", _official_site_query(" ".join([original_query, article_title, *programs])[:90], context_text), "primary_source"),
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
        for source in (official_source_candidates or [])[:5]:
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


def _candidate_dedupe_key(source: dict) -> tuple:
    url = (source.get("url") or "").split("#")[0].rstrip("/")
    domain = _publisher_from_url(url)
    title = sanitize_text(source.get("title") or "").lower()
    return (
        int(source.get("claim_index") or 0),
        url.lower(),
        title,
        domain,
        source.get("source_type") or "",
    )


def _dedupe_source_candidates(source_candidates: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for source in _stable_sort_source_candidates(source_candidates or []):
        key = _candidate_dedupe_key(source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


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
        article_title=news.get("title") or "",
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
        "source_candidates": _dedupe_source_candidates(source_candidates),
    }
