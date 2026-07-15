import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests import RequestException, Timeout
from requests.structures import CaseInsensitiveDict

from official_site_parsers import (
    extract_links_for_site,
    get_site_key,
    is_bad_official_link,
)
from official_document_classifier import EXCLUDED_DOCUMENT_TYPES, classify_official_document
from official_relevance import score_document_relevance, extract_query_terms
from official_source_search import build_official_search_url
from text_utils import decode_response_text, sanitize_data, sanitize_text

from structured_logging import get_logger

log = get_logger(__name__)

try:
    from official_browser_crawler import extract_rendered_links
except Exception:
    extract_rendered_links = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}
# audit §1.5 #5 (2026-05-26): verdict-pipeline document-fetch gates.
# Values are calibration-pinned. See docs/MAGIC_THRESHOLDS.md §1 for
# the full catalog entry (calibration source, downstream consequence,
# re-evaluation trigger). Changing any value here requires its own
# milestone with verdict regression proof — at minimum the suites in
# tests/test_verdict_label_b08_fix.py and
# tests/test_verdict_producer_comparison.py.
MIN_DOCUMENT_SCORE = 25  # candidate-document pre-evaluation gate; lower → more candidates scored (noise risk)
WEAK_DOCUMENT_RELEVANCE_THRESHOLD = 35  # M11.0c B08 weakly_usable boundary; below this → source excluded from verification
DOCUMENT_RELEVANCE_THRESHOLD = 40  # M11.0c B08 strongly_usable boundary; combined with evidence_grade ∈ {A,B,C} gates result["usable"]=True
MATERIAL_MATCH_CONCEPTS = {
    "rental_loan",
    "mortgage_loan",
    "interest_rate",
    "regulation",
    "financial_product_notice",
}

LIST_LINK_TEXTS = {
    "\ubcf4\ub3c4\uc790\ub8cc",
    "\ub354\ubcf4\uae30",
    "\ubaa9\ub85d",
    "list",
    "more",
}
LIST_PAGE_URL_SIGNALS = [
    "search",
    "list",
    "paging",
    "pagination",
    "pageindex",
    "page=",
    "page_no",
    "pageidx",
]

LINK_PRIORITY_KEYWORDS = [
    "\ubcf4\ub3c4\uc790\ub8cc",
    "\uacf5\uc9c0",
    "\uacf5\uace0",
    "\uc815\ucc45",
    "\ub300\ucd9c",
    "\uae08\uc735",
    "\uc8fc\ud0dd",
    "\uc804\uc138",
    "\uc9c0\uc6d0",
    "\ube0c\ub9ac\ud551",
    "\uc124\uba85\uc790\ub8cc",
    "\uc790\ub8cc",
    "press",
    "notice",
    "board",
]

EXCLUDED_LINK_KEYWORDS = [
    "search",
    "\uac80\uc0c9",
    "login",
    "\ub85c\uadf8\uc778",
    "menu",
    "\uba54\ub274",
    "sitemap",
    "\uc0ac\uc774\ud2b8\ub9f5",
    "attach",
    "file",
    "download",
    "\ucca8\ubd80",
    "\ud30c\uc77c",
    "javascript:",
    "mailto:",
    "/index",
    "/main",
    "portal/main",
    "portal/dataviewgov",
    "dataviewgov",
    "myresults",
    "aa040",
    "\ud1b5\ud569\uac80\uc0c9",
]

EXCLUDED_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".pdf",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
)

GENERIC_TITLE_PHRASES = [
    "\ubcf4\ub3c4\uc790\ub8cc",
    "\uac80\uc0c9\uacb0\uacfc",
    "\uc815\ubd8024",
    "\uad6d\ud1a0\uad50\ud1b5\ubd80",
    "\uae08\uc735\uc704\uc6d0\ud68c",
    "\uae08\uc735\uac10\ub3c5\uc6d0",
    "\ud55c\uad6d\uc740\ud589",
    "\uad6d\ud68c",
    "\uacf5\uc9c0\uc0ac\ud56d",
]

CONTENT_SELECTOR_KEYWORDS = [
    "content",
    "article",
    "view",
    "board",
    "press",
    "news",
    "body",
    "detail",
    "cont",
]

TITLE_SELECTOR_KEYWORDS = [
    "title",
    "subject",
    "view",
    "article",
    "board",
]


# ---------------------------------------------------------------------------
# M13.3b — opt-in HTTP cache integration.
#
# When BOTH ``HTTP_CACHE_ENABLED=true`` (M13.3a master flag) AND
# ``OFFICIAL_CRAWLER_CACHE_ENABLED=true`` are set AND the URL's domain
# is in ``GOV_CACHE_ALLOWED_DOMAINS``, ``_request_url`` first checks the
# process-local cache and returns a synthetic ``requests.Response`` on
# hit; on miss it performs the original fetch and stores the bytes
# (200-only, ≤5 MB, ``Cache-Control`` permitting).
#
# Cache-off path is byte-identical to pre-M13.3b: ``_request_url``
# simply delegates to :func:`_do_request_url_raw` which is the original
# function body verbatim. The same pin lives in
# ``tests/test_official_crawler_cache.py::CacheOffByteIdentityTests``.
#
# Conservative defaults:
#     * Domain allow-list: 20 Korean ``.go.kr`` / ``.or.kr`` domains.
#     * TTL: 600 seconds (10 minutes) — far below M13.3a's 1-hour
#       default because government notices may update.
#     * Body cap: 5 MB.
# ---------------------------------------------------------------------------


GOV_CACHE_ALLOWED_DOMAINS = frozenset({
    "fsc.go.kr",
    "fss.or.kr",
    "court.go.kr",
    "gov.kr",
    "korea.kr",
    "moel.go.kr",
    "mohw.go.kr",
    "moef.go.kr",
    "molit.go.kr",
    "msit.go.kr",
    "moe.go.kr",
    "me.go.kr",
    "moj.go.kr",
    "mois.go.kr",
    "mfds.go.kr",
    "kostat.go.kr",
    "law.go.kr",
    "assembly.go.kr",
    "epeople.go.kr",
    "data.go.kr",
})


_OFFICIAL_CRAWLER_CACHE_MAX_BODY_BYTES = 5 * 1024 * 1024


def _is_official_crawler_cache_enabled() -> bool:
    """Returns True iff env ``OFFICIAL_CRAWLER_CACHE_ENABLED == 'true'``
    (case-insensitive, spaces/tabs stripped)."""
    return os.environ.get(
        "OFFICIAL_CRAWLER_CACHE_ENABLED", "",
    ).strip(" \t").lower() == "true"


def _get_official_crawler_cache_ttl_seconds() -> int:
    """Default 600s (10 min). Conservative — government notices may
    update. Override via ``OFFICIAL_CRAWLER_CACHE_TTL_SECONDS``."""
    raw = os.environ.get(
        "OFFICIAL_CRAWLER_CACHE_TTL_SECONDS", "",
    ).strip(" \t")
    if not raw:
        return 600
    try:
        value = int(raw)
    except ValueError:
        return 600
    return value if value > 0 else 600


