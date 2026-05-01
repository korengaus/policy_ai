import feedparser
import re
import requests
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin, urlparse
from googlenewsdecoder import gnewsdecoder

from config import RECENT_DAYS


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

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
    return soup.get_text(" ", strip=True)


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", clean_html(text or "")).strip()


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
    return {
        "title": _normalize_spaces(title),
        "summary": _normalize_spaces(summary),
        "google_link": link,
        "original_url": link,
        "link": link,
        "published": _utc_now_rfc2822(),
        "published_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }


def _dedupe_news_items(items: list[dict]) -> list[dict]:
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


def _candidate_score(item: dict) -> int:
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
    if any(keyword in title for keyword in ["대출", "금리", "부동산", "정책", "규제", "지원", "전세", "주택"]):
        score += 25
    if any(pattern in url for pattern in ["news", "article", "v.daum.net", "naver.com"]):
        score += 10
    return score


def _force_select_best(items: list[dict], source: str) -> list[dict]:
    unique = _dedupe_news_items([item for item in items if item])
    if not unique:
        return []

    best = max(unique, key=_candidate_score)
    print("[NewsCollector] Forcing fallback selection: 1 item")
    forced = dict(best)
    forced["source"] = forced.get("source") or source
    forced["forced_fallback"] = True
    return [forced]


def _emergency_search_item(query: str) -> dict:
    search_url = f"https://search.naver.com/search.naver?where=news&query={quote(query)}"
    print("[NewsCollector] Forcing fallback selection: 1 item")
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
    print(f"[NewsCollector] Raw candidate: {title}")

    valid_link, link_reason = _is_valid_news_href(href, base_url)
    if not valid_link:
        print(f"[NewsCollector] Rejected reason: {link_reason}")
        return False

    raw_item = _raw_fallback_candidate(title, href, summary, source, base_url)
    if raw_item:
        raw_candidates.append(raw_item)

    reject_reason = _reject_title_reason(title, query=query)
    if reject_reason:
        print(f"[NewsCollector] Rejected reason: {reject_reason}")
        return False

    if not raw_item:
        print("[NewsCollector] Rejected reason: invalid URL")
        return False

    items.append(raw_item)
    items[:] = _dedupe_news_items(items)
    print(f"[NewsCollector] Accepted title: {title}")
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
    return {
        "title": clean_html(entry.get("title", "")),
        "summary": clean_html(entry.get("summary", "")),
        "google_link": entry.get("link", ""),
        "published": entry.get("published", ""),
        "source": "google_rss",
    }


def search_naver_news_fallback(query: str, max_results: int = 3) -> tuple[list[dict], str | None]:
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={quote(query)}"
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
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
                print(f"[NewsCollector] Fallback selected: {len(items)}")
                return items[:max_results], None

        if not items:
            items = _force_select_best(raw_candidates, "naver_fallback")

        print(f"[NewsCollector] Fallback selected: {len(items)}")
        return items[:max_results], None
    except Exception as error:
        return [], str(error)


def search_daum_news_fallback(query: str, max_results: int = 3) -> tuple[list[dict], str | None]:
    try:
        url = f"https://search.daum.net/search?w=news&q={quote(query)}"
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
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
                print(f"[NewsCollector] Fallback selected: {len(items)}")
                return items[:max_results], None

        if not items:
            items = _force_select_best(raw_candidates, "daum_fallback")

        print(f"[NewsCollector] Fallback selected: {len(items)}")
        return items[:max_results], None
    except Exception as error:
        return [], str(error)


def search_google_news_rss_with_meta(query: str, max_results: int = 3):
    encoded_query = quote(query)
    rss_url = (
        f"https://news.google.com/rss/search?"
        f"q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    )

    feed = feedparser.parse(rss_url)
    raw_results = [_entry_to_news(entry) for entry in feed.entries]
    raw_rss_count = len(raw_results)
    recent_results = [
        item for item in raw_results if is_recent(item.get("published", ""), days=RECENT_DAYS)
    ]
    filtered_recent_count = len(recent_results)
    fallback_source_attempted = []
    fallback_error = None
    no_results_reason = None

    print(f"[NewsCollector] Google RSS raw count: {raw_rss_count}")
    print(f"[NewsCollector] Recent window results: {filtered_recent_count}")

    if recent_results:
        selected = recent_results[:max_results]
        mode = "recent_window"
        collection_source = "google_rss"
    else:
        relaxed_results = [
            item for item in raw_results if is_recent(item.get("published", ""), days=7)
        ]
        if relaxed_results:
            print("[NewsCollector] Falling back to relaxed recent window results")
            selected = relaxed_results[:max_results]
            mode = "relaxed_recent_window"
            collection_source = "google_rss"
        else:
            print("[NewsCollector] Falling back to unfiltered RSS results")
            selected = raw_results[:max_results]
            mode = "unfiltered_fallback"
            collection_source = "google_rss" if selected else "none"

    if not selected and raw_rss_count == 0:
        print("[NewsCollector] Google RSS failed, trying Naver fallback")
        fallback_source_attempted.append("naver_fallback")
        selected, fallback_error = search_naver_news_fallback(query, max_results=max_results)
        print(f"[NewsCollector] Naver fallback count: {len(selected)}")
        if selected:
            mode = "naver_fallback"
            collection_source = "naver_fallback"

    if not selected:
        print("[NewsCollector] Trying Daum fallback")
        fallback_source_attempted.append("daum_fallback")
        selected, daum_error = search_daum_news_fallback(query, max_results=max_results)
        print(f"[NewsCollector] Daum fallback count: {len(selected)}")
        if daum_error:
            fallback_error = "; ".join([error for error in [fallback_error, daum_error] if error])
        if selected:
            mode = "daum_fallback"
            collection_source = "daum_fallback"

    if not selected and raw_results:
        selected = _force_select_best(raw_results, "google_rss")
        mode = "forced_google_rss"
        collection_source = "google_rss"

    if not selected:
        selected = [_emergency_search_item(query)]
        mode = "forced_search_fallback"
        collection_source = "forced_search_fallback"
        no_results_reason = "News source parsing failed; using search result page as emergency fallback."

    print(f"[NewsCollector] Selected news count: {len(selected)}")
    print(f"[NewsCollector] Collection source: {collection_source}")

    return {
        "results": selected,
        "debug": {
            "news_collection_mode": mode,
            "raw_rss_count": raw_rss_count,
            "google_raw_rss_count": raw_rss_count,
            "filtered_recent_count": filtered_recent_count,
            "selected_news_count": len(selected),
            "collection_source": collection_source,
            "fallback_source_attempted": fallback_source_attempted,
            "fallback_error": fallback_error,
            "no_results_reason": no_results_reason,
        },
    }


def search_google_news_rss(query: str, max_results: int = 3):
    return search_google_news_rss_with_meta(query, max_results=max_results)["results"]


def resolve_google_news_url(google_news_url: str) -> str:
    parsed = urlparse(google_news_url or "")
    if parsed.netloc and "news.google.com" not in parsed.netloc:
        return google_news_url

    try:
        result = gnewsdecoder(google_news_url)

        if isinstance(result, dict) and result.get("status"):
            return result.get("decoded_url", google_news_url)

        return google_news_url

    except Exception as error:
        print("원문 URL 변환 실패:", error)
        return google_news_url
