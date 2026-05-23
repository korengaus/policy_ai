"""Postgres dual-write health diagnostic (M12.0a).

Read-only CLI that reports whether the dual-write infrastructure is
enabled, configured, and reachable. Performs NO writes, NO reads from
SQLite, NO calls to ``analyze_pipeline``, and NO external network
requests other than the Postgres ``SELECT 1`` probe that the
``health_check()`` helper issues internally.

Usage:
    python scripts/check_postgres_health.py
    python scripts/check_postgres_health.py --json
    python scripts/check_postgres_health.py --ensure-schema
    python scripts/check_postgres_health.py --help

Exit codes:
    0 — status reported successfully (whether enabled or disabled)
    1 — dual-write enabled but cannot connect to Postgres
        (operator action needed)
    2 — CLI usage error

The script is intentionally minimal so the rule "SQLite is the source
of truth" stays visible at every operator touch point: when dual-write
is disabled, the CLI emits the safety footer ahead of any other action.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Make the project root importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_postgres_health",
        description=(
            "Report Postgres dual-write status. Read-only — performs "
            "no SQLite reads, no analyze_pipeline calls, and no "
            "external network requests other than a single "
            "'SELECT 1' against Postgres when dual-write is enabled."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 — status reported (dual-write disabled, or enabled "
            "and reachable)\n"
            "  1 — dual-write enabled but cannot connect "
            "(operator action needed)\n"
            "  2 — CLI usage error\n\n"
            "M12.0a — SQLite remains the source of truth. Dual-write "
            "is OFF by default; setting USE_POSTGRES_WRITE=true is an "
            "operator decision, not a deployment default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    parser.add_argument(
        "--ensure-schema",
        action="store_true",
        help=(
            "When dual-write is enabled, call create_all (idempotent) "
            "to ensure the mirror tables exist. No-op otherwise."
        ),
    )
    return parser


def _render_human(status: dict, ensure_schema_attempted: bool,
                  ensure_schema_ok: bool) -> str:
    enabled = status["dual_write_enabled"]
    url_present = status["database_url_present"]
    engine_available = status["engine_available"]
    can_connect = status["can_connect"]
    tables = status["tables_defined"]
    error = status.get("error")

    lines = ["=== Postgres Dual-Write Health ==="]
    lines.append("")
    lines.append(f"dual_write_enabled:    {enabled}")
    lines.append(f"database_url_present:  {url_present}")
    lines.append(f"engine_available:      {engine_available}")
    lines.append(f"can_connect:           {can_connect}")
    lines.append(
        f"tables_defined:        {len(tables)} tables "
        f"[{', '.join(tables[:5])}{', ...' if len(tables) > 5 else ''}]"
    )
    if error:
        lines.append(f"error:                 {error}")
    lines.append("")

    if not enabled:
        lines.append(
            "Status: dual-write is currently DISABLED "
            "(USE_POSTGRES_WRITE != true)."
        )
        lines.append(
            "SQLite is the sole source of truth. "
            "No Postgres connection attempted."
        )
    elif not url_present:
        lines.append(
            "Status: dual-write is ENABLED via USE_POSTGRES_WRITE=true "
            "but DATABASE_URL is unset."
        )
        lines.append(
            "Operator action: set DATABASE_URL to a valid Postgres "
            "connection string."
        )
    elif not engine_available:
        lines.append(
            "Status: dual-write is ENABLED but the SQLAlchemy engine "
            "could not be created."
        )
        lines.append(
            "Operator action: inspect the warning in the log for the "
            "engine-creation failure."
        )
    elif not can_connect:
        lines.append(
            "Status: dual-write is ENABLED but the Postgres connection "
            "probe (SELECT 1) failed."
        )
        lines.append(
            "Operator action: verify the DATABASE_URL host, port, "
            "credentials, and reachability."
        )
    else:
        lines.append(
            "Status: dual-write is ENABLED. "
            "Postgres is being mirrored alongside SQLite."
        )

    if ensure_schema_attempted:
        lines.append("")
        if ensure_schema_ok:
            lines.append(
                "--ensure-schema: create_all succeeded "
                "(idempotent — pre-existing tables left alone)."
            )
        else:
            lines.append(
                "--ensure-schema: create_all did not run (engine "
                "unavailable) or returned False. See logs."
            )

    lines.append("")
    lines.append("[Safety] SQLite remains the source of truth.")
    lines.append(
        "[Safety] Postgres failures (if any) never break the SQLite "
        "write path."
    )
    lines.append(
        "[Safety] This milestone (M12.0a) does NOT enable dual-write "
        "on Render."
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on usage error, 0 on --help. Preserve both.
        return int(exc.code) if isinstance(exc.code, int) else 2

    # Import postgres_storage AFTER argparse so --help does not require
    # the dependency to be installed in the operator's local env.
    import postgres_storage

    status = postgres_storage.health_check()

    ensure_schema_attempted = False
    ensure_schema_ok = False
    if args.ensure_schema and status["dual_write_enabled"]:
        engine = postgres_storage.get_engine()
        if engine is not None:
            ensure_schema_attempted = True
            ensure_schema_ok = postgres_storage.ensure_schema(engine)

    if args.json:
        payload = dict(status)
        if args.ensure_schema:
            payload["ensure_schema_attempted"] = ensure_schema_attempted
            payload["ensure_schema_ok"] = ensure_schema_ok
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_human(status, ensure_schema_attempted, ensure_schema_ok))

    # Exit policy:
    # * 0 when dual-write is disabled (the default; nothing to verify)
    # * 0 when enabled AND can_connect
    # * 1 when enabled but unable to connect (operator action needed)
    if status["dual_write_enabled"] and not status["can_connect"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
