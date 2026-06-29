import json
from datetime import datetime, timezone
from pathlib import Path

from structured_logging import get_logger
from text_utils import sanitize_data, sanitize_text


# M12.0d-1 (Stage 1) — module-level logger for new ``log.error`` calls
# in the PG-primary read functions (get_result_by_id et al.). Imported
# lazily-free at module load because structured_logging.get_logger
# only configures handlers idempotently. Existing ``_embedding_logger``
# alias at the bottom of this file is kept for the embedding-cache
# helpers and is unrelated to this logger.
#
# M12.0d-2 (Stage 2 / Q7=1.2 contract): the SQLite fallback blocks
# inside each of the 15 PG-primary read functions are preserved as
# the explicit ``pg_enabled=False`` path for local dev / tests
# without a Postgres substrate. They remain structurally unreachable
# when ``USE_POSTGRES_WRITE=true`` (the ``if pg_enabled:`` branch
# always returns first), so on Render they never execute. The
# per-function Stage 1 comments saying "SQLite block unreachable when
# dual-write enabled" remain factually accurate; Stage 2's
# contribution is the explicit decision that the block stays in tree
# rather than being deleted.
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Phase 2 M12.0a — Postgres dual-write shim.
#
# Each public write function in this module calls one of these helpers
# AFTER its SQLite write succeeds. The helpers are no-ops when the
# USE_POSTGRES_WRITE env var is unset / "false" — they return False
# without attempting any connection, and they never raise. Postgres
# failures must never break the SQLite write path or alter return
# values, so every call site wraps the helper in a defensive try/except
# (belt-and-braces; the helpers already swallow internally).
#
# SQLite remains the sole source of truth. See ``docs/POSTGRES_MIGRATION.md``.
# ---------------------------------------------------------------------------


def _mirror_write_safe(table_name: str, row_dict: dict) -> None:
    """Best-effort mirror write. Logs but never propagates failure."""
    try:
        from postgres_storage import mirror_write

        mirror_write(table_name, row_dict)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        pass


def _mirror_upsert_safe(
    table_name: str, row_dict: dict, conflict_columns: list,
) -> None:
    """Best-effort mirror upsert. Logs but never propagates failure."""
    try:
        from postgres_storage import mirror_upsert

        mirror_upsert(table_name, row_dict, conflict_columns)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        pass


def _mirror_write_returning_safe(table_name: str, row_dict: dict):
    """Best-effort mirror write that returns the PG-assigned integer id.

    M12.0d Stage 3c-1: used by ``save_analysis_result`` so the id stored
    in ``jobs.result_id`` and returned to the frontend matches the row
    that actually lives in Postgres. Returns ``None`` when dual-write
    is disabled, the import fails, or the insert fails — callers fall
    back to the SQLite-assigned id in that case.
    """
    try:
        from postgres_storage import mirror_write_returning

        return mirror_write_returning(table_name, row_dict)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        return None


def _mirror_upsert_returning_safe(
    table_name: str, row_dict: dict, conflict_columns: list,
):
    """Best-effort mirror upsert that returns the PG-assigned/updated id.

    M12.0d Stage 3c-3: the upsert analogue of
    :func:`_mirror_write_returning_safe`, used by
    ``save_producer_comparison`` (conflict on ``input_hash``) and
    ``save_verdict_label_attribution`` (conflict on ``analysis_id``) once
    those tables become PG-only writes. Returns ``None`` when dual-write
    is disabled, the import fails, or the upsert fails — callers treat
    None as an explicit write failure.
    """
    try:
        from postgres_storage import mirror_upsert_returning

        return mirror_upsert_returning(table_name, row_dict, conflict_columns)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        return None


def _serialize_market_signal(value) -> str:
    value = sanitize_data(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _serialize_json_value(value) -> str:
    value = sanitize_data(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def result_exists_by_url(original_url: str) -> bool:
    if not original_url:
        return False

    # M12.0c-3 / M12.0d-1: PG primary for duplicate detection. Both
    # True AND False are AUTHORITATIVE. The SQLite block below is
    # unreachable when dual-write is enabled (Stage 1: PG-read errors
    # now raise instead of silently falling back). SQLite remains
    # reachable when dual-write is disabled (local dev / tests).
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_analysis_result_exists_by_url,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "result_exists_by_url failed to import postgres_storage",
            exc_info=True,
            extra={"function": "result_exists_by_url"},
        )
        raise
    if pg_enabled:
        try:
            pg_result = read_analysis_result_exists_by_url(original_url)
        except Exception:
            log.error(
                "result_exists_by_url PG read failed",
                exc_info=True,
                extra={
                    "function": "result_exists_by_url",
                    "original_url": original_url,
                },
            )
            raise
        if pg_result is not None:
            return pg_result
        # PG returned None — engine None despite dual-write enabled
        # (DATABASE_URL missing / engine build failed). Treat as
        # "no row" per the Stage 1 contract; do NOT fall through to
        # SQLite. Operator should investigate via check_postgres_health.
        return False

    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → no
    # analysis_results data; PG is the sole durable store since 0e-5a).
    return False


def get_result_id_by_url(original_url: str):
    """Return the most recent analysis_results.id for the given URL, or None.

    Used when a duplicate save is skipped but the caller still needs to link
    a job row to the persisted result for durability after restart.
    """
    if not original_url:
        return None
    # M12.0c-3 / M12.0d-1: PG primary; SQLite block below is
    # unreachable when dual-write enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_analysis_result_id_by_url,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_result_id_by_url failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_result_id_by_url"},
        )
        raise
    if pg_enabled:
        try:
            pg_id = read_analysis_result_id_by_url(original_url)
        except Exception:
            log.error(
                "get_result_id_by_url PG read failed",
                exc_info=True,
                extra={
                    "function": "get_result_id_by_url",
                    "original_url": original_url,
                },
            )
            raise
        if pg_id is not None:
            return pg_id
        # PG returned None = no matching row (or engine miss).
        return None
    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → no
    # analysis_results data; PG is the sole durable store since 0e-5a).
    return None


