"""Postgres dual-write plumbing.

SQLite remains the source of truth. Postgres is opt-in via DATABASE_URL +
USE_POSTGRES_WRITE feature flag. Any failure in this module must never break
the SQLite save path; callers must catch and continue.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("policy_ai.db.postgres")

_ENGINE = None
_SESSION_FACTORY = None
_INIT_ATTEMPTED = False
_INIT_ERROR: Optional[str] = None


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def get_database_url() -> Optional[str]:
    url = os.getenv("DATABASE_URL")
    if not url or not url.strip():
        return None
    return url.strip()


def is_postgres_enabled() -> bool:
    return get_database_url() is not None


def is_dual_write_enabled() -> bool:
    """USE_POSTGRES_WRITE must be explicitly true AND DATABASE_URL must be set."""
    return _truthy(os.getenv("USE_POSTGRES_WRITE")) and is_postgres_enabled()


def _build_engine_and_session():
    """Lazily build SQLAlchemy engine/session factory. Returns (engine, SessionFactory) or (None, None)."""
    global _ENGINE, _SESSION_FACTORY, _INIT_ATTEMPTED, _INIT_ERROR

    if _ENGINE is not None and _SESSION_FACTORY is not None:
        return _ENGINE, _SESSION_FACTORY

    url = get_database_url()
    if url is None:
        _INIT_ATTEMPTED = True
        _INIT_ERROR = "DATABASE_URL not set"
        return None, None

    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
    except Exception as error:
        _INIT_ATTEMPTED = True
        _INIT_ERROR = f"sqlalchemy import failed: {error}"
        logger.warning("Postgres disabled: %s", _INIT_ERROR)
        return None, None

    try:
        engine = create_engine(url, pool_pre_ping=True, future=True)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    except Exception as error:
        _INIT_ATTEMPTED = True
        _INIT_ERROR = f"engine creation failed: {error}"
        logger.warning("Postgres engine init failed: %s", _INIT_ERROR)
        return None, None

    _ENGINE = engine
    _SESSION_FACTORY = session_factory
    _INIT_ATTEMPTED = True
    _INIT_ERROR = None
    return _ENGINE, _SESSION_FACTORY


def get_engine():
    engine, _ = _build_engine_and_session()
    return engine


def get_session():
    """Return a new SQLAlchemy session, or None if Postgres is disabled."""
    _, session_factory = _build_engine_and_session()
    if session_factory is None:
        return None
    return session_factory()


def reset_state_for_tests() -> None:
    """Clear cached engine/session — used by tests that toggle env vars."""
    global _ENGINE, _SESSION_FACTORY, _INIT_ATTEMPTED, _INIT_ERROR
    if _ENGINE is not None:
        try:
            _ENGINE.dispose()
        except Exception:
            pass
    _ENGINE = None
    _SESSION_FACTORY = None
    _INIT_ATTEMPTED = False
    _INIT_ERROR = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except Exception:
            return stripped
    return value


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def postgres_dual_write(result: dict, *, query: str = "") -> dict:
    """Best-effort dual-write to Postgres after a successful SQLite save.

    M12.0d-2 (Stage 2 / Option 5B): trimmed to write only to the
    ``audit_log`` table. The previous stories / claims / verdicts
    INSERTs have been removed — those tables were never read by any
    code in the project (verified by grep) and were write-only
    dead weight. The actual verification data flows through
    :mod:`postgres_storage` (analysis_results, review_tasks, jobs,
    artifact_* tables) as the canonical dual-write surface. This
    module is retained for the audit_log write path used by
    :mod:`api_server` and :mod:`job_manager` (see
    ``_postgres_dual_write_job``).

    Returns a small status dict describing what happened. Never raises.
    """
    status = {"attempted": False, "ok": False, "skipped_reason": None, "error": None}

    if not is_dual_write_enabled():
        if not is_postgres_enabled():
            status["skipped_reason"] = "DATABASE_URL not set"
        else:
            status["skipped_reason"] = "USE_POSTGRES_WRITE disabled"
        return status

    status["attempted"] = True

    try:
        from sqlalchemy import text  # local import to keep module importable without sqlalchemy
    except Exception as error:
        status["error"] = f"sqlalchemy import failed: {error}"
        logger.warning("Postgres dual-write skipped: %s", status["error"])
        return status

    try:
        session = get_session()
    except Exception as error:
        status["error"] = f"session acquisition failed: {error}"
        logger.warning("Postgres dual-write skipped: %s", status["error"])
        return status

    if session is None:
        status["error"] = _INIT_ERROR or "session unavailable"
        logger.warning("Postgres dual-write skipped: %s", status["error"])
        return status

    try:
        result = result or {}
        verification_card = result.get("verification_card") or {}
        final_decision = result.get("final_decision") or {}

        news_url = result.get("original_url") or ""
        verdict_label = (
            verification_card.get("verdict_label")
            or result.get("verdict_label")
            or ""
        )
        now = _utc_now()

        session.execute(
            text(
                """
                INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at)
                VALUES (:entity, :entity_id, :action, :actor, CAST(:payload AS JSONB), :created_at)
                """
            ),
            {
                "entity": "analysis_result",
                "entity_id": news_url,
                "action": "dual_write",
                "actor": "api_server",
                "payload": json.dumps(
                    {
                        "query": query,
                        "verdict_label": verdict_label,
                        "policy_alert_level": final_decision.get("policy_alert_level"),
                    },
                    ensure_ascii=False,
                ),
                "created_at": now,
            },
        )

        session.commit()
        status["ok"] = True
        logger.info(
            "Postgres audit-log dual-write ok: entity=analysis_result url=%s",
            news_url,
        )
    except Exception as error:
        try:
            session.rollback()
        except Exception:
            pass
        status["error"] = str(error)
        logger.warning(
            "Postgres dual-write failed (SQLite remains source of truth): %s",
            error,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass

    return status
