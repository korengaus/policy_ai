# VERIFY-ITEMS A4 Phase 1 — READ-ONLY window probe (pin-OUT, SELECT-only).
#
# QUESTION: the sidebar "이번 주 검증 현황" 총검증 shows ~6568, but
# read_weekly_verification_stats' own docstring assumes "~100s" rows/week.
# Is created_at >= now-7d genuinely ~a week of verifications, or has a recent
# bulk backfill/re-ingest RE-STAMPED created_at so most of the corpus falls
# inside the 7-day window (a mislabel like the A1 header fix)?
#
# Joe runs once in the Render Worker Shell:
#     PYTHONPATH=. python scripts/weekly_stats_window_probe.py
#
# Prints: total corpus, rows in the 7-day window (what /stats returns),
# window/corpus %, and the created_at daily histogram for the last ~14 days
# (a single-day spike = a backfill re-stamp, not genuine weekly volume).
#
# SAFETY: SELECT-only (created_at TEXT). No verdict column, no writes, no
# schema change. Never prints DATABASE_URL. pin-OUT scripts/*.

import collections
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(days=7)).isoformat()  # mirrors api_server.stats()

    print("WEEKLY-STATS WINDOW PROBE — SELECT-only\n")
    print("now (UTC)      : %s" % now.isoformat())
    print("7-day cutoff   : %s\n" % cutoff_iso)

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM analysis_results")
            corpus = int(cur.fetchone()[0])
            # exactly what /stats counts: created_at >= cutoff (lexicographic on ISO text)
            cur.execute(
                "SELECT COUNT(*) FROM analysis_results WHERE created_at >= %s",
                (cutoff_iso,),
            )
            in_window = int(cur.fetchone()[0])
            # daily histogram (last ~14 days) to spot a backfill spike
            cur.execute(
                "SELECT LEFT(created_at, 10) AS day, COUNT(*) "
                "FROM analysis_results "
                "WHERE created_at >= %s "
                "GROUP BY day ORDER BY day",
                ((now - timedelta(days=14)).isoformat(),),
            )
            hist = cur.fetchall()

    pct = (100.0 * in_window / corpus) if corpus else 0.0
    print("== window vs corpus ==")
    print("  total corpus            : %6d" % corpus)
    print("  in 7-day window (/stats): %6d  (%.1f%% of corpus)" % (in_window, pct))
    print("  → if this %% is near 100, '이번 주' is a mislabel: created_at was")
    print("    re-stamped by a backfill, not genuine this-week verifications.\n")

    print("== created_at daily histogram (last ~14 days) ==")
    peak = max((n for _, n in hist), default=0)
    for day, n in hist:
        bar = "#" * int(40 * n / peak) if peak else ""
        print("  %s  %6d  %s" % (day, n, bar))
    print("\n  A single-day spike ~= corpus size => a bulk re-ingest/backfill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