def save_analysis_result(result: dict, query: str):
    result = sanitize_data(result)
    query = sanitize_text(query)
    original_url = result.get("original_url")
    if result_exists_by_url(original_url):
        return {"saved": False, "duplicate": True, "id": None}

    final_decision = result.get("final_decision") or {}
    policy_confidence = result.get("policy_confidence") or {}
    policy_impact = result.get("policy_impact") or {}
    verification_card = result.get("verification_card") or {}
    created_at = datetime.now(timezone.utc).isoformat()

    # M12.0a — build the value tuple once so the SQLite INSERT and the
    # Postgres mirror_write share an identical payload. Order matches
    # the column list in the INSERT below. Refactoring to a single
    # source removes drift risk between the two write paths.
    values = (
        query,
        result.get("title"),
        original_url,
        result.get("topic"),
        final_decision.get("policy_alert_level"),
        _serialize_market_signal(final_decision.get("market_signal")),
        policy_confidence.get("policy_confidence_score"),
        policy_confidence.get("verification_strength"),
        policy_confidence.get("risk_level"),
        policy_confidence.get("action_priority"),
        policy_impact.get("impact_level"),
        policy_impact.get("impact_direction"),
        policy_impact.get("market_sensitivity"),
        policy_impact.get("consumer_sensitivity"),
        policy_impact.get("business_sensitivity"),
        verification_card.get("claim_text") or result.get("claim_text"),
        verification_card.get("verdict_label") or result.get("verdict_label"),
        verification_card.get("verdict_confidence") or result.get("verdict_confidence"),
        _serialize_json_value(
            verification_card.get("evidence_sources")
            or result.get("evidence_sources")
        ),
        verification_card.get("source_reliability_score")
        or result.get("source_reliability_score"),
        verification_card.get("source_reliability_reason")
        or result.get("source_reliability_reason"),
        verification_card.get("evidence_summary") or result.get("evidence_summary"),
        _serialize_json_value(
            verification_card.get("missing_context")
            or result.get("missing_context")
        ),
        verification_card.get("last_checked_at") or result.get("last_checked_at"),
        verification_card.get("review_status") or result.get("review_status"),
        _serialize_json_value(
            verification_card.get("claims") or result.get("claims")
        ),
        _serialize_json_value(
            verification_card.get("normalized_claims")
            or result.get("normalized_claims")
        ),
        _serialize_json_value(
            verification_card.get("source_candidates")
            or result.get("source_candidates")
        ),
        _serialize_json_value(
            verification_card.get("source_queries")
            or result.get("source_queries")
        ),
        _serialize_json_value(
            verification_card.get("source_reliability_summary")
            or result.get("source_reliability_summary")
        ),
        _serialize_json_value(
            verification_card.get("evidence_snippets")
            or result.get("evidence_snippets")
        ),
        _serialize_json_value(
            verification_card.get("claim_evidence_map")
            or result.get("claim_evidence_map")
        ),
        _serialize_json_value(
            verification_card.get("evidence_extraction_summary")
            or result.get("evidence_extraction_summary")
        ),
        _serialize_json_value(
            verification_card.get("contradiction_checks")
            or result.get("contradiction_checks")
        ),
        _serialize_json_value(
            verification_card.get("contradiction_summary")
            or result.get("contradiction_summary")
        ),
        _serialize_json_value(
            verification_card.get("bias_framing_analysis")
            or result.get("bias_framing_analysis")
        ),
        _serialize_json_value(
            verification_card.get("bias_framing_summary")
            or result.get("bias_framing_summary")
        ),
        _serialize_json_value(
            verification_card.get("debug_summary")
            or result.get("debug_summary")
        ),
        created_at,
        # CLASSIFY-2a — domain category label (metadata only; never a verdict
        # field). None when CLASSIFY_ENABLED is off or classification failed.
        result.get("domain"),
    )

    # M12.0d Stage 3c-3: build the Postgres mirror payload once. The
    # column order here matches the historical analysis_results INSERT
    # column list exactly (and the order of the ``values`` tuple above).
    _ANALYSIS_RESULTS_COLUMN_ORDER = (
        "query", "title", "original_url", "topic", "policy_alert_level",
        "market_signal", "policy_confidence_score", "verification_strength",
        "risk_level", "action_priority", "impact_level", "impact_direction",
        "market_sensitivity", "consumer_sensitivity", "business_sensitivity",
        "claim_text", "verdict_label", "verdict_confidence",
        "evidence_sources", "source_reliability_score",
        "source_reliability_reason", "evidence_summary", "missing_context",
        "last_checked_at", "review_status", "claims", "normalized_claims",
        "source_candidates", "source_queries", "source_reliability_summary",
        "evidence_snippets", "claim_evidence_map",
        "evidence_extraction_summary", "contradiction_checks",
        "contradiction_summary", "bias_framing_analysis",
        "bias_framing_summary", "debug_summary", "created_at",
        # CLASSIFY-2a — must stay in lockstep with the values tuple above.
        "domain",
    )
    row_dict = dict(zip(_ANALYSIS_RESULTS_COLUMN_ORDER, values))

    # M12.0d Stage 3c-3: Postgres is the sole write target when dual-write
    # is enabled. PG's SERIAL sequence assigns the id (captured via
    # mirror_write_returning), and that id is the durable handle the rest
    # of the system uses (jobs.result_id, frontend localStorage,
    # GET /history/{id}). The 3c-1 sequence-alignment hotfix guarantees
    # the SERIAL is past any explicitly-written id, so the RETURNING id
    # never collides.
    # M12.0e-5a: Postgres is the sole durable store; the SQLite write
    # fallback was removed (point of no return). The PG write is now
    # unconditional. A None id means the row was persisted nowhere
    # (PG write failure, or dual-write disabled) — surface an explicit
    # failure (the 3c-1 data-loss class) rather than a phantom save.
    pg_id = _mirror_write_returning_safe("analysis_results", row_dict)
    if pg_id is None:
        log.error(
            "save_analysis_result PG write returned no id",
            extra={
                "function": "save_analysis_result",
                "original_url": original_url,
            },
        )
        return {
            "saved": False,
            "duplicate": False,
            "id": None,
            "error": "pg_write_failed",
        }
    return {"saved": True, "duplicate": False, "id": pg_id}


def get_recent_results(limit: int = 20):
    safe_limit = max(1, min(int(limit or 20), 100))
    # M12.0c-minimal / M12.0d-1: PG primary when dual-write is enabled.
    # Empty list from PG is AUTHORITATIVE (== "PG has 0 rows"); None
    # means engine-not-built. SQLite block is unreachable when
    # dual-write enabled (Stage 1: PG-read errors now raise).
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_recent_analysis_results,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_recent_results failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_recent_results"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_recent_analysis_results(safe_limit)
        except Exception:
            log.error(
                "get_recent_results PG read failed",
                exc_info=True,
                extra={
                    "function": "get_recent_results",
                    "limit": safe_limit,
                },
            )
            raise
        if pg_rows is not None:
            return pg_rows
        # PG returned None — engine None despite dual-write enabled.
        return []
    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → no
    # analysis_results data; PG is the sole durable store since 0e-5a).
    return []


def get_recent_results_slim(limit: int = 20):
    # PERF-2: slim list projection for GET /history — same PG-primary
    # error handling as get_recent_results, but reads only the lightweight
    # columns the homepage card list needs (the heavy JSON body columns are
    # dropped to cut the ~16MB response). The whole-row reader above is left
    # untouched for its existing callers; the DETAIL view still uses the
    # whole-row get_result_by_id via GET /history/{id}.
    safe_limit = max(1, min(int(limit or 20), 100))
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_recent_analysis_results_slim,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_recent_results_slim failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_recent_results_slim"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_recent_analysis_results_slim(safe_limit)
        except Exception:
            log.error(
                "get_recent_results_slim PG read failed",
                exc_info=True,
                extra={
                    "function": "get_recent_results_slim",
                    "limit": safe_limit,
                },
            )
            raise
        if pg_rows is not None:
            return pg_rows
        # PG returned None — engine None despite dual-write enabled.
        return []
    return []


def get_weekly_verification_stats(cutoff_iso: str):
    # SIDEBAR-RANK-B2: read-only weekly counts for the homepage sidebar's
    # "이번 주 검증 현황" panel. PG-primary, mirroring get_recent_results_slim's
    # error/None handling. Returns {"total": int, "official": int}; an empty
    # dict {"total": 0, "official": 0} when PG is authoritative but engine-None.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_weekly_verification_stats,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_weekly_verification_stats failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_weekly_verification_stats"},
        )
        raise
    if pg_enabled:
        try:
            pg_stats = read_weekly_verification_stats(cutoff_iso)
        except Exception:
            log.error(
                "get_weekly_verification_stats PG read failed",
                exc_info=True,
                extra={"function": "get_weekly_verification_stats"},
            )
            raise
        if pg_stats is not None:
            return pg_stats
        # PG returned None — engine None despite dual-write enabled.
        return {"total": 0, "official": 0}
    return {"total": 0, "official": 0}


def get_result_by_id(result_id: int):
    # M12.0c-minimal / M12.0d-1: PG primary; SQLite block unreachable
    # when dual-write enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_analysis_result_by_id,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_result_by_id failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_result_by_id"},
        )
        raise
    if pg_enabled:
        try:
            pg_row = read_analysis_result_by_id(result_id)
        except Exception:
            log.error(
                "get_result_by_id PG read failed",
                exc_info=True,
                extra={
                    "function": "get_result_by_id",
                    "result_id": result_id,
                },
            )
            raise
        if pg_row is not None:
            return pg_row
        # PG returned None = row not found (or engine miss).
        return None
    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → no
    # analysis_results data; PG is the sole durable store since 0e-5a).
    return None


# ---------------------------------------------------------------------------
# Phase 2 M5: embedding cache helpers
# ---------------------------------------------------------------------------
# These intentionally swallow exceptions and return ``None`` / ``False`` on
# any error: the cache is a performance optimisation, not a source of truth.
# A semantic ranking run must never fail because the cache table is locked,
# corrupted, or missing — the worst case is a recomputed embedding.

