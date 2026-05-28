"""Job lifecycle manager for the async verification pipeline.

SQLite is the source of truth for writes. Postgres mirroring of job rows
via :func:`postgres_storage.mirror_write` / :func:`mirror_upsert` is
best-effort and must never break the SQLite path. This module deliberately
keeps the interface tiny so the FastAPI layer (and tests) can call it
without knowing about persistence details.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from database import get_connection, get_result_by_id

logger = logging.getLogger("policy_ai.job_manager")


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMEOUT}

STAGE_QUEUED = "queued"
STAGE_RUNNING = "running"
STAGE_PIPELINE_STARTED = "pipeline_started"
STAGE_SAVING_RESULT = "saving_result"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
STAGE_TIMEOUT = "timeout"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)


def _pipeline_version() -> str:
    return os.getenv("PIPELINE_VERSION", "phase2-m2")


def _mirror_jobs_safe(*, upsert: bool, row_dict: dict) -> None:
    """M12.0c-jobs — best-effort dual-write to postgres_storage.jobs_table.

    Mirrors the *current state* of a job row into the postgres_storage
    jobs mirror, matching the table-level mirroring used by
    analysis_results / review_tasks / etc. since M12.0a.

    NEVER raises. SQLite remains source of truth — any Postgres failure
    is logged inside ``mirror_write`` / ``mirror_upsert`` and swallowed
    here as well (belt-and-braces)."""
    try:
        from postgres_storage import mirror_upsert, mirror_write

        if upsert:
            mirror_upsert("jobs", row_dict, ["id"])
        else:
            mirror_write("jobs", row_dict)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        pass


def _read_jobs_row_full(job_id: str) -> Optional[dict]:
    """Internal helper: re-read the full SQLite row for ``job_id`` so the
    PG mirror_upsert payload contains every column. Returns None if the
    row vanished between UPDATE and SELECT (e.g. a concurrent delete by
    an external process) — caller treats None as "skip mirror"."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def create_job(query: str, max_news: int) -> dict:
    """Insert a fresh job row in 'queued' state and return it."""
    job_id = uuid.uuid4().hex
    now = _utc_now_iso()
    pipeline_version = _pipeline_version()

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                id, status, query, max_news, progress_percent, current_stage,
                created_at, pipeline_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                STATUS_QUEUED,
                query,
                int(max_news or 0),
                0,
                STAGE_QUEUED,
                now,
                pipeline_version,
            ),
        )
        connection.commit()

    record = {
        "id": job_id,
        "status": STATUS_QUEUED,
        "query": query,
        "max_news": int(max_news or 0),
        "progress_percent": 0,
        "current_stage": STAGE_QUEUED,
        "result_id": None,
        "error_message": None,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
        "pipeline_version": pipeline_version,
    }
    # M12.0c-jobs — mirror the full row into postgres_storage.jobs_table.
    # id is a UUID hex so a write (not upsert) is sufficient; retries
    # against the same id are not expected for create_job.
    _mirror_jobs_safe(upsert=False, row_dict=record)
    logger.info("Job created: id=%s query=%s max_news=%s", job_id, query, max_news)
    return record


