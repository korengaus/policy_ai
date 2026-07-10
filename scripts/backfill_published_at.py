# SPREAD-F1B — operator-run backfill: populate analysis_results.published_at
# for EXISTING rows from the article_published_at value already stored inside
# the debug_summary TEXT blob (95.9% fill measured 7/8; the rest stay NULL).
#
# ★RUN LOCATION: operator's LOCAL machine (or Worker Shell), AFTER the
# SPREAD-F1B code deploy (the deploy's startup ensure-path ADDs the
# published_at column; this script refuses with guidance if it's missing).
# Point DATABASE_URL at the external Postgres and set USE_POSTGRES_WRITE=true.
#
# INDEX (operator, Worker Shell, AFTER the backfill completes — CONCURRENTLY
# cannot run inside a transaction, so it is deliberately NOT in the startup
# ensure-path and NOT run by this script):
#   CREATE INDEX CONCURRENTLY idx_analysis_results_published_at
#       ON analysis_results (published_at);
#
# SAFETY:
#   * Writes ONLY analysis_results.published_at (additive nullable metadata
#     column). NO verdict field is read or written — verdict_label /
#     policy_confidence_score / truth_claim / operator_review_required /
#     has_genuine_official_support untouched. Verdict-isolated.
#   * Idempotent + resumable: selects WHERE published_at IS NULL with keyset
#     pagination (id > last), and every UPDATE re-checks published_at IS NULL.
#     Rerunning after an interruption simply continues; rows whose date can't
#     be parsed stay NULL forever (the raw value stays in debug_summary).
#   * Normalization is THE SAME code path as new-row saves: imports
#     database._normalize_published_at (ISO-8601 pass-through + RFC-1123 via
#     providers.naver_search._pubdate_to_iso, both -> "+00:00" ISO-UTC;
#     unparseable -> None). Backfilled and new rows can never drift.
#   * debug_summary is a TEXT column: parsed with json.loads in Python
#     (mirrors scripts/observe_daily.py) — never SQL ->>.
#   * Fail-closed: refuses without DATABASE_URL; refuses to write without
#     USE_POSTGRES_WRITE=true (--dry-run needs only DATABASE_URL).
#     Never prints DATABASE_URL or any API key.
#   * Batched: commit per batch (default 500); bounded by --limit for tests.
#
# Offline logic check: --selftest (normalization samples only; no DB, no
# network). Cost-free preview: --dry-run (counts what WOULD be filled).

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

from database import _normalize_published_at  # noqa: E402 — single source of truth

SELECT_BATCH_SQL = (
    "SELECT id, debug_summary FROM analysis_results "
    "WHERE published_at IS NULL "
    "AND debug_summary LIKE '%%article_published_at%%' "
    "AND id > %s ORDER BY id LIMIT %s"
)
UPDATE_SQL = (
    "UPDATE analysis_results SET published_at = %s "
    "WHERE id = %s AND published_at IS NULL"
)
COUNT_FILLED_SQL = (
    "SELECT COUNT(*) FROM analysis_results WHERE published_at IS NOT NULL"
)


def extract_published_at(debug_summary_text):
    """json.loads the TEXT debug_summary and normalize its
    article_published_at. Returns (normalized_or_None, reason) where reason
    is one of ok / bad_json / no_key / unparseable_date."""
    try:
        parsed = json.loads(debug_summary_text or "")
    except (TypeError, ValueError):
        return None, "bad_json"
    if not isinstance(parsed, dict) or not parsed.get("article_published_at"):
        return None, "no_key"
    normalized = _normalize_published_at(parsed.get("article_published_at"))
    if normalized is None:
        return None, "unparseable_date"
    return normalized, "ok"


