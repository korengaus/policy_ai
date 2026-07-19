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

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import config
from structured_logging import get_logger

# M25a — pgvector typed-vector column type. Imported DEFENSIVELY: when the
# pgvector package is not installed (e.g. local dev / a build without it), the
# app must still import and run on the JSON embedding_cache fallback. ``_Vector``
# stays None and every pgvector path no-ops gracefully.
try:  # pragma: no cover - import availability is environment-dependent
    from pgvector.sqlalchemy import Vector as _Vector
except Exception:  # noqa: BLE001
    _Vector = None

# Default embedding dimensionality for the typed column. text-embedding-3-small
# is 1536. The exact-key lookup M25a uses does not depend on this value; it is
# the declared column width only.
_EMBEDDING_VECTOR_DIM = 1536


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


# M12.0d Stage 3c-1 hotfix — one-shot guard for sequence alignment.
#
# ``ensure_schema`` is invoked once per process, on the first successful
# ``get_engine`` build, so ``_align_serial_sequences`` actually runs on
# Worker / Web startup. Prior to this guard the alignment lived inside
# ``ensure_schema`` but nothing on the hot startup path called it, so
# PG's SERIAL sequence stayed at its post-create value and the first
# nextval() returned id=1 — colliding with rows that M12.0a wrote with
# explicit ids. Reset by ``reset_engine_for_tests`` so test isolation
# is preserved.
_schema_ensured: bool = False


def get_engine() -> Optional[Engine]:
    """Lazy engine creation.

    Returns ``None`` only when dual-write is disabled OR when an
    ``ImportError`` fires (psycopg driver missing on a local dev
    machine that hasn't installed Postgres bindings). All other
    failures (missing ``DATABASE_URL`` when dual-write is enabled,
    SQLAlchemy errors building the engine, unexpected exceptions) now
    raise :class:`PostgresReadError` (M12.0d-2 Stage 2; previously
    swallowed and returned None per Stage 1 deviation #4).

    The engine is cached at module level; call
    :func:`reset_engine_for_tests` to force re-evaluation after env
    vars change.
    """
    global _engine, _schema_ensured
    if not is_postgres_dual_write_enabled():
        return None
    if _engine is not None:
        return _engine
    url = get_database_url()
    if not url:
        log.error(
            "USE_POSTGRES_WRITE=true but DATABASE_URL is empty",
        )
        raise PostgresReadError("DATABASE_URL not set")
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
    except ImportError as exc:
        # Driver (psycopg) not installed — keep the local-dev escape
        # valve so a CI / contributor without Postgres bindings can
        # still run the rest of the test suite.
        log.warning(
            "Postgres driver not installed: %s. Dual-write disabled.",
            exc,
        )
        return None
    except Exception as exc:
        log.error(
            "Failed to create Postgres engine: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"engine creation failed: {exc}"
        ) from exc
    # M12.0d Stage 3c-1 hotfix — run ensure_schema once per process so
    # _align_serial_sequences advances PG's SERIAL past any rows that
    # M12.0a wrote with explicit ids. ensure_schema is idempotent and
    # swallows errors (returns False on failure) so it can never block
    # engine creation. Guarded by _schema_ensured so a dropped+rebuilt
    # engine (after reset_engine_for_tests) re-runs alignment cleanly.
    if not _schema_ensured:
        ensure_schema(_engine)
        _schema_ensured = True
    return _engine


def reset_engine_for_tests() -> None:
    """Test helper: forces the next ``get_engine()`` call to re-evaluate
    env vars. Disposes the cached engine if one exists. Also resets the
    one-shot ``_schema_ensured`` guard so the next build re-runs
    ``ensure_schema`` / sequence alignment."""
    global _engine, _schema_ensured
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    _engine = None
    _schema_ensured = False


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
    # M39a — human-reviewed badge signal (nullable; set only by a manual
    # operator UPDATE, never by the pipeline). create_all does NOT add these
    # to an existing live table, so _ensure_analysis_results_columns (called
    # from ensure_schema after create_all) ALTERs the live table to match
    # this def. NULL until set -> byte-identical when no row is marked.
    sa.Column("human_reviewed_at", sa.Text),
    sa.Column("human_reviewed_by", sa.Text),
    # CLASSIFY-2a — domain category label (nullable METADATA; set by the
    # tool-free domain_classifier at analysis time, never by the verdict path).
    # create_all does NOT add this to an existing live table, so
    # _ensure_analysis_results_columns ALTERs it in. NULL until classified
    # (existing rows stay NULL until the separate 2b backfill).
    sa.Column("domain", sa.Text),
    # NOISE1-A — content-nature category label (nullable METADATA; set by the
    # tool-free content_nature_classifier at analysis time, never by the verdict
    # path). Same additive/idempotent path as ``domain``: create_all does NOT
    # add this to an existing live table, so _ensure_analysis_results_columns
    # ALTERs it in. NULL until classified (OBSERVE mode; existing rows stay NULL
    # until a separate later backfill).
    sa.Column("content_nature", sa.Text),
    # SPREAD-F1B — article publish date promoted out of the debug_summary TEXT
    # blob (nullable METADATA; normalized ISO-8601 UTC TEXT so lexicographic
    # order = chronological, matching created_at — deliberately NOT
    # timestamptz, keeping the SQLite dialect branch safe). Set at save time
    # from the trusted-source article_published_at; never by the verdict path.
    # create_all does NOT add this to an existing live table, so
    # _ensure_analysis_results_columns ALTERs it in. NULL when no trusted
    # publish date exists (existing rows stay NULL until the operator runs
    # scripts/backfill_published_at.py).
    sa.Column("published_at", sa.Text),
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


