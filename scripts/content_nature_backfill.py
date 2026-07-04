# CONTENT-NATURE-BACKFILL — backfill content_nature labels onto existing
# `content_nature IS NULL/''` rows. Modeled EXACTLY on scripts/classify_backfill.py
# (the CLASSIFY-2b domain twin): batched, per-batch commit, IS-NULL-guarded
# idempotent UPDATE, injected real classifier. The ONE addition over the twin is
# reversibility id-logging (see ADD below).
#
# WHY: forward classification (NOISE1-A) only labels NEW rows. 607 rows analyzed
# before content_nature went live have content_nature=NULL. This script labels
# each with the SAME tool-free classifier and writes ONLY the `content_nature`
# column, so the Part-B recall probe has an adequate market_commercial sample.
#
# SAFETY (mirrors classify_backfill.py):
#   * Writes ONLY the `content_nature` column. The single UPDATE names
#     `content_nature` and nothing else — NO verdict/scoring/label field
#     (verdict_label / policy_alert_level / truth_claim / operator_review_required
#     / score all untouched). content_nature is verdict-isolated metadata.
#   * Guarded by `AND (content_nature IS NULL OR content_nature = '')`, which makes
#     the write idempotent (re-running skips already-labeled rows), concurrent-safe
#     (a row labeled by forward classification between SELECT and UPDATE is a no-op,
#     never overwritten), and resumable (an interrupted Worker-Shell session
#     continues on the remaining rows).
#   * REUSES the real `content_nature_classifier.classify_content_nature` (never a
#     copy): tool-free claude-sonnet-4-6, metadata-only, NEVER raises, maps any
#     failure/unparseable reply to `mixed_or_unclear`. So backfilled labels match
#     live labels.
#   * Same inputs as live: classify_content_nature(title, claim_text) where
#     claim_text is the analysis_results.claim_text column — the field forward
#     classification feeds the live call from (verification_card.claim_text,
#     main.py:1361/1374/1405). No analyze pipeline, no browser crawler, no OOM.
#   * LOW-MEMORY: SELECTs only id, title, claim_text — never source_candidates or
#     any heavy blob — so it never loads the ingestion-backfill weight.
#   * Tool-free (inherited): no web_search, no tools. Never prints DATABASE_URL or
#     the API key. Env-guarded: absent creds -> guidance + exit 0, no DB/API touch.
#
# ADD vs the domain twin (reversibility): every successfully-updated id is appended
#   to scripts/content_nature_backfill_updated_ids.log (one id per line, append
#   mode, ONLY after the batch commit succeeds). Rollback later =
#   `UPDATE analysis_results SET content_nature = NULL WHERE id IN (<logged ids>)`,
#   or restore the pre-run Export.
#
# WHAT IT DOES NOT: no INSERT/DELETE, no schema change, no verdict-field write, no
#   re-implementation of the classifier, no tools/web_search.
#
# Run in the Render Worker Shell (DATABASE_URL + ANTHROPIC_API_KEY present), AFTER
# an Export backup. Batched; survives interruption. Offline logic check: --selftest.

import argparse
import collections
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunables (top-of-file, commented).
# ---------------------------------------------------------------------------
# Default rows per batch (one commit per batch — progress survives interruption).
DEFAULT_BATCH = 40
# Gentle pacing between API calls (matches the domain twin; not a rate-limit workaround).
PACING_SECONDS = 0.05
# Rough per-row cost for the running-estimate line only (tool-free, 1 row/call,
# ~400-500 input + ~5 output tokens). Display-only — NOT used for any decision.
ROUGH_COST_PER_ROW = 0.0010

# Reversibility id-log (the ONE addition over classify_backfill.py). Append-only,
# one updated id per line, written ONLY after a batch commit succeeds.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "content_nature_backfill_updated_ids.log")

# The guarded, content_nature-ONLY UPDATE. Module-level so a test can assert its
# shape. Writes the `content_nature` column and nothing else; the IS-NULL/'' guard
# makes it idempotent + concurrent-safe + resumable and NEVER overwrites a label.
UPDATE_SQL = ("UPDATE analysis_results SET content_nature = %s "
              "WHERE id = %s AND (content_nature IS NULL OR content_nature = '')")

