import re


EXCLUDED_DOCUMENT_TYPES = {
    "search_page",
    "menu_or_index_page",
    "service_page",
    "service_index_page",
    "faq_or_guide",
    "error_page",
    "attachment_only",
}

MATERIAL_POLICY_CONCEPTS = {
    "rental_loan",
    "mortgage_loan",
    "regulation",
    "interest_rate",
    "financial_product_notice",
}

CORE_CONCEPTS = {
    *MATERIAL_POLICY_CONCEPTS,
    "subsidy_support",
    "target_group",
    "implementation",
}

GENERIC_MATCH_CONCEPTS = {"review_stage", "implementation", "subsidy_support", "target_group"}

GOV24_SERVICE_EXCLUSION_SIGNALS = [
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
]

FSC_UNRELATED_TOPIC_SIGNALS = [
    "\uae08\uc735\uc678\uad50",
    "\uc778\ub3c4",
    "\ubca0\ud2b8\ub0a8",
    "\uac00\uc0c1\uc790\uc0b0",
    "\ud540\ud14c\ud06c",
    "\ud589\uc0ac",
    "\ud611\uc57d",
    "mmw",
    "cma",
    "\uc2dc\uc138\uc870\uc885",
]

GENERIC_TITLE_PHRASES = [
    "분야별 정책정보",
    "정책정보 목록",
    "전체 정책정보",
    "서비스 목록",
    "정책 목록",
    "이용안내",
    "민원안내",
    "전체보기",
    "검색결과",
    "정부24",
    "국토교통부",
    "금융위원회",
    "금융감독원",
]

LIST_URL_PATTERNS = [
    "listall",
    "portal/list",
    "policy/list",
    "service/list",
    "/list.",
    "/list?",
]

ERROR_SIGNALS = [
    "요청하신 페이지를 찾을 수 없습니다",
    "페이지를 찾을 수 없습니다",
    "존재하지 않는 페이지",
    "오류",
    "에러",
    "error",
    "not found",
    "404",
    "access denied",
    "forbidden",
]

PRESS_SIGNALS = ["보도자료", "설명자료", "브리핑", "press"]
NOTICE_SIGNALS = ["공지", "공고", "고시", "notice", "board"]
POLICY_SIGNALS = ["정책자료", "정책", "시행", "추진", "계획", "지원사업"]
GUIDE_SIGNALS = ["faq", "자주묻는", "이용안내", "민원안내", "고객센터", "무인민원발급"]
GENERIC_POLICY_TITLE_SIGNALS = ["국무회의", "공급방안", "전체", "홈페이지", "정책정보", "서비스"]


