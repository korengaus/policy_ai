"""Verify SQLite/Postgres dual-write parity.

Reports row counts from SQLite and Postgres and prints readable diffs.
- Exits 0 when Postgres is disabled (missing DATABASE_URL).
- Exits 0 when counts can be compared, even if they diverge (a diff is expected
  before USE_POSTGRES_WRITE is enabled or right after migrations).
- Exits non-zero only for real connection or schema errors.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.postgres import get_database_url, get_session, is_postgres_enabled  # noqa: E402


SQLITE_PATH = Path(__file__).resolve().parent.parent / "policy_ai.db"


def _sqlite_count() -> int:
    if not SQLITE_PATH.exists():
        print(f"[verify] SQLite db not found at {SQLITE_PATH}; treating as 0 rows.")
        return 0
    try:
        with sqlite3.connect(SQLITE_PATH) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM analysis_results"
            ).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.OperationalError as error:
        print(f"[verify] SQLite schema not initialized yet ({error}); treating as 0 rows.")
        return 0


def _postgres_counts() -> dict:
    from sqlalchemy import text

    session = get_session()
    if session is None:
        raise RuntimeError("Postgres session unavailable despite DATABASE_URL being set.")
    try:
        counts = {}
        for table in ("stories", "claims", "verdicts", "jobs", "audit_log"):
            row = session.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()
            counts[table] = int(row[0]) if row else 0
        return counts
    finally:
        session.close()


def main() -> int:
    if not is_postgres_enabled():
        print("[verify] DATABASE_URL not set -- Postgres is disabled. Nothing to compare.")
        print(f"[verify] SQLite analysis_results rows: {_sqlite_count()}")
        return 0

    print(f"[verify] DATABASE_URL detected; comparing against {get_database_url()!r}")

    sqlite_rows = _sqlite_count()
    print(f"[verify] SQLite analysis_results rows: {sqlite_rows}")

    try:
        counts = _postgres_counts()
    except Exception as error:
        print(f"[verify] ERROR: Postgres connection or schema failure: {error}", file=sys.stderr)
        return 2

    for table, count in counts.items():
        print(f"[verify] Postgres {table} rows: {count}")

    pg_stories = counts.get("stories", 0)
    diff = sqlite_rows - pg_stories
    if diff == 0:
        print("[verify] OK -- SQLite analysis_results and Postgres stories row counts match.")
    elif diff > 0:
        print(
            f"[verify] DIFF -- SQLite has {diff} more analysis_results than Postgres stories. "
            "Expected before USE_POSTGRES_WRITE is enabled or for legacy rows."
        )
    else:
        print(
            f"[verify] DIFF -- Postgres stories has {-diff} more rows than SQLite analysis_results. "
            "Investigate if this is unexpected."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