# M25a — pgvector typed-vector store. Lives in its OWN MetaData (NOT the shared
# ``_metadata`` used by ensure_schema's create_all) so that:
#   * it is never created on the disabled path / when pgvector is absent, and
#   * a vector-table creation failure can NEVER block the main schema's
#     create_all (which would break the whole app).
# It mirrors embedding_cache's key discipline EXACTLY (text_hash, provider,
# model unique) so lookups are 1:1. embedding_cache is preserved as the durable
# JSON fallback; this table is additive.
_vector_metadata = sa.MetaData()
_embedding_vectors_table = None  # lazily built once the Vector type is available


def _build_embedding_vectors_table():
    """Lazily define + cache the embedding_vectors Table. Returns None when the
    pgvector package is unavailable (so callers no-op gracefully)."""
    global _embedding_vectors_table
    if _embedding_vectors_table is not None:
        return _embedding_vectors_table
    if _Vector is None:
        return None
    _embedding_vectors_table = sa.Table(
        "embedding_vectors", _vector_metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("text_hash", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("dimensions", sa.Integer),
        sa.Column("embedding", _Vector(_EMBEDDING_VECTOR_DIM)),
        sa.Column("text_preview", sa.Text),
        sa.Column("created_at", sa.Text),
        sa.UniqueConstraint(
            "text_hash", "provider", "model",
            name="ux_embedding_vectors_lookup",
        ),
    )
    return _embedding_vectors_table


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


# AUTH-2a — account login store. New, additive table (created by
# ensure_schema's create_all; no ALTER needed). Mirrors review_tasks_table's
# declaration idioms (shared _metadata, sa.Text columns, created_at/updated_at).
# Holds ONLY login identity — never any verdict/scoring field. ``role`` ships
# now (default 'admin') so future non-admin users extend the same table; only
# 'admin' exists today. ``password_hash`` stores a bcrypt hash ONLY (see
# accounts.py); the plaintext password is never stored.
accounts_table = sa.Table(
    "accounts", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False, server_default=sa.text("'admin'")),
    sa.Column("created_at", sa.Text),
    sa.Column("updated_at", sa.Text),
)


# HONESTY-GUARD-DB-LOG — APPEND-ONLY observability log for honesty-guard
# violations. New, additive table (created by ensure_schema's create_all; no
# ALTER needed). Mirrors accounts_table's declaration idioms (shared _metadata,
# sa.Text columns, ISO-TEXT created_at).
#
# WHY: the guard's report mode emits ONE stdout logger.warning per violation and
# persists nothing, so "was the last week clean?" cannot be answered once Render
# log retention (~7 days, plan-dependent) rolls over. This table makes the
# question countable BEFORE HONESTY_GUARD_MODE is ever flipped to enforce.
#
# INSERT-ONLY from the app — there is no UPDATE or DELETE code path against it.
# Deliberately stores NO payload, NO violation `detail`, and NO request/user
# identifier: it records exactly what the existing log line already records
# (rule + JSON path + endpoint path), and nothing the log deliberately omits.
# `endpoint` is request.url.path ONLY (never the query string, where PII hides).
# Carries NO verdict/scoring field — this is pure observability metadata.
honesty_violations_table = sa.Table(
    "honesty_violations", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("created_at", sa.Text),
    sa.Column("mode", sa.Text),
    sa.Column("endpoint", sa.Text),
    sa.Column("rule_count", sa.Integer),
    sa.Column("rules_json", sa.Text),
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


# M12.0d Stage 3c-1 — Postgres SERIAL sequence alignment.
#
# After 3c-1 strips the SQLite-assigned id from mirror payloads, PG's
# SERIAL sequences must be at-or-above the current max id of each
# integer-PK mirror table; otherwise the next nextval would reuse an
# existing id and re-trigger the same UniqueViolation we are fixing.
#
# This is idempotent: ``setval(seq, GREATEST(nextval(seq), MAX(id)))``
# advances the sequence forward only — never backwards. It runs once
# per ``ensure_schema`` call (i.e., on app startup) and is a no-op on
# any non-PostgreSQL dialect (the SQLite-as-Postgres test substitute
# has no sequences).
_INT_PK_MIRROR_TABLES: tuple = (
    "analysis_results",
    # AUTH-2a — accounts is an INT-PK mirror table written via mirror_write
    # (id stripped → PG SERIAL owns assignment), so its sequence must be
    # aligned on PostgreSQL alongside the other INT-PK tables.
    "accounts",
    "embedding_cache",
    "source_fetch_artifacts",
    "artifact_text_extractions",
    "artifact_evidence_candidates",
    "verdict_producer_comparisons",
    "verdict_label_attributions",
)


def _align_serial_sequences(engine: Engine) -> None:
    """Advance each INT-PK mirror table's SERIAL sequence past the
    current max id. PostgreSQL-only; no-op on other dialects. Swallows
    per-table errors so one missing table cannot block the others.
    """
    if engine.dialect.name != "postgresql":
        return
    for table_name in _INT_PK_MIRROR_TABLES:
        # Table name interpolated directly (not as a bind param) because
        # PG does not allow parameterising table identifiers. ``table_name``
        # comes from the hardcoded ``_INT_PK_MIRROR_TABLES`` tuple, never
        # from user input, so SQL injection is not in scope here.
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        f"SELECT setval("
                        f"pg_get_serial_sequence(:t, 'id'), "
                        f"(SELECT COALESCE(MAX(id), 1) FROM {table_name}), "
                        f"true"
                        f")"
                    ).bindparams(t=table_name)
                )
        except SQLAlchemyError as exc:
            log.warning(
                "sequence alignment for %s failed: %s", table_name, exc,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "sequence alignment for %s unexpected error: %s",
                table_name, exc,
            )