def _do_request_url_raw(url: str):
    """The pre-M13.3b body of :func:`_request_url`, hoisted so the
    cache-off path is the original fetch code. Do NOT casually edit
    this function — it carries the byte-identicality contract, and any
    change here must still pass the ``CacheOffByteIdentityTests``
    regression pin.

    Deviation from the pre-M13.3b original (CRAWLER-CONNECT-TIMEOUT):
    the request timeout was deliberately split from the scalar ``10``
    to ``(connect=3s, read=10s)`` so unreachable gov hosts fail fast
    during batch backfill instead of burning the full budget on TCP
    connect. The read budget (10s) and the retry count (``range(2)``)
    are UNCHANGED, so a live-but-slow host still gets its full read
    window and returns byte-identical data — only unreachable hosts
    are affected, and they yielded nothing anyway.

    That split does NOT weaken the contract: this function is the sole
    fetch for BOTH the cache-off and cache-on paths, so they change
    together and cache-off ≡ cache-on ≡ pre-cache behavior still
    holds. Preserve that property in any future edit."""
    last_error = None

    for _ in range(2):
        try:
            return requests.get(
                url,
                headers=REQUEST_HEADERS,
                timeout=(3, 10),
            )
        except (ConnectionError, Timeout, RequestException) as exc:
            last_error = exc

    raise last_error


def _response_from_cache_entry(entry, url: str):
    """Construct a ``requests.Response`` that behaves identically to
    the live one for the attributes consumed by the four callers
    (``status_code``, ``raise_for_status``, ``content``, ``encoding``,
    ``apparent_encoding``, ``text``, ``headers``, ``url``).

    Other ``requests.Response`` attributes — ``elapsed``, ``cookies``,
    ``request``, ``history`` — are left at their default empty / None
    values; no current call site touches them.
    """
    response = requests.Response()
    response._content = entry.body  # noqa: SLF001 — public-via-property attribute
    response.status_code = entry.status_code
    response.headers = CaseInsensitiveDict(entry.headers or {})
    response.url = url
    response.reason = "OK" if entry.status_code == 200 else ""
    return response


def _request_url(url: str):
    """Cache-gated wrapper around :func:`_do_request_url_raw`.

    The cache-on path activates only when ALL of the following hold:

    * ``OFFICIAL_CRAWLER_CACHE_ENABLED=true`` (this milestone's flag).
    * ``HTTP_CACHE_ENABLED=true`` (M13.3a master flag).
    * The URL's domain is in :data:`GOV_CACHE_ALLOWED_DOMAINS`.

    Otherwise this function delegates straight to
    :func:`_do_request_url_raw` so the byte-identicality guarantee
    holds.
    """
    if not _is_official_crawler_cache_enabled():
        return _do_request_url_raw(url)

    # M13.3a's master flag must also be set, and the domain must be in
    # the conservative allow-list. Both are cheap stdlib lookups; we do
    # them here so a stray flag set on the official-crawler side alone
    # cannot bypass the master cache toggle.
    try:
        from http_cache import (
            extract_domain,
            get_default_cache,
            is_http_cache_enabled,
        )
        from structured_logging import get_logger
    except Exception:  # noqa: BLE001 — defensive; cache infra must never block fetches
        return _do_request_url_raw(url)

    if not is_http_cache_enabled():
        return _do_request_url_raw(url)
    if extract_domain(url) not in GOV_CACHE_ALLOWED_DOMAINS:
        return _do_request_url_raw(url)

    cache = get_default_cache()
    log = get_logger(__name__)

    # Try the cache first. ``cache.get`` already never raises.
    entry = cache.get(url)
    if entry is not None:
        log.info(
            "official_crawler_cache_event",
            extra={
                "url": url,
                "status_code": entry.status_code,
                "cache_hit": True,
                "body_bytes": entry.bytes_size,
            },
        )
        return _response_from_cache_entry(entry, url)

    # Miss — perform the original fetch. Any network exception
    # propagates unchanged.
    response = _do_request_url_raw(url)

    # Store only safe responses. Eligibility: HTTP 200 + body in bytes
    # form + body under the size cap. ``cache.put`` additionally
    # enforces Cache-Control: no-store / no-cache / private refusal.
    try:
        body_bytes = response.content or b""
        if (
            response.status_code == 200
            and len(body_bytes) <= _OFFICIAL_CRAWLER_CACHE_MAX_BODY_BYTES
        ):
            cache.put(
                url=url,
                body=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                ttl_seconds=_get_official_crawler_cache_ttl_seconds(),
            )
    except Exception as exc:  # noqa: BLE001 — cache must not affect fetches
        log.warning(
            "official_crawler_cache_put_failed",
            extra={"url": url, "error": str(exc)},
        )

    log.info(
        "official_crawler_cache_event",
        extra={
            "url": url,
            "status_code": response.status_code,
            "cache_hit": False,
            "body_bytes": (
                len(response.content) if response.content else 0
            ),
        },
    )
    return response


def _response_text(response) -> str:
    text, encoding = decode_response_text(response)
    response.encoding = encoding
    return text


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()

    return soup


def _normalize_text(value) -> str:
    return sanitize_text(" ".join(str(value or "").split()).strip())


def _extract_html_text(html: str, max_chars: int = 1500) -> tuple[str | None, str]:
    soup = _clean_soup(html)
    title = _extract_document_title(soup)[0]
    text = soup.get_text(" ", strip=True)
    return title, text[:max_chars]


def _is_generic_title(title: str | None) -> bool:
    normalized = _normalize_text(title)

    if not normalized:
        return True

    if len(normalized) <= 8:
        return any(phrase in normalized for phrase in GENERIC_TITLE_PHRASES)

    return any(normalized == phrase for phrase in GENERIC_TITLE_PHRASES) or any(
        phrase in normalized and len(normalized) <= len(phrase) + 8
        for phrase in GENERIC_TITLE_PHRASES
    )


def _title_quality(title: str | None) -> str:
    return "generic" if _is_generic_title(title) else "specific"


def _extract_document_title(soup: BeautifulSoup) -> tuple[str | None, str]:
    candidates = []

    for tag_name in ["h1", "h2", "h3"]:
        tag = soup.find(tag_name)
        if tag:
            text = _normalize_text(tag.get_text(" ", strip=True))
            if text:
                candidates.append((text, tag_name))

    og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
    if og_title and og_title.get("content"):
        candidates.append((_normalize_text(og_title.get("content")), "og:title"))

    title_tag = soup.find("title")
    if title_tag:
        text = _normalize_text(title_tag.get_text(" ", strip=True))
        if text:
            candidates.append((text, "title"))

    for element in soup.find_all(True):
        identity = " ".join(
            [
                " ".join(element.get("class") or []),
                str(element.get("id") or ""),
            ]
        ).lower()
        if not any(keyword in identity for keyword in TITLE_SELECTOR_KEYWORDS):
            continue

        text = _normalize_text(element.get_text(" ", strip=True))
        if text and len(text) <= 180:
            candidates.append((text, "title-like-element"))

    for text, method in candidates:
        if not _is_generic_title(text):
            return text, method

    if candidates:
        return candidates[0]

    return None, "none"


def _extract_document_body(soup: BeautifulSoup) -> tuple[str, str]:
    candidates = []

    for element in soup.find_all(True):
        identity = " ".join(
            [
                " ".join(element.get("class") or []),
                str(element.get("id") or ""),
            ]
        ).lower()

        if not any(keyword in identity for keyword in CONTENT_SELECTOR_KEYWORDS):
            continue

        text = _normalize_text(element.get_text(" ", strip=True))
        if len(text) >= 80:
            candidates.append((len(text), text))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], "content_candidate"

    body = soup.find("body") or soup
    return _normalize_text(body.get_text(" ", strip=True)), "body_text"


