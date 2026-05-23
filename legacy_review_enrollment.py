"""Phase 2 M11.1: legacy weak-verified row review-queue enrollment.

Reads ``verdict_label_attributions`` rows where
``is_weak_evidence_verified=1`` (the M11.0b diagnostic output) and
creates one ``review_tasks`` entry per row so operators can correct
the 21 legacy ``draft_verified`` labels that M11.0c stopped producing
but did not retroactively rewrite.

Strictly read-mostly contract
-----------------------------

    * NEVER modifies ``analysis_results``.
    * NEVER rewrites ``analysis_results.verdict_label``.
    * NEVER auto-approves, auto-publishes, or auto-finalizes a
      review_task.
    * Every enrolled task is created with
      ``review_workflow.STATUS_PENDING_REVIEW`` — the same status the
      production review API uses for fresh tasks.
    * Re-running is safe: an entry's idempotency key is
      ``sha256(analysis_id|reason|legacy_review_enrollment)[:24]``,
      which collides with itself on a second run and triggers the
      existing ``review_tasks.idempotency_key`` UNIQUE constraint.
    * ``truth_claim`` is forced to ``False`` on every
      ``LegacyEnrollmentRecord``.
    * ``operator_review_required`` is forced to ``True`` on every
      record.

Hard contract
-------------

    * Never invoked automatically. ``main.py`` / ``api_server.py`` /
      ``scheduler.py`` do not import this module.
    * No ``requests`` / ``httpx`` / ``urllib.request`` / ``socket``
      imports.
    * No ``openai`` / ``anthropic`` imports.
    * Does NOT extend the ``review_tasks`` schema — uses the existing
      ``snapshot_json`` column to carry enrollment metadata
      (enrollment_reason, attribution_id, weak_evidence_signals).

Public surface (stable, pinned by tests)
----------------------------------------

    ENROLLMENT_REASON
    ENROLLMENT_STATUS
    NOTES_ENROLLMENT
    LegacyEnrollmentRecord                          (dataclass)
    enrollment_to_dict(record) -> dict
    make_enrollment_idempotency_key(analysis_id, reason) -> str
    make_enrollment_task_id(analysis_id, reason) -> str
    find_legacy_weak_verified_rows(db_path=None) -> list[dict]
    is_already_enrolled(analysis_id, reason=ENROLLMENT_REASON,
                        db_path=None) -> bool
    enroll_legacy_row(attribution_row, db_path=None,
                      dry_run=True) -> LegacyEnrollmentRecord
    compute_enrollment_summary(records) -> dict
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from structured_logging import get_logger


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Imports of the existing review layer (read-only)
# ---------------------------------------------------------------------------


import database  # noqa: E402
import review_workflow  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Reason string stored inside ``review_tasks.snapshot_json`` and used
# as part of the idempotency key. Pinned by tests — never rename
# without bumping a milestone.
ENROLLMENT_REASON = "legacy_weak_verified_m11_0c"

# The status every enrolled task is created with. We deliberately
# do NOT use approved / published / corrected — those are operator-
# decision outcomes, not enrollment states.
ENROLLMENT_STATUS = review_workflow.STATUS_PENDING_REVIEW

# Statuses that would be unsafe to auto-assign at enrollment time
# (sanity check for tests — these must never be used here).
_FORBIDDEN_ENROLLMENT_STATUSES = frozenset({
    review_workflow.STATUS_APPROVED,
    review_workflow.STATUS_REJECTED,
    review_workflow.STATUS_PUBLISHED,
    review_workflow.STATUS_CORRECTED,
})

NOTES_ENROLLMENT = (
    "legacy weak-verified row enrolled for human review by "
    "scripts/enroll_legacy_weak_verified.py (M11.1); "
    "analysis_results.verdict_label was NOT modified"
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class LegacyEnrollmentRecord:
    """Stable wire shape consumed by tests, the CLI, and the (future)
    enrollment-audit table. ``review_task_id`` is populated only when
    a write actually occurred (``dry_run=False`` AND not already
    enrolled). ``already_enrolled`` is True iff a matching task with
    the same ``(analysis_id, reason)`` idempotency key already existed.
    """
    analysis_id: str
    attribution_id: Optional[int] = None
    stored_verdict_label: Optional[str] = None
    stored_policy_confidence_score: Optional[int] = None
    stored_verification_strength: Optional[str] = None
    weak_evidence_signals: List[str] = field(default_factory=list)
    enrollment_reason: str = ENROLLMENT_REASON
    enrollment_timestamp: str = ""
    review_task_id: Optional[str] = None
    already_enrolled: bool = False
    # Set to True only when a fresh INSERT actually happened during
    # this call. Lets the CLI distinguish "enrolled now" from
    # "idempotent skip" and "dry-run skip" cleanly.
    wrote_to_db: bool = False
    error: Optional[str] = None
    truth_claim: bool = False
    operator_review_required: bool = True
    notes: str = NOTES_ENROLLMENT


def enrollment_to_dict(record: LegacyEnrollmentRecord) -> Dict[str, Any]:
    payload = asdict(record)
    payload["truth_claim"] = False
    payload["operator_review_required"] = True
    payload["notes"] = payload.get("notes") or NOTES_ENROLLMENT
    signals = payload.get("weak_evidence_signals") or []
    if not isinstance(signals, list):
        try:
            payload["weak_evidence_signals"] = list(signals)
        except Exception:
            payload["weak_evidence_signals"] = []
    return payload


# ---------------------------------------------------------------------------
# Idempotency helpers (do NOT collide with review_workflow's own keys)
# ---------------------------------------------------------------------------


def _digest(parts: Iterable[Any], *, length: int = 16) -> str:
    blob = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(
        blob.encode("utf-8", errors="replace")
    ).hexdigest()[:length]


def make_enrollment_idempotency_key(
    analysis_id: str, reason: str = ENROLLMENT_REASON,
) -> str:
    """Deterministic idempotency key. Two enrollments for the same
    ``(analysis_id, reason)`` produce the same key, which the
    ``review_tasks.idempotency_key`` UNIQUE constraint then blocks.

    The literal suffix ``"legacy_review_enrollment"`` guarantees the
    key never collides with ``review_workflow.make_idempotency_key``
    (which hashes a different field set, so collisions were already
    astronomically unlikely — but this is belt-and-suspenders)."""
    return _digest(
        [str(analysis_id), str(reason), "legacy_review_enrollment"],
        length=24,
    )


def make_enrollment_task_id(
    analysis_id: str, reason: str = ENROLLMENT_REASON,
) -> str:
    """Stable task_id for a legacy-enrollment task. ``review_`` prefix
    matches the convention in ``review_workflow.make_review_task_id``
    so the operator UI doesn't need to special-case the new shape."""
    digest = _digest(
        [str(analysis_id), str(reason), "legacy_review_enrollment"],
        length=16,
    )
    return f"review_legacy_{digest}"


