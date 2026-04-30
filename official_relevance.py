import re


CONCEPT_SYNONYMS = {
    "rental_loan": ["전세대출", "전세자금", "버팀목", "임차보증금", "전세자금대출"],
    "mortgage_loan": ["주택담보대출", "주담대", "담보대출"],
    "interest_rate": ["금리", "이자", "우대금리", "감면"],
    "regulation": ["규제", "제한", "차단", "관리강화", "가계부채 관리", "가계부채"],
    "subsidy_support": ["지원", "보조", "보조금", "이차보전", "주거비", "혜택"],
    "target_group": ["청년", "신혼부부", "자녀출산", "중소기업 근로자", "중소기업", "근로자", "1주택자", "유주택자"],
    "implementation": ["시행", "운영", "신청", "모집", "공고", "접수", "적용"],
    "review_stage": ["검토", "추진", "조사", "착수", "논의", "현황", "파악"],
    "official_statement": ["발표", "보도자료", "설명자료", "브리핑", "공지"],
}

ERROR_SIGNALS = [
    "요청하신 페이지를 찾을 수 없습니다",
    "페이지를 찾을 수 없습니다",
    "존재하지 않는 페이지",
    "오류",
    "에러",
    "error",
    "not found",
    "404",
    "이용안내",
    "무인민원발급안내",
    "민원안내",
    "고객센터",
    "사이트맵",
    "로그인",
    "access denied",
    "forbidden",
]

HARD_ERROR_SIGNALS = [
    "요청하신 페이지를 찾을 수 없습니다",
    "페이지를 찾을 수 없습니다",
    "존재하지 않는 페이지",
    "error",
    "not found",
    "404",
    "access denied",
    "forbidden",
]

NAVIGATION_ERROR_SIGNALS = [
    "이용안내",
    "무인민원발급안내",
    "민원안내",
    "고객센터",
    "사이트맵",
    "로그인",
]

STOP_TERMS = {"뉴스", "기사", "관련", "정책", "정부", "이번", "해당", "대한", "나섰다"}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def extract_query_terms(query: str) -> list:
    terms = []

    for token in re.findall(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9.-]{1,}", query or ""):
        token = token.strip(" -_.,'\"")
        if len(token) < 2 or token in STOP_TERMS or token.isdigit():
            continue
        if token not in terms:
            terms.append(token)

    return terms[:12]


def extract_policy_concepts(text: str) -> set:
    normalized = normalize_text(text)
    concepts = set()

    for concept, synonyms in CONCEPT_SYNONYMS.items():
        if any(synonym in normalized for synonym in synonyms):
            concepts.add(concept)

    if any(
        synonym in normalized
        for synonym in [
            "\uae08\uc735\uc0c1\ud488",
            "\ube44\ub300\uba74\ub300\ucd9c",
            "\uc2e0\uc6a9\ub300\ucd9c",
            "\uc0dd\ud65c\uc548\uc815\uc790\uae08",
            "\uc0c1\ud488",
            "i-ONE",
            "i-one",
        ]
    ):
        concepts.add("financial_product_notice")

    return concepts


def detect_error_or_not_found_page(title: str, text: str, url: str = "") -> dict:
    normalized_title = normalize_text(title).lower()
    normalized_text = normalize_text(text).lower()
    normalized_url = normalize_text(url).lower()
    combined = f"{normalized_title} {normalized_text} {normalized_url}"

    signals = [signal for signal in HARD_ERROR_SIGNALS if signal.lower() in combined]
    signals.extend(
        signal
        for signal in NAVIGATION_ERROR_SIGNALS
        if (
            signal.lower() in normalized_title
            or signal.lower() in normalized_url
            or (len(normalized_text) < 500 and signal.lower() in normalized_text)
        )
    )
    if "gov.kr" in normalized_url:
        for signal in [
            "\uc5b4\ub514\uc11c\ub098 \ubbfc\uc6d0",
            "\ubbfc\uc6d0",
            "\ubbfc\uc6d0\uc548\ub0b4",
            "\ubbfc\uc6d0\uc11c\ube44\uc2a4",
            "\uc11c\ube44\uc2a4 \uc548\ub0b4",
            "\uc2e0\uccad",
            "\ubc1c\uae09",
            "\uc99d\uba85",
            "aa020anyinfocappview",
            "anyinfocappview",
        ]:
            if signal.lower() in combined:
                signals.append(signal)

    return {
        "is_error_page": bool(signals),
        "error_page_reason": ", ".join(signals) if signals else None,
        "error_page_signals": signals,
    }