def _extract_document_content(html: str, max_chars: int = 4000) -> dict:
    soup = _clean_soup(html)
    title, title_method = _extract_document_title(soup)
    body_text, body_method = _extract_document_body(soup)

    return {
        "document_title": title,
        "document_title_quality": _title_quality(title),
        "document_text_snippet": body_text[:max_chars],
        "document_text_length": len(body_text),
        "extraction_method": f"title:{title_method}|body:{body_method}",
    }


def _empty_relevance_fields() -> dict:
    return {
        "document_relevance_score": 0,
        "document_relevance_level": "unrelated",
        "matched_query_terms": [],
        "matched_concepts": [],
        "relevance_reasons": [],
        "error_page_detected": False,
        "error_page_reason": None,
        "evaluated_candidate_count": 0,
    }


def _empty_classification_fields() -> dict:
    return {
        "document_type": None,
        "evidence_grade": None,
        "should_exclude_from_verification": False,
        "title_specificity_score": 0,
        "concept_overlap_score": 0,
        "keyword_overlap_score": 0,
        "topic_match_score": 0,
        "document_quality_score": 0,
        "officiality_score": 0,
        "classification_reasons": [],
    }


def _is_weakly_usable_document(result: dict) -> bool:
    if (result.get("document_relevance_score") or 0) < WEAK_DOCUMENT_RELEVANCE_THRESHOLD:
        return False
    if result.get("error_page_detected"):
        return False
    if (result.get("document_text_length") or 0) < 300:
        return False
    if not result.get("selected_document_url"):
        return False
    if result.get("document_type") in EXCLUDED_DOCUMENT_TYPES:
        return False
    if result.get("should_exclude_from_verification"):
        return False
    if result.get("evidence_grade") not in {"A", "B", "C"}:
        return False

    matched_concepts = result.get("matched_concepts") or []
    matched_query_terms = result.get("matched_query_terms") or []
    material_matches = set(matched_concepts) & MATERIAL_MATCH_CONCEPTS
    return len(material_matches) >= 2 or len(matched_query_terms) >= 3


def _has_numeric_id(url: str) -> bool:
    parsed = urlparse(url or "")
    target = f"{parsed.path} {parsed.query}"
    return bool(re.search(r"(?<!\d)\d{4,}(?!\d)", target))


def _url_depth_score(url: str) -> int:
    path_parts = [part for part in urlparse(url or "").path.split("/") if part]
    return len(path_parts)


def _is_list_like_url(url: str) -> bool:
    normalized_url = (url or "").lower()
    parsed = urlparse(url or "")
    path = parsed.path.rstrip("/").lower()

    if re.search(r"/no01010[12]/\d{4,}$", path):
        return False
    if path in {"/no010101", "/no010102"}:
        return True
    if any(signal in normalized_url for signal in LIST_PAGE_URL_SIGNALS):
        return True
    return False


