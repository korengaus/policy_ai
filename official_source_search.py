import re
from urllib.parse import quote


OFFICIAL_SOURCE_CATALOG = [
    {
        "source_name": "Financial Services Commission",
        "query_name": "\uae08\uc735\uc704",
        "source_type": "financial_regulator",
        "reliability_score": 5,
        "search_url_base": "https://www.fsc.go.kr/search?srchTxt=",
        "keywords": [
            "\uae08\uc735\uc704",
            "\uae08\uc735\uc704\uc6d0\ud68c",
            "\uae08\uc735\ub2f9\uad6d",
            "\ub300\ucd9c",
            "\uc804\uc138\ub300\ucd9c",
            "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
            "DSR",
            "\uc740\ud589",
            "\ubcf4\uc99d",
        ],
    },
    {
        "source_name": "Financial Supervisory Service",
        "query_name": "\uae08\uac10\uc6d0",
        "source_type": "financial_regulator",
        "reliability_score": 5,
        "search_url_base": "https://www.fss.or.kr/fss/search/search.do?query=",
        "keywords": [
            "\uae08\uac10\uc6d0",
            "\uae08\uc735\uac10\ub3c5\uc6d0",
            "\uae08\uc735\ub2f9\uad6d",
            "\uc740\ud589",
            "\ub300\ucd9c",
            "\uc804\uc138\ub300\ucd9c",
            "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
            "\uac80\uc0ac",
            "\uac10\ub3c5",
        ],
    },
    {
        "source_name": "Ministry of Land, Infrastructure and Transport",
        "query_name": "\uad6d\ud1a0\ubd80",
        "source_type": "central_government",
        "reliability_score": 5,
        "search_url_base": "https://www.molit.go.kr/search/search.jsp?query=",
        "keywords": [
            "\uad6d\ud1a0\ubd80",
            "\uad6d\ud1a0\uad50\ud1b5\ubd80",
            "\ubd80\ub3d9\uc0b0",
            "\uc8fc\ud0dd",
            "\uc804\uc138",
            "\uccad\uc57d",
            "\uc784\ub300\ucc28",
            "\uc8fc\uac70",
        ],
    },
    {
        "source_name": "Bank of Korea",
        "query_name": "\ud55c\uad6d\uc740\ud589",
        "source_type": "central_bank",
        "reliability_score": 5,
        "search_url_base": "https://www.bok.or.kr/portal/search/search.do?query=",
        "keywords": [
            "\ud55c\uad6d\uc740\ud589",
            "\ud55c\uc740",
            "\uae08\ub9ac",
            "\uac00\uacc4\ubd80\ucc44",
            "\ud1b5\ud654\uc815\ucc45",
            "\uae08\uc735\uc548\uc815",
            "\ubd80\ub3d9\uc0b0 \uae08\uc735",
        ],
    },
    {
        "source_name": "Ministry of Economy and Finance",
        "query_name": "기획재정부",
        "source_type": "central_government",
        "reliability_score": 5,
        "search_url_base": "https://www.moef.go.kr/sisa/dictionary/search?searchText=",
        "keywords": [
            "기획재정부",
            "기재부",
            "세제",
            "부동산",
            "양도세",
            "세금",
            "경제정책",
            "재정",
            "부동산 PF",
        ],
    },
    {
        "source_name": "National Tax Service",
        "query_name": "국세청",
        "source_type": "central_government",
        "reliability_score": 5,
        "search_url_base": "https://www.nts.go.kr/search/search.do?query=",
        "keywords": [
            "국세청",
            "양도세",
            "양도소득세",
            "세무조사",
            "탈루",
            "가상자산",
            "부동산",
            "다주택자",
        ],
    },
    {
        "source_name": "Ministry of SMEs and Startups",
        "query_name": "중소벤처기업부",
        "source_type": "central_government",
        "reliability_score": 5,
        "search_url_base": "https://www.mss.go.kr/site/smba/search/search.jsp?query=",
        "keywords": [
            "중소벤처기업부",
            "중기부",
            "중소기업",
            "소상공인",
            "정책자금",
            "금융지원",
            "대출",
        ],
    },
    {
        "source_name": "Korea Housing Finance Corporation",
        "query_name": "한국주택금융공사",
        "source_type": "public_financial_institution",
        "reliability_score": 5,
        "search_url_base": "https://www.hf.go.kr/ko/sub05/sub05_01.do?query=",
        "keywords": [
            "한국주택금융공사",
            "주택금융공사",
            "HF",
            "보금자리론",
            "전세자금",
            "전세대출",
            "주택담보대출",
            "주택금융",
        ],
    },
    {
        "source_name": "Korea Housing & Urban Guarantee Corporation",
        "query_name": "주택도시보증공사",
        "source_type": "public_financial_institution",
        "reliability_score": 5,
        "search_url_base": "https://www.khug.or.kr/search/search.jsp?query=",
        "keywords": [
            "주택도시보증공사",
            "HUG",
            "전세보증",
            "전세사기",
            "보증",
            "임대보증",
            "주택도시기금",
        ],
    },
    {
        "source_name": "Korea Land & Housing Corporation",
        "query_name": "한국토지주택공사",
        "source_type": "public_service",
        "reliability_score": 4,
        "search_url_base": "https://www.lh.or.kr/search.do?query=",
        "keywords": [
            "한국토지주택공사",
            "LH",
            "공공주택",
            "임대주택",
            "전세임대",
            "주거지원",
            "부동산",
        ],
    },
    {
        "source_name": "Government24",
        "query_name": "\uc815\ubd8024",
        "source_type": "public_service",
        "reliability_score": 4,
        "search_url_base": "https://www.gov.kr/search?srhQuery=",
        "keywords": [
            "\uc815\ubd8024",
            "\uc2e0\uccad",
            "\ubaa8\uc9d1",
            "\uc9c0\uc6d0",
            "\ubcf4\uc870\uae08",
            "\ubbfc\uc6d0",
            "\uc8fc\uac70\ube44",
            "\uc774\uc790 \uc9c0\uc6d0",
        ],
    },
    {
        "source_name": "National Assembly",
        "query_name": "\uad6d\ud68c",
        "source_type": "legislature",
        "reliability_score": 5,
        "search_url_base": "https://www.assembly.go.kr/portal/search/search.do?query=",
        "keywords": [
            "\uad6d\ud68c",
            "\ubc95\uc548",
            "\uc758\uc6d0",
            "\uc785\ubc95",
            "\uac1c\uc815\uc548",
            "\uc0c1\uc784\uc704",
            "\ub17c\uc758",
            "\ubc1c\uc758",
        ],
    },
    {
        "source_name": "Local Government",
        "query_name": "\uc9c0\uc790\uccb4",
        "source_type": "local_government",
        "reliability_score": 4,
        "search_url_base": "https://www.jeju.go.kr/search/search.htm?q=",
        "keywords": [
            "\uc11c\uc6b8\uc2dc",
            "\uacbd\uae30\ub3c4",
            "\uc778\ucc9c\uc2dc",
            "\ubd80\uc0b0\uc2dc",
            "\ub300\uad6c\uc2dc",
            "\uad11\uc8fc\uc2dc",
            "\ub300\uc804\uc2dc",
            "\uc6b8\uc0b0\uc2dc",
            "\uc138\uc885\uc2dc",
            "\uc81c\uc8fc",
            "\uc81c\uc8fc\ub3c4",
            "\ud2b9\ubcc4\uc790\uce58\ub3c4",
            "\uc2dc\uccad",
            "\ub3c4\uccad",
            "\uad6c\uccad",
            "\uc9c0\uc790\uccb4",
        ],
    },
    {
        "source_name": "IBK Industrial Bank of Korea",
        "query_name": "\uae30\uc5c5\uc740\ud589",
        "source_type": "public_financial_institution",
        "reliability_score": 4,
        "search_url_base": "https://www.ibk.co.kr/search/search.jsp?kwd=",
        "keywords": [
            "IBK",
            "\uae30\uc5c5\uc740\ud589",
            "\uc911\uc18c\uae30\uc5c5",
            "\uae08\ub9ac",
            "\uc8fc\ub2f4\ub300",
            "\uc804\uc138\ub300\ucd9c",
            "i-ONE",
        ],
    },
]