def ensure_schema(engine: Optional[Engine]) -> bool:
    """Create all mirror tables if they don't exist. Safe to call
    repeatedly. Returns True on success, False on any failure or when
    ``engine`` is None. NEVER raises.

    M12.0d Stage 3c-1: also aligns SERIAL sequences past current max id
    on PostgreSQL so PG-assigned ids cannot collide with pre-existing
    rows after we stop injecting SQLite ids into mirror payloads.
    """
    if engine is None:
        return False
    try:
        _metadata.create_all(engine, checkfirst=True)
    except SQLAlchemyError as exc:
        log.warning("Postgres schema create_all failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("Postgres schema create_all unexpected error: %s", exc)
        return False
    _align_serial_sequences(engine)
    # M39a — additive columns on the live analysis_results table. create_all
    # above only creates MISSING TABLES; it never ALTERs an existing one, so a
    # pre-existing live table won't gain new def columns on its own. This
    # idempotent, dialect-aware step closes that gap so sa.select(table) reads
    # never reference a column the live table lacks.
    _ensure_analysis_results_columns(engine)
    # M25a — pgvector infra, ONLY when gated on. Failures here NEVER affect the
    # main schema's success (separate metadata + caught below); ensure_schema
    # still returns True so the rest of the app runs on the JSON fallback.
    if config.pgvector_enabled():
        _ensure_pgvector(engine)
    return True


# M39a — additive-column list for analysis_results. (name, sql_type) pairs.
# All NULLABLE, no server_default. Extend this tuple to add future additive
# columns to this programmatic (non-Alembic) table via the same idempotent path.
_ANALYSIS_RESULTS_ADDED_COLUMNS: tuple = (
    ("human_reviewed_at", "TEXT"),
    ("human_reviewed_by", "TEXT"),
    # CLASSIFY-2a — domain category label (nullable metadata). Additive ALTER
    # only; _align_serial_sequences / _INT_PK_MIRROR_TABLES are unaffected
    # (additive column, not a new table).
    ("domain", "TEXT"),
    # NOISE1-A — content-nature category label (nullable metadata). Additive
    # ALTER only; _align_serial_sequences / _INT_PK_MIRROR_TABLES unaffected.
    ("content_nature", "TEXT"),
    # SPREAD-F1B — normalized ISO-8601 UTC publish date (nullable metadata).
    # Additive ALTER only; plain TEXT on BOTH dialects (the SQLite branch
    # runs the same DDL fragment).
    ("published_at", "TEXT"),
)


def _analysis_results_existing_columns(conn, dialect: str) -> set:
    """Return the set of column names physically present on the live
    ``analysis_results`` table. Dialect-aware: information_schema on
    PostgreSQL, ``PRAGMA table_info`` on SQLite (the test substitute)."""
    if dialect == "postgresql":
        rows = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'analysis_results'"
            )
        ).all()
        return {row[0] for row in rows}
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    rows = conn.execute(sa.text("PRAGMA table_info(analysis_results)")).all()
    return {row[1] for row in rows}


def _ensure_analysis_results_columns(engine: Optional[Engine]) -> None:
    """Idempotent additive-column step run by :func:`ensure_schema` AFTER
    ``create_all``. Ensures the LIVE ``analysis_results`` table has the M39a
    nullable columns so ``sa.select(analysis_results_table)`` never references
    a column the table lacks.

    Dialect-aware: PostgreSQL uses ``ADD COLUMN IF NOT EXISTS``; SQLite (no
    such clause on ADD COLUMN) checks existence first via PRAGMA. Idempotent
    (no-op when present). NULLABLE, no server_default. NEVER crashes startup —
    a failure is logged (WARNING), not raised and not silently swallowed.

    Column names come from the module-level constant tuple (never user input),
    so the f-string DDL carries no injection surface.
    """
    if engine is None:
        return
    dialect = engine.dialect.name
    try:
        with engine.begin() as conn:
            existing = _analysis_results_existing_columns(conn, dialect)
            for name, col_type in _ANALYSIS_RESULTS_ADDED_COLUMNS:
                if name in existing:
                    continue
                if dialect == "postgresql":
                    conn.execute(
                        sa.text(
                            f"ALTER TABLE analysis_results "
                            f"ADD COLUMN IF NOT EXISTS {name} {col_type}"
                        )
                    )
                else:
                    conn.execute(
                        sa.text(
                            f"ALTER TABLE analysis_results ADD COLUMN {name} {col_type}"
                        )
                    )
                log.info(
                    "ensure_schema: added analysis_results.%s (%s)", name, col_type,
                )
    except SQLAlchemyError as exc:
        log.warning("ensure_schema: analysis_results column ensure failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ensure_schema: analysis_results column ensure unexpected error: %s", exc,
        )


