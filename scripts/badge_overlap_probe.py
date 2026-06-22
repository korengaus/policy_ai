"""BADGE-MERGE Phase 1 — READ-ONLY, SELECT-only probe.

Measures the real domain x topic co-occurrence behind the two homepage card
badges so the Phase 2 MERGE rule can be designed with no gaps.

Run from repo root:

    PYTHONPATH=. python scripts/badge_overlap_probe.py

SCOPE / SAFETY
--------------
- SELECT-only: the single DB access is ``sa.select(analysis_results_table)``.
  There is NO insert/update/delete/DDL anywhere in this file.
- pin-OUT: lives under scripts/, adds zero ``log.*`` sites to pinned modules
  (331/16 unaffected). It only ``print``s to stdout.
- Reads values for DISPLAY measurement only — no verdict path, no backend
  mutation, no enum/key rename. The English ``domain`` enum keys and stored
  ``topic`` string are read as-is; Korean labels are derived at print time.

WHAT IT REPLICATES
------------------
The two badges rendered side by side in ``renderTopicCardHtml`` are:

  (2) domain badge  = domainDisplayLabel(cardDomainKey(card))   (main.js ~258-263)
                      -> DOMAIN_LABELS_KO over the stored ``domain`` enum.
  (3) topic  badge  = card.topic = exportTopicLabel(result, query) (main.js ~4644)
                      -> a JS regex cascade over the article text, NOT a raw
                         stored column. We port that cascade to Python below.

IMPORTANT APPROXIMATION
-----------------------
exportTopicLabel reads [query, title, summary, final_decision.decision_summary,
verification_card.claim_text, topic]. Of these, ``summary`` and
``decision_summary`` are NOT first-class columns in analysis_results. This probe
substitutes the stored ``evidence_summary`` column as the best available proxy
for ``summary`` (and leaves ``decision_summary`` empty). The primary signal
fields (query, title, claim_text, topic) are exact. Classifications for rows
whose label hinges only on summary/decision_summary text may differ slightly
from the live badge; treat the cross-tab as a faithful-but-approximate map.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import sqlalchemy as sa

from postgres_storage import get_engine, analysis_results_table


# ---------------------------------------------------------------------------
# Ported label maps / functions (kept in lockstep with frontend/scripts/main.js)
# ---------------------------------------------------------------------------

# main.js:239-250 DOMAIN_LABELS_KO (display only; never a comparison key).
DOMAIN_LABELS_KO = {
    "finance": "금융",
    "welfare": "복지",
    "agriculture": "농업",
    "labor": "노동",
    "health": "보건",
    "environment": "환경",
    "SMB": "소상공인",
    "realestate": "부동산",
    "statistics": "통계",
    "기타-미분류": "기타",
}

# Displayed topic labels that carry no real category signal. exportTopicLabel
# never emits "미분류"; its meaningless fallbacks are "확인 필요" / "자료 부족"
# (and empty). We treat all of these (plus None) as MEANINGLESS for the merge.
MEANINGLESS_TOPIC_LABELS = {"확인 필요", "자료 부족", "미분류", ""}


def card_domain_key(domain):
    """main.js:258-261 cardDomainKey — empty/None domain -> 기타-미분류 bucket."""
    if isinstance(domain, str) and domain:
        return domain
    return "기타-미분류"


def domain_display_label(domain_key):
    """main.js:262-263 domainDisplayLabel — fallback to the 기타 label."""
    return DOMAIN_LABELS_KO.get(domain_key, DOMAIN_LABELS_KO["기타-미분류"])


def export_topic_label(query, title, summary, decision_summary, claim_text, topic):
    """Faithful port of exportTopicLabel (main.js:4644-4670).

    NOTE: ``summary`` is fed the stored ``evidence_summary`` proxy and
    ``decision_summary`` is empty (those JS inputs have no dedicated column).
    """
    primary_parts = [p for p in (query, title, summary, decision_summary, claim_text) if p]
    primary_text = " ".join(primary_parts)
    topic_text = (topic or "").strip()  # sanitizePublicExportText is display cleanup; strip suffices here
    all_parts = [p for p in (primary_text, topic_text) if p]
    all_text = " ".join(all_parts)

    primary_has_real_estate = bool(re.search(
        r"부동산|양도세|양도소득세|다주택|주택|분양|재건축|재개발|청약|토지|임대|전세사기|종부세|공시가격|세무조사",
        primary_text,
    ))
    primary_has_jeonse_loan = bool(re.search(
        r"전세대출|버팀목|전세자금|주담대|주택담보대출", primary_text,
    ))

    if re.search(r"전세사기", all_text):
        return "전세사기"
    if re.search(r"부동산", query or ""):
        return "부동산"
    if primary_has_real_estate and not primary_has_jeonse_loan:
        return "부동산"
    if re.search(r"전세대출|전세자금|버팀목", primary_text):
        return "전세대출"
    if re.search(r"금융위|금감원|금리|은행|대출|DSR|가계부채|연체율|한국은행", primary_text):
        return "금융/정책"
    if primary_has_real_estate:
        return "부동산"
    if re.search(r"전세대출", topic_text) and not primary_has_jeonse_loan:
        return "확인 필요"
    if re.search(r"부동산", topic_text):
        return "부동산"
    if re.search(r"금융|정책|금리|은행|대출", topic_text):
        return "금융/정책"
    return topic_text if (topic_text and topic_text != "자료 부족") else "확인 필요"


# ---------------------------------------------------------------------------
# Advisory classification of a (domainKo, topicKo) pair.
# Precedence: IDENTICAL -> MEANINGLESS -> CONTAINS -> DIFFERENT.
#   MEANINGLESS is checked BEFORE CONTAINS on purpose: an empty/placeholder
#   topic would otherwise be a trivial substring of the domain label and get
#   mis-bucketed as CONTAINS.
# ---------------------------------------------------------------------------

def classify_pair(domain_ko, topic_ko):
    if domain_ko == topic_ko:
        return "IDENTICAL"
    if topic_ko in MEANINGLESS_TOPIC_LABELS:
        return "MEANINGLESS"
    # Both non-empty and unequal here. CONTAINS = one is a substring of the other.
    if domain_ko and topic_ko and (domain_ko in topic_ko or topic_ko in domain_ko):
        return "CONTAINS"
    return "DIFFERENT"


def more_specific(domain_ko, topic_ko):
    """For a CONTAINS pair, the longer (superset) label is the more-specific one."""
    return topic_ko if len(topic_ko) >= len(domain_ko) else domain_ko


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def main():
    engine = get_engine()
    if engine is None:
        print("ERROR: get_engine() returned None — no DB configured. Aborting (read-only).")
        return

    t = analysis_results_table
    # SELECT-only. Only the columns the two badges need.
    stmt = (
        sa.select(
            t.c.id,
            t.c.query,
            t.c.title,
            t.c.topic,
            t.c.claim_text,
            t.c.evidence_summary,
            t.c.domain,
        )
        .where(t.c.domain.isnot(None))
        .order_by(t.c.id.desc())
    )

    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(stmt).all()]

    total = len(rows)
    if not total:
        print("No analysis_results rows with non-null domain. Nothing to measure.")
        return

    domain_raw_counts = Counter()
    topic_ko_counts = Counter()
    crosstab = Counter()            # (domainKo, topicKo) -> count
    bucket_counts = Counter()       # bucket -> row count
    contains_pairs = {}             # (domainKo, topicKo) -> more_specific
    different_samples = []          # up to 12 (domainKo, topicKo, title)

    for row in rows:
        domain_raw = row.get("domain")
        domain_ko = domain_display_label(card_domain_key(domain_raw))
        topic_ko = export_topic_label(
            query=row.get("query"),
            title=row.get("title"),
            summary=row.get("evidence_summary"),   # proxy for JS result.summary
            decision_summary="",                     # no stored column
            claim_text=row.get("claim_text"),
            topic=row.get("topic"),
        )

        domain_raw_counts[domain_raw if domain_raw else "(empty)"] += 1
        topic_ko_counts[topic_ko] += 1
        crosstab[(domain_ko, topic_ko)] += 1

        bucket = classify_pair(domain_ko, topic_ko)
        bucket_counts[bucket] += 1
        if bucket == "CONTAINS":
            contains_pairs[(domain_ko, topic_ko)] = more_specific(domain_ko, topic_ko)
        elif bucket == "DIFFERENT" and len(different_samples) < 12:
            different_samples.append((domain_ko, topic_ko, (row.get("title") or "")[:80]))

    # ---- report ----
    print("=" * 72)
    print("BADGE-MERGE Phase 1 — domain x topic co-occurrence (READ-ONLY)")
    print("=" * 72)
    print(f"Total rows with non-null domain: {total}")
    print()

    print("-- Distinct domain enum values (raw) --")
    for value, count in domain_raw_counts.most_common():
        ko = domain_display_label(card_domain_key(None if value == "(empty)" else value))
        print(f"  {value:<16} -> {ko:<8} {count:>6}")
    print()

    print("-- Distinct displayed topic labels (Korean) --")
    for value, count in topic_ko_counts.most_common():
        print(f"  {value:<14} {count:>6}")
    print()

    print("-- Full cross-tab (domainKo, topicKo) -> count, classification --")
    for (domain_ko, topic_ko), count in crosstab.most_common():
        bucket = classify_pair(domain_ko, topic_ko)
        pct = 100.0 * count / total
        print(f"  {domain_ko:<8} x {topic_ko:<14} {count:>6} ({pct:4.1f}%)  [{bucket}]")
    print()

    print("-- Bucket summary (% of rows) --")
    for bucket in ("IDENTICAL", "CONTAINS", "MEANINGLESS", "DIFFERENT"):
        count = bucket_counts.get(bucket, 0)
        pct = 100.0 * count / total
        print(f"  {bucket:<12} {count:>6} ({pct:4.1f}%)")
    print()

    print("-- COMPLETE distinct CONTAINS pairs (more-specific = render this) --")
    if not contains_pairs:
        print("  (none)")
    else:
        for (domain_ko, topic_ko), specific in sorted(contains_pairs.items()):
            print(f"  domain={domain_ko:<8} topic={topic_ko:<14} -> render '{specific}'")
    print()

    print("-- DIFFERENT samples (judge: show-both vs topic-is-noise) --")
    if not different_samples:
        print("  (none)")
    else:
        for domain_ko, topic_ko, title in different_samples:
            print(f"  domain={domain_ko:<8} topic={topic_ko:<14} | {title}")
    print()
    print("Done. (read-only; no rows written)")


if __name__ == "__main__":
    main()
