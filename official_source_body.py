from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from text_utils import decode_response_text, sanitize_text
from official_metadata import (
    OFFICIAL_AUTHORITY_DOMAINS,
    OFFICIAL_NAME_HINTS as SHARED_OFFICIAL_NAME_HINTS,
    is_official_domain,
    looks_like_official_search_or_index_url,
    name_implies_official,
)


OFFICIAL_DOMAINS = {
    "bok.or.kr",
    "fsc.go.kr",
    "fss.or.kr",
    "molit.go.kr",
    "moef.go.kr",
    "mofa.go.kr",
    "korea.kr",
    "gov.kr",
    "nts.go.kr",
    "customs.go.kr",
    "kdi.re.kr",
    "kosis.kr",
    "stat.go.kr",
    "law.go.kr",
    "epeople.go.kr",
    "hrdkorea.or.kr",
    "mss.go.kr",
    "msit.go.kr",
    "kif.re.kr",
    "hf.go.kr",
    "khug.or.kr",
    "lh.or.kr",
    "hfn.go.kr",
}
OFFICIAL_DOMAINS.update(OFFICIAL_AUTHORITY_DOMAINS)

OFFICIAL_NAME_HINTS = {
    "bank of korea",
    "financial services commission",
    "financial supervisory service",
    "ministry of land, infrastructure and transport",
    "한국은행",
    "금융위원회",
    "금융감독원",
    "국토교통부",
    "기획재정부",
    "국세청",
}

ERROR_PAGE_PATTERNS = {
    "페이지가 없거나",
    "페이지를 찾을 수 없습니다",
    "요청하신 페이지를 찾을 수 없습니다",
    "잘못된 경로",
    "에러페이지",
    "오류",
    "error",
    "not found",
    "404",
    "access denied",
    "forbidden",
}

STOPWORDS = {
    "그리고",
    "그러나",
    "있는",
    "없는",
    "대한",
    "관련",
    "이번",
    "기사",
    "뉴스",
    "정부",
    "정책",
    "것으로",
    "한다고",
    "했다",
    "한다",
    "있다",
    "없다",
}


CONCEPT_GROUPS = {
    "housing_finance": ["\uc804\uc138\ub300\ucd9c", "\uc804\uc138\uc790\uae08", "\ubc84\ud300\ubaa9", "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c", "\uc8fc\ub2f4\ub300", "\uc8fc\ud0dd\uae08\uc735"],
    "real_estate": ["\ubd80\ub3d9\uc0b0", "\uc8fc\ud0dd", "\uc2e4\uac70\uc8fc\uc790", "\uc591\ub3c4\uc138", "\ub2e4\uc8fc\ud0dd", "\uccad\uc57d", "\uc784\ub300\ucc28"],
    "rate_policy": ["\uae08\ub9ac", "\uae30\uc900\uae08\ub9ac", "\uc778\ud558", "\uc778\uc0c1", "\ud1b5\ud654\uc815\ucc45", "\ubb3c\uac00"],
    "financial_regulation": ["DSR", "\uaddc\uc81c", "\uc81c\ud55c", "\uac00\uacc4\ubd80\ucc44", "\uc5f0\uccb4\uc728", "\uac10\ub3c5"],
    "social_finance": ["\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1", "\uc0ac\ud68c\uc5f0\ub300\uae08\uc735", "\uc0ac\ud68c\uc5f0\ub300\uae08\uc735\ud611\uc758\ud68c", "\uae08\uc735\uc9c0\uc6d0"],
    "jeonse_fraud": ["\uc804\uc138\uc0ac\uae30", "\uc804\uc138\ubcf4\uc99d", "\ubcf4\uc99d\uae08", "\uc784\ub300\uc778"],
    "tax_investigation": ["\uad6d\uc138\uccad", "\uc591\ub3c4\uc138", "\uc591\ub3c4\uc18c\ub4dd\uc138", "\uc138\ubb34\uc870\uc0ac", "\ud0c8\ub8e8", "\uac00\uc0c1\uc790\uc0b0"],
}