def _is_list_like_text(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {item.lower() for item in LIST_LINK_TEXTS}


def _is_detail_candidate(candidate: dict, site_key: str) -> bool:
    url = candidate.get("url") or ""
    text = candidate.get("text") or ""
    normalized_url = url.lower()
    normalized_text = text.strip()
    id_detected = _has_numeric_id(url)

    if site_key == "fsc":
        path = urlparse(url).path.rstrip("/")
        if re.search(r"/no01010[12]/\d{4,}$", path):
            return True
        return id_detected and any(prefix in path for prefix in ["/no010101/", "/no010102/"])

    if _is_list_like_url(url) or _is_list_like_text(text):
        return False

    if site_key == "ibk":
        if id_detected:
            return True
        if any(signal in normalized_url for signal in ["detail", "view", "dtl"]):
            return True
        if len(normalized_text) > 10 and any(
            keyword in normalized_text
            for keyword in [
                "\ubcf4\ub3c4\uc790\ub8cc",
                "\ub274\uc2a4",
                "\uacf5\uc9c0",
                "\uc0c1\ud488",
                "\uae08\ub9ac",
                "\ub300\ucd9c",
            ]
        ):
            return True
        return False

    return True


def _annotate_candidate_detail_fields(candidate: dict, site_key: str) -> dict:
    candidate["id_detected"] = _has_numeric_id(candidate.get("url") or "")
    candidate["url_depth_score"] = _url_depth_score(candidate.get("url") or "")
    candidate["is_detail_page"] = _is_detail_candidate(candidate, site_key)

    if not candidate["is_detail_page"] and site_key in {"fsc", "ibk"}:
        reason = candidate.get("reason") or candidate.get("link_reason") or ""
        candidate["reason"] = (reason + "; " if reason else "") + "not a valid detail document"
        candidate["link_reason"] = candidate["reason"]

    return candidate


def _link_query_overlap(text, query_terms) -> int:
    # M19-6 (C-1): count query terms present in a candidate's link text.
    # Mirrors scripts/trace_official_detail_selection.py:97-99 — a topical
    # selection signal that is otherwise unused. Ranking-only; never feeds
    # any match/grade/verdict gate.
    body = text or ""
    return sum(1 for term in (query_terms or []) if term and term in body)


def _candidate_selection_key(candidate: dict) -> tuple:
    return (
        1 if candidate.get("is_detail_page") else 0,
        candidate.get("relevance_score") or -1,
        candidate.get("query_overlap_count") or 0,
        1 if candidate.get("id_detected") else 0,
        candidate.get("url_depth_score") or 0,
        len(candidate.get("text") or ""),
        candidate.get("score") or 0,
    )


def _is_repeated_text(text: str) -> bool:
    normalized = " ".join((text or "").split())
    if len(normalized) < 120:
        return False

    words = normalized.split()
    if not words:
        return False

    unique_ratio = len(set(words)) / max(1, len(words))
    return unique_ratio < 0.18


def _document_quality_exclusion_reason(content: dict) -> str | None:
    title = (content.get("document_title") or "").strip()
    text = content.get("document_text_snippet") or ""

    if title == "\ubcf4\ub3c4\uc790\ub8cc":
        return "generic press-list title"
    if len(title) < 6:
        return "document title is too short"
    if len(text.strip()) < 300:
        return "document body is too short"
    if _is_repeated_text(text):
        return "document text appears repetitive"
    return None


def _apply_candidate_title_fallback(content: dict, candidate: dict, site_key: str) -> dict:
    if site_key not in {"fsc", "ibk"}:
        return content

    candidate_text = " ".join((candidate.get("text") or "").split())
    current_title = (content.get("document_title") or "").strip()

    if len(candidate_text) <= 10 or _is_list_like_text(candidate_text):
        return content

    generic_title_signals = [
        "\ubcf4\ub3c4\uc790\ub8cc -",
        "\uc704\uc6d0\ud68c \uc18c\uc2dd",
        "\uae08\uc735\uc704\uc6d0\ud68c",
        "ibk\uae30\uc5c5\uc740\ud589",
    ]
    should_replace = (
        not current_title
        or len(current_title) < 12
        or any(signal.lower() in current_title.lower() for signal in generic_title_signals)
    )

    if should_replace:
        content["document_title"] = candidate_text[:300]
        content["document_title_quality"] = "specific"
        method = content.get("extraction_method") or ""
        content["extraction_method"] = f"{method}|title:candidate_link_text" if method else "title:candidate_link_text"

    return content


def _build_search_attempts(search_result: dict) -> list[dict]:
    variants = search_result.get("search_query_variants") or [search_result.get("search_query")]
    source_name = search_result.get("source_name") or ""

    if source_name == "Financial Services Commission":
        return [
            {"query": variants[0] if variants else search_result.get("search_query"), "url": "https://www.fsc.go.kr/no010101"},
            {"query": variants[1] if len(variants) > 1 else search_result.get("search_query"), "url": "https://www.fsc.go.kr/no010102"},
            {"query": variants[2] if len(variants) > 2 else search_result.get("search_query"), "url": "https://www.fsc.go.kr/po010101"},
        ]

    if source_name == "IBK Industrial Bank of Korea":
        search_query = variants[0] if variants else search_result.get("search_query")
        content_urls = [
            search_result.get("official_search_url"),
            "https://www.ibk.co.kr",
            "https://www.ibk.co.kr/common/navigation.ibk",
            "https://www.ibk.co.kr/product",
            "https://www.ibk.co.kr/loan",
            "https://www.ibk.co.kr/finance",
            "https://www.ibk.co.kr/customer",
            "https://www.ibk.co.kr/news",
            "https://www.ibk.co.kr/pr",
            "https://www.ibk.co.kr/common/board",
        ]
        return [
            {"query": variants[index] if index < len(variants) else search_query, "url": url}
            for index, url in enumerate(content_urls)
            if url
        ]

    attempts = []

    for query in variants[:3]:
        if not query:
            continue
        attempts.append(
            {
                "query": query,
                "url": build_official_search_url(
                    source_name=search_result.get("source_name") or "",
                    source_type=search_result.get("source_type") or "",
                    search_query=query,
                ),
            }
        )

    if not attempts:
        fallback_url = (
            search_result.get("official_search_url")
            or search_result.get("search_url")
            or search_result.get("url")
        )
        attempts.append({"query": search_result.get("search_query"), "url": fallback_url})

    return attempts[:3]


def _same_domain(url: str, base_url: str) -> bool:
    url_host = urlparse(url).netloc.lower()
    base_host = urlparse(base_url).netloc.lower()
    return bool(url_host and base_host and url_host == base_host)


def _is_excluded_link(url: str, link_text: str) -> bool:
    normalized = f"{url} {link_text}".lower()
    parsed_path = urlparse(url).path.lower()

    if not url or url.startswith("#"):
        return True
    if parsed_path.endswith(EXCLUDED_EXTENSIONS):
        return True

    return any(keyword.lower() in normalized for keyword in EXCLUDED_LINK_KEYWORDS)


def _score_result_link(url: str, link_text: str, base_url: str) -> int:
    normalized = f"{url} {link_text}".lower()
    score = 0
    priority_hit = False
    detail_hit = any(
        part in url.lower()
        for part in ["view", "detail", "board", "bbs", "notice", "news", "dtl"]
    )

    if _same_domain(url, base_url):
        score += 25

    for keyword in LINK_PRIORITY_KEYWORDS:
        if keyword.lower() in normalized:
            priority_hit = True
            score += 12

    if len(link_text.strip()) >= 8:
        score += 5
    if detail_hit:
        score += 8

    if not priority_hit and not detail_hit:
        return 0

    return score


def extract_official_result_links(search_html: str, base_url: str, max_links: int = 5) -> list:
    soup = BeautifulSoup(search_html or "", "html.parser")
    seen_urls = set()
    candidates = []

    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        link_text = anchor.get_text(" ", strip=True)

        if not href:
            continue

        absolute_url = urljoin(base_url, href)

        if absolute_url in seen_urls:
            continue
        if _is_excluded_link(absolute_url, link_text):
            continue

        score = _score_result_link(absolute_url, link_text, base_url)

        if score <= 0:
            continue

        seen_urls.add(absolute_url)
        candidates.append(
            {
                "url": absolute_url,
                "text": link_text[:200],
                "score": score,
                "same_domain": _same_domain(absolute_url, base_url),
            }
        )

    candidates.sort(
        key=lambda item: (item["same_domain"], item["score"], len(item["text"])),
        reverse=True,
    )

    return candidates[:max_links]


def _count_rejected_links(search_html: str, base_url: str) -> int:
    soup = BeautifulSoup(search_html or "", "html.parser")
    rejected_count = 0

    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        link_text = anchor.get_text(" ", strip=True)

        if not href:
            continue

        absolute_url = urljoin(base_url, href)

        if is_bad_official_link(absolute_url, link_text) or _is_excluded_link(absolute_url, link_text):
            rejected_count += 1

    return rejected_count


def _extract_candidate_links(
    search_html: str,
    search_url: str,
    source_name: str,
    query: str,
) -> tuple[list, str]:
    try:
        site_candidates = extract_links_for_site(
            search_html=search_html,
            base_url=search_url,
            source_name=source_name,
            query=query,
            max_links=5,
        )
    except Exception as exc:
        # M11.7a-2 Site 5b: structured warning so site-specific parser
        # regressions surface in JSON logs. Return shape unchanged —
        # falls through to the generic_fallback parser below.
        #
        # M11.7c: intentionally broad — narrowing reviewed and rejected.
        # `extract_links_for_site` dispatches to per-site parsers
        # (FSS / FSC / IBK / MOLIT / Gov24 / BOK / Assembly) whose
        # failure modes are unbounded — BS4 AttributeError on layout
        # changes, KeyError from internal mapping tables, IndexError
        # from list ops, ValueError from urljoin on malformed hrefs.
        # The contract documented in the audit is "a broken site-
        # specific parser MUST NOT block the generic_fallback parser
        # that runs below" — narrowing here would defeat that
        # guarantee. See docs/EXCEPTION_HANDLING_AUDIT.md Site 5b.
        log.warning(
            "official_crawler.site_specific_parser_failed",
            extra={
                "source_name": source_name,
                "search_url": (search_url or "")[:500],
                "query": (query or "")[:200],
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:500],
                "fallback_returned": "generic_fallback",
            },
        )
        site_candidates = []

    if site_candidates:
        return site_candidates, "site_specific"

    generic_candidates = extract_official_result_links(
        search_html,
        search_url,
        max_links=5,
    )

    for candidate in generic_candidates:
        candidate.setdefault("reason", "generic fallback candidate")

    return generic_candidates, "generic_fallback"


