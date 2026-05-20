"""Phase 2 M7.2: semantic debug-canary metrics helper.

Pure-stdlib parser that walks an analysis result payload (typically the
JSON returned by ``GET /jobs/{id}/result`` or a stored
``policy_analysis_*.json``) and extracts the runtime metrics the
operator needs to monitor a semantic debug canary — without touching
the payload, calling any external service, or influencing the verdict
path.

Design contract:
    * Pure stdlib. No network. No OpenAI. No Postgres.
    * Never mutates the input payload. Walks read-only.
    * Never raises on bad input — missing / malformed fields produce
      conservative defaults and warnings.
    * Never recommends ``verified``. Health classification vocabulary
      is strictly ``pass`` / ``warn`` / ``fail``.
    * Read-only. Verdict modules never import this helper, and this
      helper never reads or writes verdict fields.

Health rules (deterministic):
    fail when:
        - provider_error_count > 0
        - overstrong_like_count > 0
    warn when:
        - cap_ratio > 0.70
        - runtime_ms_p95 > 1500
        - semantic_enabled_count > 0 and semantic_available_count == 0
    pass otherwise.

``overstrong_like_count`` is intentionally conservative: it only counts
cases where (a) the raw support level is strong, (b) the adjusted
support level is also strong, AND (c) at least one risk flag is present
OR critical_mismatch_count > 0. That combination means a critical-fact
disagreement was detected yet the support label is still ``strong`` —
the exact failure mode M6.5 surfaced and M6.6 fixed.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Sequence


# Health vocabulary — kept in sync with semantic_thresholds where the
# vocabulary exists for activation-readiness scoring. This module's
# output is purely operational (canary monitoring), not activation
# readiness, but the words must stay consistent.
HEALTH_PASS = "pass"
HEALTH_WARN = "warn"
HEALTH_FAIL = "fail"

# Thresholds the canary trips on. Aligned with semantic_thresholds.py
# (HIGH_CAP_RATIO=0.5 is the calibration threshold; we use a more
# permissive 0.70 here because the canary monitors aggregate behavior
# across many real claims and the historical run already showed
# cap_ratio=0.0 — so anything > 0.70 in production is unambiguously a
# regression).
WARN_CAP_RATIO = 0.70
WARN_RUNTIME_MS_P95 = 1500


# ---------------------------------------------------------------------------
# Safe coercion helpers
# ---------------------------------------------------------------------------


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def percentile(values: Sequence[float], p: float) -> float:
    """Compute the ``p``-th percentile (0–100) using linear interpolation.
    Returns 0.0 on empty input. Sorts a copy so the caller's list is
    untouched."""
    if not values:
        return 0.0
    p = max(0.0, min(100.0, float(p)))
    sorted_values = sorted(float(v) for v in values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (p / 100.0) * (len(sorted_values) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return sorted_values[low]
    frac = rank - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac


# ---------------------------------------------------------------------------
# Extraction — walk the payload and collect every semantic_evidence_summary
# ---------------------------------------------------------------------------


def _summary_signature(summary: dict) -> str:
    """Content-hash dedupe key for a semantic_evidence_summary.

    The same logical summary often appears twice in a JSON-deserialized
    result payload (once under ``debug_summary.semantic_evidence_summary``,
    again under ``verification_card.debug_summary.semantic_evidence_summary``).
    On the server side these were the same dict; once serialized and
    re-parsed they're two distinct objects, so ``id()`` dedupe misses
    them. We hash a stable JSON serialization to catch content-equal
    duplicates without depending on object identity.
    """
    try:
        canonical = json.dumps(summary, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        # If something in the summary isn't JSON-serializable we fall
        # back to identity; this preserves the prior behavior rather
        # than crashing.
        return f"id:{id(summary)}"
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def extract_semantic_summaries(payload: Any, *, max_depth: int = 12) -> List[dict]:
    """Return every ``semantic_evidence_summary`` dict found anywhere in
    ``payload`` (typical sources: ``result.results[i].debug_summary``,
    ``result.results[i].verification_card.debug_summary``, or a top-level
    ``debug_summary``). Order is depth-first. Content-equal duplicates
    (same summary referenced from multiple paths) appear at most once
    regardless of whether they share object identity — important when
    the payload came in over HTTP and was deserialized into separate
    dicts per path.

    The walk is defensive: dicts, lists, and tuples are traversed; any
    other type is skipped. ``max_depth`` prevents pathological deeply
    nested payloads from blowing the stack.
    """
    out: List[dict] = []
    seen_signatures: set = set()

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth or node is None:
            return
        if isinstance(node, dict):
            target = node.get("semantic_evidence_summary")
            if isinstance(target, dict):
                signature = _summary_signature(target)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    out.append(target)
            for value in node.values():
                _walk(value, depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item, depth + 1)

    _walk(payload, 0)
    return out


def _result_count(payload: Any) -> int:
    """Best-effort result count. Tolerates several payload shapes:
    bare ``{"results": [...]}``, the wrapper ``{"result": {"results": [...]}}``,
    or a single-entry payload."""
    if not isinstance(payload, dict):
        return 0
    inner = payload.get("result")
    if isinstance(inner, dict):
        results = inner.get("results")
        if isinstance(results, list):
            return len(results)
        if isinstance(inner.get("news_results"), list):
            return len(inner["news_results"])
    if isinstance(payload.get("results"), list):
        return len(payload["results"])
    if isinstance(payload.get("news_results"), list):
        return len(payload["news_results"])
    return 0


# ---------------------------------------------------------------------------
# Summarize one payload across all its semantic summaries
# ---------------------------------------------------------------------------


def _bump(counter: Dict[str, int], key: Any) -> None:
    label = str(key) if key not in (None, "") else "(none)"
    counter[label] = counter.get(label, 0) + 1


def summarize_semantic_canary(payload: Any) -> dict:
    """Aggregate every ``semantic_evidence_summary`` in ``payload`` into a
    single canary-monitoring scorecard. The output shape is stable and
    safe to serialize as JSON."""
    summaries = extract_semantic_summaries(payload)
    result_count = _result_count(payload)

    semantic_enabled_count = 0
    semantic_available_count = 0
    provider_counts: Dict[str, int] = {}
    model_counts: Dict[str, int] = {}
    best_support_distribution: Dict[str, int] = {}
    raw_support_distribution: Dict[str, int] = {}
    risk_flag_counts: Dict[str, int] = {}
    critical_mismatch_count = 0
    support_cap_applied_count = 0
    total_claim_count = 0
    runtime_values: List[float] = []
    cache_hits_total = 0
    embedding_request_count_total = 0
    provider_errors: List[str] = []
    limitations: List[str] = []
    overstrong_like_count = 0
    warnings: List[str] = []

    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        enabled = bool(summary.get("semantic_matching_enabled"))
        available = bool(summary.get("semantic_matching_available"))
        if enabled:
            semantic_enabled_count += 1
        if available:
            semantic_available_count += 1

        _bump(provider_counts, summary.get("provider"))
        _bump(model_counts, summary.get("model"))
        _bump(best_support_distribution, summary.get("best_support_level"))
        _bump(raw_support_distribution, summary.get("best_raw_support_level"))

        for flag in summary.get("semantic_risk_flags") or []:
            if not flag:
                continue
            risk_flag_counts[str(flag)] = risk_flag_counts.get(str(flag), 0) + 1

        critical_mismatch_count += safe_int(summary.get("critical_mismatch_count"))
        support_cap_applied_count += safe_int(summary.get("support_cap_applied_count"))
        total_claim_count += safe_int(summary.get("claim_count"))
        runtime_ms = safe_int(summary.get("runtime_ms"))
        if runtime_ms > 0:
            runtime_values.append(float(runtime_ms))
        cache_hits_total += safe_int(summary.get("cache_hits"))
        embedding_request_count_total += safe_int(summary.get("embedding_request_count"))

        # Provider-side errors. The agent records both ``errors`` (per-call
        # failures) and ``limitations`` (informational notes). We surface
        # errors as fail signals; limitations are informational only.
        errs = summary.get("errors") or []
        if isinstance(errs, list):
            for err in errs:
                if err:
                    provider_errors.append(str(err))
        lims = summary.get("limitations") or []
        if isinstance(lims, list):
            for lim in lims:
                if lim:
                    limitations.append(str(lim))

        # Overstrong-like detection — per claim when ``claim_matches`` is
        # present, summary-level as a fallback. The per-claim check is
        # strictly conservative: a claim is overstrong-like only when
        # *that specific claim* (a) ended at support_level=strong, (b)
        # had raw_support_level=strong, (c) was NOT capped by guardrails
        # (support_cap_applied=False), AND (d) carries at least one risk
        # flag. A summary with one clean strong claim and one different
        # claim that was correctly capped to contextual is NOT overstrong.
        claim_matches = summary.get("claim_matches")
        if isinstance(claim_matches, list) and claim_matches:
            for claim in claim_matches:
                if not isinstance(claim, dict):
                    continue
                c_adj = str(claim.get("support_level") or "").lower()
                c_raw = str(claim.get("raw_support_level") or c_adj).lower()
                c_capped = bool(claim.get("support_cap_applied"))
                c_flags = list(claim.get("semantic_risk_flags") or [])
                if c_adj == "strong" and c_raw == "strong" and not c_capped and c_flags:
                    overstrong_like_count += 1
                    warnings.append(
                        f"overstrong_like (per-claim): support_level=strong "
                        f"raw=strong with uncapped risk flags {c_flags!r}"
                    )
        else:
            # Legacy / minimal payload — fall back to summary-level
            # signal. This path triggers when an old client sends just
            # the top-level scorecard without per-claim breakdown.
            adjusted = str(summary.get("best_support_level") or "").lower()
            raw = str(summary.get("best_raw_support_level") or adjusted).lower()
            flag_list = list(summary.get("semantic_risk_flags") or [])
            crit_count = safe_int(summary.get("critical_mismatch_count"))
            if adjusted == "strong" and raw == "strong" and (flag_list or crit_count > 0):
                overstrong_like_count += 1
                warnings.append(
                    "overstrong_like (summary fallback): adjusted=strong raw=strong "
                    f"with active risk flags or critical_mismatch_count={crit_count}"
                )

    # cap_ratio is a per-claim ratio when claim counts are available;
    # otherwise per-summary. Both are bounded in [0, 1] for sane inputs.
    cap_ratio = 0.0
    cap_basis = total_claim_count if total_claim_count > 0 else semantic_enabled_count
    if support_cap_applied_count > 0 and cap_basis > 0:
        cap_ratio = round(support_cap_applied_count / cap_basis, 3)

    runtime_ms_avg = int(round(sum(runtime_values) / len(runtime_values))) if runtime_values else 0
    runtime_ms_p95 = int(round(percentile(runtime_values, 95.0))) if runtime_values else 0

    summary_out: dict = {
        "result_count": result_count,
        "semantic_summary_count": len(summaries),
        "semantic_enabled_count": semantic_enabled_count,
        "semantic_available_count": semantic_available_count,
        "provider_counts": provider_counts,
        "model_counts": model_counts,
        "best_support_distribution": best_support_distribution,
        "raw_support_distribution": raw_support_distribution,
        "risk_flag_counts": risk_flag_counts,
        "critical_mismatch_count": critical_mismatch_count,
        "support_cap_applied_count": support_cap_applied_count,
        "cap_ratio": cap_ratio,
        "runtime_ms_avg": runtime_ms_avg,
        "runtime_ms_p95": runtime_ms_p95,
        "cache_hits_total": cache_hits_total,
        "embedding_request_count_total": embedding_request_count_total,
        "provider_error_count": len(provider_errors),
        "provider_errors": provider_errors,
        "limitations": limitations,
        "overstrong_like_count": overstrong_like_count,
        "warnings": warnings,
    }
    summary_out["health"] = classify_canary_health(summary_out)["health"]
    return summary_out


# ---------------------------------------------------------------------------
# Health classification
# ---------------------------------------------------------------------------


def classify_canary_health(summary: dict) -> dict:
    """Return ``{"health": pass|warn|fail, "reasons": [...]}``.

    Deterministic — same input always produces the same output. Rules
    are intentionally conservative; any ambiguity downgrades to fail."""
    reasons: List[str] = []

    provider_error_count = safe_int(summary.get("provider_error_count"))
    overstrong_like_count = safe_int(summary.get("overstrong_like_count"))
    cap_ratio = safe_float(summary.get("cap_ratio"))
    runtime_ms_p95 = safe_int(summary.get("runtime_ms_p95"))
    semantic_enabled_count = safe_int(summary.get("semantic_enabled_count"))
    semantic_available_count = safe_int(summary.get("semantic_available_count"))

    health = HEALTH_PASS

    if provider_error_count > 0:
        reasons.append(
            f"provider_error_count={provider_error_count} (any non-zero forces fail)"
        )
        health = HEALTH_FAIL

    if overstrong_like_count > 0:
        reasons.append(
            f"overstrong_like_count={overstrong_like_count} (a critical mismatch was "
            "detected but the support level remained strong)"
        )
        health = HEALTH_FAIL

    if health != HEALTH_FAIL:
        if cap_ratio > WARN_CAP_RATIO:
            reasons.append(
                f"cap_ratio={cap_ratio:.3f} exceeds warn threshold "
                f"{WARN_CAP_RATIO:.2f} — guardrails carrying high safety load"
            )
            health = HEALTH_WARN
        if runtime_ms_p95 > WARN_RUNTIME_MS_P95:
            reasons.append(
                f"runtime_ms_p95={runtime_ms_p95} exceeds warn threshold "
                f"{WARN_RUNTIME_MS_P95} — verify Render request budget"
            )
            health = HEALTH_WARN
        if semantic_enabled_count > 0 and semantic_available_count == 0:
            reasons.append(
                f"semantic_enabled_count={semantic_enabled_count} but "
                "semantic_available_count=0 — provider is configured but unavailable"
            )
            health = HEALTH_WARN

    return {"health": health, "reasons": reasons}


# ---------------------------------------------------------------------------
# Report helpers (Markdown / single-line text)
# ---------------------------------------------------------------------------


def format_summary_line(summary: dict) -> str:
    """One-line operator-readable summary. Suitable for smoke output."""
    return (
        "result_count={rc} semantic_summary_count={ssc} "
        "semantic_enabled={se} semantic_available={sa} "
        "provider_errors={pe} overstrong_like={ol} "
        "cap_ratio={cr:.3f} runtime_p95_ms={rp} health={h}"
    ).format(
        rc=safe_int(summary.get("result_count")),
        ssc=safe_int(summary.get("semantic_summary_count")),
        se=safe_int(summary.get("semantic_enabled_count")),
        sa=safe_int(summary.get("semantic_available_count")),
        pe=safe_int(summary.get("provider_error_count")),
        ol=safe_int(summary.get("overstrong_like_count")),
        cr=safe_float(summary.get("cap_ratio")),
        rp=safe_int(summary.get("runtime_ms_p95")),
        h=summary.get("health") or HEALTH_PASS,
    )


def format_markdown_report(summary: dict, *, base_url: Optional[str] = None) -> str:
    """Multi-section Markdown report. Always includes the conservative
    disclaimer that semantic match is metadata only."""
    lines: List[str] = []
    lines.append("# Semantic Debug Canary Report")
    lines.append("")
    if base_url:
        lines.append(f"- base_url: `{base_url}`")
    health = summary.get("health") or HEALTH_PASS
    lines.append(f"- health: **`{health}`**")
    lines.append(f"- result_count: {safe_int(summary.get('result_count'))}")
    lines.append(f"- semantic_summary_count: {safe_int(summary.get('semantic_summary_count'))}")
    lines.append(f"- semantic_enabled_count: {safe_int(summary.get('semantic_enabled_count'))}")
    lines.append(f"- semantic_available_count: {safe_int(summary.get('semantic_available_count'))}")
    lines.append(f"- provider_counts: `{summary.get('provider_counts') or {}}`")
    lines.append(f"- model_counts: `{summary.get('model_counts') or {}}`")
    lines.append("")
    lines.append("## Support distributions")
    lines.append("")
    lines.append(f"- best_support_distribution: `{summary.get('best_support_distribution') or {}}`")
    lines.append(f"- raw_support_distribution: `{summary.get('raw_support_distribution') or {}}`")
    lines.append("")
    lines.append("## Guardrails")
    lines.append("")
    lines.append(f"- risk_flag_counts: `{summary.get('risk_flag_counts') or {}}`")
    lines.append(f"- critical_mismatch_count: {safe_int(summary.get('critical_mismatch_count'))}")
    lines.append(f"- support_cap_applied_count: {safe_int(summary.get('support_cap_applied_count'))}")
    lines.append(f"- cap_ratio: {safe_float(summary.get('cap_ratio')):.3f}")
    lines.append(f"- overstrong_like_count: {safe_int(summary.get('overstrong_like_count'))}")
    lines.append("")
    lines.append("## Runtime")
    lines.append("")
    lines.append(f"- runtime_ms_avg: {safe_int(summary.get('runtime_ms_avg'))}")
    lines.append(f"- runtime_ms_p95: {safe_int(summary.get('runtime_ms_p95'))}")
    lines.append(f"- cache_hits_total: {safe_int(summary.get('cache_hits_total'))}")
    lines.append(f"- embedding_request_count_total: {safe_int(summary.get('embedding_request_count_total'))}")
    lines.append("")
    lines.append("## Provider state")
    lines.append("")
    lines.append(f"- provider_error_count: {safe_int(summary.get('provider_error_count'))}")
    errs = summary.get("provider_errors") or []
    if errs:
        lines.append("- provider_errors:")
        for e in errs[:10]:
            lines.append(f"  - {e}")
    lims = summary.get("limitations") or []
    if lims:
        lines.append("- limitations:")
        for l in lims[:10]:
            lines.append(f"  - {l}")
    warns = summary.get("warnings") or []
    if warns:
        lines.append("- warnings:")
        for w in warns[:10]:
            lines.append(f"  - {w}")
    lines.append("")
    lines.append(
        "> Semantic match strength is metadata only; rule-based "
        "verification and official body matching remain authoritative. "
        "Never treat a `strong` semantic label as verification. This "
        "report is operational monitoring only and is not a verdict signal."
    )
    lines.append("")
    return "\n".join(lines)