INSTITUTION_TERMS = [
    "\uae08\uc735\uc704",
    "\uae08\uc735\uc704\uc6d0\ud68c",
    "\uae08\uac10\uc6d0",
    "\uae08\uc735\uac10\ub3c5\uc6d0",
    "\uad6d\ud1a0\ubd80",
    "\uad6d\ud1a0\uad50\ud1b5\ubd80",
    "\uae30\uc7ac\ubd80",
    "\uae30\ud68d\uc7ac\uc815\ubd80",
    "\ud55c\uad6d\uc740\ud589",
    "\ud55c\uc740",
    "\uc8fc\ud0dd\ub3c4\uc2dc\ubcf4\uc99d\uacf5\uc0ac",
    "\uad6d\uc138\uccad",
    "HUG",
    "LH",
]


def _domain(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower().replace("www.", "")
    except Exception:
        return ""


def is_official_source(url: str = "", name: str = "") -> bool:
    domain = _domain(url)
    if is_official_domain(url) or domain.endswith(".go.kr") or domain in OFFICIAL_DOMAINS:
        return True
    if any(domain == item or domain.endswith("." + item) for item in OFFICIAL_DOMAINS):
        return True
    normalized_name = sanitize_text(name or "").lower()
    return (
        name_implies_official(normalized_name)
        or any(hint in normalized_name for hint in OFFICIAL_NAME_HINTS)
        or any(hint.lower() in normalized_name for hint in SHARED_OFFICIAL_NAME_HINTS)
    )


def _extract_title(soup: BeautifulSoup) -> str:
    for selector in ["h1", "h2", "h3"]:
        element = soup.find(selector)
        if element:
            text = sanitize_text(element.get_text(" ", strip=True))
            if len(text) >= 4:
                return text
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        return sanitize_text(og_title.get("content") or "")
    if soup.title:
        return sanitize_text(soup.title.get_text(" ", strip=True))
    return ""


def _extract_body_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()

    candidates = []
    body_selectors = [
        ".view_cont",
        ".view-content",
        ".viewContent",
        ".board_view",
        ".board-view",
        ".bbs_view",
        ".bbs-view",
        ".press_view",
        ".press-view",
        ".news_view",
        ".news-view",
        ".article_view",
        ".article-view",
        ".detail_view",
        ".detail-view",
        ".contents",
        ".content_body",
        ".content-body",
        ".article_body",
        ".article-body",
        ".txt",
        "#contents",
        "#content",
        "#article",
        "#board",
        "#view",
        "[id*=content]",
        "[class*=content]",
        "[id*=article]",
        "[class*=article]",
        "[id*=view]",
        "[class*=view]",
        "[id*=board]",
        "[class*=board]",
        "[id*=press]",
        "[class*=press]",
        "[id*=news]",
        "[class*=news]",
        "[id*=body]",
        "[class*=body]",
        "[id*=detail]",
        "[class*=detail]",
        "[id*=cont]",
        "[class*=cont]",
        "main",
        "article",
    ]
    for selector in body_selectors:
        for element in soup.select(selector):
            text = sanitize_text(element.get_text(" ", strip=True))
            if len(text) >= 120:
                candidates.append((len(text), selector, text))

    if candidates:
        _length, selector, text = max(candidates, key=lambda item: item[0])
        return text, f"selector:{selector}"

    body = soup.body or soup
    return sanitize_text(body.get_text(" ", strip=True)), "body_text"


def fetch_official_source_body(url: str, timeout: int = 10) -> dict:
    result = {
        "url": url or "",
        "ok": False,
        "status_code": None,
        "body_text": "",
        "title": "",
        "source_type": "official_body",
        "failure_reason": None,
        "body_length": 0,
        "extraction_method": None,
    }
    if not url:
        result["failure_reason"] = "official_url_missing"
        return result

    try:
        response = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Connection": "close",
            },
        )
        result["status_code"] = response.status_code
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code >= 400:
            result["failure_reason"] = f"http_status_{response.status_code}"
            return result
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            result["failure_reason"] = "official_pdf_only"
            return result
        if content_type and not any(marker in content_type for marker in ["html", "xml", "text"]):
            result["failure_reason"] = "official_page_not_fetchable"
            return result

        html, encoding = decode_response_text(response)
        soup = BeautifulSoup(html, "html.parser")
        title = _extract_title(soup)
        body_text, method = _extract_body_text(html)
        body_text = sanitize_text(body_text)
        error_blob = f"{title} {body_text[:500]}".lower()
        if any(pattern.lower() in error_blob for pattern in ERROR_PAGE_PATTERNS):
            result.update(
                {
                    "title": title,
                    "body_text": body_text[:8000],
                    "body_length": len(body_text),
                    "extraction_method": f"{method};encoding:{encoding}",
                    "failure_reason": "official_error_or_not_found_page",
                }
            )
            return result

        result.update(
            {
                "ok": len(body_text) >= 300,
                "body_text": body_text[:8000],
                "title": title,
                "body_length": len(body_text),
                "extraction_method": f"{method};encoding:{encoding}",
            }
        )
        if len(body_text) < 300:
            result["failure_reason"] = "official_body_too_short"
        return result
    except requests.Timeout:
        result["failure_reason"] = "official_body_timeout"
    except requests.RequestException as error:
        result["failure_reason"] = f"official_body_fetch_failed: {type(error).__name__}"
    except Exception as error:
        result["failure_reason"] = f"official_body_parse_failed: {type(error).__name__}"
    return result


