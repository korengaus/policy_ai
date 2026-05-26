"""Phase 2 M11.0b: read-only diagnostic for ``verification_card._verdict_label``.

Documents every branch in ``_verdict_label`` (as it currently lives in
``verification_card.py``), reconstructs the inputs that fed each
stored ``analysis_results.verdict_label`` value, attributes each
stored label to the most likely branch, and flags rows whose stored
``draft_verified`` label was produced from weak-evidence inputs (no
official sources, ``policy_confidence_score`` ≤ 30,
``verification_strength == "none"``, or an ``evidence_summary`` that
literally says the official-source comparison failed).

Why this milestone exists
-------------------------

M11.0a (verdict_producer_comparison) measured three-producer
disagreement. While running it on real DB rows, an even more urgent
pattern surfaced: at least eight stored rows (IDs 58, 65, 82, 83, 87,
95, 104, 105) carry::

    policy_alert_level=LOW
    policy_confidence_score=10
    verification_strength=none
    verdict_label=draft_verified

That directly violates the project's conservative-under-weak-evidence
invariant. Inspection of ``verification_card._verdict_label`` lines
465-466 shows::

    if claim_count and direct_support_count >= claim_count:
        return "draft_verified"

That branch returns the strongest label based **only** on counting
``evidence_snippets`` whose ``evidence_type == "direct_support"``,
without checking ``official_sources``, ``policy_confidence_score``,
or ``verification_strength``. The strict branch at lines 476-479
requires ``confidence_score >= 85`` AND
``verification_level == "strong_official_match"``; the line 465 path
silently bypasses those gates.

This module collects evidence about how often that branch fires in
production. **It does not modify any verdict logic.** The actual fix
will land in M11.0c, driven by what we learn here.

Hard contract
-------------

    * Never invoked automatically. ``main.py`` / ``api_server.py`` /
      ``scheduler.py`` do not import this module.
    * Never mutates ``verification_card.py``, ``_verdict_label``, or
      any verdict-producing function.
    * Never writes to the database (the CLI handles persistence).
    * Never modifies ``analysis_results`` data.
    * ``truth_claim`` is forced to ``False`` on every
      ``VerdictLabelAttribution``.
    * ``operator_review_required`` is forced to ``True`` on every
      ``VerdictLabelAttribution``.
    * No ``requests`` / ``httpx`` / ``urllib.request`` / ``socket``
      imports.
    * No ``openai`` / ``anthropic`` / ``playwright`` /
      ``browser_use`` / ``openclaw`` / ``selenium`` imports.

Risk classification rationale
-----------------------------

Each entry in ``VERDICT_LABEL_BRANCHES`` carries a
``risk_classification`` value taken from one of five buckets. The
buckets are deliberately conservative and exist so the operator can
rank-order which branches need scrutiny in M11.0c:

    * ``conservative_safe`` — output is a non-verified / cautious
      label (e.g. ``draft_disputed``, ``draft_needs_review``,
      ``draft_needs_context``). These are not suspected of producing
      false positives.

    * ``verified_with_strict_checks`` — output is ``draft_verified``
      AND the branch's triggers include the strong-evidence gates
      (lines 476-477: ``confidence_score >= 85`` AND
      ``verification_level == "strong_official_match"``). Surface-
      level safe.

    * ``verified_without_strict_checks`` — output is ``draft_verified``
      WITHOUT enforcing the strong-evidence gates. As of M11.0b
      exactly one branch fell in this bucket: B08 at the original
      lines 465-466. **This was the suspected bug surface M11.0a
      uncovered.** In M11.0c the B08 branch was patched to add
      ``confidence_score >= 60`` AND
      ``verification_strength in {medium, high}`` gates, so it is
      now classified ``verified_with_strict_checks``. As of M11.0c
      NO branch in ``_verdict_label`` falls in
      ``verified_without_strict_checks``; the bucket constant is
      kept exposed so any future regression that drops the gates is
      surfaced immediately.

    * ``likely_true`` — output is ``draft_likely_true`` (line 478-479).
      Carries explicit ``confidence_score >= 60`` + verification-level
      gates, so it is less risky than ``verified_without_strict_checks``
      but still worth tracking.

    * ``fallback_unverified`` — terminal conservative fallbacks
      (lines 475 and 482, both returning ``draft_unverified``).

These labels are not graded by severity; they are descriptive
buckets that group branches with similar safety properties. Operator
validation is still required before any consolidation decision in
M11.0c.

Weak-evidence signals
---------------------

A stored row whose ``verdict_label == "draft_verified"`` is flagged
``is_weak_evidence_verified=True`` when any of the following signals
fires. They are heuristics — none of them, in isolation, proves the
label is wrong, but together they make the row a high-priority
operator-review candidate:

    * ``no_official_sources`` — the reconstructed
      ``official_sources`` list is empty (the strict branch at
      lines 474-475 should have returned ``draft_unverified`` here;
      if we still see ``draft_verified``, an earlier branch
      short-circuited it).

    * ``score_leq_30`` — ``policy_confidence_score <= 30``. The
      strict ``draft_verified`` branch needs ``>= 85``; anything in
      the LOW band has no business being labeled verified.

    * ``strength_none`` — ``verification_strength == "none"``. The
      same logic: ``"none"`` strength is exactly the case the
      conservative invariant is supposed to protect.

    * ``evidence_summary_says_failure`` — the stored
      ``evidence_summary`` text contains failure phrases such as
      ``비교할 수 없`` (cannot compare) or ``접근이 실패`` (access
      failed). These are produced by the pipeline when the official
      search/document fetch failed; a verified label on such a row
      is contradictory on its face.

Public surface (stable, pinned by tests)
----------------------------------------

    VERDICT_LABEL_BRANCHES
    RISK_CLASSIFICATIONS
    WEAK_EVIDENCE_SUMMARY_PHRASES
    NOTES_DIAGNOSTIC
    VerdictLabelAttribution                                 (dataclass)
    attribution_to_dict(attribution) -> dict
    compute_weak_evidence_signals(row) -> list[str]
    attribute_branch_for_row(row) -> VerdictLabelAttribution
    compute_branch_summary(attributions) -> dict
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from structured_logging import get_logger


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Branch catalogue
# ---------------------------------------------------------------------------


RISK_CONSERVATIVE_SAFE = "conservative_safe"
RISK_VERIFIED_STRICT = "verified_with_strict_checks"
RISK_VERIFIED_LOOSE = "verified_without_strict_checks"
RISK_LIKELY_TRUE = "likely_true"
RISK_FALLBACK_UNVERIFIED = "fallback_unverified"

RISK_CLASSIFICATIONS = (
    RISK_CONSERVATIVE_SAFE,
    RISK_VERIFIED_STRICT,
    RISK_VERIFIED_LOOSE,
    RISK_LIKELY_TRUE,
    RISK_FALLBACK_UNVERIFIED,
)


# Every branch of ``verification_card._verdict_label`` (function spans
# lines 414-482 as of M11.0b). The ordering MATCHES the source-order
# of the ``return`` statements — attribution walks this list and
# stops at the first branch whose triggers match and whose label
# matches the stored label. Line numbers are approximate and meant
# to help an operator reading the source side-by-side.
VERDICT_LABEL_BRANCHES: List[Dict[str, str]] = [
    {
        "branch_id": "B01_conflict_or_official_conflict",
        "line_range": "432-433",
        "output_label": "draft_disputed",
        "trigger_summary": (
            "evidence_comparison.conflict_signals OR "
            "evidence_comparison.semantic_conflict_signals OR "
            "comparison_status == 'official_conflict_possible'"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B02_high_framing_with_confirmed",
        "line_range": "448-449",
        "output_label": "draft_high_risk_review",
        "trigger_summary": (
            "bias_framing_summary.high_framing_count > 0 AND "
            "contradiction.(confirmed_count OR likely_count) > 0"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B03_high_framing_only",
        "line_range": "450-451",
        "output_label": "draft_needs_review",
        "trigger_summary": "bias_framing_summary.high_framing_count > 0",
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B04_confirmed_contradiction",
        "line_range": "452-453",
        "output_label": "draft_disputed",
        "trigger_summary": (
            "contradiction.confirmed_contradiction_count > 0 OR "
            "contradiction.likely_contradiction_count > 0"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B05_possible_contradiction",
        "line_range": "454-455",
        "output_label": "draft_needs_review",
        "trigger_summary": "contradiction.possible_contradiction_count > 0",
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B06_needs_official_confirmation_via_contradiction",
        "line_range": "456-457",
        "output_label": "draft_needs_official_confirmation",
        "trigger_summary": (
            "claim_count > 0 AND "
            "contradiction.needs_official_confirmation_count >= "
            "max(1, claim_count // 2)"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B07_insufficient_via_contradiction",
        "line_range": "458-459",
        "output_label": "draft_needs_context",
        "trigger_summary": (
            "claim_count > 0 AND "
            "contradiction.insufficient_evidence_count >= "
            "max(1, claim_count // 2)"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        # M11.0c: B08 was the suspected bug surface uncovered by M11.0a
        # (28 production rows attributed, 21 weak-evidence verified).
        # The branch now enforces score and verification_strength gates
        # analogous to B13's intent, so it has been re-classified as
        # ``verified_with_strict_checks``. As of M11.0c, NO branch in
        # _verdict_label is in the ``verified_without_strict_checks``
        # bucket — the bucket constant remains exposed so the catalog
        # stays back-compatible and any future regression that drops
        # the gates can be flagged immediately.
        "branch_id": "B08_direct_support_only",
        "line_range": "478-484",
        "output_label": "draft_verified",
        "trigger_summary": (
            "claim_count > 0 AND direct_support_count >= claim_count "
            "AND confidence_score >= 60 AND "
            "verification_strength in {medium, high}"
        ),
        "risk_classification": RISK_VERIFIED_STRICT,
    },
    {
        "branch_id": "B09_official_reference_no_direct",
        "line_range": "467-468",
        "output_label": "draft_needs_official_confirmation",
        "trigger_summary": (
            "official_reference_count > 0 AND direct_support_count == 0"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B10_insufficient_snippets",
        "line_range": "469-470",
        "output_label": "draft_needs_context",
        "trigger_summary": "insufficient_count > 0",
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B11_excluded_non_policy_page",
        "line_range": "472-473",
        "output_label": "draft_needs_context",
        "trigger_summary": (
            "comparison_status == 'official_evidence_missing' AND "
            "verification_level == 'excluded_non_policy_page'"
        ),
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B12_no_official_or_strength_none",
        "line_range": "474-475",
        "output_label": "draft_unverified",
        "trigger_summary": (
            "official_sources empty OR verification_strength == 'none'"
        ),
        "risk_classification": RISK_FALLBACK_UNVERIFIED,
    },
    {
        "branch_id": "B13_strong_confidence_verified",
        "line_range": "476-477",
        "output_label": "draft_verified",
        "trigger_summary": (
            "confidence_score >= 85 AND "
            "verification_level == 'strong_official_match'"
        ),
        "risk_classification": RISK_VERIFIED_STRICT,
    },
    {
        "branch_id": "B14_medium_confidence_likely",
        "line_range": "478-479",
        "output_label": "draft_likely_true",
        "trigger_summary": (
            "confidence_score >= 60 AND "
            "verification_level in {'strong_official_match', "
            "'medium_official_match'}"
        ),
        "risk_classification": RISK_LIKELY_TRUE,
    },
    {
        "branch_id": "B15_mid_confidence_needs_context",
        "line_range": "480-481",
        "output_label": "draft_needs_context",
        "trigger_summary": "confidence_score >= 35",
        "risk_classification": RISK_CONSERVATIVE_SAFE,
    },
    {
        "branch_id": "B16_terminal_fallback",
        "line_range": "482",
        "output_label": "draft_unverified",
        "trigger_summary": "no earlier branch matched (terminal fallback)",
        "risk_classification": RISK_FALLBACK_UNVERIFIED,
    },
]


NOTES_DIAGNOSTIC = (
    "verdict label branch attribution only — does not modify "
    "_verdict_label or any verdict logic; operator review required"
)


# Korean failure phrases the pipeline emits in ``evidence_summary``
# when the official search / document fetch fails. Operators can
# extend the tuple; the heuristic stays case-sensitive because the
# phrases are literal Korean strings.
WEAK_EVIDENCE_SUMMARY_PHRASES = (
    "비교할 수 없",
    "접근이 실패",
    "공식 검색",
    "공식 상세문서",
    "정보가 부족",
)


# audit §1.5 #5 (2026-05-26): B08 diagnostic threshold. Used by the
# retrospective verdict-label diagnostic script, NOT the live pipeline.
# See docs/MAGIC_THRESHOLDS.md §10 + docs/VERDICT_LABEL_DIAGNOSTIC.md.
WEAK_EVIDENCE_SCORE_THRESHOLD = 30


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class VerdictLabelAttribution:
    """Stable wire shape consumed by tests, the CLI, and the DB save
    helper. ``reconstructed_*`` fields capture the inputs that
    *would* have fed ``_verdict_label`` on a re-run; the
    ``attributed_*`` fields explain which documented branch most
    likely produced the stored label.
    """
    analysis_id: str
    stored_verdict_label: Optional[str] = None
    stored_verdict_confidence: Optional[int] = None
    stored_policy_alert_level: Optional[str] = None
    stored_policy_confidence_score: Optional[int] = None
    stored_verification_strength: Optional[str] = None
    stored_claim_text: Optional[str] = None
    stored_evidence_summary: Optional[str] = None

    reconstructed_claim_count: int = 0
    reconstructed_direct_support_count: int = 0
    reconstructed_official_reference_count: int = 0
    reconstructed_insufficient_count: int = 0
    reconstructed_confirmed_count: int = 0
    reconstructed_possible_count: int = 0
    reconstructed_high_framing_count: int = 0
    reconstructed_official_confirmation_count: int = 0
    reconstructed_insufficient_claim_count: int = 0
    reconstructed_has_conflict: bool = False
    reconstructed_comparison_status: Optional[str] = None
    reconstructed_verification_level: Optional[str] = None
    reconstructed_official_sources_count: int = 0

    attributed_branch_id: Optional[str] = None
    attribution_confidence: str = "unknown"
    attribution_reason: str = ""

    is_weak_evidence_verified: bool = False
    weak_evidence_signals: List[str] = field(default_factory=list)

    diagnostic_timestamp: str = ""
    notes: str = NOTES_DIAGNOSTIC
    # Always False. The diagnostic never asserts truth — pinned by tests.
    truth_claim: bool = False
    # Always True. Branch-attribution rows always require review —
    # pinned by tests.
    operator_review_required: bool = True


def attribution_to_dict(attribution: VerdictLabelAttribution) -> Dict[str, Any]:
    """Serialize an attribution to a flat dict matching the schema
    ``database.save_verdict_label_attribution`` expects. The
    ``reconstructed_*`` fields are folded into a single JSON-encoded
    ``reconstructed_inputs`` string for the TEXT column.
    """
    payload = asdict(attribution)
    payload["truth_claim"] = False
    payload["operator_review_required"] = True
    payload["notes"] = payload.get("notes") or NOTES_DIAGNOSTIC

    reconstructed: Dict[str, Any] = {}
    for key in list(payload.keys()):
        if key.startswith("reconstructed_"):
            reconstructed[key] = payload.pop(key)
    payload["reconstructed_inputs"] = json.dumps(
        reconstructed, ensure_ascii=False, sort_keys=True,
    )

    signals = payload.get("weak_evidence_signals") or []
    if not isinstance(signals, str):
        payload["weak_evidence_signals"] = json.dumps(
            list(signals), ensure_ascii=False,
        )
    return payload


# ---------------------------------------------------------------------------
# Input reconstruction
# ---------------------------------------------------------------------------


def _safe_json_load(value: Any) -> Any:
    """Decode a JSON-encoded TEXT column. Returns the input unchanged
    on a parse failure; returns ``None`` for ``None`` input."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped[0] in "[{":
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


def _count_evidence_type(
    snippets: Any, evidence_type: str,
) -> int:
    if not isinstance(snippets, list):
        return 0
    n = 0
    for item in snippets:
        if isinstance(item, dict) and item.get("evidence_type") == evidence_type:
            n += 1
    return n


def _reconstruct_claim_count(row: Dict[str, Any]) -> int:
    """Best-effort claim count mirroring ``main.py``::

        claim_count=len(claim_list or [claim_text])
    """
    for key in ("claims", "normalized_claims"):
        decoded = _safe_json_load(row.get(key))
        if isinstance(decoded, list) and decoded:
            return len(decoded)
    if row.get("claim_text"):
        return 1
    return 0


def _reconstruct_evidence_comparison(row: Dict[str, Any]) -> Dict[str, Any]:
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


def _reconstruct_official_sources_count(row: Dict[str, Any]) -> int:
    direct = _safe_json_load(row.get("official_sources"))
    if isinstance(direct, list):
        return len(direct)
    debug = _safe_json_load(row.get("debug_summary"))
    if isinstance(debug, dict):
        value = debug.get("official_sources")
        if isinstance(value, list):
            return len(value)
    # Fall back to evidence_sources count when present — it's not the
    # same set as official_sources, but it's a useful lower bound for
    # rows where official_sources was not preserved on disk.
    sources = _safe_json_load(row.get("evidence_sources"))
    if isinstance(sources, list):
        return len(sources)
    return 0


def _row_evidence_snippets(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    decoded = _safe_json_load(row.get("evidence_snippets"))
    return decoded if isinstance(decoded, list) else []


def _row_contradiction(row: Dict[str, Any]) -> Dict[str, Any]:
    decoded = _safe_json_load(row.get("contradiction_summary"))
    return decoded if isinstance(decoded, dict) else {}


def _row_bias(row: Dict[str, Any]) -> Dict[str, Any]:
    decoded = _safe_json_load(row.get("bias_framing_summary"))
    return decoded if isinstance(decoded, dict) else {}


def _coerce_analysis_id(row: Dict[str, Any]) -> str:
    for key in ("analysis_id", "id", "original_url"):
        raw = row.get(key)
        if raw not in (None, ""):
            return str(raw)
    return ""


# ---------------------------------------------------------------------------
# Branch trigger checks
# ---------------------------------------------------------------------------


def _branch_trigger_matches(
    branch_id: str,
    *,
    claim_count: int,
    direct_support_count: int,
    official_reference_count: int,
    insufficient_count: int,
    confirmed_count: int,
    possible_count: int,
    high_framing_count: int,
    official_confirmation_count: int,
    insufficient_claim_count: int,
    has_conflict: bool,
    comparison_status: Optional[str],
    verification_level: Optional[str],
    official_sources_count: int,
    verification_strength: Optional[str],
    confidence_score: int,
) -> bool:
    """Replicate, IN A NON-MUTATING WAY, each branch's trigger
    predicate. Mirrors the source ordering at
    ``verification_card.py:432-482``. We do NOT re-run the function;
    we only evaluate the trigger condition independently to check
    "could this branch have fired against these inputs?".
    """
    if branch_id == "B01_conflict_or_official_conflict":
        return bool(
            has_conflict or comparison_status == "official_conflict_possible"
        )
    if branch_id == "B02_high_framing_with_confirmed":
        return bool(high_framing_count and confirmed_count)
    if branch_id == "B03_high_framing_only":
        return bool(high_framing_count)
    if branch_id == "B04_confirmed_contradiction":
        return bool(confirmed_count)
    if branch_id == "B05_possible_contradiction":
        return bool(possible_count)
    if branch_id == "B06_needs_official_confirmation_via_contradiction":
        return bool(
            claim_count
            and official_confirmation_count >= max(1, claim_count // 2)
        )
    if branch_id == "B07_insufficient_via_contradiction":
        return bool(
            claim_count
            and insufficient_claim_count >= max(1, claim_count // 2)
        )
    if branch_id == "B08_direct_support_only":
        # M11.0c gates: confidence_score >= 60 AND verification_strength
        # in {medium, high} (the strong-strength set documented in
        # verification_card._STRONG_VERIFICATION_STRENGTHS). Pure
        # mirror of the source condition.
        return bool(
            claim_count
            and direct_support_count >= claim_count
            and confidence_score >= 60
            and verification_strength in {"medium", "high"}
        )
    if branch_id == "B09_official_reference_no_direct":
        return bool(
            official_reference_count > 0 and direct_support_count == 0
        )
    if branch_id == "B10_insufficient_snippets":
        return bool(insufficient_count > 0)
    if branch_id == "B11_excluded_non_policy_page":
        return bool(
            comparison_status == "official_evidence_missing"
            and verification_level == "excluded_non_policy_page"
        )
    if branch_id == "B12_no_official_or_strength_none":
        return bool(
            official_sources_count == 0 or verification_strength == "none"
        )
    if branch_id == "B13_strong_confidence_verified":
        return bool(
            confidence_score >= 85
            and verification_level == "strong_official_match"
        )
    if branch_id == "B14_medium_confidence_likely":
        return bool(
            confidence_score >= 60
            and verification_level in {
                "strong_official_match", "medium_official_match",
            }
        )
    if branch_id == "B15_mid_confidence_needs_context":
        return bool(confidence_score >= 35)
    if branch_id == "B16_terminal_fallback":
        return True  # terminal — always reachable when nothing else matched
    return False


# ---------------------------------------------------------------------------
# Weak-evidence signals
# ---------------------------------------------------------------------------


def compute_weak_evidence_signals(row: Dict[str, Any]) -> List[str]:
    """Return the list of weak-evidence signal tags for one stored
    analysis row. Order-stable and deduplicated. See module docstring
    for the rationale behind each signal."""
    if not isinstance(row, dict):
        return []
    signals: List[str] = []

    official_count = _reconstruct_official_sources_count(row)
    if official_count == 0:
        signals.append("no_official_sources")

    score = _coerce_int(row.get("policy_confidence_score"), default=-1)
    if 0 <= score <= WEAK_EVIDENCE_SCORE_THRESHOLD:
        signals.append("score_leq_30")

    if row.get("verification_strength") == "none":
        signals.append("strength_none")

    summary = row.get("evidence_summary")
    if isinstance(summary, str) and summary:
        for phrase in WEAK_EVIDENCE_SUMMARY_PHRASES:
            if phrase in summary:
                signals.append("evidence_summary_says_failure")
                break

    return signals


# ---------------------------------------------------------------------------
# Branch attribution
# ---------------------------------------------------------------------------


def _attribute(
    stored_label: Optional[str],
    triggers: Dict[str, Any],
) -> Dict[str, Any]:
    """Walk ``VERDICT_LABEL_BRANCHES`` in source order and pick the
    branch that BOTH (a) has its trigger predicate satisfied by the
    reconstructed inputs AND (b) emits the same label as
    ``stored_label``. Returns a dict with ``attributed_branch_id``,
    ``attribution_confidence``, ``attribution_reason``.

    Confidence buckets:

        * ``high`` — exactly one branch matched.
        * ``medium`` — multiple branches matched; first source-order
          match wins (matches ``_verdict_label``'s own short-circuit
          semantics).
        * ``low`` — no triggered branch emits ``stored_label`` but at
          least one branch with the matching label exists (so the
          input reconstruction is probably incomplete).
        * ``unknown`` — ``stored_label`` is not a label any branch in
          the catalogue emits, or ``stored_label`` is None.
    """
    if not stored_label:
        return {
            "attributed_branch_id": None,
            "attribution_confidence": "unknown",
            "attribution_reason": "stored_verdict_label is empty",
        }
    label_branches = [
        b for b in VERDICT_LABEL_BRANCHES
        if b["output_label"] == stored_label
    ]
    if not label_branches:
        return {
            "attributed_branch_id": None,
            "attribution_confidence": "unknown",
            "attribution_reason": (
                f"no branch in catalogue emits {stored_label!r}"
            ),
        }
    triggered: List[Dict[str, str]] = []
    for branch in VERDICT_LABEL_BRANCHES:
        if _branch_trigger_matches(branch["branch_id"], **triggers):
            triggered.append(branch)

    label_triggered = [
        b for b in triggered if b["output_label"] == stored_label
    ]
    if not label_triggered:
        # No triggered branch produces this label — the inputs we
        # reconstructed don't satisfy any matching branch. Surface
        # the first label-only candidate so the operator has
        # something to investigate.
        return {
            "attributed_branch_id": label_branches[0]["branch_id"],
            "attribution_confidence": "low",
            "attribution_reason": (
                "no branch's trigger conditions matched the "
                "reconstructed inputs; fell back to the first branch "
                f"in source order that emits {stored_label!r} — "
                "input reconstruction is probably incomplete"
            ),
        }

    # The first triggered branch is the one ``_verdict_label`` itself
    # would have hit, because the function returns at the first
    # match. Pick it.
    chosen = triggered[0]
    if chosen["output_label"] != stored_label:
        # Earlier branches fired first but produced a different label;
        # the stored label came from a later branch only because the
        # earlier one didn't fire in the real run. The reconstruction
        # is incomplete — surface the matching branch with medium
        # confidence.
        return {
            "attributed_branch_id": label_triggered[0]["branch_id"],
            "attribution_confidence": "medium",
            "attribution_reason": (
                "first triggered branch "
                f"({chosen['branch_id']!r}) emits "
                f"{chosen['output_label']!r}, not stored "
                f"{stored_label!r}; chose the earliest label-matching "
                "triggered branch instead"
            ),
        }
    confidence = "high" if len(label_triggered) == 1 else "medium"
    if confidence == "medium":
        ids = ", ".join(b["branch_id"] for b in label_triggered)
        reason = (
            "triggers match and label matches; multiple branches "
            f"could explain the stored label ({ids}); picked the "
            "first in source order"
        )
    else:
        reason = (
            "triggers match and label matches; unambiguous "
            "attribution"
        )
    return {
        "attributed_branch_id": chosen["branch_id"],
        "attribution_confidence": confidence,
        "attribution_reason": reason,
    }


def attribute_branch_for_row(
    row: Dict[str, Any],
) -> VerdictLabelAttribution:
    """Build a ``VerdictLabelAttribution`` for one
    ``analysis_results`` row. Never raises. Never re-runs
    ``_verdict_label``. Never mutates ``row``."""
    if not isinstance(row, dict):
        row = {}

    analysis_id = _coerce_analysis_id(row)
    stored_label = row.get("verdict_label")

    claim_count = _reconstruct_claim_count(row)
    snippets = _row_evidence_snippets(row)
    direct_support_count = _count_evidence_type(snippets, "direct_support")
    official_reference_count = _count_evidence_type(
        snippets, "official_reference",
    )
    insufficient_count = _count_evidence_type(snippets, "insufficient_evidence")
    contradiction = _row_contradiction(row)
    bias = _row_bias(row)
    confirmed_count = _coerce_int(
        contradiction.get("confirmed_contradiction_count")
        or contradiction.get("likely_contradiction_count")
        or 0
    )
    possible_count = _coerce_int(
        contradiction.get("possible_contradiction_count"),
    )
    high_framing_count = _coerce_int(bias.get("high_framing_count"))
    official_confirmation_count = _coerce_int(
        contradiction.get("needs_official_confirmation_count"),
    )
    insufficient_claim_count = _coerce_int(
        contradiction.get("insufficient_evidence_count"),
    )
    evidence_comparison = _reconstruct_evidence_comparison(row)
    comparison_status = evidence_comparison.get("comparison_status")
    verification_level = evidence_comparison.get("verification_level")
    has_conflict = bool(
        evidence_comparison.get("conflict_signals")
        or evidence_comparison.get("semantic_conflict_signals")
    )
    official_sources_count = _reconstruct_official_sources_count(row)
    confidence_score = _coerce_int(row.get("policy_confidence_score"))
    verification_strength = row.get("verification_strength")

    triggers = {
        "claim_count": claim_count,
        "direct_support_count": direct_support_count,
        "official_reference_count": official_reference_count,
        "insufficient_count": insufficient_count,
        "confirmed_count": confirmed_count,
        "possible_count": possible_count,
        "high_framing_count": high_framing_count,
        "official_confirmation_count": official_confirmation_count,
        "insufficient_claim_count": insufficient_claim_count,
        "has_conflict": has_conflict,
        "comparison_status": (
            str(comparison_status) if comparison_status else None
        ),
        "verification_level": (
            str(verification_level) if verification_level else None
        ),
        "official_sources_count": official_sources_count,
        "verification_strength": verification_strength,
        "confidence_score": confidence_score,
    }
    attribution = _attribute(stored_label, triggers)

    weak_signals = compute_weak_evidence_signals(row)
    is_weak_verified = bool(
        stored_label == "draft_verified" and weak_signals
    )

    return VerdictLabelAttribution(
        analysis_id=analysis_id,
        stored_verdict_label=stored_label,
        stored_verdict_confidence=_coerce_int(
            row.get("verdict_confidence"), default=0,
        ) if row.get("verdict_confidence") is not None else None,
        stored_policy_alert_level=row.get("policy_alert_level"),
        stored_policy_confidence_score=_coerce_int(
            row.get("policy_confidence_score"), default=0,
        ) if row.get("policy_confidence_score") is not None else None,
        stored_verification_strength=verification_strength,
        stored_claim_text=row.get("claim_text"),
        stored_evidence_summary=row.get("evidence_summary"),
        reconstructed_claim_count=claim_count,
        reconstructed_direct_support_count=direct_support_count,
        reconstructed_official_reference_count=official_reference_count,
        reconstructed_insufficient_count=insufficient_count,
        reconstructed_confirmed_count=confirmed_count,
        reconstructed_possible_count=possible_count,
        reconstructed_high_framing_count=high_framing_count,
        reconstructed_official_confirmation_count=official_confirmation_count,
        reconstructed_insufficient_claim_count=insufficient_claim_count,
        reconstructed_has_conflict=has_conflict,
        reconstructed_comparison_status=(
            str(comparison_status) if comparison_status else None
        ),
        reconstructed_verification_level=(
            str(verification_level) if verification_level else None
        ),
        reconstructed_official_sources_count=official_sources_count,
        attributed_branch_id=attribution["attributed_branch_id"],
        attribution_confidence=attribution["attribution_confidence"],
        attribution_reason=attribution["attribution_reason"],
        is_weak_evidence_verified=is_weak_verified,
        weak_evidence_signals=weak_signals,
        diagnostic_timestamp=datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        notes=NOTES_DIAGNOSTIC,
        truth_claim=False,
        operator_review_required=True,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _get(attr: Any, key: str) -> Any:
    if isinstance(attr, VerdictLabelAttribution):
        return getattr(attr, key, None)
    if isinstance(attr, dict):
        return attr.get(key)
    return None


def _branch_lookup() -> Dict[str, Dict[str, str]]:
    return {b["branch_id"]: b for b in VERDICT_LABEL_BRANCHES}


def compute_branch_summary(
    attributions: Iterable[Any],
) -> Dict[str, Any]:
    """Aggregate counts across many attribution rows. Accepts a mix
    of dataclass instances and dicts (the DB round-trip yields dicts).
    """
    items = list(attributions or [])
    total = len(items)
    branch_counts: Counter = Counter()
    label_counts: Counter = Counter()
    risk_counts: Counter = Counter()
    confidence_counts: Counter = Counter()
    weak_signal_counts: Counter = Counter()
    weak_verified = 0
    unknown = 0
    lookup = _branch_lookup()

    for item in items:
        branch_id = _get(item, "attributed_branch_id")
        confidence = _get(item, "attribution_confidence") or "unknown"
        confidence_counts[str(confidence)] += 1
        stored_label = _get(item, "stored_verdict_label")
        if stored_label:
            label_counts[str(stored_label)] += 1
        else:
            label_counts["NONE"] += 1
        if branch_id:
            branch_counts[branch_id] += 1
            branch_info = lookup.get(branch_id)
            risk = (
                branch_info["risk_classification"] if branch_info
                else "unknown_branch"
            )
            risk_counts[risk] += 1
        else:
            branch_counts["UNATTRIBUTED"] += 1
            risk_counts["unknown_branch"] += 1
        if confidence == "unknown":
            unknown += 1
        if _get(item, "is_weak_evidence_verified"):
            weak_verified += 1
        signals = _get(item, "weak_evidence_signals") or []
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except (TypeError, ValueError):
                signals = []
        if isinstance(signals, list):
            for signal in signals:
                weak_signal_counts[str(signal)] += 1

    def _pct(n: int, d: int) -> float:
        if d <= 0:
            return 0.0
        return round((n / d) * 100.0, 2)

    return {
        "total": total,
        "unknown_attribution_count": unknown,
        "per_branch_counts": dict(branch_counts),
        "per_output_label_counts": dict(label_counts),
        "per_risk_classification_counts": dict(risk_counts),
        "attribution_confidence_counts": dict(confidence_counts),
        "weak_evidence_verified_count": weak_verified,
        "weak_evidence_verified_percent": _pct(weak_verified, total),
        "weak_evidence_signal_histogram": dict(weak_signal_counts),
    }