import logging as _embedding_logging  # local alias avoids top-of-file churn

_embedding_logger = _embedding_logging.getLogger(__name__)


def get_cached_embedding(text_hash: str, provider: str, model: str):
    """Return a previously stored vector, or ``None`` if absent/unusable.

    M12.0d-2 (Stage 2): PG-primary when dual-write is enabled, so the
    Web and Worker services on Render share a single cache instead of
    each rebuilding their own SQLite cache from scratch after every
    restart (Render free-tier filesystems are ephemeral). A PG cache
    miss (``None``) is a legitimate miss — caller computes a fresh
    embedding; we do NOT fall through to SQLite when PG is enabled.
    When dual-write is disabled (local dev / tests), the SQLite path
    runs unchanged."""
    if not text_hash or not provider:
        return None
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_cached_embedding,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_cached_embedding failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_cached_embedding"},
        )
        raise
    if pg_enabled:
        # M25a — when PGVECTOR_ENABLED, prefer the typed embedding_vectors store,
        # then fall back to the JSON embedding_cache. Best-effort: any pgvector
        # error is treated as a miss and we read the JSON cache. When the gate is
        # OFF this block is skipped entirely → byte-identical to pre-M25a.
        import config as _config

        if _config.pgvector_enabled():
            try:
                from postgres_storage import read_cached_embedding_vector

                pgv = read_cached_embedding_vector(text_hash, provider, model)
            except Exception:
                pgv = None
            if pgv is not None:
                return pgv
        try:
            pg_vector = read_cached_embedding(text_hash, provider, model)
        except Exception:
            log.error(
                "get_cached_embedding PG read failed",
                exc_info=True,
                extra={
                    "function": "get_cached_embedding",
                    "text_hash_prefix": text_hash[:16] if text_hash else None,
                    "provider": provider,
                    "model": model,
                },
            )
            raise
        # PG returned vector OR None (legitimate cache miss).
        # We do NOT fall through to SQLite — caller recomputes on miss.
        return pg_vector
    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → cache
    # miss; the caller recomputes the embedding). PG is the sole durable
    # cache since 0e-5a.
    return None


