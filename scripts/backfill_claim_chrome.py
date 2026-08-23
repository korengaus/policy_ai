# CHROME-BACKFILL (91-APPLY) — one-time guarded backfill: strip article
# chrome (bylines / datelines / edit-input stamps / wire footers / photo
# captions) from analysis_results.claim_text for rows stored BEFORE the
# 71-APPLY extractor detector shipped. Rows collected since are clean.
#
# ★The patterns are IMPORTED from claim_extractor (_CHROME_PATTERNS /
# _strip_article_chrome / _normalize_text) — the shipped detector itself,
# never a reimplementation. A per-row assert pins the collector loop below
# to the shipped function's output, so they cannot drift.
#
# SAFETY (mirrors scripts/backfill_published_at.py):
#   * BACKUP FIRST: before any write, the ENTIRE (id, claim_text) column is
#     exported to a JSONL backup file. Write mode refuses to run if the
#     backup cannot be written.
#   * DRY RUN FIRST: --dry-run computes every change and writes the full
#     change log (id / before / after / removed fragments) WITHOUT any
#     UPDATE and without requiring USE_POSTGRES_WRITE.
#   * Touches claim_text ONLY. The UPDATE is
#       UPDATE analysis_results SET claim_text = %s
#       WHERE id = %s AND claim_text = %s
#     — SET names one column (claim_text); WHERE reads id + claim_text as an
#     optimistic guard so a row edited since the SELECT is left alone (and
#     counted). No verdict field, score, label, status or timestamp is read
#     or written.
#   * A row whose claim_text would strip to EMPTY is skipped and counted —
#     the empty-drop belongs to extraction time, not to stored history.
#     The same guard covers results below the extractor's own 18-char
#     fragment floor (claim_extractor._collect_sentences): every such row in
#     the dry run (66) was punctuation/date debris from truncated wire
#     footers (", >", "2026.7.15 , >") or residual footer chrome the shipped
#     patterns don't cover ("재판매 및 DB 금지] , >") — storing a fragment
#     manufactures garbage, so those rows are left untouched instead.
#   * Change log is JSONL, flushed + fsync'd BEFORE the first UPDATE runs,
#     so every before-value survives even a mid-run crash.
#   * Fail-closed: refuses without DATABASE_URL; write mode additionally
#     refuses without USE_POSTGRES_WRITE=true. Never prints DATABASE_URL.

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

# The shipped detector — the single source of truth for what chrome is.
from claim_extractor import (  # noqa: E402
    _CHROME_PATTERNS,
    _normalize_text,
    _strip_article_chrome,
)

BACKUP_PATH = _PROJECT_ROOT / "_chrome_backfill_backup.jsonl"
LOG_PATH_DRY = _PROJECT_ROOT / "_chrome_backfill_dryrun_log.jsonl"
LOG_PATH_WRITE = _PROJECT_ROOT / "_chrome_backfill_log.jsonl"

SELECT_BATCH_SQL = (
    "SELECT id, claim_text FROM analysis_results "
    "WHERE id > %s ORDER BY id LIMIT %s"
)
# claim_text is the ONLY column SET; id + claim_text in WHERE are reads
# (optimistic guard against concurrent edits since the SELECT).
UPDATE_SQL = (
    "UPDATE analysis_results SET claim_text = %s "
    "WHERE id = %s AND claim_text = %s"
)


def strip_and_collect(text):
    """EXACTLY _strip_article_chrome's fixpoint loop, additionally collecting
    each removed fragment. The caller asserts the result equals the shipped
    function's output for every row, so this loop can never silently drift."""
    removed = []
    previous = None
    while previous != text:
        previous = text
        for pattern in _CHROME_PATTERNS:
            for match in pattern.finditer(text):
                fragment = match.group(0).strip()
                if fragment:
                    removed.append(fragment)
            text = pattern.sub(" ", text)
        text = _normalize_text(text)
    return text, removed


# The extractor's own fragment floor (claim_extractor._collect_sentences:
# "<18 chars is still a fragment, not a claim").
FRAGMENT_FLOOR = 18


def compute_change(claim_text):
    """Returns (kind, after, removed). kind is one of:
    clean       — no chrome pattern matches; row untouched
    unchanged   — stripping yields the identical stored bytes; untouched
    empty       — stripping removes everything; row deliberately untouched
    fragment    — result falls under the 18-char extractor floor; untouched
    change      — chrome removed, real text remains; row is a candidate"""
    original = claim_text or ""
    if not original:
        return "clean", original, []
    stripped, removed = strip_and_collect(original)
    assert stripped == _strip_article_chrome(original), (
        "collector drifted from shipped _strip_article_chrome")
    if not removed:
        return "clean", original, []
    if stripped == original:
        return "unchanged", original, []
    if not stripped:
        return "empty", original, removed
    if len(stripped) < FRAGMENT_FLOOR:
        return "fragment", original, removed
    return "change", stripped, removed


