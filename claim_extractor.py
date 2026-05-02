import re


POLICY_KEYWORDS = [
    "정부",
    "금융당국",
    "금융위원회",
    "금감원",
    "국토부",
    "한국은행",
    "국회",
    "지자체",
    "은행",
    "기업은행",
    "규제",
    "제한",
    "차단",
    "금지",
    "검토",
    "추진",
    "조사",
    "착수",
    "시행",
    "운영",
    "지원",
    "확대",
    "축소",
    "감면",
    "인하",
    "인상",
    "동결",
    "대출",
    "전세대출",
    "주택담보대출",
    "주담대",
    "금리",
    "전세",
    "주택",
    "부동산",
    "청년",
    "중소기업",
]

OPINION_KEYWORDS = [
    "전망이다",
    "예상된다",
    "분석된다",
    "관측된다",
    "관측이",
    "관측도",
    "전망했다",
    "예상했다",
    "분석했다",
    "지적했다",
    "강조했다",
    "밝혔다",
    "주장했다",
    "평가했다",
    "의견",
    "칼럼",
    "사설",
]

WEAK_ENDINGS = [
    "것으로 보인다",
    "가능성도 있다",
    "필요가 있다",
    "해야 한다",
]


def _normalize_text(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    parts = re.split(r"(?<=[.!?다요죠음임됨함])\s+", normalized)
    sentences = []
    for part in parts:
        sentence = part.strip(" -•·\t\r\n")
        sentence = re.sub(r"\s+", " ", sentence)
        if 18 <= len(sentence) <= 260:
            sentences.append(sentence)
    return sentences


def _is_opinion(sentence: str) -> bool:
    opinion_hits = sum(1 for keyword in OPINION_KEYWORDS if keyword in sentence)
    has_policy_signal = any(keyword in sentence for keyword in POLICY_KEYWORDS)
    has_number = bool(re.search(r"\d", sentence))
    has_official_action = _has_official_actor_action(sentence)

    if opinion_hits and not has_official_action:
        return True
    if opinion_hits >= 2 and not has_number:
        return True
    if any(ending in sentence for ending in WEAK_ENDINGS) and not has_policy_signal:
        return True
    return False


def _has_official_actor_action(sentence: str) -> bool:
    return bool(
        re.search(
            r"(정부|당국|금융당국|금융위|금융위원회|금감원|금융감독원|국토부|한국은행|국회|은행|기업은행).{0,45}"
            r"(검토|추진|조사|착수|시행|운영|지원|제한|차단|금지|감면|인하|인상|동결|결정|발표)",
            sentence,
        )
    )


def _is_verifiable(sentence: str) -> bool:
    if not sentence:
        return False
    if _is_opinion(sentence):
        return False

    has_policy = any(keyword in sentence for keyword in POLICY_KEYWORDS)
    has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|%p|원|억원|조원|명|건|일|년|개월|주택자)?", sentence))
    has_actor_action = _has_official_actor_action(sentence)

    return has_actor_action or (has_policy and has_number) or sum(keyword in sentence for keyword in POLICY_KEYWORDS) >= 3


def _claim_score(sentence: str) -> int:
    score = 0
    score += min(len(sentence), 140)
    score += sum(18 for keyword in POLICY_KEYWORDS if keyword in sentence)
    if re.search(r"\d", sentence):
        score += 30
    if re.search(r"(검토|추진|조사|착수|시행|운영|지원|제한|차단|금지|감면|인하|인상|동결)", sentence):
        score += 35
    if re.search(r"(정부|금융당국|금융위|금감원|국토부|한국은행|국회|기업은행)", sentence):
        score += 30
    if _is_opinion(sentence):
        score -= 80
    return score


def _clean_claim(sentence: str) -> str:
    sentence = _normalize_text(sentence)
    sentence = re.sub(r"[^\w\s가-힣.,!?%·…~()\[\]{}<>:;\"'“”‘’/\-+_=|]", "", sentence)
    sentence = _normalize_text(sentence)
    sentence = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", sentence)
    if len(sentence) > 220:
        sentence = sentence[:217].rstrip() + "..."
    return sentence


def extract_verifiable_claims(
    article_body: str,
    title: str = "",
    summary: str = "",
    max_claims: int = 5,
) -> list[str]:
    source_text = article_body if article_body and len(article_body) >= 100 else ""
    fallback_text = summary or title or ""
    sentences = _split_sentences(source_text) if source_text else []

    ranked = sorted(
        (sentence for sentence in sentences if _is_verifiable(sentence)),
        key=_claim_score,
        reverse=True,
    )

    claims = []
    seen = set()
    for sentence in ranked:
        claim = _clean_claim(sentence)
        dedupe_key = re.sub(r"\W+", "", claim)[:80]
        if not claim or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        claims.append(claim)
        if len(claims) >= max_claims:
            break

    if not claims and fallback_text:
        fallback_claim = _clean_claim(fallback_text)
        if fallback_claim:
            claims.append(fallback_claim)

    print(f"[ClaimExtractor] extracted {len(claims)} claims")
    return claims