QUERY_STOPWORDS = {
    "\ub274\uc2a4",
    "\uae30\uc0ac",
    "\uad00\ub828",
    "\uc815\ucc45",
    "\uc815\ubd80",
    "\ud604\uc7ac",
    "\uc624\ub298",
    "\uc774\ubc88",
    "\ud574\ub2f9",
    "\ub300\ud574",
    "\ub4f1\uc744",
    "\ub4f1\uc774",
    "\ubc1d\ud614\ub2e4",
    "\uc804\ud588\ub2e4",
    "\ud55c\ub2e4",
    "\uc788\ub2e4",
    "\uc5c6\ub2e4",
    "\uc704\ud574",
    "\uc911",
    "\ubc0f",
    "\uc774\ucc98\ub7fc",
    "\ud604\uc2e4\uacfc",
    "\uc5ec\uac74",
    "\uad34\ub9ac\uac00",
    "\uc81c\ub3c4\ub294",
    "\uadfc\ub85c\uc790\uc758",
    "\uc548\uc815\uacfc",
}


EXTRA_SOURCE_KEYWORDS = {
    "Financial Services Commission": [
        "\uae08\uc735\uc704",
        "\uae08\uc735\uc704\uc6d0\ud68c",
        "\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1",
        "\uc0ac\ud68c\uc5f0\ub300\uae08\uc735",
        "\ud3ec\uc6a9\uae08\uc735",
        "\uc2e0\uc6a9\ud3c9\uac00",
        "\uc5ec\uc2e0\uc2dc\uc2a4\ud15c",
        "\uae08\uc735\uc9c0\uc6d0",
        "\uac00\uacc4\ubd80\ucc44",
        "\uc815\ucc45\uae08\uc735",
    ],
    "National Tax Service": [
        "\uad6d\uc138\uccad",
        "\uc591\ub3c4\uc138",
        "\uc591\ub3c4\uc18c\ub4dd\uc138",
        "\uc138\ubb34\uc870\uc0ac",
        "\ud0c8\ub8e8",
        "\uac00\uc0c1\uc790\uc0b0",
        "\ubd80\ub3d9\uc0b0",
    ],
    "Financial Supervisory Service": [
        "\uae08\uac10\uc6d0",
        "\uae08\uc735\uac10\ub3c5\uc6d0",
        "\uc5f0\uccb4\uc728",
        "\uac80\uc0ac",
        "\uac10\ub3c5",
        "\uc740\ud589",
    ],
    "Ministry of Land, Infrastructure and Transport": [
        "\uad6d\ud1a0\uad50\ud1b5\ubd80",
        "\uad6d\ud1a0\ubd80",
        "\uc804\uc138\uc0ac\uae30",
        "\uc2e4\uac70\uc8fc\uc790",
        "\ubd80\ub3d9\uc0b0 \uc2dc\uc7a5",
        "\uc784\ub300\ucc28",
        "\uc804\uc138",
    ],
    "Ministry of Economy and Finance": [
        "\uae30\ud68d\uc7ac\uc815\ubd80",
        "\uae30\uc7ac\ubd80",
        "\uad6c\uc724\ucca0",
        "\ubd80\ub3d9\uc0b0 \uc2dc\uc7a5",
        "\uc2e4\uac70\uc8fc\uc790",
        "\uacbd\uc81c\uc815\ucc45",
        "\uc138\uc81c",
    ],
    "Bank of Korea": [
        "\ud55c\uad6d\uc740\ud589",
        "\ud55c\uc740",
        "\uae30\uc900\uae08\ub9ac",
        "\uae08\ud1b5\uc704",
        "\ud1b5\ud654\uc815\ucc45",
        "\ubb3c\uac00",
    ],
    "Korea Housing & Urban Guarantee Corporation": [
        "\uc8fc\ud0dd\ub3c4\uc2dc\ubcf4\uc99d\uacf5\uc0ac",
        "HUG",
        "\uc804\uc138\ubcf4\uc99d",
        "\uc804\uc138\uc0ac\uae30",
    ],
    "Korea Land & Housing Corporation": [
        "\ud55c\uad6d\ud1a0\uc9c0\uc8fc\ud0dd\uacf5\uc0ac",
        "LH",
        "\uacf5\uacf5\uc8fc\ud0dd",
        "\uc784\ub300\uc8fc\ud0dd",
    ],
}