def export_backup(conn):
    """Full (id, claim_text) export BEFORE any write. Returns row count."""
    count = 0
    with open(BACKUP_PATH, "w", encoding="utf-8") as fh:
        with conn.cursor(name="chrome_backup") as cur:
            cur.itersize = 2000
            cur.execute("SELECT id, claim_text FROM analysis_results ORDER BY id")
            for row_id, claim_text in cur:
                fh.write(json.dumps(
                    {"id": row_id, "claim_text": claim_text},
                    ensure_ascii=False) + "\n")
                count += 1
        fh.flush()
        os.fsync(fh.fileno())
    return count


def scan(conn, batch_size):
    """Scan every row; return (examined, changes, counts) where changes is
    [(id, before, after, removed)] and counts tallies the skip kinds."""
    examined = 0
    changes = []
    counts = {"clean": 0, "unchanged": 0, "empty": 0, "fragment": 0}
    last_id = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(SELECT_BATCH_SQL, (last_id, batch_size))
            rows = cur.fetchall()
        if not rows:
            break
        for row_id, claim_text in rows:
            last_id = row_id
            examined += 1
            kind, after, removed = compute_change(claim_text)
            if kind == "change":
                changes.append((row_id, claim_text, after, removed))
            else:
                counts[kind] += 1
    return examined, changes, counts


def write_log(path, changes):
    with open(path, "w", encoding="utf-8") as fh:
        for row_id, before, after, removed in changes:
            fh.write(json.dumps(
                {"id": row_id, "before": before, "after": after,
                 "removed": removed}, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_claim_chrome",
        description="One-time guarded backfill: strip article chrome from "
                    "stored claim_text using the SHIPPED claim_extractor "
                    "patterns. Backup + dry-run + change log; claim_text "
                    "is the only column written.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + log every change; NO UPDATE.")
    parser.add_argument("--sample", type=int, default=30,
                        help="Removed-fragment samples to print (default 30).")
    parser.add_argument("--batch-size", type=int, default=1000)
    # 102-APPLY: a later re-run (new chrome pattern) must NOT overwrite the
    # 91-APPLY backup/log — those are the rollback record for 875 rows. A
    # tag gives this run its own files: _chrome_backfill_<tag>_backup.jsonl.
    parser.add_argument("--tag", default="",
                        help="Suffix for this run's backup/log file names.")
    args = parser.parse_args(argv)
    global BACKUP_PATH, LOG_PATH_DRY, LOG_PATH_WRITE
    if args.tag:
        tag = args.tag.strip("_")
        BACKUP_PATH = _PROJECT_ROOT / f"_chrome_backfill_{tag}_backup.jsonl"
        LOG_PATH_DRY = _PROJECT_ROOT / f"_chrome_backfill_{tag}_dryrun_log.jsonl"
        LOG_PATH_WRITE = _PROJECT_ROOT / f"_chrome_backfill_{tag}_log.jsonl"

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — refusing.")
        return 1
    if not args.dry_run and os.environ.get(
            "USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. "
              "Use --dry-run, or set it true.")
        return 1

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(f"CHROME-BACKFILL — {mode} (batch={args.batch_size})")

    with psycopg.connect(url) as conn:
        if not args.dry_run:
            backup_rows = export_backup(conn)
            print(f"[backup] {BACKUP_PATH} — {backup_rows} rows "
                  "(full id+claim_text export)")

        examined, changes, counts = scan(conn, args.batch_size)
        log_path = LOG_PATH_DRY if args.dry_run else LOG_PATH_WRITE
        write_log(log_path, changes)
        print(f"[log] {log_path} — {len(changes)} change records "
              "(id / before / after / removed), fsync'd")

        print(f"[scan] examined={examined} would_change={len(changes)} "
              f"no_chrome={counts['clean']} match_but_unchanged={counts['unchanged']} "
              f"skipped_empty_result={counts['empty']} "
              f"skipped_below_floor={counts['fragment']}")

        shown = 0
        for row_id, before, after, removed in changes:
            if shown >= args.sample:
                break
            shown += 1
            print(f"--- id={row_id}")
            for fragment in removed:
                print(f"    REMOVED: {fragment}")

        if args.dry_run:
            print(f"[dry-run] DONE — no UPDATE executed. Review "
                  f"{log_path.name} before running write mode.")
            return 0

        updated = 0
        guard_missed = 0
        batch = []
        with conn.cursor() as cur:
            for row_id, before, after, _removed in changes:
                batch.append((after, row_id, before))
                if len(batch) >= args.batch_size:
                    for params in batch:
                        cur.execute(UPDATE_SQL, params)
                        updated += cur.rowcount
                        guard_missed += 1 - cur.rowcount
                    conn.commit()
                    batch = []
            for params in batch:
                cur.execute(UPDATE_SQL, params)
                updated += cur.rowcount
                guard_missed += 1 - cur.rowcount
            conn.commit()

        print(f"[write] DONE: examined={examined} updated={updated} "
              f"guard_missed_concurrent_edit={guard_missed} "
              f"skipped_empty_result={counts['empty']} "
              f"skipped_below_floor={counts['fragment']} "
              f"no_chrome={counts['clean']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
