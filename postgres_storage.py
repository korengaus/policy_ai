"""Postgres dual-write foundation (M12.0a).

Behaviour:
- When the ``USE_POSTGRES_WRITE`` env var is ``"true"``, every supported
  write function in :mod:`database` mirrors its data into Postgres in
  parallel with SQLite.
- When the env var is ``"false"`` (default) or unset, this module is a
  no-op and no Postgres connection is attempted.
- ``DATABASE_URL`` is required only when ``USE_POSTGRES_WRITE`` is
  ``"true"``.
- Any Postgres failure is LOGGED and SWALLOWED. SQLite is the source of
  truth; Postgres failures must never break the SQLite write path or
  the caller's flow.

Read paths are NOT changed by this module. All reads continue from
SQLite via :mod:`database`.

This module is the foundation for the subsequent M12.0 sub-phases:

* M12.0b — backfill existing rows.
* M12.0c — switch reads to Postgres.
* M12.0d — retire SQLite.

Until M12.0d, the schema defined here MUST stay in sync with the SQLite
schema in :mod:`database`. Adding or removing a column on either side
requires updating both files in the same change. See
``docs/POSTGRES_MIGRATION.md``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from structured_logging import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Stage 1 (M12.0d-1) — read-helper exception type.
#
# Read helpers previously swallowed every SQLAlchemy / unexpected error
# and returned ``None`` so callers in database.py / job_manager.py
# could silently fall back to SQLite. That contract masked real PG
# outages and schema drift. After M12.0d-1, read helpers raise
# :class:`PostgresReadError` (wrapping the underlying cause) on engine
# / driver / SQL errors. ``None`` and ``[]`` returns are now reserved
# for legitimate "row not present" / "zero rows" outcomes only.
#
# Callers in database.py wrap the read in their own ``try / except
# Exception`` and re-raise after logging with their own function-name
# context, so a bare ``Exception`` subclass is sufficient — no need
# for a finer hierarchy.
# ---------------------------------------------------------------------------


class PostgresReadError(Exception):
    """Raised by ``read_*`` helpers when a real engine / SQL error
    fires. ``None`` / ``[]`` returns now mean "no row" only."""


# ---------------------------------------------------------------------------
# Feature-flag helpers
# ---------------------------------------------------------------------------


def is_postgres_dual_write_enabled() -> bool:
    """Returns True iff env var ``USE_POSTGRES_WRITE`` equals ``"true"``
    (case-insensitive, leading/trailing whitespace stripped). Any other
    value — including unset, empty string, ``"false"``, ``"0"``,
    ``"no"`` — returns False.
    """
    return os.environ.get("USE_POSTGRES_WRITE", "").strip().lower() == "true"


def get_database_url() -> Optional[str]:
    """Returns the ``DATABASE_URL`` env var (stripped) or ``None``.
    Required when dual-write is enabled; the engine refuses to build
    without it.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    return url or None


# ---------------------------------------------------------------------------
# Engine — lazy, cached at module level, never built on import.
# ---------------------------------------------------------------------------


_engine: Optional[Engine] = None


def get_engine() -> Optional[Engine]:
    """Lazy engine creation.

    Returns ``None`` when dual-write is disabled or ``DATABASE_URL`` is
    missing. The engine is cached at module level; call
    :func:`reset_engine_for_tests` to force re-evaluation after env vars
    change.
    """
    global _engine
    if not is_postgres_dual_write_enabled():
        return None
    if _engine is not None:
        return _engine
    url = get_database_url()
    if not url:
        log.warning(
            "USE_POSTGRES_WRITE=true but DATABASE_URL is empty; "
            "dual-write disabled."
        )
        return None
    try:
        # pool_pre_ping handles dropped connections gracefully on
        # Render free-tier sleeping instances. Pool kept small because
        # dual-write is a side channel — the SQLite path owns concurrency.
        _engine = sa.create_engine(
            url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
            future=True,
        )
        return _engine
    except Exception as exc:  # noqa: BLE001 — never propagate
        log.warning(
            "Failed to create Postgres engine: %s. Dual-write disabled.",
            exc,
        )
        return None


def reset_engine_for_tests() -> None:
    """Test helper: forces the next ``get_engine()`` call to re-evaluate
    env vars. Disposes the cached engine if one exists."""
    global _engine
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    _engine = None


# ---------------------------------------------------------------------------
# Schema definitions — mirror every SQLite table from ``database.py``.
#
# Column NAMES and the set of UNIQUE constraints MUST stay in sync with
# the SQLite schema. Postgres types are kept loose on purpose: TEXT for
# anything stored as text in SQLite (including JSON-encoded TEXT
# columns), INTEGER for integer columns (including SQLite-style booleans
# stored as 0/1), REAL for floats. This keeps the schemas trivially
# comparable and avoids any type-coercion surprises during M12.0b
# backfill.
# ---------------------------------------------------------------------------


