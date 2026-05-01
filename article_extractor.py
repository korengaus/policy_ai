import re

import requests
import trafilatura
from bs4 import BeautifulSoup


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

BAD_KEYWORDS = [
    "무단전재",
    "사업자번호",
    "등록번호",
    "청소년보호책임자",
    "무단복제",
    "재배포 금지",
    "Copyright",
    "copyright",
    "로그인",
    "회원가입",
    "기사제보",
    "고객센터",
    "개인정보",
    "이용약관",
    "많이 본 뉴스",
    "주요뉴스",
    "오늘의 포토",
    "추천기사",
    "관련기사",
    "랭킹뉴스",
]


def clean_extracted_text(text: str) -> str:
    if not text:
        return ""

    cleaned_lines = []
    seen = set()

    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line or "").strip()

        if len(line) < 20:
            continue

        if any(keyword in line for keyword in BAD_KEYWORDS):
            continue

        if line in seen:
            continue

        seen.add(line)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def _extract_with_trafilatura(url: str) -> str:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return ""

    extracted = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return clean_extracted_text(extracted or "")


def _best_text_block(soup: BeautifulSoup) -> str:
    selectors = [
        "article",
        "[itemprop='articleBody']",
        ".article_view",
        ".article-body",
        ".article_body",
        ".news_view",
        ".news_body",
        ".view_cont",
        ".view_content",
        ".content",
        "#articleBody",
        "#news_body",
        "#articeBody",
        "#dic_area",
    ]

    candidates = []
    for selector in selectors:
        for element in soup.select(selector):
            text = element.get_text("\n", strip=True)
            cleaned = clean_extracted_text(text)
            if cleaned:
                candidates.append(cleaned)

    if candidates:
        return max(candidates, key=len)

    body = soup.body or soup
    return clean_extracted_text(body.get_text("\n", strip=True))


def _extract_with_beautifulsoup(url: str) -> str:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=12)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup.select("script, style, header, footer, nav, aside, form, iframe, noscript"):
        tag.decompose()

    return _best_text_block(soup)


def fetch_article_body(url: str, max_chars: int = 5000) -> str:
    try:
        extracted = _extract_with_trafilatura(url)
        if len(extracted) < 300:
            fallback = _extract_with_beautifulsoup(url)
            if len(fallback) > len(extracted):
                extracted = fallback

        if extracted:
            print(f"[ArticleExtractor] Extracted length: {len(extracted)}")
            if len(extracted) >= 300:
                print("[ArticleExtractor] Using content for claim")
            else:
                print("[ArticleExtractor] Fallback to title")
            return extracted[:max_chars]

        print("[ArticleExtractor] Extracted length: 0")
        print("[ArticleExtractor] Fallback to title")
        return "본문 추출 실패: 기사 본문을 찾지 못함"

    except Exception as error:
        print("[ArticleExtractor] Extracted length: 0")
        print("[ArticleExtractor] Fallback to title")
        return f"본문 수집 중 오류 발생: {error}"
