# BACKFILL-RERESOLVE B5a Phase 1 — READ-ONLY diagnosis probe (pin-OUT).
#
# QUESTION: do the APIs-OFF backfill rows measurably understate official-source
# resolution vs live rows? This probe answers it from STORED markers only —
# no re-run, no reconstruction, no write of any kind (SELECT-only; the
# standard probe idiom). Joe runs it once in the Render Worker Shell:
#
#     PYTHONPATH=. python scripts/backfill_official_gap_probe.py
#
# GROUND-TRUTH MARKERS (why stored data can answer this):
#   * cohort:   debug_summary '"ingest_origin"' — backfill_pilot /
#               backfill_scale_* rows are tagged; live rows lack the key.
#   * flag state AT ANALYSIS TIME: main.py adds debug_summary keys
#     policy_briefing_count / national_law_count / fss_count ONLY when the
#     matching *_ENABLED flag was on (in-branch-only keys, main.py:904-918).
#     Key present == API was ON for that row. No guessing.
#   * outcome:  verification_strength column, policy_confidence_score column,
#               source_reliability_summary '"has_genuine_official_support"'.
#
# BUCKETS (the §1 floor layers):
#   (a) fixable        = rows analyzed with the official APIs OFF that today
#                        show weak/no official support — the ONLY bucket a
#                        re-resolution could help. Upper bound printed.
#   (b)+(c) honest     = the no-match rate measured on rows analyzed WITH the
#                        APIs ON — matches that don't exist even when we look.
#   Estimated conversion of (a) = (a) x (match rate of the APIs-ON cohort).
#
# SAFETY: SELECT-only (aggregates computed server-side; no debug_summary is
# ever transferred), verdict-free output (counts/rates only), never prints
# DATABASE_URL, refuses without DATABASE_URL. pin-OUT scripts/* — no pinned
# file touched, 331/16 unaffected.

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

# Cohort expression reused by every query. Matches the JSON text form written
# by json.dumps (': ' separator) but tolerates compact form via double LIKE.
COHORT_SQL = (
    "CASE "
    "WHEN debug_summary LIKE '%\"ingest_origin\"%backfill_scale%' THEN 'backfill_scale' "
    "WHEN debug_summary LIKE '%\"ingest_origin\"%backfill_pilot%' THEN 'backfill_pilot' "
    "WHEN debug_summary LIKE '%\"ingest_origin\"%' THEN 'other_tagged' "
    "ELSE 'live' END"
)

MAIN_SQL = f"""
SELECT
  {COHORT_SQL} AS cohort,
  COUNT(*) AS rows,
  MIN(created_at) AS first_at,
  MAX(created_at) AS last_at,
  SUM(CASE WHEN debug_summary LIKE '%"policy_briefing_count"%' THEN 1 ELSE 0 END) AS pb_on,
  SUM(CASE WHEN debug_summary LIKE '%"national_law_count"%' THEN 1 ELSE 0 END) AS law_on,
  SUM(CASE WHEN debug_summary LIKE '%"fss_count"%' THEN 1 ELSE 0 END) AS fss_on,
  SUM(CASE WHEN source_reliability_summary LIKE '%"has_genuine_official_support": true%'
        OR source_reliability_summary LIKE '%"has_genuine_official_support":true%'
      THEN 1 ELSE 0 END) AS genuine_true,
  SUM(CASE WHEN verification_strength IN ('none', '') OR verification_strength IS NULL
      THEN 1 ELSE 0 END) AS strength_none,
  ROUND(AVG(COALESCE(policy_confidence_score, 0)), 1) AS avg_conf
FROM analysis_results
GROUP BY 1
ORDER BY rows DESC
"""

# The flag-state x outcome cross — the actual gap measurement. Splits EVERY
# row by whether the official APIs were on at analysis time, regardless of
# cohort (backfill ran across dates; some live rows may predate M21/M23 too).
CROSS_SQL = f"""
SELECT
  {COHORT_SQL} AS cohort,
  CASE WHEN debug_summary LIKE '%"policy_briefing_count"%'
         OR debug_summary LIKE '%"national_law_count"%'
       THEN 'apis_on' ELSE 'apis_off' END AS flag_state,
  COUNT(*) AS rows,
  SUM(CASE WHEN source_reliability_summary LIKE '%"has_genuine_official_support": true%'
        OR source_reliability_summary LIKE '%"has_genuine_official_support":true%'
      THEN 1 ELSE 0 END) AS genuine_true,
  SUM(CASE WHEN verification_strength IN ('none', '') OR verification_strength IS NULL
      THEN 1 ELSE 0 END) AS strength_none,
  ROUND(AVG(COALESCE(policy_confidence_score, 0)), 1) AS avg_conf
FROM analysis_results
GROUP BY 1, 2
ORDER BY 1, 2
"""