# Read-only batch fetch of still-unlabeled rows. ORDER BY id for stable resume.
# Real mode relies on the UPDATE removing each row from the missing set, so the
# next batch naturally advances and the loop ends when none remain. LOW-MEMORY:
# only id, title, claim_text (never source_candidates or any heavy blob).
SELECT_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE (content_nature IS NULL OR content_nature = '') ORDER BY id LIMIT %s"
)
# Dry-run writes nothing, so the missing set would never shrink — paginate by id
# (`id > last_id`) so the preview advances through all missing rows and terminates.
SELECT_DRYRUN_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE (content_nature IS NULL OR content_nature = '') AND id > %s "
    "ORDER BY id LIMIT %s"
)

# Read-only remaining-work count (printed in the summary).
COUNT_NULL_SQL = ("SELECT count(*) FROM analysis_results "
                  "WHERE content_nature IS NULL OR content_nature = ''")


def _normalize_url(raw_url: str) -> str:
    """Mirror scripts/classify_backfill.py: psycopg wants a plain libpq URL, not
    the SQLAlchemy driver form."""
    return (raw_url.replace("postgresql+psycopg://", "postgresql://")
                   .replace("postgresql+psycopg2://", "postgresql://"))


def _append_updated_ids(path, ids) -> None:
    """Append committed row ids to the reversibility log (one per line). Called
    ONLY after conn.commit() so the log never lists an uncommitted id."""
    if not ids:
        return
    with open(path, "a", encoding="utf-8") as f:
        for rid in ids:
            f.write("%s\n" % rid)


def _classify_batch(conn, classify, limit, remaining_cap, dry_run, counts,
                    batch_no, last_id, id_log_path):
    """Fetch up to `limit` missing-content_nature rows, classify each, and (unless
    dry-run) write ONLY the content_nature column via the guarded UPDATE. Commits
    once at the end of the batch, THEN appends the committed ids to id_log_path.
    Returns (processed, max_id_seen) — the caller advances the dry-run id cursor
    with max_id_seen.

    `remaining_cap` (or None) caps how many rows this batch may process so a
    --max-rows total is honored. `counts` is a Counter mutated with the assigned
    labels for the final summary.
    """
    with conn.cursor() as cur:
        if dry_run:
            cur.execute(SELECT_DRYRUN_SQL, (last_id, limit))
        else:
            cur.execute(SELECT_SQL, (limit,))
        rows = cur.fetchall()

    if remaining_cap is not None:
        rows = rows[:remaining_cap]

    processed = 0
    max_id = last_id
    batch_ids = []
    with conn.cursor() as cur:
        for rid, title, claim_text in rows:
            # classify_content_nature NEVER raises — failures/unparseable ->
            # mixed_or_unclear (its own fail-to-safe fallback).
            label = classify(title, claim_text)
            counts[label] += 1
            if not dry_run:
                # content_nature-ONLY write, guarded by IS NULL/'' (idempotent/
                # concurrent-safe/never overwrites a live label).
                cur.execute(UPDATE_SQL, (label, rid))
                # rowcount 0 => the guard skipped an already-labeled row; don't log
                # it as updated. (Fake + real psycopg cursors both expose rowcount.)
                if getattr(cur, "rowcount", 1) != 0:
                    batch_ids.append(rid)
            else:
                print("  [dry-run] id=%s would set content_nature=%s" % (rid, label))
            processed += 1
            if rid > max_id:
                max_id = rid
            time.sleep(PACING_SECONDS)

    if not dry_run:
        conn.commit()                        # per-batch commit — progress survives interruption
        _append_updated_ids(id_log_path, batch_ids)  # log ONLY after commit succeeds

    return processed, max_id


