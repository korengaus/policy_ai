"""Phase 2 M11.0a: read-only verdict-producer comparison tool.

Re-runs the three current verdict producers against stored
``analysis_results`` rows (and optionally ``reports/policy_analysis_*.
json`` files) and records each one's output for the same input. Does
**not** modify any of the three producers, the live pipeline, or any
user-facing output. This is purely a measurement layer that feeds
into the future M11.0b consolidation milestone.

Three producers under comparison
--------------------------------

    1. ``policy_decision.make_final_decision``
       Inputs: ``policy_confidence``, ``policy_impact``.
       Output: dict with ``policy_alert_level`` in
       {``HIGH``, ``MEDIUM``, ``LOW``, ``WATCH``}.

    2. ``policy_scoring.calibrate_final_decision`` (alert via
       ``_alert_from_score``)
       Inputs: ``final_decision``, ``policy_confidence``,
       ``policy_impact``, ``verification_card``, ``source_candidates``,
       ``evidence_snippets``, ``debug_summary``.
       Output: ``(calibrated_decision, debug_summary)``. Calibrated
       ``policy_alert_level`` is in {``HIGH``, ``WATCH``, ``LOW``}
       (the calibrator's score-based decider does not emit MEDIUM).

    3. ``verification_card._verdict_label``
       Inputs: ``policy_confidence``, ``evidence_comparison``,
       ``official_sources``, ``evidence_snippets``,
       ``contradiction_summary``, ``bias_framing_summary``,
       ``claim_count``.
       Output: a ``draft_*`` label string (``draft_verified``,
       ``draft_likely_true``, ``draft_needs_context``,
       ``draft_unverified``, ``draft_disputed``,
       ``draft_needs_review``, ``draft_needs_official_confirmation``,
       ``draft_high_risk_review``).

Conservative-ordering ranking
-----------------------------

When computing ``most_conservative_label``, raw labels are mapped to
a numeric severity rank where **lower = more conservative** (less
action signal). The mapping is deliberately exhaustive and stable:

    rank 0 (most conservative — "no clear action")
        ``LOW`` (P1, P2), ``draft_unverified`` (P3)
    rank 1 (watch / needs-review — operator attention but not action)
        ``WATCH`` (P1, P2), ``draft_needs_context``,
        ``draft_needs_review``, ``draft_needs_official_confirmation``,
        ``draft_disputed``, ``draft_high_risk_review`` (P3)
    rank 2 (medium — likely-true / measurable signal)
        ``MEDIUM`` (P1), ``draft_likely_true`` (P3)
    rank 3 (high — verified / actionable)
        ``HIGH`` (P1, P2), ``draft_verified`` (P3)

The "most conservative" producer is the one whose label maps to the
lowest rank. Ties are broken by producer order (P1 wins, then P2,
then P3). Labels unknown to the map are treated as rank ``None`` and
do not participate in the conservatism comparison.

This ranking is itself a judgment that requires operator validation
before being used in M11.0b consolidation. It does NOT replace any
existing verdict logic.

Hard contract
-------------

    * Never invoked automatically. ``main.py`` / ``api_server.py`` /
      ``scheduler.py`` do not import this module.
    * Never mutates any of the three producer modules.
    * Never writes to the database (the CLI handles persistence).
    * ``truth_claim`` is forced to ``False`` on every
      ``ProducerComparison``.
    * ``operator_review_required`` is forced to ``True`` on every
      ``ProducerComparison``.
    * No ``requests`` / ``httpx`` / ``urllib.request`` / ``socket``
      imports.
    * No ``openai`` / ``anthropic`` / ``playwright`` /
      ``browser_use`` / ``openclaw`` / ``selenium`` imports.

Public surface (stable, pinned by tests)
----------------------------------------

    LABEL_SEVERITY_RANK
    NOTES_OPERATOR_REVIEW
    ProducerComparison                                       (dataclass)
    comparison_to_dict(comparison) -> dict
    compute_input_hash(payload) -> str
    compare_producers_for_analysis(analysis_row,
                                   source="sqlite") -> ProducerComparison
    compute_disagreement_summary(comparisons) -> dict
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Imports of the three producers — read-only.
#
# Importing these modules at module level is safe: each defines only
# functions (no top-level side effects, no DB connections, no HTTP).
# Verified by ``StaticSafetyTests`` in tests/test_verdict_producer_comparison.py.
# ---------------------------------------------------------------------------


from policy_decision import make_final_decision  # noqa: E402
from policy_scoring import (  # noqa: E402
    _alert_from_score,
    calibrate_final_decision,
)
from verification_card import _verdict_label  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------


NOTES_OPERATOR_REVIEW = (
    "verdict-producer disagreement analysis only — does not change any "
    "verdict; operator review required"
)


# Severity mapping. ``None`` = unknown / not ranked. Lower number is
# "more conservative" (less action signal). See module docstring for
# the rationale.
LABEL_SEVERITY_RANK: Dict[str, int] = {
    # Producer 1 + Producer 2 raw labels.
    "LOW": 0,
    "WATCH": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    # Producer 3 raw labels (draft_*).
    "draft_unverified": 0,
    "draft_needs_context": 1,
    "draft_needs_review": 1,
    "draft_needs_official_confirmation": 1,
    "draft_disputed": 1,
    "draft_high_risk_review": 1,
    "draft_likely_true": 2,
    "draft_verified": 3,
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ProducerComparison:
    """Stable wire shape consumed by tests, the CLI, and the DB save
    helper. The three ``producerN_*`` triples each carry whatever the
    producer returned (``label``, optional score, free-form ``extra``
    dict for diagnostic notes when the producer errored or had
    missing inputs).
    """
    analysis_id: str
    source: str
    input_hash: str

    producer1_label: Optional[str] = None
    producer1_score: Optional[float] = None
    producer1_extra: Dict[str, Any] = field(default_factory=dict)

    producer2_label: Optional[str] = None
    producer2_alert_level: Optional[str] = None
    producer2_score: Optional[float] = None
    producer2_extra: Dict[str, Any] = field(default_factory=dict)

    producer3_label: Optional[str] = None
    producer3_extra: Dict[str, Any] = field(default_factory=dict)

    all_three_agree: bool = False
    p1_p2_agree: bool = False
    p1_p3_agree: bool = False
    p2_p3_agree: bool = False
    disagreement_pattern: str = ""
    most_conservative_label: Optional[str] = None

    comparison_timestamp: str = ""
    notes: str = NOTES_OPERATOR_REVIEW
    # Always False. The tool never asserts truth — pinned by tests.
    truth_claim: bool = False
    # Always True. Disagreement signals always require human review —
    # pinned by tests.
    operator_review_required: bool = True


def comparison_to_dict(comparison: ProducerComparison) -> Dict[str, Any]:
    """Serialize a ``ProducerComparison`` to a plain dict (the shape
    ``database.save_producer_comparison`` expects).

    Re-asserts ``truth_claim=False`` and ``operator_review_required=True``
    so a defensive serializer cannot leak the wrong values even if a
    caller mutated the dataclass fields. The three ``producerN_extra``
    dicts are JSON-encoded so they land cleanly in ``TEXT`` columns.
    """
    payload = asdict(comparison)
    payload["truth_claim"] = False
    payload["operator_review_required"] = True
    payload["notes"] = payload.get("notes") or NOTES_OPERATOR_REVIEW
    for key in ("producer1_extra", "producer2_extra", "producer3_extra"):
        value = payload.get(key)
        if not isinstance(value, str):
            payload[key] = json.dumps(value or {}, ensure_ascii=False)
    return payload


# ---------------------------------------------------------------------------
# Input reconstruction + hashing
# ---------------------------------------------------------------------------


def _safe_json_load(value: Any) -> Any:
    """If ``value`` is a JSON-encoded string, decode it. Otherwise
    return as-is. Used to inflate the JSON-as-TEXT columns
    ``analysis_results`` stores (debug_summary, evidence_snippets,
    etc.)."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                return json.loads(stripped)
            except (TypeError, ValueError):
                return value
    return value


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _reconstruct_policy_confidence(row: Dict[str, Any]) -> Dict[str, Any]:
    """Build the partial ``policy_confidence`` dict the producers
    actually read, from the columns ``analysis_results`` stores.
    Missing fields default to safe values; nothing is invented."""
    return {
        "policy_confidence_score": _coerce_int(
            row.get("policy_confidence_score"),
        ),
        "verification_strength": row.get("verification_strength"),
        "risk_level": row.get("risk_level"),
        # The producers also read these when present; pull through if
        # the caller had a richer source object.
        "confidence_reasons": (
            _safe_json_load(row.get("confidence_reasons"))
            if row.get("confidence_reasons") is not None else []
        ),
        "confidence_evidence_grade": row.get("confidence_evidence_grade"),
    }


