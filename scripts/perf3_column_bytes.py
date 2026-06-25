"""PERF-3 — per-column byte probe for the GET /history payload.

READ-ONLY diagnostic. Performs ONLY SELECT / octet_length / pg_column_size /
row_to_json-for-sizing. NO writes, NO updates, NO deletes, NO verdict path,
NO stored value changed. Pin-OUT (new scripts/ file).

WHY: PERF-2 dropped the columns we BELIEVED were heavy, but GET /history?limit=50
is still ~16MB. So the real weight is in a column we KEPT in the slim whitelist.
This probe MEASURES the actual per-column serialized bytes for the exact
/history?limit=50 window (latest 50 rows by id DESC), ranks them, and proves
whether the slim projection is taking effect.

Run it in the Render Worker Shell (after confirming the deploy commit):

    git log --oneline -1            # expect d565490 (PERF-2) or later
    PYTHONPATH=. python scripts/perf3_column_bytes.py
    PYTHONPATH=. python scripts/perf3_column_bytes.py --limit 50 --json

Exit codes:
    0 — measured and reported
    1 — engine unavailable (dual-write disabled / DATABASE_URL unset)
    2 — CLI usage error
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
        prog="perf3_column_bytes",
        description=(
            "READ-ONLY: measure per-column serialized bytes for the latest "
            "N analysis_results rows (the GET /history?limit=N window) and "
            "rank them to find the real heavy column. SELECT/length only."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Row window to measure (default 50 — matches /history?limit=50).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human table.",
    )
    return parser


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:,.1f} kB"


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    limit = max(1, min(int(args.limit or 50), 1000))

    import sqlalchemy as sa
    import postgres_storage
    from postgres_storage import analysis_results_table

    # The exact column set the slim reader projects (single source of truth).
    slim_cols = list(postgres_storage._SLIM_LIST_COLUMNS)
    all_cols = list(analysis_results_table.columns.keys())
    slim_set = set(slim_cols)
    dropped_cols = [c for c in all_cols if c not in slim_set]

    engine = postgres_storage.get_engine()
    if engine is None:
        print(
            "ERROR: Postgres engine unavailable (dual-write disabled or "
            "DATABASE_URL unset). Cannot probe prod columns.",
            file=sys.stderr,
        )
        return 1

    # ----- Step 1 proof: what keys does the slim reader actually return? -----
    slim_sample = postgres_storage.read_recent_analysis_results_slim(2) or []
    slim_returned_keys = sorted(slim_sample[0].keys()) if slim_sample else []
    heavy_keys_present = sorted(k for k in slim_returned_keys if k in set(dropped_cols))

    base = f"SELECT * FROM analysis_results ORDER BY id DESC LIMIT {limit}"

    # ----- Step 2/3: per-column totals over the latest-N window -----
    # octet_length(coalesce(col::text,'')) == the serialized text byte length
    # of that field (the JSON columns are stored as TEXT, so this is what ends
    # up in the response string, minus per-field JSON quoting/escaping).
    # pg_column_size is reported as an in-memory-datum cross-check.
    select_terms = []
    for c in all_cols:
        select_terms.append(
            "sum(octet_length(coalesce(t.\"%s\"::text, '')))::bigint AS \"tot_%s\"" % (c, c)
        )
        select_terms.append(
            "round(avg(octet_length(coalesce(t.\"%s\"::text, ''))))::bigint AS \"avg_%s\"" % (c, c)
        )
        select_terms.append(
            "sum(pg_column_size(t.\"%s\"))::bigint AS \"pg_%s\"" % (c, c)
        )

    with engine.connect() as conn:
        n_rows = conn.execute(
            sa.text(f"SELECT count(*) FROM ({base}) t")
        ).scalar() or 0

        per_col = conn.execute(
            sa.text(f"SELECT {', '.join(select_terms)} FROM ({base}) t")
        ).mappings().first() or {}

        # Whole-row serialized total for the same window (SELECT *).
        whole_total = conn.execute(
            sa.text(
                f"SELECT coalesce(sum(octet_length(row_to_json(t)::text)),0)::bigint "
                f"FROM ({base}) t"
            )
        ).scalar() or 0

        # Slim-kept serialized total (only the whitelisted columns).
        slim_select = ", ".join('"%s"' % c for c in slim_cols)
        slim_total = conn.execute(
            sa.text(
                "SELECT coalesce(sum(octet_length(row_to_json(t)::text)),0)::bigint "
                "FROM (SELECT %s FROM analysis_results ORDER BY id DESC LIMIT %d) t"
                % (slim_select, limit)
            )
        ).scalar() or 0

    # Assemble ranking.
    cols = []
    for c in all_cols:
        tot = int(per_col.get(f"tot_{c}") or 0)
        avg = int(per_col.get(f"avg_{c}") or 0)
        pg = int(per_col.get(f"pg_{c}") or 0)
        cols.append({
            "column": c,
            "kept": c in slim_set,
            "total_bytes": tot,
            "avg_bytes": avg,
            "pg_total_bytes": pg,
            "share_of_whole": (tot / whole_total) if whole_total else 0.0,
        })
    cols.sort(key=lambda r: r["total_bytes"], reverse=True)

    if args.json:
        print(json.dumps({
            "rows_measured": n_rows,
            "limit": limit,
            "whole_row_total_bytes": whole_total,
            "slim_kept_total_bytes": slim_total,
            "slim_returned_keys": slim_returned_keys,
            "heavy_keys_still_present_in_slim": heavy_keys_present,
            "columns": cols,
        }, indent=2, ensure_ascii=False))
        return 0

    print("=== PERF-3 per-column byte probe (READ-ONLY) ===")
    print(f"rows measured:        {n_rows} (window: latest {limit} by id DESC)")
    print(f"whole-row total:      {_fmt_kb(whole_total)}  ({whole_total:,} bytes)")
    print(f"slim-kept total:      {_fmt_kb(slim_total)}  ({slim_total:,} bytes)")
    if whole_total:
        print(f"slim / whole:         {slim_total / whole_total * 100:.1f}%")
    print()
    print("--- Step 1: is the slim projection in effect (in this process)? ---")
    print(f"slim reader returned {len(slim_returned_keys)} keys.")
    if heavy_keys_present:
        print(f"!! HEAVY (dropped) keys STILL present in slim output: {heavy_keys_present}")
        print("   -> routing/deploy problem: slim reader not actually used.")
    else:
        print("OK: none of the dropped heavy keys are in the slim reader output.")
    print()
    print("--- Step 2/3: columns ranked by serialized total_bytes (desc) ---")
    print(f"{'rank':>4}  {'column':30} {'kept':>4}  {'total':>13}  {'avg/row':>10}  {'share':>7}  {'pg_total':>13}")
    for i, r in enumerate(cols, 1):
        print(
            f"{i:>4}  {r['column']:30} {('Y' if r['kept'] else 'drop'):>4}  "
            f"{_fmt_kb(r['total_bytes']):>13}  {_fmt_kb(r['avg_bytes']):>10}  "
            f"{r['share_of_whole'] * 100:>6.1f}%  {_fmt_kb(r['pg_total_bytes']):>13}"
        )
    print()
    kept_ranked = [r for r in cols if r["kept"]]
    print("--- Verdict: heaviest KEPT columns (the real /history weight) ---")
    for r in kept_ranked[:5]:
        print(f"  {r['column']:30} {_fmt_kb(r['total_bytes']):>13}  ({r['share_of_whole'] * 100:.1f}% of whole row)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