def _current_status(job_id: str) -> Optional[str]:
    """Return the current ``status`` of a job, or None if unknown.

    M12.0d Stage 3b: PG-primary for the idempotency guard so the read
    survives Worker restarts (SQLite is ephemeral on Render). Falls
    back to SQLite when PG dual-write is disabled (local dev / tests),
    matching the ``database.py`` lazy-import + PG-primary pattern."""
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_job_status,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        logger.error(
            "_current_status failed to import postgres_storage",
            exc_info=True,
            extra={"function": "_current_status", "job_id": job_id},
        )
        raise
    if pg_enabled:
        return read_job_status(job_id)
    # SQLite fallback — only reached when dual-write is disabled (no
    # DATABASE_URL or USE_POSTGRES_DUAL_WRITE off). Stage 3c will
    # remove the SQLite jobs writes; until then this fallback keeps
    # local-dev / test environments correct.
    with get_connection() as connection:
        row = connection.execute(
            "SELECT status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    return row["status"]


def start_job(job_id: str) -> None:
    now = _utc_now_iso()
    current = _current_status(job_id)
    if current in TERMINAL_STATUSES:
        logger.debug("start_job skipped (terminal): id=%s status=%s", job_id, current)
        return
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, current_stage = ?, progress_percent = ?, started_at = ?
            WHERE id = ?
            """,
            (STATUS_RUNNING, STAGE_RUNNING, 5, now, job_id),
        )
        connection.commit()
    # M12.0c-jobs — re-read the full row from SQLite (the source of
    # truth) and upsert it into the PG mirror so all 12 columns stay
    # byte-identical between the two stores.
    full_row = _read_jobs_row_full(job_id)
    if full_row is not None:
        _mirror_jobs_safe(upsert=True, row_dict=full_row)
    logger.info("Job started: id=%s", job_id)


def update_progress(job_id: str, stage: str, percent: int) -> None:
    safe_percent = max(0, min(int(percent or 0), 100))
    current = _current_status(job_id)
    if current in TERMINAL_STATUSES:
        logger.debug(
            "update_progress skipped (terminal): id=%s status=%s stage=%s",
            job_id, current, stage,
        )
        return
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET current_stage = ?, progress_percent = ?
            WHERE id = ?
            """,
            (stage, safe_percent, job_id),
        )
        connection.commit()
    # M12.0c-jobs — full-row mirror via SELECT * + mirror_upsert.
    full_row = _read_jobs_row_full(job_id)
    if full_row is not None:
        _mirror_jobs_safe(upsert=True, row_dict=full_row)


