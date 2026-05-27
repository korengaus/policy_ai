import feedparser
import hashlib
import json
import os
import re
import requests
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse
from googlenewsdecoder import gnewsdecoder

from config import RECENT_DAYS
from text_utils import decode_response_text, sanitize_data, sanitize_text

from structured_logging import get_logger

log = get_logger(__name__)


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

NEWS_CACHE_TTL_SECONDS = 30 * 60
NEWS_CACHE_PATH = Path(".cache") / "news_collection_cache.json"

# M16-speed-1a Part H: gnewsdecoder URL cache.
# Decoder calls hit news.google.com and take 0.7-2.5s per URL on
# Render. The decoded URL is deterministic per Google-News URL within
# Google's redirect-token rotation window (observed >> 24h in
# practice). Always-on (no env flag) — matches news_collection_cache
# and analysis_cache precedent (key-value caches are always on; only
# HTTP-body caches are env-gated). TTL 24h. Failed decodes are NOT
# cached so a transient gnewsdecoder error does not pin a fallback
# for 24h.
GNEWSDECODER_CACHE_TTL_SECONDS = 24 * 60 * 60
GNEWSDECODER_CACHE_PATH = Path(".cache") / "gnewsdecoder_cache.json"

MEDIA_ONLY_TITLES = {
    "SBS Biz",
    "SBSBiz",
    "\uc5f0\ud569\ub274\uc2a4",
    "\ub274\uc2a41",
    "\uc774\ub370\uc77c\ub9ac",
    "\ud55c\uad6d\uacbd\uc81c",
    "\ub9e4\uc77c\uacbd\uc81c",
    "\uc870\uc120\uc77c\ubcf4",
    "\uc911\uc559\uc77c\ubcf4",
    "\ud55c\uaca8\ub808",
    "\uacbd\ud5a5\uc2e0\ubb38",
    "\uba38\ub2c8\ud22c\ub370\uc774",
    "\ud30c\uc774\ub0b8\uc15c\ub274\uc2a4",
    "\uc11c\uc6b8\uacbd\uc81c",
    "\uc544\uc2dc\uc544\uacbd\uc81c",
    "\ud5e4\ub7f4\ub4dc\uacbd\uc81c",
    "\ub514\uc9c0\ud138\ud0c0\uc784\uc2a4",
    "\uc804\uc790\uc2e0\ubb38",
    "\ub274\uc2dc\uc2a4",
    "KBS \ub274\uc2a4",
    "MBC \ub274\uc2a4",
    "SBS \ub274\uc2a4",
    "YTN",
}

LOW_QUALITY_TITLE_PHRASES = {
    "\uc120\uc815\ub41c \uc8fc\uc694\uae30\uc0ac",
    "\uc2ec\uce35\uae30\ud68d \uae30\uc0ac\uc785\ub2c8\ub2e4",
    "\uc5b8\ub860\uc0ac \uad6c\ub3c5\ud558\uc138\uc694",
    "\ub124\uc774\ubc84 \uba54\uc778",
    "\uce74\ud14c\uace0\ub9ac \uc548\ub0b4",
    "\ub354\ubcf4\uae30",
    "\ub274\uc2a4 \ubaa9\ub85d",
    "\ucd94\ucc9c \uae30\uc0ac",
    "\uc5b8\ub860\uc0ac\uac00 \uc120\uc815\ud55c \uc8fc\uc694\uae30\uc0ac",
    "메뉴",
    "바로가기",
    "로그인",
    "뉴스홈",
    "전체보기",
    "구독",
    "설정",
}

UI_ONLY_TITLES = {
    "홈",
    "메뉴",
    "더보기",
    "로그인",
    "뉴스홈",
    "전체보기",
    "구독",
    "설정",
    "메뉴 영역으로 바로가기",
    "본문 영역으로 바로가기",
}

NAVER_NEWS_ZONE_SELECTORS = [
    "div.group_news",
    "ul.list_news",
    "div.news_wrap",
    "div.news_area",
    "div.api_subject_bx",
    "section.sc_new.sp_nnews",
    "div#main_pack div.api_subject_bx",
]

DAUM_NEWS_ZONE_SELECTORS = [
    "div#newsColl",
    "div.coll_cont",
    "ul.c-list-basic",
    "ul.list_news",
    "div.wrap_cont",
    "div.cont_inner",
    "div.item-title",
]