OFFICIAL_QUERY_PREFIXES = {
    "Financial Services Commission": ["\uae08\uc735\uc704\uc6d0\ud68c", "\uae08\uc735\uc704"],
    "Financial Supervisory Service": ["\uae08\uc735\uac10\ub3c5\uc6d0", "\uae08\uac10\uc6d0"],
    "Ministry of Land, Infrastructure and Transport": ["\uad6d\ud1a0\uad50\ud1b5\ubd80", "\uad6d\ud1a0\ubd80"],
    "Ministry of Economy and Finance": ["\uae30\ud68d\uc7ac\uc815\ubd80", "\uae30\uc7ac\ubd80"],
    "Bank of Korea": ["\ud55c\uad6d\uc740\ud589", "\ud55c\uc740"],
    "National Tax Service": ["\uad6d\uc138\uccad"],
    "Korea Housing & Urban Guarantee Corporation": ["\uc8fc\ud0dd\ub3c4\uc2dc\ubcf4\uc99d\uacf5\uc0ac", "HUG"],
    "Korea Land & Housing Corporation": ["\ud55c\uad6d\ud1a0\uc9c0\uc8fc\ud0dd\uacf5\uc0ac", "LH"],
}


def _extend_source_catalog_keywords() -> None:
    for source in OFFICIAL_SOURCE_CATALOG:
        extras = EXTRA_SOURCE_KEYWORDS.get(source.get("source_name"), [])
        for keyword in extras:
            if keyword not in source["keywords"]:
                source["keywords"].append(keyword)


