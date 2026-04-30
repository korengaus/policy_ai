import feedparser
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from googlenewsdecoder import gnewsdecoder

from config import RECENT_DAYS
def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
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


def _entry_to_news(entry) -> dict:
    return {
        "title": clean_html(entry.get("title", "")),
        "summary": clean_html(entry.get("summary", "")),
        "google_link": entry.get("link", ""),
        "published": entry.get("published", ""),
    }


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

    print(f"[NewsCollector] Raw RSS results: {raw_rss_count}")
    print(f"[NewsCollector] Recent window results: {filtered_recent_count}")

    if recent_results:
        selected = recent_results[:max_results]
        mode = "recent_window"
    else:
        relaxed_results = [
            item for item in raw_results if is_recent(item.get("published", ""), days=7)
        ]
        if relaxed_results:
            print("[NewsCollector] Falling back to relaxed recent window results")
            selected = relaxed_results[:max_results]
            mode = "relaxed_recent_window"
        else:
            print("[NewsCollector] Falling back to unfiltered RSS results")
            selected = raw_results[:max_results]
            mode = "unfiltered_fallback"

    print(f"[NewsCollector] Fallback selected: {len(selected)}")

    return {
        "results": selected,
        "debug": {
            "news_collection_mode": mode,
            "raw_rss_count": raw_rss_count,
            "filtered_recent_count": filtered_recent_count,
            "selected_news_count": len(selected),
        },
    }


def search_google_news_rss(query: str, max_results: int = 3):
    return search_google_news_rss_with_meta(query, max_results=max_results)["results"]


def resolve_google_news_url(google_news_url: str) -> str:
    try:
        result = gnewsdecoder(google_news_url)

        if isinstance(result, dict) and result.get("status"):
            return result.get("decoded_url", google_news_url)

        return google_news_url

    except Exception as e:
        print("?먮Ц URL 蹂???ㅽ뙣:", e)
        return google_news_url