def save_cached_embedding(
    text_hash: str,
    provider: str,
    model: str,
    vector,
    text_preview: str = "",
):
    """Persist a vector. Returns True on success, False on any failure."""
    if not text_hash or not provider or not isinstance(vector, (list, tuple)):
        return False
    if not vector:
        return False
    try:
        vector_json = json.dumps(list(vector), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        _embedding_logger.warning("embedding_cache could not serialize vector: %s", error)
        return False
    preview = (text_preview or "")[:200]
    created_at = datetime.now(timezone.utc).isoformat()

    # M12.0e-5a: PG-primary, SQLite write fallback removed. The embedding
    # cache is best-effort; under dual-write the Postgres mirror below is
    # the durable copy and get_cached_embedding never consults SQLite. The
    # upsert maps INSERT OR REPLACE → ON CONFLICT (text_hash, provider,
    # model) DO UPDATE via the UNIQUE constraint in postgres_storage.py.
    _mirror_upsert_safe(
        "embedding_cache",
        {
            "text_hash": text_hash,
            "provider": provider,
            "model": model or "",
            "dimensions": len(vector),
            "vector_json": vector_json,
            "text_preview": preview,
            "created_at": created_at,
        },
        ["text_hash", "provider", "model"],
    )
    # M25a — when PGVECTOR_ENABLED, ALSO persist to the typed embedding_vectors
    # store (in addition to the JSON embedding_cache above, which stays the
    # durable fallback). Best-effort: failure never changes behavior. Skipped
    # entirely when the gate is OFF → byte-identical to pre-M25a.
    import config as _config

    if _config.pgvector_enabled():
        try:
            from postgres_storage import upsert_embedding_vector

            upsert_embedding_vector(
                text_hash=text_hash,
                provider=provider,
                model=model or "",
                dimensions=len(vector),
                embedding=list(vector),
                text_preview=preview,
                created_at=created_at,
            )
        except Exception:
            pass  # embedding_cache (JSON) is the durable copy
    return True


# ---------------------------------------------------------------------------
# Phase 2 M8.0: server-backed reviewer workflow persistence.
#
# Two tables — review_tasks (one row per (result_id, job_id, item_index,
# claim_text) tuple via the idempotency key) and review_decisions (one
# row per recorded reviewer action, append-only). The verdict tables
# (analysis_results) are NEVER mutated by anything in this section; the
# review layer is strictly additive.
# ---------------------------------------------------------------------------


def _row_to_review_task(row) -> dict:
    """SQLite row → review_task dict. Inflates snapshot_json to a dict."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    snapshot_raw = out.pop("snapshot_json", "")
    try:
        out["snapshot"] = json.loads(snapshot_raw) if snapshot_raw else {}
    except (TypeError, ValueError):
        out["snapshot"] = {}
    out["human_review_required"] = bool(out.get("human_review_required", 1))
    out["item_index"] = int(out.get("item_index") or 0)
    return out


def _row_to_review_decision(row) -> dict:
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    metadata_raw = out.pop("metadata_json", "")
    try:
        out["metadata"] = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError):
        out["metadata"] = {}
    return out


def get_review_task_by_idempotency_key(idempotency_key: str):
    """Return the existing task for an idempotency key, or None."""
    if not idempotency_key:
        return None
    # M12.0c-2 / M12.0d-1: PG primary; SQLite block unreachable when
    # dual-write enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_review_task_by_idempotency_key,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_review_task_by_idempotency_key failed to import "
            "postgres_storage",
            exc_info=True,
            extra={"function": "get_review_task_by_idempotency_key"},
        )
        raise
    if pg_enabled:
        try:
            pg_row = read_review_task_by_idempotency_key(idempotency_key)
        except Exception:
            log.error(
                "get_review_task_by_idempotency_key PG read failed",
                exc_info=True,
                extra={
                    "function": "get_review_task_by_idempotency_key",
                    "idempotency_key": idempotency_key,
                },
            )
            raise
        if pg_row is not None:
            return _row_to_review_task(pg_row)
        return None
    # Stage 3d Commit B: SQLite read-fallback removed (dual-write OFF → no review data)
    return None


def create_review_task(*, task_id: str, result_id, job_id, item_index: int,
                       status: str, query: str, claim_text: str, title: str,
                       url: str, final_decision: str, policy_confidence: str,
                       human_review_required: bool, snapshot: dict,
                       idempotency_key: str, created_at: str,
                       updated_at: str):
    """Insert a new review task (or return the existing row when the
    idempotency_key conflicts). Returns ``(task, was_existing)`` —
    callers use the second value to set the API's ``idempotent`` flag
    rather than relying on timestamp comparison (which fails when two
    calls land within the same second-precision ``now_iso()``)."""
    existing = get_review_task_by_idempotency_key(idempotency_key)
    if existing:
        return existing, True
    snapshot_json = json.dumps(snapshot or {}, ensure_ascii=False)
    # M12.0d Stage 3c-2: Postgres is the sole write target. The SQLite
    # INSERT (and the concurrent-writer IntegrityError fallback that
    # re-fetched the row) is gone; the PG ON CONFLICT DO UPDATE on
    # idempotency_key is now the only collision-resolution path.
    _mirror_upsert_safe(
        "review_tasks",
        {
            "task_id": task_id,
            "result_id": str(result_id) if result_id is not None else None,
            "job_id": str(job_id) if job_id is not None else None,
            "item_index": int(item_index or 0),
            "status": status,
            "query": query,
            "claim_text": claim_text,
            "title": title,
            "url": url,
            "final_decision": final_decision,
            "policy_confidence": policy_confidence,
            "human_review_required": 1 if human_review_required else 0,
            "snapshot_json": snapshot_json,
            "created_at": created_at,
            "updated_at": updated_at,
            "idempotency_key": idempotency_key,
        },
        ["idempotency_key"],
    )
    return (get_review_task(task_id) or {}), False


def get_review_task(task_id: str):
    """Return a single review_task dict, or None when not found."""
    if not task_id:
        return None
    # M12.0c-2 / M12.0d-1: PG primary; SQLite block unreachable when
    # dual-write enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_review_task_by_task_id,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_review_task failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_review_task"},
        )
        raise
    if pg_enabled:
        try:
            pg_row = read_review_task_by_task_id(task_id)
        except Exception:
            log.error(
                "get_review_task PG read failed",
                exc_info=True,
                extra={
                    "function": "get_review_task",
                    "task_id": task_id,
                },
            )
            raise
        if pg_row is not None:
            return _row_to_review_task(pg_row)
        return None
    # Stage 3d Commit B: SQLite read-fallback removed (dual-write OFF → no review data)
    return None


def list_review_tasks(*, status=None, limit: int = 50, offset: int = 0) -> list:
    """List review tasks (newest first). ``limit`` is clamped to [1, 100]
    so a single API call cannot pull the whole table."""
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    # M12.0c-2 / M12.0d-1: PG primary; [] is PG truth, None means
    # engine-not-built. SQLite block unreachable when dual-write
    # enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_review_tasks,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "list_review_tasks failed to import postgres_storage",
            exc_info=True,
            extra={"function": "list_review_tasks"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_review_tasks(
                status=status, limit=limit, offset=offset,
            )
        except Exception:
            log.error(
                "list_review_tasks PG read failed",
                exc_info=True,
                extra={
                    "function": "list_review_tasks",
                    "status": status,
                    "limit": limit,
                    "offset": offset,
                },
            )
            raise
        if pg_rows is not None:
            return [_row_to_review_task(r) for r in pg_rows]
        # PG returned None — engine not built.
        return []
    # Stage 3d Commit B: SQLite read-fallback removed (dual-write OFF → no review data)
    return []


def update_review_task_status(task_id: str, *, new_status: str,
                              updated_at: str) -> dict:
    """Update a task's status row. Caller is responsible for
    transition validation via review_workflow.validate_status_transition.

    M12.0d Stage 3c-2: Postgres is the sole write target. The previous
    SQLite UPDATE + SQLite re-read + ``mirror_upsert(full row)`` pattern
    is replaced by a direct PG ``UPDATE`` on ``status`` and ``updated_at``
    via :func:`postgres_storage.pg_update_review_task_status`. The final
    ``get_review_task`` re-read is PG-primary, so callers see the
    just-committed state."""
    try:
        from postgres_storage import pg_update_review_task_status
        pg_update_review_task_status(task_id, new_status, updated_at)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        pass
    return get_review_task(task_id) or {}


def set_analysis_human_review(result_id: int, *, reviewed: bool,
                              reviewer=None) -> bool:
    """M40a — set/clear the human-review columns on analysis_results.

    Thin PG-primary facade over
    :func:`postgres_storage.pg_set_analysis_human_review`. Returns its
    bool verbatim: ``True`` when a row matched and was updated, ``False``
    otherwise (no such id, dual-write disabled, or DB error). Postgres is
    the sole write target (M12.0e); SQLite holds no analysis_results
    data. Never raises — touches ONLY human_reviewed_at/by."""
    try:
        from postgres_storage import pg_set_analysis_human_review
        return pg_set_analysis_human_review(result_id, reviewed, reviewer)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        return False


def record_review_decision(*, decision_id: str, task_id: str, decision: str,
                           reviewer_id=None, comment=None, public_note=None,
                           previous_status=None, new_status=None,
                           created_at: str, metadata: dict = None,
                           decision_source: str = None) -> dict:
    """Append a decision row. Append-only — no UPDATE / DELETE path.

    ``decision_source`` (Phase 2 M9.0) is an operator-supplied audit
    label like ``review_api`` / ``review_ui`` / ``smoke_test``. It is
    NOT identity / auth and is never derived from ``REVIEW_API_TOKEN``.
    A None value is stored as SQL NULL; the audit-record builder maps
    that to ``unknown`` at the wire layer.
    """
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    # M12.0d Stage 3c-2: Postgres is the sole write target. The SQLite
    # INSERT is gone; the PG mirror_write (append-only, no upsert) is
    # now the only persistence step.
    _mirror_write_safe(
        "review_decisions",
        {
            "decision_id": decision_id,
            "task_id": task_id,
            "decision": decision,
            "reviewer_id": reviewer_id,
            "comment": comment,
            "public_note": public_note,
            "previous_status": previous_status,
            "new_status": new_status,
            "created_at": created_at,
            "metadata_json": metadata_json,
            "decision_source": decision_source,
        },
    )
    return get_review_decision(decision_id) or {}


def get_review_decision(decision_id: str):
    if not decision_id:
        return None
    # M12.0c-2 / M12.0d-1: PG primary; SQLite block unreachable when
    # dual-write enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_review_decision_by_id,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_review_decision failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_review_decision"},
        )
        raise
    if pg_enabled:
        try:
            pg_row = read_review_decision_by_id(decision_id)
        except Exception:
            log.error(
                "get_review_decision PG read failed",
                exc_info=True,
                extra={
                    "function": "get_review_decision",
                    "decision_id": decision_id,
                },
            )
            raise
        if pg_row is not None:
            return _row_to_review_decision(pg_row)
        return None
    # Stage 3d Commit B: SQLite read-fallback removed (dual-write OFF → no review data)
    return None


def list_review_decisions(task_id: str) -> list:
    if not task_id:
        return []
    # M12.0c-2 / M12.0d-1: PG primary; [] is PG truth, None means
    # engine-not-built. SQLite block unreachable when dual-write
    # enabled. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_review_decisions_for_task,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "list_review_decisions failed to import postgres_storage",
            exc_info=True,
            extra={"function": "list_review_decisions"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_review_decisions_for_task(task_id)
        except Exception:
            log.error(
                "list_review_decisions PG read failed",
                exc_info=True,
                extra={
                    "function": "list_review_decisions",
                    "task_id": task_id,
                },
            )
            raise
        if pg_rows is not None:
            return [_row_to_review_decision(r) for r in pg_rows]
        return []
    # Stage 3d Commit B: SQLite read-fallback removed (dual-write OFF → no review data)
    return []


# ---------------------------------------------------------------------------
# AUTH-2a: account login store.
#
# Caller-facing layer for the ``accounts`` table (declared in
# postgres_storage.py). Mirrors the review-table layering exactly: reads go
# through postgres_storage.read_account_by_username via a PG-primary gate;
# writes go through postgres_storage.mirror_write. Password hashing lives in
# accounts.py (bcrypt) and is invoked here at create time — only the hash is
# ever stored, never the plaintext. Nothing here touches any verdict field.
#
# Keeping create/get in this layer means api_server (AUTH-2b) reaches accounts
# through database.py and never imports postgres_storage directly (Phase-0
# import-discipline guard, test_postgres_storage.py:3109).
# ---------------------------------------------------------------------------


class AccountExistsError(Exception):
    """Raised by :func:`create_account` when ``username`` already exists.
    Callers that want idempotent behavior (e.g. scripts/create_admin.py)
    pre-check with :func:`get_account_by_username` or catch this."""


def _row_to_account(row) -> dict:
    """Raw account row (dict from postgres_storage) → plain dict. No
    transformation needed today; kept for parity with _row_to_review_task
    and as the single place to shape account rows if columns grow."""
    if not row:
        return {}
    return dict(row)


def get_account_by_username(username: str):
    """Return the account dict for ``username``, or None when missing /
    dual-write disabled. PG-primary read, mirroring get_review_task."""
    if not username:
        return None
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_account_by_username,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_account_by_username failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_account_by_username"},
        )
        raise
    if not pg_enabled:
        return None
    try:
        pg_row = read_account_by_username(username)
    except Exception:
        log.error(
            "get_account_by_username PG read failed",
            exc_info=True,
            extra={"function": "get_account_by_username"},
        )
        raise
    return _row_to_account(pg_row) if pg_row else None


def create_account(username: str, plain_password: str, role: str = "admin"):
    """Create one account with a bcrypt-hashed password.

    Hashing happens here (via accounts.hash_password); ONLY the hash is
    persisted — the plaintext is never stored, logged, or returned. Raises
    :class:`AccountExistsError` when ``username`` already exists, ``ValueError``
    on empty username/password, and ``RuntimeError`` if the persist fails.
    Returns the stored account dict on success.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username must be non-empty")
    if not plain_password:
        raise ValueError("password must be non-empty")
    if get_account_by_username(username):
        raise AccountExistsError(f"account already exists: {username}")
    # Local import keeps database import-light and avoids loading bcrypt
    # unless an account is actually created.
    from accounts import hash_password
    password_hash = hash_password(plain_password)
    now = datetime.now(timezone.utc).isoformat()
    from postgres_storage import mirror_write
    ok = mirror_write(
        "accounts",
        {
            "username": username,
            "password_hash": password_hash,
            "role": (role or "admin"),
            "created_at": now,
            "updated_at": now,
        },
    )
    if not ok:
        raise RuntimeError("failed to persist account")
    return get_account_by_username(username)


# ---------------------------------------------------------------------------
# Phase 2 M10.2: source-fetch-artifact persistence.
#
# Read-only catalog of operator-triggered static fetches against
# registry-candidate sources. The pipeline (analyze_pipeline /
# main.py) never reads or writes this table. ``truth_claim`` is
# stored as 0 on every row — the registry contract is that fetch
# artifacts never assert truth.
# ---------------------------------------------------------------------------


def _row_to_fetch_artifact(row) -> dict:
    """SQLite row → fetch-artifact dict. Maps the integer success /
    truth_claim / official_source_candidate columns back to booleans
    so callers don't have to remember the SQLite-boolean convention."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    out["success"] = bool(out.get("success", 0))
    # truth_claim is stored as 0 and surfaced as a bool. The registry
    # contract is that this field is always False; we re-assert here
    # as a defensive measure against any future row corruption.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["official_source_candidate"] = bool(
        out.get("official_source_candidate", 0)
    )
    return out


def save_fetch_artifact(fetch_result: dict) -> int:
    """Persist one fetch artifact and return the inserted row id.

    ``fetch_result`` matches the shape returned by
    ``source_crawler.fetch_result_to_dict``. Missing fields default
    safely. ``truth_claim`` is always stored as 0 regardless of the
    input (defensive against future regressions in the crawler that
    might try to set it true)."""
    if not isinstance(fetch_result, dict):
        raise ValueError("fetch_result must be a dict")
    if not fetch_result.get("source_id"):
        raise ValueError("fetch_result.source_id is required")
    if not fetch_result.get("url"):
        raise ValueError("fetch_result.url is required")
    if not fetch_result.get("fetch_timestamp"):
        raise ValueError("fetch_result.fetch_timestamp is required")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row_values = (
        str(fetch_result.get("source_id")),
        str(fetch_result.get("url")),
        str(fetch_result.get("fetch_timestamp")),
        fetch_result.get("status_code"),
        fetch_result.get("content_type"),
        1 if fetch_result.get("success") else 0,
        fetch_result.get("error"),
        fetch_result.get("text_content"),
        fetch_result.get("raw_html"),
        fetch_result.get("fetch_duration_ms"),
        # truth_claim is forced to 0 — the registry contract.
        0,
        1 if fetch_result.get("official_source_candidate") else 0,
        created_at,
    )
    # M12.0d Stage 3c-3: build the Postgres mirror payload once (column
    # order matches the historical source_fetch_artifacts INSERT).
    mirror_payload = {
        "source_id": row_values[0],
        "url": row_values[1],
        "fetch_timestamp": row_values[2],
        "status_code": row_values[3],
        "content_type": row_values[4],
        "success": row_values[5],
        "error": row_values[6],
        "text_content": row_values[7],
        "raw_html": row_values[8],
        "fetch_duration_ms": row_values[9],
        "truth_claim": row_values[10],
        "official_source_candidate": row_values[11],
        "created_at": row_values[12],
    }

    # M12.0d Stage 3c-3: Postgres is the sole write target when dual-write
    # is enabled. PG's SERIAL sequence assigns the id (captured via
    # mirror_write_returning); the returned id is the value the operator
    # CLI (scripts/fetch_registry_source.py) prints as ``saved_row_id``.
    # The function keeps its
    # ``-> int`` contract: on PG write failure it returns the sentinel
    # ``-1`` (an impossible real row id) rather than a phantom positive id
    # — the 3c-1 data-loss class, surfaced explicitly via log.error.
    # M12.0e-5a: Postgres is the sole durable store; the SQLite write
    # fallback was removed. The PG write is now unconditional and returns
    # the sentinel -1 on failure (the 3c-1 data-loss class, surfaced via
    # log.error) rather than a phantom positive id.
    pg_id = _mirror_write_returning_safe(
        "source_fetch_artifacts", mirror_payload,
    )
    if pg_id is None:
        log.error(
            "save_fetch_artifact PG write returned no id",
            extra={
                "function": "save_fetch_artifact",
                "source_id": row_values[0],
                "url": row_values[1],
            },
        )
        return -1
    return pg_id


def get_fetch_artifacts(source_id: str = None, limit: int = 50) -> list:
    """Return fetch artifacts (newest first), optionally filtered
    by ``source_id``. ``limit`` is clamped to ``[1, 500]`` so a single
    call cannot pull the whole table."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    # M12.0c-4 / M12.0d-1: PG primary; [] is PG truth, None means
    # engine-not-built. PG-read errors now raise.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_fetch_artifacts,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_fetch_artifacts failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_fetch_artifacts"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_fetch_artifacts(
                source_id=source_id, limit=capped_limit,
            )
        except Exception:
            log.error(
                "get_fetch_artifacts PG read failed",
                exc_info=True,
                extra={
                    "function": "get_fetch_artifacts",
                    "source_id": source_id,
                    "limit": capped_limit,
                },
            )
            raise
        if pg_rows is not None:
            return [_row_to_fetch_artifact(r) for r in pg_rows]
        return []
    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → no
    # source_fetch_artifacts data; PG is the sole durable store since 0e-5a).
    return []


