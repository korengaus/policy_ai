# STATS-SOURCE Phase 1 — READ-ONLY demand probe (pin-OUT, SELECT-only).
#
# QUESTION (the FSS lesson: domain size != addressable size): how many rows —
# in the statistics domain and corpus-wide — make a CHECKABLE NUMERIC claim
# (a specific figure an official statistic could confirm or contradict:
# "청년 실업률 7.6%", "출산율 0.72명", "물가 3.1% 상승")? Only those rows could
# ever benefit from a KOSIS figure source; vague statistical framing
# ("고용 지표 개선") cannot be grounded by any table cell.
#
# Joe runs once in the Render Worker Shell:
#     PYTHONPATH=. python scripts/stats_claim_demand_probe.py
#
# Buckets per row (title + claim_text):
#   CHECKABLE  — a stat-indicator word AND a specific figure near it
#                (%, %p, 배, 명/만 명, 억/조 원, 포인트, 건).
#   NUMERIC    — a figure but no indicator word (money amounts in policy
#                stories etc. — NOT stat-checkable).
#   INDICATOR  — an indicator word but NO figure (vague framing).
#   NEITHER    — everything else.
# Also prints the top indicator words inside CHECKABLE + samples, so the
# "curated headline indicators" idea can be sized honestly.
#
# SAFETY: SELECT-only (id, domain, title, claim_text). No verdict column, no
# writes. Keyset pagination. Never prints DATABASE_URL. pin-OUT scripts/*.

import collections
import os
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

SELECT_SQL = (
    "SELECT id, COALESCE(domain, '(NULL)'), title, claim_text "
    "FROM analysis_results WHERE id > %s ORDER BY id LIMIT 1000"
)

# Statistic-indicator vocabulary (the kinds of figures KOSIS publishes).
INDICATOR_WORDS = (
    "실업률", "고용률", "물가", "소비자물가", "출산율", "출생아", "인구",
    "성장률", "증가율", "감소율", "상승률", "가계부채", "가계소득", "임금",
    "고용보험", "취업자", "실업자", "지니계수", "빈곤율", "자살률",
    "혼인", "이혼", "매매가", "전세가", "미분양", "수출", "수입", "무역수지",
)
# A specific figure: 7.6% / 0.72명 / 3만 명 / 1.2%p / 5조 원 / 120건 ...
FIGURE_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|퍼센트|%p|포인트|배|명|만\s*명|천\s*명|가구|건|"
    r"호|억\s*원|조\s*원|만\s*원|달러|세|년|개월)"
)


def bucket(text):
    has_figure = bool(FIGURE_RE.search(text))
    hit_words = [w for w in INDICATOR_WORDS if w in text]
    if has_figure and hit_words:
        return "CHECKABLE", hit_words
    if has_figure:
        return "NUMERIC_ONLY", []
    if hit_words:
        return "INDICATOR_ONLY", hit_words
    return "NEITHER", []


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    per_domain = collections.defaultdict(collections.Counter)
    corpus = collections.Counter()
    indicator_counts = collections.Counter()
    samples = {"statistics": [], "other": []}
    last_id = 0
    print("STATS-CLAIM DEMAND PROBE — SELECT-only\n")
    with psycopg.connect(url) as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, (last_id,))
                rows = cur.fetchall()
            if not rows:
                break
            for rid, domain, title, claim in rows:
                last_id = max(last_id, rid)
                text = "%s %s" % (title or "", claim or "")
                kind, words = bucket(text)
                per_domain[domain][kind] += 1
                corpus[kind] += 1
                if kind == "CHECKABLE":
                    for w in words:
                        indicator_counts[w] += 1
                    key = "statistics" if domain == "statistics" else "other"
                    if len(samples[key]) < 6:
                        samples[key].append((rid, domain, (title or "")[:64]))

    total = sum(corpus.values())
    print("== corpus-wide buckets (n=%d) ==" % total)
    for kind, n in corpus.most_common():
        print("  %-15s %6d (%4.1f%%)" % (kind, n, 100.0 * n / total))

    print("\n== per-domain CHECKABLE counts (the addressable set) ==")
    for domain in sorted(per_domain,
                         key=lambda d: -per_domain[d]["CHECKABLE"]):
        c = per_domain[domain]
        dtotal = sum(c.values())
        print("  %-14s checkable %5d / %5d rows (%4.1f%%)"
              % (domain, c["CHECKABLE"], dtotal,
                 100.0 * c["CHECKABLE"] / dtotal if dtotal else 0))

    print("\n== top indicator words inside CHECKABLE rows ==")
    for word, n in indicator_counts.most_common(15):
        print("  %-12s %5d" % (word, n))

    print("\n== samples ==")
    for key, rows_ in samples.items():
        print("  [%s-domain checkable]" % key)
        for rid, domain, title in rows_:
            print("    id=%-6s %-12s %s" % (rid, domain, title))

    print("\n[Probe] SELECT-only; nothing written; no verdict column read. "
          "The CHECKABLE counts (esp. per headline indicator) are the honest "
          "addressable ceiling for any KOSIS figure source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
