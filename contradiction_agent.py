from datetime import datetime, timezone
import hashlib
import re

from text_utils import sanitize_text


EXPLICIT_CONTRADICTION_KEYWORDS = [
    "반박",
    "사실 아님",
    "사실이 아님",
    "사실과 다르다",
    "해명",
    "부인",
    "정정",
    "오류",
    "허위",
    "가짜",
    "misleading",
    "false",
    "denied",
    "correction",
]

OPPOSING_ACTIONS = [
    ({"인상", "상승", "확대", "강화", "시행", "확정", "승인"}, {"인하", "하락", "축소", "완화", "중단", "부인", "철회"}),
    ({"규제", "제한", "차단", "금지"}, {"완화", "허용", "해제"}),
    ({"지원", "감면", "혜택", "보조"}, {"축소", "중단", "폐지"}),
]

SOURCE_SCORE_MINIMUM = 45


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _tokens(text: str) -> set[str]:
    cleaned = sanitize_text(text or "")
    raw_tokens = re.findall(r"[\uac00-\ud7a3A-Za-z0-9.%]+", cleaned)
    tokens = set()
    for token in raw_tokens:
        variants = {token}
        for suffix in ["으로", "에서", "에게", "까지", "부터", "보다", "처럼", "했다", "한다", "되는", "하고", "하며", "을", "를", "은", "는", "이", "가", "의", "와", "과", "도", "만", "로"]:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                variants.add(token[: -len(suffix)])
        tokens.update(item for item in variants if len(item) >= 2)
    return tokens


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?\s*(?:%p|%|조원|억원|만원|원|건|명|배)?", text or ""))


def _date_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d{4}년|\d{1,2}월|\d{1,2}일|올해|내년|지난해|전월|전년", text or ""))


def _claim_text(claim: dict) -> str:
    return sanitize_text(
        " ".join(
            str(claim.get(key) or "")
            for key in ["claim_text", "actor", "action", "target", "object", "quantity", "date_or_time", "location"]
        )
    )


def _same_context_score(claim: dict, snippet: dict) -> tuple[int, list[str]]:
    claim_terms = _tokens(_claim_text(claim))
    text = " ".join(
        [
            snippet.get("evidence_text") or "",
            snippet.get("source_title") or "",
            snippet.get("publisher") or "",
        ]
    )
    evidence_terms = _tokens(text)
    overlap = sorted(claim_terms & evidence_terms)
    score = min(70, len(overlap) * 10)
    for field in ["actor", "target", "object"]:
        value = sanitize_text(str(claim.get(field) or ""))
        if value and value != "unknown" and value in text:
            score += 10
    if _numbers(_claim_text(claim)) & _numbers(text):
        score += 10
    if _date_tokens(_claim_text(claim)) & _date_tokens(text):
        score += 10
    return min(score, 100), overlap[:10]


def _credible_snippet(snippet: dict) -> bool:
    quality = int(snippet.get("evidence_quality_score") or 0)
    relevance = int(snippet.get("relevance_score") or 0)
    strength = snippet.get("evidence_strength")
    evidence_type = snippet.get("evidence_type")
    if evidence_type == "insufficient_evidence":
        return False
    if strength not in {"strong", "medium"} and quality < SOURCE_SCORE_MINIMUM:
        return False
    return quality >= SOURCE_SCORE_MINIMUM or relevance >= 45


def _has_explicit_denial(text: str) -> bool:
    lowered = sanitize_text(text).lower()
    return any(keyword.lower() in lowered for keyword in EXPLICIT_CONTRADICTION_KEYWORDS)


def _direction_conflict(claim: dict, evidence_text: str) -> bool:
    claim_terms = _tokens(_claim_text(claim))
    evidence_terms = _tokens(evidence_text)
    for left, right in OPPOSING_ACTIONS:
        if claim_terms & left and evidence_terms & right:
            return True
        if claim_terms & right and evidence_terms & left:
            return True
    return False


def _numeric_conflict(claim: dict, evidence_text: str) -> bool:
    claim_numbers = _numbers(_claim_text(claim))
    evidence_numbers = _numbers(evidence_text)
    if not claim_numbers or not evidence_numbers:
        return False
    return not bool(claim_numbers & evidence_numbers)


def _make_conflicting_evidence(snippet: dict, conflict_type: str, confidence: str) -> dict:
    return {
        "source_title": snippet.get("source_title") or "",
        "source_url": snippet.get("source_url") or "",
        "publisher": snippet.get("publisher") or "",
        "evidence_text": snippet.get("evidence_text") or "",
        "conflict_type": conflict_type,
        "confidence": confidence,
    }


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
    return " ".join(
        query.get("query") or ""
        for query in source_queries or []
        if int(query.get("claim_index") or -1) == claim_index
        and query.get("purpose") in {"contradiction", "fact_check"}
    )


def _evaluate_conflicts(claim: dict, snippets: list[dict]) -> tuple[list[dict], str, int]:
    conflicts = []
    candidate_count = 0
    for snippet in snippets:
        text = " ".join(
            [
                snippet.get("evidence_text") or "",
                snippet.get("source_title") or "",
                snippet.get("publisher") or "",
            ]
        )
        if not _credible_snippet(snippet):
            continue
        context_score, _overlap = _same_context_score(claim, snippet)
        if context_score < 45:
            continue
        candidate_count += 1

        explicit_denial = _has_explicit_denial(text)
        direction_conflict = _direction_conflict(claim, text)
        numeric_conflict = _numeric_conflict(claim, text)

        if explicit_denial and context_score >= 45:
            conflicts.append(_make_conflicting_evidence(snippet, "explicit_denial", "high"))
        elif numeric_conflict and context_score >= 60:
            conflicts.append(_make_conflicting_evidence(snippet, "numerical_mismatch", "medium"))
        elif direction_conflict and context_score >= 60:
            conflicts.append(_make_conflicting_evidence(snippet, "policy_status_mismatch", "medium"))

    if conflicts:
        if any(item.get("conflict_type") == "explicit_denial" for item in conflicts):
            return conflicts[:3], "explicit_conflict", candidate_count
        return conflicts[:3], "explicit_conflict", candidate_count
    if candidate_count:
        return [], "no_match", candidate_count
    return [], "context_mismatch", candidate_count


