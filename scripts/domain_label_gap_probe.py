# DOMAIN-LABEL Phase 1 — READ-ONLY distribution probe (pin-OUT, SELECT-only).
#
# QUESTION: what is the current domain distribution, and which NEW labels are
# hiding inside 기타-미분류 (sized by title-keyword clusters) — so the next
# label (education is the hypothesis) is chosen from data, not assumption.
#
# Joe runs once in the Render Worker Shell:
#     PYTHONPATH=. python scripts/domain_label_gap_probe.py
#
# WHAT IT PRINTS:
#   1. domain counts over the whole corpus (incl. NULL = pre-classifier rows).
#   2. Inside domain='기타-미분류': candidate NEW-label keyword clusters with
#      counts + one sample title each (education / transport / culture-tourism
#      / science-ICT / diplomacy-security). Also EXISTING-label keyword hits
#      inside 미분류 (rows the LLM fallback swallowed on API failure — they
#      would be recovered by a re-classify pass, no new label needed).
#   3. The unmatched remainder (genuinely uncategorizable size).
#
# SAFETY: SELECT-only (id/title/domain only; no verdict column, no debug blob).
# Keyset pagination. Never prints DATABASE_URL. pin-OUT scripts/*.

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

COUNTS_SQL = (
    "SELECT COALESCE(domain, '(NULL)') AS d, COUNT(*) "
    "FROM analysis_results GROUP BY 1 ORDER BY 2 DESC"
)
MISC_TITLES_SQL = (
    "SELECT id, title FROM analysis_results "
    "WHERE domain = '기타-미분류' AND id > %s ORDER BY id LIMIT 1000"
)

# Candidate NEW labels (policy areas with official-doc coverage; defamation/
# copyright-safe). Keyword sets are sizing heuristics only — the real label
# assignment stays the LLM classifier.
NEW_LABEL_KEYWORDS = {
    "education": ("교육", "대입", "수능", "입시", "학교", "교육청", "대학",
                  "등록금", "교사", "학생", "유치원", "돌봄학교", "학기"),
    "transport": ("교통", "철도", "지하철", "버스", "도로", "항공", "공항",
                  "택시", "화물", "KTX"),
    "culture_tourism": ("문화", "관광", "체육", "콘텐츠", "공연", "축제",
                        "박물관", "스포츠"),
    "science_ict": ("과학기술", "과기", "통신", "인공지능", "AI", "데이터",
                    "디지털", "반도체", "우주", "R&D"),
    "diplomacy_security": ("외교", "북한", "통일", "국방", "병역", "안보",
                           "한미", "방위"),
}
# EXISTING labels' keywords — hits inside 미분류 are fallback-swallowed rows
# (API failure / ambiguity), recoverable by a re-classify pass w/o new labels.
EXISTING_LABEL_KEYWORDS = {
    "finance?": ("금리", "대출", "가계부채", "세제", "은행", "금융"),
    "realestate?": ("부동산", "주택", "전세", "임대", "분양"),
    "welfare?": ("복지", "지원금", "연금", "수당", "돌봄"),
    "labor?": ("고용", "일자리", "실업", "임금", "근로", "노동"),
    "health?": ("의료", "질병", "백신", "병원", "감염"),
    "environment?": ("환경", "탄소", "에너지", "기후", "온실가스"),
    "agriculture?": ("농업", "농가", "농산물", "축산"),
    "SMB?": ("소상공인", "자영업", "중소기업"),
}


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("DOMAIN-LABEL GAP PROBE — SELECT-only\n")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(COUNTS_SQL)
            counts = cur.fetchall()
        total = sum(c for _, c in counts)
        print("== 1. domain distribution (total %d) ==" % total)
        for domain, count in counts:
            print("  %-14s %6d (%4.1f%%)" % (domain, count, 100.0 * count / total))

        print("\n== 2. keyword clusters inside 기타-미분류 ==")
        new_hits = {k: [0, ""] for k in NEW_LABEL_KEYWORDS}
        old_hits = {k: 0 for k in EXISTING_LABEL_KEYWORDS}
        misc_total = 0
        unmatched = 0
        unmatched_samples = []
        last_id = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(MISC_TITLES_SQL, (last_id,))
                rows = cur.fetchall()
            if not rows:
                break
            for rid, title in rows:
                last_id = max(last_id, rid)
                misc_total += 1
                text = title or ""
                matched = False
                for label, words in NEW_LABEL_KEYWORDS.items():
                    if any(w in text for w in words):
                        new_hits[label][0] += 1
                        if not new_hits[label][1]:
                            new_hits[label][1] = text
                        matched = True
                        break  # first cluster wins — sizing, not taxonomy
                if not matched:
                    for label, words in EXISTING_LABEL_KEYWORDS.items():
                        if any(w in text for w in words):
                            old_hits[label] += 1
                            matched = True
                            break
                if not matched:
                    unmatched += 1
                    if len(unmatched_samples) < 8:
                        unmatched_samples.append(text)

        print("  미분류 rows total: %d" % misc_total)
        print("\n  -- NEW-label candidates --")
        for label, (count, sample) in sorted(new_hits.items(),
                                             key=lambda x: -x[1][0]):
            print("  %-20s %5d   e.g. %.55s" % (label, count, sample))
        print("\n  -- EXISTING-label keywords found in 미분류 (fallback-"
              "swallowed; re-classify recovers, no new label) --")
        for label, count in sorted(old_hits.items(), key=lambda x: -x[1]):
            print("  %-20s %5d" % (label, count))
        print("\n== 3. unmatched remainder (genuinely uncategorizable?) ==")
        print("  %d rows (%.1f%% of 미분류)"
              % (unmatched, 100.0 * unmatched / misc_total if misc_total else 0))
        for sample in unmatched_samples:
            print("    %.70s" % sample)

    print("\n[Probe] SELECT-only; id/title/domain read only; nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