def _reconstruct_policy_impact(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "impact_level": row.get("impact_level"),
        "impact_direction": row.get("impact_direction"),
        "market_sensitivity": _coerce_int(row.get("market_sensitivity")),
        "consumer_sensitivity": _coerce_int(row.get("consumer_sensitivity")),
        "business_sensitivity": _coerce_int(row.get("business_sensitivity")),
        "affected_groups": (
            _safe_json_load(row.get("affected_groups")) or []
        ),
        "affected_sectors": (
            _safe_json_load(row.get("affected_sectors")) or []
        ),
        "impact_reasons": (
            _safe_json_load(row.get("impact_reasons")) or []
        ),
    }


def _reconstruct_verification_card(row: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct the portion of the ``verification_card`` dict the
    calibrator actually consumes: ``evidence_quality_summary``,
    ``source_reliability_summary``, ``contradiction_summary``,
    ``official_mismatch``, ``official_mismatch_reasons``."""
    return {
        "evidence_quality_summary": _safe_json_load(
            row.get("evidence_quality_summary")
        ) or {},
        "source_reliability_summary": _safe_json_load(
            row.get("source_reliability_summary")
        ) or {},
        "contradiction_summary": _safe_json_load(
            row.get("contradiction_summary")
        ) or {},
        "bias_framing_summary": _safe_json_load(
            row.get("bias_framing_summary")
        ) or {},
        "official_mismatch": bool(row.get("official_mismatch")),
        "official_mismatch_reasons": (
            _safe_json_load(row.get("official_mismatch_reasons")) or []
        ),
    }


def _reconstruct_evidence_comparison(row: Dict[str, Any]) -> Dict[str, Any]:
    """Find the evidence_comparison dict where it lives. It is not a
    direct column on ``analysis_results``, but the report-JSON form
    sometimes carries it, and some legacy rows nest it inside
    ``debug_summary`` or as ``evidence_comparison`` field. Return
    ``{}`` when nothing usable is available."""
    direct = _safe_json_load(row.get("evidence_comparison"))
    if isinstance(direct, dict) and direct:
        return direct
    debug = _safe_json_load(row.get("debug_summary"))
    if isinstance(debug, dict):
        for key in ("evidence_comparison", "comparison"):
            value = debug.get(key)
            if isinstance(value, dict) and value:
                return value
    return {}


def _reconstruct_official_sources(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    direct = _safe_json_load(row.get("official_sources"))
    if isinstance(direct, list):
        return direct
    # Some legacy rows store the list under verification_card surface.
    debug = _safe_json_load(row.get("debug_summary"))
    if isinstance(debug, dict):
        value = debug.get("official_sources")
        if isinstance(value, list):
            return value
    return []


def _reconstruct_claim_count(row: Dict[str, Any]) -> int:
    """Best-effort claim count for ``_verdict_label``. Reads
    ``claims`` / ``normalized_claims`` first, falls back to 1 when a
    bare ``claim_text`` is present, else 0."""
    for key in ("claims", "normalized_claims"):
        decoded = _safe_json_load(row.get(key))
        if isinstance(decoded, list) and decoded:
            return len(decoded)
    if row.get("claim_text"):
        return 1
    return 0


def _reconstruct_source_candidates(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    decoded = _safe_json_load(row.get("source_candidates"))
    return decoded if isinstance(decoded, list) else []


def _reconstruct_evidence_snippets(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    decoded = _safe_json_load(row.get("evidence_snippets"))
    return decoded if isinstance(decoded, list) else []


def _reconstruct_debug_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    decoded = _safe_json_load(row.get("debug_summary"))
    return decoded if isinstance(decoded, dict) else {}


def _coerce_analysis_id(row: Dict[str, Any]) -> str:
    """``analysis_results.id`` is INTEGER; reports JSON files may
    carry richer string ids. Return a stable string identifier or
    empty string when none is present."""
    for key in ("analysis_id", "id", "original_url"):
        raw = row.get(key)
        if raw not in (None, ""):
            return str(raw)
    return ""


def compute_input_hash(payload: Dict[str, Any]) -> str:
    """Deterministic SHA-256 of a normalized JSON payload. Used as
    the dedup key on the ``verdict_producer_comparisons`` table —
    re-running the tool on the same input overwrites the prior row
    via ``INSERT OR REPLACE``."""
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str,
        )
    except Exception:
        encoded = str(payload)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Producer call wrappers — each returns
# ``(label, extra, optional_score, optional_alert_level)``.
# ---------------------------------------------------------------------------


def _run_producer1(
    *, policy_confidence: Dict[str, Any],
    policy_impact: Dict[str, Any],
) -> Tuple[Optional[str], Dict[str, Any], Optional[float]]:
    """``policy_decision.make_final_decision`` — pure function."""
    extra: Dict[str, Any] = {}
    missing = []
    if not isinstance(policy_confidence, dict):
        missing.append("policy_confidence")
    if not isinstance(policy_impact, dict):
        missing.append("policy_impact")
    if missing:
        extra["missing_inputs"] = missing
        return None, extra, None
    try:
        result = make_final_decision(
            policy_confidence=policy_confidence,
            policy_impact=policy_impact,
        )
    except Exception as error:
        extra["error"] = f"{type(error).__name__}: {error}"
        return None, extra, None
    if not isinstance(result, dict):
        extra["error"] = (
            f"make_final_decision returned non-dict ({type(result).__name__})"
        )
        return None, extra, None
    label = result.get("policy_alert_level")
    score_raw = policy_confidence.get("policy_confidence_score")
    score: Optional[float]
    try:
        score = float(score_raw) if score_raw is not None else None
    except (TypeError, ValueError):
        score = None
    extra["market_signal"] = result.get("market_signal")
    extra["action_recommendation"] = result.get("action_recommendation")
    extra["decision_reasons"] = result.get("decision_reasons")
    return label, extra, score


def _run_producer2(
    *, final_decision: Dict[str, Any],
    policy_confidence: Dict[str, Any],
    policy_impact: Dict[str, Any],
    verification_card: Dict[str, Any],
    source_candidates: List[Dict[str, Any]],
    evidence_snippets: List[Dict[str, Any]],
    debug_summary: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Dict[str, Any], Optional[float]]:
    """``policy_scoring.calibrate_final_decision`` —
    returns ``(label, alert_level, extra, final_score)``. The label
    here is the calibrated ``policy_alert_level``; the alert_level
    field is the same value re-exposed (mirrors the spec field name)."""
    extra: Dict[str, Any] = {}
    missing = []
    for name, value in (
        ("final_decision", final_decision),
        ("policy_confidence", policy_confidence),
        ("policy_impact", policy_impact),
        ("verification_card", verification_card),
    ):
        if not isinstance(value, dict):
            missing.append(name)
    if missing:
        extra["missing_inputs"] = missing
        return None, None, extra, None
    try:
        calibrated, _debug = calibrate_final_decision(
            final_decision=final_decision,
            policy_confidence=policy_confidence,
            policy_impact=policy_impact,
            verification_card=verification_card,
            source_candidates=source_candidates or [],
            evidence_snippets=evidence_snippets or [],
            debug_summary=debug_summary or {},
        )
    except Exception as error:
        extra["error"] = f"{type(error).__name__}: {error}"
        return None, None, extra, None
    if not isinstance(calibrated, dict):
        extra["error"] = (
            f"calibrate_final_decision returned non-dict ({type(calibrated).__name__})"
        )
        return None, None, extra, None
    label = calibrated.get("policy_alert_level")
    alert_level = label  # same value, mirroring the spec field name
    try:
        score = float(calibrated.get("final_score")) if calibrated.get(
            "final_score"
        ) is not None else None
    except (TypeError, ValueError):
        score = None
    extra["final_score"] = calibrated.get("final_score")
    extra["evidence_quality_score"] = calibrated.get("evidence_quality_score")
    extra["source_trust_score"] = calibrated.get("source_trust_score")
    extra["calibration_reasons"] = calibrated.get("calibration_reasons")
    return label, alert_level, extra, score


def _run_producer3(
    *, policy_confidence: Dict[str, Any],
    evidence_comparison: Dict[str, Any],
    official_sources: List[Dict[str, Any]],
    evidence_snippets: List[Dict[str, Any]],
    contradiction_summary: Dict[str, Any],
    bias_framing_summary: Dict[str, Any],
    claim_count: int,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """``verification_card._verdict_label`` — pure function."""
    extra: Dict[str, Any] = {}
    missing = []
    if not isinstance(policy_confidence, dict):
        missing.append("policy_confidence")
    if not isinstance(evidence_comparison, dict):
        # _verdict_label tolerates an empty dict but a non-dict
        # would raise; track it as a soft warning.
        evidence_comparison = {}
        extra.setdefault("warnings", []).append(
            "evidence_comparison missing — used empty dict"
        )
    if missing:
        extra["missing_inputs"] = missing
        return None, extra
    try:
        label = _verdict_label(
            policy_confidence,
            evidence_comparison,
            official_sources or [],
            evidence_snippets=evidence_snippets or [],
            contradiction_summary=contradiction_summary or {},
            bias_framing_summary=bias_framing_summary or {},
            claim_count=int(claim_count or 0),
        )
    except Exception as error:
        extra["error"] = f"{type(error).__name__}: {error}"
        return None, extra
    return label, extra


# ---------------------------------------------------------------------------
# Cross-producer analysis
# ---------------------------------------------------------------------------


def _rank(label: Optional[str]) -> Optional[int]:
    if label is None:
        return None
    return LABEL_SEVERITY_RANK.get(label)


def _ranks_agree(a: Optional[str], b: Optional[str]) -> bool:
    """Two labels agree iff both are known and map to the same
    severity rank. When either side is ``None`` (errored / missing)
    or unmapped, the comparison is **False** by definition — we never
    silently treat a missing reading as agreement."""
    ra = _rank(a)
    rb = _rank(b)
    if ra is None or rb is None:
        return False
    return ra == rb


def _disagreement_pattern(
    p1: Optional[str], p2: Optional[str], p3: Optional[str],
) -> str:
    return (
        f"P1={p1 if p1 is not None else 'NONE'},"
        f"P2={p2 if p2 is not None else 'NONE'},"
        f"P3={p3 if p3 is not None else 'NONE'}"
    )


def _most_conservative(
    p1: Optional[str], p2: Optional[str], p3: Optional[str],
) -> Optional[str]:
    """Return the label among the three that maps to the lowest
    severity rank. Producers with unmapped or None labels are skipped.
    Ties are broken by producer order (P1 wins, then P2)."""
    best_rank: Optional[int] = None
    best_label: Optional[str] = None
    for candidate in (p1, p2, p3):
        rank = _rank(candidate)
        if rank is None:
            continue
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_label = candidate
    return best_label


# ---------------------------------------------------------------------------
# Public entry point — compare one analysis row
# ---------------------------------------------------------------------------


def compare_producers_for_analysis(
    analysis_row: Dict[str, Any],
    source: str = "sqlite",
) -> ProducerComparison:
    """Re-run the three producers against the reconstructed inputs
    on ``analysis_row`` and return a ``ProducerComparison``.

    Never raises. Producers whose inputs are unavailable or whose
    invocation raises are recorded with ``label=None`` and a
    diagnostic note in the matching ``producerN_extra`` dict. The
    function does not mutate ``analysis_row``."""
    if not isinstance(analysis_row, dict):
        analysis_row = {}
    if not isinstance(source, str) or not source.strip():
        source = "sqlite"

    policy_confidence = _reconstruct_policy_confidence(analysis_row)
    policy_impact = _reconstruct_policy_impact(analysis_row)
    verification_card_dict = _reconstruct_verification_card(analysis_row)
    evidence_comparison = _reconstruct_evidence_comparison(analysis_row)
    official_sources = _reconstruct_official_sources(analysis_row)
    evidence_snippets = _reconstruct_evidence_snippets(analysis_row)
    source_candidates = _reconstruct_source_candidates(analysis_row)
    debug_summary = _reconstruct_debug_summary(analysis_row)
    contradiction_summary = (
        verification_card_dict.get("contradiction_summary") or {}
    )
    bias_framing_summary = (
        verification_card_dict.get("bias_framing_summary") or {}
    )
    claim_count = _reconstruct_claim_count(analysis_row)

    # ------------------------------ P1 ------------------------------
    p1_label, p1_extra, p1_score = _run_producer1(
        policy_confidence=policy_confidence,
        policy_impact=policy_impact,
    )

    # ------------------------------ P2 ------------------------------
    # The calibrator needs a starting final_decision dict. Prefer the
    # exact output P1 just produced (this mirrors main.py: P1 → P2);
    # fall back to a minimal shape so the calibrator can still try.
    if p1_label is not None:
        starting_decision = {
            "policy_alert_level": p1_label,
            "market_signal": p1_extra.get("market_signal") or [],
            "decision_reasons": p1_extra.get("decision_reasons") or [],
        }
    else:
        starting_decision = {
            "policy_alert_level": analysis_row.get("policy_alert_level"),
            "market_signal": _safe_json_load(
                analysis_row.get("market_signal")
            ) or [],
            "decision_reasons": [],
        }
    p2_label, p2_alert_level, p2_extra, p2_score = _run_producer2(
        final_decision=starting_decision,
        policy_confidence=policy_confidence,
        policy_impact=policy_impact,
        verification_card=verification_card_dict,
        source_candidates=source_candidates,
        evidence_snippets=evidence_snippets,
        debug_summary=debug_summary,
    )

    # ------------------------------ P3 ------------------------------
    p3_label, p3_extra = _run_producer3(
        policy_confidence=policy_confidence,
        evidence_comparison=evidence_comparison,
        official_sources=official_sources,
        evidence_snippets=evidence_snippets,
        contradiction_summary=contradiction_summary,
        bias_framing_summary=bias_framing_summary,
        claim_count=claim_count,
    )

    # ---------------------- agreement analysis ----------------------
    p1_p2_agree = _ranks_agree(p1_label, p2_label)
    p1_p3_agree = _ranks_agree(p1_label, p3_label)
    p2_p3_agree = _ranks_agree(p2_label, p3_label)
    # all_three_agree requires every label to be present AND to share
    # the same rank. Missing readings never count as agreement.
    if p1_label is None or p2_label is None or p3_label is None:
        all_three_agree = False
    else:
        ranks = {_rank(p1_label), _rank(p2_label), _rank(p3_label)}
        all_three_agree = len(ranks) == 1 and None not in ranks

    pattern = _disagreement_pattern(p1_label, p2_label, p3_label)
    most_conservative = _most_conservative(p1_label, p2_label, p3_label)

    # ----------------------- identity + hash ------------------------
    analysis_id = _coerce_analysis_id(analysis_row)
    hash_payload = {
        "analysis_id": analysis_id,
        "source": source,
        "policy_confidence": policy_confidence,
        "policy_impact": policy_impact,
        "verification_card": verification_card_dict,
        "evidence_comparison": evidence_comparison,
        "official_sources": official_sources,
        "evidence_snippets": evidence_snippets,
        "source_candidates": source_candidates,
        "debug_summary": debug_summary,
        "claim_count": claim_count,
    }
    input_hash = compute_input_hash(hash_payload)

    return ProducerComparison(
        analysis_id=analysis_id,
        source=source,
        input_hash=input_hash,
        producer1_label=p1_label,
        producer1_score=p1_score,
        producer1_extra=p1_extra,
        producer2_label=p2_label,
        producer2_alert_level=p2_alert_level,
        producer2_score=p2_score,
        producer2_extra=p2_extra,
        producer3_label=p3_label,
        producer3_extra=p3_extra,
        all_three_agree=bool(all_three_agree),
        p1_p2_agree=bool(p1_p2_agree),
        p1_p3_agree=bool(p1_p3_agree),
        p2_p3_agree=bool(p2_p3_agree),
        disagreement_pattern=pattern,
        most_conservative_label=most_conservative,
        comparison_timestamp=datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        notes=NOTES_OPERATOR_REVIEW,
        truth_claim=False,
        operator_review_required=True,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _is_error_extra(extra: Any) -> bool:
    """An ``extra`` dict (or its JSON string form) signals an error
    when it carries an ``error`` key. ``missing_inputs`` alone is
    *not* counted as an errored run — the producer simply could not
    be called."""
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (TypeError, ValueError):
            return False
    if not isinstance(extra, dict):
        return False
    return bool(extra.get("error"))


def _get_label(comparison: Any, attr: str) -> Optional[str]:
    """Read a ``producerN_label`` from either a dataclass or a dict
    (the CLI / DB round-trip passes dicts)."""
    if isinstance(comparison, ProducerComparison):
        return getattr(comparison, attr)
    if isinstance(comparison, dict):
        return comparison.get(attr)
    return None


def _get_extra(comparison: Any, attr: str) -> Any:
    if isinstance(comparison, ProducerComparison):
        return getattr(comparison, attr)
    if isinstance(comparison, dict):
        return comparison.get(attr)
    return None


def _get_bool(comparison: Any, attr: str) -> bool:
    if isinstance(comparison, ProducerComparison):
        return bool(getattr(comparison, attr))
    if isinstance(comparison, dict):
        return bool(comparison.get(attr))
    return False


def _get_str(comparison: Any, attr: str) -> str:
    if isinstance(comparison, ProducerComparison):
        return getattr(comparison, attr) or ""
    if isinstance(comparison, dict):
        return comparison.get(attr) or ""
    return ""


def compute_disagreement_summary(
    comparisons: Iterable[Any],
) -> Dict[str, Any]:
    """Aggregate statistics across many comparisons (each may be a
    ``ProducerComparison`` or a dict from the DB round-trip).

    Returns a stable dict with:

        * ``total``
        * ``all_three_agree_count`` + ``all_three_agree_percent``
        * ``at_least_one_disagreement_count`` + ``...percent``
        * ``pairwise_disagreement_counts`` (p1_vs_p2 / p1_vs_p3 /
          p2_vs_p3) with both raw and percent
        * ``disagreement_pattern_histogram`` (Counter -> dict)
        * ``producer_label_distribution`` (per-producer Counter
          -> dict)
        * ``errored_producer_runs_count``
    """
    comparisons_list = list(comparisons or [])
    total = len(comparisons_list)
    all_three = 0
    p1_p2_disagree = 0
    p1_p3_disagree = 0
    p2_p3_disagree = 0
    pattern_counter: Counter = Counter()
    p1_labels: Counter = Counter()
    p2_labels: Counter = Counter()
    p3_labels: Counter = Counter()
    errored_runs = 0

    for c in comparisons_list:
        if _get_bool(c, "all_three_agree"):
            all_three += 1
        if not _get_bool(c, "p1_p2_agree"):
            p1_p2_disagree += 1
        if not _get_bool(c, "p1_p3_agree"):
            p1_p3_disagree += 1
        if not _get_bool(c, "p2_p3_agree"):
            p2_p3_disagree += 1
        pattern = _get_str(c, "disagreement_pattern")
        if pattern:
            pattern_counter[pattern] += 1
        p1_lab = _get_label(c, "producer1_label")
        p2_lab = _get_label(c, "producer2_label")
        p3_lab = _get_label(c, "producer3_label")
        if p1_lab is not None:
            p1_labels[str(p1_lab)] += 1
        else:
            p1_labels["NONE"] += 1
        if p2_lab is not None:
            p2_labels[str(p2_lab)] += 1
        else:
            p2_labels["NONE"] += 1
        if p3_lab is not None:
            p3_labels[str(p3_lab)] += 1
        else:
            p3_labels["NONE"] += 1
        if (
            _is_error_extra(_get_extra(c, "producer1_extra"))
            or _is_error_extra(_get_extra(c, "producer2_extra"))
            or _is_error_extra(_get_extra(c, "producer3_extra"))
        ):
            errored_runs += 1

    def _pct(numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round((numerator / denominator) * 100.0, 2)

    at_least_one = total - all_three
    return {
        "total": total,
        "all_three_agree_count": all_three,
        "all_three_agree_percent": _pct(all_three, total),
        "at_least_one_disagreement_count": at_least_one,
        "at_least_one_disagreement_percent": _pct(at_least_one, total),
        "pairwise_disagreement_counts": {
            "p1_vs_p2": p1_p2_disagree,
            "p1_vs_p3": p1_p3_disagree,
            "p2_vs_p3": p2_p3_disagree,
        },
        "pairwise_disagreement_percent": {
            "p1_vs_p2": _pct(p1_p2_disagree, total),
            "p1_vs_p3": _pct(p1_p3_disagree, total),
            "p2_vs_p3": _pct(p2_p3_disagree, total),
        },
        "disagreement_pattern_histogram": dict(pattern_counter),
        "producer_label_distribution": {
            "producer1": dict(p1_labels),
            "producer2": dict(p2_labels),
            "producer3": dict(p3_labels),
        },
        "errored_producer_runs_count": errored_runs,
    }
