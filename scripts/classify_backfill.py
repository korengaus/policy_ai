# CLASSIFY-2b — backfill domain labels onto existing `domain IS NULL` rows.
#
# WHY: forward classification (CLASSIFY-2a) only labels NEW rows. Rows analyzed
# before the classifier went live still have domain=NULL. This script classifies
# each of them with the SAME tool-free classifier and writes ONLY the `domain`
# column, so the category UI (CLASSIFY-2c) shows abundant cards immediately.
#
# SAFETY (design locked in CLASSIFY-2b Phase 1):
#   * Writes ONLY the `domain` column. The single UPDATE names `domain` and
#     nothing else — NO verdict/scoring/matcher/official-evidence field.
#   * Always guarded by `AND domain IS NULL`, which makes the write
#     idempotent (re-running skips already-labeled rows), concurrent-safe
#     (a row labeled by forward classification between SELECT and UPDATE is a
#     no-op, never overwritten), and resumable (an interrupted run continues).
#   * REUSES the real `domain_classifier.classify_domain` (never a copy): it is
#     tool-free claude-sonnet-4-6, NEVER raises, and maps any failure /
#     unparseable reply to `기타-미분류`. So backfilled labels match live labels.
#   * Same inputs as live: classify_domain(title, claim_text) where claim_text is
#     the analysis_results.claim_text column (the field forward classification
#     persists from verification_card.claim_text) — so a backfilled row gets the
#     label it would have gotten at analysis time.
#   * Tool-free (inherited): no web_search, no tools. Never prints DATABASE_URL
#     or the API key. Env-guarded: if creds are absent it prints guidance and
#     exits 0 WITHOUT connecting to the DB or calling the API.
#
# WHAT IT DOES NOT: no INSERT/DELETE, no schema change, no verdict-field write,
#   no re-implementation of the classifier, no tools/web_search.
#
# Run in the Render Worker Shell (DATABASE_URL + ANTHROPIC_API_KEY + the `domain`
# column all present), AFTER an Export backup. Batched; survives interruption.

import argparse
import collections
import os
import sys
import time

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunables (top-of-file, commented).
# ---------------------------------------------------------------------------
# Default rows per batch (one commit per batch — progress survives interruption).
DEFAULT_BATCH = 50
# Gentle pacing between API calls (matches the probe; not a rate-limit workaround).
PACING_SECONDS = 0.05
# Rough per-row cost for the running-estimate line only (tool-free, 1 row/call,
# ~400-500 input + ~5 output tokens). Display-only — NOT used for any decision.
ROUGH_COST_PER_ROW = 0.0010

# The guarded, domain-ONLY UPDATE. Module-level so a test can assert its shape.
# Writes the `domain` column and nothing else; the `AND domain IS NULL` guard
# makes it idempotent + concurrent-safe + resumable.
UPDATE_SQL = "UPDATE analysis_results SET domain = %s WHERE id = %s AND domain IS NULL"

# Read-only batch fetch of still-unlabeled rows. ORDER BY id for stable resume.
# Real mode relies on the UPDATE removing each row from `domain IS NULL`, so the
# next batch naturally advances and the loop ends when none remain.
SELECT_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE domain IS NULL ORDER BY id LIMIT %s"
)
# Dry-run writes nothing, so `domain IS NULL` would never shrink — paginate by id
# (`id > last_id`) so the preview advances through all NULL rows and terminates.
SELECT_DRYRUN_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE domain IS NULL AND id > %s ORDER BY id LIMIT %s"
)

# Read-only remaining-work count (printed in the summary).
COUNT_NULL_SQL = "SELECT count(*) FROM analysis_results WHERE domain IS NULL"

