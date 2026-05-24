"""M15.0a — Job queue health / readiness diagnostic.

Read-only CLI that reports whether the M15.0a job queue infrastructure
is configured and reachable on this host (or the host this script is
deployed to). Performs:

  * A ``REDIS_URL`` env-var presence check.
  * A single ``PING`` against Redis when ``REDIS_URL`` is set.
  * A ``LEN <queue_name>`` count of pending jobs.
  * A ``Worker.all`` poll of currently-registered RQ workers.

Performs NO writes, NO calls to ``analyze_pipeline``, and NO external
network requests other than the Redis probes.

Usage:
    python scripts/check_job_queue.py
    python scripts/check_job_queue.py --json
    python scripts/check_job_queue.py --help

Exit codes:
    0 — status reported (Redis reachable, OR REDIS_URL unset and
        we report degraded — both are operator-informational)
    1 — REDIS_URL is set but Redis is unreachable (operator action
        needed — verify Redis is running and the URL is correct)
    2 — CLI usage error

The script is intentionally minimal so the rule "the application
degrades gracefully without Redis" stays visible at every operator
touch point: when REDIS_URL is unset, the CLI emits a safety note
ahead of any other action.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_job_queue",
        description=(
            "Report M15.0a job queue health. Read-only — performs no "
            "writes, no analyze_pipeline calls, and no external "
            "network requests other than Redis probes (PING + queue "
            "depth + worker count)."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 — status reported (Redis reachable, OR REDIS_URL "
            "unset and degraded — both informational)\n"
            "  1 — REDIS_URL set but Redis unreachable\n"
            "  2 — CLI usage error\n\n"
            "M15.0a — the application degrades gracefully when Redis "
            "is unset / unreachable. /analyze and /jobs/* paths "
            "remain functional via the pre-existing process-local "
            "job_manager. Redis-backed queue is opt-in."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    parser.add_argument(
        "--queue-name", default="default",
        help="RQ queue name to probe (default: %(default)s).",
    )
    return parser


def _format_human(payload: dict) -> str:
    lines = []
    lines.append("=== M15.0a Job Queue Health ===")
    lines.append(f"REDIS_URL set:      {payload['redis_url_set']}")
    lines.append(f"Redis connected:    {payload['redis_connected']}")
    lines.append(f"Queue name:         {payload['queue_name']}")
    lines.append(f"Queue depth:        {payload['queue_depth']}")
    lines.append(f"Workers count:      {payload['workers_count']}")
    lines.append(f"rq library:         {payload['is_rq_available']}")
    lines.append(f"redis library:      {payload['is_redis_available']}")
    if not payload["redis_url_set"]:
        lines.append("")
        lines.append(
            "[note] REDIS_URL is not set. The application degrades "
            "gracefully — /analyze and /jobs/* continue to work via "
            "the pre-existing process-local job_manager. To enable "
            "the M15.0a Redis-backed queue, set REDIS_URL on this "
            "process (or the Render web/worker service)."
        )
    elif not payload["redis_connected"]:
        lines.append("")
        lines.append(
            "[error] REDIS_URL is set but the connection failed. "
            "Verify Redis is reachable from this host."
        )
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import job_queue

    payload = job_queue.get_queue_health(queue_name=args.queue_name)
    payload["is_rq_available"] = job_queue.IS_RQ_AVAILABLE
    payload["is_redis_available"] = job_queue.IS_REDIS_AVAILABLE

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_format_human(payload), end="")

    # Exit code policy: degraded-when-REDIS_URL-unset is informational
    # (exit 0). Only the case "URL set but unreachable" is an operator
    # action signal (exit 1).
    if payload["redis_url_set"] and not payload["redis_connected"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
