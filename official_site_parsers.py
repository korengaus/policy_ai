from urllib.parse import urljoin, urlparse
import re

from bs4 import BeautifulSoup


BAD_TEXT_KEYWORDS = [
    "\ub85c\uadf8\uc778",
    "\ud68c\uc6d0\uac00\uc785",
    "\uc0ac\uc774\ud2b8\ub9f5",
    "\uac1c\uc778\uc815\ubcf4",
    "\uc800\uc791\uad8c",
    "\uace0\uac1d\uc13c\ud130",
    "\ubb34\uc778\ubbfc\uc6d0\ubc1c\uae09",
    "\ubbfc\uc6d0\uc548\ub0b4",
    "\uc804\uccb4\uba54\ub274",
    "\uac80\uc0c9",
    "\uc815\ubd8024 \uc774\uc6a9\uc548\ub0b4",
    "\uc774\uc6a9\uc548\ub0b4",
]

BAD_URL_KEYWORDS = [
    "javascript:",
    "mailto:",
    "login",
    "sitemap",
    "search",
    "menu",
    "minwon",
    "customer",
    "privacy",
    "copyright",
    "attach",
    "file",
    "download",
    "/index",
    "/main",
    "main.do",
    "portal/main",
    "/home",
    "home.do",
    "dataviewgov",
    "myresults",
    "aa040",
    "aa090nomanminwon",
    "benefitserviceagree",
]

RENDERED_LINK_SELECTORS = [
    "div.search_list a",
    "ul.search_list a",
    ".result a",
    ".board_list a",
    ".news_list a",
    ".bbs_list a",
    "article a",
    "li a",
    "a[href]",
]

RENDERED_BAD_URL_KEYWORDS = [
    "javascript:void",
    "javascript:;",
    "login",
    "sitemap",
    "main.do",
    "/main",
    "portal/main",
    "/home",
    "home.do",
]

RENDERED_BAD_TEXT_KEYWORDS = [
    "\ub85c\uadf8\uc778",
    "\uc0ac\uc774\ud2b8\ub9f5",
    "\ud648",
    "\uba54\uc778",
]

BAD_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".pdf",
    ".xls",
    ".xlsx",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".zip",
)

