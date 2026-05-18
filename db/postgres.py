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
        news_title = result.get("title") or ""
        now = _utc_now()

        story_row = session.execute(
            text(
                """
                INSERT INTO stories (news_url, news_title, fetched_at, created_at)
                VALUES (:news_url, :news_title, :fetched_at, :created_at)
                RETURNING id
                """
            ),
            {
                "news_url": news_url,
                "news_title": news_title,
                "fetched_at": _parse_iso(verification_card.get("last_checked_at")) or now,
                "created_at": now,
            },
        ).fetchone()
        story_id = story_row[0] if story_row else None

        claim_text_value = verification_card.get("claim_text") or result.get("claim_text") or ""
        normalized_claims = result.get("normalized_claims") or verification_card.get("normalized_claims") or []
        normalized_first = ""
        if isinstance(normalized_claims, list) and normalized_claims:
            first = normalized_claims[0]
            if isinstance(first, dict):
                normalized_first = first.get("normalized") or first.get("text") or ""
            else:
                normalized_first = str(first)

        claim_row = session.execute(
            text(
                """
                INSERT INTO claims (story_id, text, normalized, claim_type, created_at)
                VALUES (:story_id, :text, :normalized, :claim_type, :created_at)
                RETURNING id
                """
            ),
            {
                "story_id": story_id,
                "text": claim_text_value,
                "normalized": normalized_first,
                "claim_type": result.get("topic") or "",
                "created_at": now,
            },
        ).fetchone()
        claim_id = claim_row[0] if claim_row else None

        verdict_label = verification_card.get("verdict_label") or result.get("verdict_label") or ""
        verdict_confidence = _coerce_int(
            verification_card.get("verdict_confidence") or result.get("verdict_confidence")
        )

        session.execute(
            text(
                """
                INSERT INTO verdicts (
                    claim_id, label, confidence, pipeline_version, rules_version,
                    schema_version, llm_model, created_at
                ) VALUES (
                    :claim_id, :label, :confidence, :pipeline_version, :rules_version,
                    :schema_version, :llm_model, :created_at
                )
                """
            ),
            {
                "claim_id": claim_id,
                "label": verdict_label,
                "confidence": verdict_confidence,
                "pipeline_version": os.getenv("PIPELINE_VERSION", "phase2-m1"),
                "rules_version": os.getenv("RULES_VERSION", "v1"),
                "schema_version": os.getenv("SCHEMA_VERSION", "v1"),
                "llm_model": result.get("ai_model") or os.getenv("AI_MODEL", ""),
                "created_at": now,
            },
        )

        session.execute(
            text(
                """
                INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at)
                VALUES (:entity, :entity_id, :action, :actor, CAST(:payload AS JSONB), :created_at)
                """
            ),
            {
                "entity": "story",
                "entity_id": str(story_id) if story_id is not None else "",
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
        status["story_id"] = story_id
        status["claim_id"] = claim_id
        logger.info(
            "Postgres dual-write ok: story_id=%s claim_id=%s url=%s",
            story_id,
            claim_id,
            news_url,
        )
    except Exception as error:
        try:
            session.rollback()
        except Exception:
            pass
        status["error"] = str(error)
        logger.warning("Postgres dual-write failed (SQLite remains source of truth): %s", error)
    finally:
        try:
            session.close()
        except Exception:
            pass

    return status