_extend_source_catalog_keywords()


def _normalize_text(*values: str | None) -> str:
    return " ".join(value for value in values if value).strip()


def _score_source(source: dict, text: str, topic: str) -> tuple[int, list[str]]:
    reasons = []
    score = source["reliability_score"]

    for keyword in source["keywords"]:
        if keyword in text:
            reasons.append(f"matched keyword: {keyword}")
            score += 2

    if topic and any(keyword in topic for keyword in source["keywords"]):
        reasons.append(f"topic matches {source['source_name']}")
        score += 2

    source_name = source.get("source_name", "")
    if "\ubd80\ub3d9\uc0b0" in text and source_name in {"Ministry of Land, Infrastructure and Transport", "Ministry of Economy and Finance"}:
        reasons.append("topic family: real estate policy")
        score += 5
    if any(term in text for term in ["\uc591\ub3c4\uc138", "\uc591\ub3c4\uc18c\ub4dd\uc138", "\uc138\ubb34\uc870\uc0ac", "\ud0c8\ub8e8", "\uac00\uc0c1\uc790\uc0b0"]) and source_name == "National Tax Service":
        reasons.append("topic family: tax / real estate investigation")
        score += 6
    if any(term in text for term in ["\uae08\ub9ac", "\uae30\uc900\uae08\ub9ac", "\ud1b5\ud654\uc815\ucc45"]) and source_name == "Bank of Korea":
        reasons.append("topic family: interest rate")
        score += 5
    if any(term in text for term in ["\uc804\uc138\ub300\ucd9c", "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c", "DSR", "\uac00\uacc4\ubd80\ucc44"]) and source_name in {"Financial Services Commission", "Financial Supervisory Service"}:
        reasons.append("topic family: financial regulation")
        score += 5
    if any(term in text for term in ["\uc804\uc138\uc0ac\uae30", "\uc804\uc138\ubcf4\uc99d"]) and source_name in {"Ministry of Land, Infrastructure and Transport", "Korea Housing & Urban Guarantee Corporation"}:
        reasons.append("topic family: jeonse fraud / guarantee")
        score += 5

    return score, reasons


def _extract_query_keywords(text: str, source: dict, max_keywords: int = 6) -> list[str]:
    tokens = re.findall(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9.-]{1,}", text)
    keywords = []

    for keyword in source["keywords"]:
        if keyword in text and keyword not in keywords:
            keywords.append(keyword)

    for token in tokens:
        token = token.strip(" -_.,'\"\u2018\u2019\u201c\u201d\u2026")

        if len(token) < 2:
            continue
        if token in QUERY_STOPWORDS or token.lower() in QUERY_STOPWORDS:
            continue
        if token.isdigit():
            continue
        if token in keywords:
            continue
        if any(token in existing or existing in token for existing in keywords):
            continue

        keywords.append(token)

        if len(keywords) >= max_keywords:
            break

    return keywords[:max_keywords]


def _trim_query(query: str, max_chars: int = 80) -> str:
    query = re.sub(r"\s+", " ", query).strip()

    if len(query) <= max_chars:
        return query

    words = query.split()
    trimmed = []

    for word in words:
        candidate = " ".join(trimmed + [word])
        if len(candidate) > max_chars:
            break
        trimmed.append(word)

    return " ".join(trimmed) if trimmed else query[:max_chars].strip()