def _tokens(text: str) -> list[str]:
    cleaned = sanitize_text(text or "")
    raw_tokens = re.findall(r"[\uac00-\ud7a3A-Za-z0-9.%]+", cleaned)
    tokens = []
    for token in raw_tokens:
        variants = {token}
        for suffix in ["으로", "에서", "에게", "까지", "부터", "보다", "처럼", "하고", "하며", "했다", "한다", "되는", "했다", "을", "를", "은", "는", "이", "가", "의", "와", "과", "도", "만", "로"]:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                variants.add(token[: -len(suffix)])
        for variant in variants:
            if len(variant) >= 2 and variant not in STOPWORDS and variant.lower() not in STOPWORDS:
                tokens.append(variant)
    return tokens


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%?|\d{4}년|\d+월|\d+일", text or ""))


def _concepts(text: str) -> set[str]:
    clean_text = sanitize_text(text or "")
    concepts = set()
    for concept, terms in CONCEPT_GROUPS.items():
        if any(term and term in clean_text for term in terms):
            concepts.add(concept)
    return concepts


def _matched_institutions(claim_text: str, body_text: str) -> list[str]:
    claim_clean = sanitize_text(claim_text or "")
    body_clean = sanitize_text(body_text or "")
    matched = []
    for term in INSTITUTION_TERMS:
        if term in claim_clean and term in body_clean and term not in matched:
            matched.append(term)
    return matched