# ---------------------------------------------------------------------------
# DB context helpers
# ---------------------------------------------------------------------------


def _with_db_path(db_path: Optional[str]):
    """Temporarily swap ``database.DB_PATH`` so the existing review
    helpers (which use the module-level path) read/write the right
    file. Returns the original value to be restored by the caller."""
    if db_path is None:
        return None
    original = database.DB_PATH
    database.DB_PATH = Path(db_path)
    return original


def _restore_db_path(original):
    if original is None:
        return
    database.DB_PATH = original


# ---------------------------------------------------------------------------
# Discovery + idempotency check
# ---------------------------------------------------------------------------


def find_legacy_weak_verified_rows(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return ``verdict_label_attributions`` rows where
    ``is_weak_evidence_verified=1``, newest first.

    Wraps ``database.get_verdict_label_attributions(only_weak_evidence_verified=True)``
    so callers don't have to manage ``db_path`` themselves. Returns
    ``[]`` on any DB error (logged at WARNING)."""
    try:
        return database.get_verdict_label_attributions(
            only_weak_evidence_verified=True,
            db_path=db_path,
            # Use the DB-side maximum so we never truncate the queue.
            limit=500,
        )
    except Exception as error:
        logger.warning(
            "[legacy_review_enrollment] find_legacy_weak_verified_rows "
            "failed: %s: %s", type(error).__name__, error,
        )
        return []


def is_already_enrolled(
    analysis_id: str,
    reason: str = ENROLLMENT_REASON,
    db_path: Optional[str] = None,
) -> bool:
    """True iff a review_task already exists with the idempotency key
    for ``(analysis_id, reason)``. Never raises."""
    if not analysis_id:
        return False
    key = make_enrollment_idempotency_key(analysis_id, reason)
    original = _with_db_path(db_path)
    try:
        existing = database.get_review_task_by_idempotency_key(key)
    except Exception as error:
        logger.warning(
            "[legacy_review_enrollment] is_already_enrolled "
            "lookup failed for analysis_id=%s reason=%s: %s",
            analysis_id, reason, error,
        )
        existing = None
    finally:
        _restore_db_path(original)
    return existing is not None


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


def _coerce_signals(value: Any) -> List[str]:
    """Defensive parse of ``weak_evidence_signals`` — accepts the JSON-
    encoded string the DB stores or a Python list. Falsy/malformed
    input yields ``[]``."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(s) for s in value if s is not None]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(decoded, list):
            return [str(s) for s in decoded if s is not None]
    return []


def _build_snapshot(
    *,
    analysis_id: str,
    attribution_row: Dict[str, Any],
    enrolled_at: str,
) -> Dict[str, Any]:
    """The dict that becomes ``review_tasks.snapshot_json``. Mirrors
    the field shape the operator UI expects from the existing review
    snapshot extractor where possible, and adds the enrollment-
    specific metadata under ``legacy_enrollment``.
    """
    signals = _coerce_signals(attribution_row.get("weak_evidence_signals"))
    return {
        "query": None,
        "claim_text": attribution_row.get("stored_claim_text") or "",
        "title": "",
        "url": "",
        "final_decision": {
            "policy_alert_level": attribution_row.get(
                "stored_policy_alert_level"
            ),
        },
        "policy_confidence": {
            "policy_confidence_score": attribution_row.get(
                "stored_policy_confidence_score"
            ),
            "verification_strength": attribution_row.get(
                "stored_verification_strength"
            ),
        },
        "verification_card": {
            "claim_text": attribution_row.get("stored_claim_text") or "",
            "verdict_label": attribution_row.get("stored_verdict_label"),
            "review_status": "ai_draft_pending_human_review",
        },
        "legacy_enrollment": {
            "reason": ENROLLMENT_REASON,
            "attribution_id": attribution_row.get("id"),
            "weak_evidence_signals": signals,
            "stored_evidence_summary": attribution_row.get(
                "stored_evidence_summary"
            ),
            "enrolled_at": enrolled_at,
            "source_milestone": "M11.1",
            "operator_review_required": True,
            "truth_claim": False,
            "notes": NOTES_ENROLLMENT,
        },
    }


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_record(analysis_id: str, attribution_row: Dict[str, Any]) -> LegacyEnrollmentRecord:
    signals = _coerce_signals(attribution_row.get("weak_evidence_signals"))
    return LegacyEnrollmentRecord(
        analysis_id=str(analysis_id),
        attribution_id=(
            int(attribution_row.get("id"))
            if attribution_row.get("id") is not None
            else None
        ),
        stored_verdict_label=attribution_row.get("stored_verdict_label"),
        stored_policy_confidence_score=(
            int(attribution_row.get("stored_policy_confidence_score"))
            if attribution_row.get("stored_policy_confidence_score") is not None
            else None
        ),
        stored_verification_strength=attribution_row.get(
            "stored_verification_strength"
        ),
        weak_evidence_signals=signals,
        enrollment_reason=ENROLLMENT_REASON,
        enrollment_timestamp=_utc_now_iso(),
    )


def enroll_legacy_row(
    attribution_row: Dict[str, Any],
    db_path: Optional[str] = None,
    dry_run: bool = True,
) -> LegacyEnrollmentRecord:
    """Enroll one ``verdict_label_attributions`` row into the review
    queue. Never raises. Three terminal states (recorded in the
    returned record):

        * ``already_enrolled=True`` — a matching idempotency key
          already exists. ``review_task_id`` is the existing task's
          id. ``wrote_to_db`` is False.
        * ``dry_run=True`` and not already enrolled — ``review_task_id``
          is None, ``wrote_to_db`` is False.
        * fresh write — ``review_task_id`` set to the new task id,
          ``wrote_to_db`` is True.

    Defensive invariants pinned by tests:

        * Status passed to ``create_review_task`` is
          ``STATUS_PENDING_REVIEW`` and is never in
          ``_FORBIDDEN_ENROLLMENT_STATUSES``.
        * ``human_review_required=True``.
        * No write to ``analysis_results``.
        * ``truth_claim`` stays False; ``operator_review_required``
          stays True.
    """
    if not isinstance(attribution_row, dict):
        record = _empty_record("", {})
        record.error = "attribution_row must be a dict"
        return record

    analysis_id = str(attribution_row.get("analysis_id") or "")
    if not analysis_id:
        record = _empty_record("", attribution_row)
        record.error = "attribution_row.analysis_id is missing or empty"
        return record

    record = _empty_record(analysis_id, attribution_row)

    # --- Idempotency check ---
    try:
        already = is_already_enrolled(
            analysis_id, ENROLLMENT_REASON, db_path=db_path,
        )
    except Exception as error:
        record.error = (
            f"idempotency lookup failed: {type(error).__name__}: {error}"
        )
        return record
    if already:
        # Surface the existing task_id so the CLI can point the
        # operator at it.
        existing_task_id = make_enrollment_task_id(
            analysis_id, ENROLLMENT_REASON,
        )
        original = _with_db_path(db_path)
        try:
            existing = database.get_review_task_by_idempotency_key(
                make_enrollment_idempotency_key(
                    analysis_id, ENROLLMENT_REASON,
                )
            )
        except Exception:
            existing = None
        finally:
            _restore_db_path(original)
        if isinstance(existing, dict) and existing.get("task_id"):
            existing_task_id = existing["task_id"]
        record.review_task_id = existing_task_id
        record.already_enrolled = True
        record.wrote_to_db = False
        return record

    # --- Dry-run short-circuit ---
    if dry_run:
        record.review_task_id = None
        record.already_enrolled = False
        record.wrote_to_db = False
        return record

    # --- Fresh write ---
    # Triple-check the status is one of the documented pending
    # statuses before handing it to the DB layer. A future refactor
    # that flipped ENROLLMENT_STATUS to APPROVED would be caught here.
    if ENROLLMENT_STATUS in _FORBIDDEN_ENROLLMENT_STATUSES:
        record.error = (
            f"refusing to enroll: ENROLLMENT_STATUS={ENROLLMENT_STATUS!r} "
            "is in the forbidden auto-finalized set"
        )
        return record

    now = _utc_now_iso()
    snapshot = _build_snapshot(
        analysis_id=analysis_id,
        attribution_row=attribution_row,
        enrolled_at=now,
    )
    task_id = make_enrollment_task_id(analysis_id, ENROLLMENT_REASON)
    idempotency_key = make_enrollment_idempotency_key(
        analysis_id, ENROLLMENT_REASON,
    )

    original = _with_db_path(db_path)
    try:
        try:
            _task, was_existing = database.create_review_task(
                task_id=task_id,
                result_id=analysis_id,
                job_id=None,
                item_index=0,
                status=ENROLLMENT_STATUS,
                query="",
                claim_text=attribution_row.get("stored_claim_text") or "",
                title="",
                url="",
                final_decision=attribution_row.get(
                    "stored_policy_alert_level"
                ) or "",
                policy_confidence=str(
                    attribution_row.get("stored_policy_confidence_score") or ""
                ),
                human_review_required=True,
                snapshot=snapshot,
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
        except Exception as error:
            record.error = (
                f"create_review_task failed: {type(error).__name__}: {error}"
            )
            return record
    finally:
        _restore_db_path(original)

    record.review_task_id = task_id
    if was_existing:
        # A concurrent enrollment beat us to it; record idempotency
        # rather than double-counting.
        record.already_enrolled = True
        record.wrote_to_db = False
    else:
        record.already_enrolled = False
        record.wrote_to_db = True
    return record


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def compute_enrollment_summary(
    records: Iterable[LegacyEnrollmentRecord],
) -> Dict[str, Any]:
    """Stable aggregation across many enrollment records. Accepts the
    dataclass instances; the CLI converts dicts back via the
    serializer when needed."""
    items = list(records or [])
    total = len(items)
    enrolled_now = 0
    already_enrolled = 0
    dry_run_skipped = 0
    errors = 0
    signal_counter: Counter = Counter()
    label_counter: Counter = Counter()
    score_counter: Counter = Counter()
    strength_counter: Counter = Counter()

    for r in items:
        if r.error:
            errors += 1
            continue
        if r.wrote_to_db:
            enrolled_now += 1
        elif r.already_enrolled:
            already_enrolled += 1
        else:
            dry_run_skipped += 1
        for signal in r.weak_evidence_signals or []:
            signal_counter[str(signal)] += 1
        if r.stored_verdict_label:
            label_counter[str(r.stored_verdict_label)] += 1
        if r.stored_policy_confidence_score is not None:
            score_counter[str(r.stored_policy_confidence_score)] += 1
        if r.stored_verification_strength:
            strength_counter[str(r.stored_verification_strength)] += 1

    return {
        "total": total,
        "enrolled_now": enrolled_now,
        "already_enrolled": already_enrolled,
        "dry_run_skipped": dry_run_skipped,
        "errors": errors,
        "weak_evidence_signal_histogram": dict(signal_counter),
        "stored_verdict_label_histogram": dict(label_counter),
        "stored_policy_confidence_score_histogram": dict(score_counter),
        "stored_verification_strength_histogram": dict(strength_counter),
    }
