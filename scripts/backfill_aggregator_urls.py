# AGGREGATOR-URL BACKFILL (106-APPLY) — one-time guarded backfill: rows whose
# original_url is an undecoded news.google.com redirect (stored only when the
# collector's gnewsdecoder failed at collection; 34 of the 36 rows date to a
# single 2026-07-22 outage) get the redirect RESOLVED to the publisher URL it
# points at, via the SAME gnewsdecoder the collector uses. The aggregator is
# a directory, not an outlet; leaving its host in original_url let the graph
# builder count it as a second 매체 in 10 live clusters.
#
# SAFETY (mirrors scripts/backfill_claim_chrome.py):
#   * BACKUP FIRST: full (id, original_url) export before any write.
#   * DRY RUN FIRST: --dry-run decodes and logs every change, no UPDATE.
#   * Touches original_url ONLY:
#       UPDATE analysis_results SET original_url = %s
#       WHERE id = %s AND original_url = %s
#     (optimistic guard). No verdict field, score, label, status, claim or
#     timestamp is read or written.
#   * A URL the decoder cannot resolve is SKIPPED and counted — never guessed.
#   * Change log JSONL is flushed + fsync'd before the first UPDATE.
#   * Fail-closed: refuses without DATABASE_URL; write mode additionally
#     refuses without USE_POSTGRES_WRITE=true.

import argparse
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

from googlenewsdecoder import gnewsdecoder  # noqa: E402 — the collector's own decoder

BACKUP_PATH = _PROJECT_ROOT / "_aggregator_url_backfill_backup.jsonl"
LOG_PATH_DRY = _PROJECT_ROOT / "_aggregator_url_backfill_dryrun_log.jsonl"
LOG_PATH_WRITE = _PROJECT_ROOT / "_aggregator_url_backfill_log.jsonl"

SELECT_SQL = ("SELECT id, original_url FROM analysis_results "
              "WHERE original_url LIKE 'https://news.google.com%' ORDER BY id")
UPDATE_SQL = ("UPDATE analysis_results SET original_url = %s "
              "WHERE id = %s AND original_url = %s")


def export_backup(conn):
    count = 0
    with open(BACKUP_PATH, "w", encoding="utf-8") as fh:
        with conn.cursor(name="agg_backup") as cur:
            cur.itersize = 2000
            cur.execute("SELECT id, original_url FROM analysis_results ORDER BY id")
            for row_id, original_url in cur:
                fh.write(json.dumps({"id": row_id, "original_url": original_url},
                                    ensure_ascii=False) + "\n")
                count += 1
        fh.flush()
        os.fsync(fh.fileno())
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="backfill_aggregator_urls")
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
    print(f"AGGREGATOR-URL BACKFILL — {mode}")

    with psycopg.connect(url) as conn:
        if not args.dry_run:
            print(f"[backup] {BACKUP_PATH} — {export_backup(conn)} rows")

        with conn.cursor() as cur:
            cur.execute(SELECT_SQL)
            rows = cur.fetchall()
        print(f"[scan] {len(rows)} rows carry an undecoded news.google.com URL")

        changes, failed = [], []
        for row_id, google_url in rows:
            try:
                result = gnewsdecoder(google_url, interval=1)
                decoded = result.get("decoded_url") if result.get("status") else None
            except Exception:
                decoded = None
            if decoded and not decoded.startswith("https://news.google.com"):
                changes.append((row_id, google_url, decoded))
                print(f"  id={row_id} -> {decoded[:90]}")
            else:
                failed.append(row_id)
                print(f"  id={row_id} DECODE FAILED — skipped")

        log_path = LOG_PATH_DRY if args.dry_run else LOG_PATH_WRITE
        with open(log_path, "w", encoding="utf-8") as fh:
            for row_id, before, after in changes:
                fh.write(json.dumps({"id": row_id, "before": before,
                                     "after": after}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        print(f"[log] {log_path} — {len(changes)} change records, fsync'd; "
              f"skipped_undecodable={len(failed)} {failed or ''}")

        if args.dry_run:
            print("[dry-run] DONE — no UPDATE executed.")
            return 0

        updated = guard_missed = 0
        with conn.cursor() as cur:
            for row_id, before, after in changes:
                cur.execute(UPDATE_SQL, (after, row_id, before))
                updated += cur.rowcount
                guard_missed += 1 - cur.rowcount
            conn.commit()
        print(f"[write] DONE: updated={updated} guard_missed={guard_missed} "
              f"skipped_undecodable={len(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
