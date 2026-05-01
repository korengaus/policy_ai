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

ENCODING_CANDIDATES = ["utf-8", "cp949", "euc-kr"]

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

MOJIBAKE_MARKERS = ["ì", "í", "ë", "ê", "Â", "Ã", "�", "媛", "쒓", "뺤", "댁", "齊"]


def _normalize_text(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text or "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _hangul_count(text: str) -> int:
    return len(re.findall(r"[가-힣]", text or ""))


def _mojibake_score(text: str) -> int:
    if not text:
        return 999
    marker_score = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    replacement_score = text.count("�") * 5
    hangul = _hangul_count(text)
    korean_expected = bool(re.search(r"[\uac00-\ud7a3]", text))
    if korean_expected and hangul == 0:
        marker_score += 50
    return marker_score + replacement_score


def _text_quality_score(text: str) -> int:
    if not text:
        return -10000
    normalized = _normalize_text(text)
    hangul = _hangul_count(normalized)
    mojibake = _mojibake_score(normalized)
    replacement = normalized.count("�")
    readable = len(re.findall(r"[가-힣A-Za-z0-9]", normalized))
    return hangul * 5 + readable - mojibake * 30 - replacement * 80


def _repair_utf8_mojibake(text: str) -> str:
    if not text or not any(marker in text for marker in ["ì", "í", "ë", "ê", "Â", "Ã"]):
        return text
    for source_encoding in ("latin1", "cp1252"):
        try:
            repaired = text.encode(source_encoding, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if _text_quality_score(repaired) > _text_quality_score(text):
            return repaired
    return text


def _is_probably_broken(text: str) -> bool:
    if not text:
        return True
    normalized = _normalize_text(text)
    if len(normalized) < 50:
        return True
    if _mojibake_score(normalized) >= 8 and _hangul_count(normalized) < 20:
        return True
    if normalized.count("�") >= 3:
        return True
    return False


def _decode_response_content(response: requests.Response) -> tuple[str, str, bool]:
    declared = response.encoding
    apparent = response.apparent_encoding

    decoded_candidates = []

    if apparent:
        response.encoding = apparent
        decoded_candidates.append((response.text, apparent, "apparent"))

    for encoding in [*ENCODING_CANDIDATES, declared, apparent]:
        if not encoding:
            continue
        try:
            decoded = response.content.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            try:
                decoded = response.content.decode(encoding, errors="replace")
            except LookupError:
                continue
        decoded_candidates.append((decoded, encoding, "candidate"))

    if not decoded_candidates:
        return response.text, response.encoding or "unknown", False

    expanded_candidates = []
    for decoded, encoding, source in decoded_candidates:
        expanded_candidates.append((decoded, encoding, source))
        repaired = _repair_utf8_mojibake(decoded)
        if repaired != decoded:
            expanded_candidates.append((repaired, f"{encoding}+utf8-repair", "repair"))

    best_text, best_encoding, source = max(
        expanded_candidates,
        key=lambda item: (_text_quality_score(item[0]), len(item[0])),
    )
    fallback_used = source != "apparent" or best_encoding != (apparent or "")
    return best_text, best_encoding, fallback_used


def clean_extracted_text(text: str) -> str:
    if not text:
        return ""

    text = _repair_utf8_mojibake(_normalize_text(text))
    cleaned_lines = []
    seen = set()

    for line in text.splitlines():
        line = _normalize_text(line)

        if len(line) < 20:
            continue

        if any(keyword in line for keyword in BAD_KEYWORDS):
            continue

        line = _repair_utf8_mojibake(line)

        if _mojibake_score(line) >= 8 and _hangul_count(line) < 5:
            continue

        if line in seen:
            continue

        seen.add(line)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def _fetch_html(url: str) -> tuple[str, str, bool]:
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=12)
    response.raise_for_status()
    html, encoding, fallback_used = _decode_response_content(response)
    response.encoding = encoding
    print(f"[ArticleExtractor] encoding used: {encoding}")
    if fallback_used:
        print("[ArticleExtractor] fallback encoding triggered")
    return html, encoding, fallback_used


def _extract_with_trafilatura_html(html: str) -> str:
    extracted = trafilatura.extract(
        html,
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


def _extract_with_beautifulsoup_html(html: str, encoding: str = "utf-8") -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, header, footer, nav, aside, form, iframe, noscript"):
        tag.decompose()

    return _best_text_block(soup)


def fetch_article_body(url: str, max_chars: int = 5000) -> str:
    try:
        html, encoding, _ = _fetch_html(url)
        extracted = _extract_with_trafilatura_html(html)

        if len(extracted) < 300 or _is_probably_broken(extracted):
            fallback = _extract_with_beautifulsoup_html(html, encoding=encoding)
            if len(fallback) > len(extracted) or not _is_probably_broken(fallback):
                extracted = fallback

        extracted = clean_extracted_text(extracted)
        quality_score = _text_quality_score(extracted)
        print(f"[ArticleExtractor] text quality score: {quality_score}")
        print(f"[ArticleExtractor] text length: {len(extracted)}")
        print(f"[ArticleExtractor] Extracted length: {len(extracted)}")

        if extracted and not _is_probably_broken(extracted) and len(extracted) >= 100:
            if len(extracted) >= 300:
                print("[ArticleExtractor] Using content for claim")
            else:
                print("[ArticleExtractor] Fallback to title")
            return extracted[:max_chars]

        print("[ArticleExtractor] Fallback to title")
        return ""

    except Exception as error:
        print("[ArticleExtractor] encoding used: unknown")
        print("[ArticleExtractor] text quality score: -10000")
        print("[ArticleExtractor] text length: 0")
        print("[ArticleExtractor] Extracted length: 0")
        print("[ArticleExtractor] Fallback to title")
        return ""