# ---------------------------------------------------------------------------
# Phase 2 M10.4 — artifact_text_extractions table.
#
# Stores the cleaned title / main_text / sections produced by
# ``artifact_extractor.extract_text_from_artifact`` against rows in
# ``source_fetch_artifacts``. The registry contract still holds:
# ``truth_claim`` is forced to 0 on every persisted row regardless of
# the caller's input. Extraction results never feed the verdict path —
# they exist purely as raw, reviewable artifacts.
# ---------------------------------------------------------------------------


def _row_to_extraction_result(row) -> dict:
    """SQLite row → extraction-result dict. Maps the integer success /
    truth_claim / official_source_candidate columns back to booleans
    so callers don't have to remember the SQLite-boolean convention."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    out["success"] = bool(out.get("success", 0))
    # truth_claim is stored as 0 and surfaced as a bool. The registry
    # contract is that this field is always False; we re-assert here
    # as a defensive measure against any future row corruption.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["official_source_candidate"] = bool(
        out.get("official_source_candidate", 0)
    )
    return out


def save_extraction_result(result_dict: dict) -> int:
    """Persist one extraction artifact and return the inserted row id.

    ``result_dict`` matches the shape returned by
    ``artifact_extractor.extraction_result_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 regardless
    of the input (defensive against future regressions in the
    extractor that might try to set it true).
    """
    if not isinstance(result_dict, dict):
        raise ValueError("result_dict must be a dict")
    if result_dict.get("artifact_id") is None:
        raise ValueError("result_dict.artifact_id is required")
    if not result_dict.get("source_id"):
        raise ValueError("result_dict.source_id is required")
    if not result_dict.get("url"):
        raise ValueError("result_dict.url is required")
    if not result_dict.get("extraction_timestamp"):
        raise ValueError("result_dict.extraction_timestamp is required")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row_values = (
        int(result_dict.get("artifact_id") or 0),
        str(result_dict.get("source_id")),
        str(result_dict.get("url")),
        str(result_dict.get("extraction_timestamp")),
        result_dict.get("extraction_duration_ms"),
        1 if result_dict.get("success") else 0,
        result_dict.get("error"),
        result_dict.get("title"),
        result_dict.get("main_text"),
        result_dict.get("sections"),
        result_dict.get("word_count"),
        result_dict.get("language_hint"),
        # truth_claim is forced to 0 — the registry contract.
        0,
        1 if result_dict.get("official_source_candidate") else 0,
        created_at,
    )
    # M12.0d Stage 3c-3: build the Postgres mirror payload once (column
    # order matches the historical artifact_text_extractions INSERT).
    mirror_payload = {
        "artifact_id": row_values[0],
        "source_id": row_values[1],
        "url": row_values[2],
        "extraction_timestamp": row_values[3],
        "extraction_duration_ms": row_values[4],
        "success": row_values[5],
        "error": row_values[6],
        "title": row_values[7],
        "main_text": row_values[8],
        "sections": row_values[9],
        "word_count": row_values[10],
        "language_hint": row_values[11],
        "truth_claim": row_values[12],
        "official_source_candidate": row_values[13],
        "created_at": row_values[14],
    }

    # M12.0e-5a: Postgres is the sole durable store. PG's SERIAL sequence
    # assigns the id (captured via mirror_write_returning); the function
    # keeps its ``-> int`` contract, returning the sentinel ``-1`` on PG
    # write failure (the 3c-1 data-loss class, surfaced via log.error).
    pg_id = _mirror_write_returning_safe(
        "artifact_text_extractions", mirror_payload,
    )
    if pg_id is None:
        log.error(
            "save_extraction_result PG write returned no id",
            extra={
                "function": "save_extraction_result",
                "artifact_id": row_values[0],
                "source_id": row_values[1],
            },
        )
        return -1
    return pg_id


def get_extraction_results(source_id: str = None, artifact_id: int = None,
                           limit: int = 50) -> list:
    """Return extraction artifacts (newest first), optionally filtered
    by ``source_id`` and/or ``artifact_id``. ``limit`` is clamped to
    ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    # M12.0c-4 / M12.0d-1: PG primary. PG-read errors raise.
    # M12.0e-6a: the SQLite read path and the dual-write-OFF SQLite
    # fallback were removed; PG is the sole durable store since 0e-5a.
    # OFF → [].
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_extraction_results,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_extraction_results failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_extraction_results"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_extraction_results(
                source_id=source_id,
                artifact_id=artifact_id,
                limit=capped_limit,
            )
        except Exception:
            log.error(
                "get_extraction_results PG read failed",
                exc_info=True,
                extra={
                    "function": "get_extraction_results",
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "limit": capped_limit,
                },
            )
            raise
        if pg_rows is not None:
            return [_row_to_extraction_result(r) for r in pg_rows]
        return []
    return []