NEWS_LINK_SELECTORS = [
    "a.news_tit",
    "a.f_link_b",
    "a.tit_main",
    "a.link_tit",
    "a[class*='tit']",
    "a[class*='news']",
    "a[href*='news']",
    "a[href*='article']",
    "a[href*='v.daum.net']",
    "a[href*='n.news.naver.com']",
    "a[href*='media.naver.com']",
    "a[href*='oid=']",
    "a[href*='aid=']",
]


def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    return sanitize_text(soup.get_text(" ", strip=True))


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", clean_html(text or "")).strip()


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", sanitize_text(query or "").strip().lower())


def _cache_key(query: str, max_results: int) -> str:
    raw = f"{_normalize_query(query)}|{int(max_results or 0)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_news_cache() -> dict:
    try:
        if NEWS_CACHE_PATH.exists():
            return json.loads(NEWS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as error:
        log.error(f"[NewsCollector] Cache read failed: {error}")
    return {}


def _save_news_cache(cache: dict) -> None:
    try:
        NEWS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        NEWS_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        log.error(f"[NewsCollector] Cache write failed: {error}")


def _cache_entry_fresh(entry: dict) -> bool:
    try:
        cached_at = datetime.fromisoformat(entry.get("cached_at") or "")
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        return age <= NEWS_CACHE_TTL_SECONDS
    except Exception:
        return False


def _published_sort_value(item: dict) -> float:
    try:
        published_date = parsedate_to_datetime(item.get("published", "") or "")
        if published_date.tzinfo is None:
            published_date = published_date.replace(tzinfo=timezone.utc)
        return published_date.timestamp()
    except Exception:
        return 0.0


def _stable_sort_news(items: list[dict]) -> list[dict]:
    indexed = list(enumerate(items or []))
    indexed.sort(
        key=lambda pair: (
            -_published_sort_value(pair[1]),
            _normalize_spaces(pair[1].get("title") or ""),
            pair[1].get("original_url") or pair[1].get("link") or pair[1].get("google_link") or "",
            pair[0],
        )
    )
    return [item for _index, item in indexed]


def _cached_news_response(query: str, max_results: int) -> dict | None:
    key = _cache_key(query, max_results)
    cache = _load_news_cache()
    entry = cache.get(key)
    if not entry or not _cache_entry_fresh(entry):
        return None
    results = sanitize_data(_stable_sort_news(entry.get("results") or []))[:max_results]
    debug = dict(entry.get("debug") or {})
    debug.update(
        {
            "news_cache_hit": True,
            "news_cache_key": key,
            "news_cache_ttl_seconds": NEWS_CACHE_TTL_SECONDS,
            "news_cache_cached_at": entry.get("cached_at"),
            "selected_news_count": len(results),
        }
    )
    log.info(f"[NewsCollector] Cache hit: key={key} selected={len(results)}")
    return {"results": results, "debug": debug}


def _store_news_response(query: str, max_results: int, results: list[dict], debug: dict) -> None:
    key = _cache_key(query, max_results)
    cache = _load_news_cache()
    cache[key] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "query": _normalize_query(query),
        "max_results": max_results,
        "results": sanitize_data(_stable_sort_news(results))[:max_results],
        "debug": sanitize_data(debug or {}),
    }
    _save_news_cache(cache)
    log.info(f"[NewsCollector] Cache stored: key={key} ttl={NEWS_CACHE_TTL_SECONDS}s")


# ---------------------------------------------------------------------------
# M16-speed-1a Part H — gnewsdecoder URL cache
#
# Helpers mirror the news_collection cache pattern above:
#   * _gnewsdecoder_cache_key  → sha1(url)[:16] (same shape as _cache_key)
#   * _load_gnewsdecoder_cache / _save_gnewsdecoder_cache  → disk JSON
#   * _gnewsdecoder_cache_fresh                            → TTL check
#   * _cached_decoder_response / _store_decoder_response    → public API
#   * _reset_gnewsdecoder_cache_for_tests                   → test scaffolding
#
# Wired into resolve_google_news_url AFTER the non-Google short-circuit
# (preserves the assert_not_called contract in
# tests/test_m11_7a_2_exception_logging.py::test_non_google_url_short_circuit_no_error).
# Decoder failures are NOT cached — a transient gnewsdecoder error
# would otherwise pin the fallback (original URL) for 24h.
# ---------------------------------------------------------------------------


