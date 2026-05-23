"""Postgres backfill CLI (M12.0b).

Copies existing SQLite rows into the M12.0a Postgres mirror tables.
Three modes:

* ``--status`` — report connectivity and per-table SQLite/Postgres
  counts. Never writes.
* ``--dry-run`` (default) — print what would be backfilled without
  writing. Probes Postgres for "already-present" rows so the
  would-insert count is accurate.
* ``--execute --yes`` — actually backfill. Requires explicit ``--yes``
  to skip the interactive ``YES`` prompt; refuses to run on non-TTY
  stdin without ``--yes``.

Exit codes:
    0 — success (dry-run, status, or execute completed)
    1 — confirmation refused, engine unavailable, or one or more tables
        errored
    2 — CLI usage error

Safety contract:
    * SQLite is the source of truth. The backfill never modifies SQLite.
    * Backfill is idempotent. Re-running is safe.
    * No external network requests are made other than the Postgres
      connection itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


_VALID_TABLES = {
    "analysis_results",
    "jobs",
    "embedding_cache",
    "review_tasks",
    "review_decisions",
    "source_fetch_artifacts",
    "artifact_text_extractions",
    "artifact_evidence_candidates",
    "verdict_producer_comparisons",
    "verdict_label_attributions",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_postgres_backfill",
        description=(
            "Copy existing SQLite rows into the M12.0a Postgres mirror "
            "tables. SQLite is read-only; the backfill is idempotent."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 — success (dry-run, status, or execute completed)\n"
            "  1 — confirmation refused / engine unavailable / errors\n"
            "  2 — CLI usage error\n\n"
            "Safety: SQLite is the source of truth. The backfill never "
            "modifies SQLite. Re-running is safe (idempotent via primary "
            "key or UNIQUE column checks)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Report what would be backfilled without writing. "
            "This is the default when neither --execute nor --status "
            "is given."
        ),
    )
    mode.add_argument(
        "--execute", action="store_true",
        help=(
            "Actually write to Postgres. Requires --yes to skip the "
            "interactive confirmation prompt."
        ),
    )
    mode.add_argument(
        "--status", action="store_true",
        help=(
            "Report connectivity and per-table SQLite/Postgres row "
            "counts. Never writes."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Skip the interactive YES prompt. Required when --execute "
            "is run on non-TTY stdin (e.g. CI). Has no effect on dry-run "
            "or status modes."
        ),
    )
    parser.add_argument(
        "--table", default=None,
        help=(
            "Restrict to a single table. Must be one of the 10 mirror "
            "tables defined in postgres_storage.py."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=(
            "Maximum rows per table (safety cap). Default: no cap."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    return parser


def _format_pad(name: str, width: int = 28) -> str:
    return (name + " " * width)[:width]


def _render_status_human(status: dict) -> str:
    health = status["health"]
    sqlite_counts = status["sqlite_counts"]
    postgres_counts = status["postgres_counts"]
    enabled = health["dual_write_enabled"]
    url_present = health["database_url_present"]
    engine_available = health["engine_available"]
    can_connect = health["can_connect"]

    lines = ["=== Postgres Backfill Status ==="]
    lines.append("")
    lines.append(f"Postgres dual-write enabled: {enabled}")
    lines.append(f"Database URL present:        {url_present}")
    lines.append(f"Engine available:            {engine_available}")
    lines.append(f"Can connect:                 {can_connect}")
    lines.append("")

    if not enabled:
        lines.append("Backfill cannot run: dual-write is disabled.")
        lines.append("")
        lines.append("To enable backfill:")
        lines.append(
            "  1. Provision a Postgres database (Render Postgres, "
            "Neon, etc.)"
        )
        lines.append(
            "  2. Set "
            "DATABASE_URL=postgresql+psycopg://user:pass@host:port/dbname"
        )
        lines.append("  3. Set USE_POSTGRES_WRITE=true")
        lines.append("  4. Run: python scripts/check_postgres_health.py")
        lines.append(
            "  5. If healthy, run: "
            "python scripts/run_postgres_backfill.py --dry-run"
        )
        lines.append("")
        lines.append(
            "[Safety] SQLite remains the source of truth in M12.0a/b."
        )
        lines.append("[Safety] No Postgres connection was attempted.")
        return "\n".join(lines)

    if not can_connect:
        lines.append(
            "Backfill cannot run: Postgres dual-write is enabled but "
            "the SELECT 1 probe failed."
        )
        if health.get("error"):
            lines.append(f"  error: {health['error']}")

    lines.append("Per-table counts:")
    for name in sorted(sqlite_counts.keys()):
        s = sqlite_counts[name]
        p = postgres_counts.get(name, 0)
        lines.append(
            f"  {_format_pad(name)} | SQLite={s:<6} Postgres={p}"
        )
    lines.append("")
    lines.append("[Safety] SQLite is the source of truth.")
    lines.append("[Safety] No writes were performed.")
    return "\n".join(lines)


def _render_dry_run_human(
    status: dict, results: list, mode_label: str,
) -> str:
    sqlite_counts = status["sqlite_counts"]
    postgres_counts = status["postgres_counts"]
    lines = ["=== Postgres Backfill ==="]
    lines.append("")
    lines.append(f"Mode: {mode_label}")
    lines.append(
        f"Postgres dual-write enabled: {status['health']['dual_write_enabled']}"
    )
    lines.append(
        f"Database URL present: {status['health']['database_url_present']}"
    )
    lines.append(
        f"Engine available: {status['health']['engine_available']}"
    )
    lines.append("")

    if not status["health"]["dual_write_enabled"]:
        lines.append(
            "Backfill cannot run: dual-write is disabled "
            "(USE_POSTGRES_WRITE != 'true')."
        )
        lines.append("")
        lines.append(
            "Set USE_POSTGRES_WRITE=true and DATABASE_URL, then "
            "re-run --status before --dry-run."
        )
        return "\n".join(lines)

    lines.append("Per-table preview:")
    total_insert = 0
    total_skip = 0
    total_error = 0
    by_table = {r.table_name: r for r in results}
    for name in sorted(sqlite_counts.keys()):
        r = by_table.get(name)
        s = sqlite_counts[name]
        p = postgres_counts.get(name, 0)
        ins = r.rows_inserted if r else 0
        skip = r.rows_skipped_existing if r else 0
        err = r.rows_errored if r else 0
        total_insert += ins
        total_skip += skip
        total_error += err
        action = "Would insert" if mode_label == "DRY RUN" else "Inserted"
        lines.append(
            f"  {_format_pad(name)} | SQLite={s:<5} Postgres={p:<5} "
            f"{action}: {ins:<4} Skip: {skip:<4} Err: {err}"
        )
    lines.append("")
    if mode_label == "DRY RUN":
        lines.append(
            f"Total: {total_insert} rows would be inserted, "
            f"{total_skip} skipped, {total_error} errors."
        )
        lines.append("")
        lines.append("[Safety] DRY RUN — no Postgres writes performed.")
    else:
        lines.append(
            f"Total: {total_insert} rows inserted, "
            f"{total_skip} skipped, {total_error} errors."
        )
        lines.append("")
        lines.append("[Safety] EXECUTE — Postgres writes performed.")
    lines.append("[Safety] SQLite is the source of truth. SQLite is not modified.")
    lines.append(
        "[Safety] Re-running this command is safe — idempotent via "
        "primary key / UNIQUE checks."
    )
    return "\n".join(lines)


def _stdin_is_tty() -> bool:
    """Defensive isatty check — some CI runners stub stdin in odd ways."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _prompt_interactive_confirmation(prompt_message: str) -> bool:
    """Prompts on stdin for an exact ``YES`` answer."""
    try:
        sys.stdout.write(prompt_message)
        sys.stdout.flush()
        answer = sys.stdin.readline()
    except Exception:  # noqa: BLE001
        return False
    return (answer or "").strip() == "YES"


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.table is not None and args.table not in _VALID_TABLES:
        print(
            f"error: --table must be one of {sorted(_VALID_TABLES)}",
            file=sys.stderr,
        )
        return 2

    # Default mode is dry-run when neither --execute nor --status is set.
    is_status = args.status
    is_execute = args.execute
    is_dry_run = args.dry_run or (not is_execute and not is_status)

    # Import only after argparse so --help works without the dependency
    # being importable.
    import postgres_backfill
    import postgres_storage

    # Ensure cached engine reflects current env vars (operator may have
    # just toggled USE_POSTGRES_WRITE in this shell).
    postgres_storage.reset_engine_for_tests()

    if is_status:
        status = postgres_backfill.collect_status()
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True, default=str))
        else:
            print(_render_status_human(status))
        return 0

    # Dual-write must be enabled for both dry-run and execute modes,
    # because both query the Postgres side for the idempotency probe.
    # Dry-run can still report the configuration mismatch and exit
    # cleanly without going further.
    health = postgres_storage.health_check()
    if not health["dual_write_enabled"]:
        # Both dry-run and execute report disabled state.
        status = postgres_backfill.collect_status()
        if args.json:
            payload = {
                "mode": "dry_run" if is_dry_run else "execute",
                "ran": False,
                "reason": "dual_write_disabled",
                "status": status,
            }
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(_render_dry_run_human(
                status, [], "DRY RUN" if is_dry_run else "EXECUTE",
            ))
        # Dry-run returns 0 (informational). Execute returns 1 because
        # the operator asked for a write that cannot happen.
        return 0 if is_dry_run else 1

    # When executing, require --yes OR an interactive YES from a TTY.
    if is_execute and not args.yes:
        if not _stdin_is_tty():
            print(
                "error: --execute requires --yes when stdin is not a "
                "TTY (refuses to proceed without explicit confirmation)",
                file=sys.stderr,
            )
            return 1
        # Compute the to-be-affected row count for the prompt.
        preview_results = postgres_backfill.backfill_all_tables(
            dry_run=True,
            limit=args.limit,
            only_table=args.table,
        )
        n_rows = sum(r.rows_inserted for r in preview_results)
        n_tables = len(preview_results)
        prompt = (
            f"\nThis will copy {n_rows} rows from SQLite to Postgres "
            f"across {n_tables} table(s).\n"
            "SQLite will NOT be modified. The backfill is idempotent.\n"
            "Type YES to proceed: "
        )
        if not _prompt_interactive_confirmation(prompt):
            print("Confirmation refused — no writes performed.")
            return 1

    # Run the backfill (dry-run or execute).
    results = postgres_backfill.backfill_all_tables(
        dry_run=is_dry_run,
        limit=args.limit,
        only_table=args.table,
    )

    # Build the status payload for the human report.
    status_snapshot = postgres_backfill.collect_status()

    if args.json:
        payload = {
            "mode": "dry_run" if is_dry_run else "execute",
            "ran": True,
            "summary": postgres_backfill.summarize_results(results),
            "status": status_snapshot,
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        mode_label = "DRY RUN" if is_dry_run else "EXECUTE"
        print(_render_dry_run_human(status_snapshot, results, mode_label))

    any_errored = any(r.rows_errored > 0 or r.errors for r in results)
    return 1 if any_errored else 0


if __name__ == "__main__":
    sys.exit(main())