# ---------------------------------------------------------------------------
# Phase 2 M10.5 — artifact_evidence_candidates table.
#
# Stores keyword-overlap evidence candidates produced by
# ``artifact_evidence_linker.find_evidence_candidates`` against rows
# in ``artifact_text_extractions`` and ``analysis_results``. The
# registry contract still holds: ``truth_claim`` is forced to 0 and
# ``operator_review_required`` is forced to 1 on every persisted row,
# regardless of the caller's input. Candidates never feed the verdict
# path — they exist purely as raw, reviewable artifacts for operators.
# ---------------------------------------------------------------------------


def _row_to_evidence_candidate(row) -> dict:
    """SQLite row → evidence-candidate dict. Maps the integer
    truth_claim / official_source_candidate / operator_review_required
    columns back to booleans so callers don't have to remember the
    SQLite-boolean convention. ``matched_tokens`` stays as the stored
    JSON string — callers decide whether to decode it."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    # truth_claim is stored as 0 and surfaced as a bool. The registry
    # contract is that this field is always False; re-assert here as a
    # defensive measure against any future row corruption.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["official_source_candidate"] = bool(
        out.get("official_source_candidate", 0)
    )
    # operator_review_required is stored as 1 and surfaced as a bool.
    # The contract is that candidates always require review — defensive
    # re-assertion the same way.
    out["operator_review_required"] = bool(
        out.get("operator_review_required", 1)
    )
    try:
        out["match_score"] = float(out.get("match_score") or 0.0)
    except (TypeError, ValueError):
        out["match_score"] = 0.0
    return out


def save_evidence_candidate(candidate_dict: dict) -> int:
    """Persist one evidence candidate and return the inserted row id.

    ``candidate_dict`` matches the shape returned by
    ``artifact_evidence_linker.candidate_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 and
    ``operator_review_required`` as 1 regardless of the input
    (defensive against future regressions in the linker that might
    try to flip either flag).
    """
    if not isinstance(candidate_dict, dict):
        raise ValueError("candidate_dict must be a dict")
    if candidate_dict.get("extraction_id") is None:
        raise ValueError("candidate_dict.extraction_id is required")
    if not candidate_dict.get("source_id"):
        raise ValueError("candidate_dict.source_id is required")
    if not candidate_dict.get("url"):
        raise ValueError("candidate_dict.url is required")
    if not candidate_dict.get("analysis_id"):
        raise ValueError("candidate_dict.analysis_id is required")
    if not candidate_dict.get("claim_text"):
        raise ValueError("candidate_dict.claim_text is required")
    if not candidate_dict.get("candidate_timestamp"):
        raise ValueError("candidate_dict.candidate_timestamp is required")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    matched_tokens_raw = candidate_dict.get("matched_tokens")
    if isinstance(matched_tokens_raw, (list, tuple)):
        matched_tokens_text = json.dumps(
            list(matched_tokens_raw), ensure_ascii=False,
        )
    elif matched_tokens_raw is None:
        matched_tokens_text = json.dumps([], ensure_ascii=False)
    else:
        matched_tokens_text = str(matched_tokens_raw)
    try:
        match_score = float(candidate_dict.get("match_score") or 0.0)
    except (TypeError, ValueError):
        match_score = 0.0
    row_values = (
        int(candidate_dict.get("extraction_id") or 0),
        str(candidate_dict.get("source_id")),
        str(candidate_dict.get("url")),
        str(candidate_dict.get("analysis_id")),
        str(candidate_dict.get("claim_text")),
        match_score,
        matched_tokens_text,
        candidate_dict.get("supporting_passage"),
        str(candidate_dict.get("candidate_timestamp")),
        # truth_claim is forced to 0 — the registry contract.
        0,
        1 if candidate_dict.get("official_source_candidate") else 0,
        # operator_review_required is forced to 1 — the candidate contract.
        1,
        candidate_dict.get("notes"),
        created_at,
    )
    # M12.0d Stage 3c-3: build the Postgres mirror payload once (column
    # order matches the historical artifact_evidence_candidates INSERT).
    mirror_payload = {
        "extraction_id": row_values[0],
        "source_id": row_values[1],
        "url": row_values[2],
        "analysis_id": row_values[3],
        "claim_text": row_values[4],
        "match_score": row_values[5],
        "matched_tokens": row_values[6],
        "supporting_passage": row_values[7],
        "candidate_timestamp": row_values[8],
        "truth_claim": row_values[9],
        "official_source_candidate": row_values[10],
        "operator_review_required": row_values[11],
        "notes": row_values[12],
        "created_at": row_values[13],
    }

    # M12.0e-5a: Postgres is the sole durable store. PG's SERIAL sequence
    # assigns the id (captured via mirror_write_returning); the function
    # keeps its ``-> int`` contract, returning the sentinel ``-1`` on PG
    # write failure (the 3c-1 data-loss class, surfaced via log.error).
    pg_id = _mirror_write_returning_safe(
        "artifact_evidence_candidates", mirror_payload,
    )
    if pg_id is None:
        log.error(
            "save_evidence_candidate PG write returned no id",
            extra={
                "function": "save_evidence_candidate",
                "analysis_id": row_values[3],
                "extraction_id": row_values[0],
            },
        )
        return -1
    return pg_id


def get_evidence_candidates(
    analysis_id: str = None,
    source_id: str = None,
    extraction_id: int = None,
    limit: int = 50,
) -> list:
    """Return evidence candidates (newest first), optionally filtered
    by any combination of ``analysis_id``, ``source_id``, and
    ``extraction_id``. ``limit`` is clamped to ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    # M12.0c-4 / M12.0d-1: PG primary. PG-read errors raise.
    # M12.0e-6a: the SQLite read path and the dual-write-OFF SQLite
    # fallback were removed; PG is the sole durable store since 0e-5a.
    # OFF → [].
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_evidence_candidates,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_evidence_candidates failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_evidence_candidates"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_evidence_candidates(
                analysis_id=analysis_id,
                source_id=source_id,
                extraction_id=extraction_id,
                limit=capped_limit,
            )
        except Exception:
            log.error(
                "get_evidence_candidates PG read failed",
                exc_info=True,
                extra={
                    "function": "get_evidence_candidates",
                    "analysis_id": analysis_id,
                    "source_id": source_id,
                    "extraction_id": extraction_id,
                    "limit": capped_limit,
                },
            )
            raise
        if pg_rows is not None:
            return [_row_to_evidence_candidate(r) for r in pg_rows]
        return []
    return []