# ---------------------------------------------------------------------------
# DOMAIN-LABEL 2b — additive RE-CLASSIFY target: the 기타-미분류 fallback pool.
# Now that 'education' exists (2a), rows the classifier previously fell back on
# can be re-labeled. The guard `AND domain = '기타-미분류'` means a row that
# already carries a REAL label is NEVER overwritten. Unlike the NULL path,
# BOTH modes paginate by keyset (`id > last_id`): a row the classifier AGAIN
# labels 기타-미분류 stays in the target set, so relying on the UPDATE to
# shrink the set (the NULL path's loop) would refetch it forever. A row moved
# to a real label leaves the pool -> re-runs skip it (idempotent); a re-run
# after interruption simply advances past already-moved ids (resumable).
# ---------------------------------------------------------------------------
MISC_LABEL = "기타-미분류"
UPDATE_MISC_SQL = ("UPDATE analysis_results SET domain = %s "
                   "WHERE id = %s AND domain = '기타-미분류'")
SELECT_MISC_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE domain = '기타-미분류' AND id > %s ORDER BY id LIMIT %s"
)
COUNT_MISC_SQL = ("SELECT count(*) FROM analysis_results "
                  "WHERE domain = '기타-미분류'")


def _normalize_url(raw_url: str) -> str:
    """Mirror scripts/classify_probe.py: psycopg wants a plain libpq URL, not the
    SQLAlchemy driver form."""
    return (raw_url.replace("postgresql+psycopg://", "postgresql://")
                   .replace("postgresql+psycopg2://", "postgresql://"))


def _classify_batch(conn, classify, limit, remaining_cap, dry_run, counts,
                    batch_no, last_id, reclassify_misc=False):
    """Fetch up to `limit` target rows, classify each, and (unless dry-run)
    write ONLY the domain column via the guarded UPDATE. Commits once at the end
    of the batch. Returns (processed, max_id_seen) — the caller advances the
    id cursor with max_id_seen (used by dry-run, and ALWAYS by misc mode).

    `remaining_cap` (or None) caps how many rows this batch may process so a
    --max-rows total is honored. `counts` is a Counter mutated with the assigned
    labels for the final summary. `reclassify_misc` switches the target from
    `domain IS NULL` to `domain = '기타-미분류'` (DOMAIN-LABEL 2b): keyset
    pagination in BOTH modes, and a row re-labeled 기타-미분류 is SKIPPED
    (no-op write avoided; the keyset advances past it).
    """
    with conn.cursor() as cur:
        if reclassify_misc:
            cur.execute(SELECT_MISC_SQL, (last_id, limit))
        elif dry_run:
            cur.execute(SELECT_DRYRUN_SQL, (last_id, limit))
        else:
            cur.execute(SELECT_SQL, (limit,))
        rows = cur.fetchall()

    if remaining_cap is not None:
        rows = rows[:remaining_cap]

    processed = 0
    max_id = last_id
    with conn.cursor() as cur:
        for rid, title, claim_text in rows:
            # classify_domain NEVER raises — failures/unparseable → 기타-미분류.
            label = classify(title, claim_text)
            counts[label] += 1
            if dry_run:
                print("  [dry-run] id=%s %s -> %s"
                      % (rid,
                         MISC_LABEL if reclassify_misc else "(NULL)",
                         label))
            elif reclassify_misc:
                if label != MISC_LABEL:
                    # domain-ONLY write, guarded to the fallback pool — a row
                    # with a real label is never overwritten.
                    cur.execute(UPDATE_MISC_SQL, (label, rid))
                # label == 기타-미분류: leave it; keyset advances past it.
            else:
                # domain-ONLY write, guarded by IS NULL (idempotent/concurrent-safe).
                cur.execute(UPDATE_SQL, (label, rid))
            processed += 1
            if rid > max_id:
                max_id = rid
            time.sleep(PACING_SECONDS)

    if not dry_run:
        conn.commit()  # per-batch commit so an interrupted run keeps progress.

    return processed, max_id