def _dedupe_queries(queries: list[str], max_queries: int = 3) -> list[str]:
    deduped = []

    for query in queries:
        normalized = _trim_query(query, max_chars=50)
        if not normalized:
            continue
        if normalized in deduped:
            continue
        if any(normalized in existing or existing in normalized for existing in deduped):
            continue
        deduped.append(normalized)
        if len(deduped) >= max_queries:
            break

    return deduped


def _pick_policy_terms(text: str, limit: int = 5) -> list[str]:
    priority_terms = [
        "\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1",
        "\uc0ac\ud68c\uc5f0\ub300\uae08\uc735\ud611\uc758\ud68c",
        "\uc0ac\ud68c\uc5f0\ub300\uae08\uc735",
        "\ud3ec\uc6a9\uae08\uc735",
        "\uc2e0\uc6a9\ud3c9\uac00",
        "\uc5ec\uc2e0\uc2dc\uc2a4\ud15c",
        "\uae08\uc735\uc9c0\uc6d0",
        "\uc2e4\uac70\uc8fc\uc790",
        "\ubd80\ub3d9\uc0b0 \uc2dc\uc7a5",
        "\uacfc\uc5f4",
        "\uc7ac\ud3b8",
        "\uad6c\uc724\ucca0",
        "\uc591\ub3c4\uc18c\ub4dd\uc138",
        "\uc591\ub3c4\uc138",
        "\uc138\ubb34\uc870\uc0ac",
        "\ud0c8\ub8e8",
        "\uac00\uc0c1\uc790\uc0b0",
        "\uc804\uc138\ub300\ucd9c",
        "\uc720\uc8fc\ud0dd\uc790",
        "1\uc8fc\ud0dd\uc790",
        "\uaddc\uc81c\uc9c0\uc5ed",
        "\uaddc\uc81c",
        "\uccad\ub144",
        "\ubc84\ud300\ubaa9",
        "\uc804\uc138\uc790\uae08",
        "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
        "\uc8fc\ub2f4\ub300",
        "\uae08\ub9ac\uac10\uba74",
        "\uae08\ub9ac",
        "DSR",
        "\uc5f0\uccb4\uc728",
        "\ubd80\ub3d9\uc0b0 PF",
        "PF",
        "\uc804\uc138\uc0ac\uae30",
        "\uc591\ub3c4\uc138",
        "\ubcf4\uc99d",
        "\uc8fc\ud0dd\uae08\uc735",
        "\uc911\uc18c\uae30\uc5c5",
        "\uadfc\ub85c\uc790",
        "i-ONE",
        "\uc774\ucc28\ubcf4\uc804",
        "\uc8fc\uac70\ube44",
        "\uc9c0\uc6d0",
    ]
    terms = []

    for term in priority_terms:
        if term in text and term not in terms:
            terms.append(term)
        if len(terms) >= limit:
            return terms

    for token in re.findall(r"[\uac00-\ud7a3A-Za-z0-9][\uac00-\ud7a3A-Za-z0-9.-]{1,}", text):
        token = token.strip(" -_.,'\"\u2018\u2019\u201c\u201d\u2026")
        if len(token) < 2 or token in QUERY_STOPWORDS or token.isdigit():
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= limit:
            break

    return terms


def _numbers_for_query(text: str, limit: int = 3) -> list[str]:
    numbers = re.findall(r"\d+(?:\.\d+)?\s*(?:%p|%|\uc870|\uc5b5|\ub9cc|\uc6d0|bp|BPS)?", text or "")
    cleaned = []
    for number in numbers:
        value = re.sub(r"\s+", "", number)
        if value and value not in cleaned:
            cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def _target_terms(text: str, limit: int = 3) -> list[str]:
    targets = [
        "\uccad\ub144",
        "\uc2e0\ud63c\ubd80\ubd80",
        "\uc18c\uc0c1\uacf5\uc778",
        "\uc911\uc18c\uae30\uc5c5",
        "\uadfc\ub85c\uc790",
        "\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1",
        "\uc2e4\uac70\uc8fc\uc790",
        "1\uc8fc\ud0dd\uc790",
        "\uc720\uc8fc\ud0dd\uc790",
    ]
    found = [term for term in targets if term in text]
    return found[:limit]