STRENGTH_SQL = f"""
SELECT
  {COHORT_SQL} AS cohort,
  COALESCE(NULLIF(verification_strength, ''), '(unset)') AS strength,
  COUNT(*) AS rows
FROM analysis_results
GROUP BY 1, 2
ORDER BY 1, 3 DESC
"""


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("BACKFILL OFFICIAL-GAP PROBE — SELECT-only, stored markers only\n")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(MAIN_SQL)
            main_rows = cur.fetchall()
            cur.execute(CROSS_SQL)
            cross_rows = cur.fetchall()
            cur.execute(STRENGTH_SQL)
            strength_rows = cur.fetchall()

    print("== 1. Cohorts (creation method x stored official markers) ==")
    print("%-15s %6s %8s %8s %8s %13s %14s %9s  %s .. %s"
          % ("cohort", "rows", "pb_on", "law_on", "fss_on",
             "genuine_true", "strength_none", "avg_conf", "first", "last"))
    for (cohort, rows, first_at, last_at, pb_on, law_on, fss_on,
         genuine_true, strength_none, avg_conf) in main_rows:
        print("%-15s %6d %8d %8d %8d %8d (%3.0f%%) %9d (%3.0f%%) %9s  %.10s .. %.10s"
              % (cohort, rows, pb_on, law_on, fss_on,
                 genuine_true, 100.0 * genuine_true / rows if rows else 0,
                 strength_none, 100.0 * strength_none / rows if rows else 0,
                 avg_conf, str(first_at), str(last_at)))

    print("\n== 2. THE GAP: flag-state at analysis time x outcome ==")
    print("%-15s %-9s %6s %13s %14s %9s"
          % ("cohort", "flags", "rows", "genuine_true", "strength_none", "avg_conf"))
    on_match_rate = None
    fixable_upper = 0
    for cohort, flag_state, rows, genuine_true, strength_none, avg_conf in cross_rows:
        rate = 100.0 * genuine_true / rows if rows else 0.0
        print("%-15s %-9s %6d %8d (%3.0f%%) %9d (%3.0f%%) %9s"
              % (cohort, flag_state, rows, genuine_true, rate,
                 strength_none, 100.0 * strength_none / rows if rows else 0,
                 avg_conf))
        if flag_state == "apis_on" and rows >= 30:
            # pooled APIs-ON match rate (any cohort) — the (b)/(c) estimator.
            on_match_rate = (on_match_rate or 0) + genuine_true
        if flag_state == "apis_off":
            fixable_upper += rows - genuine_true

    on_rows_total = sum(r[2] for r in cross_rows if r[1] == "apis_on")
    on_true_total = sum(r[3] for r in cross_rows if r[1] == "apis_on")
    print("\n== 3. Buckets ==")
    print("(a) fixable UPPER BOUND (apis_off rows without genuine support): %d"
          % fixable_upper)
    if on_rows_total:
        rate = on_true_total / on_rows_total
        print("APIs-ON genuine-support rate (the conversion estimator): "
              "%.1f%% (n=%d)" % (100 * rate, on_rows_total))
        print("(a) ESTIMATED CONVERTIBLE on re-resolution: ~%d rows"
              % int(fixable_upper * rate))
        print("(b)+(c) honest floor estimate: ~%d of the %d apis_off-weak rows"
              % (int(fixable_upper * (1 - rate)), fixable_upper))
    else:
        print("No apis_on rows found — conversion rate unmeasurable from "
              "stored data (the whole corpus predates the providers?).")

    print("\n== 4. verification_strength distribution ==")
    for cohort, strength, rows in strength_rows:
        print("  %-15s %-12s %6d" % (cohort, strength, rows))

    print("\n[Probe] SELECT-only; nothing written, no verdict field read "
          "beyond aggregate counts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