def complete_job(job_id: str, result_id: Optional[int]) -> None:
    now = _utc_now_iso()
    current = _current_status(job_id)
    if current in TERMINAL_STATUSES:
        logger.debug(
            "complete_job skipped (already terminal): id=%s status=%s",
            job_id, current,
        )
        return
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, current_stage = ?, progress_percent = ?,
                result_id = ?, completed_at = ?, error_message = NULL
            WHERE id = ?
            """,
            (STATUS_COMPLETED, STAGE_COMPLETED, 100, result_id, now, job_id),
        )
        connection.commit()
    # M12.0c-jobs — full-row mirror.
    full_row = _read_jobs_row_full(job_id)
    if full_row is not None:
        _mirror_jobs_safe(upsert=True, row_dict=full_row)
    logger.info("Job completed: id=%s result_id=%s", job_id, result_id)


def fail_job(job_id: str, error_message: str, *, stage: str = STAGE_FAILED, status: str = STATUS_FAILED) -> None:
    now = _utc_now_iso()
    safe_message = (error_message or "")[:2000]
    current = _current_status(job_id)
    if current in TERMINAL_STATUSES:
        logger.debug(
            "fail_job skipped (already terminal): id=%s status=%s -> %s",
            job_id, current, status,
        )
        return
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, current_stage = ?, error_message = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, stage, safe_message, now, job_id),
        )
        connection.commit()
    # M12.0c-jobs — full-row mirror.
    full_row = _read_jobs_row_full(job_id)
    if full_row is not None:
        _mirror_jobs_safe(upsert=True, row_dict=full_row)
    logger.warning("Job %s: id=%s reason=%s", status, job_id, safe_message)


def timeout_job(job_id: str, error_message: str = "job exceeded timeout") -> None:
    fail_job(job_id, error_message, stage=STAGE_TIMEOUT, status=STATUS_TIMEOUT)


def get_job_status(job_id: str) -> Optional[dict]:
    # M12.0c-jobs / M12.0d-1 — PG primary when dual-write is enabled
    # so the Web service sees jobs that the Worker has updated
    # (separate filesystems on Render). PG-read errors now raise;
    # post-PG row mutation is wrapped separately so a malformed PG row
    # surfaces with a distinct log message instead of masquerading as
    # a PG read failure.
    try:
        from postgres_storage import (
            is_postgres_dual_write_enabled,
            read_job_by_id,
        )
        pg_enabled = is_postgres_dual_write_enabled()
    except Exception:
        logger.error(
            "get_job_status failed to import postgres_storage",
            exc_info=True,
            extra={"function": "get_job_status", "job_id": job_id},
        )
        raise
    if pg_enabled:
        try:
            pg_row = read_job_by_id(job_id)
        except Exception:
            logger.error(
                "get_job_status PG read failed",
                exc_info=True,
                extra={"function": "get_job_status", "job_id": job_id},
            )
            raise
        if pg_row is not None:
            try:
                pg_row["job_id"] = pg_row.get("id")
            except Exception:
                logger.error(
                    "get_job_status PG row mutation failed",
                    exc_info=True,
                    extra={
                        "function": "get_job_status",
                        "job_id": job_id,
                    },
                )
                raise
            return pg_row
        # PG returned None = job not found (or engine miss).
        return None
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    record = _row_to_dict(row)
    if record is None:
        return None
    record["job_id"] = record.get("id")
    return record


def get_job_result(job_id: str) -> Optional[dict]:
    """Return the persisted analysis_results row associated with the job, or None."""
    status = get_job_status(job_id)
    if status is None:
        return None
    if status.get("status") != STATUS_COMPLETED:
        return None
    result_id = status.get("result_id")
    if result_id is None:
        return None
    try:
        return get_result_by_id(int(result_id))
    except Exception:
        return None


def get_default_job_timeout_seconds() -> int:
    try:
        raw = int(os.getenv("JOB_TIMEOUT_SECONDS", "600"))
    except (TypeError, ValueError):
        raw = 600
    return max(30, min(raw, 3600))


# ---------------------------------------------------------------------------
# M14.3b — context propagation helpers for worker submission.
#
# These wrappers capture the current contextvars context (including the
# request_id set by api_server's M14.3a middleware) at submit time, then
# execute the target callable inside that context on the worker thread.
# Without explicit capture, concurrent.futures.ThreadPoolExecutor.submit()
# and bare threading.Thread() targets run in the default empty context,
# losing request_id.
#
# Python's asyncio.create_task() and asyncio.to_thread() both already
# propagate context automatically in Python 3.9+, so api_server.py's
# current path (create_task -> coroutine -> asyncio.to_thread -> sync
# _run_pipeline_for_job) does NOT lose request_id under concurrent load.
# These helpers make the propagation contract explicit for any future
# caller that uses concurrent.futures directly — scheduler.py batch
# extensions, the LLM judge worker pool planned for M13.1b, ad-hoc
# operator tools, etc.
#
# Importing request_context inside the functions keeps job_manager
# import-light at module load and avoids a hard dependency on
# request_context for legacy callers that never use these helpers.
# ---------------------------------------------------------------------------


def submit_in_context(executor, func, *args, **kwargs):
    """Submit ``func(*args, **kwargs)`` to ``executor`` (a
    :class:`concurrent.futures.Executor`), capturing the current
    contextvars context so the worker sees the originating request's
    ``request_id``.

    Returns the standard :class:`concurrent.futures.Future`.
    Exceptions inside ``func`` are re-raised when ``.result()`` is
    called, exactly as with plain ``executor.submit``.

    Usage::

        from concurrent.futures import ThreadPoolExecutor
        from job_manager import submit_in_context

        with ThreadPoolExecutor() as pool:
            future = submit_in_context(pool, run_pipeline, query, max_news)
            result = future.result()
    """
    from request_context import capture_context, run_in_captured_context
    ctx = capture_context()
    return executor.submit(run_in_captured_context, ctx, func, *args, **kwargs)


def run_in_thread_with_context(func, *args, **kwargs):
    """Run ``func(*args, **kwargs)`` synchronously in a fresh
    :class:`threading.Thread`, propagating the current contextvars
    context to the worker thread.

    Blocks until the thread completes. Returns the callable's return
    value, or re-raises its exception. The captured context is
    immutable from the caller's perspective: any
    :func:`set_request_id` inside ``func`` only mutates the worker
    thread's copy.

    Use this for tests and for simple non-asyncio job runners. For
    HTTP request handlers, prefer ``asyncio.to_thread`` (which also
    propagates context in Python 3.9+).
    """
    import threading

    from request_context import capture_context, run_in_captured_context

    ctx = capture_context()
    container = {"result": None, "error": None}

    def _target():
        try:
            container["result"] = run_in_captured_context(
                ctx, func, *args, **kwargs,
            )
        except BaseException as error:  # noqa: BLE001 — re-raised below
            container["error"] = error

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if container["error"] is not None:
        raise container["error"]
    return container["result"]
