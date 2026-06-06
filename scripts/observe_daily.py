"""READ-ONLY daily observation summary for analysis_results. No writes.
Reuses postgres_storage.get_engine().

This is an operator reporting tool. It replaces the hand-pasted psql /
python one-liners used during the OBSERVE period with a single command
that prints, in order:

    1. HEADER              — current local + UTC timestamp
    2. TOTALS              — row count, MAX(id), latest created_at
    3. DAILY GROWTH        — rows per date (last 10 dates, oldest->newest)
    4. VERDICT DISTRIBUTION
    5. ALERT-LEVEL DISTRIBUTION
    6. CONFIDENCE SUMMARY  — both policy_confidence_score and verdict_confidence
    7. HUMAN-REVIEWED      — count of human_reviewed_at IS NOT NULL
    8. OFF-TOPIC FLAGS     — advisory keyword suspects in the last N rows

Every database statement is a SELECT. The script issues NO INSERT /
UPDATE / DELETE / ALTER and never touches verdict logic, the pipeline,
the scheduler, the frontend, pins, or any test. A flagged off-topic row
is ADVISORY ONLY — it is printed for the operator's eyes and is not
necessarily wrong; the operator judges.

Usage:
    python scripts/observe_daily.py
    python scripts/observe_daily.py --limit-offtopic 50

Requires (real run happens in the Render Worker Shell):
    USE_POSTGRES_WRITE=true
    DATABASE_URL=postgresql+psycopg://...

Exit codes:
    0 — summary printed, OR engine unavailable (clean message, no crash)
    2 — CLI usage error (argparse)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


# Make the project root importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Off-topic advisory keyword buckets.
#
# Each bucket maps a label -> list of Korean substrings. Matching is plain
# case-insensitive substring containment against (title + claim_text +
# query). These flags are ADVISORY ONLY: they do not modify, delete, rank,
# or otherwise influence any row. A match simply prints the row as a
# suspect for the operator to eyeball. A genuinely on-topic real-estate
# policy article that happens to mention an election is NOT wrong — the
# operator decides.
# ---------------------------------------------------------------------------
_OFFTOPIC_BUCKETS = {
    "OBITUARY": ["별세", "부고", "빈소", "발인", "영결식", "장례식장", "故"],
    "ELECTION": ["선거", "시장 선거", "당선", "득표", "지방선거", "여당", "야당"],
    "FOREIGN_MARKET": [
        "일본은행", "연준", "미국 금리", "블룸버그", "로이터",
        "엔저", "중동", "이란",
    ],
    "SECURITIES": ["증권", "채권운용", "투자증권", "연구원"],
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observe_daily",
        description=(
            "READ-ONLY daily observation summary for analysis_results. "
            "Runs SELECTs only via postgres_storage.get_engine(); issues "
            "no INSERT/UPDATE/DELETE/ALTER and changes no existing code."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--limit-offtopic",
        type=int,
        default=30,
        metavar="N",
        help=(
            "How many of the most recent rows (ORDER BY id DESC) to scan "
            "for advisory off-topic keyword matches. Default: 30."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Small formatting helpers.
# ---------------------------------------------------------------------------


def _section(title: str) -> str:
    return f"\n=== {title} ==="


def _label_or_none(value) -> str:
    """Render a verdict/alert label, mapping NULL / empty / whitespace to
    the explicit sentinel '(none)' so the operator can see how many rows
    have no label at all."""
    if value is None:
        return "(none)"
    text = str(value).strip()
    return text if text else "(none)"


def _fmt_num(value) -> str:
    """Render a numeric aggregate, tolerating None (no rows)."""
    if value is None:
        return "n/a"
    return str(value)


# ---------------------------------------------------------------------------
# Report sections. Each takes an open connection + the SQLAlchemy module and
# returns a list of printable lines. SELECT-only throughout.
# ---------------------------------------------------------------------------


def _report_totals(conn, sa) -> list:
    lines = [_section("TOTALS")]
    row = conn.execute(
        sa.text(
            "SELECT COUNT(*) AS n, MAX(id) AS max_id, "
            "MAX(created_at) AS latest_created_at "
            "FROM analysis_results"
        )
    ).first()
    total = row._mapping["n"] if row is not None else 0
    max_id = row._mapping["max_id"] if row is not None else None
    latest = row._mapping["latest_created_at"] if row is not None else None
    lines.append(f"total rows:         {_fmt_num(total)}")
    lines.append(f"MAX(id):            {_fmt_num(max_id)}")
    lines.append(f"latest created_at:  {latest if latest else 'n/a'}")
    return lines


def _report_daily_growth(conn, sa) -> list:
    lines = [_section("DAILY GROWTH (last 10 dates, oldest -> newest)")]
    # substr(created_at,1,10) -> 'YYYY-MM-DD'. NULL / short / odd values
    # bucket as '(unknown)' so a malformed timestamp can never crash the
    # report. Grab the newest 10 buckets, then reverse for oldest->newest.
    rows = conn.execute(
        sa.text(
            "SELECT COALESCE(NULLIF(substr(created_at, 1, 10), ''), "
            "'(unknown)') AS day, COUNT(*) AS n "
            "FROM analysis_results "
            "GROUP BY day "
            "ORDER BY day DESC "
            "LIMIT 10"
        )
    ).all()
    if not rows:
        lines.append("(no rows)")
        return lines
    for r in reversed(rows):
        day = r._mapping["day"]
        n = r._mapping["n"]
        lines.append(f"{day} : {n} rows")
    return lines


def _report_distribution(conn, sa, column: str, title: str) -> list:
    lines = [_section(title)]
    # column is a hardcoded identifier from the caller (never user input),
    # so interpolating it into the GROUP BY carries no injection surface.
    rows = conn.execute(
        sa.text(
            f"SELECT {column} AS label, COUNT(*) AS n "
            f"FROM analysis_results "
            f"GROUP BY {column} "
            f"ORDER BY n DESC"
        )
    ).all()
    if not rows:
        lines.append("(no rows)")
        return lines
    for r in rows:
        label = _label_or_none(r._mapping["label"])
        n = r._mapping["n"]
        lines.append(f"{label:<24} {n}")
    return lines


def _report_confidence(conn, sa, column: str) -> list:
    lines = [_section(f"CONFIDENCE SUMMARY — {column}")]
    # Aggregate in SQL rather than loading rows. NULLs are ignored by the
    # SQL aggregates; the >=70 / <=10 counts are over non-NULL values only.
    row = conn.execute(
        sa.text(
            f"SELECT "
            f"MIN({column}) AS mn, "
            f"MAX({column}) AS mx, "
            f"AVG({column}) AS av, "
            f"COUNT({column}) AS non_null, "
            f"SUM(CASE WHEN {column} >= 70 THEN 1 ELSE 0 END) AS ge70, "
            f"SUM(CASE WHEN {column} <= 10 THEN 1 ELSE 0 END) AS le10 "
            f"FROM analysis_results"
        )
    ).first()
    m = row._mapping if row is not None else {}
    avg = m.get("av") if row is not None else None
    avg_str = f"{round(float(avg))}" if avg is not None else "n/a"
    lines.append(f"min:                {_fmt_num(m.get('mn'))}")
    lines.append(f"max:                {_fmt_num(m.get('mx'))}")
    lines.append(f"avg (rounded):      {avg_str}")
    lines.append(f"non-null count:     {_fmt_num(m.get('non_null'))}")
    lines.append(f"count >= 70 (cap):  {_fmt_num(m.get('ge70'))}")
    lines.append(f"count <= 10 (floor):{_fmt_num(m.get('le10'))}")
    return lines


def _report_human_reviewed(conn, sa) -> list:
    lines = [_section("HUMAN-REVIEWED")]
    row = conn.execute(
        sa.text(
            "SELECT COUNT(*) AS n FROM analysis_results "
            "WHERE human_reviewed_at IS NOT NULL"
        )
    ).first()
    n = row._mapping["n"] if row is not None else 0
    lines.append(f"rows with human_reviewed_at set: {_fmt_num(n)} (expected sparse)")
    return lines


def _classify_offtopic(text: str) -> list:
    """Return the list of bucket labels whose keywords appear (case-
    insensitive substring) in ``text``. Advisory only."""
    haystack = text.lower()
    hits = []
    for bucket, keywords in _OFFTOPIC_BUCKETS.items():
        for kw in keywords:
            if kw.lower() in haystack:
                hits.append(bucket)
                break
    return hits


def _report_offtopic(conn, sa, limit: int) -> list:
    lines = [_section(f"OFF-TOPIC FLAGS (advisory; last {limit} rows by id)")]
    rows = conn.execute(
        sa.text(
            "SELECT id, query, title, claim_text "
            "FROM analysis_results "
            "ORDER BY id DESC "
            "LIMIT :lim"
        ).bindparams(lim=limit)
    ).all()
    matched_lines = []
    for r in rows:
        m = r._mapping
        query = m["query"] or ""
        title = m["title"] or ""
        claim_text = m["claim_text"] or ""
        combined = f"{title}\n{claim_text}\n{query}"
        buckets = _classify_offtopic(combined)
        if not buckets:
            continue
        bucket_str = ",".join(buckets)
        title_50 = str(title)[:50]
        matched_lines.append(
            f"{m['id']} | {bucket_str} | {query} | {title_50}"
        )
    if not matched_lines:
        lines.append("OFF-TOPIC FLAGS: none in last 30 rows."
                     if limit == 30 else
                     f"OFF-TOPIC FLAGS: none in last {limit} rows.")
        return lines
    lines.append("(advisory only — a flagged row is not necessarily wrong)")
    lines.extend(matched_lines)
    return lines


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    limit = max(1, int(args.limit_offtopic or 30))

    # HEADER — printed before any DB work so the operator always sees when
    # the snapshot was taken, even if the engine turns out to be unavailable.
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()
    print("=== OBS-1 DAILY OBSERVATION (READ-ONLY) ===")
    print(f"local: {now_local.isoformat(timespec='seconds')}")
    print(f"UTC:   {now_utc.isoformat(timespec='seconds')}")

    # Import postgres_storage AFTER argparse so --help does not require the
    # dependency to be installed in the operator's local env. Mirrors
    # scripts/check_postgres_health.py.
    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        print(
            "\nEngine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL."
        )
        return 0

    # All reads in a single read-only connection. engine.connect() (not
    # begin()) — no transaction is committed; we only ever SELECT.
    with engine.connect() as conn:
        for line in _report_totals(conn, sa):
            print(line)
        for line in _report_daily_growth(conn, sa):
            print(line)
        for line in _report_distribution(
            conn, sa, "verdict_label", "VERDICT DISTRIBUTION"
        ):
            print(line)
        for line in _report_distribution(
            conn, sa, "policy_alert_level", "ALERT-LEVEL DISTRIBUTION"
        ):
            print(line)
        # Two confidence columns, reported separately so the operator can
        # see which one matches the on-screen "신뢰도".
        for line in _report_confidence(conn, sa, "policy_confidence_score"):
            print(line)
        for line in _report_confidence(conn, sa, "verdict_confidence"):
            print(line)
        for line in _report_human_reviewed(conn, sa):
            print(line)
        for line in _report_offtopic(conn, sa, limit):
            print(line)

    print("\n[Safety] READ-ONLY summary — no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