def fetch_best_official_document(search_result: dict, news_context: dict | None = None) -> dict:
    search_attempts = _build_search_attempts(search_result)
    search_url = search_attempts[0].get("url") if search_attempts else (
        search_result.get("official_search_url")
        or search_result.get("search_url")
        or search_result.get("url")
    )
    result = {
        "source_name": search_result.get("source_name"),
        "source_type": search_result.get("source_type"),
        "search_query": search_result.get("search_query"),
        "search_query_used": search_result.get("search_query"),
        "search_query_variants": search_result.get("search_query_variants") or [search_result.get("search_query")],
        "search_attempt_count": 0,
        "search_attempt_results": [],
        "ibk_content_url_used": None,
        "ibk_content_attempt_results": [],
        "reliability_score": search_result.get("reliability_score"),
        "site_key": get_site_key(search_url or "", search_result.get("source_name") or ""),
        "usable": False,
        "weakly_usable": False,
        "parser_used": None,
        "rejected_links_count": 0,
        "browser_fallback_used": False,
        "rendered_links_count": 0,
        "rendered_candidate_links_count": 0,
        "rendered_rejected_links_count": 0,
        "rendered_parser_used": None,
        "raw_links_count": 0,
        "filtered_links_count": 0,
        "final_candidate_links_count": 0,
        "rendered_text_snippet": "",
        "rendered_html_snippet": "",
        "rendered_error": None,
        "rendered_title": None,
        "url": search_url,
        "fetched": False,
        "status_code": None,
        "title": None,
        "text_snippet": "",
        "search_url": search_url,
        "fetched_search_page": False,
        "search_status_code": None,
        "candidate_links": [],
        "selected_document_url": None,
        "document_fetched": False,
        "document_status_code": None,
        "document_title": None,
        "document_title_quality": "generic",
        "document_text_snippet": "",
        "document_text_length": 0,
        "extraction_method": None,
        "selected_document_score": None,
        "selected_document_reason": None,
        "is_detail_page": False,
        "url_depth_score": 0,
        "id_detected": False,
        "error": None,
    }
    result.update(_empty_relevance_fields())
    result.update(_empty_classification_fields())

    if not search_url:
        result["error"] = "No official search URL found for candidate."
        return result

    try:
        search_response = _request_url(search_url)
        result["search_status_code"] = search_response.status_code
        search_response.raise_for_status()
        result["fetched_search_page"] = True

        search_title, search_text_snippet = _extract_html_text(
            _response_text(search_response),
            max_chars=1500,
        )
        result["title"] = search_title
        result["text_snippet"] = search_text_snippet
        result["url"] = search_url
        result["fetched"] = True
        result["status_code"] = search_response.status_code
        result["rejected_links_count"] = _count_rejected_links(
            _response_text(search_response),
            search_url,
        )

        candidate_links, parser_used = _extract_candidate_links(
            search_html=_response_text(search_response),
            search_url=search_url,
            source_name=search_result.get("source_name") or "",
            query=search_result.get("search_query") or "",
        )
        should_use_browser = (
            not candidate_links
            or (
                result["fetched"]
                and result["title"] is None
                and len(result.get("text_snippet") or "") < 200
            )
            or (
                search_result.get("source_name")
                in {
                    "Financial Services Commission",
                    "IBK Industrial Bank of Korea",
                }
            )
            or (
                search_result.get("source_name")
                in {
                    "Bank of Korea",
                }
                and not candidate_links
            )
        )

        if should_use_browser and extract_rendered_links is not None:
            rendered = extract_rendered_links(
                search_url,
                source_name=search_result.get("source_name") or "",
                query=search_result.get("search_query") or "",
                max_links=10,
            )
            result["browser_fallback_used"] = bool(rendered.get("rendered_used"))
            result["rendered_links_count"] = rendered.get("rendered_links_count", 0)
            result["rendered_candidate_links_count"] = rendered.get(
                "rendered_candidate_links_count",
                rendered.get("rendered_links_count", 0),
            )
            result["rendered_rejected_links_count"] = rendered.get("rendered_rejected_links_count", 0)
            result["rendered_parser_used"] = rendered.get("rendered_parser_used")
            result["raw_links_count"] = rendered.get("raw_links_count", 0)
            result["filtered_links_count"] = rendered.get("filtered_links_count", 0)
            result["final_candidate_links_count"] = rendered.get("final_candidate_links_count", 0)
            result["rendered_text_snippet"] = rendered.get("rendered_text_snippet") or ""
            result["rendered_html_snippet"] = rendered.get("rendered_html_snippet") or ""
            result["rendered_error"] = rendered.get("rendered_error")
            result["rendered_title"] = rendered.get("rendered_title")
            parser_used = rendered.get("rendered_parser_used") or parser_used

            if rendered.get("rendered_links"):
                candidate_links = rendered["rendered_links"][:5]
                parser_used = rendered.get("rendered_parser_used") or "browser_site_specific"
        elif should_use_browser and extract_rendered_links is None:
            result["rendered_error"] = "Playwright browser crawler is unavailable."

        result["search_query_used"] = search_result.get("search_query") or result.get("search_query_used")
        result["search_attempt_count"] = 1
        result["search_attempt_results"].append(
            {
                "query": result.get("search_query_used"),
                "url": search_url,
                "fetched": result.get("fetched"),
                "status_code": result.get("status_code"),
                "title": result.get("rendered_title") or result.get("title"),
                "candidate_links_count": len(candidate_links or []),
                "rendered_links_count": result.get("rendered_links_count", 0),
                "raw_links_count": result.get("raw_links_count", 0),
                "filtered_links_count": result.get("filtered_links_count", 0),
                "error": result.get("rendered_error"),
            }
        )

        if not candidate_links:
            remaining_attempts = search_attempts[1:] if result.get("site_key") == "ibk" else search_attempts[1:3]
            for attempt in remaining_attempts:
                attempt_query = attempt.get("query") or ""
                attempt_url = attempt.get("url") or search_url
                attempt_result = {
                    "query": attempt_query,
                    "url": attempt_url,
                    "fetched": False,
                    "status_code": None,
                    "title": None,
                    "candidate_links_count": 0,
                    "rendered_links_count": 0,
                    "raw_links_count": 0,
                    "filtered_links_count": 0,
                    "error": None,
                }
                result["search_attempt_count"] += 1

                try:
                    attempt_response = _request_url(attempt_url)
                    attempt_result["status_code"] = attempt_response.status_code
                    attempt_response.raise_for_status()
                    attempt_result["fetched"] = True
                    attempt_title, attempt_text_snippet = _extract_html_text(
                        _response_text(attempt_response),
                        max_chars=1500,
                    )
                    attempt_result["title"] = attempt_title

                    attempt_candidate_links, attempt_parser_used = _extract_candidate_links(
                        search_html=_response_text(attempt_response),
                        search_url=attempt_url,
                        source_name=search_result.get("source_name") or "",
                        query=attempt_query,
                    )
                    attempt_rendered = {}
                    should_use_attempt_browser = (
                        not attempt_candidate_links
                        or (
                            attempt_title is None
                            and len(attempt_text_snippet or "") < 200
                        )
                        or (
                            search_result.get("source_name")
                            in {
                                "Financial Services Commission",
                                "IBK Industrial Bank of Korea",
                            }
                        )
                        or (
                            search_result.get("source_name")
                            in {
                                "Bank of Korea",
                            }
                            and not attempt_candidate_links
                        )
                    )

                    if should_use_attempt_browser and extract_rendered_links is not None:
                        attempt_rendered = extract_rendered_links(
                            attempt_url,
                            source_name=search_result.get("source_name") or "",
                            query=attempt_query,
                            max_links=10,
                        )
                        attempt_result["rendered_links_count"] = attempt_rendered.get("rendered_links_count", 0)
                        attempt_result["raw_links_count"] = attempt_rendered.get("raw_links_count", 0)
                        attempt_result["filtered_links_count"] = attempt_rendered.get("filtered_links_count", 0)
                        attempt_result["title"] = attempt_rendered.get("rendered_title") or attempt_title

                        if attempt_rendered.get("rendered_links"):
                            attempt_candidate_links = attempt_rendered["rendered_links"][:5]
                            attempt_parser_used = attempt_rendered.get("rendered_parser_used") or "browser_site_specific"
                    elif should_use_attempt_browser and extract_rendered_links is None:
                        attempt_result["error"] = "Playwright browser crawler is unavailable."

                    attempt_result["candidate_links_count"] = len(attempt_candidate_links or [])
                    if result.get("site_key") == "ibk":
                        if (
                            not result.get("ibk_content_url_used")
                            and "search.jsp" not in attempt_url.lower()
                            and (attempt_result.get("raw_links_count") or 0) > 20
                        ):
                            result["ibk_content_url_used"] = attempt_url
                        result["ibk_content_attempt_results"].append(attempt_result.copy())
                    result["search_attempt_results"].append(attempt_result)

                    if attempt_candidate_links:
                        search_url = attempt_url
                        candidate_links = attempt_candidate_links
                        parser_used = attempt_parser_used
                        result["search_query_used"] = attempt_query
                        result["search_query"] = attempt_query
                        result["search_url"] = attempt_url
                        result["url"] = attempt_url
                        result["official_search_url"] = attempt_url
                        result["search_status_code"] = attempt_response.status_code
                        result["fetched_search_page"] = True
                        result["title"] = attempt_title
                        result["text_snippet"] = attempt_text_snippet
                        result["fetched"] = True
                        result["status_code"] = attempt_response.status_code
                        result["rejected_links_count"] = _count_rejected_links(_response_text(attempt_response), attempt_url)
                        result["browser_fallback_used"] = bool(attempt_rendered.get("rendered_used"))
                        result["rendered_links_count"] = attempt_rendered.get("rendered_links_count", 0)
                        result["rendered_candidate_links_count"] = attempt_rendered.get(
                            "rendered_candidate_links_count",
                            attempt_rendered.get("rendered_links_count", 0),
                        )
                        result["rendered_rejected_links_count"] = attempt_rendered.get("rendered_rejected_links_count", 0)
                        result["rendered_parser_used"] = attempt_rendered.get("rendered_parser_used")
                        result["raw_links_count"] = attempt_rendered.get("raw_links_count", 0)
                        result["filtered_links_count"] = attempt_rendered.get("filtered_links_count", 0)
                        result["final_candidate_links_count"] = attempt_rendered.get("final_candidate_links_count", 0)
                        result["rendered_text_snippet"] = attempt_rendered.get("rendered_text_snippet") or ""
                        result["rendered_html_snippet"] = attempt_rendered.get("rendered_html_snippet") or ""
                        result["rendered_error"] = attempt_rendered.get("rendered_error")
                        result["rendered_title"] = attempt_rendered.get("rendered_title")
                        if (
                            result.get("site_key") == "ibk"
                            and not result.get("ibk_content_url_used")
                            and "search.jsp" not in attempt_url.lower()
                            and (attempt_rendered.get("raw_links_count") or 0) > 20
                        ):
                            result["ibk_content_url_used"] = attempt_url
                        break
                except Exception as exc:
                    # M11.7a-2 Site 5c: structured warning for per-attempt
                    # retry-loop failures. Return shape unchanged — the
                    # error string is still captured on attempt_result and
                    # the loop continues to the next query variant.
                    #
                    # M11.7c: intentionally broad — narrowing reviewed and
                    # rejected pending production-log audit. The try-body
                    # fans out across `_request_url` (RequestException
                    # family), `raise_for_status` (HTTPError),
                    # `_response_text` (UnicodeDecodeError), `_extract_html_text`
                    # (BS4 errors), and `_extract_candidate_links` (which
                    # falls through to `extract_official_result_links`
                    # OUTSIDE Site 5b's inner try → BS4/urljoin errors can
                    # surface here). Narrowing to RequestException would
                    # propagate BS4/urljoin/scoring errors up to the outer
                    # wrapper (Site 5e), mis-classifying per-attempt parse
                    # failures as outer-wrapper failures. The M11.7a-2
                    # `exception_type` field in the warning now collects
                    # the data needed for a future evidence-based narrowing
                    # — see docs/EXCEPTION_HANDLING_AUDIT.md Site 5c.
                    log.warning(
                        "official_crawler.attempt_failed",
                        extra={
                            "source_name": result.get("source_name"),
                            "site_key": result.get("site_key"),
                            "attempt_query": (attempt_query or "")[:200],
                            "attempt_url": (attempt_url or "")[:500],
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc)[:500],
                        },
                    )
                    attempt_result["error"] = str(exc)
                    if result.get("site_key") == "ibk":
                        result["ibk_content_attempt_results"].append(attempt_result.copy())
                    result["search_attempt_results"].append(attempt_result)

        result["parser_used"] = parser_used
        query_terms = extract_query_terms(
            result.get("search_query_used") or search_result.get("search_query") or ""
        )
        for candidate in candidate_links:
            candidate.setdefault("link_score", candidate.get("score"))
            candidate.setdefault("link_reason", candidate.get("reason"))
            candidate.setdefault("relevance_score", None)
            candidate.setdefault("relevance_level", None)
            candidate["query_overlap_count"] = _link_query_overlap(candidate.get("text"), query_terms)
            _annotate_candidate_detail_fields(candidate, result.get("site_key") or "")
        result["candidate_links"] = candidate_links

        if not candidate_links:
            result["error"] = "No official document candidate links found on search page."
            result["selected_document_reason"] = "no usable candidate links after filtering"
            return result

        evaluation_candidates = candidate_links
        if result.get("site_key") in {"fsc", "ibk"}:
            evaluation_candidates = [
                candidate
                for candidate in candidate_links
                if candidate.get("is_detail_page")
                and len(candidate.get("text") or "") > 10
                and not _is_list_like_url(candidate.get("url") or "")
                and not _is_list_like_text(candidate.get("text") or "")
            ]

        if not evaluation_candidates:
            result["error"] = "No valid detail document candidate links found after list/index filtering."
            result["selected_document_reason"] = "no valid detail document candidates after filtering"
            result["should_exclude_from_verification"] = True
            result["document_type"] = "service_index_page"
            result["evidence_grade"] = "F"
            return result

        evaluation_candidates = sorted(evaluation_candidates, key=_candidate_selection_key, reverse=True)

        evaluated = []
        for candidate in evaluation_candidates[:5]:
            if is_bad_official_link(candidate.get("url"), candidate.get("text")):
                continue

            candidate_score = candidate.get("score") or 0
            candidate["link_score"] = candidate_score
            candidate["link_reason"] = candidate.get("reason")

            if candidate_score < MIN_DOCUMENT_SCORE:
                candidate["relevance_score"] = 0
                candidate["relevance_level"] = "unrelated"
                continue

            try:
                document_response = _request_url(candidate.get("url"))
                document_status_code = document_response.status_code
                document_response.raise_for_status()
                content = _extract_document_content(_response_text(document_response), max_chars=4000)
                content = _apply_candidate_title_fallback(
                    content,
                    candidate,
                    result.get("site_key") or "",
                )
                quality_reason = _document_quality_exclusion_reason(content)
                if quality_reason:
                    candidate["relevance_score"] = 0
                    candidate["relevance_level"] = "unrelated"
                    candidate["relevance_error"] = quality_reason
                    continue
                document = {
                    **content,
                    "url": candidate.get("url"),
                    "document_status_code": document_status_code,
                }
                relevance = score_document_relevance(
                    news_context={
                        **(news_context or {}),
                        "search_query": search_result.get("search_query") or "",
                    },
                    candidate=candidate,
                    document=document,
                )
                candidate["relevance_score"] = relevance["relevance_score"]
                candidate["relevance_level"] = relevance["relevance_level"]
                evaluated.append(
                    {
                        "candidate": candidate,
                        "document": document,
                        "relevance": relevance,
                    }
                )
            except Exception as exc:
                # M11.7a-2 Site 5d: structured warning for per-candidate
                # document evaluation failures. Return shape unchanged —
                # candidate marked error_page and excluded from `evaluated`.
                #
                # M11.7c: intentionally broad — narrowing reviewed and
                # rejected pending production-log audit. The try-body
                # fans out across `_request_url` (RequestException
                # family + MissingSchema/InvalidURL on malformed candidate
                # URLs), `raise_for_status` (HTTPError),
                # `_extract_document_content` (BS4/trafilatura),
                # `_apply_candidate_title_fallback` /
                # `_document_quality_exclusion_reason` (string ops), and
                # `score_document_relevance` (KeyError/TypeError on
                # malformed scoring inputs). Narrowing to RequestException
                # would mis-classify scoring/parsing errors as outer-
                # wrapper failures. The M11.7a-2 `exception_type` field
                # in the warning now collects the data needed for a
                # future evidence-based narrowing — see
                # docs/EXCEPTION_HANDLING_AUDIT.md Site 5d.
                log.warning(
                    "official_crawler.candidate_evaluation_failed",
                    extra={
                        "source_name": result.get("source_name"),
                        "site_key": result.get("site_key"),
                        "candidate_url": (candidate.get("url") or "")[:500],
                        "candidate_score": candidate.get("score"),
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:500],
                        "fallback_relevance_level": "error_page",
                    },
                )
                candidate["relevance_score"] = 0
                candidate["relevance_level"] = "error_page"
                candidate["relevance_error"] = str(exc)

        result["evaluated_candidate_count"] = len(evaluated)

        if not evaluated:
            result["error"] = "No candidate documents could be evaluated."
            result["selected_document_reason"] = "no usable candidate links after filtering"
            return result

        evaluated.sort(
            key=lambda item: (
                item["relevance"]["relevance_score"],
                item["candidate"].get("query_overlap_count") or 0,
                1 if item["candidate"].get("is_detail_page") else 0,
                1 if item["candidate"].get("id_detected") else 0,
                item["candidate"].get("url_depth_score") or 0,
                len(item["candidate"].get("text") or ""),
            ),
            reverse=True,
        )
        best = evaluated[0]
        selected_link = best["candidate"]
        selected_score = selected_link.get("score") or 0
        selected_reason = selected_link.get("reason")
        relevance = best["relevance"]

        result["selected_document_score"] = selected_score
        result["selected_document_reason"] = selected_reason
        result["is_detail_page"] = bool(selected_link.get("is_detail_page"))
        result["url_depth_score"] = selected_link.get("url_depth_score") or 0
        result["id_detected"] = bool(selected_link.get("id_detected"))
        result["document_relevance_score"] = relevance["relevance_score"]
        result["document_relevance_level"] = relevance["relevance_level"]
        result["matched_query_terms"] = relevance["matched_query_terms"]
        result["matched_concepts"] = relevance["matched_concepts"]
        result["relevance_reasons"] = relevance["relevance_reasons"]
        result["error_page_detected"] = relevance["error_page_detected"]
        result["error_page_reason"] = relevance["error_page_reason"]
        best_document = best["document"]
        result["selected_document_url"] = selected_link.get("url")
        result["document_status_code"] = best_document.get("document_status_code")
        result["document_title"] = best_document.get("document_title")
        result["document_title_quality"] = best_document.get("document_title_quality")
        result["document_text_snippet"] = best_document.get("document_text_snippet")
        result["document_text_length"] = best_document.get("document_text_length")
        result["extraction_method"] = best_document.get("extraction_method")
        result["document_fetched"] = True

        classification = classify_official_document(
            {
                **best_document,
                "selected_document_url": result.get("selected_document_url"),
                "document_relevance_score": result.get("document_relevance_score"),
                "matched_query_terms": result.get("matched_query_terms"),
                "matched_concepts": result.get("matched_concepts"),
                "error_page_detected": result.get("error_page_detected"),
            },
            source_name=search_result.get("source_name") or "",
            site_key=result.get("site_key") or "",
        )
        result.update(classification)

        if relevance["relevance_score"] < WEAK_DOCUMENT_RELEVANCE_THRESHOLD:
            result["error"] = f"Best official document relevance below threshold: {relevance['relevance_score']}"
            result["usable"] = False
            result["weakly_usable"] = False
            return result

        if result.get("should_exclude_from_verification"):
            result["usable"] = False
            result["weakly_usable"] = False
            result["error"] = "Official document excluded from verification: " + "; ".join(
                result.get("classification_reasons") or []
            )
            return result

        if relevance["relevance_score"] >= DOCUMENT_RELEVANCE_THRESHOLD and result.get("evidence_grade") in {"A", "B", "C"}:
            result["usable"] = True
            result["weakly_usable"] = False
        elif _is_weakly_usable_document(result):
            result["usable"] = False
            result["weakly_usable"] = True
            result["error"] = (
                "Best official document is weakly usable: "
                f"score={relevance['relevance_score']}, grade={result.get('evidence_grade')}"
            )
        else:
            result["usable"] = False
            result["weakly_usable"] = False
            result["error"] = "Best official document did not pass strengthened weakly_usable checks: " + "; ".join(
                result.get("classification_reasons") or []
            )
            return result

    except Exception as exc:
        log.warning(
            "official_crawler.outer_wrapper_failure",
            extra={
                "source_name": result.get("source_name"),
                "site_key": result.get("site_key"),
                "search_query_used": result.get("search_query_used"),
                "search_url": search_url,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:500],
                "fallback_returned": "unusable_result_dict",
            },
        )
        if not result.get("search_attempt_results"):
            result["search_attempt_count"] = max(result.get("search_attempt_count") or 0, 1)
            result["search_attempt_results"].append(
                {
                    "query": result.get("search_query_used"),
                    "url": search_url,
                    "fetched": False,
                    "status_code": result.get("search_status_code"),
                    "candidate_links_count": 0,
                    "rendered_links_count": 0,
                    "error": str(exc),
                }
            )
        result["url"] = search_url
        result["fetched"] = False
        result["status_code"] = result["search_status_code"]
        result["title"] = None
        result["text_snippet"] = ""
        result["error"] = str(exc)
        return result

    return result