def run_backfill(conn, classify, batch, max_rows, dry_run,
                 reclassify_misc=False):
    """Drive the batched backfill against an already-open connection `conn`.
    `classify` is injected (the real classify_domain in main) so tests can pass a
    fake; `conn` is injected so tests can pass a fake connection with no DB.
    `reclassify_misc=True` targets the 기타-미분류 pool instead of NULLs
    (DOMAIN-LABEL 2b) — see _classify_batch."""
    counts = collections.Counter()
    total = 0
    batch_no = 0
    last_id = 0  # id cursor (dry-run always; misc mode always)
    while True:
        if max_rows is not None and total >= max_rows:
            break
        this_limit = batch
        remaining_cap = None
        if max_rows is not None:
            remaining_cap = max_rows - total
            this_limit = min(batch, remaining_cap)
            if this_limit <= 0:
                break

        batch_no += 1
        processed, last_id = _classify_batch(
            conn, classify, this_limit, remaining_cap, dry_run, counts, batch_no,
            last_id, reclassify_misc=reclassify_misc,
        )
        if processed == 0:
            break
        total += processed
        print("[backfill] batch %d: classified %d rows (running total %d)%s"
              % (batch_no, processed, total,
                 "  ~$%.3f" % (total * ROUGH_COST_PER_ROW)))

    # Re-query remaining target rows for the summary (read-only).
    with conn.cursor() as cur:
        cur.execute(COUNT_MISC_SQL if reclassify_misc else COUNT_NULL_SQL)
        remaining = cur.fetchone()[0]

    moved = total - counts.get(MISC_LABEL, 0)
    # ---- Final summary -----------------------------------------------------
    print()
    print("=== SUMMARY%s ===" % (" (DRY-RUN — no writes)" if dry_run else ""))
    print("  total rows classified this run : %d" % total)
    print("  labels assigned:")
    for label, n in counts.most_common():
        print("      %-14s %d" % (label, n))
    if reclassify_misc:
        print("  moved to a real label          : %d" % moved)
        print("  stayed 기타-미분류             : %d" % counts.get(MISC_LABEL, 0))
        print("  rows still 기타-미분류 (DB)    : %d%s"
              % (remaining, "  (dry-run: nothing written)" if dry_run else ""))
    else:
        print("  fell to 기타-미분류             : %d" % counts.get("기타-미분류", 0))
        print("  rows still domain IS NULL      : %d%s"
              % (remaining, "  (dry-run: nothing written)" if dry_run else ""))
    print("  rough estimated spend          : ~$%.3f" % (total * ROUGH_COST_PER_ROW))
    print()
    print("[Safety] Wrote ONLY the `domain` column via "
          "`%s`." % (UPDATE_MISC_SQL if reclassify_misc else UPDATE_SQL))
    print("         No verdict/scoring/matcher field touched; tool-free; "
          "no secrets printed.")
    return total


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="classify_backfill",
        description="Backfill domain labels onto existing domain IS NULL rows "
                    "(metadata-only; writes the domain column only).",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help="Batch size (rows per SELECT/commit). Default %d." % DEFAULT_BATCH)
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Optional total cap for this run. Default: drain all NULLs.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and print would-be labels; SKIP the UPDATE.")
    parser.add_argument("--reclassify-misc", action="store_true",
                        help="DOMAIN-LABEL 2b: target domain='기타-미분류' rows "
                             "instead of NULLs (re-classify the fallback pool; "
                             "never overwrites a real label).")
    args = parser.parse_args(argv)

    # --- Env guard: NO DB connect / NO API call when creds are absent. --------
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this backfill must run in the Render Worker "
              "Shell (or locally with $env:DATABASE_URL pointed at the external DB).")
        return 0
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY not set — the tool-free Sonnet classifier cannot run.")
        return 0

    # Import the REAL classifier lazily (after the env guard) so importing this
    # module is side-effect-free and never connects or calls the API.
    from domain_classifier import classify_domain

    url = _normalize_url(raw_url)
    if args.limit <= 0:
        print("--limit must be positive.")
        return 2

    print("CLASSIFY-2b backfill — tool-free Sonnet domain labels (domain column only)")
    print("  batch=%d  max_rows=%s  dry_run=%s  target=%s"
          % (args.limit, args.max_rows if args.max_rows is not None else "all",
             args.dry_run,
             "기타-미분류 (re-classify)" if args.reclassify_misc else "domain IS NULL"))
    print()
    with psycopg.connect(url) as conn:
        run_backfill(conn, classify_domain, args.limit, args.max_rows,
                     args.dry_run, reclassify_misc=args.reclassify_misc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