def _build_query_variants(source: dict, news_title: str, core_policy_issue: str, topic: str) -> list[str]:
    text = _normalize_text(news_title, core_policy_issue, topic)
    terms = _pick_policy_terms(text, limit=6)
    numbers = _numbers_for_query(text, limit=2)
    targets = _target_terms(text, limit=2)
    primary_terms = terms[:4]
    short_terms = terms[:3]
    entity_terms = terms[:3]
    query_name = source.get("query_name") or ""
    source_name = source.get("source_name")
    variants = []

    if source_name == "Financial Services Commission":
        if "\ud3ec\uc6a9\uae08\uc735" in text or "\uc2e0\uc6a9\ud3c9\uac00" in text or "\uc5ec\uc2e0\uc2dc\uc2a4\ud15c" in text:
            variants.extend(["\uae08\uc735\uc704\uc6d0\ud68c \ud3ec\uc6a9\uae08\uc735 TF", "\ud3ec\uc6a9\uae08\uc735 \uc2e0\uc6a9\ud3c9\uac00 \uc5ec\uc2e0\uc2dc\uc2a4\ud15c", "\uae08\uc735\uc704 \ud3ec\uc6a9\uae08\uc735"])
        elif "\uc804\uc138\ub300\ucd9c" in text:
            variants.extend(["\uc804\uc138\ub300\ucd9c \uaddc\uc81c", "\uc804\uc138\ub300\ucd9c \uaddc\uc81c\uc9c0\uc5ed", "\uae08\uc735\uc704 \uc804\uc138\ub300\ucd9c"])
        elif "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c" in text or "\uc8fc\ub2f4\ub300" in text:
            variants.extend(["\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c \uae08\ub9ac", "\uc8fc\ub2f4\ub300 \uae08\ub9ac", "\uae08\uc735\uc704 \uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c"])
    elif source_name == "IBK Industrial Bank of Korea":
        if "\uc804\uc138\ub300\ucd9c" in text:
            variants.extend(["\uc804\uc138\ub300\ucd9c \uae08\ub9ac", "i-ONE \uc804\uc138\ub300\ucd9c", "\uc911\uc18c\uae30\uc5c5 \uadfc\ub85c\uc790 \ub300\ucd9c"])
        elif "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c" in text or "\uc8fc\ub2f4\ub300" in text:
            variants.extend(["\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c \uae08\ub9ac", "\uc911\uc18c\uae30\uc5c5 \uadfc\ub85c\uc790 \ub300\ucd9c", "i-ONE \uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c"])

    prefixes = OFFICIAL_QUERY_PREFIXES.get(source_name, [query_name] if query_name else [])
    if source_name == "Financial Services Commission" and "\uc0ac\ud68c\uc5f0\ub300" in text:
        variants.extend([
            _normalize_text("\uae08\uc735\uc704\uc6d0\ud68c", "\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1", *numbers, "\uacf5\uae09"),
            _normalize_text("\uc0ac\ud68c\uc5f0\ub300\uae08\uc735\ud611\uc758\ud68c", "\uae08\uc735\uc704\uc6d0\ud68c"),
            _normalize_text("\uc0ac\ud68c\uc5f0\ub300\uacbd\uc81c\uc870\uc9c1", "\uae08\uc735\uc9c0\uc6d0", *numbers),
        ])
    if source_name == "Ministry of Economy and Finance" and "\ubd80\ub3d9\uc0b0" in text:
        variants.extend([
            _normalize_text("\uae30\ud68d\uc7ac\uc815\ubd80", "\ubd80\ub3d9\uc0b0 \uc2dc\uc7a5", "\uc2e4\uac70\uc8fc\uc790"),
            _normalize_text("\uad6c\uc724\ucca0", "\ubd80\ub3d9\uc0b0 \uc2dc\uc7a5", "\uacfc\uc5f4", "\uc2e4\uac70\uc8fc\uc790"),
        ])
    if source_name == "National Tax Service" and any(term in text for term in ["\uc591\ub3c4\uc138", "\uc591\ub3c4\uc18c\ub4dd\uc138", "\uc138\ubb34\uc870\uc0ac", "\ud0c8\ub8e8", "\uac00\uc0c1\uc790\uc0b0"]):
        variants.extend([
            _normalize_text("\uad6d\uc138\uccad", "\ubd80\ub3d9\uc0b0", "\uc591\ub3c4\uc18c\ub4dd\uc138", "\uc138\ubb34\uc870\uc0ac"),
            _normalize_text("\uad6d\uc138\uccad", "\uac00\uc0c1\uc790\uc0b0", "\ubd80\ub3d9\uc0b0", "\ud0c8\ub8e8"),
            _normalize_text("\uc591\ub3c4\uc138", "\ud0c8\ub8e8", "\uad6d\uc138\uccad"),
        ])
    for prefix in prefixes[:2]:
        variants.append(_normalize_text(prefix, *primary_terms[:3], *numbers[:1]))
        variants.append(_normalize_text(prefix, *targets[:1], *short_terms[:2]))

    variants.extend(
        [
            _normalize_text(*primary_terms),
            _normalize_text(*short_terms),
            _normalize_text(query_name, *entity_terms),
        ]
    )

    return _dedupe_queries(variants, max_queries=3)


