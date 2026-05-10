from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

from official_metadata import is_official_domain, looks_like_official_search_or_index_url
from text_utils import sanitize_text


DETAIL_URL_SIGNALS = [
    "/press/",
    "/news/",
    "/policy/",
    "/announcement/",
    "/briefing/",
    "/report/",
    "/board/",
    "/bbs/",
    "/notice/",
    "/view",
    "view",
    "detail",
    "article",
    "press",
    "brd",
    "bbs",
]

WEAK_URL_SIGNALS = [
    "search",
    "list",
    "main",
    "index",
    "category",
    "menu",
    "portal",
    "login",
    "sitemap",
    "minwon",
]

POLICY_KEYWORDS = [
    "금융위",
    "금융위원회",
    "금감원",
    "금융감독원",
    "국토부",
    "국토교통부",
    "기재부",
    "기획재정부",
    "한국은행",
    "국세청",
    "경찰청",
    "법무부",
    "전세대출",
    "전세사기",
    "전세보증",
    "부동산",
    "양도세",
    "세무조사",
    "DSR",
    "대출",
    "금리",
    "주택",
    "임대차",
    "피해자",
    "지원",
    "공급",
    "규제",
    "조사",
    "수사",
    "보도자료",
    "설명자료",
    "브리핑",
    "공고",
    "공지",
]

ACTION_TERMS = [
    "지원",
    "공급",
    "발표",
    "시행",
    "공고",
    "조사",
    "수사",
    "규제",
    "제한",
    "완화",
    "인상",
    "인하",
    "감면",
    "보증",
    "대출",
    "처벌",
    "단속",
]