# =========================================================================
# M16-speed-2a — parallel fetch_official_evidence
#
# Replaces the original 5-candidate sequential loop with an
# index-mapped ThreadPoolExecutor. Mirrors the M15.0d parallel news
# pattern at main.py:1214-1249 (same future_to_index strategy for
# byte-identical result ordering regardless of completion order).
#
# Concurrency: env-configurable via MAX_PARALLEL_OFFICIAL_CANDIDATES,
# default 3. Setting to 1 reverts to the byte-identical sequential
# path (which is the literal pre-M16-speed-2a code).
#
# Playwright safety: per-thread `sync_playwright()` calls are
# serialized by `official_browser_crawler._PLAYWRIGHT_LOCK`. HTTP-
# only candidates parallelize freely; Playwright-using candidates
# queue behind the lock. This protects the 512MB Render Starter RAM
# ceiling (3 parallel Chromium ≈ 750MB peak → OOM without the lock).
#
# Per-candidate failure isolation: a failure in
# `fetch_best_official_document` for one candidate writes a sentinel
# error dict to the corresponding slot (matching the failure-shape
# the function itself would have returned). Downstream consumers
# (evidence_comparator, policy_confidence, verification_card,
# enrich_official_source_candidates_with_bodies) iterate the list
# with `.get(...)` — they require dict entries, not None, hence the
# sentinel rather than None.
# =========================================================================


