"""M15.0a — Standalone RQ worker entry point.

Run this script as the entry point for a Render Background Worker
service (or locally for development). It is **opt-in**: the web
service does NOT auto-start a worker, and no Render configuration
file references this module. Provisioning a worker is an explicit
operator decision (see ``docs/JOB_QUEUE.md``).

Local development:

    set REDIS_URL=redis://localhost:6379/0      # PowerShell
    python worker.py                            # listens on "default" queue

Render Background Worker (operator-provisioned):

    Start command: ``python worker.py``
    Requires REDIS_URL to be set on the worker service.

Exit behaviour:

  * Exit 0 — worker shut down cleanly (SIGTERM / SIGINT).
  * Exit 1 — REDIS_URL is unset OR Redis is unreachable. Operator
             must check ``REDIS_URL`` env var or Redis health.
  * Exit 2 — ``rq`` / ``redis`` packages not installed.

This is the only file in M15.0a that fails loudly on missing deps —
all other touchpoints (``job_queue.py``, ``/health/queue``, tests)
degrade gracefully.
"""

from __future__ import annotations

import os
import sys


def _fail(code: int, message: str) -> int:
    print(f"[worker] {message}", file=sys.stderr)
    return code


def main() -> int:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return _fail(
            1,
            "REDIS_URL is not set in the environment. The worker cannot "
            "start without a Redis connection. Set REDIS_URL on this "
            "process (or this Render Background Worker service) and "
            "retry.",
        )

    try:
        import rq
        import redis
    except ImportError as exc:
        return _fail(
            2,
            f"rq/redis packages are not installed: {exc}. Install via "
            "`pip install -r requirements.txt`.",
        )

    try:
        connection = redis.Redis.from_url(url, socket_connect_timeout=5)
        connection.ping()
    except Exception as exc:  # noqa: BLE001 — fail-loud at startup
        return _fail(
            1,
            f"Redis connection failed: {type(exc).__name__}: {exc}. "
            "Verify REDIS_URL points at a reachable Redis instance.",
        )

    queue_name = os.environ.get("WORKER_QUEUE_NAME", "default").strip() or "default"
    print(
        f"[worker] starting RQ worker on queue {queue_name!r} "
        f"(redis url set; ping OK)"
    )
    queue = rq.Queue(queue_name, connection=connection)
    worker = rq.Worker([queue], connection=connection)
    try:
        worker.work(with_scheduler=False)
    except KeyboardInterrupt:
        print("[worker] interrupted by keyboard; shutting down cleanly")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
