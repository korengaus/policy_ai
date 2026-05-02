import re


ACTOR_PATTERNS = [
    "정부",
    "금융당국",
    "금융위원회",
    "금융위",
    "금융감독원",
    "금감원",
    "국토교통부",
    "국토부",
    "한국은행",
    "국회",
    "은행권",
    "은행",
    "IBK기업은행",
    "기업은행",
    "주택도시기금",
    "지자체",
    "제주도",
]

ACTION_PATTERNS = [
    "검토",
    "추진",
    "조사",
    "전수조사",
    "착수",
    "발표",
    "시행",
    "운영",
    "신청",
    "모집",
    "지원",
    "확대",
    "축소",
    "제한",
    "차단",
    "금지",
    "불허",
    "허용",
    "감면",
    "인하",
    "인상",
    "동결",
    "연장",
    "제출",
    "파악",
]

TARGET_PATTERNS = [
    "전세대출",
    "전세자금대출",
    "주택담보대출",
    "주담대",
    "가계대출",
    "대출",
    "금리",
    "전세보증금",
    "보증부 전세대출",
    "청년",
    "신혼부부",
    "1주택자",
    "유주택자",
    "중소기업",
    "중소기업 근로자",
    "소상공인",
    "부동산",
    "양도세",
    "장기보유특별공제",
]

OBJECT_PATTERNS = [
    "규제",
    "제한",
    "차단",
    "금지",
    "전수조사",
    "현황 자료",
    "대출이자",
    "우대금리",
    "금리 감면",
    "지원",
    "세제 개편",
    "공제",
    "보증",
    "만기 연장",
    "신청 기간",
]

LOCATION_PATTERNS = [
    "수도권",
    "규제지역",
    "서울",
    "경기",
    "인천",
    "제주",
    "일본",
    "미국",
    "영국",
    "프랑스",
]

DATE_PATTERN = re.compile(
    r"(\d{4}년|\d{1,2}월|\d{1,2}일|\d{1,2}~\d{1,2}일|지난달|지난\s*\d{1,2}일|오늘|내년|올해|2026년까지)"
)
QUANTITY_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:%p|%|조원|억원|만원|원|명|건|개월|년|일|주택자)|월\s*\d+\s*만\s*원|최대\s*\d+(?:\.\d+)?\s*(?:%p|%|만원|원))"
)


def _first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        if pattern in text:
            return pattern
    return ""


def _all_quantity(text: str) -> str:
    matches = [match.group(0).strip() for match in QUANTITY_PATTERN.finditer(text or "")]
    return ", ".join(dict.fromkeys(matches))


def _date_or_time(text: str) -> str:
    matches = [match.group(0).strip() for match in DATE_PATTERN.finditer(text or "")]
    return ", ".join(dict.fromkeys(matches))


def _status(text: str) -> str:
    if any(keyword in text for keyword in ["부인", "반박", "사실과 다르다", "해명", "아니다"]):
        return "denied"
    if any(keyword in text for keyword in ["시행", "운영", "신청", "적용", "제출 예정", "나섭니다"]):
        return "implemented"
    if any(keyword in text for keyword in ["발표", "공고", "확정", "결정"]):
        return "announced"
    if any(keyword in text for keyword in ["검토", "파악", "조사", "착수", "논의"]):
        return "under_review"
    if any(keyword in text for keyword in ["추진", "계획", "방안", "예정"]):
        return "proposed"
    return "uncertain"


def _claim_type(text: str, action: str, quantity: str, status: str) -> str:
    if any(keyword in text for keyword in ["전망", "관측", "분석", "전문가", "연구원"]):
        return "expert_opinion"
    if quantity and any(keyword in text for keyword in ["금리", "이자", "예대금리차", "%", "%p"]):
        return "financial_condition"
    if action and any(
        keyword in text
        for keyword in ["규제", "검토", "조사", "착수", "시행", "운영", "지원", "제한", "차단", "금지", "감면"]
    ):
        return "policy_action"
    if any(keyword in text for keyword in ["대상", "자격", "요건", "1주택자", "유주택자", "청년", "중소기업"]):
        return "eligibility"
    if quantity:
        return "numerical_claim"
    if any(keyword in text for keyword in ["오늘", "지난", "올해", "내년", "2026년", "연장", "기간"]):
        return "timeline_claim"
    if any(keyword in text for keyword in ["부담", "영향", "위축", "상승", "하락", "감소", "증가"]):
        return "market_impact"
    if action or status in {"proposed", "under_review", "announced", "implemented", "denied"}:
        return "policy_action"
    return "unknown"


def _uncertainty_level(text: str, status: str, claim_type: str) -> str:
    if status in {"implemented", "announced", "denied"}:
        return "low"
    if status == "under_review":
        return "medium"
    if claim_type == "expert_opinion":
        return "high"
    if any(keyword in text for keyword in ["가능성", "전망", "관측", "풀이", "보인다", "예상"]):
        return "high"
    return "medium"


def normalize_claim(claim_text: str) -> dict:
    text = (claim_text or "").strip()
    try:
        actor = _first_match(text, ACTOR_PATTERNS)
        action = _first_match(text, ACTION_PATTERNS)
        target = _first_match(text, TARGET_PATTERNS)
        obj = _first_match(text, OBJECT_PATTERNS)
        quantity = _all_quantity(text)
        date_or_time = _date_or_time(text)
        location = _first_match(text, LOCATION_PATTERNS)
        status = _status(text)
        claim_type = _claim_type(text, action, quantity, status)
        uncertainty_level = _uncertainty_level(text, status, claim_type)

        return {
            "claim_text": text,
            "actor": actor or "unknown",
            "action": action or "unknown",
            "target": target or "",
            "object": obj or "",
            "quantity": quantity,
            "date_or_time": date_or_time,
            "location": location,
            "status": status,
            "claim_type": claim_type,
            "uncertainty_level": uncertainty_level,
        }
    except Exception:
        return {
            "claim_text": text,
            "actor": "unknown",
            "action": "unknown",
            "target": "",
            "object": "",
            "quantity": "",
            "date_or_time": "",
            "location": "",
            "status": "uncertain",
            "claim_type": "unknown",
            "uncertainty_level": "high",
        }


def normalize_claims(claims: list[str]) -> list[dict]:
    normalized = [normalize_claim(claim) for claim in (claims or [])]
    print(f"[ClaimNormalizer] normalized {len(normalized)} claims")
    return normalized