def _max_parallel_official_candidates() -> int:
    """Return the per-pipeline parallel-worker limit for
    fetch_official_evidence. Defaults to 3 when the env var is
    unset / invalid. Caller should clamp to len(candidates) so we
    never spawn more workers than candidates."""
    raw = os.environ.get("MAX_PARALLEL_OFFICIAL_CANDIDATES", "").strip()
    if not raw:
        return 3
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return max(1, value)


def _candidate_failure_sentinel(candidate: dict, exc: BaseException) -> dict:
    """Failure-shape dict written into evidence_results when a per-
    candidate fetch_best_official_document call raises in the parallel
    pool. Mirrors the failure-shape fetch_best_official_document itself
    produces on its internal error paths (see e.g. official_crawler.py
    :993, :1266, :1382, :1497) so downstream `.get(...)` consumers
    don't see a structural difference between parallel-pool failure
    and in-function failure."""
    return {
        "source_name": (candidate or {}).get("source_name"),
        "source_type": (candidate or {}).get("source_type"),
        "search_query": (candidate or {}).get("search_query"),
        "fetched": False,
        "usable": False,
        "weakly_usable": False,
        "document_fetched": False,
        "should_exclude_from_verification": False,
        "error": f"parallel_pool_failed: {type(exc).__name__}: {str(exc)[:200]}",
    }