_metadata: sa.MetaData = sa.MetaData()


analysis_results_table = sa.Table(
    "analysis_results", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("query", sa.Text),
    sa.Column("title", sa.Text),
    sa.Column("original_url", sa.Text),
    sa.Column("topic", sa.Text),
    sa.Column("policy_alert_level", sa.Text),
    sa.Column("market_signal", sa.Text),
    sa.Column("policy_confidence_score", sa.Integer),
    sa.Column("verification_strength", sa.Text),
    sa.Column("risk_level", sa.Text),
    sa.Column("action_priority", sa.Text),
    sa.Column("impact_level", sa.Text),
    sa.Column("impact_direction", sa.Text),
    sa.Column("market_sensitivity", sa.Integer),
    sa.Column("consumer_sensitivity", sa.Integer),
    sa.Column("business_sensitivity", sa.Integer),
    sa.Column("claim_text", sa.Text),
    sa.Column("verdict_label", sa.Text),
    sa.Column("verdict_confidence", sa.Integer),
    sa.Column("evidence_sources", sa.Text),
    sa.Column("source_reliability_score", sa.Integer),
    sa.Column("source_reliability_reason", sa.Text),
    sa.Column("evidence_summary", sa.Text),
    sa.Column("missing_context", sa.Text),
    sa.Column("last_checked_at", sa.Text),
    sa.Column("review_status", sa.Text),
    sa.Column("claims", sa.Text),
    sa.Column("normalized_claims", sa.Text),
    sa.Column("source_candidates", sa.Text),
    sa.Column("source_queries", sa.Text),
    sa.Column("source_reliability_summary", sa.Text),
    sa.Column("evidence_snippets", sa.Text),
    sa.Column("claim_evidence_map", sa.Text),
    sa.Column("evidence_extraction_summary", sa.Text),
    sa.Column("contradiction_checks", sa.Text),
    sa.Column("contradiction_summary", sa.Text),
    sa.Column("bias_framing_analysis", sa.Text),
    sa.Column("bias_framing_summary", sa.Text),
    sa.Column("debug_summary", sa.Text),
    sa.Column("created_at", sa.Text),
)


jobs_table = sa.Table(
    "jobs", _metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("query", sa.Text),
    sa.Column("max_news", sa.Integer),
    sa.Column("progress_percent", sa.Integer, server_default=sa.text("0")),
    sa.Column("current_stage", sa.Text),
    sa.Column("result_id", sa.Integer),
    sa.Column("error_message", sa.Text),
    sa.Column("created_at", sa.Text),
    sa.Column("started_at", sa.Text),
    sa.Column("completed_at", sa.Text),
    sa.Column("pipeline_version", sa.Text),
)


embedding_cache_table = sa.Table(
    "embedding_cache", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("text_hash", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text),
    sa.Column("dimensions", sa.Integer),
    sa.Column("vector_json", sa.Text, nullable=False),
    sa.Column("text_preview", sa.Text),
    sa.Column("created_at", sa.Text),
    sa.UniqueConstraint(
        "text_hash", "provider", "model",
        name="ux_embedding_cache_lookup",
    ),
)