def _build_search_query(source: dict, news_title: str, core_policy_issue: str, topic: str) -> str:
    variants = _build_query_variants(source, news_title, core_policy_issue, topic)
    if variants:
        return variants[0]

    source_text = _normalize_text(news_title, core_policy_issue)
    keywords = _extract_query_keywords(source_text, source, max_keywords=4)
    query_parts = []

    for part in [topic, source.get("query_name", ""), *keywords]:
        if not part:
            continue
        if part in query_parts:
            continue
        if any(part in existing or existing in part for existing in query_parts):
            continue
        query_parts.append(part)

    return _trim_query(_normalize_text(*query_parts), max_chars=80)


def build_official_search_url(source_name: str, source_type: str, search_query: str) -> str:
    encoded_query = quote(search_query)

    for source in OFFICIAL_SOURCE_CATALOG:
        if source["source_name"] == source_name:
            return f"{source['search_url_base']}{encoded_query}"

    if source_type == "local_government":
        return f"https://www.jeju.go.kr/search/search.htm?q={encoded_query}"

    return f"https://www.gov.kr/search?srhQuery={encoded_query}"


def generate_official_source_candidates(
    news_title: str,
    core_policy_issue: str,
    topic: str,
    max_candidates: int = 5,
) -> list[dict]:
    source_text = _normalize_text(news_title, core_policy_issue)
    topic_for_query = topic if len(_pick_policy_terms(source_text, limit=3)) < 2 else ""
    text = _normalize_text(source_text, topic_for_query)
    candidates = []

    for source in OFFICIAL_SOURCE_CATALOG:
        match_score, matched_reasons = _score_source(source, source_text, topic_for_query)
        if not matched_reasons and source["source_type"] not in {
            "financial_regulator",
            "central_government",
            "legislature",
        }:
            continue

        search_query = _build_search_query(
            source=source,
            news_title=news_title,
            core_policy_issue=core_policy_issue,
            topic=topic_for_query,
        )
        search_query_variants = _build_query_variants(
            source=source,
            news_title=news_title,
            core_policy_issue=core_policy_issue,
            topic=topic_for_query,
        ) or [search_query]
        search_query = search_query_variants[0]
        official_search_url = build_official_search_url(
            source_name=source["source_name"],
            source_type=source["source_type"],
            search_query=search_query,
        )
        reason = "; ".join(matched_reasons) if matched_reasons else "high-trust official source for policy verification"
        candidates.append(
            {
                "source_name": source["source_name"],
                "source_type": source["source_type"],
                "reliability_score": source["reliability_score"],
                "search_query": search_query,
                "primary_query": search_query_variants[0],
                "short_query": search_query_variants[1] if len(search_query_variants) > 1 else search_query_variants[0],
                "entity_query": search_query_variants[2] if len(search_query_variants) > 2 else search_query_variants[-1],
                "search_query_variants": search_query_variants,
                "official_search_url": official_search_url,
                "reason": reason,
                "_match_score": match_score,
            }
        )

    candidates.sort(key=lambda item: (item["_match_score"], item["reliability_score"]), reverse=True)

    return [
        {key: value for key, value in candidate.items() if key != "_match_score"}
        for candidate in candidates[:max_candidates]
    ]


def print_official_source_candidates(candidates: list[dict]):
    print("\n----- Official source candidates -----")

    if not candidates:
        print("No official source candidates generated.")
        return

    for candidate in candidates:
        print(f"- {candidate['source_name']} ({candidate['source_type']})")
        print(f"  reliability: {candidate['reliability_score']}/5")
        print(f"  query: {candidate['search_query']}")
        print(f"  search_query_variants: {', '.join(candidate.get('search_query_variants') or [])}")
        print(f"  official_search_url: {candidate['official_search_url']}")
        print(f"  reason: {candidate['reason']}")