GENERIC_GOOD_KEYWORDS = [
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

SITE_RULES = {
    "gov24": {
        "patterns": ["subsidy", "policy", "service", "svc", "portal/service", "portal/rcvfvrsvc"],
        "keywords": [
            "\ubcf4\uc870\uae0824",
            "\uc815\ucc45\uc815\ubcf4",
            "\uc11c\ube44\uc2a4 \uc0c1\uc138",
            "\uc9c0\uc6d0\uc0ac\uc5c5",
            "\uc2e0\uccad",
            "\uc8fc\uac70",
            "\uc804\uc138",
            "\uccad\ub144",
            "\uc2e0\ud63c\ubd80\ubd80",
            "\ub300\ucd9c\uc774\uc790",
        ],
        "bad_keywords": [
            "\ubb34\uc778\ubbfc\uc6d0\ubc1c\uae09\uc548\ub0b4",
            "\ubbfc\uc6d0\uc548\ub0b4",
            "\uc815\ubd8024 \uc774\uc6a9\uc548\ub0b4",
        ],
    },
    "molit": {
        "patterns": ["/USR/NEWS/", "/USR/BORD0201/", "/USR/policyData/", "/USR/I0204/"],
        "keywords": [
            "\ubcf4\ub3c4\uc790\ub8cc",
            "\uc124\uba85\uc790\ub8cc",
            "\uc815\ucc45\uc790\ub8cc",
            "\uc8fc\ud0dd",
            "\uc804\uc138",
            "\uccad\ub144",
            "\ub300\ucd9c",
            "\uc8fc\uac70",
            "\uc9c0\uc6d0",
        ],
    },
    "fsc": {
        "patterns": ["/no010101", "/po010101", "/policy", "/press", "/bbs"],
        "keywords": [
            "\ubcf4\ub3c4\uc790\ub8cc",
            "\uae08\uc735\uc815\ucc45",
            "\uc815\ucc45\ub9c8\ub2f9",
            "\uac00\uacc4\ub300\ucd9c",
            "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
            "\uc804\uc138\ub300\ucd9c",
            "\uae08\uc735\uc704",
            "\uc124\uba85\uc790\ub8cc",
        ],
    },
    "fss": {
        "patterns": ["/fss/bbs/", "/fss/job/", "/fss/main/contents.do"],
        "keywords": [
            "\ubcf4\ub3c4\uc790\ub8cc",
            "\uacf5\uc9c0",
            "\uc740\ud589",
            "\ub300\ucd9c",
            "\uac00\uacc4\ub300\ucd9c",
            "\uc804\uc138\ub300\ucd9c",
            "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
            "\uae08\uc735\uac10\ub3c5\uc6d0",
        ],
    },
    "ibk": {
        "patterns": ["/common/navigation", "/news", "/board", "/product", "/prd"],
        "keywords": [
            "\ubcf4\ub3c4\uc790\ub8cc",
            "\uacf5\uc9c0",
            "\uc0c1\ud488",
            "\uae08\ub9ac",
            "\uc804\uc138\ub300\ucd9c",
            "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
            "i-ONE",
            "\uc911\uc18c\uae30\uc5c5",
            "\uadfc\ub85c\uc790",
        ],
        "soft_bad_keywords": ["\uace0\uac1d\uc13c\ud130", "\uc0c1\ud488\ubaa9\ub85d", "\ub85c\uadf8\uc778"],
    },
    "bok": {
        "patterns": ["/portal/bbs/", "/portal/singl/", "/portal/main/contents.do"],
        "keywords": ["\ubcf4\ub3c4\uc790\ub8cc", "\ud1b5\ud654\uc815\ucc45", "\uae08\uc735\uc548\uc815", "\uae08\ub9ac", "\uc790\ub8cc"],
    },
    "assembly": {
        "patterns": ["/portal/bbs/", "/bill/", "/assm/"],
        "keywords": ["\ubcf4\ub3c4\uc790\ub8cc", "\uc758\uc548", "\ubc95\uc548", "\uc785\ubc95", "\uc815\ucc45", "\uc790\ub8cc"],
    },
    "generic": {
        "patterns": ["view", "detail", "board", "bbs", "notice", "news", "dtl"],
        "keywords": GENERIC_GOOD_KEYWORDS,
    },
}


def get_site_key(url: str, source_name: str = "") -> str:
    text = f"{url} {source_name}".lower()

    if "fsc.go.kr" in text or "financial services commission" in text:
        return "fsc"
    if "fss.or.kr" in text or "financial supervisory service" in text:
        return "fss"
    if "molit.go.kr" in text or "ministry of land" in text:
        return "molit"
    if "gov.kr" in text or "government24" in text:
        return "gov24"
    if "ibk.co.kr" in text or "industrial bank" in text or "\uae30\uc5c5\uc740\ud589" in text:
        return "ibk"
    if "bok.or.kr" in text or "bank of korea" in text:
        return "bok"
    if "assembly.go.kr" in text or "national assembly" in text:
        return "assembly"

    return "generic"


def is_bad_official_link(url: str, text: str = "") -> bool:
    normalized_url = (url or "").lower()
    normalized_text = (text or "").lower()
    path = urlparse(url or "").path.lower()

    if not normalized_url or normalized_url.startswith("#"):
        return True
    if path.endswith(BAD_EXTENSIONS):
        return True
    if any(keyword in normalized_url for keyword in BAD_URL_KEYWORDS):
        return True
    if any(keyword.lower() in normalized_text for keyword in BAD_TEXT_KEYWORDS):
        return True

    return False


def _same_domain(url: str, base_url: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower()


def _query_matches(text: str, query: str) -> int:
    score = 0

    for token in (query or "").split():
        token = token.strip()
        if len(token) < 2:
            continue
        if token.lower() in text.lower():
            score += 2

    return min(score, 12)


def score_official_link(link: dict, site_key: str, query: str = "") -> int:
    url = link.get("url", "")
    text = link.get("text", "")
    combined = f"{url} {text}"
    rules = SITE_RULES.get(site_key) or SITE_RULES["generic"]
    score = 0
    reasons = []

    if is_bad_official_link(url, text):
        link["reason"] = "bad link excluded"
        return 0

    if link.get("same_domain"):
        score += 20
        reasons.append("same domain")

    normalized_url = url.lower()
    normalized_text = text.strip().lower()
    if site_key == "fsc" and re.search(r"/no01010[12]/\d{4,}", normalized_url):
        score += 35
        reasons.append("fsc detail press url")
    if any(part in normalized_url for part in ["detail", "view", "dtl"]):
        score += 20
        reasons.append("detail/view/dtl url")
    if re.search(r"(?<!\d)\d{4,}(?!\d)", normalized_url):
        score += 10
        reasons.append("numeric id")
    if any(part in normalized_url for part in ["list", "search", "paging", "pagination"]):
        score -= 35
        reasons.append("list/search page penalty")
    if normalized_text in {"\ubcf4\ub3c4\uc790\ub8cc", "\ub354\ubcf4\uae30", "\ubaa9\ub85d", "list", "more"}:
        score -= 35
        reasons.append("generic list text penalty")

    for pattern in rules.get("patterns", []):
        if pattern.lower() in url.lower():
            score += 18
            reasons.append(f"url pattern: {pattern}")

    for keyword in rules.get("keywords", []):
        if keyword.lower() in combined.lower():
            score += 10
            reasons.append(f"keyword: {keyword}")

    for keyword in rules.get("soft_bad_keywords", []):
        if keyword.lower() in combined.lower():
            score -= 15
            reasons.append(f"soft bad keyword: {keyword}")

    query_score = _query_matches(combined, query)
    if query_score:
        score += query_score
        reasons.append("query token match")

    if len(text.strip()) >= 8:
        score += 4
        reasons.append("descriptive text")

    link["reason"] = "; ".join(reasons) if reasons else "generic candidate"
    return max(0, score)


def _is_bad_rendered_link(url: str, text: str = "") -> bool:
    normalized_url = (url or "").lower()
    normalized_text = (text or "").lower()
    stripped_url = normalized_url.strip()

    if not stripped_url or stripped_url == "#" or stripped_url.endswith("#"):
        return True
    if is_bad_official_link(url, text):
        return True
    if any(keyword in normalized_url for keyword in RENDERED_BAD_URL_KEYWORDS):
        return True
    if any(keyword.lower() in normalized_text for keyword in RENDERED_BAD_TEXT_KEYWORDS):
        return True

    return False


def _rendered_link_result(
    search_html: str,
    base_url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
    site_key: str | None = None,
) -> dict:
    site_key = site_key or get_site_key(base_url, source_name)
    soup = BeautifulSoup(search_html or "", "html.parser")
    seen_urls = set()
    candidates = []
    rejected_links_count = 0

    for selector_index, selector in enumerate(RENDERED_LINK_SELECTORS):
        for anchor in soup.select(selector):
            href = (anchor.get("href") or "").strip()
            link_text = anchor.get_text(" ", strip=True)

            if not href:
                rejected_links_count += 1
                continue

            absolute_url = urljoin(base_url, href)

            if absolute_url in seen_urls:
                continue

            seen_urls.add(absolute_url)

            if _is_bad_rendered_link(absolute_url, link_text):
                rejected_links_count += 1
                continue

            link = {
                "url": absolute_url,
                "text": link_text[:200],
                "same_domain": _same_domain(absolute_url, base_url),
                "site_key": site_key,
                "selector": selector,
            }
            link_score = score_official_link(link, site_key, query=query)

            if selector != "a[href]":
                link_score += max(0, 12 - selector_index)
                link["reason"] = f"{link.get('reason')}; rendered selector: {selector}"

            if link_score <= 0:
                rejected_links_count += 1
                continue

            link["score"] = link_score
            link["link_score"] = link_score
            link["link_reason"] = link.get("reason")
            candidates.append(link)

    candidates.sort(
        key=lambda item: (item.get("score", 0), item.get("same_domain", False), len(item.get("text", ""))),
        reverse=True,
    )

    return {
        "links": candidates[:max_links],
        "rejected_links_count": rejected_links_count,
        "parser_used": f"{site_key}_rendered",
    }


def extract_fsc_rendered_links(
    search_html: str,
    base_url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
) -> dict:
    return _rendered_link_result(search_html, base_url, source_name, query, max_links, site_key="fsc")


def extract_ibk_rendered_links(
    search_html: str,
    base_url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
) -> dict:
    return _rendered_link_result(search_html, base_url, source_name, query, max_links, site_key="ibk")


def extract_fss_rendered_links(
    search_html: str,
    base_url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
) -> dict:
    return _rendered_link_result(search_html, base_url, source_name, query, max_links, site_key="fss")


def extract_gov24_rendered_links(
    search_html: str,
    base_url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 10,
) -> dict:
    return _rendered_link_result(search_html, base_url, source_name, query, max_links, site_key="gov24")


def extract_links_for_site(
    search_html: str,
    base_url: str,
    source_name: str = "",
    query: str = "",
    max_links: int = 5,
) -> list:
    site_key = get_site_key(base_url, source_name)
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

        seen_urls.add(absolute_url)

        if is_bad_official_link(absolute_url, link_text):
            continue

        link = {
            "url": absolute_url,
            "text": link_text[:200],
            "same_domain": _same_domain(absolute_url, base_url),
            "site_key": site_key,
        }
        link["score"] = score_official_link(link, site_key, query=query)

        if link["score"] <= 0:
            continue

        candidates.append(link)

    candidates.sort(
        key=lambda item: (item.get("score", 0), item.get("same_domain", False), len(item.get("text", ""))),
        reverse=True,
    )

    return candidates[:max_links]
