"""Job lifecycle manager for the async verification pipeline.

M12.0d Stage 3c-2: Postgres is the sole write target for the ``jobs``
table. ``create_job`` mirrors a fresh row via
:func:`postgres_storage.mirror_write`; ``start_job`` / ``update_progress``
/ ``complete_job`` / ``fail_job`` apply field-level updates via
:func:`postgres_storage.pg_update_job_fields`.

M12.0d Stage 3c-4: the ``_current_status`` / ``get_job_status`` SQLite
read fallbacks were removed. Since 3c-2 the ``jobs`` table has had no
SQLite writer on any supported path (``create_job`` is PG-mirror-only and
there is no ``db_path`` path for jobs), so those fallbacks read a
permanently empty table — behaviourally identical to the PG read helpers
returning None when dual-write is disabled. Reads are now PG-only.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from database import get_result_by_id

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


def _pipeline_version() -> str:
    return os.getenv("PIPELINE_VERSION", "phase2-m2")


def _mirror_jobs_safe(row_dict: dict) -> None:
    """M12.0c-jobs / M12.0d Stage 3c-4 — best-effort PG mirror write of a
    fresh ``jobs`` row.

    ``create_job`` is the only caller and always inserts a brand-new row
    (caller-supplied UUID-hex id), so a plain ``mirror_write`` is correct.
    The prior ``upsert`` branch was dead (no caller ever passed
    ``upsert=True``) and was removed in 3c-4.

    NEVER raises. Postgres is the source of truth; any Postgres failure is
    logged inside ``mirror_write`` and swallowed here as well
    (belt-and-braces)."""
    try:
        from postgres_storage import mirror_write

        mirror_write("jobs", row_dict)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        pass


def _pg_update_job_fields_safe(job_id: str, fields: dict) -> None:
    """M12.0d Stage 3c-2 — lazy-import wrapper around
    :func:`postgres_storage.pg_update_job_fields`. Swallows ImportError
    so the module loads on a dev box without psycopg installed. The
    underlying helper already swallows DB errors and never raises."""
    try:
        from postgres_storage import pg_update_job_fields

        pg_update_job_fields(job_id, fields)
    except Exception:  # noqa: BLE001 — Postgres failures must not surface
        pass


def create_job(query: str, max_news: int) -> dict:
    """Insert a fresh job row in 'queued' state and return it.

    M12.0d Stage 3c-2: Postgres is the sole write target. The SQLite
    INSERT was removed; the PG mirror_write is now the only persistence
    step. Caller-supplied UUID-hex ``id`` means a plain INSERT (not
    upsert) is correct."""
    job_id = uuid.uuid4().hex
    now = _utc_now_iso()
    pipeline_version = _pipeline_version()

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
    _mirror_jobs_safe(row_dict=record)
    logger.info("Job created: id=%s query=%s max_news=%s", job_id, query, max_news)
    return record


def _current_status(job_id: str) -> Optional[str]:
    """Return the current ``status`` of a job, or None if unknown.

    M12.0d Stage 3b: PG-primary for the idempotency guard so the read
    survives Worker restarts (SQLite is ephemeral on Render).

    M12.0d Stage 3c-4: the SQLite fallback was removed. Jobs have had no
    SQLite writer since 3c-2 (``create_job`` is PG-mirror-only, no
    ``db_path`` path), so the fallback read a permanently empty table —
    behaviourally identical to ``read_job_status`` returning None when
    dual-write is disabled (engine is None)."""
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
    # Dual-write disabled: no jobs writer exists on any supported path, so
    # there is nothing to read (read_job_status would also return None here
    # via the engine-None path).
    return None


def start_job(job_id: str) -> None:
    now = _utc_now_iso()
    current = _current_status(job_id)
    if current in TERMINAL_STATUSES:
        logger.debug("start_job skipped (terminal): id=%s status=%s", job_id, current)
        return
    _pg_update_job_fields_safe(
        job_id,
        {
            "status": STATUS_RUNNING,
            "current_stage": STAGE_RUNNING,
            "progress_percent": 5,
            "started_at": now,
        },
    )
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
    _pg_update_job_fields_safe(
        job_id,
        {"current_stage": stage, "progress_percent": safe_percent},
    )


def complete_job(job_id: str, result_id: Optional[int]) -> None:
    now = _utc_now_iso()
    current = _current_status(job_id)
    if current in TERMINAL_STATUSES:
        logger.debug(
            "complete_job skipped (already terminal): id=%s status=%s",
            job_id, current,
        )
        return
    _pg_update_job_fields_safe(
        job_id,
        {
            "status": STATUS_COMPLETED,
            "current_stage": STAGE_COMPLETED,
            "progress_percent": 100,
            "result_id": result_id,
            "completed_at": now,
            "error_message": None,
        },
    )
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
    _pg_update_job_fields_safe(
        job_id,
        {
            "status": status,
            "current_stage": stage,
            "error_message": safe_message,
            "completed_at": now,
        },
    )
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
    # M12.0d Stage 3c-4: SQLite fallback removed. Jobs have no SQLite
    # writer on any supported path (see module docstring / _current_status),
    # so this point — reached only when dual-write is disabled — has
    # nothing to return.
    return None


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