# ---------------------------------------------------------------------------
# Phase 2 M11.0a — verdict_producer_comparisons table.
#
# Read-only measurement layer for the three current verdict producers
# (policy_decision.make_final_decision,
# policy_scoring.calibrate_final_decision via _alert_from_score, and
# verification_card._verdict_label). The registry-style invariants
# still hold: ``truth_claim`` is forced to 0 and
# ``operator_review_required`` is forced to 1 on every persisted row.
# Comparison rows never feed the verdict path — they exist purely as
# operator-reviewable measurement.
# ---------------------------------------------------------------------------


def _row_to_producer_comparison(row) -> dict:
    """SQLite row → comparison dict. Maps the integer flag columns
    back to booleans and leaves the JSON-encoded ``producerN_extra``
    columns as strings (the caller decides whether to decode them)."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    for flag in (
        "all_three_agree", "p1_p2_agree", "p1_p3_agree", "p2_p3_agree",
    ):
        out[flag] = bool(out.get(flag, 0))
    # truth_claim must always read back False; operator_review_required
    # must always read back True. Defensive re-assertion mirrors the
    # registry contract pattern.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["operator_review_required"] = bool(
        out.get("operator_review_required", 1)
    )
    for score_field in ("producer1_score", "producer2_score"):
        if out.get(score_field) is not None:
            try:
                out[score_field] = float(out[score_field])
            except (TypeError, ValueError):
                out[score_field] = None
    return out


def save_producer_comparison(comparison_dict: dict) -> int:
    """Persist (or replace, on input_hash collision) one comparison
    row and return the resulting row id.

    ``comparison_dict`` matches
    ``verdict_producer_comparison.comparison_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 and
    ``operator_review_required`` as 1 regardless of input (defensive
    against future regressions in the comparison module).

    Re-saving the same ``input_hash`` overwrites the prior row via
    the UNIQUE index on ``input_hash`` (INSERT OR REPLACE)."""
    if not isinstance(comparison_dict, dict):
        raise ValueError("comparison_dict must be a dict")
    if not comparison_dict.get("analysis_id"):
        raise ValueError("comparison_dict.analysis_id is required")
    if not comparison_dict.get("source"):
        raise ValueError("comparison_dict.source is required")
    if not comparison_dict.get("input_hash"):
        raise ValueError("comparison_dict.input_hash is required")
    if not comparison_dict.get("comparison_timestamp"):
        raise ValueError("comparison_dict.comparison_timestamp is required")

    def _ensure_extra_text(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row_values = (
        str(comparison_dict.get("analysis_id")),
        str(comparison_dict.get("source")),
        str(comparison_dict.get("input_hash")),
        comparison_dict.get("producer1_label"),
        comparison_dict.get("producer1_score"),
        _ensure_extra_text(comparison_dict.get("producer1_extra")),
        comparison_dict.get("producer2_label"),
        comparison_dict.get("producer2_alert_level"),
        comparison_dict.get("producer2_score"),
        _ensure_extra_text(comparison_dict.get("producer2_extra")),
        comparison_dict.get("producer3_label"),
        _ensure_extra_text(comparison_dict.get("producer3_extra")),
        1 if comparison_dict.get("all_three_agree") else 0,
        1 if comparison_dict.get("p1_p2_agree") else 0,
        1 if comparison_dict.get("p1_p3_agree") else 0,
        1 if comparison_dict.get("p2_p3_agree") else 0,
        comparison_dict.get("disagreement_pattern"),
        comparison_dict.get("most_conservative_label"),
        str(comparison_dict.get("comparison_timestamp")),
        comparison_dict.get("notes"),
        # truth_claim is forced to 0 — the registry contract.
        0,
        # operator_review_required is forced to 1 — the measurement contract.
        1,
        created_at,
    )
    # M12.0d Stage 3c-3: build the Postgres mirror payload once. INSERT OR
    # REPLACE on the SQLite side maps to ON CONFLICT (input_hash) DO UPDATE
    # on the PG side.
    mirror_payload = {
        "analysis_id": row_values[0],
        "source": row_values[1],
        "input_hash": row_values[2],
        "producer1_label": row_values[3],
        "producer1_score": row_values[4],
        "producer1_extra": row_values[5],
        "producer2_label": row_values[6],
        "producer2_alert_level": row_values[7],
        "producer2_score": row_values[8],
        "producer2_extra": row_values[9],
        "producer3_label": row_values[10],
        "producer3_extra": row_values[11],
        "all_three_agree": row_values[12],
        "p1_p2_agree": row_values[13],
        "p1_p3_agree": row_values[14],
        "p2_p3_agree": row_values[15],
        "disagreement_pattern": row_values[16],
        "most_conservative_label": row_values[17],
        "comparison_timestamp": row_values[18],
        "notes": row_values[19],
        "truth_claim": row_values[20],
        "operator_review_required": row_values[21],
        "created_at": row_values[22],
    }

    # M12.0e-5a: Postgres is the sole durable store. PG assigns the id via
    # ON CONFLICT ... RETURNING (mirror_upsert_returning); the function
    # keeps its ``-> int`` contract, returning the sentinel ``-1`` on PG
    # write failure (the 3c-1 data-loss class, surfaced via log.error).
    pg_id = _mirror_upsert_returning_safe(
        "verdict_producer_comparisons", mirror_payload, ["input_hash"],
    )
    if pg_id is None:
        log.error(
            "save_producer_comparison PG write returned no id",
            extra={
                "function": "save_producer_comparison",
                "analysis_id": row_values[0],
                "input_hash": row_values[2],
            },
        )
        return -1
    return pg_id


def get_producer_comparisons(
    analysis_id: str = None,
    disagreement_pattern: str = None,
    only_disagreements: bool = False,
    limit: int = 50,
) -> list:
    """Return verdict-producer comparisons (newest first), optionally
    filtered by ``analysis_id``, ``disagreement_pattern``, or the
    ``only_disagreements`` flag (rows where ``all_three_agree=0``).
    ``limit`` is clamped to ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        capped_limit = 50
    # M12.0c-4 / M12.0d-1: PG primary. PG-read errors raise.
    # M12.0e-6a: the SQLite read path and the dual-write-OFF SQLite
    # fallback were removed; PG is the sole durable store since 0e-5a.
    # OFF → [].
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_producer_comparisons,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_producer_comparisons failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_producer_comparisons"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_producer_comparisons(
                analysis_id=analysis_id,
                disagreement_pattern=disagreement_pattern,
                only_disagreements=only_disagreements,
                limit=capped_limit,
            )
        except Exception:
            log.error(
                "get_producer_comparisons PG read failed",
                exc_info=True,
                extra={
                    "function": "get_producer_comparisons",
                    "analysis_id": analysis_id,
                    "disagreement_pattern": disagreement_pattern,
                    "only_disagreements": only_disagreements,
                    "limit": capped_limit,
                },
            )
            raise
        if pg_rows is not None:
            return [_row_to_producer_comparison(r) for r in pg_rows]
        return []
    return []