def _status_for_claim(
    *,
    claim: dict,
    snippets: list[dict],
    query_text: str,
) -> dict:
    credible_support = [
        snippet
        for snippet in snippets
        if _credible_snippet(snippet)
        and snippet.get("supports_claim") == "supports"
        and snippet.get("evidence_type") in {"direct_support", "indirect_support"}
    ]
    conflicts, verdict_source, candidate_count = _evaluate_conflicts(claim, snippets)

    if conflicts:
        has_high = any(item.get("confidence") == "high" for item in conflicts)
        return {
            "contradiction_status": "confirmed_contradiction" if has_high else "possible_contradiction",
            "contradiction_score": 88 if has_high else 58,
            "contradiction_reason": (
                "동일 대상·시점에 대해 상충되는 근거가 확인되었습니다."
                if has_high
                else "일부 상충 가능성이 있으나 같은 시점/대상인지 추가 확인이 필요합니다."
            ),
            "conflicting_evidence": conflicts,
            "missing_evidence_warning": "",
            "needs_human_review": True,
            "contradiction_verdict_source": verdict_source,
            "contradiction_candidate_count": candidate_count,
            "contradiction_matched_count": len(conflicts),
        }

    if credible_support:
        return {
            "contradiction_status": "no_contradiction",
            "contradiction_score": 5,
            "contradiction_reason": "직접적인 반박 근거는 확인되지 않았습니다.",
            "conflicting_evidence": [],
            "missing_evidence_warning": "",
            "needs_human_review": False,
            "contradiction_verdict_source": "no_match",
            "contradiction_candidate_count": candidate_count,
            "contradiction_matched_count": 0,
        }

    query_has_fact_check = _has_explicit_denial(query_text)
    return {
        "contradiction_status": "insufficient_contradiction_evidence",
        "contradiction_score": 30 if query_has_fact_check else 20,
        "contradiction_reason": "반박 여부를 판단할 충분한 독립 근거가 부족합니다.",
        "conflicting_evidence": [],
        "missing_evidence_warning": "증거 부족은 모순으로 처리하지 않았습니다.",
        "needs_human_review": bool(query_has_fact_check),
        "contradiction_verdict_source": "insufficient_evidence" if not snippets else verdict_source,
        "contradiction_candidate_count": candidate_count,
        "contradiction_matched_count": 0,
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
        checks.append(
            {
                "contradiction_id": _check_id(str(index), claim.get("claim_text") or "", checked_at),
                "claim_index": index,
                "claim_text": claim.get("claim_text") or "",
                **result,
                "checked_at": checked_at,
            }
        )

    summary = summarize_contradiction_checks(checks)
    print(f"[ContradictionAgent] checked {len(checks)} claims")
    print(f"[ContradictionAgent] possible contradictions: {summary.get('possible_contradiction_count', 0)}")
    print(f"[ContradictionAgent] confirmed contradictions: {summary.get('confirmed_contradiction_count', 0)}")
    print(f"[ContradictionAgent] verdict source: {summary.get('contradiction_verdict_source')}")
    return {
        "contradiction_checks": checks,
        "contradiction_summary": summary,
    }


def summarize_contradiction_checks(contradiction_checks: list[dict]) -> dict:
    checks = contradiction_checks or []
    counts = {
        "no_contradiction_count": 0,
        "possible_contradiction_count": 0,
        "confirmed_contradiction_count": 0,
        "likely_contradiction_count": 0,
        "insufficient_evidence_count": 0,
        "needs_official_confirmation_count": 0,
    }
    source_counts = {}
    searched = 0
    matched = 0
    for check in checks:
        status = check.get("contradiction_status")
        source = check.get("contradiction_verdict_source") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        searched += int(check.get("contradiction_candidate_count") or 0)
        matched += int(check.get("contradiction_matched_count") or 0)
        if status in {"no_contradiction", "no_contradiction_found"}:
            counts["no_contradiction_count"] += 1
        elif status == "possible_contradiction":
            counts["possible_contradiction_count"] += 1
        elif status in {"confirmed_contradiction", "likely_contradiction"}:
            counts["confirmed_contradiction_count"] += 1
            counts["likely_contradiction_count"] += 1
        elif status in {"insufficient_contradiction_evidence", "insufficient_evidence"}:
            counts["insufficient_evidence_count"] += 1
        elif status == "needs_official_confirmation":
            counts["needs_official_confirmation_count"] += 1

    if counts["confirmed_contradiction_count"]:
        risk = "high"
    elif counts["possible_contradiction_count"]:
        risk = "medium"
    elif counts["insufficient_evidence_count"]:
        risk = "watch"
    else:
        risk = "low"

    verdict_source = max(source_counts, key=source_counts.get, default="no_match")
    return {
        "total_claims_checked": len(checks),
        **counts,
        "contradiction_candidates_searched": searched,
        "contradiction_candidates_matched": matched,
        "confirmed_contradictions": counts["confirmed_contradiction_count"],
        "possible_contradictions": counts["possible_contradiction_count"],
        "contradiction_verdict_source": verdict_source,
        "contradiction_verdict_source_counts": source_counts,
        "overall_contradiction_risk": risk,
    }
