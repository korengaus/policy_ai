from official_site_parsers import (
    extract_fsc_rendered_links,
    extract_fss_rendered_links,
    extract_gov24_rendered_links,
    extract_ibk_rendered_links,
    extract_links_for_site,
    get_site_key,
)
from urllib.parse import urljoin, urlparse
import re

from text_utils import sanitize_data, sanitize_text


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_rendered_page(url: str, timeout_ms: int = 15000) -> dict:
    result = {
        "url": url,
        "rendered": False,
        "status_code": None,
        "title": None,
        "html": "",
        "text": "",
        "raw_links": [],
        "error": None,
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        result["error"] = f"Playwright is not installed: {exc}"
        return result

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="ko-KR",
                extra_http_headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            page = context.new_page()
            response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(1000)

            result["status_code"] = response.status if response else None
            result["title"] = sanitize_text(page.title())
            result["html"] = sanitize_text(page.content())[:500000]
            result["text"] = sanitize_text(page.locator("body").inner_text(timeout=5000))[:50000]
            result["raw_links"] = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                    href: a.href || a.getAttribute('href') || '',
                    text: (a.innerText || a.textContent || '').trim()
                }))"""
            )
            result["raw_links"] = sanitize_data(result["raw_links"])
            result["rendered"] = True

            context.close()
            browser.close()

    except Exception as exc:
        result["error"] = str(exc)

    return result


def _is_bad_raw_link(url: str, text: str) -> bool:
    normalized_url = (url or "").lower().strip()
    normalized_text = (text or "").lower().strip()

    if len(normalized_url) <= 10 or len(normalized_text) <= 5:
        return True
    if normalized_url.startswith("#") or normalized_url == "#":
        return True
    if any(
        keyword in normalized_url
        for keyword in ["javascript:", "login", "sitemap", "main.do", "portal", "home"]
    ):
        return True
    if any(keyword in normalized_text for keyword in ["로그인", "사이트맵", "홈", "메인"]):
        return True

    return False


def _score_raw_link(url: str, text: str, query: str, base_url: str) -> tuple[int, str]:
    normalized_url = (url or "").lower()
    normalized_text = text or ""
    combined = f"{url} {text}".lower()
    score = 0
    reasons = []

    if urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower():
        score += 15
        reasons.append("same domain")
    if any(part in normalized_url for part in ["detail", "view", "dtl"]):
        score += 20
        reasons.append("detail/view/dtl url")
    if any(part in normalized_url for part in ["news", "press", "bbs"]):
        score += 15
        reasons.append("news/press/bbs url")
    if re.search(r"/no01010[12]/\d{4,}", normalized_url):
        score += 35
        reasons.append("fsc detail press url")
    if re.search(r"\d{4,}", normalized_url):
        score += 10
        reasons.append("numeric id")
    if len(normalized_text) > 15:
        score += 10
        reasons.append("descriptive text")
    if any(part in normalized_url for part in ["list", "search", "paging", "pagination"]):
        score -= 35
        reasons.append("list/search page penalty")
    if normalized_text.strip().lower() in {"\ubcf4\ub3c4\uc790\ub8cc", "\ub354\ubcf4\uae30", "\ubaa9\ub85d", "list", "more"}:
        score -= 35
        reasons.append("generic list text penalty")

    query_hits = 0
    for token in (query or "").split():
        token = token.strip().lower()
        if len(token) >= 2 and token in combined:
            query_hits += 1

    if query_hits:
        score += min(20, query_hits * 10)
        reasons.append("query keyword")

    return score, "; ".join(reasons) if reasons else "raw rendered link"


def _extract_raw_rendered_links(raw_links: list[dict], base_url: str, query: str, site_key: str, max_links: int) -> dict:
    seen_urls = set()
    filtered = []
    rejected = 0

    for raw_link in raw_links or []:
        href = (raw_link.get("href") or "").strip()
        text = (raw_link.get("text") or "").strip()
        absolute_url = urljoin(base_url, href)

        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        if _is_bad_raw_link(absolute_url, text):
            rejected += 1
            continue

        score, reason = _score_raw_link(absolute_url, text, query, base_url)
        if score <= 0:
            rejected += 1
            continue

        filtered.append(
            {
                "url": absolute_url,
                "text": text[:200],
                "score": score,
                "link_score": score,
                "reason": reason,
                "link_reason": reason,
                "same_domain": urlparse(absolute_url).netloc.lower() == urlparse(base_url).netloc.lower(),
                "site_key": site_key,
                "selector": "page.evaluate:a[href]",
            }
        )

    filtered.sort(
        key=lambda item: (item.get("score", 0), item.get("same_domain", False), len(item.get("text", ""))),
        reverse=True,
    )

    return {
        "links": filtered[:max_links],
        "filtered_links_count": len(filtered),
        "rejected_links_count": rejected,
    }


def extract_rendered_links(
    url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
) -> dict:
    rendered_page = fetch_rendered_page(url)
    links = []
    rejected_links_count = 0
    raw_links_count = len(rendered_page.get("raw_links") or [])
    filtered_links_count = 0
    parser_used = None

    if rendered_page.get("rendered"):
        try:
            site_key = get_site_key(url, source_name)
            rendered_extractors = {
                "fsc": extract_fsc_rendered_links,
                "fss": extract_fss_rendered_links,
                "gov24": extract_gov24_rendered_links,
                "ibk": extract_ibk_rendered_links,
            }
            extractor = rendered_extractors.get(site_key)

            if extractor:
                parsed = extractor(
                    rendered_page.get("html") or "",
                    url,
                    source_name=source_name,
                    query=query,
                    max_links=max_links,
                )
                links = parsed.get("links") or []
                rejected_links_count = parsed.get("rejected_links_count", 0)
                parser_used = parsed.get("parser_used")

            if not links:
                raw_parsed = _extract_raw_rendered_links(
                    rendered_page.get("raw_links") or [],
                    url,
                    query=query,
                    site_key=site_key,
                    max_links=max_links,
                )
                links = raw_parsed.get("links") or []
                filtered_links_count = raw_parsed.get("filtered_links_count", 0)
                rejected_links_count += raw_parsed.get("rejected_links_count", 0)
                parser_used = "rendered_raw_a_href"

            if not links:
                links = extract_links_for_site(
                    rendered_page.get("html") or "",
                    url,
                    source_name=source_name,
                    query=query,
                    max_links=max_links,
                )
                parser_used = parser_used or "rendered_generic_fallback"
        except Exception as exc:
            rendered_page["error"] = str(exc)

    return sanitize_data({
        "rendered_used": bool(rendered_page.get("rendered")),
        "rendered_status_code": rendered_page.get("status_code"),
        "rendered_title": rendered_page.get("title"),
        "rendered_text_snippet": (rendered_page.get("text") or "")[:1000],
        "rendered_html_snippet": (rendered_page.get("html") or "")[:2000],
        "rendered_links": links,
        "rendered_links_count": len(links),
        "rendered_candidate_links_count": len(links),
        "raw_links_count": raw_links_count,
        "filtered_links_count": filtered_links_count or len(links),
        "final_candidate_links_count": len(links),
        "rendered_rejected_links_count": rejected_links_count,
        "rendered_parser_used": parser_used,
        "rendered_error": rendered_page.get("error"),
    })