def run_backfill(conn, classify, batch, max_rows, dry_run, id_log_path=LOG_FILE):
    """Drive the batched backfill against an already-open connection `conn`.
    `classify` is injected (the real classify_content_nature in main) so tests can
    pass a fake; `conn` is injected so tests can pass a fake connection with no DB.
    `id_log_path` is injected so tests can point the reversibility log at a tmp file."""
    counts = collections.Counter()
    total = 0
    batch_no = 0
    last_id = 0  # dry-run id cursor (ignored in real mode)
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
            last_id, id_log_path,
        )
        if processed == 0:
            break
        total += processed
        print("[backfill] batch %d: classified %d rows (running total %d)%s"
              % (batch_no, processed, total,
                 "  ~$%.3f" % (total * ROUGH_COST_PER_ROW)))

    # Re-query remaining missing rows for the summary (read-only).
    with conn.cursor() as cur:
        cur.execute(COUNT_NULL_SQL)
        remaining_null = cur.fetchone()[0]

    # ---- Final summary -----------------------------------------------------
    print()
    print("=== SUMMARY%s ===" % (" (DRY-RUN — no writes)" if dry_run else ""))
    print("  total rows classified this run : %d" % total)
    print("  labels assigned:")
    for label, n in counts.most_common():
        print("      %-18s %d" % (label, n))
    print("  fell to mixed_or_unclear       : %d" % counts.get("mixed_or_unclear", 0))
    print("  rows still content_nature empty: %d%s"
          % (remaining_null, "  (dry-run: nothing written)" if dry_run else ""))
    print("  rough estimated spend          : ~$%.3f" % (total * ROUGH_COST_PER_ROW))
    if not dry_run:
        print("  reversibility id-log           : %s" % id_log_path)
    print()
    print("[Safety] Wrote ONLY the `content_nature` column via "
          "`%s`." % UPDATE_SQL)
    print("         No verdict/scoring/label field touched; tool-free; "
          "no secrets printed.")
    return total


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — fake in-memory DB + mock classifier (no DB, no network).
# ---------------------------------------------------------------------------
def _is_missing(val) -> bool:
    return val is None or (isinstance(val, str) and val.strip() == "")


class _FakeCursor:
    """Minimal psycopg-cursor stand-in that honors the module SQL constants +
    the IS-NULL/'' guard, so the selftest exercises the REAL run_backfill logic."""

    def __init__(self, store):
        self.store = store
        self._result = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        params = params or ()
        if sql == SELECT_SQL:
            (limit,) = params
            rows = sorted((r for r in self.store.rows if _is_missing(r["content_nature"])),
                          key=lambda r: r["id"])
            self._result = [(r["id"], r["title"], r["claim_text"]) for r in rows[:limit]]
        elif sql == SELECT_DRYRUN_SQL:
            last_id, limit = params
            rows = sorted((r for r in self.store.rows
                           if _is_missing(r["content_nature"]) and r["id"] > last_id),
                          key=lambda r: r["id"])
            self._result = [(r["id"], r["title"], r["claim_text"]) for r in rows[:limit]]
        elif sql == UPDATE_SQL:
            label, rid = params
            hit = 0
            for r in self.store.rows:
                if r["id"] == rid and _is_missing(r["content_nature"]):
                    r["content_nature"] = label
                    hit = 1
            self.rowcount = hit
            self._result = None
        elif sql == COUNT_NULL_SQL:
            n = sum(1 for r in self.store.rows if _is_missing(r["content_nature"]))
            self._result = [(n,)]
        else:
            raise AssertionError("unexpected SQL in selftest: %r" % sql)

    def fetchall(self):
        return list(self._result or [])

    def fetchone(self):
        return (self._result or [None])[0]


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0


class _FakeConn:
    def __init__(self, rows):
        self.store = _FakeStore(rows)

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        self.store.commits += 1


def _fresh_rows():
    # id3 already labeled (guard must skip); id4 empty-string (guard must include).
    return [
        {"id": 1, "title": "신규 아파트 분양 마케팅", "claim_text": "분양가 안내", "content_nature": None},
        {"id": 2, "title": "정부 정책 발표", "claim_text": "지원 대책", "content_nature": None},
        {"id": 3, "title": "이미 라벨된 행", "claim_text": "정책", "content_nature": "government_policy"},
        {"id": 4, "title": "빈 문자열 라벨", "claim_text": "정책 개편", "content_nature": ""},
        {"id": 5, "title": "애매한 기타", "claim_text": "기타 내용", "content_nature": None},
    ]


def _mock_classify(title, claim_text):
    t = "%s %s" % (title or "", claim_text or "")
    if "분양" in t:
        return "market_commercial"
    if "정책" in t:
        return "government_policy"
    return "mixed_or_unclear"