def official_body_supports_claim(claim: dict, body_text: str) -> dict:
    title_text = ""
    if isinstance(claim, dict) and claim.get("_official_title_for_match"):
        title_text = str(claim.get("_official_title_for_match") or "")
    claim_text = " ".join(
        str(claim.get(key) or "")
        for key in [
            "claim_text",
            "actor",
            "action",
            "target",
            "object",
            "quantity",
            "date_or_time",
            "location",
        ]
    )
    # Backward compatible: callers may pass "title body" as body_text only.
    combined_text = sanitize_text(body_text or "")
    title_terms_set = set(_tokens(title_text))
    body_terms = set(_tokens(combined_text))
    claim_terms = Counter(_tokens(claim_text))
    matched_terms = sorted(term for term in claim_terms if term in body_terms)
    matched_title_terms = sorted(term for term in claim_terms if term in title_terms_set)
    claim_numbers = _numbers(claim_text)
    body_numbers = _numbers(body_text)
    matched_numbers = sorted(claim_numbers & body_numbers)
    claim_concepts = _concepts(claim_text)
    body_concepts = _concepts(body_text)
    matched_concepts = sorted(claim_concepts & body_concepts)
    matched_institutions = _matched_institutions(claim_text, body_text)

    material_terms = {
        term
        for term in matched_terms
        if len(term) >= 3
        or term in {"금리", "전세", "대출", "주택", "규제", "지원", "세금", "양도세", "물가"}
    }
    title_match_score = min(20, len([term for term in matched_title_terms if len(term) >= 2]) * 5)
    body_match_score = min(35, len(material_terms) * 7)
    entity_match_score = min(15, len(matched_institutions) * 7)
    numeric_date_match_score = min(20, len(matched_numbers) * 10)
    agency_match_score = 10 if matched_institutions else 0
    concept_score = min(20, len(matched_concepts) * 10)
    score = body_match_score + title_match_score + entity_match_score + numeric_date_match_score + concept_score
    if matched_concepts:
        score += min(5, len(matched_concepts) * 2)
    if matched_numbers:
        score += 0
    if matched_institutions:
        score += 0
    for field in ["actor", "action", "target", "object"]:
        value = sanitize_text(str(claim.get(field) or ""))
        if value and value != "unknown" and value in body_text:
            score += 8

    score = max(0, min(100, score))
    supports = (
        score >= 62
        and len(matched_concepts) >= 1
        and (
            len(material_terms) >= 3
            or (len(material_terms) >= 2 and bool(matched_numbers))
            or (len(material_terms) >= 2 and bool(matched_institutions))
        )
    )
    if supports:
        if score >= 78 and (len(material_terms) >= 4 or matched_numbers or len(matched_concepts) >= 2):
            classification = "strong_official_direct_support"
        else:
            classification = "medium_official_contextual_support"
        reason = "기사 핵심 주장과 제목·수치가 직접 일치하는 공식 상세문서를 찾았습니다." if classification == "strong_official_direct_support" else "같은 기관의 관련 자료는 찾았지만, 기사 핵심 주장과 직접 일치하지는 않습니다."
    elif body_text:
        classification = "weak_official_candidate_only"
        reason = "공식기관 후보는 있으나 제목/본문이 넓은 주제 수준에서만 겹칩니다."
    else:
        classification = "no_usable_official_detail"
        reason = "직접 확인 가능한 공식 상세문서는 찾지 못했습니다."
    return {
        "supports": supports,
        "match_score": score,
        "title_match_score": title_match_score,
        "body_match_score": body_match_score,
        "entity_match_score": entity_match_score,
        "numeric_date_match_score": numeric_date_match_score,
        "agency_match_score": agency_match_score,
        "final_direct_match_score": score,
        "official_direct_match_classification": classification,
        "matched_terms": matched_terms[:12],
        "matched_numbers": matched_numbers[:8],
        "matched_concepts": matched_concepts,
        "matched_institutions": matched_institutions[:6],
        "reason": reason,
    }


def _official_result_for_source(source: dict, official_evidence_results: list[dict]) -> dict:
    publisher = sanitize_text(source.get("publisher") or source.get("title") or "").lower()
    source_url = source.get("url") or ""
    for item in official_evidence_results or []:
        names = " ".join(
            [
                str(item.get("source_name") or ""),
                str(item.get("search_url") or ""),
                str(item.get("selected_document_url") or ""),
            ]
        ).lower()
        if publisher and publisher in names:
            return item
        if source_url and source_url in names:
            return item
    return {}


def _looks_like_search_or_index_url(url: str) -> bool:
    return looks_like_official_search_or_index_url(url)


