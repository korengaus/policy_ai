# DOMAIN-LABEL 2b-DIAG — READ-ONLY dry probe: why did the keyword probe say
# education = 709/1020 (69%) of 기타-미분류 while the live classifier DRY run
# said ~6%? This classifies ONLY the education-keyword rows and prints
# title -> label, split by keyword strength:
#
#   STRONG keywords (대입/수능/입시/교육청/교육부/등록금/학기/유치원) are
#   unambiguous education-policy markers — if THESE rows come back
#   statistics/finance/기타-미분류, the CLASSIFIER under-classifies (a 2a
#   prompt-boundary problem, fixable).
#   WEAK keywords (교육/학교/대학/학생/교사) appear inside non-education
#   stories (대학병원=health, 직업교육=labor, 안전교육=misc...) — if only
#   these rows fail to classify education, the KEYWORD PROBE over-counted
#   and education is genuinely small.
#
# Joe runs in the Worker Shell (DATABASE_URL + ANTHROPIC_API_KEY present):
#     PYTHONPATH=. python scripts/education_gap_dry_probe.py            # 40 rows
#     PYTHONPATH=. python scripts/education_gap_dry_probe.py --cap 80
#
# SAFETY: SELECT-only + classifier calls (tool-free Sonnet, ~$0.001/row —
# cap*2 rows max). NO UPDATE/INSERT of any kind, no verdict column read.
# pin-OUT scripts/*; 331/16 unaffected.

import argparse
import collections
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

STRONG_KEYWORDS = ("대입", "수능", "입시", "교육청", "교육부", "등록금",
                   "유치원", "학기")
WEAK_KEYWORDS = ("교육", "학교", "대학", "학생", "교사")

SELECT_MISC_TITLES_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE domain = '기타-미분류' AND id > %s ORDER BY id LIMIT 1000"
)


def _matched(title, keywords):
    for word in keywords:
        if word in (title or ""):
            return word
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="education_gap_dry_probe")
    parser.add_argument("--cap", type=int, default=40,
                        help="Rows to classify PER strength tier (default 40).")
    args = parser.parse_args()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY not set — the classifier cannot run.")
        return 0

    import psycopg
    from domain_classifier import classify_domain

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    strong_rows, weak_rows = [], []
    with psycopg.connect(url) as conn:
        last_id = 0
        while len(strong_rows) < args.cap or len(weak_rows) < args.cap:
            with conn.cursor() as cur:
                cur.execute(SELECT_MISC_TITLES_SQL, (last_id,))
                rows = cur.fetchall()
            if not rows:
                break
            for rid, title, claim in rows:
                last_id = max(last_id, rid)
                strong = _matched(title, STRONG_KEYWORDS)
                if strong and len(strong_rows) < args.cap:
                    strong_rows.append((rid, strong, title, claim))
                    continue
                weak = _matched(title, WEAK_KEYWORDS)
                if weak and len(weak_rows) < args.cap:
                    weak_rows.append((rid, weak, title, claim))

    print("EDUCATION-GAP DRY PROBE — %d strong-keyword rows, %d weak-keyword "
          "rows (DRY: classify + print, NO write)\n"
          % (len(strong_rows), len(weak_rows)))

    verdict_input = {}
    for tier, rows in (("STRONG", strong_rows), ("WEAK", weak_rows)):
        print("== %s-keyword rows -> live classifier label ==" % tier)
        counter = collections.Counter()
        for rid, word, title, claim in rows:
            label = classify_domain(title, claim)  # never raises; no write
            counter[label] += 1
            print("  id=%-6s [%s] %-52.52s -> %s" % (rid, word, title or "", label))
        total = sum(counter.values())
        edu = counter.get("education", 0)
        print("  %s summary: %s | education %d/%d (%.0f%%)\n"
              % (tier, dict(counter.most_common()), edu, total,
                 100.0 * edu / total if total else 0))
        verdict_input[tier] = (edu, total)

    s_edu, s_total = verdict_input.get("STRONG", (0, 0))
    w_edu, w_total = verdict_input.get("WEAK", (0, 0))
    print("== READING GUIDE ==")
    print("  STRONG rows mostly education  -> probe over-counted via the weak "
          "keywords; real education ~= (strong pool size) + (weak pool x weak "
          "education rate). Accept the smaller N.")
    print("  STRONG rows NOT education     -> the 2a prompt under-claims "
          "unambiguous education-policy stories; fix the prompt boundary and "
          "re-DRY before any backfill.")
    print("  (this run: strong %d/%d edu, weak %d/%d edu)"
          % (s_edu, s_total, w_edu, w_total))
    print("\n[Probe] SELECT + classify only; nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
