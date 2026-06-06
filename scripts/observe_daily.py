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
    9. COLLECTOR MIX       — data-backed: which collector won, parsed from
                             debug_summary.collection_source (overall +
                             per-date last 7 days)
   10. DOMAIN MIX          — ADVISORY keyword estimate over query/title/
                             claim_text (no domain field is stored)

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
import json
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


# ---------------------------------------------------------------------------
# Domain advisory keyword buckets (SECTION 10).
#
# IMPORTANT: these are an ADVISORY ESTIMATE, NOT a measurement. OBS-2 Phase 1
# confirmed that analysis_results stores NO finance/legal/policy domain field
# — `topic` is only a 9-value housing-finance sub-taxonomy (…, 미분류). So the
# only way to gauge domain mix is a keyword scan of the already-safe text
# columns (query + title + claim_text), exactly like _OFFTOPIC_BUCKETS above.
# A row may match multiple buckets (or none); nothing here ranks, modifies, or
# influences any row. Treat the counts as a rough hint for the operator.
# ---------------------------------------------------------------------------
_DOMAIN_BUCKETS = {
    "FINANCE_금융": [
        "금리", "대출", "가계부채", "DSR", "증권", "채권", "은행",
        "금융위", "금감원", "투자", "예금", "보험",
    ],
    "LEGAL_법률": [
        "법령", "법안", "시행령", "시행규칙", "판결", "판례",
        "소송", "고시", "개정안", "위헌", "헌법", "법률",
    ],
    "REALESTATE_부동산": [
        "부동산", "전세", "주택", "주담대", "분양", "청약", "임대",
        "양도세", "종부세", "LTV",
    ],
    "WELFARE_복지": [
        "복지", "지원금", "보조금", "수당", "연금", "바우처", "취약계층",
    ],
    "SMB_소상공인": [
        "소상공인", "자영업", "중소기업", "새출발기금", "상생",
    ],
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


# ---------------------------------------------------------------------------
# SECTION 9 — COLLECTOR MIX (data-backed, from debug_summary.collection_source).
#
# OBS-2 Phase 1 confirmed: the collector that won (naver_api / google_rss /
# naver_fallback / daum_fallback / forced_search_fallback / none) is stored
# per row inside the debug_summary TEXT column as JSON under the key
# "collection_source". debug_summary is loose-typed TEXT (NOT jsonb) and may
# contain malformed legacy values, so it MUST be parsed Python-side with
# json.loads inside try/except — never a ::jsonb cast in SQL. Any parse
# failure / missing key / empty value buckets as "(unknown)".
# ---------------------------------------------------------------------------


_COLLECTOR_UNKNOWN = "(unknown)"


def _collector_of(debug_summary_text) -> str:
    """Extract collection_source from a debug_summary TEXT cell. Returns
    '(unknown)' on NULL / non-string / parse failure / missing-or-empty key.
    Pure Python — defends against malformed legacy rows that a ::jsonb cast
    would choke on."""
    if not debug_summary_text or not isinstance(debug_summary_text, str):
        return _COLLECTOR_UNKNOWN
    try:
        parsed = json.loads(debug_summary_text)
    except Exception:  # noqa: BLE001 — malformed legacy JSON must not crash
        return _COLLECTOR_UNKNOWN
    if not isinstance(parsed, dict):
        return _COLLECTOR_UNKNOWN
    value = parsed.get("collection_source")
    if value is None:
        return _COLLECTOR_UNKNOWN
    text = str(value).strip()
    return text if text else _COLLECTOR_UNKNOWN


def _fetch_collector_rows(conn, sa) -> list:
    """SELECT ONLY the columns SECTION 9 needs (id, created_at,
    debug_summary) — not every column. Returns a list of (day, collector)
    tuples with JSON parsed + bucketed in Python. day = substr(created_at,
    1,10) or '(unknown)'."""
    rows = conn.execute(
        sa.text(
            "SELECT id, created_at, debug_summary "
            "FROM analysis_results"
        )
    ).all()
    parsed = []
    for r in rows:
        m = r._mapping
        created_at = m["created_at"]
        if created_at and isinstance(created_at, str) and len(created_at) >= 10:
            day = created_at[:10]
        else:
            day = "(unknown)"
        parsed.append((day, _collector_of(m["debug_summary"])))
    return parsed


def _report_collector_mix(conn, sa) -> list:
    lines = [_section("COLLECTOR MIX (data-backed; debug_summary.collection_source)")]
    lines.append(
        "Note: Naver became primary collector ~2026-06-02; rows before that "
        "are google_rss by default."
    )
    parsed = _fetch_collector_rows(conn, sa)
    if not parsed:
        lines.append("(no rows)")
        return lines

    # (a) OVERALL distribution — count per collector, desc.
    overall = {}
    for _day, collector in parsed:
        overall[collector] = overall.get(collector, 0) + 1
    lines.append("")
    lines.append("(a) OVERALL collector distribution:")
    for collector, n in sorted(
        overall.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append(f"{collector:<24} {n}")

    # (b) PER-DATE distribution for the last 7 dates, oldest -> newest.
    by_day = {}
    for day, collector in parsed:
        bucket = by_day.setdefault(day, {})
        bucket[collector] = bucket.get(collector, 0) + 1
    # Newest 7 real dates first, then reverse to oldest->newest. '(unknown)'
    # sorts to the bottom of a desc string sort, so it only appears if it is
    # genuinely among the most recent 7 distinct day-keys.
    last_7_days = sorted(by_day.keys(), reverse=True)[:7]
    lines.append("")
    lines.append("(b) PER-DATE collector distribution (last 7 dates, oldest -> newest):")
    for day in reversed(last_7_days):
        counts = by_day[day]
        parts = ", ".join(
            f"{collector}={n}"
            for collector, n in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )
        lines.append(f"{day} : {parts}")
    return lines


# ---------------------------------------------------------------------------
# SECTION 10 — DOMAIN MIX (ADVISORY keyword heuristic ONLY).
#
# No domain field is stored (OBS-2 Phase 1). This is a keyword estimate over
# query + title + claim_text for the most recent N rows, bucketed via
# _DOMAIN_BUCKETS. A row may match multiple buckets, so the per-bucket counts
# sum to >= the scanned row count. NOT authoritative — advisory only.
# ---------------------------------------------------------------------------


def _classify_domain(text: str) -> list:
    """Return the list of domain-bucket labels whose keywords appear (case-
    insensitive substring) in ``text``. Advisory only; a row may match
    several buckets."""
    haystack = text.lower()
    hits = []
    for bucket, keywords in _DOMAIN_BUCKETS.items():
        for kw in keywords:
            if kw.lower() in haystack:
                hits.append(bucket)
                break
    return hits


def _report_domain_mix(conn, sa, limit: int) -> list:
    lines = [_section(f"DOMAIN MIX (ADVISORY keyword estimate; last {limit} rows by id)")]
    lines.append(
        "DOMAIN MIX is an advisory keyword estimate. No domain field is "
        "stored; topic is only a 9-value housing-finance sub-taxonomy. "
        "Treat as a rough hint, not a measurement."
    )
    rows = conn.execute(
        sa.text(
            "SELECT id, query, title, claim_text "
            "FROM analysis_results "
            "ORDER BY id DESC "
            "LIMIT :lim"
        ).bindparams(lim=limit)
    ).all()
    scanned = 0
    counts = {bucket: 0 for bucket in _DOMAIN_BUCKETS}
    other = 0
    for r in rows:
        scanned += 1
        m = r._mapping
        query = m["query"] or ""
        title = m["title"] or ""
        claim_text = m["claim_text"] or ""
        combined = f"{title}\n{claim_text}\n{query}"
        buckets = _classify_domain(combined)
        if not buckets:
            other += 1
            continue
        for bucket in buckets:
            counts[bucket] += 1
    lines.append("")
    lines.append("(rows may match multiple buckets; advisory keyword estimate, "
                 "not a stored domain)")
    for bucket, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"{bucket:<22} {n}")
    lines.append(f"{'(기타/미분류)':<22} {other}")
    lines.append(f"scanned {scanned} rows")
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
        # SECTION 9 — collector mix (data-backed, JSON-parsed Python-side).
        for line in _report_collector_mix(conn, sa):
            print(line)
        # SECTION 10 — domain mix (advisory keyword estimate; reuses the
        # --limit-offtopic window as the scan size).
        for line in _report_domain_mix(conn, sa, limit):
            print(line)

    print("\n[Safety] READ-ONLY summary — no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
