"""Job lifecycle manager for the async verification pipeline.

SQLite is the source of truth. Postgres dual-write of job rows is best-effort
and must never break the SQLite path. This module deliberately keeps the
interface tiny so the FastAPI layer (and tests) can call it without knowing
about persistence details.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

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


def _postgres_dual_write_job(payload: dict) -> None:
    """Best-effort dual-write of a job row. Never raises."""
    try:
        from db import postgres as pg
    except Exception:
        return

    if not pg.is_dual_write_enabled():
        return

    try:
        from sqlalchemy import text
    except Exception as error:
        logger.debug("Postgres jobs dual-write skipped (sqlalchemy import): %s", error)
        return

    try:
        session = pg.get_session()
    except Exception as error:
        logger.debug("Postgres jobs dual-write skipped (session): %s", error)
        return

    if session is None:
        return

    try:
        session.execute(
            text(
                """
                INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at)
                VALUES (:entity, :entity_id, :action, :actor, CAST(:payload AS JSONB), :created_at)
                """
            ),
            {
                "entity": "job",
                "entity_id": str(payload.get("id") or ""),
                "action": payload.get("action") or "job_event",
                "actor": "job_manager",
                "payload": _payload_json(payload),
                "created_at": datetime.now(timezone.utc),
            },
        )
        session.commit()
    except Exception as error:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            "Postgres jobs dual-write failed (SQLite remains source of truth): %s",
            error,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def _payload_json(payload: dict) -> str:
    import json

    safe = {k: v for k, v in payload.items() if k != "action"}
    try:
        return json.dumps(safe, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


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
    _postgres_dual_write_job({**record, "action": "create"})
    logger.info("Job created: id=%s query=%s max_news=%s", job_id, query, max_news)
    return record


def _current_status(connection, job_id: str) -> Optional[str]:
    row = connection.execute(
        "SELECT status FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return row["status"]


def start_job(job_id: str) -> None:
    now = _utc_now_iso()
    with get_connection() as connection:
        current = _current_status(connection, job_id)
        if current in TERMINAL_STATUSES:
            logger.debug("start_job skipped (terminal): id=%s status=%s", job_id, current)
            return
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, current_stage = ?, progress_percent = ?, started_at = ?
            WHERE id = ?
            """,
            (STATUS_RUNNING, STAGE_RUNNING, 5, now, job_id),
        )
        connection.commit()
    _postgres_dual_write_job({
        "id": job_id,
        "status": STATUS_RUNNING,
        "current_stage": STAGE_RUNNING,
        "started_at": now,
        "action": "start",
    })
    logger.info("Job started: id=%s", job_id)


def update_progress(job_id: str, stage: str, percent: int) -> None:
    safe_percent = max(0, min(int(percent or 0), 100))
    with get_connection() as connection:
        current = _current_status(connection, job_id)
        if current in TERMINAL_STATUSES:
            logger.debug(
                "update_progress skipped (terminal): id=%s status=%s stage=%s",
                job_id, current, stage,
            )
            return
        connection.execute(
            """
            UPDATE jobs
            SET current_stage = ?, progress_percent = ?
            WHERE id = ?
            """,
            (stage, safe_percent, job_id),
        )
        connection.commit()
    _postgres_dual_write_job({
        "id": job_id,
        "current_stage": stage,
        "progress_percent": safe_percent,
        "action": "progress",
    })


def complete_job(job_id: str, result_id: Optional[int]) -> None:
    now = _utc_now_iso()
    with get_connection() as connection:
        current = _current_status(connection, job_id)
        if current in TERMINAL_STATUSES:
            logger.debug(
                "complete_job skipped (already terminal): id=%s status=%s",
                job_id, current,
            )
            return
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
    _postgres_dual_write_job({
        "id": job_id,
        "status": STATUS_COMPLETED,
        "current_stage": STAGE_COMPLETED,
        "result_id": result_id,
        "completed_at": now,
        "action": "complete",
    })
    logger.info("Job completed: id=%s result_id=%s", job_id, result_id)


def fail_job(job_id: str, error_message: str, *, stage: str = STAGE_FAILED, status: str = STATUS_FAILED) -> None:
    now = _utc_now_iso()
    safe_message = (error_message or "")[:2000]
    with get_connection() as connection:
        current = _current_status(connection, job_id)
        if current in TERMINAL_STATUSES:
            logger.debug(
                "fail_job skipped (already terminal): id=%s status=%s -> %s",
                job_id, current, status,
            )
            return
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, current_stage = ?, error_message = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, stage, safe_message, now, job_id),
        )
        connection.commit()
    _postgres_dual_write_job({
        "id": job_id,
        "status": status,
        "current_stage": stage,
        "error_message": safe_message,
        "completed_at": now,
        "action": "fail",
    })
    logger.warning("Job %s: id=%s reason=%s", status, job_id, safe_message)


def timeout_job(job_id: str, error_message: str = "job exceeded timeout") -> None:
    fail_job(job_id, error_message, stage=STAGE_TIMEOUT, status=STATUS_TIMEOUT)


def get_job_status(job_id: str) -> Optional[dict]:
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