def _policy_claims_text(policy_claims) -> str:
    parts = []

    for claim in policy_claims or []:
        if isinstance(claim, dict):
            parts.append(normalize_text(claim.get("sentence")))
        else:
            parts.append(normalize_text(claim))

    return " ".join(part for part in parts if part)


def _level(score: int, is_error: bool) -> str:
    if is_error:
        return "error_page"
    if score >= 70:
        return "high"
    if score >= 50:
        return "medium"
    if score >= 40:
        return "low"
    return "unrelated"


def score_document_relevance(news_context: dict, candidate: dict, document: dict) -> dict:
    query = news_context.get("search_query") or ""
    news_text = normalize_text(
        " ".join(
            [
                news_context.get("title") or "",
                news_context.get("summary") or "",
                news_context.get("topic") or "",
                _policy_claims_text(news_context.get("policy_claims")),
                (news_context.get("article_body") or "")[:1500],
            ]
        )
    )
    doc_title = normalize_text(document.get("document_title") or document.get("title"))
    doc_text = normalize_text(document.get("document_text_snippet") or document.get("text_snippet"))
    doc_url = document.get("url") or candidate.get("url") or ""
    combined_doc = normalize_text(f"{doc_title} {doc_text}")

    error_info = detect_error_or_not_found_page(doc_title, doc_text, doc_url)
    query_terms = extract_query_terms(query)
    matched_query_terms = [term for term in query_terms if term in combined_doc]
    missing_query_terms = [term for term in query_terms if term not in combined_doc]

    news_concepts = extract_policy_concepts(news_text)
    doc_concepts = extract_policy_concepts(combined_doc)
    matched_concepts = sorted(news_concepts & doc_concepts)
    missing_concepts = sorted(news_concepts - doc_concepts)

    score = 0
    reasons = []

    if query_terms:
        query_score = min(25, round((len(matched_query_terms) / len(query_terms)) * 25))
        score += query_score
        reasons.append(f"query term match: {query_score}")

    if news_concepts:
        concept_score = min(35, round((len(matched_concepts) / len(news_concepts)) * 35))
        score += concept_score
        reasons.append(f"policy concept match: {concept_score}")

    title_hits = sum(1 for term in query_terms if term in doc_title)
    if title_hits:
        title_score = min(20, title_hits * 5)
        score += title_score
        reasons.append(f"title relevance: {title_score}")

    link_score = min(10, int((candidate.get("score") or candidate.get("link_score") or 0) / 8))
    score += link_score
    reasons.append(f"source/link quality: {link_score}")

    text_length = document.get("document_text_length") or len(doc_text)
    quality_score = 10 if text_length >= 500 else 6 if text_length >= 200 else 2 if text_length >= 100 else 0
    score += quality_score
    reasons.append(f"document text quality: {quality_score}")

    if error_info["is_error_page"]:
        score -= 100
        reasons.append("error/not-found page penalty: -100")

    if document.get("document_title_quality") == "generic":
        score -= 10
        reasons.append("generic title penalty: -10")

    if text_length < 100:
        score -= 15
        reasons.append("short document text penalty: -15")

    if news_concepts and not matched_concepts:
        score -= 30
        reasons.append("topic/concept mismatch penalty: -30")

    doc_lower = f"{doc_title} {doc_text} {doc_url}".lower()
    if "gov.kr" in doc_lower and any(signal in doc_lower for signal in ["무인민원발급안내", "민원안내", "이용안내", "benefitserviceagree"]):
        score -= 100
        reasons.append("gov24 general guide page penalty: -100")

    if any(term in doc_title for term in ["국무회의", "전체", "홈페이지", "이용안내"]) and len(matched_query_terms) <= 1 and not matched_concepts:
        score -= 30
        reasons.append("generic/unrelated title penalty: -30")

    score = max(0, min(100, int(score)))

    return {
        "relevance_score": score,
        "relevance_level": _level(score, error_info["is_error_page"]),
        "matched_query_terms": matched_query_terms,
        "missing_query_terms": missing_query_terms,
        "matched_concepts": matched_concepts,
        "missing_concepts": missing_concepts,
        "relevance_reasons": reasons,
        "error_page_detected": error_info["is_error_page"],
        "error_page_reason": error_info["error_page_reason"],
    }