def _domain(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _tokens(text: str) -> set[str]:
    cleaned = sanitize_text(text or "")
    return {
        token
        for token in re.findall(r"[\uac00-\ud7a3A-Za-z0-9.%]+", cleaned)
        if len(token) >= 2 and not token.isdigit()
    }


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%?|\d+(?:조|억|만|천)?원|\d{4}년|\d+월|\d+일", text or ""))


def _split_sentences(text: str) -> list[str]:
    normalized = sanitize_text(text or "")
    parts = re.split(r"(?<=[.!?])\s+|(?<=[다요죠음함됨])\.\s*|(?<=[다요죠음함됨])\s+", normalized)
    seen = set()
    sentences = []
    for part in parts:
        sentence = sanitize_text(part).strip(" -•·")
        if not (25 <= len(sentence) <= 450):
            continue
        key = sentence[:140]
        if key in seen:
            continue
        seen.add(key)
        sentences.append(sentence)
    return sentences


def _claim_text(claim: dict) -> str:
    return sanitize_text(
        " ".join(
            str(claim.get(key) or "")
            for key in [
                "claim_text",
                "actor",
                "action",
                "target",
                "object",
                "quantity",
                "date_or_time",
                "location",
                "status",
                "claim_type",
            ]
        )
    )


def _extract_publish_date(text: str) -> str:
    match = re.search(r"(20\d{2}[.\-/년]\s?\d{1,2}[.\-/월]\s?\d{1,2}일?)", text or "")
    return sanitize_text(match.group(1)) if match else ""


def _extract_department(text: str) -> str:
    for pattern in [
        r"(담당부서|부서|담당과|소관부서)\s*[:：]?\s*([가-힣A-Za-z0-9·\s]{2,30})",
        r"(금융위원회|금융감독원|국토교통부|기획재정부|한국은행|국세청|경찰청|법무부|주택도시보증공사)",
    ]:
        match = re.search(pattern, text or "")
        if not match:
            continue
        value = match.group(2) if len(match.groups()) >= 2 else match.group(1)
        return sanitize_text(value)[:40]
    return ""


def score_official_url(url: str, title: str = "") -> dict:
    normalized_url = (url or "").lower()
    title_text = sanitize_text(title or "")
    score = 0
    reasons = []

    if is_official_domain(url):
        score += 25
        reasons.append("official_domain")
    if normalized_url.endswith(".pdf") or ".pdf" in normalized_url:
        score += 12
        reasons.append("pdf_policy_document")
    if any(signal in normalized_url for signal in DETAIL_URL_SIGNALS):
        score += 28
        reasons.append("detail_url_pattern")
    if re.search(r"\d{4,}", normalized_url):
        score += 10
        reasons.append("numeric_detail_id")
    if any(keyword in title_text for keyword in ["보도자료", "설명자료", "브리핑", "공고", "공지", "정책"]):
        score += 15
        reasons.append("official_content_title")
    if looks_like_official_search_or_index_url(url) or any(signal in normalized_url for signal in WEAK_URL_SIGNALS):
        score -= 30
        reasons.append("search_or_index_like")
    if not url:
        score -= 40
        reasons.append("url_missing")

    score = max(0, min(100, score))
    if score >= 65:
        status = "detail_page_likely"
    elif score >= 35:
        status = "candidate_needs_body_check"
    else:
        status = "weak_or_search_page"
    return {
        "official_url_score": score,
        "official_url_resolution_status": status,
        "official_url_resolution_reasons": reasons,
    }


def _sentence_match_score(claim: dict, sentence: str, source_title: str = "") -> dict:
    claim_text = _claim_text(claim)
    sentence_text = sanitize_text(sentence or "")
    combined_text = sanitize_text(f"{source_title} {sentence_text}")
    claim_terms = _tokens(claim_text)
    body_terms = _tokens(combined_text)
    matched_terms = sorted(claim_terms & body_terms)

    material_terms = [
        term
        for term in matched_terms
        if len(term) >= 3 or term in {"금리", "전세", "대출", "주택", "규제", "지원", "세금", "수사"}
    ]
    claim_numbers = _numbers(claim_text)
    matched_numbers = sorted(claim_numbers & _numbers(combined_text))
    action_matches = sorted(term for term in ACTION_TERMS if term in claim_text and term in combined_text)
    policy_matches = sorted(term for term in POLICY_KEYWORDS if term in claim_text and term in combined_text)

    semantic_match_score = min(100, len(material_terms) * 11 + len(policy_matches) * 8 + len(matched_numbers) * 15)
    policy_alignment_score = min(100, len(policy_matches) * 15 + len(action_matches) * 12 + len(matched_numbers) * 12)
    if source_title and any(term in source_title for term in policy_matches[:3]):
        policy_alignment_score = min(100, policy_alignment_score + 10)

    final_score = round(semantic_match_score * 0.45 + policy_alignment_score * 0.4 + min(100, len(material_terms) * 12) * 0.15)
    reason = (
        f"matched_terms={len(material_terms)}, "
        f"policy_terms={len(policy_matches)}, "
        f"numbers={len(matched_numbers)}, "
        f"actions={len(action_matches)}"
    )
    return {
        "sentence": sentence_text,
        "semantic_match_score": semantic_match_score,
        "policy_alignment_score": policy_alignment_score,
        "official_evidence_score": final_score,
        "matched_terms": material_terms[:12],
        "matched_numbers": matched_numbers[:8],
        "matched_policy_terms": policy_matches[:10],
        "matched_action_terms": action_matches[:8],
        "reason": reason,
    }


def _classify_official_evidence(score: int, has_body: bool, url_status: str) -> str:
    if not has_body:
        return "no_usable_official_detail"
    if url_status == "weak_or_search_page":
        return "weak_official_candidate_only"
    if score >= 75:
        return "strong_official_direct_support"
    if score >= 55:
        return "medium_official_contextual_support"
    if score >= 30:
        return "weak_official_candidate_only"
    return "no_usable_official_detail"


def _resolve_source(source: dict, claims: list[dict]) -> dict:
    item = dict(source or {})
    if item.get("source_type") not in {"official_government", "public_institution"}:
        return item

    url = item.get("official_detail_url") or item.get("official_body_url") or item.get("url") or ""
    title = item.get("title") or item.get("official_detail_title") or ""
    body_text = sanitize_text(item.get("official_body_text") or item.get("body_text") or item.get("raw_text") or "")
    url_resolution = score_official_url(url, title)
    sentences = _split_sentences(body_text)
    source_title = sanitize_text(title)

    all_matches = []
    for claim in claims or []:
        if int(claim.get("_claim_index", 0)) != int(item.get("claim_index") or 0):
            continue
        scored = sorted(
            (_sentence_match_score(claim, sentence, source_title) for sentence in sentences[:80]),
            key=lambda match: (-match["official_evidence_score"], match["sentence"]),
        )
        all_matches.extend(scored[:3])

    best = max(all_matches, key=lambda match: match["official_evidence_score"], default={})
    score = int(best.get("official_evidence_score") or 0)
    classification = _classify_official_evidence(
        score,
        bool(body_text and len(body_text) >= 300),
        url_resolution["official_url_resolution_status"],
    )
    body_status = "body_fetched" if body_text and len(body_text) >= 300 else "body_missing_or_short"
    if not url:
        body_status = "detail_url_missing"

    item.update(url_resolution)
    item.update(
        {
            "official_resolution_status": body_status,
            "official_document_kind": "pdf" if ".pdf" in (url or "").lower() else "html",
            "official_publish_date": _extract_publish_date(f"{title} {body_text[:1000]}"),
            "official_department": _extract_department(f"{title} {body_text[:1000]}"),
            "official_policy_keywords": [keyword for keyword in POLICY_KEYWORDS if keyword in f"{title} {body_text}"][:12],
            "semantic_match_score": int(best.get("semantic_match_score") or 0),
            "policy_alignment_score": int(best.get("policy_alignment_score") or 0),
            "official_evidence_score": score,
            "official_evidence_classification": classification,
            "official_direct_match_classification": classification,
            "official_matched_sentences": [
                {
                    "sentence": match["sentence"],
                    "score": match["official_evidence_score"],
                    "matched_terms": match["matched_terms"],
                    "matched_numbers": match["matched_numbers"],
                    "reason": match["reason"],
                }
                for match in sorted(all_matches, key=lambda match: -match["official_evidence_score"])[:3]
            ],
            "official_resolution_reason": best.get("reason")
            or (
                "official body missing or too short"
                if not body_text
                else "official body fetched but semantic match is weak"
            ),
        }
    )

    if classification in {"strong_official_direct_support", "medium_official_contextual_support"}:
        item["official_body_match"] = True
        item["official_body_match_score"] = max(int(item.get("official_body_match_score") or 0), score)
        item["official_final_direct_match_score"] = max(int(item.get("official_final_direct_match_score") or 0), score)
        item["official_body_match_reason"] = item["official_resolution_reason"]
        item["retrieval_method"] = "official_evidence_resolved"
    elif item.get("official_body_fetched"):
        item["official_body_match"] = False
        item["official_body_match_reason"] = item["official_resolution_reason"]
        item["official_final_direct_match_score"] = max(int(item.get("official_final_direct_match_score") or 0), score)

    return item


def resolve_official_evidence(
    source_candidates: list[dict],
    normalized_claims: list[dict],
) -> tuple[list[dict], dict]:
    indexed_claims = []
    for index, claim in enumerate(normalized_claims or []):
        item = dict(claim or {})
        item["_claim_index"] = index
        indexed_claims.append(item)

    resolved = [_resolve_source(source, indexed_claims) for source in (source_candidates or [])]
    official = [
        source
        for source in resolved
        if source.get("source_type") in {"official_government", "public_institution"}
    ]
    classifications = Counter(source.get("official_evidence_classification") or "unresolved" for source in official)
    failures = Counter()
    for source in official:
        if source.get("official_evidence_classification") in {"strong_official_direct_support", "medium_official_contextual_support"}:
            continue
        reason = (
            source.get("official_body_failure_reason")
            or source.get("official_url_resolution_status")
            or source.get("official_resolution_status")
            or "official_resolution_weak"
        )
        failures[reason] += 1

    summary = {
        "official_resolution_candidates": len(official),
        "official_resolution_body_fetched": sum(1 for source in official if source.get("official_body_fetched")),
        "official_resolution_direct_matches": classifications.get("strong_official_direct_support", 0),
        "official_resolution_contextual_matches": classifications.get("medium_official_contextual_support", 0),
        "official_resolution_weak_candidates": classifications.get("weak_official_candidate_only", 0),
        "official_resolution_no_detail": classifications.get("no_usable_official_detail", 0),
        "official_resolution_failures": dict(sorted(failures.items())),
        "official_resolution_top_score": max((int(source.get("official_evidence_score") or 0) for source in official), default=0),
    }
    print(
        "[OfficialEvidenceResolution] "
        f"candidates={summary['official_resolution_candidates']} "
        f"direct={summary['official_resolution_direct_matches']} "
        f"contextual={summary['official_resolution_contextual_matches']} "
        f"weak={summary['official_resolution_weak_candidates']} "
        f"top_score={summary['official_resolution_top_score']}"
    )
    return resolved, summary
