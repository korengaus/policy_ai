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


def search_google_news_rss(query: str, max_results: int = 3):
    encoded_query = quote(query)

    rss_url = (
        f"https://news.google.com/rss/search?"
        f"q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    )

    feed = feedparser.parse(rss_url)
    results = []

    for entry in feed.entries:
        title = clean_html(entry.get("title", ""))
        summary = clean_html(entry.get("summary", ""))
        google_link = entry.get("link", "")
        published = entry.get("published", "")

        if not is_recent(published, days=RECENT_DAYS):
            continue

        results.append(
            {
                "title": title,
                "summary": summary,
                "google_link": google_link,
                "published": published,
            }
        )

        if len(results) >= max_results:
            break

    return results


def resolve_google_news_url(google_news_url: str) -> str:
    try:
        result = gnewsdecoder(google_news_url)

        if isinstance(result, dict) and result.get("status"):
            return result.get("decoded_url", google_news_url)

        return google_news_url

    except Exception as e:
        print("?먮Ц URL 蹂???ㅽ뙣:", e)
        return google_news_url