def _gnewsdecoder_cache_key(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def _load_gnewsdecoder_cache() -> dict:
    try:
        if GNEWSDECODER_CACHE_PATH.exists():
            return json.loads(GNEWSDECODER_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as error:
        log.error(f"[NewsCollector] gnewsdecoder cache read failed: {error}")
    return {}


def _save_gnewsdecoder_cache(cache: dict) -> None:
    try:
        GNEWSDECODER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GNEWSDECODER_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        log.error(f"[NewsCollector] gnewsdecoder cache write failed: {error}")


def _gnewsdecoder_cache_fresh(entry: dict) -> bool:
    try:
        cached_at = datetime.fromisoformat(entry.get("cached_at") or "")
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        return age <= GNEWSDECODER_CACHE_TTL_SECONDS
    except Exception:
        return False


def _cached_decoder_response(google_url: str) -> Optional[str]:
    if not google_url:
        return None
    key = _gnewsdecoder_cache_key(google_url)
    cache = _load_gnewsdecoder_cache()
    entry = cache.get(key)
    if not entry or not _gnewsdecoder_cache_fresh(entry):
        return None
    decoded = entry.get("decoded_url")
    if not decoded:
        return None
    log.info(
        f"[NewsCollector] gnewsdecoder cache hit: key={key} url={google_url[:80]}"
    )
    return decoded


def _store_decoder_response(google_url: str, decoded_url: str) -> None:
    if not google_url or not decoded_url:
        return
    key = _gnewsdecoder_cache_key(google_url)
    cache = _load_gnewsdecoder_cache()
    cache[key] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "google_news_url": google_url,
        "decoded_url": decoded_url,
    }
    _save_gnewsdecoder_cache(cache)
    log.info(
        f"[NewsCollector] gnewsdecoder cache stored: key={key} "
        f"ttl={GNEWSDECODER_CACHE_TTL_SECONDS}s"
    )


def _reset_gnewsdecoder_cache_for_tests() -> None:
    """Clear the disk-backed gnewsdecoder cache. Used in test setUp to
    prevent state leak between methods. Best-effort — never raises."""
    try:
        if GNEWSDECODER_CACHE_PATH.exists():
            GNEWSDECODER_CACHE_PATH.unlink()
    except Exception:  # noqa: BLE001 — test-only scaffolding
        pass


def _query_terms(query: str) -> list[str]:
    return [
        term.strip()
        for term in re.split(r"[\s,./|·ㆍ]+", query or "")
        if len(term.strip()) >= 2
    ]


def _is_media_only_title(title: str) -> bool:
    normalized = _normalize_spaces(title).strip(" -–—|:")
    return normalized in MEDIA_ONLY_TITLES


def _low_quality_phrase(title: str) -> str | None:
    normalized = _normalize_spaces(title)
    if normalized in UI_ONLY_TITLES:
        return "UI element"
    for phrase in LOW_QUALITY_TITLE_PHRASES:
        if phrase in normalized:
            return phrase
    return None


def _looks_sentence_like(title: str) -> bool:
    normalized = _normalize_spaces(title)
    has_space_or_punctuation = bool(re.search(r"\s|[.?!…\"'“”‘’·ㆍ,-]", normalized))
    has_josa = bool(re.search(r"[은는이가을를에의로과와]\b", normalized))
    has_mixed_words = len(re.findall(r"[가-힣A-Za-z0-9]{2,}", normalized)) >= 2
    return has_space_or_punctuation and (has_josa or has_mixed_words)


def _reject_title_reason(title: str, query: str = "") -> str | None:
    normalized = _normalize_spaces(title)
    if not normalized:
        return "empty title"
    if _low_quality_phrase(normalized):
        return f"low quality phrase: {_low_quality_phrase(normalized)}"
    if _is_media_only_title(normalized):
        return "media name only"
    if normalized.isdigit():
        return "numeric only"
    if not re.search(r"[가-힣A-Za-z0-9]", normalized):
        return "no readable characters"
    if len(normalized) < 15:
        return "too short"
    if not _looks_sentence_like(normalized):
        return "not sentence-like"
    return None


def is_good_news_title(title: str, query: str = "") -> bool:
    return _reject_title_reason(title, query=query) is None


def _candidate_title(link) -> str:
    # Prefer the visible anchor text because title/aria-label often contain
    # service descriptions on portal search pages.
    visible_text = _normalize_spaces(link.get_text(" ", strip=True))
    if visible_text:
        return visible_text

    for attr in ("aria-label", "title"):
        value = _normalize_spaces(link.get(attr, ""))
        if value:
            return value
    return ""


def _absolute_url(href: str, base_url: str) -> str:
    if not href:
        return ""
    return urljoin(base_url, href)


def _is_valid_news_href(href: str, base_url: str) -> tuple[bool, str]:
    if not href:
        return False, "invalid link"
    lowered = href.strip().lower()
    if lowered.startswith(("javascript:", "#", "mailto:")):
        return False, "invalid link"

    absolute = _absolute_url(href, base_url)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "invalid link"

    link_text = absolute.lower()
    news_patterns = [
        "news",
        "article",
        "view",
        "v.daum.net",
        "n.news.naver.com",
        "media.naver.com",
        "oid=",
        "aid=",
    ]
    if not any(pattern in link_text for pattern in news_patterns):
        return False, "invalid link"
    return True, ""


def _news_zones(soup: BeautifulSoup, selectors: list[str]):
    zones = []
    seen = set()
    for selector in selectors:
        for zone in soup.select(selector):
            marker = id(zone)
            if marker in seen:
                continue
            seen.add(marker)
            zones.append(zone)
    return zones


def _iter_news_links(soup: BeautifulSoup, zone_selectors: list[str]):
    zones = _news_zones(soup, zone_selectors)
    seen_links = set()

    for zone in zones:
        for selector in NEWS_LINK_SELECTORS:
            for link in zone.select(selector):
                href = link.get("href", "")
                if not href or href in seen_links:
                    continue
                seen_links.add(href)
                yield link


def _summary_from_container(container) -> str:
    if not container:
        return ""

    selectors = [
        ".news_dsc",
        ".dsc_wrap",
        ".api_txt_lines",
        ".desc",
        ".cont",
        ".txt_info",
        ".desc_news",
        "p",
    ]
    for selector in selectors:
        summary_el = container.select_one(selector)
        if summary_el:
            summary = _normalize_spaces(summary_el.get_text(" ", strip=True))
            if summary:
                return summary
    return ""


def _fallback_item(title: str, link: str, summary: str, source: str) -> dict:
    return sanitize_data({
        "title": _normalize_spaces(title),
        "summary": _normalize_spaces(summary),
        "google_link": link,
        "original_url": link,
        "link": link,
        "published": _utc_now_rfc2822(),
        "published_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    })


def _dedupe_news_items(items: list[dict]) -> list[dict]:
    # NOTE (M15-dedup-1): this helper is only called by the
    # Naver / Daum fallback paths (``_force_select_best`` at L455
    # and ``_accept_fallback_candidate`` at L513). The Google RSS
    # path does NOT route through here — its dedup is handled
    # post-resolve in ``main.analyze_pipeline`` against the decoded
    # ``original_url`` (see "M15-dedup-1 Part A" block in main.py).
    # Do not call this from the Google RSS code path without
    # converting it to use ``original_url`` first; ``google_link``
    # keys differ between syndications even when they resolve to
    # the same upstream article.
    seen = set()
    unique = []

    for item in items:
        key = item.get("original_url") or item.get("google_link") or item.get("link") or item.get("title")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def _raw_fallback_candidate(title: str, href: str, summary: str, source: str, base_url: str) -> dict | None:
    valid, _ = _is_valid_news_href(href, base_url)
    if not valid:
        return None

    original_url = _absolute_url(href, base_url)
    parsed = urlparse(original_url)
    if not parsed.scheme or not parsed.netloc:
        return None

    title = _normalize_spaces(title)
    summary = _normalize_spaces(summary)
    if not title and summary:
        title = summary
    if not title or title.isdigit():
        return None
    return _fallback_item(title, original_url, summary, source)


def _query_tokens_for_scoring(query: str) -> set[str]:
    # M17-search-quality: replaces the hard-coded housing-keyword bias
    # in _candidate_score. Returns lowercased tokens with length >= 2
    # so single-char particles ('의', '를', '이') don't match noise.
    if not query:
        return set()
    normalized = _normalize_query(query)
    return {tok for tok in normalized.split() if len(tok) >= 2}


def _candidate_score(item: dict, query: str | None = None) -> int:
    title = _normalize_spaces(item.get("title", ""))
    url = item.get("original_url") or item.get("link") or ""
    score = min(len(title), 120)

    if _is_media_only_title(title):
        score -= 100
    if _low_quality_phrase(title):
        score -= 80
    if re.search(r"[가-힣]", title):
        score += 20
    if re.search(r"[.?!…\"'“”‘’]|[가-힣]{2,}\s+[가-힣]{2,}", title):
        score += 15
    # M17-search-quality: query-token overlap replaces the previous
    # hard-coded +25 bonus for 대출/금리/부동산/정책/규제/지원/전세/주택.
    # The old code biased selection toward housing-finance titles
    # regardless of what the user searched for, surfacing 전세대출
    # articles for queries like "기후변화 정책". Now the bonus only
    # fires when the title actually overlaps with the user's query.
    query_tokens = _query_tokens_for_scoring(query) if query else set()
    if query_tokens:
        title_lower = title.lower()
        overlap_count = sum(1 for tok in query_tokens if tok in title_lower)
        if overlap_count >= 1:
            score += 25
        if overlap_count >= 2:
            score += 10
    if any(pattern in url for pattern in ["news", "article", "v.daum.net", "naver.com"]):
        score += 10
    return score


def _force_select_best(items: list[dict], source: str, query: str | None = None) -> list[dict]:
    unique = _dedupe_news_items([item for item in items if item])
    if not unique:
        return []

    best = max(unique, key=lambda candidate: _candidate_score(candidate, query=query))
    log.info("[NewsCollector] Forcing fallback selection: 1 item")
    forced = dict(best)
    forced["source"] = forced.get("source") or source
    forced["forced_fallback"] = True
    return [forced]


def _emergency_search_item(query: str) -> dict:
    search_url = f"https://search.naver.com/search.naver?where=news&query={quote(query)}"
    log.info("[NewsCollector] Forcing fallback selection: 1 item")
    return _fallback_item(
        title=f"{query} 뉴스 검색 결과",
        link=search_url,
        summary="Google/Naver/Daum에서 기사 링크를 확정하지 못해 검색 결과 페이지를 임시 분석 대상으로 사용합니다.",
        source="forced_search_fallback",
    )


def _accept_fallback_candidate(
    items: list[dict],
    raw_candidates: list[dict],
    *,
    title: str,
    href: str,
    summary: str,
    source: str,
    query: str,
    base_url: str,
    max_results: int,
) -> bool:
    title = _normalize_spaces(title)
    log.info(f"[NewsCollector] Raw candidate: {title}")

    valid_link, link_reason = _is_valid_news_href(href, base_url)
    if not valid_link:
        log.info(f"[NewsCollector] Rejected reason: {link_reason}")
        return False

    raw_item = _raw_fallback_candidate(title, href, summary, source, base_url)
    if raw_item:
        raw_candidates.append(raw_item)

    reject_reason = _reject_title_reason(title, query=query)
    if reject_reason:
        log.info(f"[NewsCollector] Rejected reason: {reject_reason}")
        return False

    if not raw_item:
        log.info("[NewsCollector] Rejected reason: invalid URL")
        return False

    items.append(raw_item)
    items[:] = _dedupe_news_items(items)
    log.info(f"[NewsCollector] Accepted title: {title}")
    return len(items) >= max_results


def is_recent(published_text: str, days: int = 30) -> bool:
    try:
        published_date = parsedate_to_datetime(published_text)

        if published_date.tzinfo is None:
            published_date = published_date.replace(tzinfo=timezone.utc)

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return published_date >= cutoff

    except Exception:
        return False


def _utc_now_rfc2822() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _entry_to_news(entry) -> dict:
    return sanitize_data({
        "title": clean_html(entry.get("title", "")),
        "summary": clean_html(entry.get("summary", "")),
        "google_link": entry.get("link", ""),
        "published": entry.get("published", ""),
        "source": "google_rss",
    })


def search_naver_news_fallback(query: str, max_results: int = 3) -> tuple[list[dict], str | None]:
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={quote(query)}"
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        response.raise_for_status()
        html, _encoding = decode_response_text(response)
        soup = BeautifulSoup(html, "html.parser")
        items = []
        raw_candidates = []

        for link in _iter_news_links(soup, NAVER_NEWS_ZONE_SELECTORS):
            href = link.get("href", "")
            title = _candidate_title(link)
            container = link.find_parent(["li", "div"])
            summary = _summary_from_container(container)

            if _accept_fallback_candidate(
                items,
                raw_candidates,
                title=title,
                href=href,
                summary=summary,
                source="naver_fallback",
                query=query,
                base_url=url,
                max_results=max_results,
            ):
                log.info(f"[NewsCollector] Fallback selected: {len(items)}")
                return items[:max_results], None

        if not items:
            items = _force_select_best(raw_candidates, "naver_fallback", query=query)

        log.info(f"[NewsCollector] Fallback selected: {len(items)}")
        return items[:max_results], None
    except Exception as error:
        return [], str(error)


def search_daum_news_fallback(query: str, max_results: int = 3) -> tuple[list[dict], str | None]:
    try:
        url = f"https://search.daum.net/search?w=news&q={quote(query)}"
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        response.raise_for_status()
        html, _encoding = decode_response_text(response)
        soup = BeautifulSoup(html, "html.parser")
        items = []
        raw_candidates = []

        for link in _iter_news_links(soup, DAUM_NEWS_ZONE_SELECTORS):
            href = link.get("href", "")
            title = _candidate_title(link)
            container = link.find_parent(["li", "div"])
            summary = _summary_from_container(container)

            if _accept_fallback_candidate(
                items,
                raw_candidates,
                title=title,
                href=href,
                summary=summary,
                source="daum_fallback",
                query=query,
                base_url=url,
                max_results=max_results,
            ):
                log.info(f"[NewsCollector] Fallback selected: {len(items)}")
                return items[:max_results], None

        if not items:
            items = _force_select_best(raw_candidates, "daum_fallback", query=query)

        log.info(f"[NewsCollector] Fallback selected: {len(items)}")
        return items[:max_results], None
    except Exception as error:
        return [], str(error)


# ---------------------------------------------------------------------------
# M13.3d — opt-in HTTP cache for the Google News RSS fetch only.
#
# When BOTH ``HTTP_CACHE_ENABLED=true`` (M13.3a master flag) AND
# ``NEWS_COLLECTOR_CACHE_ENABLED`` is truthy AND the RSS URL's host is
# ``news.google.com``, :func:`_parse_google_news_rss` first checks a
# module-local cache. On hit, the cached raw bytes are re-parsed via
# ``feedparser.parse(bytes)``. On miss, the bytes are fetched via
# ``requests.get`` (so we can persist them) and then parsed.
#
# Cache-off path is byte-identical to pre-M13.3d:
# ``_parse_google_news_rss`` falls back to ``feedparser.parse(rss_url)``
# which is the original call. The wrapper adds zero observable effect
# when either flag is off or the URL is not a Google News RSS URL.
#
# Naver / Daum / direct fallbacks live in different functions
# (``search_naver_news_fallback``, ``search_daum_news_fallback``) and
# are NOT touched by M13.3d — those are rare error paths where
# freshness matters more than latency.
#
# Conservative defaults:
#     * Host allow-list: exactly ``news.google.com`` (not generalised).
#     * TTL: 300 seconds (5 minutes) — RSS feeds update fast.
#     * Body cap: 2 MB (Google News RSS is small; reject anything huge).
#     * Separate cache instance from the M13.3b/M13.3d crawler caches.
# ---------------------------------------------------------------------------


_GOOGLE_NEWS_RSS_HOST = "news.google.com"
_DEFAULT_RSS_CACHE_TTL_SECONDS = 300  # 5 minutes
_NEWS_COLLECTOR_CACHE_MAX_BODY_BYTES = 2 * 1024 * 1024


def is_news_collector_cache_enabled() -> bool:
    """True iff BOTH ``HTTP_CACHE_ENABLED=true`` (master flag, M13.3a)
    AND ``NEWS_COLLECTOR_CACHE_ENABLED`` is truthy (case-insensitive).
    Any other value → False, so a typo never silently enables the cache.
    """
    try:
        from http_cache import is_http_cache_enabled
    except Exception:  # noqa: BLE001 — never block fetches on cache infra
        return False
    if not is_http_cache_enabled():
        return False
    raw = os.environ.get(
        "NEWS_COLLECTOR_CACHE_ENABLED", "",
    ).strip(" \t").lower()
    return raw in ("1", "true", "yes", "on")


def _get_rss_cache_ttl_seconds() -> int:
    """Default 300s (5 min). Override via
    ``NEWS_COLLECTOR_CACHE_TTL_SECONDS`` env."""
    raw = os.environ.get(
        "NEWS_COLLECTOR_CACHE_TTL_SECONDS", "",
    ).strip(" \t")
    if not raw:
        return _DEFAULT_RSS_CACHE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RSS_CACHE_TTL_SECONDS
    return value if value > 0 else _DEFAULT_RSS_CACHE_TTL_SECONDS


_RSS_CACHE = None  # type: Optional[object]


def _get_rss_cache():
    """Process-local RSS cache singleton, distinct from any other
    HttpCache instance so eviction state is independent."""
    global _RSS_CACHE
    if _RSS_CACHE is None:
        from http_cache import HttpCache
        _RSS_CACHE = HttpCache(
            max_entries=200,
            default_ttl_seconds=_DEFAULT_RSS_CACHE_TTL_SECONDS,
        )
    return _RSS_CACHE


def _reset_rss_cache_for_tests() -> None:
    """Drop the module-local RSS cache singleton."""
    global _RSS_CACHE
    if _RSS_CACHE is not None:
        try:
            _RSS_CACHE.clear()
        except Exception:  # noqa: BLE001
            pass
    _RSS_CACHE = None


def _parse_google_news_rss(rss_url: str):
    """Cache-gated wrapper around ``feedparser.parse(rss_url)``.

    Cache-on activates only when all of the following hold:

    * ``NEWS_COLLECTOR_CACHE_ENABLED`` is truthy.
    * ``HTTP_CACHE_ENABLED=true`` (M13.3a master flag).
    * ``urlparse(rss_url).netloc`` equals ``news.google.com``.

    Otherwise this function delegates straight to
    ``feedparser.parse(rss_url)`` so the byte-identicality guarantee
    holds. Naver / Daum / direct-URL paths must NOT route through
    here; they call ``feedparser`` (or ``requests.get``) directly.
    """
    if not is_news_collector_cache_enabled():
        return feedparser.parse(rss_url)

    try:
        from http_cache import extract_domain
    except Exception:  # noqa: BLE001
        return feedparser.parse(rss_url)

    if extract_domain(rss_url) != _GOOGLE_NEWS_RSS_HOST:
        return feedparser.parse(rss_url)

    cache = _get_rss_cache()

    entry = cache.get(rss_url)
    if entry is not None:
        log.info(
            "news_collector_cache_event",
            extra={
                "url": rss_url,
                "status_code": entry.status_code,
                "cache_hit": True,
                "body_bytes": entry.bytes_size,
            },
        )
        return feedparser.parse(entry.body)

    # Miss — fetch raw bytes via requests so we can persist them. Any
    # network exception falls back to feedparser's own fetch (which is
    # what the cache-off path would do anyway) — never propagate cache
    # plumbing errors to the caller.
    try:
        response = requests.get(
            rss_url, headers=REQUEST_HEADERS, timeout=10,
        )
        body_bytes = response.content or b""
    except Exception as exc:  # noqa: BLE001 — cache fetch must not break the pipeline
        log.warning(
            "news_collector_cache_fetch_failed",
            extra={"url": rss_url, "error": str(exc)},
        )
        return feedparser.parse(rss_url)

    try:
        if (
            response.status_code == 200
            and len(body_bytes) <= _NEWS_COLLECTOR_CACHE_MAX_BODY_BYTES
        ):
            cache.put(
                url=rss_url,
                body=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                ttl_seconds=_get_rss_cache_ttl_seconds(),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "news_collector_cache_put_failed",
            extra={"url": rss_url, "error": str(exc)},
        )

    log.info(
        "news_collector_cache_event",
        extra={
            "url": rss_url,
            "status_code": response.status_code,
            "cache_hit": False,
            "body_bytes": len(body_bytes),
        },
    )
    return feedparser.parse(body_bytes)


def search_google_news_rss_with_meta(query: str, max_results: int = 3):
    cached = _cached_news_response(query, max_results)
    if cached is not None:
        return cached

    encoded_query = quote(query)
    rss_url = (
        f"https://news.google.com/rss/search?"
        f"q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    )

    feed = _parse_google_news_rss(rss_url)
    raw_results = _stable_sort_news([_entry_to_news(entry) for entry in feed.entries])
    raw_rss_count = len(raw_results)
    recent_results = [
        item for item in raw_results if is_recent(item.get("published", ""), days=RECENT_DAYS)
    ]
    filtered_recent_count = len(recent_results)
    fallback_source_attempted = []
    fallback_error = None
    no_results_reason = None

    log.info(f"[NewsCollector] Google RSS raw count: {raw_rss_count}")
    log.info(f"[NewsCollector] Recent window results: {filtered_recent_count}")

    if recent_results:
        selected = _stable_sort_news(recent_results)[:max_results]
        mode = "recent_window"
        collection_source = "google_rss"
    else:
        relaxed_results = [
            item for item in raw_results if is_recent(item.get("published", ""), days=7)
        ]
        if relaxed_results:
            log.info("[NewsCollector] Falling back to relaxed recent window results")
            selected = _stable_sort_news(relaxed_results)[:max_results]
            mode = "relaxed_recent_window"
            collection_source = "google_rss"
        else:
            log.info("[NewsCollector] Falling back to unfiltered RSS results")
            selected = _stable_sort_news(raw_results)[:max_results]
            mode = "unfiltered_fallback"
            collection_source = "google_rss" if selected else "none"

    if not selected and raw_rss_count == 0:
        log.error("[NewsCollector] Google RSS failed, trying Naver fallback")
        fallback_source_attempted.append("naver_fallback")
        selected, fallback_error = search_naver_news_fallback(query, max_results=max_results)
        log.info(f"[NewsCollector] Naver fallback count: {len(selected)}")
        if selected:
            mode = "naver_fallback"
            collection_source = "naver_fallback"

    if not selected:
        log.info("[NewsCollector] Trying Daum fallback")
        fallback_source_attempted.append("daum_fallback")
        selected, daum_error = search_daum_news_fallback(query, max_results=max_results)
        log.info(f"[NewsCollector] Daum fallback count: {len(selected)}")
        if daum_error:
            fallback_error = "; ".join([error for error in [fallback_error, daum_error] if error])
        if selected:
            mode = "daum_fallback"
            collection_source = "daum_fallback"

    if not selected and raw_results:
        selected = _force_select_best(raw_results, "google_rss", query=query)
        mode = "forced_google_rss"
        collection_source = "google_rss"

    if not selected:
        selected = [_emergency_search_item(query)]
        mode = "forced_search_fallback"
        collection_source = "forced_search_fallback"
        no_results_reason = "News source parsing failed; using search result page as emergency fallback."

    selected = _stable_sort_news(selected)[:max_results]
    log.info(f"[NewsCollector] Selected news count: {len(selected)}")
    log.info(f"[NewsCollector] Collection source: {collection_source}")
    selected = sanitize_data(selected)
    debug = {
        "news_collection_mode": mode,
        "raw_rss_count": raw_rss_count,
        "google_raw_rss_count": raw_rss_count,
        "filtered_recent_count": filtered_recent_count,
        "selected_news_count": len(selected),
        "collection_source": collection_source,
        "fallback_source_attempted": fallback_source_attempted,
        "fallback_error": fallback_error,
        "no_results_reason": no_results_reason,
        "news_cache_hit": False,
        "news_cache_key": _cache_key(query, max_results),
        "news_cache_ttl_seconds": NEWS_CACHE_TTL_SECONDS,
    }
    _store_news_response(query, max_results, selected, debug)

    return {
        "results": selected,
        "debug": debug,
    }


def search_google_news_rss(query: str, max_results: int = 3):
    return search_google_news_rss_with_meta(query, max_results=max_results)["results"]


def resolve_google_news_url(google_news_url: str) -> str:
    parsed = urlparse(google_news_url or "")
    if parsed.netloc and "news.google.com" not in parsed.netloc:
        # M16-speed-1a Part H: short-circuit MUST stay before the cache
        # lookup. Pinned by tests/test_m11_7a_2_exception_logging.py
        # ::test_non_google_url_short_circuit_no_error which asserts
        # mocked_decoder.assert_not_called() for non-Google URLs.
        return google_news_url

    # M16-speed-1a Part H: cache lookup before the decoder call. Saves
    # ~0.7-2.5s per repeat URL. Cache stores only successful decodes
    # (see store-on-success guard below) so a transient decoder failure
    # does not pin the fallback for 24h.
    cached = _cached_decoder_response(google_news_url)
    if cached is not None:
        return cached

    try:
        result = gnewsdecoder(google_news_url)

        if isinstance(result, dict) and result.get("status"):
            decoded = result.get("decoded_url", google_news_url)
            # Cache only when the decoder actually produced a different
            # URL — caching `decoded == input` would store a no-op and
            # the next call would re-attempt anyway, so it's wasted
            # writes.
            if decoded and decoded != google_news_url:
                _store_decoder_response(google_news_url, decoded)
            return decoded

        return google_news_url

    except Exception as error:
        # M11.7a-2 Site 3a: structured-upgrade of the existing log.error.
        # The Korean f-string message is preserved verbatim because
        # PRESERVED_REAL_ERRORS in tests/test_log_level_reclassification.py
        # pins the substring '원문 URL 변환 실패'. Adds extra={} fields for
        # alertable observability; +0 log call (same call, structured).
        #
        # M11.7c: intentionally broad — narrowing reviewed and rejected.
        # The googlenewsdecoder library (decoderv2.py) raises bare
        # `Exception("Failed to fetch data from Google.")`,
        # `Exception("Header not found...")`, and
        # `Exception("Footer not found...")` directly — not subclasses.
        # Narrowing to ANY tuple of specific types would silently fail
        # to catch these library-raised exceptions and break the
        # "decoder failed → return original URL" fallback. Broad catch
        # is also forward-compatible with future library bumps that
        # might change exception classes — see docs/EXCEPTION_HANDLING_AUDIT.md
        # Site 3a.
        log.error(
            f'원문 URL 변환 실패: {error}',
            extra={
                "url": (google_news_url or "")[:500],
                "exception_type": type(error).__name__,
                "exception_message": str(error)[:500],
            },
        )
        return google_news_url
