"""Phase 2 M8.0: server-backed reviewer workflow helpers.

Pure-stdlib status / decision vocabulary, validation rules, and snapshot
extraction. Has **no** database access, no FastAPI dependency, no
network, no OpenAI. The DB layer (``database.py``) and the API layer
(``api_server.py``) import these helpers; they do not import the reverse.

Verdict isolation contract:
    * ``policy_decision`` / ``policy_scoring`` / ``verification_card``
      are NOT imported here. The reviewer workflow can read the verdict
      that the pipeline produced (via the snapshot extractor below) but
      it cannot change it.
    * Status transitions are validated deterministically — same input
      always produces the same allowed/refused result.
    * Publication is intentionally not implemented in M8.0. ``published``
      and ``corrected`` are reserved status names; no transition to them
      is allowed.

Reviewer principle: AI drafts and summarizes; humans approve / reject /
request more evidence. No path in this module can publish or change the
final verdict.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Statuses a review task can hold. ``published`` and ``corrected`` are
# reserved for future milestones and are NOT reachable in M8.0 — the
# transition table refuses any decision that would move into them.
STATUS_PENDING_REVIEW = "pending_review"
STATUS_NEEDS_MORE_EVIDENCE = "needs_more_evidence"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PUBLISHED = "published"        # reserved
STATUS_CORRECTED = "corrected"        # reserved

ALL_STATUSES = (
    STATUS_PENDING_REVIEW,
    STATUS_NEEDS_MORE_EVIDENCE,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_PUBLISHED,
    STATUS_CORRECTED,
)

# Decisions a reviewer can record. ``comment`` is purely informational
# and never changes status. The other three change status per the
# transition table below.
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_NEEDS_MORE_EVIDENCE = "needs_more_evidence"
DECISION_COMMENT = "comment"

ALL_DECISIONS = (
    DECISION_APPROVE,
    DECISION_REJECT,
    DECISION_NEEDS_MORE_EVIDENCE,
    DECISION_COMMENT,
)

# Decisions that move the task to a new status. Other decisions leave
# status untouched (``comment`` is the only such case in M8.0).
_DECISION_TARGET_STATUS = {
    DECISION_APPROVE: STATUS_APPROVED,
    DECISION_REJECT: STATUS_REJECTED,
    DECISION_NEEDS_MORE_EVIDENCE: STATUS_NEEDS_MORE_EVIDENCE,
}

# Which (current_status, decision) pairs are allowed. The matrix is
# deliberately conservative: once a task is approved or rejected, only
# comments are accepted — re-opening / overriding requires a future
# explicit milestone.
_ALLOWED_TRANSITIONS = {
    STATUS_PENDING_REVIEW: {
        DECISION_APPROVE, DECISION_REJECT,
        DECISION_NEEDS_MORE_EVIDENCE, DECISION_COMMENT,
    },
    STATUS_NEEDS_MORE_EVIDENCE: {
        DECISION_APPROVE, DECISION_REJECT,
        DECISION_NEEDS_MORE_EVIDENCE, DECISION_COMMENT,
    },
    STATUS_APPROVED: {DECISION_COMMENT},
    STATUS_REJECTED: {DECISION_COMMENT},
    STATUS_PUBLISHED: {DECISION_COMMENT},
    STATUS_CORRECTED: {DECISION_COMMENT},
}


class ReviewWorkflowError(ValueError):
    """Raised when a status / decision input is invalid or a transition
    is not allowed. Carries a stable ``reason`` attribute so the API
    layer can map specific failures to specific HTTP responses."""

    def __init__(self, message: str, *, reason: str = "invalid"):
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Normalizers — every public entry point flows through these.
# ---------------------------------------------------------------------------


def normalize_review_status(value: object) -> str:
    """Return a known status string. Raises on unknown."""
    raw = (str(value or "").strip().lower())
    if raw not in ALL_STATUSES:
        raise ReviewWorkflowError(
            f"unknown review status: {value!r}", reason="unknown_status",
        )
    return raw


def normalize_review_decision(value: object) -> str:
    """Return a known decision string. Raises on unknown."""
    raw = (str(value or "").strip().lower())
    if raw not in ALL_DECISIONS:
        raise ReviewWorkflowError(
            f"unknown review decision: {value!r}", reason="unknown_decision",
        )
    return raw


def validate_status_transition(current_status: object, decision: object) -> str:
    """Validate (current_status, decision) and return the new status.

    For decisions that don't change status (currently only ``comment``)
    the returned new_status equals ``current_status``. The function
    raises ``ReviewWorkflowError`` for unknown values or disallowed
    transitions; the caller (DB / API) maps the exception to an HTTP
    response.
    """
    current = normalize_review_status(current_status)
    dec = normalize_review_decision(decision)
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if dec not in allowed:
        raise ReviewWorkflowError(
            f"decision {dec!r} not allowed from status {current!r}",
            reason="transition_not_allowed",
        )
    target = _DECISION_TARGET_STATUS.get(dec)
    if target is None:
        # comment-only decision — status does not change.
        return current
    # Reserved future statuses are not reachable from any decision.
    # Defensive check in case the transition matrix grows incorrectly
    # in a future milestone without updating this guard.
    if target in (STATUS_PUBLISHED, STATUS_CORRECTED):
        raise ReviewWorkflowError(
            f"target status {target!r} is reserved for a future milestone",
            reason="reserved_status",
        )
    return target


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def _stable_digest(parts: List[Any], *, length: int = 16) -> str:
    """Deterministic SHA-256 prefix used for idempotency keys."""
    blob = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:length]


def make_review_task_id(*, result_id: Any, job_id: Any, item_index: int,
                        claim_text: str) -> str:
    """Stable task ID for idempotent upsert keyed on the analysis run.

    Same ``(result_id, job_id, item_index, claim_text)`` always produces
    the same ID. The hash includes ``claim_text`` so different claims
    within the same news item get distinct tasks.
    """
    digest = _stable_digest(
        [result_id, job_id, int(item_index or 0), (claim_text or "").strip()],
        length=16,
    )
    return f"review_{digest}"


def make_review_decision_id() -> str:
    """Unique decision ID. Decisions are append-only — every decision
    gets a fresh UUID so we never overwrite history."""
    return f"decision_{uuid.uuid4().hex[:16]}"


def make_idempotency_key(*, result_id: Any, job_id: Any, item_index: int,
                         claim_text: str) -> str:
    """Idempotency key stored on the row so a duplicate POST returns the
    existing task. Same identifying tuple as ``make_review_task_id``."""
    return _stable_digest(
        [result_id, job_id, int(item_index or 0), (claim_text or "").strip()],
        length=24,
    )


def now_iso() -> str:
    """UTC ISO timestamp at microsecond precision.

    Microsecond precision matters here because review decisions can
    legitimately land within the same wall-clock second (tests / fast
    reviewer actions) and the API contract guarantees an audit-friendly
    chronological order. Second-level precision would collapse those
    timestamps and force callers to rely on insertion ID for ordering.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Snapshot extraction from a /jobs/{id}/result-style payload
