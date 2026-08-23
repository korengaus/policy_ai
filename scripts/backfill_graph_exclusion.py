# GRAPH-EXCLUSION BACKFILL (106-APPLY) — one-time guarded backfill: mark the
# archived periodic-bulletin rows (2015-2026 editions of one statistic that
# our own STAT-SEED queries dredged out of go.seoul.co.kr's archive) so the
# brainmap graph builder skips them. The 104-APPLY collection gate stops NEW
# ones; these are the rows collected before it shipped.
#
# ★MARK, NOT DELETE: an archived weekly report (week 2026-08-03) references
# row 14618 by /?result_id= link, and every row is a collected record with
# provenance. Marked rows stay readable at their links; they only stop
# feeding the graph (build_brainmap_graph SELECT_ROWS_SQL:
# WHERE graph_exclusion IS NULL).
#
# ★THE SHIPPED GATE IS THE INSTRUMENT: a row qualifies exactly when
# news_collector._stale_periodic_bulletin_period(title, collection_date)
# fires — the same function, not a reimplementation or an id list.
#
# SAFETY (mirrors scripts/backfill_claim_chrome.py):
#   * ADDITIVE COLUMN: ALTER TABLE ... ADD COLUMN IF NOT EXISTS
#     graph_exclusion TEXT (nullable; normal rows stay NULL).
#   * BACKUP FIRST: full (id, title, created_at, graph_exclusion) export.
#   * DRY RUN FIRST: --dry-run computes + logs every change, no write.
#   * Touches graph_exclusion ONLY:
#       UPDATE analysis_results SET graph_exclusion = %s
#       WHERE id = %s AND title = %s AND graph_exclusion IS NULL
#     No verdict field, score, label, claim, URL or timestamp touched.
#   * Change log JSONL flushed + fsync'd before the first UPDATE.
#   * Fail-closed: DATABASE_URL required; write needs USE_POSTGRES_WRITE=true.

import argparse
import datetime
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

# The shipped collection gate — the single source of truth for staleness.
from news_collector import _stale_periodic_bulletin_period  # noqa: E402

EXCLUSION_REASON = "stale_periodic_bulletin"

BACKUP_PATH = _PROJECT_ROOT / "_graph_exclusion_backfill_backup.jsonl"
LOG_PATH_DRY = _PROJECT_ROOT / "_graph_exclusion_backfill_dryrun_log.jsonl"
LOG_PATH_WRITE = _PROJECT_ROOT / "_graph_exclusion_backfill_log.jsonl"

ADD_COLUMN_SQL = ("ALTER TABLE analysis_results "
                  "ADD COLUMN IF NOT EXISTS graph_exclusion TEXT")
SELECT_SQL = ("SELECT id, title, created_at, graph_exclusion "
              "FROM analysis_results ORDER BY id")
UPDATE_SQL = ("UPDATE analysis_results SET graph_exclusion = %s "
              "WHERE id = %s AND title = %s AND graph_exclusion IS NULL")


def collection_date(created_at):
    text = str(created_at or "")[:10]
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="backfill_graph_exclusion")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL not set — refusing.")
        return 1
    if not args.dry_run and os.environ.get(
            "USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write.")
        return 1

    import psycopg
    url = (os.environ["DATABASE_URL"]
           .replace("postgresql+psycopg://", "postgresql://")
           .replace("postgresql+psycopg2://", "postgresql://"))
    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(f"GRAPH-EXCLUSION BACKFILL — {mode} (reason={EXCLUSION_REASON})")

    with psycopg.connect(url) as conn:
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute(ADD_COLUMN_SQL)
            conn.commit()
            print("[schema] graph_exclusion column ensured (additive, nullable)")

        with conn.cursor() as cur:
            try:
                cur.execute(SELECT_SQL)
            except psycopg.errors.UndefinedColumn:
                conn.rollback()
                cur.execute("SELECT id, title, created_at, NULL "
                            "FROM analysis_results ORDER BY id")
            rows = cur.fetchall()

        if not args.dry_run:
            count = 0
            with open(BACKUP_PATH, "w", encoding="utf-8") as fh:
                for row_id, title, created_at, marker in rows:
                    fh.write(json.dumps(
                        {"id": row_id, "title": title,
                         "created_at": str(created_at or ""),
                         "graph_exclusion": marker}, ensure_ascii=False) + "\n")
                    count += 1
                fh.flush()
                os.fsync(fh.fileno())
            print(f"[backup] {BACKUP_PATH} — {count} rows")

        changes, already = [], 0
        for row_id, title, created_at, marker in rows:
            day = collection_date(created_at)
            if not title or day is None:
                continue
            period = _stale_periodic_bulletin_period(title, day)
            if period is None:
                continue
            if marker:
                already += 1
                continue
            changes.append((row_id, title, str(day), list(period)))

        log_path = LOG_PATH_DRY if args.dry_run else LOG_PATH_WRITE
        with open(log_path, "w", encoding="utf-8") as fh:
            for row_id, title, day, period in changes:
                fh.write(json.dumps(
                    {"id": row_id, "title": title, "collected": day,
                     "bulletin_period": period, "before": None,
                     "after": EXCLUSION_REASON}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        print(f"[scan] examined={len(rows)} would_mark={len(changes)} "
              f"already_marked={already}")
        print(f"[log] {log_path} — {len(changes)} change records, fsync'd")
        for row_id, title, day, period in changes:
            print(f"  id={row_id} collected={day} period={period[0]}-{period[1]:02d} {title[:60]}")

        if args.dry_run:
            print("[dry-run] DONE — no write executed.")
            return 0

        updated = guard_missed = 0
        with conn.cursor() as cur:
            for row_id, title, _day, _period in changes:
                cur.execute(UPDATE_SQL, (EXCLUSION_REASON, row_id, title))
                updated += cur.rowcount
                guard_missed += 1 - cur.rowcount
            conn.commit()
        print(f"[write] DONE: marked={updated} guard_missed={guard_missed} "
              f"already_marked={already}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
