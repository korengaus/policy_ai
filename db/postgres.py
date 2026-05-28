"""Postgres connection plumbing.

Provides the engine / session factory + feature-flag helpers
(``is_postgres_enabled`` / ``is_dual_write_enabled``) used by callers that
need to detect or initialize Postgres connectivity.

M12.0d Stage 3a: the ``postgres_dual_write`` function (audit_log INSERT
path) has been removed. It was a write-only telemetry surface with zero
readers and a table that never existed in production. All canonical
dual-write now flows through :mod:`postgres_storage`.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

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