# ---------------------------------------------------------------------------


def _safe_get(obj: Any, path: List[Any], default: Any = None) -> Any:
    """Walk ``path`` (sequence of dict keys / list indices) safely.
    Never raises; returns ``default`` at the first failure."""
    cur = obj
    for step in path:
        try:
            if isinstance(cur, dict):
                cur = cur.get(step, default)
            elif isinstance(cur, list) and isinstance(step, int):
                cur = cur[step] if 0 <= step < len(cur) else default
            else:
                return default
        except Exception:
            return default
        if cur is default:
            return default
    return cur


def _coerce_str(value: Any, *, max_chars: int = 0) -> str:
    if value is None:
        return ""
    try:
        out = str(value).strip()
    except Exception:
        return ""
    if max_chars and len(out) > max_chars:
        return out[:max_chars].rstrip()
    return out


def _extract_first_claim(news_result: Any) -> str:
    """Best-effort claim_text extraction. Prefers normalized_claims
    (cleanest single-sentence claims), then policy_claims, then
    top-level title/query."""
    if not isinstance(news_result, dict):
        return ""
    norm = news_result.get("normalized_claims")
    if isinstance(norm, list):
        for item in norm:
            if isinstance(item, dict):
                text = _coerce_str(item.get("claim_text") or item.get("text"))
                if text:
                    return text
            elif isinstance(item, str):
                text = _coerce_str(item)
                if text:
                    return text
    policy = news_result.get("policy_claims")
    if isinstance(policy, list):
        for item in policy:
            if isinstance(item, dict):
                text = _coerce_str(item.get("sentence") or item.get("claim_text"))
                if text:
                    return text
    # Fall back to the news-item's own title / query.
    return _coerce_str(news_result.get("title") or news_result.get("query"))


