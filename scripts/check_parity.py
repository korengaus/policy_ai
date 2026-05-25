"""Postgres dual-write parity check (M12.1).

Read-only CLI that compares per-table row counts (and, optionally, the
per-row identity sets) between SQLite and the Postgres mirror. The
script writes NOTHING to either database under any circumstance.

Three signals are reported per mirror table:

* ``sqlite_count`` — ``SELECT COUNT(*)`` against the local SQLite.
* ``postgres_count`` — ``SELECT COUNT(*)`` against the Postgres mirror
  table (0 when the engine is unavailable or the table is missing).
* ``in_parity`` — ``sqlite_count == postgres_count``.

When ``--sample`` is passed the script additionally fetches up to
``--sample-limit`` ``id`` (or unique-key) values from each side, computes
the set difference, and reports a bounded preview of rows that exist
only in one place. The previews are capped at 20 entries so a large
drift cannot flood the report.

Usage:
    python scripts/check_parity.py
    python scripts/check_parity.py --json
    python scripts/check_parity.py --table analysis_results
    python scripts/check_parity.py --sample --sample-limit 200
    python scripts/check_parity.py --strict

Exit codes:
    0 — parity check ran cleanly AND every table is in parity
        (or dual-write is disabled, which is a no-op pass)
    1 — at least one table is out of parity, OR --strict was passed
        and dual-write is enabled but Postgres is unreachable
    2 — CLI usage error

Safety contract:
    * Read-only on both SQLite and Postgres. No INSERT / UPDATE / DELETE
      is ever issued.
    * Re-running is safe and idempotent.
    * No external network requests other than the Postgres connection
      itself.
    * SQLite remains the source of truth — a parity report saying
      "drift" never implies SQLite is wrong; it implies Postgres is
      behind (or that an unexpected row entered Postgres via some
      other path).

This script is the M12.1 companion to ``check_postgres_health.py``
(connectivity probe) and ``run_postgres_backfill.py`` (row mover).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# Bounded preview cap. Drift reports never include more than this many
# example ids per side per table, so a large drift cannot flood the
# operator's terminal or the JSON payload.
_MAX_PREVIEW_PER_SIDE = 20


# Per-table identity column used by --sample mode. Mirrors the
# idempotency strategies declared in postgres_backfill.get_backfill_specs:
# tables backfilled via skip_existing_id use "id"; tables backfilled via
# upsert_by_columns or skip_existing_unique use the documented unique
# column. embedding_cache has a composite unique key
# (text_hash, provider, model) — we represent it as a tuple of those
# three columns; the preview formatter joins with "|".
_IDENTITY_COLUMNS: Dict[str, List[str]] = {
    "analysis_results": ["id"],
    "jobs": ["id"],
    "embedding_cache": ["text_hash", "provider", "model"],
    "review_tasks": ["idempotency_key"],
    "review_decisions": ["decision_id"],
    "source_fetch_artifacts": ["id"],
    "artifact_text_extractions": ["id"],
    "artifact_evidence_candidates": ["id"],
    "verdict_producer_comparisons": ["input_hash"],
    "verdict_label_attributions": ["analysis_id"],
}


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_parity",
        description=(
            "Compare per-table row counts (and optionally per-row "
            "identity sets) between SQLite and the Postgres mirror. "
            "Read-only on both sides. SQLite remains the source of truth."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 — every mirror table is in parity (or dual-write is "
            "disabled, which is a no-op pass)\n"
            "  1 — at least one table is out of parity, or --strict was "
            "set and Postgres is unreachable\n"
            "  2 — CLI usage error\n\n"
            "Safety: read-only on both databases. Never modifies "
            "SQLite or Postgres."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    parser.add_argument(
        "--table", default=None,
        help=(
            "Restrict to a single mirror table. Must be one of the 10 "
            "mirror tables defined in postgres_storage.py."
        ),
    )
    parser.add_argument(
        "--sample", action="store_true",
        help=(
            "Fetch identity columns from each side and report a bounded "
            "preview of rows that exist on only one side. Slower than "
            "the default count-only mode but catches same-count drift."
        ),
    )
    parser.add_argument(
        "--sample-limit", type=int, default=500,
        help=(
            "Maximum identity rows to fetch per side when --sample is "
            "set. Default: %(default)s. The preview itself is always "
            "bounded to 20 entries per side per table."
        ),
    )
    parser.add_argument(
        "--strict", action="store_true",
        help=(
            "When dual-write is ENABLED but Postgres cannot be reached, "
            "exit 1 instead of the default exit 0. Has no effect when "
            "dual-write is disabled (still exits 0)."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Core parity computation — exposed for tests.
# ---------------------------------------------------------------------------


def _format_identity(row: Any, columns: List[str]) -> str:
    """Render an identity tuple as a stable string for preview output.
    Single-column ids print as ``"42"``; composite keys print as
    ``"abc|openai|text-embedding-3-small"``."""
    if isinstance(row, tuple):
        values = row
    elif isinstance(row, (list,)):
        values = tuple(row)
    else:
        values = (row,)
    return "|".join("" if v is None else str(v) for v in values)


def _sample_sqlite_identities(
    table_name: str, columns: List[str], limit: int,
) -> List[tuple]:
    """Read identity tuples from SQLite. Returns [] on any error."""
    import database  # local import keeps --help cheap
    import sqlite3

    if not columns:
        return []
    select_cols = ", ".join(columns)
    sql = f"SELECT {select_cols} FROM {table_name}"
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    try:
        with database.get_connection() as connection:
            rows = connection.execute(sql).fetchall()
        return [tuple(row[c] for c in columns) for row in rows]
    except sqlite3.OperationalError:
        return []
    except Exception:  # noqa: BLE001
        return []


def _sample_postgres_identities(
    engine: Any, table_name: str, columns: List[str], limit: int,
) -> List[tuple]:
    """Read identity tuples from the Postgres mirror. Returns [] on any
    error including engine-missing / table-missing."""
    if engine is None or not columns:
        return []
    try:
        import sqlalchemy as sa
        import postgres_storage
        from sqlalchemy.exc import SQLAlchemyError
    except Exception:  # noqa: BLE001
        return []

    table = postgres_storage._metadata.tables.get(table_name)
    if table is None:
        return []
    try:
        cols = [table.c[c] for c in columns]
    except KeyError:
        return []
    try:
        stmt = sa.select(*cols)
        if limit is not None and limit > 0:
            stmt = stmt.limit(int(limit))
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [tuple(row) for row in rows]
    except SQLAlchemyError:
        return []
    except Exception:  # noqa: BLE001
        return []


def compute_parity_for_table(
    table_name: str,
    sqlite_count: int,
    postgres_count: int,
    *,
    engine: Any = None,
    sample: bool = False,
    sample_limit: int = 500,
) -> dict:
    """Build the parity record for one table.

    Pure-ish: counts are passed in by the caller. When ``sample`` is
    True and the table has a known identity column set, the function
    fetches identity tuples from both sides (read-only) and computes
    the set difference. The preview list per side is capped at
    :data:`_MAX_PREVIEW_PER_SIDE` entries.
    """
    columns = _IDENTITY_COLUMNS.get(table_name, [])
    record: Dict[str, Any] = {
        "table": table_name,
        "sqlite_count": int(sqlite_count),
        "postgres_count": int(postgres_count),
        "delta": int(sqlite_count) - int(postgres_count),
        "in_parity": int(sqlite_count) == int(postgres_count),
        "identity_columns": columns,
    }

    if not sample or not columns:
        record["sampled"] = False
        return record

    sqlite_keys = set(
        _sample_sqlite_identities(table_name, columns, sample_limit)
    )
    postgres_keys = set(
        _sample_postgres_identities(engine, table_name, columns, sample_limit)
    )
    sqlite_only = sqlite_keys - postgres_keys
    postgres_only = postgres_keys - sqlite_keys
    record["sampled"] = True
    record["sample_limit"] = int(sample_limit)
    record["sqlite_keys_sampled"] = len(sqlite_keys)
    record["postgres_keys_sampled"] = len(postgres_keys)
    record["sqlite_only_count"] = len(sqlite_only)
    record["postgres_only_count"] = len(postgres_only)
    record["sqlite_only_preview"] = sorted(
        _format_identity(k, columns) for k in list(sqlite_only)[
            :_MAX_PREVIEW_PER_SIDE
        ]
    )
    record["postgres_only_preview"] = sorted(
        _format_identity(k, columns) for k in list(postgres_only)[
            :_MAX_PREVIEW_PER_SIDE
        ]
    )
    # in_parity is downgraded to False if the sampled identity sets
    # disagree, even when counts happen to match (catches the
    # same-count-different-rows drift case).
    if sqlite_only or postgres_only:
        record["in_parity"] = False
    return record


def collect_parity_report(
    *,
    only_table: Optional[str] = None,
    sample: bool = False,
    sample_limit: int = 500,
) -> dict:
    """Build the full parity report.

    Wraps :func:`postgres_backfill.collect_status` for the per-table
    counts, then layers identity-set sampling on top when ``sample`` is
    True. Never raises; the ``health`` block always reflects current
    connectivity so the caller can decide policy.
    """
    import postgres_backfill
    import postgres_storage

    status = postgres_backfill.collect_status()
    health = status["health"]
    sqlite_counts = status["sqlite_counts"]
    postgres_counts = status["postgres_counts"]

    engine = None
    if sample and health["dual_write_enabled"]:
        engine = postgres_storage.get_engine()

    table_names = sorted(sqlite_counts.keys())
    if only_table is not None:
        table_names = [t for t in table_names if t == only_table]

    per_table: Dict[str, Any] = {}
    # When dual-write is disabled, the Postgres counts are all zero (no
    # engine) while the SQLite counts reflect real data. A row-by-row
    # comparison would surface misleading "drift" rows even though the
    # mirror is intentionally empty. The human report and the exit-code
    # policy already special-case the disabled branch — keep per_table
    # empty in JSON output for the same reason so consumers cannot
    # accidentally act on the bogus delta.
    if health["dual_write_enabled"]:
        for name in table_names:
            per_table[name] = compute_parity_for_table(
                name,
                sqlite_counts.get(name, 0),
                postgres_counts.get(name, 0),
                engine=engine,
                sample=sample,
                sample_limit=sample_limit,
            )

    summary = summarize_parity(per_table)
    return {
        "health": health,
        "summary": summary,
        "per_table": per_table,
        "sampled": bool(sample),
        "sample_limit": int(sample_limit) if sample else None,
        "only_table": only_table,
    }


def summarize_parity(per_table: Dict[str, Any]) -> dict:
    """Aggregate the per-table records into a single summary."""
    in_parity = [t for t, r in per_table.items() if r.get("in_parity")]
    drift = [t for t, r in per_table.items() if not r.get("in_parity")]
    return {
        "tables_checked": len(per_table),
        "tables_in_parity": len(in_parity),
        "tables_with_drift": len(drift),
        "any_drift": bool(drift),
        "drift_tables": sorted(drift),
        "total_delta_abs": sum(
            abs(r.get("delta", 0)) for r in per_table.values()
        ),
    }


# ---------------------------------------------------------------------------
# Human-readable rendering
# ---------------------------------------------------------------------------


def _format_pad(name: str, width: int = 28) -> str:
    return (name + " " * width)[:width]


def _render_human(report: dict) -> str:
    health = report["health"]
    summary = report["summary"]
    per_table = report["per_table"]

    lines = ["=== Postgres Dual-Write Parity ==="]
    lines.append("")
    lines.append(f"Dual-write enabled:  {health['dual_write_enabled']}")
    lines.append(f"Database URL present:{health['database_url_present']}")
    lines.append(f"Engine available:    {health['engine_available']}")
    lines.append(f"Can connect:         {health['can_connect']}")
    lines.append("")

    if not health["dual_write_enabled"]:
        lines.append(
            "Parity check is a no-op: dual-write is disabled "
            "(USE_POSTGRES_WRITE != 'true')."
        )
        lines.append(
            "[Safety] SQLite is the sole source of truth. Nothing to "
            "compare."
        )
        return "\n".join(lines)

    if not health["can_connect"]:
        lines.append(
            "Parity check cannot run: Postgres SELECT 1 probe failed."
        )
        if health.get("error"):
            lines.append(f"  error: {health['error']}")
        lines.append("")

    lines.append("Per-table parity:")
    for name in sorted(per_table.keys()):
        r = per_table[name]
        marker = "OK " if r["in_parity"] else "!! "
        line = (
            f"  {marker}{_format_pad(name)} | "
            f"SQLite={r['sqlite_count']:<6} "
            f"Postgres={r['postgres_count']:<6} "
            f"delta={r['delta']:+d}"
        )
        if r.get("sampled"):
            so = r.get("sqlite_only_count", 0)
            po = r.get("postgres_only_count", 0)
            line += f"  sqlite_only={so}  postgres_only={po}"
        lines.append(line)

    # Drift detail block — only printed when at least one table drifted
    # AND --sample mode produced previews.
    drift_lines: List[str] = []
    for name in sorted(per_table.keys()):
        r = per_table[name]
        if r.get("in_parity"):
            continue
        previews_present = r.get("sampled") and (
            r.get("sqlite_only_preview") or r.get("postgres_only_preview")
        )
        if not previews_present:
            continue
        drift_lines.append("")
        drift_lines.append(f"  [{name}] drift detail")
        if r.get("sqlite_only_preview"):
            drift_lines.append(
                f"    sqlite_only ({r['sqlite_only_count']}):"
            )
            for key in r["sqlite_only_preview"]:
                drift_lines.append(f"      {key}")
        if r.get("postgres_only_preview"):
            drift_lines.append(
                f"    postgres_only ({r['postgres_only_count']}):"
            )
            for key in r["postgres_only_preview"]:
                drift_lines.append(f"      {key}")
    if drift_lines:
        lines.append("")
        lines.append("Drift previews (capped at 20 keys per side):")
        lines.extend(drift_lines)

    lines.append("")
    lines.append("Summary:")
    lines.append(
        f"  tables_checked:     {summary['tables_checked']}"
    )
    lines.append(
        f"  tables_in_parity:   {summary['tables_in_parity']}"
    )
    lines.append(
        f"  tables_with_drift:  {summary['tables_with_drift']}"
    )
    lines.append(
        f"  total_delta_abs:    {summary['total_delta_abs']}"
    )
    lines.append("")
    if summary["any_drift"]:
        lines.append(
            "Status: DRIFT detected. Run "
            "`python scripts/run_postgres_backfill.py --dry-run` "
            "to preview what would be copied."
        )
    else:
        lines.append(
            "Status: parity OK — SQLite and Postgres row counts match "
            "on every mirror table."
        )

    lines.append("")
    lines.append("[Safety] Read-only on both databases.")
    lines.append("[Safety] SQLite is the source of truth.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _valid_table_names() -> set:
    """Authoritative list of mirror tables — derived from the identity
    column map at module load time so this stays in sync with the
    backfill spec catalogue."""
    return set(_IDENTITY_COLUMNS.keys())


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.table is not None and args.table not in _valid_table_names():
        print(
            f"error: --table must be one of {sorted(_valid_table_names())}",
            file=sys.stderr,
        )
        return 2

    if args.sample_limit is not None and args.sample_limit < 0:
        print("error: --sample-limit must be >= 0", file=sys.stderr)
        return 2

    # Import only after argparse so --help works without the dependency
    # being importable in the operator's env.
    import postgres_storage

    # Refresh the cached engine to reflect current env vars (the operator
    # may have just toggled USE_POSTGRES_WRITE in this shell).
    postgres_storage.reset_engine_for_tests()

    report = collect_parity_report(
        only_table=args.table,
        sample=args.sample,
        sample_limit=args.sample_limit,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(_render_human(report))

    health = report["health"]
    summary = report["summary"]

    # Exit policy:
    # * 0 when dual-write disabled (informational no-op)
    # * 0 when enabled + parity holds
    # * 1 when enabled + parity drift detected
    # * 1 when --strict and enabled + cannot connect
    if not health["dual_write_enabled"]:
        return 0
    if not health["can_connect"]:
        return 1 if args.strict else 0
    return 1 if summary["any_drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
