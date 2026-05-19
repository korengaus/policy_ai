"""Phase 2 M5.8: threshold recommendation helper for semantic matching.

Pure-stdlib module that translates calibration scorecards (produced by
``semantic_calibration.summarize_calibration_results``) into a conservative
recommendation: should this provider be considered for any kind of
activation, and at what thresholds?

Strict design contract:
    * No network, no external dependencies.
    * Deterministic. Same inputs always produce the same outputs.
    * Never recommend ``verified``. The vocabulary is strictly:
      ``not_ready``, ``local_only``, ``debug_canary_candidate``.
    * Production user-facing activation is *not* an output of this module —
      that decision belongs to operators after a separate review.
    * Output is informational. Verdict-side modules never read this.

The reasoning here is intentionally simple — a transparent rule set is
easier to argue about than a learned threshold, and the safety bar is
"never let semantic-only signals overstate verification." If anything
looks risky, downgrade to ``not_ready``.
"""

from __future__ import annotations

from typing import Optional


# --- Thresholds ------------------------------------------------------------
# Tuned so that anything other than a clean calibration falls back to
# ``not_ready``. Operators can tune these later, but the defaults should
# never be loosened in production code.

MIN_RELATED_TOP1_RATE_FOR_CANDIDATE = 0.80
MIN_RELATED_TOP1_RATE_FOR_LOCAL = 0.60
RUNTIME_LATENCY_WARN_MS = 1500
RUNTIME_LATENCY_BLOCK_MS = 4000
HIGH_CAP_RATIO = 0.5  # fraction of cases whose support_level was capped


ACTIVATION_READINESS = ("not_ready", "local_only", "debug_canary_candidate")


