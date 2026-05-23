"""Postgres backfill (M12.0b).

Reads existing rows from SQLite and inserts them into Postgres using the
M12.0a :mod:`postgres_storage` helpers. Idempotent: re-running is safe.

Behaviour:

* Requires ``USE_POSTGRES_WRITE=true`` AND ``DATABASE_URL`` set. When
  either is missing the public functions still return cleanly — they
  populate :class:`BackfillResult.errors` and the CLI prints an
  operator-friendly message.
* For tables with UNIQUE constraints (``review_tasks.idempotency_key``,
  ``verdict_producer_comparisons.input_hash``,
  ``verdict_label_attributions.analysis_id``,
  ``embedding_cache.(text_hash, provider, model)``), the backfill uses
  :func:`postgres_storage.mirror_upsert`.
* For tables without UNIQUE constraints, the backfill uses
  :func:`postgres_storage.mirror_write` but skips rows whose ``id``
  already exists in Postgres — idempotency via primary-key check.
* Per-row errors are captured in :class:`BackfillResult.errors` and
  counted; one bad row never aborts the run.
* The backfill is **read-only** against SQLite. The script never
  modifies the SQLite database under any circumstance.

The backfill is incremental-safe: re-running picks up new rows added to
SQLite since the last run. This is the foundation for M12.0c (read
switch) which requires Postgres to be in sync with SQLite at switch
time.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import database
import postgres_storage


log = logging.getLogger(__name__)


# Documented idempotency strategies. Anything else is rejected at spec
# construction time by :func:`get_backfill_specs`.
IDEMPOTENCY_STRATEGIES = (
    "skip_existing_id",
    "skip_existing_unique",
    "upsert_by_columns",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BackfillSpec:
    """Defines how to backfill one table."""

    table_name: str
    sqlite_reader: Callable[[], list]
    idempotency_strategy: str
    conflict_columns: list = field(default_factory=list)
    row_transformer: Optional[Callable[[dict], dict]] = None


@dataclass
class BackfillResult:
    """Outcome of backfilling one table."""

    table_name: str
    rows_read: int = 0
    rows_inserted: int = 0
    rows_skipped_existing: int = 0
    rows_errored: int = 0
    errors: list = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Private SQLite readers — raw SELECT *, dict-form, no transformation.
#
# The public ``database.get_*`` helpers are limit-capped (50-500 rows)
# and several of them call ``_row_to_*`` transformations that drop or
# rename columns (e.g. ``_row_to_review_task`` removes ``snapshot_json``
# and adds ``snapshot``). For backfill we need every row, untransformed,
# so the mirror schema receives an exact copy of the SQLite source.
#
# Each reader is fully defensive: when the table does not exist yet
# (e.g. on a fresh checkout where ``init_db`` was never called) the
# reader returns an empty list rather than raising.
# ---------------------------------------------------------------------------


def _read_all_rows(table_name: str) -> list:
    """SQLite ``SELECT * FROM <table>``. Returns a list of plain dicts
    with the native sqlite3 column values (no bool coercion, no JSON
    inflation). Empty list if the table is missing."""
    try:
        with database.get_connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table_name}"
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        log.warning(
            "SQLite read of %s failed (table missing?): %s",
            table_name, exc,
        )
        return []


def _count_rows(table_name: str) -> int:
    """SQLite ``SELECT COUNT(*) FROM <table>``. Returns 0 on missing
    table (defensive for fresh checkouts)."""
    try:
        with database.get_connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM {table_name}"
            ).fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _read_analysis_results() -> list:
    return _read_all_rows("analysis_results")


def _read_jobs() -> list:
    return _read_all_rows("jobs")


def _read_embedding_cache() -> list:
    return _read_all_rows("embedding_cache")


def _read_review_tasks() -> list:
    return _read_all_rows("review_tasks")


def _read_review_decisions() -> list:
    return _read_all_rows("review_decisions")


def _read_source_fetch_artifacts() -> list:
    return _read_all_rows("source_fetch_artifacts")


def _read_artifact_text_extractions() -> list:
    return _read_all_rows("artifact_text_extractions")


def _read_artifact_evidence_candidates() -> list:
    return _read_all_rows("artifact_evidence_candidates")


def _read_verdict_producer_comparisons() -> list:
    return _read_all_rows("verdict_producer_comparisons")


def _read_verdict_label_attributions() -> list:
    return _read_all_rows("verdict_label_attributions")


# Map of table_name → reader. Exposed so the CLI's --status mode can
# count rows without resorting to the spec list.
TABLE_READERS = {
    "analysis_results": _read_analysis_results,
    "jobs": _read_jobs,
    "embedding_cache": _read_embedding_cache,
    "review_tasks": _read_review_tasks,
    "review_decisions": _read_review_decisions,
    "source_fetch_artifacts": _read_source_fetch_artifacts,
    "artifact_text_extractions": _read_artifact_text_extractions,
    "artifact_evidence_candidates": _read_artifact_evidence_candidates,
    "verdict_producer_comparisons": _read_verdict_producer_comparisons,
    "verdict_label_attributions": _read_verdict_label_attributions,
}


# ---------------------------------------------------------------------------
# Spec catalogue
# ---------------------------------------------------------------------------


def get_backfill_specs() -> list:
    """Returns the list of :class:`BackfillSpec` for the 10 M12.0a mirror
    tables.

    Order is stable but not semantically load-bearing — the mirror
    schema declares no FK constraints, so the rows are independent. We
    list the parent-ish tables first (analysis_results, jobs) before
    children (review_tasks references result_id/job_id at the data
    level even though no FK is declared).
    """
    return [
        BackfillSpec(
            table_name="analysis_results",
            sqlite_reader=_read_analysis_results,
            idempotency_strategy="skip_existing_id",
        ),
        BackfillSpec(
            table_name="jobs",
            sqlite_reader=_read_jobs,
            idempotency_strategy="skip_existing_unique",
            conflict_columns=["id"],
        ),
        BackfillSpec(
            table_name="embedding_cache",
            sqlite_reader=_read_embedding_cache,
            idempotency_strategy="upsert_by_columns",
            conflict_columns=["text_hash", "provider", "model"],
        ),
        BackfillSpec(
            table_name="review_tasks",
            sqlite_reader=_read_review_tasks,
            idempotency_strategy="upsert_by_columns",
            conflict_columns=["idempotency_key"],
        ),
        BackfillSpec(
            table_name="review_decisions",
            sqlite_reader=_read_review_decisions,
            idempotency_strategy="skip_existing_unique",
            conflict_columns=["decision_id"],
        ),
        BackfillSpec(
            table_name="source_fetch_artifacts",
            sqlite_reader=_read_source_fetch_artifacts,
            idempotency_strategy="skip_existing_id",
        ),
        BackfillSpec(
            table_name="artifact_text_extractions",
            sqlite_reader=_read_artifact_text_extractions,
            idempotency_strategy="skip_existing_id",
        ),
        BackfillSpec(
            table_name="artifact_evidence_candidates",
            sqlite_reader=_read_artifact_evidence_candidates,
            idempotency_strategy="skip_existing_id",
        ),
        BackfillSpec(
            table_name="verdict_producer_comparisons",
            sqlite_reader=_read_verdict_producer_comparisons,
            idempotency_strategy="upsert_by_columns",
            conflict_columns=["input_hash"],
        ),
        BackfillSpec(
            table_name="verdict_label_attributions",
            sqlite_reader=_read_verdict_label_attributions,
            idempotency_strategy="upsert_by_columns",
            conflict_columns=["analysis_id"],
        ),
    ]


# ---------------------------------------------------------------------------
# Postgres-side identity probes (read-only).
# ---------------------------------------------------------------------------


def _existing_ids_in_postgres(engine: Optional[Engine], table_name: str) -> set:
    """Returns the set of ``id`` values already in the Postgres mirror.
    Empty set on any error so the caller treats unknown state as "try
    every row" — the mirror_write/upsert path is still idempotent
    enough that this is safe."""
    if engine is None:
        return set()
    table = postgres_storage._metadata.tables.get(table_name)
    if table is None or "id" not in table.c:
        return set()
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.select(table.c.id)).all()
        return {row[0] for row in rows if row[0] is not None}
    except SQLAlchemyError as exc:
        log.warning(
            "_existing_ids_in_postgres %s failed: %s", table_name, exc,
        )
        return set()


def _existing_unique_keys_in_postgres(
    engine: Optional[Engine],
    table_name: str,
    key_columns: list,
) -> set:
    """Returns the set of tuples for the given unique-key columns
    already present in Postgres. Used by ``skip_existing_unique``."""
    if engine is None or not key_columns:
        return set()
    table = postgres_storage._metadata.tables.get(table_name)
    if table is None:
        return set()
    try:
        cols = [table.c[k] for k in key_columns]
    except KeyError as exc:
        log.warning(
            "_existing_unique_keys_in_postgres %s missing column %s",
            table_name, exc,
        )
        return set()
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.select(*cols)).all()
        return {tuple(row) for row in rows}
    except SQLAlchemyError as exc:
        log.warning(
            "_existing_unique_keys_in_postgres %s failed: %s",
            table_name, exc,
        )
        return set()


def _count_rows_in_postgres(
    engine: Optional[Engine], table_name: str,
) -> int:
    """Read-only COUNT(*). Returns 0 on any error (including the table
    not existing yet)."""
    if engine is None:
        return 0
    table = postgres_storage._metadata.tables.get(table_name)
    if table is None:
        return 0
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sa.select(sa.func.count()).select_from(table)
            ).scalar()
        return int(result or 0)
    except SQLAlchemyError as exc:
        log.warning(
            "_count_rows_in_postgres %s failed: %s", table_name, exc,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_count_rows_in_postgres %s unexpected error: %s",
            table_name, exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Backfill executors
# ---------------------------------------------------------------------------


def backfill_table(
    spec: BackfillSpec,
    dry_run: bool = True,
    limit: Optional[int] = None,
) -> BackfillResult:
    """Backfill one table according to its spec. NEVER raises.

    When ``dry_run`` is True, no Postgres write is performed but the
    counts reflect what WOULD have been written. Skip counts are
    accurate in either mode (the Postgres-side identity probe runs in
    both).
    """
    result = BackfillResult(table_name=spec.table_name, dry_run=dry_run)
    start = time.time()

    if not postgres_storage.is_postgres_dual_write_enabled():
        result.errors.append(
            "USE_POSTGRES_WRITE is not 'true'; backfill cannot run"
        )
        result.duration_seconds = time.time() - start
        return result

    engine = postgres_storage.get_engine()
    if engine is None:
        result.errors.append(
            "Postgres engine unavailable; check DATABASE_URL"
        )
        result.duration_seconds = time.time() - start
        return result

    # Schema must exist before we probe / write. ensure_schema is
    # idempotent and silently no-ops on already-present tables.
    postgres_storage.ensure_schema(engine)

    # Read existing identity set for idempotency. Even in dry-run mode
    # we probe so the "would-insert" count is honest.
    existing_ids: set = set()
    existing_unique: set = set()
    if spec.idempotency_strategy == "skip_existing_id":
        existing_ids = _existing_ids_in_postgres(engine, spec.table_name)
    elif spec.idempotency_strategy == "skip_existing_unique":
        existing_unique = _existing_unique_keys_in_postgres(
            engine, spec.table_name, spec.conflict_columns,
        )

    # Read SQLite rows.
    try:
        rows = spec.sqlite_reader()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"sqlite_reader failed: {exc}")
        result.duration_seconds = time.time() - start
        return result

    if limit is not None and limit >= 0:
        rows = rows[:limit]
    result.rows_read = len(rows)

    for row in rows:
        # Apply optional transformer (never used by default specs; the
        # raw SQLite rows already match the mirror schema column names).
        if spec.row_transformer is not None:
            try:
                row = spec.row_transformer(row)
            except Exception as exc:  # noqa: BLE001
                result.rows_errored += 1
                result.errors.append(
                    f"row_transformer error on id={row.get('id')}: {exc}"
                )
                continue

        row_id = row.get("id")

        # Idempotency check (runs in dry-run mode too).
        if spec.idempotency_strategy == "skip_existing_id":
            if row_id is not None and row_id in existing_ids:
                result.rows_skipped_existing += 1
                continue
        elif spec.idempotency_strategy == "skip_existing_unique":
            key = tuple(row.get(c) for c in spec.conflict_columns)
            if key in existing_unique:
                result.rows_skipped_existing += 1
                continue

        if dry_run:
            # "Would insert" — record but do not write. Upsert tables
            # would also touch existing rows; we report all such writes
            # under inserted to keep the report compact. The skip
            # counters above already isolate truly-unchanged rows.
            result.rows_inserted += 1
            continue

        if spec.idempotency_strategy == "upsert_by_columns":
            ok = postgres_storage.mirror_upsert(
                spec.table_name, row, list(spec.conflict_columns),
            )
        else:
            ok = postgres_storage.mirror_write(spec.table_name, row)

        if ok:
            result.rows_inserted += 1
            # When the strategy is skip_existing_id, augment the
            # in-memory cache so a row inserted on this run is also
            # considered "existing" if it somehow appears again later in
            # the same batch (defensive against duplicate ids in the
            # source — which shouldn't happen, but mirrors the contract).
            if (
                spec.idempotency_strategy == "skip_existing_id"
                and row_id is not None
            ):
                existing_ids.add(row_id)
            elif spec.idempotency_strategy == "skip_existing_unique":
                existing_unique.add(
                    tuple(row.get(c) for c in spec.conflict_columns)
                )
        else:
            result.rows_errored += 1
            result.errors.append(f"mirror failed for id={row_id}")

    result.duration_seconds = time.time() - start
    return result


def backfill_all_tables(
    dry_run: bool = True,
    limit: Optional[int] = None,
    only_table: Optional[str] = None,
) -> list:
    """Run backfill for all specs (or just one if ``only_table`` is
    given). Returns a list of :class:`BackfillResult`. NEVER raises."""
    specs = get_backfill_specs()
    if only_table is not None:
        specs = [s for s in specs if s.table_name == only_table]
    results = []
    for spec in specs:
        try:
            r = backfill_table(spec, dry_run=dry_run, limit=limit)
        except Exception as exc:  # noqa: BLE001 — never propagate
            r = BackfillResult(table_name=spec.table_name, dry_run=dry_run)
            r.errors.append(f"backfill_table crashed: {exc}")
            r.duration_seconds = 0.0
        results.append(r)
    return results


def summarize_results(results: list) -> dict:
    """Aggregate stats across all backfilled tables."""
    return {
        "total_tables": len(results),
        "total_rows_read": sum(r.rows_read for r in results),
        "total_rows_inserted": sum(r.rows_inserted for r in results),
        "total_rows_skipped_existing": sum(
            r.rows_skipped_existing for r in results
        ),
        "total_rows_errored": sum(r.rows_errored for r in results),
        "tables_with_errors": [r.table_name for r in results if r.errors],
        "per_table": [
            {
                "table": r.table_name,
                "read": r.rows_read,
                "inserted": r.rows_inserted,
                "skipped": r.rows_skipped_existing,
                "errored": r.rows_errored,
                "duration_seconds": round(r.duration_seconds, 3),
                "dry_run": r.dry_run,
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Status helpers — used by the CLI's --status mode. No writes.
# ---------------------------------------------------------------------------


def collect_status() -> dict:
    """Returns a snapshot of connectivity + per-table row counts.

    The status payload is intentionally a superset of the M12.0a
    ``health_check`` output so an operator can read one report and
    decide whether to run the backfill.
    """
    health = postgres_storage.health_check()
    engine = (
        postgres_storage.get_engine()
        if health["dual_write_enabled"] else None
    )
    sqlite_counts = {
        name: _count_rows(name) for name in TABLE_READERS
    }
    postgres_counts = {
        name: _count_rows_in_postgres(engine, name) if engine else 0
        for name in TABLE_READERS
    }
    return {
        "health": health,
        "sqlite_counts": sqlite_counts,
        "postgres_counts": postgres_counts,
    }
