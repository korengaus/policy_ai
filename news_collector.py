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
    "연합뉴스",
    "뉴스1",
    "이데일리",
    "한국경제",
    "매일경제",
    "조선일보",
    "중앙일보",
    "한겨레",
    "경향신문",
    "머니투데이",
    "파이낸셜뉴스",
    "서울경제",
    "아시아경제",
    "헤럴드경제",
    "디지털타임스",
    "전자신문",
    "뉴시스",
    "KBS 뉴스",
    "MBC 뉴스",
    "SBS 뉴스",
    "YTN",
}


def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    return soup.get_text(" ", strip=True)


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", clean_html(text or "")).strip()


def _query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.split(r"[\s,./|·ㆍ]+", query or "")
        if len(term.strip()) >= 2
    ]


def _is_media_only_title(title: str) -> bool:
    normalized = _normalize_spaces(title).strip(" -–—|:")
    return normalized in MEDIA_ONLY_TITLES


def is_good_news_title(title: str, query: str = "") -> bool:
    normalized = _normalize_spaces(title)
    if not normalized:
        return False
    if len(normalized) < 8:
        return False
    if _is_media_only_title(normalized):
        return False
    if not re.search(r"[가-힣A-Za-z0-9]", normalized):
        return False

    terms = _query_terms(query)
    if terms:
        matched = [term for term in terms if term in normalized]
        has_policy_signal = re.search(
            r"전세|대출|금리|부동산|주택|청년|중소기업|금융|정책|규제|지원|은행|감면",
            normalized,
        )
        if not matched and not has_policy_signal:
            return False

    return True


def _candidate_title(link) -> str:
    for attr in ("title", "aria-label"):
        value = _normalize_spaces(link.get(attr, ""))
        if value:
            return value
    return _normalize_spaces(link.get_text(" ", strip=True))


def _absolute_url(href: str, base_url: str) -> str:
    if not href:
        return ""
    return urljoin(base_url, href)


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


def _accept_fallback_candidate(
    items: list[dict],
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
    print(f"[NewsCollector] {source.split('_')[0].capitalize()} candidate title: {title}")

    if not is_good_news_title(title, query):
        print(f"[NewsCollector] Skipped low quality title: {title}")
        return False

    original_url = _absolute_url(href, base_url)
    parsed = urlparse(original_url)
    if not parsed.scheme or not parsed.netloc:
        print(f"[NewsCollector] Skipped low quality title: {title}")
        return False

    items.append(_fallback_item(title, original_url, summary, source))
    unique = _dedupe_news_items(items)
    items[:] = unique
    print(f"[NewsCollector] Accepted fallback title: {title}")
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


def _fallback_item(title: str, link: str, summary: str, source: str) -> dict:
    return {
        "title": clean_html(title),
        "summary": clean_html(summary),
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


def search_naver_news_fallback(query: str, max_results: int = 3) -> tuple[list[dict], str | None]:
    try:
        url = f"https://search.naver.com/search.naver?where=news&query={quote(query)}"
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        items = []

        selectors = [
            "a.news_tit",
            "a[href*='news.naver.com']",
            "a[href*='n.news.naver.com']",
            "a[href*='media.naver.com']",
            "a[class*='title']",
            "a[class*='news']",
        ]
        seen_links = set()

        for selector in selectors:
            for link in soup.select(selector):
                href = link.get("href", "")
                if not href or href in seen_links:
                    continue
                seen_links.add(href)

                title = _candidate_title(link)
                container = link.find_parent(["li", "div"])
                summary = _summary_from_container(container)

                if _accept_fallback_candidate(
                    items,
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
        selectors = [
            "a.f_link_b",
            "a.tit_main",
            "a.link_tit",
            "a[class*='tit']",
            "a[class*='news']",
            "a[href*='news.v.daum.net']",
            "a[href*='v.daum.net']",
        ]
        items = []
        seen_links = set()

        for selector in selectors:
            for link in soup.select(selector):
                href = link.get("href", "")
                if not href or href in seen_links:
                    continue
                seen_links.add(href)

                title = _candidate_title(link)
                container = link.find_parent(["li", "div"])
                summary = _summary_from_container(container)

                if _accept_fallback_candidate(
                    items,
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

    if not selected:
        mode = "none"
        collection_source = "none"
        no_results_reason = "Google RSS and fallback news sources returned no usable items."

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
