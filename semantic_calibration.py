"""Phase 2 M5.6: semantic calibration classification helpers.

Standalone module used by ``scripts/evaluate_semantic_calibration.py`` and
``tests/test_semantic_calibration.py``. Has NO dependency on the production
pipeline, the verdict modules, or the API server. It only takes the agent's
``semantic_evidence_summary`` plus an ``expected`` block and decides whether
each calibration case passed.

The classification here is intentionally conservative — anything ambiguous
counts as a pass on the calibration side but is flagged so the evaluator
can surface it in the failure list. The goal is not to declare absolute
truth, but to give operators a repeatable scorecard before they enable real
embeddings in production.
"""

from __future__ import annotations

from typing import Iterable, List, Optional


SUPPORT_LEVEL_RANK = {
    "unavailable": 0,
    "weak": 1,
    "contextual": 2,
    "strong": 3,
}


def support_level_rank(level: object) -> int:
    """Numeric ordering of support labels. Unknown values fall to 0."""
    return SUPPORT_LEVEL_RANK.get(str(level or "").lower(), 0)


def is_overstrong(actual: str, expected: str) -> bool:
    """True when the model claims a stronger label than the case allows.

    The calibration fixtures encode ``should_not_be_strong`` for risky cases
    (number/date/eligibility mismatch, unrelated text, contradictions). When
    that's set, the actual label must be at most ``contextual``. ``expected``
    can also be a concrete level (``unavailable``, ``weak``, ...) and we
    treat any rank above it as overstrong.
    """
    if not expected or expected == "any":
        return False
    return support_level_rank(actual) > support_level_rank(expected)


def _related_source_marker(expected: dict) -> Optional[str]:
    marker = expected.get("related_source_url_contains")
    if isinstance(marker, str) and marker.strip():
        return marker.strip()
    return None


def _top_match_from_summary(summary: dict) -> Optional[dict]:
    """Pull the highest-ranked match across all claims in the summary."""
    best: Optional[dict] = None
    for claim_match in summary.get("claim_matches") or []:
        for match in claim_match.get("top_matches") or []:
            if best is None or float(match.get("score") or 0.0) > float(best.get("score") or 0.0):
                best = match
    return best


def _match_is_from_related(match: dict, marker: str) -> bool:
    if not match or not marker:
        return False
    blob = (
        str(match.get("source_id") or "")
        + " "
        + str(match.get("source_url") or "")
    )
    return marker in blob


def evaluate_case(summary: dict, expected: dict) -> dict:
    """Compare an agent summary against the case's expectations.

    Returns a row with:
        * ``checks``: per-check pass/fail flags + notes
        * ``passed``: bool — all required checks passed
        * ``failures``: list[str] of human-readable reasons
        * ``support_level``: the agent's reported best_support_level
        * ``best_score_percent``: agent's reported best score percentage
        * ``related_top1``: whether the related source ranked top-1 (when applicable)
        * ``overstrong``: whether the agent overstated support
    """
    expected = expected or {}
    summary = summary or {}

    actual_level = str(summary.get("best_support_level") or "unavailable").lower()
    raw_level = str(
        summary.get("best_raw_support_level") or actual_level
    ).lower()
    best_percent = int(summary.get("best_overall_score_percent") or 0)

    # Guardrail-side telemetry (M5.7). ``semantic_risk_flags`` may already
    # populate from the agent; we union with the fixture's own ``risk_flags``
    # below so the evaluator output captures both expected and observed risks.
    guardrail_risk_flags = list(summary.get("semantic_risk_flags") or [])
    critical_mismatch_count = int(summary.get("critical_mismatch_count") or 0)
    support_cap_applied_count = int(summary.get("support_cap_applied_count") or 0)

    checks: List[dict] = []
    failures: List[str] = []

    # Check 1: related source ranks first (when fixture specifies one).
    marker = _related_source_marker(expected)
    related_top1: Optional[bool] = None
    if marker and expected.get("should_rank_related_first", False):
        top = _top_match_from_summary(summary)
        related_top1 = bool(top and _match_is_from_related(top, marker))
        checks.append({
            "name": "related_ranks_first",
            "passed": related_top1,
            "marker": marker,
            "top_source_url": str((top or {}).get("source_url") or ""),
        })
        if not related_top1:
            failures.append(
                f"related source (url contains {marker!r}) did not rank top-1"
            )

    # Check 2: agent did not overstate support strength.
    overstrong = False
    if expected.get("should_not_be_strong", False):
        overstrong = actual_level == "strong"
        checks.append({
            "name": "not_overstrong",
            "passed": not overstrong,
            "actual": actual_level,
        })
        if overstrong:
            failures.append(
                f"agent reported support_level=strong but case is risky ({actual_level!r})"
            )

    # Check 3: explicit expected level (only enforced when not 'any').
    expected_level = str(expected.get("expected_support_level") or "any").lower()
    if expected_level and expected_level != "any":
        level_overstrong = is_overstrong(actual_level, expected_level)
        checks.append({
            "name": "expected_level_not_exceeded",
            "passed": not level_overstrong,
            "actual": actual_level,
            "expected_max": expected_level,
        })
        if level_overstrong:
            overstrong = True
            failures.append(
                f"agent reported {actual_level!r} but case expected at most {expected_level!r}"
            )

    # Check 4: unavailable handling for no-body cases.
    if expected.get("should_be_unavailable_when_no_body", False):
        passed = actual_level == "unavailable"
        checks.append({
            "name": "unavailable_when_no_body",
            "passed": passed,
            "actual": actual_level,
        })
        if not passed:
            failures.append(
                f"case has no official body but agent reported {actual_level!r}"
            )

    expected_risk_flags = list(expected.get("risk_flags") or [])
    combined_risk_flags = list(expected_risk_flags)
    for flag in guardrail_risk_flags:
        if flag not in combined_risk_flags:
            combined_risk_flags.append(flag)

    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "support_level": actual_level,
        "raw_support_level": raw_level,
        "best_score_percent": best_percent,
        "related_top1": related_top1,
        "overstrong": overstrong,
        "risk_flags": combined_risk_flags,
        "expected_risk_flags": expected_risk_flags,
        "semantic_risk_flags": guardrail_risk_flags,
        "critical_mismatch_count": critical_mismatch_count,
        "support_cap_applied_count": support_cap_applied_count,
        "support_cap_applied": support_cap_applied_count > 0,
        "category": expected.get("category") or "",
    }