def _ensure_pgvector(engine: Optional[Engine]) -> bool:
    """Create the pgvector extension + embedding_vectors table. Gated by the
    caller on config.pgvector_enabled(). NEVER raises; on ANY failure (package
    missing, role lacks CREATE EXTENSION rights, table create error) it logs a
    WARNING and returns False so the pipeline falls back to embedding_cache."""
    if engine is None:
        return False
    table = _build_embedding_vectors_table()
    if table is None:
        log.warning(
            "pgvector enabled but pgvector package not importable; "
            "falling back to embedding_cache (JSON)",
        )
        return False
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as exc:  # noqa: BLE001 — permission/availability dependent
        log.warning(
            "pgvector CREATE EXTENSION failed (role may lack permission); "
            "falling back to embedding_cache: %s", exc,
        )
        return False
    try:
        table.create(engine, checkfirst=True)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pgvector embedding_vectors table create failed; "
            "falling back to embedding_cache: %s", exc,
        )
        return False
    return True


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


def mirror_write_returning(table_name: str, row_dict: dict) -> Optional[int]:
    """Insert one row into the named Postgres mirror table and return
    the **PG-assigned** integer primary key.

    M12.0d Stage 3c-1: this is the id-authoritative variant of
    :func:`mirror_write`. Any ``id`` key in ``row_dict`` is stripped
    before the INSERT so PG's SERIAL sequence (or the SQLite substitute's
    autoincrement) assigns the id. The returned id is then the durable
    identifier the caller stores in ``jobs.result_id`` and serves to the
    frontend, eliminating the SQLite-id vs PG-id divergence that caused
    the ``UniqueViolation`` on ``analysis_results.id=1``.

    Returns ``None`` when dual-write is disabled, the table is unknown,
    the insert fails, or no primary key was assigned. NEVER raises.
    """
    engine = get_engine()
    if engine is None:
        return None
    table = _metadata.tables.get(table_name)
    if table is None:
        log.warning("mirror_write_returning: unknown table %s", table_name)
        return None
    try:
        filtered = {
            k: v for k, v in _filter_row(table, row_dict).items()
            if k != "id"
        }
        with engine.begin() as conn:
            result = conn.execute(sa.insert(table).values(**filtered))
            pk = result.inserted_primary_key
        if pk is None:
            return None
        try:
            return int(pk[0])
        except (TypeError, ValueError, IndexError):
            return None
    except SQLAlchemyError as exc:
        log.warning(
            "mirror_write_returning %s failed: %s", table_name, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mirror_write_returning %s unexpected error: %s",
            table_name, exc,
        )
        return None


def pg_update_job_fields(job_id: str, fields: dict) -> bool:
    """Update arbitrary columns of ``jobs`` for the given ``job_id``.

    M12.0d Stage 3c-2 helper. Replaces the SQLite-UPDATE + SQLite-re-read +
    mirror_upsert pattern used by ``job_manager.start_job`` /
    ``update_progress`` / ``complete_job`` / ``fail_job``. After 3c-2,
    Postgres is the sole write target for the ``jobs`` table.

    Returns ``True`` on success, ``False`` when dual-write is disabled,
    ``fields`` is empty, or any DB error fires. NEVER raises.
    """
    engine = get_engine()
    if engine is None:
        return False
    if not fields:
        return False
    table = _metadata.tables.get("jobs")
    if table is None:
        log.warning("pg_update_job_fields: jobs table not registered")
        return False
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.update(table)
                .where(table.c.id == job_id)
                .values(**fields)
            )
        return True
    except SQLAlchemyError as exc:
        log.warning(
            "pg_update_job_fields failed for %s: %s", job_id, exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pg_update_job_fields unexpected error for %s: %s",
            job_id, exc,
        )
        return False


def pg_update_review_task_status(
    task_id: str, new_status: str, updated_at: str,
) -> bool:
    """Update ``review_tasks.status`` and ``updated_at`` directly in
    Postgres for the given ``task_id``.

    M12.0d Stage 3c-2 helper. Replaces the SQLite-UPDATE + SQLite-re-read
    + mirror_upsert pattern in ``database.update_review_task_status``.
    After 3c-2, Postgres is the sole write target for the
    ``review_tasks`` table; the re-read pattern (which assumed SQLite
    had a fresh row to mirror) no longer applies.

    Returns ``True`` on success, ``False`` when dual-write is disabled
    or any DB error fires. NEVER raises.
    """
    engine = get_engine()
    if engine is None:
        return False
    table = _metadata.tables.get("review_tasks")
    if table is None:
        log.warning(
            "pg_update_review_task_status: review_tasks table not registered",
        )
        return False
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.update(table)
                .where(table.c.task_id == task_id)
                .values(status=new_status, updated_at=updated_at)
            )
        return True
    except SQLAlchemyError as exc:
        log.warning(
            "pg_update_review_task_status failed for %s: %s",
            task_id, exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pg_update_review_task_status unexpected error for %s: %s",
            task_id, exc,
        )
        return False