def run_selftest() -> int:
    import tempfile

    print("=== CONTENT-NATURE-BACKFILL --selftest (offline; no DB, no network) ===")
    # Assert the module UPDATE names ONLY content_nature + carries the IS-NULL guard.
    guard_ok = ("SET content_nature =" in UPDATE_SQL
                and "content_nature IS NULL OR content_nature = ''" in UPDATE_SQL
                and "verdict" not in UPDATE_SQL and "score" not in UPDATE_SQL)
    print("  [%s] UPDATE writes content_nature only + IS-NULL guard" % ("ok" if guard_ok else "xx"))

    tmpdir = tempfile.mkdtemp(prefix="cnbf_selftest_")

    # --- Test 1: REAL run honors the guard + logs exactly the updated ids. ------
    log1 = os.path.join(tmpdir, "ids_real.log")
    conn = _FakeConn(_fresh_rows())
    run_backfill(conn, _mock_classify, batch=2, max_rows=None, dry_run=False, id_log_path=log1)
    by_id = {r["id"]: r for r in conn.store.rows}
    updated = {r["id"]: r["content_nature"] for r in conn.store.rows}
    logged = [int(x) for x in open(log1, encoding="utf-8").read().split()] if os.path.exists(log1) else []
    guard_skipped = by_id[3]["content_nature"] == "government_policy"      # untouched
    filled = (updated[1] == "market_commercial" and updated[2] == "government_policy"
              and updated[4] == "government_policy" and updated[5] == "mixed_or_unclear")
    log_matches = sorted(logged) == [1, 2, 4, 5]                          # id3 NOT logged
    id3_not_logged = 3 not in logged
    print("  [%s] real run filled 4 missing rows, mock labels correct" % ("ok" if filled else "xx"))
    print("  [%s] IS-NULL guard skipped already-labeled id=3 (not overwritten, not logged)"
          % ("ok" if guard_skipped and id3_not_logged else "xx"))
    print("  [%s] reversibility id-log appended exactly [1,2,4,5] (got %s)"
          % ("ok" if log_matches else "xx", sorted(logged)))

    # --- Test 2: DRY-RUN writes nothing + no id-log file created. ----------------
    log2 = os.path.join(tmpdir, "ids_dry.log")
    conn2 = _FakeConn(_fresh_rows())
    run_backfill(conn2, _mock_classify, batch=2, max_rows=None, dry_run=True, id_log_path=log2)
    dry_unchanged = all(_is_missing(r["content_nature"]) for r in conn2.store.rows
                        if r["id"] in (1, 2, 4, 5)) and conn2.store.commits == 0
    dry_no_log = not os.path.exists(log2)
    print("  [%s] dry-run wrote nothing (0 commits, missing rows still empty)"
          % ("ok" if dry_unchanged else "xx"))
    print("  [%s] dry-run created no id-log file" % ("ok" if dry_no_log else "xx"))

    # --- Test 3: --max-rows caps the total processed. ---------------------------
    log3 = os.path.join(tmpdir, "ids_cap.log")
    conn3 = _FakeConn(_fresh_rows())
    total3 = run_backfill(conn3, _mock_classify, batch=40, max_rows=2, dry_run=False, id_log_path=log3)
    logged3 = [int(x) for x in open(log3, encoding="utf-8").read().split()] if os.path.exists(log3) else []
    cap_ok = total3 == 2 and len(logged3) == 2
    print("  [%s] --max-rows 2 processed exactly 2 rows (logged %d)"
          % ("ok" if cap_ok else "xx", len(logged3)))

    ok = all([guard_ok, filled, guard_skipped, id3_not_logged, log_matches,
              dry_unchanged, dry_no_log, cap_ok])
    print()
    print("SELFTEST: %s" % ("PASS (IS-NULL guard + id-log append + dry-run no-write + max-rows cap)"
                            if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="content_nature_backfill",
        description="Backfill content_nature labels onto existing content_nature "
                    "IS NULL/'' rows (metadata-only; writes the content_nature "
                    "column only, reversible id-log).",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help="Batch size (rows per SELECT/commit). Default %d." % DEFAULT_BATCH)
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Optional total cap for this run. Default: drain all missing rows.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and print would-be labels; SKIP the UPDATE.")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

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

    # Import psycopg + the REAL classifier lazily (after the env guard) so importing
    # this module is side-effect-free and never connects or calls the API.
    import psycopg
    from content_nature_classifier import classify_content_nature

    url = _normalize_url(raw_url)
    if args.limit <= 0:
        print("--limit must be positive.")
        return 2

    print("CONTENT-NATURE-BACKFILL — tool-free Sonnet content_nature labels (content_nature column only)")
    print("  batch=%d  max_rows=%s  dry_run=%s"
          % (args.limit, args.max_rows if args.max_rows is not None else "all",
             args.dry_run))
    print()
    with psycopg.connect(url) as conn:
        run_backfill(conn, classify_content_nature, args.limit, args.max_rows, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