def summarize_calibration_results(rows: Iterable[dict]) -> dict:
    """Aggregate per-case rows into a single scorecard."""
    rows = list(rows or [])
    case_count = len(rows)
    pass_count = 0
    fail_count = 0
    related_top1_count = 0
    related_top1_eligible = 0
    overstrong_count = 0
    unavailable_count = 0
    runtime_total_ms = 0
    cache_hits_total = 0
    embedding_requests_total = 0
    support_distribution: dict[str, int] = {}
    raw_support_distribution: dict[str, int] = {}
    cap_applied_count = 0
    total_critical_mismatches = 0
    semantic_risk_flag_counts: dict[str, int] = {}

    for row in rows:
        evaluation = row.get("evaluation") or {}
        summary = row.get("summary") or {}
        if evaluation.get("passed"):
            pass_count += 1
        else:
            fail_count += 1
        related = evaluation.get("related_top1")
        if related is not None:
            related_top1_eligible += 1
            if related:
                related_top1_count += 1
        if evaluation.get("overstrong"):
            overstrong_count += 1
        level = str(evaluation.get("support_level") or "").lower() or "unknown"
        support_distribution[level] = support_distribution.get(level, 0) + 1
        raw_level = str(
            evaluation.get("raw_support_level") or level
        ).lower() or "unknown"
        raw_support_distribution[raw_level] = raw_support_distribution.get(raw_level, 0) + 1
        if level == "unavailable":
            unavailable_count += 1
        if evaluation.get("support_cap_applied"):
            cap_applied_count += 1
        total_critical_mismatches += int(evaluation.get("critical_mismatch_count") or 0)
        for flag in evaluation.get("semantic_risk_flags") or []:
            semantic_risk_flag_counts[flag] = semantic_risk_flag_counts.get(flag, 0) + 1
        runtime_total_ms += int(summary.get("runtime_ms") or 0)
        cache_hits_total += int(summary.get("cache_hits") or 0)
        embedding_requests_total += int(summary.get("embedding_request_count") or 0)

    average_runtime_ms = int(round(runtime_total_ms / case_count)) if case_count else 0
    related_top1_rate = (
        round(related_top1_count / related_top1_eligible, 3)
        if related_top1_eligible else None
    )

    return {
        "case_count": case_count,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "related_top1_count": related_top1_count,
        "related_top1_eligible": related_top1_eligible,
        "related_top1_rate": related_top1_rate,
        "overstrong_count": overstrong_count,
        "unavailable_count": unavailable_count,
        "average_runtime_ms": average_runtime_ms,
        "total_runtime_ms": runtime_total_ms,
        "total_cache_hits": cache_hits_total,
        "total_embedding_request_count": embedding_requests_total,
        "support_level_distribution": support_distribution,
        "raw_support_level_distribution": raw_support_distribution,
        "support_cap_applied_count": cap_applied_count,
        "total_critical_mismatches": total_critical_mismatches,
        "semantic_risk_flag_counts": semantic_risk_flag_counts,
    }
