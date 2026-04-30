import feedparser
import requests
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse
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


def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    return soup.get_text(" ", strip=True)


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

        for link in soup.select("a.news_tit"):
            href = link.get("href", "")
            title = link.get("title") or link.get_text(" ", strip=True)
            if not href or not title:
                continue

            container = link.find_parent(["li", "div"])
            summary = ""
            if container:
                summary_el = container.select_one(".news_dsc, .dsc_wrap, .api_txt_lines")
                if summary_el:
                    summary = summary_el.get_text(" ", strip=True)

            items.append(_fallback_item(title, href, summary, "naver_fallback"))
            if len(_dedupe_news_items(items)) >= max_results:
                break

        return _dedupe_news_items(items)[:max_results], None
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
            "a[href*='news.v.daum.net']",
            "a[href*='v.daum.net']",
        ]
        items = []

        for selector in selectors:
            for link in soup.select(selector):
                href = link.get("href", "")
                title = link.get_text(" ", strip=True) or link.get("title", "")
                if not href or not title or len(title) < 5:
                    continue

                container = link.find_parent(["li", "div"])
                summary = ""
                if container:
                    summary_el = container.select_one(".desc, .cont, .txt_info, .desc_news")
                    if summary_el:
                        summary = summary_el.get_text(" ", strip=True)

                items.append(_fallback_item(title, href, summary, "daum_fallback"))
                if len(_dedupe_news_items(items)) >= max_results:
                    return _dedupe_news_items(items)[:max_results], None

        return _dedupe_news_items(items)[:max_results], None
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