def pg_set_analysis_human_review(
    result_id: int, reviewed: bool, reviewer: Optional[str] = None,
) -> bool:
    """Set or clear the M39a human-review columns on ``analysis_results``.

    M40a helper. Mirrors ``pg_update_job_fields`` /
    ``pg_update_review_task_status``: a single parameterized
    ``UPDATE ... WHERE id == :id`` inside ``engine.begin()``. Touches
    ONLY ``human_reviewed_at`` / ``human_reviewed_by`` — never verdict,
    review_status, or any invariant column.

    * ``reviewed=True``  → ``human_reviewed_at`` = current UTC ISO
      timestamp, ``human_reviewed_by`` = ``reviewer`` (default
      ``"operator"`` when None/empty).
    * ``reviewed=False`` → both columns set back to NULL (un-promote).

    Returns ``True`` when a row matched and was updated, ``False`` when
    dual-write is disabled, no row matched the id, or any DB error
    fires. NEVER raises (same contract as the sibling updaters).
    """
    engine = get_engine()
    if engine is None:
        return False
    table = _metadata.tables.get("analysis_results")
    if table is None:
        log.warning(
            "pg_set_analysis_human_review: analysis_results table not registered",
        )
        return False
    if reviewed:
        reviewed_at: Optional[str] = datetime.now(timezone.utc).isoformat(
            timespec="microseconds",
        )
        reviewed_by: Optional[str] = (reviewer or "").strip() or "operator"
    else:
        reviewed_at = None
        reviewed_by = None
    try:
        with engine.begin() as conn:
            result = conn.execute(
                sa.update(table)
                .where(table.c.id == int(result_id))
                .values(
                    human_reviewed_at=reviewed_at,
                    human_reviewed_by=reviewed_by,
                )
            )
        return (result.rowcount or 0) > 0
    except SQLAlchemyError as exc:
        log.warning(
            "pg_set_analysis_human_review failed for %s: %s", result_id, exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pg_set_analysis_human_review unexpected error for %s: %s",
            result_id, exc,
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


def _select_id_by_conflict(
    engine: Engine,
    table: sa.Table,
    filtered: dict,
    conflict_columns: list,
) -> Optional[int]:
    """Return the ``id`` of the row whose conflict-column values match
    ``filtered``, or None when no such row exists / the conflict columns
    are absent from ``filtered``.

    Helper for :func:`mirror_upsert_returning` — used on the SQLite-as-
    Postgres substitute (where RETURNING on the UPDATE branch is
    unreliable) and as the ``on_conflict_do_nothing`` fallback on real
    Postgres. Runs in its own short-lived connection AFTER the upsert
    transaction has committed, so the row is guaranteed visible."""
    where_clauses = [
        table.c[col] == filtered[col]
        for col in conflict_columns
        if col in filtered
    ]
    if not where_clauses:
        return None
    with engine.connect() as conn:
        row_id = conn.execute(
            sa.select(table.c.id).where(*where_clauses).limit(1)
        ).scalar()
    if row_id is None:
        return None
    try:
        return int(row_id)
    except (TypeError, ValueError):
        return None


def mirror_upsert_returning(
    table_name: str,
    row_dict: dict,
    conflict_columns: list,
) -> Optional[int]:
    """``INSERT ... ON CONFLICT DO UPDATE`` that returns the integer
    primary key of the inserted-or-updated row.

    M12.0d Stage 3c-3: the id-authoritative variant of
    :func:`mirror_upsert`, used by ``database.save_producer_comparison``
    (conflict on ``input_hash``) and
    ``database.save_verdict_label_attribution`` (conflict on
    ``analysis_id``) once those tables become PG-only writes. Any ``id``
    key in ``row_dict`` is stripped before the statement so PG's SERIAL
    sequence (or the SQLite substitute's autoincrement) owns id
    assignment.

    On a real Postgres engine the ``RETURNING id`` clause yields the id
    for BOTH the insert branch and the on-conflict-update branch. On the
    SQLite-as-Postgres test substitute (non-postgresql dialect) RETURNING
    on the UPDATE branch is unreliable across SQLAlchemy versions, so the
    id is recovered with a follow-up ``SELECT id ... WHERE <conflict
    cols>`` via :func:`_select_id_by_conflict`.

    Returns ``None`` when dual-write is disabled, the table is unknown,
    ``conflict_columns`` is empty, the operation fails, or no id could be
    determined. NEVER raises.
    """
    engine = get_engine()
    if engine is None:
        return None
    table = _metadata.tables.get(table_name)
    if table is None:
        log.warning("mirror_upsert_returning: unknown table %s", table_name)
        return None
    if not conflict_columns:
        # An upsert without a conflict target degenerates to a plain
        # insert — call mirror_write_returning instead. Defensive: return
        # None so the caller surfaces the bug.
        log.warning(
            "mirror_upsert_returning %s called with empty conflict_columns",
            table_name,
        )
        return None
    try:
        filtered = {
            k: v for k, v in _filter_row(table, row_dict).items()
            if k != "id"
        }
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
            stmt = stmt.returning(table.c.id)
            with engine.begin() as conn:
                new_id = conn.execute(stmt).scalar()
            if new_id is not None:
                return int(new_id)
            # on_conflict_do_nothing yields no RETURNING row when the row
            # already exists — recover the existing id by conflict match.
            return _select_id_by_conflict(
                engine, table, filtered, conflict_columns,
            )
        # Non-Postgres dialect — exercised by the SQLite-as-Postgres
        # substitute in the test suite. INSERT-or-UPDATE inside one
        # transaction, then SELECT the id (RETURNING on the UPDATE branch
        # is unreliable here). Mirrors mirror_upsert's substitute path.
        with engine.begin() as conn:
            inserted = False
            try:
                conn.execute(sa.insert(table).values(**filtered))
                inserted = True
            except SQLAlchemyError:
                # Likely UNIQUE conflict — fall through to UPDATE.
                inserted = False
            if not inserted:
                update_values = {
                    k: v
                    for k, v in filtered.items()
                    if k not in set(conflict_columns) and k != "id"
                }
                where_clauses = [
                    table.c[col] == filtered[col]
                    for col in conflict_columns
                    if col in filtered
                ]
                if update_values and where_clauses:
                    conn.execute(
                        sa.update(table).where(*where_clauses).values(
                            **update_values,
                        ),
                    )
        return _select_id_by_conflict(
            engine, table, filtered, conflict_columns,
        )
    except SQLAlchemyError as exc:
        log.warning(
            "mirror_upsert_returning %s failed: %s", table_name, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mirror_upsert_returning %s unexpected error: %s",
            table_name, exc,
        )
        return None


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


# PERF-2/PERF-4 — slim list projection. The homepage card list (GET /history)
# only consumes lightweight scalars + a few small JSON columns
# (debug_summary, source_reliability_summary) + the small claims array.
# PERF-4: source_candidates was DROPPED here. PERF-2 had kept it on the
# assumption the card derived hasDirectOfficialSupport from it, but PERF-3
# measured it at ~94% of the payload (~1.2MB/row) and PERF-4 proved the card
# never reads it — that boolean comes from buildOfficialEvidenceState, which
# reads only source_reliability_summary + debug_summary (both still kept). So
# dropping it is byte-identical for cards with zero frontend change.
# The heavy JSON columns (evidence_snippets / evidence_sources /
# claim_evidence_map / contradiction_* / bias_framing_* / normalized_claims
# / source_queries / missing_context / evidence_extraction_summary) blow the
# response up to ~16MB for 50 rows; the card path never reads them, and the
# DETAIL view re-fetches the full row via the unchanged GET /history/{id}.
# This reader is ADDITIVE — read_recent_analysis_results stays whole-row for
# its existing callers/tests. Same contract: None when engine unavailable,
# [] when authoritative-zero, raise PostgresReadError on SQL/engine error.
_SLIM_LIST_COLUMNS = (
    "id", "query", "title", "original_url", "topic", "domain",
    "content_nature",
    "policy_alert_level", "market_signal", "policy_confidence_score",
    "verification_strength", "risk_level", "action_priority",
    "impact_level", "impact_direction",
    "market_sensitivity", "consumer_sensitivity", "business_sensitivity",
    "claim_text", "verdict_label", "verdict_confidence",
    "source_reliability_score", "source_reliability_reason", "evidence_summary",
    "last_checked_at", "review_status", "created_at",
    "human_reviewed_at", "human_reviewed_by",
    "source_reliability_summary", "debug_summary", "claims",
)


def read_recent_analysis_results_slim(
    limit: int = 20, domain: Optional[str] = None,
) -> Optional[list]:
    """Like :func:`read_recent_analysis_results` but SELECTs only the
    lightweight columns the homepage card list needs (see
    ``_SLIM_LIST_COLUMNS``), dropping the heavy JSON body columns.

    Identical semantics to the whole-row reader:

    * None → engine not built (dual-write disabled / URL missing).
    * ``[]`` → Postgres is authoritative and says zero rows.
    * Raises :class:`PostgresReadError` on engine / SQL errors.

    Limit is clamped to ``[1, 100]`` exactly like the whole-row reader so
    the contract stays identical between paths.
    """
    engine = get_engine()
    if engine is None:
        return None
    safe_limit = max(1, min(int(limit or 20), 100))
    columns = [analysis_results_table.c[name] for name in _SLIM_LIST_COLUMNS]
    try:
        with engine.connect() as conn:
            stmt = sa.select(*columns)
            # STABLE-TABS S1: optional domain-scoped feed. Added ONLY when
            # `domain` is truthy, so the no-domain 전체 path stays byte-identical.
            # A domain value absent from the table yields an empty result (no
            # error). Read-only filter on the display-metadata `domain` column —
            # never a verdict field.
            if domain:
                stmt = stmt.where(analysis_results_table.c.domain == domain)
            rows = conn.execute(
                stmt
                .order_by(analysis_results_table.c.id.desc())
                .limit(safe_limit)
            ).all()
        return [dict(row._mapping) for row in rows]
    except SQLAlchemyError as exc:
        log.error(
            "read_recent_analysis_results_slim failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_recent_analysis_results_slim failed: {exc}"
        ) from exc


def read_weekly_verification_stats(cutoff_iso: str) -> Optional[dict]:
    """SIDEBAR-RANK-B2 — read-only weekly counts for the homepage sidebar's
    "이번 주 검증 현황" panel. No write, no verdict path, no schema change.

    Counts analysis_results rows with ``created_at >= cutoff_iso`` (created_at is
    stored as ISO-8601 TEXT, so a lexicographic ``>=`` is a correct time window).

    Returns ``{"total": int, "official": int, "cumulative_total": int}`` where
    ``official`` reuses the
    PERSISTED ``source_reliability_summary["has_genuine_official_support"]``
    boolean (NOT a re-derived predicate), with the SAME old-row fallback the
    frontend uses (``debug_summary.official_body_matches > 0``). The boolean
    lives inside the source_reliability_summary JSON text, so it is parsed in
    Python — no Postgres-only ``::jsonb`` cast (keeps the SQLite test path
    portable). The week's row volume is small (~100s), so a window SELECT +
    Python count is cheap.

    MOBILE-POLISH B — ``cumulative_total`` is the UNBOUNDED corpus ``COUNT(*)``
    (no window, no filter), for the header banner's "누적 검증" figure. Pure row
    metadata: no verdict path, no honesty predicate, no schema change. Counted in
    SQL rather than in Python because — unlike the 7-day window — the full table
    must never be pulled into memory.

    Contract mirrors the slim reader: ``None`` → engine not built; a dict
    (possibly zeroed) when Postgres is authoritative; raises on SQL error.
    """
    engine = get_engine()
    if engine is None:
        return None
    t = analysis_results_table.c
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    t.source_reliability_summary,
                    t.debug_summary,
                ).where(t.created_at >= cutoff_iso)
            ).all()
            cumulative_total = conn.execute(
                sa.select(sa.func.count()).select_from(analysis_results_table)
            ).scalar()
    except SQLAlchemyError as exc:
        log.error(
            "read_weekly_verification_stats failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_weekly_verification_stats failed: {exc}"
        ) from exc

    total = len(rows)
    official = 0
    for row in rows:
        summary = _safe_json_obj(row[0])
        genuine = summary.get("has_genuine_official_support")
        if not isinstance(genuine, bool):
            # Old-row fallback (mirrors officialStatusLabel in main.js): a real
            # body-sentence match. Reads debug_summary.official_body_matches > 0.
            debug = _safe_json_obj(row[1])
            try:
                genuine = int(debug.get("official_body_matches") or 0) > 0
            except (TypeError, ValueError):
                genuine = False
        if genuine:
            official += 1
    return {
        "total": total,
        "official": official,
        "cumulative_total": int(cumulative_total or 0),
    }