def extract_review_snapshot_from_result(payload: Any, *, item_index: int = 0,
                                        query: Optional[str] = None) -> dict:
    """Pull a defensively-shaped review snapshot from a result payload.

    Accepts both raw pipeline reports (``{"news_results": [...]}``) and
    the ``/jobs/{id}/result`` wrapper (``{"result": {"results": [...]}}``).
    Missing or malformed fields produce empty strings rather than
    exceptions — the snapshot is meant to be a defensive cache that the
    reviewer UI can later display alongside the original payload.
    """
    if not isinstance(payload, dict):
        payload = {}

    # Find the inner news-result list across both shapes.
    candidates: List[Any] = []
    if isinstance(payload.get("results"), list):
        candidates = payload["results"]
    elif isinstance(payload.get("news_results"), list):
        candidates = payload["news_results"]
    else:
        inner = payload.get("result")
        if isinstance(inner, dict):
            if isinstance(inner.get("results"), list):
                candidates = inner["results"]
            elif isinstance(inner.get("news_results"), list):
                candidates = inner["news_results"]

    item: dict = {}
    if isinstance(candidates, list) and 0 <= item_index < len(candidates):
        if isinstance(candidates[item_index], dict):
            item = candidates[item_index]

    claim_text = _coerce_str(_extract_first_claim(item), max_chars=2000)
    title = _coerce_str(item.get("title"), max_chars=400)
    url = _coerce_str(item.get("original_url") or item.get("url"), max_chars=600)

    final_decision = _coerce_str(
        _safe_get(item, ["final_decision", "decision_label"])
        or _safe_get(item, ["final_decision", "final_decision_label"])
        or item.get("final_decision"),
        max_chars=200,
    )
    if isinstance(item.get("final_decision"), dict) and not final_decision:
        # When final_decision is a dict we serialize the label/key safely.
        label = item["final_decision"].get("decision_label") or \
                item["final_decision"].get("verdict_label")
        final_decision = _coerce_str(label, max_chars=200)

    pc = item.get("policy_confidence")
    if isinstance(pc, dict):
        policy_confidence = _coerce_str(
            pc.get("verification_strength") or pc.get("policy_confidence_label"),
            max_chars=200,
        )
    else:
        policy_confidence = _coerce_str(pc, max_chars=200)

    return {
        "query": _coerce_str(query or payload.get("query"), max_chars=400),
        "item_index": int(item_index or 0),
        "claim_text": claim_text,
        "title": title,
        "url": url,
        "final_decision": final_decision,
        "policy_confidence": policy_confidence,
        "human_review_required": True,
        "has_verification_card": isinstance(item.get("verification_card"), dict),
        "has_semantic_evidence_summary": (
            isinstance(_safe_get(item, ["debug_summary", "semantic_evidence_summary"]), dict)
        ),
    }


def summarize_review_task(task: dict) -> dict:
    """Project a stored task row into a stable wire shape for API responses.

    Strips internal-only fields (raw snapshot blob, idempotency key) and
    leaves the public, JSON-safe summary the reviewer UI can render.
    """
    if not isinstance(task, dict):
        return {}
    return {
        "task_id": task.get("task_id"),
        "result_id": task.get("result_id"),
        "job_id": task.get("job_id"),
        "item_index": task.get("item_index", 0),
        "status": task.get("status"),
        "query": task.get("query"),
        "claim_text": task.get("claim_text"),
        "title": task.get("title"),
        "url": task.get("url"),
        "final_decision": task.get("final_decision"),
        "policy_confidence": task.get("policy_confidence"),
        "human_review_required": bool(task.get("human_review_required", True)),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


def detail_review_task(task: dict, *, decisions: Optional[List[dict]] = None,
                       include_snapshot: bool = True) -> dict:
    """Wire-shape for the task-detail endpoint. Includes decisions and
    (optionally) the stored snapshot JSON the reviewer UI can render."""
    summary = summarize_review_task(task)
    summary["decisions"] = list(decisions or [])
    if include_snapshot:
        summary["snapshot"] = task.get("snapshot") if isinstance(task, dict) else None
    return summary