def _ratio(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return float(numerator) / float(denominator)


def classify_activation_readiness(scorecard: dict) -> dict:
    """Inspect a single provider's scorecard. Returns activation readiness.

    The classification is deliberately conservative: any signal that a
    provider could overstate evidence pushes the result to ``not_ready``.
    """
    scorecard = scorecard or {}
    reasons: list[str] = []
    safety_notes: list[str] = []

    case_count = int(scorecard.get("case_count") or 0)
    fail_count = int(scorecard.get("fail_count") or 0)
    overstrong_count = int(scorecard.get("overstrong_count") or 0)
    related_top1_count = int(scorecard.get("related_top1_count") or 0)
    related_top1_eligible = int(scorecard.get("related_top1_eligible") or 0)
    related_top1_rate = scorecard.get("related_top1_rate")
    if related_top1_rate is None:
        related_top1_rate = _ratio(related_top1_count, related_top1_eligible)
    average_runtime_ms = int(scorecard.get("average_runtime_ms") or 0)
    cap_applied = int(scorecard.get("support_cap_applied_count") or 0)
    total_critical_mismatches = int(scorecard.get("total_critical_mismatches") or 0)

    cap_ratio = _ratio(cap_applied, case_count)

    readiness = "debug_canary_candidate"

    if case_count <= 0:
        reasons.append("no cases evaluated — scorecard is empty")
        readiness = "not_ready"

    if overstrong_count > 0:
        reasons.append(
            f"overstrong_count={overstrong_count} (the provider labeled a "
            "risky case as 'strong' — must be 0 before any activation)"
        )
        readiness = "not_ready"

    if fail_count > 0 and readiness != "not_ready":
        reasons.append(
            f"fail_count={fail_count} (calibration cases failed — review "
            "individual failures before considering activation)"
        )
        readiness = "not_ready"

    if related_top1_rate < MIN_RELATED_TOP1_RATE_FOR_LOCAL:
        reasons.append(
            f"related_top1_rate={related_top1_rate:.2f} is below "
            f"{MIN_RELATED_TOP1_RATE_FOR_LOCAL} — provider does not reliably "
            "rank the related official source first"
        )
        readiness = "not_ready"
    elif related_top1_rate < MIN_RELATED_TOP1_RATE_FOR_CANDIDATE:
        # Acceptable for local experimentation but not for canary.
        if readiness == "debug_canary_candidate":
            readiness = "local_only"
        reasons.append(
            f"related_top1_rate={related_top1_rate:.2f} is below "
            f"{MIN_RELATED_TOP1_RATE_FOR_CANDIDATE} — usable locally but not "
            "ready for canary"
        )

    if average_runtime_ms >= RUNTIME_LATENCY_BLOCK_MS:
        reasons.append(
            f"average_runtime_ms={average_runtime_ms} exceeds latency block "
            f"threshold {RUNTIME_LATENCY_BLOCK_MS} — too slow for any "
            "interactive activation"
        )
        readiness = "not_ready"
    elif average_runtime_ms >= RUNTIME_LATENCY_WARN_MS:
        safety_notes.append(
            f"average_runtime_ms={average_runtime_ms} exceeds warn threshold "
            f"{RUNTIME_LATENCY_WARN_MS} — verify Render request budget "
            "before any canary"
        )
        if readiness == "debug_canary_candidate":
            readiness = "local_only"

    if cap_applied > 0:
        safety_notes.append(
            f"critical-fact guardrails capped {cap_applied}/{case_count} "
            "cases — embeddings alone are insufficient; guardrails are "
            "doing meaningful safety work"
        )
    if cap_ratio >= HIGH_CAP_RATIO and case_count > 0:
        safety_notes.append(
            f"cap_ratio={cap_ratio:.2f} is high — semantic similarity is "
            "frequently misleading on this fixture; expand the calibration "
            "set before activation"
        )

    if total_critical_mismatches > 0:
        safety_notes.append(
            f"total_critical_mismatches={total_critical_mismatches} — these "
            "are claim/source disagreements caught by guardrails, not by "
            "embeddings alone"
        )

    # Always remind operators that this layer is never verification.
    safety_notes.append(
        "Semantic match strength is metadata only; rule-based verification "
        "and official body matching remain authoritative."
    )

    return {
        "activation_readiness": readiness,
        "reasons": reasons,
        "safety_notes": safety_notes,
        "metrics": {
            "case_count": case_count,
            "fail_count": fail_count,
            "overstrong_count": overstrong_count,
            "related_top1_rate": round(related_top1_rate, 3),
            "average_runtime_ms": average_runtime_ms,
            "support_cap_applied_count": cap_applied,
            "support_cap_applied_ratio": round(cap_ratio, 3),
            "total_critical_mismatches": total_critical_mismatches,
        },
    }


def recommend_thresholds(provider_results: dict) -> dict:
    """Compare provider scorecards and produce a conservative recommendation.

    ``provider_results`` is a dict keyed by provider name; each value must
    have a ``scorecard`` dict and an ``available`` flag. Output shape::

        {
            "activation_readiness": "...",
            "reasons": [...],
            "recommended_thresholds": {"support": None, "context": None},
            "safety_notes": [...],
            "per_provider": {
                "<name>": {
                    "available": bool,
                    "activation_readiness": "...",
                    "reasons": [...],
                    "safety_notes": [...],
                    "metrics": {...},
                },
                ...
            },
        }

    Per-provider classification uses ``classify_activation_readiness``. The
    aggregate ``activation_readiness`` is the *lowest* readiness across all
    available providers — one risky provider downgrades the whole recommendation.
    Thresholds are intentionally left as ``None``: M5.8 does not tune the
    cosine cutoffs from data; that calibration happens in a later phase once
    operators agree the comparison is trustworthy.
    """
    provider_results = provider_results or {}
    per_provider: dict[str, dict] = {}
    aggregate_reasons: list[str] = []
    aggregate_notes: list[str] = []

    rank = {"not_ready": 0, "local_only": 1, "debug_canary_candidate": 2}
    overall = "debug_canary_candidate"
    any_available = False
    has_real_provider_available = False  # openai = the production target

    for provider, payload in provider_results.items():
        payload = payload or {}
        scorecard = payload.get("scorecard") or {}
        available = bool(payload.get("available"))
        if not available:
            per_provider[provider] = {
                "available": False,
                "activation_readiness": "not_ready",
                "reasons": [f"provider {provider!r} reported available=False"],
                "safety_notes": [],
                "metrics": {},
            }
            aggregate_reasons.append(
                f"provider {provider!r} unavailable — no calibration data"
            )
            continue
        any_available = True
        if provider == "openai":
            has_real_provider_available = True
        classification = classify_activation_readiness(scorecard)
        per_provider[provider] = {
            "available": True,
            **classification,
        }
        # Bubble up reasons / notes verbatim with provider tag so the
        # aggregate explanation is traceable.
        for reason in classification["reasons"]:
            aggregate_reasons.append(f"[{provider}] {reason}")
        for note in classification["safety_notes"]:
            aggregate_notes.append(f"[{provider}] {note}")
        if rank[classification["activation_readiness"]] < rank[overall]:
            overall = classification["activation_readiness"]

    if not any_available:
        overall = "not_ready"
        aggregate_reasons.append(
            "no available provider scorecards — cannot recommend activation"
        )

    # Canary readiness requires the same provider that production would use.
    # The deterministic provider is a test surrogate; passing it alone is
    # necessary but not sufficient.
    if (
        overall == "debug_canary_candidate"
        and any_available
        and not has_real_provider_available
    ):
        overall = "local_only"
        aggregate_reasons.append(
            "no real-embedding provider (openai) measured — deterministic "
            "results alone cannot support a canary recommendation"
        )

    # Deduplicate aggregate safety notes while preserving order; the same
    # "Semantic match strength is metadata only..." line will otherwise show
    # up once per provider.
    seen_notes: set[str] = set()
    deduped_notes: list[str] = []
    for note in aggregate_notes:
        if note in seen_notes:
            continue
        seen_notes.add(note)
        deduped_notes.append(note)

    return {
        "activation_readiness": overall,
        "reasons": aggregate_reasons,
        "recommended_thresholds": {
            # Threshold tuning belongs to a later phase. Intentionally None
            # here so consumers do not mistake an evaluation default for a
            # production-ready cutoff.
            "support": None,
            "context": None,
        },
        "safety_notes": deduped_notes,
        "per_provider": per_provider,
    }
