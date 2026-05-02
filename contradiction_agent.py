from datetime import datetime, timezone
import hashlib
import re


CONTRADICTION_KEYWORDS = [
    "반박",
    "사실 아님",
    "사실이 아님",
    "해명",
    "부인",
    "정정",
    "오류",
    "가짜",
    "허위",
    "misleading",
    "false",
    "denied",
    "correction",
]

DENIED_STATUSES = {"denied"}
UNCERTAIN_STATUSES = {"proposed", "under_review", "uncertain"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?\s*(?:%p|%|조원|억원|만원|년|월|일)?", text or ""))


def _date_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d{4}년|\d{1,2}월|\d{1,2}일|오늘|내일|올해|내년", text or ""))


def _has_keyword(text: str, keywords: list[str]) -> bool:
    lowered = (text or "").lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _mapped_snippets(
    claim_index: int,
    evidence_snippets: list[dict],
    claim_evidence_map: dict,
) -> list[dict]:
    mapped_ids = set(claim_evidence_map.get(str(claim_index)) or claim_evidence_map.get(claim_index) or [])
    if mapped_ids:
        return [snippet for snippet in evidence_snippets or [] if snippet.get("evidence_id") in mapped_ids]
    return [
        snippet
        for snippet in evidence_snippets or []
        if int(snippet.get("claim_index") or -1) == claim_index
    ]


def _query_text_for_claim(claim_index: int, source_queries: list[dict]) -> str:
    parts = []
    for query in source_queries or []:
        if int(query.get("claim_index") or -1) == claim_index:
            parts.append(query.get("query") or "")
            parts.append(query.get("purpose") or "")
    return " ".join(parts)


def _make_conflicting_evidence(
    snippet: dict,
    conflict_type: str,
    confidence: str,
) -> dict:
    return {
        "source_title": snippet.get("source_title") or "",
        "source_url": snippet.get("source_url") or "",
        "publisher": snippet.get("publisher") or "",
        "evidence_text": snippet.get("evidence_text") or "",
        "conflict_type": conflict_type,
        "confidence": confidence,
    }


def _detect_mismatch(claim: dict, snippets: list[dict]) -> list[dict]:
    conflicts = []
    claim_text = claim.get("claim_text") or ""
    claim_numbers = _numbers(" ".join([claim_text, claim.get("quantity") or ""]))
    claim_dates = _date_tokens(" ".join([claim_text, claim.get("date_or_time") or ""]))
    claim_status = claim.get("status") or ""

    for snippet in snippets:
        evidence_text = snippet.get("evidence_text") or ""
        if snippet.get("evidence_type") == "insufficient_evidence":
            continue

        evidence_numbers = _numbers(evidence_text)
        if claim_numbers and evidence_numbers and not (claim_numbers & evidence_numbers):
            conflicts.append(_make_conflicting_evidence(snippet, "numerical_mismatch", "medium"))

        evidence_dates = _date_tokens(evidence_text)
        if claim_dates and evidence_dates and not (claim_dates & evidence_dates):
            conflicts.append(_make_conflicting_evidence(snippet, "date_mismatch", "medium"))

        if claim_status in DENIED_STATUSES and not _has_keyword(evidence_text, ["부인", "반박", "사실 아님", "denied"]):
            conflicts.append(_make_conflicting_evidence(snippet, "policy_status_mismatch", "medium"))
        if claim_status in UNCERTAIN_STATUSES and _has_keyword(evidence_text, ["확정", "시행", "implemented", "announced"]):
            conflicts.append(_make_conflicting_evidence(snippet, "policy_status_mismatch", "low"))

    return conflicts[:3]


def _explicit_denial_conflicts(snippets: list[dict], query_text: str) -> list[dict]:
    conflicts = []
    if _has_keyword(query_text, CONTRADICTION_KEYWORDS):
        conflicts.append(
            {
                "source_title": "Generated contradiction query",
                "source_url": "",
                "publisher": "",
                "evidence_text": query_text[:300],
                "conflict_type": "explicit_denial",
                "confidence": "low",
            }
        )

    for snippet in snippets:
        text = " ".join(
            [
                snippet.get("evidence_text") or "",
                snippet.get("source_title") or "",
                snippet.get("source_url") or "",
            ]
        )
        if _has_keyword(text, CONTRADICTION_KEYWORDS):
            conflicts.append(_make_conflicting_evidence(snippet, "explicit_denial", "high"))

    return conflicts[:3]


def _status_for_claim(
    *,
    claim: dict,
    snippets: list[dict],
    query_text: str,
) -> dict:
    explicit_conflicts = _explicit_denial_conflicts(snippets, query_text)
    mismatch_conflicts = _detect_mismatch(claim, snippets)
    conflicts = explicit_conflicts + mismatch_conflicts

    direct_support = [
        snippet
        for snippet in snippets
        if snippet.get("supports_claim") == "supports"
        and snippet.get("evidence_type") in {"direct_support", "indirect_support"}
    ]
    official_reference = [
        snippet for snippet in snippets if snippet.get("evidence_type") == "official_reference"
    ]
    insufficient = [
        snippet for snippet in snippets if snippet.get("evidence_type") == "insufficient_evidence"
    ]

    actual_denial_conflicts = [
        conflict
        for conflict in explicit_conflicts
        if conflict.get("source_title") != "Generated contradiction query"
    ]
    if actual_denial_conflicts:
        return {
            "contradiction_status": "likely_contradiction",
            "contradiction_score": 82,
            "contradiction_reason": "반박/해명/정정 등 명시적 부인 신호가 근거 또는 검색 후보에서 발견되었습니다.",
            "conflicting_evidence": conflicts[:3],
            "missing_evidence_warning": "",
            "needs_human_review": True,
        }

    if explicit_conflicts:
        return {
            "contradiction_status": "possible_contradiction",
            "contradiction_score": 48,
            "contradiction_reason": "반박/해명/정정 여부를 확인하기 위한 검색 후보가 생성되어 사람 검토가 필요합니다.",
            "conflicting_evidence": explicit_conflicts[:3],
            "missing_evidence_warning": "",
            "needs_human_review": True,
        }

    if mismatch_conflicts:
        return {
            "contradiction_status": "possible_contradiction",
            "contradiction_score": 55,
            "contradiction_reason": "근거는 있으나 숫자, 날짜, 주체 또는 정책 상태가 주장과 완전히 일치하지 않을 수 있습니다.",
            "conflicting_evidence": conflicts[:3],
            "missing_evidence_warning": "",
            "needs_human_review": True,
        }

    if official_reference and not direct_support:
        return {
            "contradiction_status": "needs_official_confirmation",
            "contradiction_score": 42,
            "contradiction_reason": "공식기관 후보는 있으나 실제 공식 문서 본문이 아직 확보되지 않았습니다.",
            "conflicting_evidence": [],
            "missing_evidence_warning": "공식기관 후보는 있으나 실제 공식 문서 본문이 아직 수집되지 않음",
            "needs_human_review": True,
        }

    if official_reference and direct_support:
        return {
            "contradiction_status": "needs_official_confirmation",
            "contradiction_score": 34,
            "contradiction_reason": "기사 본문 근거는 있으나 공식기관 후보의 본문 확인이 남아 있습니다.",
            "conflicting_evidence": [],
            "missing_evidence_warning": "공식기관 후보는 있으나 실제 공식 문서 본문이 아직 수집되지 않음",
            "needs_human_review": True,
        }

    if insufficient or not snippets:
        return {
            "contradiction_status": "insufficient_evidence",
            "contradiction_score": 50,
            "contradiction_reason": "주장을 지지하거나 반박할 충분한 근거 문장이 아직 확보되지 않았습니다.",
            "conflicting_evidence": [],
            "missing_evidence_warning": "검증 가능한 근거 문장이 부족합니다.",
            "needs_human_review": True,
        }

    if direct_support:
        max_support = max(int(snippet.get("relevance_score") or 0) for snippet in direct_support)
        return {
            "contradiction_status": "no_contradiction_found",
            "contradiction_score": 5 if max_support >= 70 else 18,
            "contradiction_reason": "현재 확보된 근거 문장은 주장과 같은 방향이며 명시적 반박 신호는 발견되지 않았습니다.",
            "conflicting_evidence": [],
            "missing_evidence_warning": "",
            "needs_human_review": False,
        }

    return {
        "contradiction_status": "insufficient_evidence",
        "contradiction_score": 50,
        "contradiction_reason": "근거가 배경 정보 수준이라 반박 여부를 판단하기 어렵습니다.",
        "conflicting_evidence": [],
        "missing_evidence_warning": "직접 근거가 부족합니다.",
        "needs_human_review": True,
    }


def run_contradiction_checks(
    *,
    normalized_claims: list[dict],
    evidence_snippets: list[dict],
    claim_evidence_map: dict,
    source_queries: list[dict] | None = None,
) -> dict:
    checks = []
    checked_at = _now_iso()

    for index, claim in enumerate(normalized_claims or []):
        snippets = _mapped_snippets(index, evidence_snippets or [], claim_evidence_map or {})
        query_text = _query_text_for_claim(index, source_queries or [])
        result = _status_for_claim(claim=claim, snippets=snippets, query_text=query_text)
        check = {
            "contradiction_id": _check_id(str(index), claim.get("claim_text") or "", checked_at),
            "claim_index": index,
            "claim_text": claim.get("claim_text") or "",
            "contradiction_status": result["contradiction_status"],
            "contradiction_score": result["contradiction_score"],
            "contradiction_reason": result["contradiction_reason"],
            "conflicting_evidence": result["conflicting_evidence"],
            "missing_evidence_warning": result["missing_evidence_warning"],
            "needs_human_review": result["needs_human_review"],
            "checked_at": checked_at,
        }
        checks.append(check)

    summary = summarize_contradiction_checks(checks)
    print(f"[ContradictionAgent] checked {len(checks)} claims")
    print(f"[ContradictionAgent] possible contradictions: {summary.get('possible_contradiction_count', 0) + summary.get('likely_contradiction_count', 0)}")
    print(f"[ContradictionAgent] official confirmation needed: {summary.get('needs_official_confirmation_count', 0)}")
    return {
        "contradiction_checks": checks,
        "contradiction_summary": summary,
    }


def summarize_contradiction_checks(contradiction_checks: list[dict]) -> dict:
    checks = contradiction_checks or []
    counts = {
        "no_contradiction_count": 0,
        "possible_contradiction_count": 0,
        "likely_contradiction_count": 0,
        "insufficient_evidence_count": 0,
        "needs_official_confirmation_count": 0,
    }
    for check in checks:
        status = check.get("contradiction_status")
        if status == "no_contradiction_found":
            counts["no_contradiction_count"] += 1
        elif status == "possible_contradiction":
            counts["possible_contradiction_count"] += 1
        elif status == "likely_contradiction":
            counts["likely_contradiction_count"] += 1
        elif status == "insufficient_evidence":
            counts["insufficient_evidence_count"] += 1
        elif status == "needs_official_confirmation":
            counts["needs_official_confirmation_count"] += 1

    if counts["likely_contradiction_count"]:
        risk = "high"
    elif counts["possible_contradiction_count"]:
        risk = "medium"
    elif counts["needs_official_confirmation_count"] or counts["insufficient_evidence_count"]:
        risk = "watch"
    else:
        risk = "low"

    return {
        "total_claims_checked": len(checks),
        **counts,
        "overall_contradiction_risk": risk,
    }