def run_backfill(conn, dry_run, limit, batch_size):
    last_id = 0
    scanned = updated = 0
    skipped = {"bad_json": 0, "no_key": 0, "unparseable_date": 0}
    while True:
        take = batch_size if limit <= 0 else min(batch_size, limit - scanned)
        if take <= 0:
            break
        with conn.cursor() as cur:
            cur.execute(SELECT_BATCH_SQL, (last_id, take))
            rows = cur.fetchall()
        if not rows:
            break
        batch_updates = []
        for row_id, debug_summary_text in rows:
            last_id = row_id
            scanned += 1
            normalized, reason = extract_published_at(debug_summary_text)
            if normalized is None:
                skipped[reason] += 1
                continue
            batch_updates.append((normalized, row_id))
        if batch_updates and not dry_run:
            with conn.cursor() as cur:
                cur.executemany(UPDATE_SQL, batch_updates)
            conn.commit()
        updated += len(batch_updates)
        print("[backfill] scanned=%d %s=%d skipped=%s (last id=%d)"
              % (scanned, "would_fill" if dry_run else "updated",
                 updated, skipped, last_id))
    return scanned, updated, skipped


def run_selftest() -> int:
    print("=== BACKFILL-PUBLISHED-AT --selftest (offline; no DB, no network) ===")
    samples = [
        # (input debug_summary text, expected normalized value)
        ('{"article_published_at": "2026-07-10T06:50:00+09:00"}',
         "2026-07-09T21:50:00+00:00"),                       # ISO, KST -> UTC
        ('{"article_published_at": "Wed, 10 Jul 2026 06:50:00 +0900"}',
         "2026-07-09T21:50:00+00:00"),                       # RFC-1123 -> UTC
        ('{"article_published_at": "garbage-not-a-date"}', None),
        ('{"other_key": 1}', None),                          # key absent
        ("not json at all", None),                           # bad TEXT blob
    ]
    ok = True
    for text, expected in samples:
        got, reason = extract_published_at(text)
        good = got == expected
        ok = ok and good
        print("  [%s] %-55s -> %s (%s)"
              % ("ok" if good else "xx", text[:55], got, reason))
    print()
    print("SELFTEST: %s" % ("PASS (ISO + RFC-1123 -> +00:00 ISO-UTC; garbage/"
                            "missing/bad-json -> NULL)" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_published_at",
        description="Backfill analysis_results.published_at from the "
                    "article_published_at stored inside debug_summary "
                    "(batched, idempotent, resumable; unparseable stays NULL).",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE normalization check (no DB, no network).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count what WOULD be filled; NO UPDATE, NO commit.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows to scan (0 = all; for testing).")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Rows per SELECT/UPDATE batch (default 500).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0
    if not args.dry_run and os.environ.get("USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it "
              "true, or use --dry-run.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("BACKFILL-PUBLISHED-AT — %s (batch=%d, limit=%s)"
          % ("DRY-RUN" if args.dry_run else "WRITE",
             args.batch_size, args.limit or "all"))
    with psycopg.connect(url) as conn:
        try:
            scanned, updated, skipped = run_backfill(
                conn, args.dry_run, args.limit, args.batch_size)
        except psycopg.errors.UndefinedColumn:
            print("[backfill] published_at column missing — deploy the "
                  "SPREAD-F1B code first (startup ensure-path adds it).")
            return 1
        with conn.cursor() as cur:
            cur.execute(COUNT_FILLED_SQL)
            filled_total = cur.fetchone()[0]
    print()
    print("[backfill] DONE %s: scanned=%d %s=%d skipped=%s; "
          "published_at NOT NULL total=%d"
          % ("(dry-run)" if args.dry_run else "", scanned,
             "would_fill" if args.dry_run else "updated",
             updated, skipped, filled_total))
    if not args.dry_run:
        print("[backfill] Next (operator, Worker Shell): CREATE INDEX "
              "CONCURRENTLY idx_analysis_results_published_at ON "
              "analysis_results (published_at);")
    return 0


if __name__ == "__main__":
    sys.exit(main())
