# MATCHER-MED B5b Phase 1 — READ-ONLY triage probe (pin-OUT, SELECT-only).
#
# Confirms/sizes the three triage claims that need STORED-DATA evidence
# (code-read diagnoses have been wrong before — OOM/DB-FULL lessons).
# Joe runs once in the Worker Shell:
#     PYTHONPATH=. python scripts/matcher_med_triage_probe.py
#
#   A. oer:420-431 overwrite — count candidates where the enrich lane matched
#      (official_body_match_score >= 62) but official_body_match ended False
#      (the sentence lane downgraded it). n>0 = the wiring defect fired on
#      real rows; n==0 = theoretical, deprioritize.
#   F. force-path intake — rows whose debug/source marks forced_fallback:
#      run the REAL _reject_title_reason over their titles offline; n(rejected
#      titles that shipped) sizes the intake leak.
#   G. obit/opinion precedence — primary-pool rows whose title carries BOTH
#      an obituary marker and an opinion marker (the interaction case).
#
# SAFETY: SELECT-only; parses JSON in Python (never SQL ->>); LOW-MEMORY
# keyset pagination fetching only (id, title, source_candidates-or-debug
# slices); imports news_collector ONLY for the pure _reject_title_reason;
# no verdict column is read beyond counts; never prints DATABASE_URL.

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

PAGE = 200

# A: only rows whose candidates mention a fetched body are interesting.
SELECT_CANDIDATES_SQL = (
    "SELECT id, source_candidates FROM analysis_results "
    "WHERE id > %s AND source_candidates LIKE '%%official_body_match%%' "
    "ORDER BY id LIMIT %s"
)
# F: forced_fallback rows (marker lives in debug_summary; source column too).
SELECT_FORCED_SQL = (
    "SELECT id, title FROM analysis_results "
    "WHERE debug_summary LIKE '%%forced_fallback%%' ORDER BY id"
)
# G: primary-pool rows only (no fallback markers) — fetched pages, filtered in
# Python against the real marker lists.
SELECT_TITLES_SQL = (
    "SELECT id, title FROM analysis_results WHERE id > %s ORDER BY id LIMIT %s"
)


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg
    from news_collector import (OBITUARY_MARKERS, OPINION_MARKERS,
                                _reject_title_reason)

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("MATCHER-MED TRIAGE PROBE — SELECT-only\n")
    with psycopg.connect(url) as conn:
        # --- A: enrich-matched but resolve-downgraded candidates -----------
        downgraded = zero_sentence = rows_seen = cands_seen = 0
        samples = []
        last_id = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(SELECT_CANDIDATES_SQL, (last_id, PAGE))
                rows = cur.fetchall()
            if not rows:
                break
            for rid, cand_json in rows:
                last_id = max(last_id, rid)
                rows_seen += 1
                try:
                    cands = json.loads(cand_json or "[]")
                except (TypeError, ValueError):
                    continue
                for cand in cands if isinstance(cands, list) else []:
                    if not isinstance(cand, dict) or "official_body_match" not in cand:
                        continue
                    cands_seen += 1
                    score = int(cand.get("official_body_match_score") or 0)
                    if cand.get("official_body_match") is False and score >= 62:
                        downgraded += 1
                        if len(samples) < 5:
                            samples.append((rid, score,
                                            cand.get("official_evidence_classification")))
                    if (cand.get("official_body_fetched")
                            and cand.get("official_matched_sentences") == []
                            and int(cand.get("official_body_length") or 0) > 0):
                        zero_sentence += 1
        print("== A. resolve-downgraded enrich matches ==")
        print("  rows_with_body_candidates=%d candidates=%d" % (rows_seen, cands_seen))
        print("  match=False but score>=62 (the overwrite fired): %d" % downgraded)
        print("  fetched body, zero split sentences: %d" % zero_sentence)
        for rid, score, cls in samples:
            print("    e.g. row %s score=%s classification=%s" % (rid, score, cls))

        # --- F: forced_fallback titles vs the real reject ------------------
        with conn.cursor() as cur:
            cur.execute(SELECT_FORCED_SQL)
            forced = cur.fetchall()
        leaked = [(rid, title, _reject_title_reason(title or ""))
                  for rid, title in forced]
        bad = [x for x in leaked if x[2]]
        print("\n== F. forced_fallback intake leak ==")
        print("  forced_fallback rows=%d; titles the CURRENT reject would "
              "refuse=%d" % (len(forced), len(bad)))
        for rid, title, reason in bad[:5]:
            print("    row %s [%s] %.60s" % (rid, reason, title))

        # --- G: obit+opinion double-marker titles --------------------------
        both = []
        last_id = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(SELECT_TITLES_SQL, (last_id, 1000))
                rows = cur.fetchall()
            if not rows:
                break
            for rid, title in rows:
                last_id = max(last_id, rid)
                text = title or ""
                if (any(m in text for m in OBITUARY_MARKERS)
                        and any(m in text for m in OPINION_MARKERS)):
                    both.append((rid, text))
        print("\n== G. obit+opinion double-marker titles in corpus ==")
        print("  count=%d" % len(both))
        for rid, title in both[:5]:
            print("    row %s %.70s" % (rid, title))

    print("\n[Probe] SELECT-only; verdicts untouched; n==0 on a section means "
          "that triage item is theoretical on this corpus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
