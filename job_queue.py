"""M15.0a — RQ-based job queue wrapper for Phase 2 async orchestration.

This module is **infrastructure only** as of M15.0a. The existing
``/analyze`` endpoint and the existing process-local ``/jobs/analyze``
flow in ``api_server.py`` are NOT touched here — M15.0b through M15.0e
will wire them onto this queue as separate milestones.

Design contracts
================

1. **Graceful degradation.** Every public function returns a safe value
   (``None`` or a documented ``"unavailable"`` sentinel dict) when:

   - ``REDIS_URL`` is unset in the environment, OR
   - the Redis server is unreachable, OR
   - the optional ``rq`` / ``redis`` libraries are not installed.

   In none of those failure modes does this module raise from a public
   function. Production traffic that does not need the queue (every
   pre-M15.0b code path) sees zero new exception surface.

2. **Lazy imports.** ``rq`` and ``redis`` are imported lazily inside
   the connection factory, so ``import job_queue`` succeeds even when
   neither package is installed. The ``IS_RQ_AVAILABLE`` and
   ``IS_REDIS_AVAILABLE`` module-level booleans report what was
   actually importable on this interpreter.

3. **No verdict-producing code.** This module never imports
   ``policy_decision`` / ``policy_scoring`` / ``verification_card``
   and never makes LLM calls. It is pure plumbing.

4. **No I/O at import time.** No connection is opened, no env var is
   read, no log line is emitted during ``import job_queue``. Side
   effects start only when a public function is called.

Public surface (stable, pinned by tests/test_job_queue.py)
==========================================================

  * ``IS_RQ_AVAILABLE``        — bool, was ``rq`` importable?
  * ``IS_REDIS_AVAILABLE``     — bool, was ``redis`` importable?
  * ``get_redis_connection()`` — returns ``redis.Redis | None``
  * ``get_queue(name)``        — returns ``rq.Queue | None``
  * ``enqueue_job(func, *args, **kwargs)`` — returns ``str | None``
                                 (the RQ job_id, or None on degraded)
  * ``get_job_status(job_id)`` — returns dict (see schema below)
  * ``get_queue_health()``     — returns dict (see schema below)

Schemas
=======

``get_job_status(job_id)`` always returns a dict with these keys
(values may be ``None`` when unknown):

    {
      "status": "queued" | "started" | "deferred" | "finished"
              | "failed" | "stopped" | "scheduled" | "canceled"
              | "not_found" | "unavailable",
      "result": <Any | None>,
      "error": <str | None>,
      "enqueued_at": <ISO8601 str | None>,
      "started_at":  <ISO8601 str | None>,
      "ended_at":    <ISO8601 str | None>,
    }

``get_queue_health()`` always returns:

    {
      "redis_connected": <bool>,
      "queue_depth":     <int>,    # 0 when degraded
      "workers_count":   <int>,    # 0 when degraded
      "queue_name":      <str>,    # always "default" in M15.0a
      "redis_url_set":   <bool>,
    }
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional


log = logging.getLogger("policy_ai.job_queue")


# ---------------------------------------------------------------------------
# Optional-import probe
# ---------------------------------------------------------------------------


try:
    import redis  # noqa: F401
    IS_REDIS_AVAILABLE = True
except ImportError:
    IS_REDIS_AVAILABLE = False


try:
    import rq  # noqa: F401
    IS_RQ_AVAILABLE = True
except ImportError:
    IS_RQ_AVAILABLE = False


_DEFAULT_QUEUE_NAME = "default"


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


def _redis_factory(url: str):
    """Instantiate a real ``redis.Redis`` client from a URL.

    Separated into its own function so tests can monkey-patch this
    module attribute with ``fakeredis.FakeRedis.from_url`` without
    touching ``REDIS_URL`` in the environment.
    """
    if not IS_REDIS_AVAILABLE:
        return None
    import redis as _redis
    return _redis.Redis.from_url(url, socket_connect_timeout=5)


def get_redis_connection():
    """Return a Redis client built from ``REDIS_URL``, or ``None``.

    Returns ``None`` (never raises) when:
      - ``REDIS_URL`` is unset / blank in the environment,
      - the ``redis`` package is not installed,
      - the connection check (``ping()``) fails.
    """
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    if not IS_REDIS_AVAILABLE:
        log.warning(
            "job_queue.redis_unavailable",
            extra={"reason": "redis_package_not_installed"},
        )
        return None
    try:
        client = _redis_factory(url)
        if client is None:
            return None
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 — graceful degradation contract
        log.warning(
            "job_queue.redis_connection_failed",
            extra={
                "reason": type(exc).__name__,
                "exception_message": str(exc)[:300],
            },
        )
        return None


def get_queue(name: str = _DEFAULT_QUEUE_NAME):
    """Return an RQ Queue bound to the named queue, or ``None``.

    The queue is built on a fresh Redis client each call; callers are
    expected to be infrequent (typically per-request).
    """
    if not IS_RQ_AVAILABLE:
        log.warning(
            "job_queue.rq_unavailable",
            extra={"reason": "rq_package_not_installed"},
        )
        return None
    client = get_redis_connection()
    if client is None:
        return None
    import rq as _rq
    try:
        return _rq.Queue(name, connection=client)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "job_queue.queue_construct_failed",
            extra={
                "queue_name": name,
                "reason": type(exc).__name__,
                "exception_message": str(exc)[:300],
            },
        )
        return None


# ---------------------------------------------------------------------------
# Public API — enqueue + status
# ---------------------------------------------------------------------------


def enqueue_job(
    func: Callable[..., Any],
    *args: Any,
    queue_name: str = _DEFAULT_QUEUE_NAME,
    job_timeout: int = 600,
    **kwargs: Any,
) -> Optional[str]:
    """Enqueue ``func(*args, **kwargs)`` on the named queue.

    Returns the RQ job_id (a string) on success, ``None`` when the
    queue is unavailable. NEVER raises.
    """
    queue = get_queue(queue_name)
    if queue is None:
        log.warning(
            "job_queue.enqueue_degraded",
            extra={
                "queue_name": queue_name,
                "reason": "queue_unavailable",
            },
        )
        return None
    try:
        job = queue.enqueue(func, *args, job_timeout=job_timeout, **kwargs)
        return str(job.id) if job is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "job_queue.enqueue_failed",
            extra={
                "queue_name": queue_name,
                "reason": type(exc).__name__,
                "exception_message": str(exc)[:300],
            },
        )
        return None


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def get_job_status(job_id: str) -> dict:
    """Return the status dict for ``job_id``. See module docstring for schema."""
    base = {
        "status": "unavailable",
        "result": None,
        "error": None,
        "enqueued_at": None,
        "started_at": None,
        "ended_at": None,
    }
    if not IS_RQ_AVAILABLE:
        base["error"] = "rq_unavailable"
        return base
    client = get_redis_connection()
    if client is None:
        base["error"] = "redis_unavailable"
        return base
    import rq as _rq
    try:
        job = _rq.job.Job.fetch(job_id, connection=client)
    except Exception as exc:  # noqa: BLE001
        if "NoSuchJobError" in type(exc).__name__:
            return {
                "status": "not_found",
                "result": None,
                "error": "job_not_found",
                "enqueued_at": None,
                "started_at": None,
                "ended_at": None,
            }
        log.warning(
            "job_queue.fetch_failed",
            extra={
                "job_id": job_id,
                "reason": type(exc).__name__,
                "exception_message": str(exc)[:300],
            },
        )
        base["error"] = type(exc).__name__
        return base
    try:
        status_str = job.get_status()
    except Exception as exc:  # noqa: BLE001
        status_str = "unavailable"
        log.warning(
            "job_queue.status_fetch_failed",
            extra={
                "job_id": job_id,
                "reason": type(exc).__name__,
            },
        )
    result_value = None
    if hasattr(job, "return_value") and callable(job.return_value):
        try:
            result_value = job.return_value()
        except Exception:  # noqa: BLE001
            result_value = None
    elif hasattr(job, "result"):
        result_value = getattr(job, "result", None)
    error_value = getattr(job, "exc_info", None)
    if error_value:
        error_value = str(error_value)[:1000]
    return {
        "status": status_str or "unavailable",
        "result": result_value,
        "error": error_value,
        "enqueued_at": _iso_or_none(getattr(job, "enqueued_at", None)),
        "started_at": _iso_or_none(getattr(job, "started_at", None)),
        "ended_at": _iso_or_none(getattr(job, "ended_at", None)),
    }


# ---------------------------------------------------------------------------
# Health / observability
# ---------------------------------------------------------------------------


def get_queue_health(queue_name: str = _DEFAULT_QUEUE_NAME) -> dict:
    """Return the dict shape that ``/health/queue`` serves."""
    redis_url_set = bool(os.environ.get("REDIS_URL", "").strip())
    base = {
        "redis_connected": False,
        "queue_depth": 0,
        "workers_count": 0,
        "queue_name": queue_name,
        "redis_url_set": redis_url_set,
    }
    client = get_redis_connection()
    if client is None:
        return base
    base["redis_connected"] = True
    if not IS_RQ_AVAILABLE:
        return base
    import rq as _rq
    try:
        queue = _rq.Queue(queue_name, connection=client)
        base["queue_depth"] = int(len(queue))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "job_queue.health_depth_failed",
            extra={
                "queue_name": queue_name,
                "reason": type(exc).__name__,
            },
        )
    try:
        workers = _rq.Worker.all(connection=client, queue=_rq.Queue(queue_name, connection=client))
        base["workers_count"] = len(list(workers))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "job_queue.health_workers_failed",
            extra={
                "queue_name": queue_name,
                "reason": type(exc).__name__,
            },
        )
    return base