def _normalize(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _lower(value) -> str:
    return _normalize(value).lower()


def _contains_any(text: str, patterns: list[str]) -> bool:
    normalized = _lower(text)
    return any(pattern.lower() in normalized for pattern in patterns)


def _title_specificity_score(title: str, title_quality: str | None) -> int:
    normalized = _normalize(title)

    if not normalized:
        return 0
    if title_quality == "generic":
        return 5
    if _contains_any(normalized, GENERIC_TITLE_PHRASES):
        return 5
    if len(normalized) < 8:
        return 8
    if len(normalized) >= 20 and not _contains_any(normalized, GENERIC_POLICY_TITLE_SIGNALS):
        return 20
    return 14


def _keyword_overlap_score(matched_query_terms: list[str]) -> int:
    count = len(matched_query_terms or [])
    if count >= 5:
        return 20
    if count >= 2:
        return 15
    if count >= 1:
        return 8
    return 0


def _concept_overlap_score(matched_concepts: list[str]) -> int:
    core_matches = set(matched_concepts or []) & CORE_CONCEPTS
    if len(core_matches) >= 3:
        return 30
    if len(core_matches) == 2:
        return 22
    if len(core_matches) == 1:
        return 10
    return 0


def _topic_match_score(matched_concepts: list[str], matched_query_terms: list[str]) -> int:
    core_matches = set(matched_concepts or []) & CORE_CONCEPTS
    if len(core_matches) >= 2:
        return 20
    if len(core_matches) == 1 and len(matched_query_terms or []) >= 3:
        return 14
    if len(matched_query_terms or []) >= 3:
        return 10
    return 0


def _document_quality_score(text_length: int, title_specificity_score: int) -> int:
    score = 0

    if text_length >= 1000:
        score += 20
    elif text_length >= 500:
        score += 15
    elif text_length >= 300:
        score += 10
    elif text_length >= 100:
        score += 5

    if title_specificity_score >= 14:
        score += 5

    return min(25, score)


def _officiality_score(site_key: str, source_name: str, combined: str) -> int:
    score = 15 if site_key in {"fsc", "fss", "molit", "gov24", "ibk", "bok", "assembly"} else 8

    if _contains_any(combined, PRESS_SIGNALS + NOTICE_SIGNALS + POLICY_SIGNALS):
        score += 5
    if source_name:
        score += 2

    return min(20, score)


def _classify_type(title: str, url: str, text: str, site_key: str, is_error_page: bool) -> tuple[str, list[str]]:
    combined = f"{title} {url} {text}"
    normalized_url = _lower(url)
    reasons = []

    if is_error_page or _contains_any(combined, ERROR_SIGNALS):
        return "error_page", ["error/not-found signal"]

    if normalized_url.endswith((".pdf", ".hwp", ".hwpx", ".xls", ".xlsx", ".doc", ".docx")):
        return "attachment_only", ["attachment-only URL"]

    if site_key == "gov24" and _contains_any(combined, GOV24_SERVICE_EXCLUSION_SIGNALS):
        return "service_page", ["Gov24 civil-service/application guide page"]

    if site_key == "fsc" and re.search(r"/no01010[12]/\d{4,}", normalized_url):
        return "press_release", ["FSC detail press URL"]

    if site_key == "ibk" and (
        "noticedatadetailcyber.ibk" in normalized_url
        or any(pattern in normalized_url for pattern in ["detail", "view", "dtl"])
    ):
        return "press_release", ["IBK detail/news URL"]

    if site_key == "gov24" and (
        _contains_any(title, ["분야별 정책정보", "정책정보 목록", "서비스 목록", "이용안내", "민원안내"])
        or any(pattern in normalized_url for pattern in ["listall", "portal/list", "policy/list", "service/list"])
    ):
        return "service_index_page", ["Gov24 policy/service index page"]

    if _contains_any(title, GENERIC_TITLE_PHRASES) or any(pattern in normalized_url for pattern in LIST_URL_PATTERNS):
        return "service_index_page", ["generic list/index title or URL"]

    if "search" in normalized_url or _contains_any(title, ["검색결과"]):
        return "search_page", ["search page"]

    if _contains_any(combined, GUIDE_SIGNALS):
        return "faq_or_guide", ["guide/FAQ/minwon signal"]

    if any(pattern in normalized_url for pattern in ["/main", "main.do", "/index", "/home", "home.do"]):
        return "menu_or_index_page", ["main/menu/index URL"]

    if _contains_any(combined, PRESS_SIGNALS):
        return "press_release", reasons
    if _contains_any(combined, NOTICE_SIGNALS):
        return "official_notice", reasons
    if _contains_any(combined, ["시행", "운영", "신청", "모집", "공고", "계획"]):
        return "implementation_plan", reasons
    if _contains_any(combined, POLICY_SIGNALS):
        return "policy_release", reasons

    return "unrelated_page", ["no clear policy-document signal"]


def _grade(
    document_type: str,
    relevance_score: int,
    title_specificity_score: int,
    concept_overlap_score: int,
    keyword_overlap_score: int,
    should_exclude: bool,
    material_concept_count: int = 0,
) -> str:
    if should_exclude or document_type in EXCLUDED_DOCUMENT_TYPES:
        return "F"
    if material_concept_count == 0 and keyword_overlap_score < 15:
        return "F"
    if material_concept_count <= 1 and keyword_overlap_score <= 8:
        return "D"
    if document_type in {"press_release", "policy_release", "official_notice", "implementation_plan"}:
        if relevance_score >= 60 and concept_overlap_score >= 22 and title_specificity_score >= 14:
            return "A"
        if relevance_score >= 45 and (concept_overlap_score >= 10 or keyword_overlap_score >= 15):
            return "B"
        if relevance_score >= 35 and (concept_overlap_score >= 22 or keyword_overlap_score >= 15):
            return "C"
    if relevance_score >= 35 and concept_overlap_score >= 22:
        return "C"
    if relevance_score >= 25:
        return "D"
    return "E"


def classify_official_document(document: dict, source_name: str = "", site_key: str = "") -> dict:
    title = _normalize(document.get("document_title") or document.get("title"))
    text = _normalize(document.get("document_text_snippet") or document.get("text_snippet"))
    url = document.get("url") or document.get("selected_document_url") or ""
    matched_concepts = document.get("matched_concepts") or []
    matched_query_terms = document.get("matched_query_terms") or []
    relevance_score = int(document.get("document_relevance_score") or document.get("relevance_score") or 0)
    text_length = int(document.get("document_text_length") or len(text))
    is_error_page = bool(document.get("error_page_detected"))
    combined = f"{title} {url} {text}"

    document_type, type_reasons = _classify_type(title, url, text, site_key, is_error_page)
    title_score = _title_specificity_score(title, document.get("document_title_quality"))
    concept_score = _concept_overlap_score(matched_concepts)
    keyword_score = _keyword_overlap_score(matched_query_terms)
    topic_score = _topic_match_score(matched_concepts, matched_query_terms)
    quality_score = _document_quality_score(text_length, title_score)
    officiality_score = _officiality_score(site_key, source_name, combined)
    material_matches = set(matched_concepts) & MATERIAL_POLICY_CONCEPTS

    exclusion_reasons = list(type_reasons)
    if document_type in EXCLUDED_DOCUMENT_TYPES:
        exclusion_reasons.append(f"excluded document_type: {document_type}")
    if _contains_any(title, GENERIC_POLICY_TITLE_SIGNALS) and concept_score < 22 and keyword_score < 15:
        exclusion_reasons.append("generic policy title with weak concept/keyword overlap")
    if text_length < 300:
        exclusion_reasons.append("document text is shorter than 300 characters")
    if concept_score == 0 and keyword_score < 15:
        exclusion_reasons.append("insufficient core concept or query-token overlap")
    if (
        document_type in {"press_release", "policy_release", "official_notice", "implementation_plan"}
        and not material_matches
        and keyword_score < 15
    ):
        exclusion_reasons.append("insufficient material policy concept overlap")
    if (
        document_type in {"press_release", "policy_release", "official_notice", "implementation_plan"}
        and len(matched_query_terms) < 2
        and len(material_matches) < 2
    ):
        exclusion_reasons.append("insufficient matched query/material concept overlap")
    if site_key == "fsc" and _contains_any(combined, FSC_UNRELATED_TOPIC_SIGNALS) and (
        len(matched_query_terms) < 2 or len(material_matches) < 2
    ):
        exclusion_reasons.append("FSC unrelated general finance/foreign-affairs press release")

    should_exclude = bool(exclusion_reasons) and (
        document_type in EXCLUDED_DOCUMENT_TYPES
        or "generic policy title with weak concept/keyword overlap" in exclusion_reasons
        or "insufficient core concept or query-token overlap" in exclusion_reasons
        or "insufficient material policy concept overlap" in exclusion_reasons
        or "insufficient matched query/material concept overlap" in exclusion_reasons
        or "FSC unrelated general finance/foreign-affairs press release" in exclusion_reasons
    )

    evidence_grade = _grade(
        document_type=document_type,
        relevance_score=relevance_score,
        title_specificity_score=title_score,
        concept_overlap_score=concept_score,
        keyword_overlap_score=keyword_score,
        should_exclude=should_exclude,
        material_concept_count=len(material_matches),
    )

    return {
        "document_type": document_type,
        "evidence_grade": evidence_grade,
        "should_exclude_from_verification": should_exclude,
        "title_specificity_score": title_score,
        "concept_overlap_score": concept_score,
        "keyword_overlap_score": keyword_score,
        "topic_match_score": topic_score,
        "document_quality_score": quality_score,
        "officiality_score": officiality_score,
        "classification_reasons": exclusion_reasons or ["classified as usable official evidence candidate"],
    }