def enrich_official_source_candidates_with_bodies(
    source_candidates: list[dict],
    official_evidence_results: list[dict],
    normalized_claims: list[dict],
) -> tuple[list[dict], dict]:
    claims_by_index = {
        int(index): claim
        for index, claim in enumerate(normalized_claims or [])
    }
    enriched = []
    failures = Counter()
    fetched = 0
    usable = 0
    matched = 0
    official_count = 0

    for source in source_candidates or []:
        item = dict(source or {})
        is_official = item.get("source_type") in {"official_government", "public_institution"} or is_official_source(
            item.get("url") or "",
            item.get("publisher") or item.get("title") or "",
        )
        if not is_official:
            enriched.append(item)
            continue

        official_count += 1
        original_candidate_url = item.get("url") or ""
        official_result = _official_result_for_source(item, official_evidence_results)
        selected_url = official_result.get("selected_document_url") or ""
        if not selected_url and not _looks_like_search_or_index_url(original_candidate_url):
            selected_url = original_candidate_url
        item["url"] = selected_url
        item["official_body_url"] = selected_url
        item["official_detail_url"] = selected_url
        item["official_search_url"] = official_result.get("search_url") or official_result.get("official_search_url") or item.get("official_search_url") or original_candidate_url
        item["official_document_type"] = official_result.get("document_type")
        item["official_evidence_grade"] = official_result.get("evidence_grade")
        item["official_document_relevance_score"] = official_result.get("document_relevance_score")
        item["official_candidate_error"] = official_result.get("error")
        item["official_should_exclude_from_verification"] = bool(
            official_result.get("should_exclude_from_verification")
        )

        body_text = sanitize_text(official_result.get("document_text_snippet") or "")
        title = sanitize_text(official_result.get("document_title") or item.get("title") or "")
        failure_reason = None
        extraction_method = "official_crawler_document_text"
        body_fetch_ok = len(body_text) >= 300

        if official_result.get("should_exclude_from_verification") or official_result.get("evidence_grade") == "F":
            failure_reason = "official_topic_mismatch"
            body_fetch_ok = False
            body_text = ""
        elif not selected_url:
            if official_result:
                failure_reason = "official_detail_missing"
            elif _looks_like_search_or_index_url(original_candidate_url):
                failure_reason = "official_search_only"
            else:
                failure_reason = "official_detail_url_missing"
        elif _looks_like_search_or_index_url(selected_url):
            failure_reason = "official_search_only"
            body_fetch_ok = False
        elif not body_fetch_ok:
            fetched_body = fetch_official_source_body(selected_url)
            title = fetched_body.get("title") or title
            body_text = sanitize_text(fetched_body.get("body_text") or "")
            body_fetch_ok = bool(fetched_body.get("ok"))
            failure_reason = fetched_body.get("failure_reason")
            extraction_method = fetched_body.get("extraction_method") or "official_body_fetch"

        if body_fetch_ok:
            fetched += 1
            usable += 1
        else:
            failures[failure_reason or "official_body_fetch_failed"] += 1

        claim = dict(claims_by_index.get(int(item.get("claim_index") or 0), {}) or {})
        claim["_official_title_for_match"] = title
        match = official_body_supports_claim(claim, f"{title} {body_text}")
        if body_fetch_ok and match.get("supports"):
            matched += 1

        item.update(
            {
                "title": title or item.get("title") or "",
                "raw_text_available": bool(body_fetch_ok),
                "official_body_fetched": bool(body_fetch_ok),
                "official_body_usable": bool(body_fetch_ok),
                "official_body_text": body_text[:5000] if body_fetch_ok else "",
                "official_body_length": len(body_text),
                "official_body_failure_reason": None if body_fetch_ok else (failure_reason or "official_page_not_fetchable"),
                "official_body_match": bool(body_fetch_ok and match.get("supports")),
                "official_body_match_score": match.get("match_score", 0),
                "official_title_match_score": match.get("title_match_score", 0),
                "official_body_text_match_score": match.get("body_match_score", 0),
                "official_entity_match_score": match.get("entity_match_score", 0),
                "official_numeric_date_match_score": match.get("numeric_date_match_score", 0),
                "official_agency_match_score": match.get("agency_match_score", 0),
                "official_final_direct_match_score": match.get("final_direct_match_score", 0),
                "official_direct_match_classification": match.get("official_direct_match_classification"),
                "official_body_matched_terms": match.get("matched_terms", []),
                "official_body_matched_numbers": match.get("matched_numbers", []),
                "official_body_matched_concepts": match.get("matched_concepts", []),
                "official_body_matched_institutions": match.get("matched_institutions", []),
                "official_body_match_reason": match.get("reason"),
                "retrieval_method": (
                    "official_body_verified"
                    if body_fetch_ok and match.get("supports")
                    else ("official_body_fetched_unmatched" if body_fetch_ok else item.get("retrieval_method"))
                ),
                "official_body_extraction_method": extraction_method,
            }
        )
        enriched.append(item)

    summary = {
        "official_body_candidates": official_count,
        "official_bodies_fetched": fetched,
        "official_bodies_usable": usable,
        "official_body_matches": matched,
        "official_body_failures": dict(sorted(failures.items())),
    }
    print(
        "[OfficialBody] "
        f"candidates={official_count} fetched={fetched} usable={usable} "
        f"matched={matched} failures={summary['official_body_failures']}"
    )
    return enriched, summary
