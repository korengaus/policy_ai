"""Postgres dual-write parity check (M12.1) — NEUTRALIZED in M12.0e-5a.

DEPRECATED: PG-only since M12.0e-5a. Stage 0e-5a removed the SQLite
write-fallback — SQLite is no longer a write target and Postgres is the
sole durable store. With SQLite never written, it is permanently empty,
so a SQLite-vs-Postgres parity comparison is vacuous. As of M12.0e-5b
this CLI is a no-op that reports "parity OK" and always exits 0; it no
longer compares counts or identity sets against SQLite.

The script writes NOTHING to either database under any circumstance.

Historical behavior (pre-M12.0e-5b): three signals were reported per
mirror table — ``sqlite_count``, ``postgres_count``, and ``in_parity``
(``sqlite_count == postgres_count``). M12.0e-6b-3 removed the
``--sample`` identity-set helpers (they used ``database.get_connection``,
now retired). The pure count helpers (:func:`compute_parity_for_table`,
:func:`summarize_parity`) are retained for the count-math unit tests but
are no longer reached by :func:`collect_parity_report`.

Usage:
    python scripts/check_parity.py
    python scripts/check_parity.py --json
    python scripts/check_parity.py --table analysis_results
    python scripts/check_parity.py --sample --sample-limit 200
    python scripts/check_parity.py --strict   (no effect — deprecated)

Exit codes:
    0 — always (deprecated no-op; parity is vacuous under PG-only)
    2 — CLI usage error (unknown --table, negative --sample-limit)

Safety contract:
    * Read-only on both SQLite and Postgres. No INSERT / UPDATE / DELETE
      is ever issued.
    * Re-running is safe and idempotent.
    * No external network requests other than the Postgres connection
      itself.
    * Postgres is the sole source of truth (M12.0e-5a). SQLite is no
      longer written and is not consulted for parity.

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


def compute_parity_for_table(
    table_name: str,
    sqlite_count: int,
    postgres_count: int,
) -> dict:
    """Build the parity record for one table (count comparison only).

    M12.0e-6b-3: the ``--sample`` identity-set comparison was removed
    with the SQLite sampling helpers (``_sample_sqlite_identities`` used
    ``database.get_connection``, now retired). Parity is count-based.
    The neutralized :func:`collect_parity_report` does not call this —
    it is kept for the count-math unit tests."""
    columns = _IDENTITY_COLUMNS.get(table_name, [])
    return {
        "table": table_name,
        "sqlite_count": int(sqlite_count),
        "postgres_count": int(postgres_count),
        "delta": int(sqlite_count) - int(postgres_count),
        "in_parity": int(sqlite_count) == int(postgres_count),
        "identity_columns": columns,
        "sampled": False,
    }


def collect_parity_report(
    *,
    only_table: Optional[str] = None,
    sample: bool = False,
    sample_limit: int = 500,
) -> dict:
    """Build the (deprecated) parity report.

    NEUTRALIZED in M12.0e-5b. SQLite is no longer a write target
    (M12.0e-5a), so SQLite-vs-Postgres parity is vacuous. This function
    now short-circuits to a no-op report: ``per_table`` is empty and
    ``summary.any_drift`` is always False. The ``health`` block is
    sourced directly from :func:`postgres_storage.health_check` so
    operators keep visibility into current Postgres connectivity.
    (M12.0e-6b-2: repointed off the retired ``postgres_backfill`` — its
    ``collect_status`` health block was just ``health_check`` anyway.)
    The per-table count/identity comparison is NOT performed — the
    engine is never touched here.

    The report dict keys are preserved exactly (``health`` / ``summary``
    / ``per_table`` / ``sampled`` / ``sample_limit`` / ``only_table``) so
    existing consumers (run_operational_checks parsers, test_check_parity)
    continue to read the same shape. Never raises.
    """
    import postgres_storage

    health = postgres_storage.health_check()

    # M12.0e-5b: no comparison. per_table stays empty; summarize_parity
    # over an empty map yields tables_checked=0, any_drift=False.
    per_table: Dict[str, Any] = {}
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
    """Render the deprecated no-op report (M12.0e-5b).

    SQLite is no longer written (M12.0e-5a), so there is nothing to
    compare. The render keeps the historical header and reports
    "parity OK" unconditionally; it never emits per-table drift. The
    ``health`` block is preserved for operator connectivity visibility.
    """
    health = report["health"]

    lines = ["=== Postgres Dual-Write Parity ==="]
    lines.append("")
    lines.append(
        "[deprecated] PG-only since M12.0e-5a — SQLite is no longer "
        "written; parity is vacuous. This check is a no-op."
    )
    lines.append("")
    lines.append(f"Dual-write enabled:  {health['dual_write_enabled']}")
    lines.append(f"Database URL present:{health['database_url_present']}")
    lines.append(f"Engine available:    {health['engine_available']}")
    lines.append(f"Can connect:         {health['can_connect']}")
    lines.append("")
    lines.append(
        "Status: parity OK — PG-only mode (M12.0e-5a); nothing to "
        "compare."
    )
    lines.append("")
    lines.append("[Safety] Read-only on both databases.")
    lines.append(
        "[Safety] Postgres is the sole source of truth (M12.0e-5a)."
    )
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

    # M12.0e-5b exit policy: always 0. Parity is vacuous under PG-only
    # (SQLite is no longer written), so there is no drift to detect and
    # --strict has nothing to be strict about. CLI usage errors still
    # return 2 above, before the report is built.
    return 0


if __name__ == "__main__":
    sys.exit(main())