def _safe_json_obj(value) -> dict:
    """Parse a JSON-text column into a dict; return {} on null/blank/non-dict
    or malformed JSON. Used by the read-only weekly-stats counter."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


def read_account_by_username(username: str) -> Optional[dict]:
    """Return the accounts row for ``username`` as a RAW dict, or None when
    the engine is unbuilt / the row is missing. ``username`` is UNIQUE so at
    most one row matches. Mirrors read_review_task_by_task_id's shape.

    Raises :class:`PostgresReadError` on engine / SQL errors."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(accounts_table).where(
                    accounts_table.c.username == username
                )
            ).first()
        return dict(row._mapping) if row is not None else None
    except SQLAlchemyError as exc:
        log.error(
            "read_account_by_username failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_account_by_username failed: {exc}"
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


def read_job_status(job_id: str) -> Optional[str]:
    """Return just the ``status`` column for ``job_id`` (PG-primary).

    M12.0d Stage 3b: idempotency-guard read path for
    :func:`job_manager._current_status`. Thin wrapper over
    :func:`read_job_by_id` — reuses the engine / error-handling /
    not-found semantics. Returns None when the row is missing or the
    engine is unavailable; raises :class:`PostgresReadError` on real
    SQL errors (inherited from :func:`read_job_by_id`)."""
    row = read_job_by_id(job_id)
    if row is None:
        return None
    return row.get("status")


# ---------------------------------------------------------------------------
# Read helper — M12.0d-2 (embedding cache).
#
# Used by ``database.get_cached_embedding`` so the Web and Worker
# services on Render share a single embedding cache instead of each
# rebuilding their own SQLite cache from scratch after every restart.
# Embedding writes have been mirrored into PG since M12.0a; the read
# side is what M12.0d-2 wires up.
#
# Same contract as the other M12.0c/d read helpers:
#   * Return the parsed vector as ``list[float]`` when the row exists.
#   * Return None when the engine is not built OR no cache row matches
#     the (text_hash, provider, model) tuple — caller treats this as a
#     legitimate cache miss and computes a fresh embedding.
#   * Raise :class:`PostgresReadError` on real engine / SQL errors.
# ---------------------------------------------------------------------------


def read_cached_embedding(
    text_hash: str, provider: str, model: str,
) -> Optional[list]:
    """Return the cached vector for ``(text_hash, provider, model)`` or
    None on miss / engine not built.

    Raises :class:`PostgresReadError` on engine / SQL errors (M12.0d-2)."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    embedding_cache_table.c.vector_json,
                    embedding_cache_table.c.dimensions,
                ).where(
                    embedding_cache_table.c.text_hash == text_hash,
                    embedding_cache_table.c.provider == provider,
                    embedding_cache_table.c.model == (model or ""),
                ).limit(1)
            ).first()
        if row is None:
            return None
        vector_json = row._mapping.get("vector_json")
        if not vector_json:
            return None
    except SQLAlchemyError as exc:
        log.error(
            "read_cached_embedding failed: %s", exc, exc_info=True,
        )
        raise PostgresReadError(
            f"read_cached_embedding failed: {exc}"
        ) from exc
    # Decode + validate outside the engine context so JSON errors don't
    # masquerade as SQL errors. A corrupted cache row is best-effort
    # treated as a miss; we DO NOT raise here because the caller can
    # always recompute the embedding.
    try:
        vector = json.loads(vector_json)
    except (TypeError, ValueError):
        log.warning(
            "read_cached_embedding row had unreadable vector_json; "
            "treating as cache miss",
        )
        return None
    if not isinstance(vector, list):
        return None
    if not all(isinstance(v, (int, float)) for v in vector):
        return None
    return [float(v) for v in vector]


def read_cached_embedding_vector(
    text_hash: str, provider: str, model: str,
) -> Optional[list]:
    """M25a — read a cached vector from the typed embedding_vectors table.

    Returns the vector (list[float]) on hit, or None on miss / when pgvector is
    disabled / unavailable / the table doesn't exist. NEVER raises — the caller
    falls back to the JSON embedding_cache, so this is best-effort only."""
    if not config.pgvector_enabled():
        return None
    table = _build_embedding_vectors_table()
    if table is None:
        return None
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(table.c.embedding).where(
                    table.c.text_hash == text_hash,
                    table.c.provider == provider,
                    table.c.model == (model or ""),
                ).limit(1)
            ).first()
        if row is None:
            return None
        raw = row._mapping.get("embedding")
        if raw is None:
            return None
        # pgvector returns a numpy array / sequence; normalize to list[float].
        vector = [float(v) for v in list(raw)]
        return vector or None
    except Exception as exc:  # noqa: BLE001 — best-effort; treat as miss
        log.warning("read_cached_embedding_vector failed (treating as miss): %s", exc)
        return None


def upsert_embedding_vector(
    *,
    text_hash: str,
    provider: str,
    model: str,
    dimensions: int,
    embedding: list,
    text_preview: str = "",
    created_at: str = "",
) -> bool:
    """M25a — best-effort write of a vector into the typed embedding_vectors
    table (INSERT ... ON CONFLICT DO UPDATE on the unique key, mirroring
    embedding_cache). Returns True on success, False on any failure / when
    pgvector is disabled or unavailable. NEVER raises — embedding_cache remains
    the durable copy."""
    if not config.pgvector_enabled():
        return False
    if not text_hash or not provider or not isinstance(embedding, (list, tuple)) or not embedding:
        return False
    table = _build_embedding_vectors_table()
    if table is None:
        return False
    engine = get_engine()
    if engine is None:
        return False
    row = {
        "text_hash": text_hash,
        "provider": provider,
        "model": model or "",
        "dimensions": int(dimensions or len(embedding)),
        "embedding": list(embedding),
        "text_preview": (text_preview or "")[:200],
        "created_at": created_at or "",
    }
    try:
        if engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(table).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=["text_hash", "provider", "model"],
                set_={
                    "dimensions": stmt.excluded.dimensions,
                    "embedding": stmt.excluded.embedding,
                    "text_preview": stmt.excluded.text_preview,
                    "created_at": stmt.excluded.created_at,
                },
            )
            with engine.begin() as conn:
                conn.execute(stmt)
            return True
        # Non-Postgres (test substitute): plain insert, swallow conflicts.
        with engine.begin() as conn:
            try:
                conn.execute(sa.insert(table).values(**row))
            except SQLAlchemyError:
                pass
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("upsert_embedding_vector failed: %s", exc)
        return False


def embedding_cache_stats_pg():
    """Return ``(total, per_provider)`` for the Postgres embedding_cache
    mirror, or ``None`` when the engine is not built / dual-write is off.

    M12.0e-1: PG-primary counterpart to ``database.embedding_cache_stats``
    so the diagnostic still works when SQLite is no longer written. This
    is a PURE DIAGNOSTIC — it uses the SOFT failure contract (returns
    ``None`` on any SQL error, NEVER raises ``PostgresReadError``), mirroring
    the SQLite version's ``{"available": False, ...}`` behaviour rather than
    the Stage-1 raise-on-read contract."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            total = conn.execute(
                sa.select(sa.func.count()).select_from(embedding_cache_table)
            ).scalar()
            rows = conn.execute(
                sa.select(
                    embedding_cache_table.c.provider,
                    sa.func.count().label("n"),
                )
                .group_by(embedding_cache_table.c.provider)
                .order_by(sa.func.count().desc())
            ).all()
    except SQLAlchemyError as exc:
        log.warning("embedding_cache_stats_pg failed: %s", exc)
        return None
    per_provider = {
        row._mapping["provider"]: int(row._mapping["n"] or 0) for row in rows
    }
    return int(total or 0), per_provider


# ---------------------------------------------------------------------------
# Diagnostic helper — read-only, used by scripts/check_postgres_health.py.
# ---------------------------------------------------------------------------


def health_check() -> dict:
    """Returns a stable status dict for diagnostic CLI use. NEVER raises.

    M12.0d-2: ``get_engine`` now raises ``PostgresReadError`` on
    configuration / SQLAlchemy failures (Stage 1 deviation #4 fix).
    The operator-facing health probe still must not raise, so the
    engine call is wrapped here — any raise becomes a populated
    ``error`` field with ``engine_available=False``.
    """
    enabled = is_postgres_dual_write_enabled()
    url_present = bool(get_database_url())
    engine: Optional[Engine] = None
    error: Optional[str] = None
    if enabled:
        try:
            engine = get_engine()
        except PostgresReadError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — health probe must not raise
            error = str(exc)
    can_connect = False
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