def fetch_official_evidence(candidates: list, max_candidates: int = 3, news_context: dict | None = None) -> list:
    selected_candidates = list(candidates[:max_candidates])
    total = len(selected_candidates)

    if total == 0:
        return []

    max_parallel = min(_max_parallel_official_candidates(), total)

    # Sequential fallback: env=1 OR only one candidate. Preserves
    # byte-identical behavior with the pre-M16-speed-2a code path.
    # Setting MAX_PARALLEL_OFFICIAL_CANDIDATES=1 is the documented
    # rollback (mirrors MAX_PARALLEL_NEWS_ITEMS=1 at main.py:1184).
    if max_parallel <= 1 or total <= 1:
        evidence_results = []
        for candidate in selected_candidates:
            evidence_results.append(
                sanitize_data(fetch_best_official_document(candidate, news_context=news_context))
            )
        return evidence_results

    log.info(
        "M16-speed-2a fetch_official_evidence parallel start: total=%d workers=%d",
        total, max_parallel,
    )

    evidence_results: list = [None] * total

    with ThreadPoolExecutor(
        max_workers=max_parallel,
        thread_name_prefix="m16-speed-2-official-fetch",
    ) as executor:
        future_to_index = {
            executor.submit(
                fetch_best_official_document,
                candidate,
                news_context=news_context,
            ): i
            for i, candidate in enumerate(selected_candidates)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                evidence_results[idx] = sanitize_data(future.result())
            except Exception as exc:  # noqa: BLE001 — M16-speed-2a: isolate per-candidate failure, mirror M15.0d Phase A pattern at main.py:1240
                log.exception(
                    "M16-speed-2a fetch_official_evidence failed for candidate index %d",
                    idx,
                )
                evidence_results[idx] = sanitize_data(
                    _candidate_failure_sentinel(selected_candidates[idx], exc)
                )

    log.info(
        "M16-speed-2a fetch_official_evidence parallel complete: total=%d",
        total,
    )

    return evidence_results


def print_official_evidence_results(evidence_results: list[dict]):
    log.info("\n----- Official evidence fetch -----")

    if not evidence_results:
        log.info("No official evidence fetch attempted.")
        return

    for result in evidence_results:
        log.info(f"- source_name: {result.get('source_name')}")
        log.info(f"  site_key: {result.get('site_key')}")
        log.info(f"  search_query_used: {result.get('search_query_used')}")
        log.info(f"  search_attempt_count: {result.get('search_attempt_count')}")
        log.info(f"  search_query_variants: {', '.join(result.get('search_query_variants') or [])}")
        log.info(f"  ibk_content_url_used: {result.get('ibk_content_url_used')}")
        log.info(f"  ibk_content_attempts_count: {len(result.get('ibk_content_attempt_results') or [])}")
        log.info(f"  usable: {result.get('usable')}")
        log.info(f"  weakly_usable: {result.get('weakly_usable')}")
        log.info(f"  parser_used: {result.get('parser_used')}")
        log.info(f"  rejected_links_count: {result.get('rejected_links_count')}")
        log.info(f"  browser_fallback_used: {result.get('browser_fallback_used')}")
        log.info(f"  rendered_links_count: {result.get('rendered_links_count')}")
        log.info(f"  rendered_candidate_links_count: {result.get('rendered_candidate_links_count')}")
        log.info(f"  rendered_rejected_links_count: {result.get('rendered_rejected_links_count')}")
        log.info(f"  rendered_parser_used: {result.get('rendered_parser_used')}")
        log.info(f"  raw_links_count: {result.get('raw_links_count')}")
        log.info(f"  filtered_links_count: {result.get('filtered_links_count')}")
        log.info(f"  final_candidate_links_count: {result.get('final_candidate_links_count')}")
        log.info(f"  rendered_title: {result.get('rendered_title')}")
        log.info(f"  rendered_text_snippet: {(result.get('rendered_text_snippet') or '')[:1000]}")
        log.info(f"  rendered_html_snippet: {(result.get('rendered_html_snippet') or '')[:2000]}")
        log.info(f"  rendered_error: {result.get('rendered_error')}")
        log.info(f"  fetched: {result.get('fetched')}")
        log.info(f"  status_code: {result.get('status_code')}")
        log.info(f"  title: {result.get('title')}")
        log.info(f"  url: {result.get('url') or result.get('search_url')}")
        log.info(f"  selected_document_url: {result.get('selected_document_url')}")
        log.info(f"  is_detail_page: {result.get('is_detail_page')}")
        log.info(f"  url_depth_score: {result.get('url_depth_score')}")
        log.info(f"  id_detected: {result.get('id_detected')}")
        log.info(f"  document_fetched: {result.get('document_fetched')}")
        log.info(f"  document_title: {result.get('document_title')}")
        log.info(f"  document_title_quality: {result.get('document_title_quality')}")
        log.info(f"  document_text_length: {result.get('document_text_length')}")
        log.info(f"  document_type: {result.get('document_type')}")
        log.info(f"  evidence_grade: {result.get('evidence_grade')}")
        log.info(f"  should_exclude_from_verification: {result.get('should_exclude_from_verification')}")
        log.info(f"  title_specificity_score: {result.get('title_specificity_score')}")
        log.info(f"  concept_overlap_score: {result.get('concept_overlap_score')}")
        log.info(f"  keyword_overlap_score: {result.get('keyword_overlap_score')}")
        log.info(f"  topic_match_score: {result.get('topic_match_score')}")
        log.info(f"  extraction_method: {result.get('extraction_method')}")
        log.info(f"  selected_document_score: {result.get('selected_document_score')}")
        log.info(f"  selected_document_reason: {result.get('selected_document_reason')}")
        log.info(f"  document_relevance_score: {result.get('document_relevance_score')}")
        log.info(f"  document_relevance_level: {result.get('document_relevance_level')}")
        log.info(f"  matched_query_terms: {', '.join(result.get('matched_query_terms') or [])}")
        log.info(f"  matched_concepts: {', '.join(result.get('matched_concepts') or [])}")
        log.info(f"  relevance_reasons: {'; '.join(result.get('relevance_reasons') or [])}")
        log.info(f"  error_page_detected: {result.get('error_page_detected')}")
        log.info(f"  error_page_reason: {result.get('error_page_reason')}")
        log.info(f"  evaluated_candidate_count: {result.get('evaluated_candidate_count')}")
        log.info(f"  candidate_links_count: {len(result.get('candidate_links') or [])}")
        log.info(f"  error: {result.get('error')}")