# ---------------------------------------------------------------------------
# Phase 2 M11.0b — verdict_label_attributions table.
#
# Read-only diagnostic layer for ``verification_card._verdict_label``.
# Each row records which documented branch most likely produced the
# stored ``analysis_results.verdict_label`` value AND whether the
# stored label is a weak-evidence "draft_verified" candidate that the
# operator should investigate (the line 465-466 bug surface uncovered
# by M11.0a). The registry-style invariants still hold: ``truth_claim``
# is forced to 0 and ``operator_review_required`` is forced to 1 on
# every persisted row. Attribution rows never feed the verdict path.
# ---------------------------------------------------------------------------


def _row_to_verdict_label_attribution(row) -> dict:
    """SQLite row → attribution dict. Surfaces booleans as Python
    bools and leaves the JSON-encoded ``reconstructed_inputs`` /
    ``weak_evidence_signals`` columns as strings (callers decide
    whether to decode)."""
    if row is None:
        return {}
    out = {k: row[k] for k in row.keys()}
    out["is_weak_evidence_verified"] = bool(
        out.get("is_weak_evidence_verified", 0)
    )
    # truth_claim must always read back False; operator_review_required
    # must always read back True. Defensive re-assertion.
    out["truth_claim"] = bool(out.get("truth_claim", 0))
    out["operator_review_required"] = bool(
        out.get("operator_review_required", 1)
    )
    return out


def save_verdict_label_attribution(attribution_dict: dict) -> int:
    """Persist (or replace, on analysis_id collision) one attribution
    row and return the resulting row id.

    ``attribution_dict`` matches
    ``verdict_label_diagnostic.attribution_to_dict``. Missing fields
    default safely. ``truth_claim`` is always stored as 0 and
    ``operator_review_required`` as 1 regardless of input (defensive
    against future regressions).

    Re-saving the same ``analysis_id`` overwrites the prior row via
    the UNIQUE index on ``analysis_id`` (INSERT OR REPLACE)."""
    if not isinstance(attribution_dict, dict):
        raise ValueError("attribution_dict must be a dict")
    if not attribution_dict.get("analysis_id"):
        raise ValueError("attribution_dict.analysis_id is required")
    if not attribution_dict.get("diagnostic_timestamp"):
        raise ValueError(
            "attribution_dict.diagnostic_timestamp is required"
        )

    def _ensure_text(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row_values = (
        str(attribution_dict.get("analysis_id")),
        attribution_dict.get("stored_verdict_label"),
        attribution_dict.get("stored_verdict_confidence"),
        attribution_dict.get("stored_policy_alert_level"),
        attribution_dict.get("stored_policy_confidence_score"),
        attribution_dict.get("stored_verification_strength"),
        attribution_dict.get("stored_claim_text"),
        attribution_dict.get("stored_evidence_summary"),
        _ensure_text(attribution_dict.get("reconstructed_inputs")),
        attribution_dict.get("attributed_branch_id"),
        attribution_dict.get("attribution_confidence"),
        attribution_dict.get("attribution_reason"),
        1 if attribution_dict.get("is_weak_evidence_verified") else 0,
        _ensure_text(attribution_dict.get("weak_evidence_signals")),
        str(attribution_dict.get("diagnostic_timestamp")),
        attribution_dict.get("notes"),
        # truth_claim is forced to 0 — the registry contract.
        0,
        # operator_review_required is forced to 1 — the diagnostic contract.
        1,
        created_at,
    )
    # M12.0d Stage 3c-3: build the Postgres mirror payload once. INSERT OR
    # REPLACE on the SQLite side maps to ON CONFLICT (analysis_id) DO UPDATE
    # on the PG side.
    mirror_payload = {
        "analysis_id": row_values[0],
        "stored_verdict_label": row_values[1],
        "stored_verdict_confidence": row_values[2],
        "stored_policy_alert_level": row_values[3],
        "stored_policy_confidence_score": row_values[4],
        "stored_verification_strength": row_values[5],
        "stored_claim_text": row_values[6],
        "stored_evidence_summary": row_values[7],
        "reconstructed_inputs": row_values[8],
        "attributed_branch_id": row_values[9],
        "attribution_confidence": row_values[10],
        "attribution_reason": row_values[11],
        "is_weak_evidence_verified": row_values[12],
        "weak_evidence_signals": row_values[13],
        "diagnostic_timestamp": row_values[14],
        "notes": row_values[15],
        "truth_claim": row_values[16],
        "operator_review_required": row_values[17],
        "created_at": row_values[18],
    }

    # M12.0e-5a: Postgres is the sole durable store. PG assigns the id via
    # ON CONFLICT ... RETURNING (mirror_upsert_returning); the function
    # keeps its ``-> int`` contract, returning the sentinel ``-1`` on PG
    # write failure (the 3c-1 data-loss class, surfaced via log.error).
    pg_id = _mirror_upsert_returning_safe(
        "verdict_label_attributions", mirror_payload, ["analysis_id"],
    )
    if pg_id is None:
        log.error(
            "save_verdict_label_attribution PG write returned no id",
            extra={
                "function": "save_verdict_label_attribution",
                "analysis_id": row_values[0],
            },
        )
        return -1
    return pg_id


def get_verdict_label_attributions(
    analysis_id: str = None,
    attributed_branch_id: str = None,
    only_weak_evidence_verified: bool = False,
    limit: int = 100,
) -> list:
    """Return verdict-label attribution rows (newest first), filtered
    by any combination of ``analysis_id``, ``attributed_branch_id``,
    and the ``only_weak_evidence_verified`` flag. ``limit`` is
    clamped to ``[1, 500]``."""
    try:
        capped_limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        capped_limit = 100
    # M12.0c-4 / M12.0d-1: PG primary. PG-read errors raise.
    # M12.0e-6a: the SQLite read path and the dual-write-OFF SQLite
    # fallback were removed; PG is the sole durable store since 0e-5a.
    # OFF → [].
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_verdict_label_attributions,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        log.error(
            "get_verdict_label_attributions failed to import "
            "postgres_storage",
            exc_info=True,
            extra={"function": "get_verdict_label_attributions"},
        )
        raise
    if pg_enabled:
        try:
            pg_rows = read_verdict_label_attributions(
                analysis_id=analysis_id,
                attributed_branch_id=attributed_branch_id,
                only_weak_evidence_verified=only_weak_evidence_verified,
                limit=capped_limit,
            )
        except Exception:
            log.error(
                "get_verdict_label_attributions PG read failed",
                exc_info=True,
                extra={
                    "function": "get_verdict_label_attributions",
                    "analysis_id": analysis_id,
                    "attributed_branch_id": attributed_branch_id,
                    "only_weak_evidence_verified":
                        only_weak_evidence_verified,
                    "limit": capped_limit,
                },
            )
            raise
        if pg_rows is not None:
            return [
                _row_to_verdict_label_attribution(r)
                for r in pg_rows
            ]
        return []
    return []


def embedding_cache_stats() -> dict:
    """Optional diagnostic — total rows + per-provider counts."""
    # M12.0e-1: PG-primary. When dual-write is enabled the durable cache
    # lives in Postgres (SQLite is no longer written), so read the stats
    # from the PG mirror. Falls back to the SQLite body below when
    # dual-write is disabled (local dev / tests).
    from postgres_storage import (
        embedding_cache_stats_pg,
        is_postgres_dual_write_enabled,
    )

    if is_postgres_dual_write_enabled():
        stats = embedding_cache_stats_pg()
        if stats is None:
            return {"available": False, "error": "postgres embedding_cache unavailable"}
        total, per_provider = stats
        return {
            "available": True,
            "total": total,
            "per_provider": per_provider,
        }
    # M12.0e-6a: SQLite read-fallback removed (dual-write OFF → no
    # durable embedding cache; report empty). PG is the sole durable
    # cache since 0e-5a.
    return {"available": True, "total": 0, "per_provider": {}}