review_tasks_table = sa.Table(
    "review_tasks", _metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("result_id", sa.Text),
    sa.Column("job_id", sa.Text),
    sa.Column("item_index", sa.Integer, server_default=sa.text("0")),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("query", sa.Text),
    sa.Column("claim_text", sa.Text),
    sa.Column("title", sa.Text),
    sa.Column("url", sa.Text),
    sa.Column("final_decision", sa.Text),
    sa.Column("policy_confidence", sa.Text),
    sa.Column("human_review_required", sa.Integer, server_default=sa.text("1")),
    sa.Column("snapshot_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text, unique=True),
)


review_decisions_table = sa.Table(
    "review_decisions", _metadata,
    sa.Column("decision_id", sa.Text, primary_key=True),
    sa.Column("task_id", sa.Text, nullable=False),
    sa.Column("decision", sa.Text, nullable=False),
    sa.Column("reviewer_id", sa.Text),
    sa.Column("comment", sa.Text),
    sa.Column("public_note", sa.Text),
    sa.Column("previous_status", sa.Text),
    sa.Column("new_status", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("metadata_json", sa.Text),
    sa.Column("decision_source", sa.Text),
)


source_fetch_artifacts_table = sa.Table(
    "source_fetch_artifacts", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("fetch_timestamp", sa.Text, nullable=False),
    sa.Column("status_code", sa.Integer),
    sa.Column("content_type", sa.Text),
    sa.Column("success", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("error", sa.Text),
    sa.Column("text_content", sa.Text),
    sa.Column("raw_html", sa.Text),
    sa.Column("fetch_duration_ms", sa.Integer),
    sa.Column("truth_claim", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "official_source_candidate",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column("created_at", sa.Text, nullable=False),
)


artifact_text_extractions_table = sa.Table(
    "artifact_text_extractions", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("artifact_id", sa.Integer, nullable=False),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("extraction_timestamp", sa.Text, nullable=False),
    sa.Column("extraction_duration_ms", sa.Integer),
    sa.Column("success", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("error", sa.Text),
    sa.Column("title", sa.Text),
    sa.Column("main_text", sa.Text),
    sa.Column("sections", sa.Text),
    sa.Column("word_count", sa.Integer),
    sa.Column("language_hint", sa.Text),
    sa.Column("truth_claim", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "official_source_candidate",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column("created_at", sa.Text, nullable=False),
)


artifact_evidence_candidates_table = sa.Table(
    "artifact_evidence_candidates", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("extraction_id", sa.Integer, nullable=False),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("analysis_id", sa.Text, nullable=False),
    sa.Column("claim_text", sa.Text, nullable=False),
    sa.Column("match_score", sa.Float, nullable=False, server_default=sa.text("0.0")),
    sa.Column("matched_tokens", sa.Text),
    sa.Column("supporting_passage", sa.Text),
    sa.Column("candidate_timestamp", sa.Text, nullable=False),
    sa.Column("truth_claim", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "official_source_candidate",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "operator_review_required",
        sa.Integer,
        nullable=False,
        server_default=sa.text("1"),
    ),
    sa.Column("notes", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
)


verdict_producer_comparisons_table = sa.Table(
    "verdict_producer_comparisons", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("analysis_id", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("input_hash", sa.Text, nullable=False, unique=True),
    sa.Column("producer1_label", sa.Text),
    sa.Column("producer1_score", sa.Float),
    sa.Column("producer1_extra", sa.Text),
    sa.Column("producer2_label", sa.Text),
    sa.Column("producer2_alert_level", sa.Text),
    sa.Column("producer2_score", sa.Float),
    sa.Column("producer2_extra", sa.Text),
    sa.Column("producer3_label", sa.Text),
    sa.Column("producer3_extra", sa.Text),
    sa.Column(
        "all_three_agree",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column(
        "p1_p2_agree", sa.Integer, nullable=False, server_default=sa.text("0"),
    ),
    sa.Column(
        "p1_p3_agree", sa.Integer, nullable=False, server_default=sa.text("0"),
    ),
    sa.Column(
        "p2_p3_agree", sa.Integer, nullable=False, server_default=sa.text("0"),
    ),
    sa.Column("disagreement_pattern", sa.Text),
    sa.Column("most_conservative_label", sa.Text),
    sa.Column("comparison_timestamp", sa.Text, nullable=False),
    sa.Column("notes", sa.Text),
    sa.Column("truth_claim", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "operator_review_required",
        sa.Integer,
        nullable=False,
        server_default=sa.text("1"),
    ),
    sa.Column("created_at", sa.Text, nullable=False),
)


verdict_label_attributions_table = sa.Table(
    "verdict_label_attributions", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("analysis_id", sa.Text, nullable=False, unique=True),
    sa.Column("stored_verdict_label", sa.Text),
    sa.Column("stored_verdict_confidence", sa.Integer),
    sa.Column("stored_policy_alert_level", sa.Text),
    sa.Column("stored_policy_confidence_score", sa.Integer),
    sa.Column("stored_verification_strength", sa.Text),
    sa.Column("stored_claim_text", sa.Text),
    sa.Column("stored_evidence_summary", sa.Text),
    sa.Column("reconstructed_inputs", sa.Text),
    sa.Column("attributed_branch_id", sa.Text),
    sa.Column("attribution_confidence", sa.Text),
    sa.Column("attribution_reason", sa.Text),
    sa.Column(
        "is_weak_evidence_verified",
        sa.Integer,
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column("weak_evidence_signals", sa.Text),
    sa.Column("diagnostic_timestamp", sa.Text, nullable=False),
    sa.Column("notes", sa.Text),
    sa.Column("truth_claim", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column(
        "operator_review_required",
        sa.Integer,
        nullable=False,
        server_default=sa.text("1"),
    ),
    sa.Column("created_at", sa.Text, nullable=False),
)


# Public registry of every mirror table. Used by ``ensure_schema``,
# ``health_check``, and the test suite's parity assertions.
MIRROR_TABLE_NAMES: tuple = tuple(sorted(_metadata.tables.keys()))


def ensure_schema(engine: Optional[Engine]) -> bool:
    """Create all mirror tables if they don't exist. Safe to call
    repeatedly. Returns True on success, False on any failure or when
    ``engine`` is None. NEVER raises.
    """
    if engine is None:
        return False
    try:
        _metadata.create_all(engine, checkfirst=True)
        return True
    except SQLAlchemyError as exc:
        log.warning("Postgres schema create_all failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("Postgres schema create_all unexpected error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Dual-write helpers — must NEVER raise.
# ---------------------------------------------------------------------------


def _filter_row(table: sa.Table, row_dict: dict) -> dict:
    """Drop keys that aren't columns of ``table``. Defensive against
    callers passing extra fields (e.g. derived booleans surfaced by
    ``_row_to_*`` helpers that aren't real columns)."""
    cols = {c.name for c in table.columns}
    return {k: v for k, v in row_dict.items() if k in cols}


def mirror_write(table_name: str, row_dict: dict) -> bool:
    """Insert one row into the named Postgres mirror table.

    Returns ``True`` on success, ``False`` when dual-write is disabled,
    the table is unknown, or any database error fires. NEVER raises.
    """
    engine = get_engine()
    if engine is None:
        return False
    table = _metadata.tables.get(table_name)
    if table is None:
        log.warning("mirror_write: unknown table %s", table_name)
        return False
    try:
        filtered = _filter_row(table, row_dict)
        with engine.begin() as conn:
            conn.execute(sa.insert(table).values(**filtered))
        return True
    except SQLAlchemyError as exc:
        log.warning("mirror_write %s failed: %s", table_name, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mirror_write %s unexpected error: %s", table_name, exc,
        )
        return False


def mirror_upsert(
    table_name: str,
    row_dict: dict,
    conflict_columns: list,
) -> bool:
    """Postgres-flavoured ``INSERT ... ON CONFLICT DO UPDATE`` for
    tables that use UNIQUE constraints in SQLite (e.g.
    ``review_tasks.idempotency_key``,
    ``verdict_producer_comparisons.input_hash``,
    ``verdict_label_attributions.analysis_id``).

    The PG path uses ``sqlalchemy.dialects.postgresql.insert``; when the
    engine is a non-Postgres dialect (e.g. the SQLite-backed test
    substitute used by integration tests), the helper falls back to a
    plain ``INSERT`` followed by a same-transaction ``UPDATE`` on the
    conflict columns so the test harness can exercise the upsert path
    without standing up a real Postgres server.

    Returns ``True`` on success, ``False`` on any failure or when
    dual-write is disabled. NEVER raises.
    """
    engine = get_engine()
    if engine is None:
        return False
    table = _metadata.tables.get(table_name)
    if table is None:
        log.warning("mirror_upsert: unknown table %s", table_name)
        return False
    if not conflict_columns:
        # An upsert without a conflict target degenerates to a plain
        # insert — call mirror_write instead. Defensive: return False so
        # the caller surfaces the bug rather than silently inserting.
        log.warning(
            "mirror_upsert %s called with empty conflict_columns",
            table_name,
        )
        return False
    try:
        filtered = _filter_row(table, row_dict)
        dialect = engine.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(table).values(**filtered)
            # On conflict, update everything except the conflict keys
            # themselves and the synthetic ``id`` column.
            excluded = set(conflict_columns) | {"id"}
            update_cols = {
                k: stmt.excluded[k]
                for k in filtered.keys()
                if k not in excluded
            }
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(conflict_columns),
                    set_=update_cols,
                )
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=list(conflict_columns),
                )
            with engine.begin() as conn:
                conn.execute(stmt)
            return True
        # Non-Postgres dialect — exercised only by the SQLite-as-Postgres
        # substitute in the test suite. INSERT-then-UPDATE inside one
        # transaction approximates ON CONFLICT DO UPDATE closely enough
        # for the test invariants (final row state, idempotency).
        with engine.begin() as conn:
            try:
                conn.execute(sa.insert(table).values(**filtered))
            except SQLAlchemyError:
                # Likely UNIQUE conflict — fall through to UPDATE.
                pass
            else:
                return True
            update_values = {
                k: v
                for k, v in filtered.items()
                if k not in set(conflict_columns) and k != "id"
            }
            if update_values:
                where_clauses = [
                    table.c[col] == filtered[col]
                    for col in conflict_columns
                    if col in filtered
                ]
                if where_clauses:
                    conn.execute(
                        sa.update(table).where(*where_clauses).values(
                            **update_values,
                        ),
                    )
        return True
    except SQLAlchemyError as exc:
        log.warning("mirror_upsert %s failed: %s", table_name, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mirror_upsert %s unexpected error: %s", table_name, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Read helpers — M12.0c-minimal (semantic updated in M12.0d-1).
#
# Used by ``database.get_result_by_id`` / ``get_recent_results`` when
# dual-write is enabled, so the Web service on Render can see rows
# the Worker process wrote (the two services run on separate
# ephemeral filesystems and never share a SQLite file).
#
# M12.0d-1 (Stage 1 of staged SQLite-fallback removal) narrowed the
# exception contract:
#
#   * ``None`` (single-row helpers) → row not present in PG (or engine
#     not built because dual-write is disabled / URL missing). NOT an
#     error signal anymore — callers treat as "not found".
#   * ``[]`` (list helpers) → PG has zero matching rows
#     (authoritative). Unchanged from M12.0c.
#   * Real engine / SQL errors now RAISE :class:`PostgresReadError`
#     instead of being swallowed. The caller in database.py logs +
#     re-raises so PG failures surface instead of silently falling
#     back to a (possibly stale) SQLite row.
# ---------------------------------------------------------------------------


def read_analysis_result_by_id(result_id: int) -> Optional[dict]:
    """Return the analysis_results row as a dict, or None when:
    - dual-write is disabled (no engine),
    - the row is not present in Postgres.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1).
    """
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(analysis_results_table).where(
                    analysis_results_table.c.id == int(result_id)
                )
            ).first()
        return dict(row._mapping) if row is not None else None
    except SQLAlchemyError as exc:
        log.error(
            "read_analysis_result_by_id failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_analysis_result_by_id failed: {exc}"
        ) from exc


def read_recent_analysis_results(limit: int = 20) -> Optional[list]:
    """Return the newest analysis_results rows as a list of dicts, or
    None when the engine is unavailable.

    * None → engine not built (dual-write disabled / URL missing).
    * ``[]`` → Postgres is authoritative and says zero rows.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1).

    Limit is clamped to ``[1, 100]`` to match the SQLite-side helper
    so the contract stays identical between paths.
    """
    engine = get_engine()
    if engine is None:
        return None
    safe_limit = max(1, min(int(limit or 20), 100))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(analysis_results_table)
                .order_by(analysis_results_table.c.id.desc())
                .limit(safe_limit)
            ).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error(
            "read_recent_analysis_results failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_recent_analysis_results failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Read helpers — M12.0c-2 (reviewer dashboard path; semantic updated in M12.0d-1).
#
# These mirror the M12.0c-minimal pattern for the reviewer dashboard
# read functions in database.py:
#
#   - get_review_task / get_review_task_by_idempotency_key
#   - list_review_tasks
#   - get_review_decision / list_review_decisions
#
# Same contract as the M12.0c-minimal helpers above (post-M12.0d-1):
#
#   * Return None (single-row helpers) when dual-write is disabled or
#     the row is missing. NOT an error signal anymore.
#   * Return ``[]`` (list helpers) when Postgres has zero matching
#     rows — authoritative.
#   * Raise :class:`PostgresReadError` on real engine / SQL errors.
#   * Return RAW dicts (``dict(row._mapping)``) without applying the
#     SQLite-side ``_row_to_review_task`` / ``_row_to_review_decision``
#     normalizations. Those live in database.py and the wrapper there
#     applies them to both SQLite Rows and PG raw dicts (both are
#     duck-typed for ``[k]`` + ``keys()``). Keeping the normalization
#     out of postgres_storage avoids a forbidden import of database
#     (pinned by test_does_not_import_database_module).
# ---------------------------------------------------------------------------


def read_review_task_by_task_id(task_id: str) -> Optional[dict]:
    """Return the review_tasks row for ``task_id`` as a RAW dict, or
    None when dual-write is disabled or the row is missing.
    ``task_id`` is the SQLite PRIMARY KEY column.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(review_tasks_table).where(
                    review_tasks_table.c.task_id == task_id
                )
            ).first()
        return dict(row._mapping) if row is not None else None
    except SQLAlchemyError as exc:
        log.error(
            "read_review_task_by_task_id failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_review_task_by_task_id failed: {exc}"
        ) from exc


def read_review_task_by_idempotency_key(
    idempotency_key: str,
) -> Optional[dict]:
    """Return the review_tasks row for ``idempotency_key`` as a RAW
    dict, or None. UNIQUE on the PG side guarantees at most one row.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(review_tasks_table).where(
                    review_tasks_table.c.idempotency_key == idempotency_key
                )
            ).first()
        return dict(row._mapping) if row is not None else None
    except SQLAlchemyError as exc:
        log.error(
            "read_review_task_by_idempotency_key failed: %s",
            exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_review_task_by_idempotency_key failed: {exc}"
        ) from exc


def read_review_tasks(
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Optional[list]:
    """Return review_tasks rows as RAW dicts, newest first. ``limit``
    clamped to ``[1, 100]``, ``offset`` clamped to ``[0, ∞)`` —
    identical to the SQLite-side ``list_review_tasks`` contract.

    Returns ``[]`` when Postgres has zero matching rows (authoritative);
    None when the engine is unavailable.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1).

    Sort order matches SQLite: ``ORDER BY created_at DESC, task_id DESC``.
    """
    engine = get_engine()
    if engine is None:
        return None
    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))
    try:
        stmt = sa.select(review_tasks_table)
        if status:
            stmt = stmt.where(review_tasks_table.c.status == status)
        stmt = (
            stmt.order_by(
                review_tasks_table.c.created_at.desc(),
                review_tasks_table.c.task_id.desc(),
            )
            .limit(safe_limit)
            .offset(safe_offset)
        )
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error("read_review_tasks failed: %s", exc, exc_info=True)
        raise PostgresReadError(
            f"read_review_tasks failed: {exc}"
        ) from exc


def read_review_decision_by_id(decision_id: str) -> Optional[dict]:
    """Return the review_decisions row for ``decision_id`` as a RAW
    dict, or None on engine miss / missing row.
    ``decision_id`` is the SQLite PRIMARY KEY column.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(review_decisions_table).where(
                    review_decisions_table.c.decision_id == decision_id
                )
            ).first()
        return dict(row._mapping) if row is not None else None
    except SQLAlchemyError as exc:
        log.error(
            "read_review_decision_by_id failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_review_decision_by_id failed: {exc}"
        ) from exc


def read_review_decisions_for_task(task_id: str) -> Optional[list]:
    """Return review_decisions rows for ``task_id`` as RAW dicts,
    oldest first (``ORDER BY created_at ASC, decision_id ASC``) so the
    append-only history reads in occurrence order — matches SQLite.

    Returns ``[]`` when Postgres has zero rows for the task
    (authoritative); None when engine miss.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1).
    """
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(review_decisions_table)
                .where(review_decisions_table.c.task_id == task_id)
                .order_by(
                    review_decisions_table.c.created_at.asc(),
                    review_decisions_table.c.decision_id.asc(),
                )
            ).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error(
            "read_review_decisions_for_task failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_review_decisions_for_task failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Read helpers — M12.0c-3 (duplicate INSERT prevention).
#
# Used by ``database.result_exists_by_url`` and
# ``database.get_result_id_by_url`` to prevent duplicate
# ``analysis_results`` rows when the Web service is asked to save a URL
# that the Worker has already persisted (Web and Worker have separate
# ephemeral filesystems on Render and never share SQLite).
#
# Asymmetric None semantics:
#
#   * ``read_analysis_result_exists_by_url`` returns Optional[bool]:
#       - True  → PG has at least one matching row (authoritative).
#       - False → PG authoritatively has zero matching rows; caller
#                 MUST trust this and NOT fall back to SQLite. Same
#                 ``[]`` = PG truth contract as M12.0c-2.
#       - None  → engine unavailable (dual-write disabled / URL
#                 missing). Real errors RAISE PostgresReadError.
#
#   * ``read_analysis_result_id_by_url`` returns Optional[int]:
#       - int   → the latest analysis_results.id matching the URL.
#       - None  → engine miss OR row missing (conflated, same as the
#                 M12.0c-minimal ``read_analysis_result_by_id`` helper).
#
# M12.0d-1: real engine / SQL errors now raise :class:`PostgresReadError`
# instead of being swallowed into a None return.
# ---------------------------------------------------------------------------


def read_analysis_result_exists_by_url(
    original_url: str,
) -> Optional[bool]:
    """True / False AUTHORITATIVELY when the engine is reachable; None
    on engine miss (dual-write disabled / URL missing). See module
    docstring above for the full semantics — True AND False are both
    PG-authoritative.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        # SELECT 1 ... LIMIT 1 — we only need to know whether any row
        # exists, not pull the (potentially large) row. ``.scalar()``
        # returns the literal 1 on a hit or None on a miss; we coerce
        # to bool so the contract returns True / False explicitly.
        stmt = (
            sa.select(sa.literal(1))
            .select_from(analysis_results_table)
            .where(analysis_results_table.c.original_url == original_url)
            .limit(1)
        )
        with engine.connect() as conn:
            hit = conn.execute(stmt).scalar()
        return hit is not None
    except SQLAlchemyError as exc:
        log.error(
            "read_analysis_result_exists_by_url failed: %s",
            exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_analysis_result_exists_by_url failed: {exc}"
        ) from exc


def read_analysis_result_id_by_url(
    original_url: str,
) -> Optional[int]:
    """Return the most recent ``analysis_results.id`` for ``original_url``
    (``ORDER BY id DESC LIMIT 1``), or None when:
      * dual-write disabled (no engine),
      * no matching row in Postgres.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1).

    None conflates 'no row' with 'engine miss' — the caller in
    database.py handles both as 'no row found'."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        stmt = (
            sa.select(analysis_results_table.c.id)
            .where(analysis_results_table.c.original_url == original_url)
            .order_by(analysis_results_table.c.id.desc())
            .limit(1)
        )
        with engine.connect() as conn:
            row_id = conn.execute(stmt).scalar()
        if row_id is None:
            return None
        return int(row_id)
    except SQLAlchemyError as exc:
        log.error(
            "read_analysis_result_id_by_url failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_analysis_result_id_by_url failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Read helpers — M12.0c-4 (operator CLI tables; semantic updated in M12.0d-1).
#
# These mirror the M12.0c-minimal / M12.0c-2 pattern for the five
# operator-facing read functions in database.py:
#
#   - get_fetch_artifacts            (source_fetch_artifacts)
#   - get_extraction_results         (artifact_text_extractions)
#   - get_evidence_candidates        (artifact_evidence_candidates)
#   - get_producer_comparisons       (verdict_producer_comparisons)
#   - get_verdict_label_attributions (verdict_label_attributions)
#
# Contract (identical across all five; post-M12.0d-1):
#
#   * Return ``[]`` when Postgres has zero matching rows — AUTHORITATIVE.
#     The caller in database.py MUST treat ``[]`` and None differently:
#       - None → engine unavailable (dual-write disabled / URL missing).
#       - ``[]`` → PG is authoritative and says zero rows; trust it.
#   * Return RAW dicts (``dict(row._mapping)``) without applying the
#     SQLite-side ``_row_to_*`` normalizations. Those live in
#     database.py and the wrapper there applies them to both SQLite
#     Rows and PG raw dicts (both are duck-typed).
#   * Raise :class:`PostgresReadError` on real engine / SQL errors
#     (M12.0d-1). Callers in database.py log + re-raise so PG failures
#     surface instead of silently leaking stale SQLite rows.
#   * Filter args are keyword-only. Truthy guards on free-text filters
#     so a passed ``""`` does not produce a ``WHERE col = ''`` clause
#     (matches the SQLite-side ``if analysis_id is not None and
#     str(analysis_id):`` guard).
#   * No ``db_path`` arg — these helpers always read the default
#     Postgres engine. The caller in database.py is responsible for
#     skipping the helper entirely when an explicit ``db_path`` was
#     passed (CLI's ``--db-path`` opts into a specific SQLite file).
# ---------------------------------------------------------------------------


def read_fetch_artifacts(
    *,
    source_id: Optional[str] = None,
    limit: int = 50,
) -> Optional[list]:
    """source_fetch_artifacts rows, newest first. ``limit`` clamped
    to ``[1, 500]``."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        safe_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        safe_limit = 50
    try:
        stmt = sa.select(source_fetch_artifacts_table)
        if source_id:
            stmt = stmt.where(
                source_fetch_artifacts_table.c.source_id == str(source_id),
            )
        stmt = stmt.order_by(
            source_fetch_artifacts_table.c.fetch_timestamp.desc(),
            source_fetch_artifacts_table.c.id.desc(),
        ).limit(safe_limit)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error("read_fetch_artifacts failed: %s", exc, exc_info=True)
        raise PostgresReadError(
            f"read_fetch_artifacts failed: {exc}"
        ) from exc


def read_extraction_results(
    *,
    source_id: Optional[str] = None,
    artifact_id: Optional[int] = None,
    limit: int = 50,
) -> Optional[list]:
    """artifact_text_extractions rows, newest first. ``limit``
    clamped to ``[1, 500]``."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        safe_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        safe_limit = 50
    try:
        stmt = sa.select(artifact_text_extractions_table)
        if source_id:
            stmt = stmt.where(
                artifact_text_extractions_table.c.source_id == str(source_id),
            )
        if artifact_id is not None:
            try:
                stmt = stmt.where(
                    artifact_text_extractions_table.c.artifact_id
                    == int(artifact_id),
                )
            except (TypeError, ValueError):
                pass
        stmt = stmt.order_by(
            artifact_text_extractions_table.c.extraction_timestamp.desc(),
            artifact_text_extractions_table.c.id.desc(),
        ).limit(safe_limit)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error("read_extraction_results failed: %s", exc, exc_info=True)
        raise PostgresReadError(
            f"read_extraction_results failed: {exc}"
        ) from exc


def read_evidence_candidates(
    *,
    analysis_id: Optional[str] = None,
    source_id: Optional[str] = None,
    extraction_id: Optional[int] = None,
    limit: int = 50,
) -> Optional[list]:
    """artifact_evidence_candidates rows, newest first. ``limit``
    clamped to ``[1, 500]``."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        safe_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        safe_limit = 50
    try:
        stmt = sa.select(artifact_evidence_candidates_table)
        if analysis_id is not None and str(analysis_id):
            stmt = stmt.where(
                artifact_evidence_candidates_table.c.analysis_id
                == str(analysis_id),
            )
        if source_id:
            stmt = stmt.where(
                artifact_evidence_candidates_table.c.source_id
                == str(source_id),
            )
        if extraction_id is not None:
            try:
                stmt = stmt.where(
                    artifact_evidence_candidates_table.c.extraction_id
                    == int(extraction_id),
                )
            except (TypeError, ValueError):
                pass
        stmt = stmt.order_by(
            artifact_evidence_candidates_table.c.candidate_timestamp.desc(),
            artifact_evidence_candidates_table.c.id.desc(),
        ).limit(safe_limit)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error("read_evidence_candidates failed: %s", exc, exc_info=True)
        raise PostgresReadError(
            f"read_evidence_candidates failed: {exc}"
        ) from exc


def read_producer_comparisons(
    *,
    analysis_id: Optional[str] = None,
    disagreement_pattern: Optional[str] = None,
    only_disagreements: bool = False,
    limit: int = 50,
) -> Optional[list]:
    """verdict_producer_comparisons rows, newest first. ``limit``
    clamped to ``[1, 500]``.

    ``only_disagreements=True`` maps to ``all_three_agree == 0`` per
    the SQLite-side semantics (PG mirror also stores the bool as INT)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        safe_limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        safe_limit = 50
    try:
        stmt = sa.select(verdict_producer_comparisons_table)
        if analysis_id is not None and str(analysis_id):
            stmt = stmt.where(
                verdict_producer_comparisons_table.c.analysis_id
                == str(analysis_id),
            )
        if disagreement_pattern:
            stmt = stmt.where(
                verdict_producer_comparisons_table.c.disagreement_pattern
                == str(disagreement_pattern),
            )
        if only_disagreements:
            stmt = stmt.where(
                verdict_producer_comparisons_table.c.all_three_agree == 0,
            )
        stmt = stmt.order_by(
            verdict_producer_comparisons_table.c.comparison_timestamp.desc(),
            verdict_producer_comparisons_table.c.id.desc(),
        ).limit(safe_limit)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error("read_producer_comparisons failed: %s", exc, exc_info=True)
        raise PostgresReadError(
            f"read_producer_comparisons failed: {exc}"
        ) from exc


def read_verdict_label_attributions(
    *,
    analysis_id: Optional[str] = None,
    attributed_branch_id: Optional[str] = None,
    only_weak_evidence_verified: bool = False,
    limit: int = 100,
) -> Optional[list]:
    """verdict_label_attributions rows, newest first. ``limit``
    clamped to ``[1, 500]`` (default 100 to match the SQLite-side
    helper).

    ``only_weak_evidence_verified=True`` maps to
    ``is_weak_evidence_verified == 1`` per the SQLite-side semantics."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        safe_limit = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        safe_limit = 100
    try:
        stmt = sa.select(verdict_label_attributions_table)
        if analysis_id is not None and str(analysis_id):
            stmt = stmt.where(
                verdict_label_attributions_table.c.analysis_id
                == str(analysis_id),
            )
        if attributed_branch_id:
            stmt = stmt.where(
                verdict_label_attributions_table.c.attributed_branch_id
                == str(attributed_branch_id),
            )
        if only_weak_evidence_verified:
            stmt = stmt.where(
                verdict_label_attributions_table.c.is_weak_evidence_verified
                == 1,
            )
        stmt = stmt.order_by(
            verdict_label_attributions_table.c.diagnostic_timestamp.desc(),
            verdict_label_attributions_table.c.id.desc(),
        ).limit(safe_limit)
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error(
            "read_verdict_label_attributions failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_verdict_label_attributions failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Read helper — M12.0c-jobs (jobs table).
#
# Used by ``job_manager.get_job_status`` so the Web service can see job
# progress that the Worker has written. Mirror-write of jobs rows is
# wired in job_manager.py (paired write+read milestone, unlike the
# earlier M12.0c sub-milestones which only added the read side on top
# of M12.0a writes).
#
# Same contract as the other M12.0c read helpers (post-M12.0d-1):
#   * Return RAW dict (caller adds the ``job_id`` alias on top of
#     ``id``) or None when:
#       - dual-write is disabled (no engine),
#       - the row is not present in Postgres.
#   * Raise :class:`PostgresReadError` on real engine / SQL errors
#     (M12.0d-1).
# ---------------------------------------------------------------------------


def read_job_by_id(job_id: str) -> Optional[dict]:
    """Return the jobs row for ``job_id`` as a RAW dict, or None.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-1)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(jobs_table).where(
                    jobs_table.c.id == str(job_id)
                )
            ).first()
        return dict(row._mapping) if row is not None else None
    except SQLAlchemyError as exc:
        log.error("read_job_by_id failed: %s", exc, exc_info=True)
        raise PostgresReadError(
            f"read_job_by_id failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Diagnostic helper — read-only, used by scripts/check_postgres_health.py.
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """Returns a stable status dict for diagnostic CLI use. NEVER raises."""
    enabled = is_postgres_dual_write_enabled()
    url_present = bool(get_database_url())
    engine = get_engine() if enabled else None
    can_connect = False
    error: Optional[str] = None
    if engine is not None:
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            can_connect = True
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    return {
        "dual_write_enabled": enabled,
        "database_url_present": url_present,
        "engine_available": engine is not None,
        "can_connect": can_connect,
        "error": error,
        "tables_defined": list(MIRROR_TABLE_NAMES),
    }
