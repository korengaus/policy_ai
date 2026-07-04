# DOMAIN-USABILITY-PROBE Phase 2 — per-domain category-readiness probe.
# SELECT-only, no writes, no network, safe to run in the Render Worker Shell.
#
# QUESTION THIS PROBE ANSWERS
# ---------------------------
# The DOMAIN-COVERAGE-PROBE showed our policy_briefing collection already carries
# releases from ~43 ministries (복지/노동/농림/산업/질병관리청/...). So "adding a
# category" LOOKS like a classify-existing-data problem, not a new-source problem.
# BUT "a ministry's releases arrive" != "that domain is category-ready." Before
# building any domain category we measure, per candidate domain, three things:
#   * VOLUME        — enough analysis ROWS actually about that domain?
#   * MATCH QUALITY — do that domain's official releases MATCH the news (lift
#                     confidence above the floor), or arrive-but-fail-to-match?
#   * CLASSIFIABILITY — can we even tell which rows belong to the domain?
# This probe MEASURES those so "which categories are ready" is data-driven.
#
# CLASSIFIABILITY — Phase-1 finding (READ THIS BEFORE TRUSTING ANY NUMBER)
# -----------------------------------------------------------------------
# There is NO stored cross-domain label. The `topic` column is only a ~9-value
# housing-finance sub-taxonomy (전세대출 규제 / DSR 규제 / 주택담보대출 규제 /
# 주거비 지원 / ... / 미분류) — every welfare/labor/agriculture/health story
# collapses to 미분류 (topic_classifier.py). So domain assignment HERE is ADVISORY
# INFERENCE, never an authoritative taxonomy, built from:
#   (a) keyword match of title+claim_text+query against per-domain keyword sets
#       (the only cross-domain text signal; multi-bucket, noisy) — the VOLUME
#       proxy; and
#   (b) the `publisher` ministry of an attached policy_briefing release
#       (source_candidates[].publisher) — a NOISY tag, because many rows are
#       floor rows whose attached doc never matched; it is only trustworthy when
#       paired with a usable confidence (pcs>=70). Used as the MINISTRY-FIT
#       match-quality signal, NOT as a volume label.
# That no clean label exists is itself a finding: a real classifier would have to
# be built, which RAISES the cost of "just classify existing data."
#
# WHAT IT TOUCHES
# ---------------
# Reads `analysis_results` (SELECT-only) via the same psycopg pattern as
# scripts/domain_coverage_probe.py. Modifies NO row, NO pipeline code, NO config,
# NO frontend. Issues NO INSERT/UPDATE/DELETE/DDL and makes NO network call.

import os
import json
import sys
import collections
from datetime import datetime, timedelta

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunable constants (commented, top-of-file).
# ---------------------------------------------------------------------------
# How many days back to scan. 0 = WHOLE CORPUS (default — readiness is about
# everything we have accumulated). When >0, windowed Python-side on
# created_at[:10] (YYYY-MM-DD), mirroring domain_coverage_probe.py.
LOOKBACK_DAYS = 0

# MEMORY BOUND (OBS-LIMIT). The SELECT below pulls the heavy source_candidates
# JSON column, so it must NEVER materialize the whole (growing) corpus — a full
# fetchall of ~838 rows x source_candidates OOM'd the 2GB Worker. Load only the
# newest MAX_ROWS rows (ORDER BY id DESC LIMIT). This bounds memory INDEPENDENTLY
# of corpus size — including a large backfill landing many recent rows — because
# the cap is a fixed row count, not a date range. The LOOKBACK_DAYS date filter
# still applies Python-side WITHIN this capped window; aggregation is order-
# independent (Counters) so the DESC order is harmless. Set MAX_ROWS=0 to disable
# the cap (whole corpus — only safe on a small table / big-memory host).
MAX_ROWS = 300

# Official candidate source_types that can carry a policy_briefing release
# (mirrors domain_coverage_probe.py / body2_overlap.py).
OFFICIAL_TYPES = ("official_government", "public_institution")

# Confidence floor/cap (mirrors observe_daily.py): cap = usable official match,
# floor = arrived-but-failed-to-match.
PCS_CAP = 70
PCS_FLOOR = 10

# Advisory verdict thresholds (operator-tunable). VOLUME_READY_MIN: rows needed
# for a domain to count as "high volume." MATCH_OK_MIN_CAP_RATIO: share of a
# domain's classified rows that must reach the cap to count as "decent match."
VOLUME_READY_MIN = 20
MATCH_OK_MIN_CAP_RATIO = 0.15

# Per-domain TEXT keyword sets (advisory VOLUME proxy). Case-insensitive
# substring match against title + claim_text + query. A row may match several
# domains. NOT a stored taxonomy — a rough hint. statistics is included
# specifically to test the "is 통계 genuinely absent?" question from text too.
DOMAIN_KEYWORDS = {
    "welfare (복지)":      ["복지", "지원금", "돌봄", "연금", "수당", "취약계층", "바우처"],
    "labor (노동)":        ["고용", "일자리", "실업", "임금", "근로", "노동"],
    "agriculture (농업)":  ["농업", "축산", "농가", "농림", "식품", "농산물"],
    "health (보건)":       ["의료", "질병", "백신", "병원", "건강", "감염병"],
    "environment (환경)":  ["환경", "탄소", "에너지", "기후", "온실가스", "재생에너지"],
    "finance (금융)":      ["금융", "대출", "가계부채", "DSR", "금리", "은행"],
    "SMB (소상공인)":      ["소상공인", "자영업", "중소기업", "새출발기금"],
    "statistics (통계)":   ["통계", "지표", "물가지수", "고용률", "실업률", "통계청"],
}

# Per-domain ministry hint substrings (MINISTRY-FIT match-quality signal). If a
# row's attached policy_briefing release has a publisher containing one of these,
# the domain's OWN official source is what was attached. Advisory.
DOMAIN_MINISTRY_HINTS = {
    "welfare (복지)":      ["복지부", "보건복지"],
    "labor (노동)":        ["고용노동", "고용부", "노동부"],
    "agriculture (농업)":  ["농림축산", "농림", "농촌진흥"],
    "health (보건)":       ["복지부", "보건복지", "질병관리", "식품의약", "식약처"],
    "environment (환경)":  ["환경부", "기후", "에너지"],
    "finance (금융)":      ["금융위", "금융감독", "금감원", "기획재정"],
    "SMB (소상공인)":      ["중소벤처", "중소기업"],
    "statistics (통계)":   ["통계청"],
}

# Statistics domain key (Section 4 special flag).
STATS_DOMAIN = "statistics (통계)"


def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed."""
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _pb_publishers(cands) -> set:
    """Distinct non-blank publishers of the attached policy_briefing releases on
    a row. PB releases identified by the STABLE policy_briefing_news_item_id
    marker (never overwritten by resolve/evaluate). Deduped per-row by news id."""
    pubs = set()
    seen_ids = set()
    for c in cands:
        if not isinstance(c, dict) or c.get("source_type") not in OFFICIAL_TYPES:
            continue
        pb_id = str(c.get("policy_briefing_news_item_id") or "").strip()
        if not pb_id or pb_id in seen_ids:
            continue
        seen_ids.add(pb_id)
        pub = str(c.get("publisher") or "").strip()
        if pub:
            pubs.add(pub)
    return pubs


def _classify_domains(text: str) -> list:
    """Advisory: list of domain labels whose keywords appear (case-insensitive
    substring) in text. A row may match several domains."""
    haystack = (text or "").lower()
    hits = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in haystack:
                hits.append(domain)
                break
    return hits


def _ministry_fits(publishers: set, domain: str) -> bool:
    """True if any attached-release publisher matches the domain's ministry
    hints (advisory)."""
    hints = DOMAIN_MINISTRY_HINTS.get(domain, ())
    return any(any(h in p for h in hints) for p in publishers)


def _row_date(created_at) -> str:
    """First 10 chars (YYYY-MM-DD) of a loose-TEXT created_at, or '' if unusable."""
    if created_at is None:
        return ""
    s = str(created_at)
    return s[:10] if len(s) >= 10 else ""


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    cutoff = ""
    if LOOKBACK_DAYS and LOOKBACK_DAYS > 0:
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    # SELECT-only. Pull the text + confidence + candidates; aggregate Python-side.
    # MEMORY BOUND: newest MAX_ROWS rows only (heavy source_candidates column) +
    # a cheap COUNT(*) for the corpus-total context line (no row materialization).
    rows = []
    corpus_total = 0
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM analysis_results")
        corpus_total = int((cur.fetchone() or [0])[0] or 0)
        if MAX_ROWS and MAX_ROWS > 0:
            cur.execute(
                "SELECT id, created_at, query, title, claim_text, "
                "policy_confidence_score, source_candidates "
                "FROM analysis_results ORDER BY id DESC LIMIT %s",
                (MAX_ROWS,),
            )
        else:
            cur.execute(
                "SELECT id, created_at, query, title, claim_text, "
                "policy_confidence_score, source_candidates "
                "FROM analysis_results ORDER BY id"
            )
        fetched = cur.fetchall()
        for rid, created_at, query, title, claim_text, pcs, sc in fetched:
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue
            rows.append((rid, query, title, claim_text, pcs, _j(sc) or []))
    n_loaded = len(fetched)

    print("DOMAIN-USABILITY-PROBE Phase 2 — per-domain category-readiness (READ-ONLY)")
    scope = ("newest %d rows" % MAX_ROWS) if (MAX_ROWS and MAX_ROWS > 0) else "whole corpus"
    if cutoff:
        scope += " AND created_at >= %s" % cutoff
    print("  scope: %s   (MAX_ROWS=%d, LOOKBACK_DAYS=%d)" % (scope, MAX_ROWS, LOOKBACK_DAYS))
    print("  window: %d rows loaded of %d corpus total (memory-bounded, independent of corpus size)"
          % (n_loaded, corpus_total))
    print("  ADVISORY: domain assignment is keyword/publisher INFERENCE, NOT a stored")
    print("            taxonomy. `topic` is only a housing-finance sub-taxonomy.")
    print()

    # ---- aggregation ------------------------------------------------------
    n_pcs_nonnull = 0
    n_rows_with_pb = 0
    # per-domain accumulators
    dom_rows = collections.Counter()        # rows classified to domain (volume)
    dom_cap = collections.Counter()         # ... with pcs >= cap
    dom_floor = collections.Counter()       # ... with pcs <= floor
    dom_minfit = collections.Counter()      # ... with a ministry-fit attached release
    dom_minfit_cap = collections.Counter()  # ... ministry-fit AND pcs >= cap
    n_unclassified = 0                       # rows matching NO domain
    any_stats_publisher = False             # any 통계청 publisher seen anywhere

    for rid, query, title, claim_text, pcs, cands in rows:
        if pcs is not None:
            n_pcs_nonnull += 1
        publishers = _pb_publishers(cands)
        if any(c.get("policy_briefing_news_item_id") for c in cands
               if isinstance(c, dict)):
            n_rows_with_pb += 1
        if any("통계청" in p for p in publishers):
            any_stats_publisher = True

        combined = "%s\n%s\n%s" % (title or "", claim_text or "", query or "")
        domains = _classify_domains(combined)
        if not domains:
            n_unclassified += 1
            continue

        try:
            pcs_val = int(pcs) if pcs is not None else None
        except (TypeError, ValueError):
            pcs_val = None

        for d in domains:
            dom_rows[d] += 1
            if pcs_val is not None and pcs_val >= PCS_CAP:
                dom_cap[d] += 1
            if pcs_val is not None and pcs_val <= PCS_FLOOR:
                dom_floor[d] += 1
            if _ministry_fits(publishers, d):
                dom_minfit[d] += 1
                if pcs_val is not None and pcs_val >= PCS_CAP:
                    dom_minfit_cap[d] += 1

    # ---- SECTION 1: CORPUS SIZE -------------------------------------------
    print("=== 1. CORPUS SIZE ===")
    print("  rows scanned (in scope)                     :", len(rows))
    print("  rows with non-null policy_confidence_score  :", n_pcs_nonnull)
    print("  rows carrying >=1 policy_briefing release   :", n_rows_with_pb)
    if not rows:
        print("\n  No rows in scope — nothing to aggregate.")
        print("\n[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
        return 0
    print()

    # ---- SECTION 2: PER-DOMAIN VOLUME (advisory) --------------------------
    print("=== 2. PER-DOMAIN VOLUME (advisory keyword match on title+claim+query) ===")
    print("    (rows may match multiple domains; counts sum to >= scanned rows.")
    print("     NOT a stored taxonomy — a rough hint, not a measurement.)")
    for d, _ in sorted(DOMAIN_KEYWORDS.items(), key=lambda kv: -dom_rows[kv[0]]):
        print("  %-22s rows=%d" % (d, dom_rows[d]))
    print("  %-22s rows=%d" % ("(none/미분류)", n_unclassified))
    print()

    # ---- SECTION 3: PER-DOMAIN MATCH QUALITY ------------------------------
    print("=== 3. PER-DOMAIN MATCH QUALITY (floor vs cap; ministry-fit) ===")
    print("    cap = pcs>=%d (official match lifted confidence); floor = pcs<=%d"
          % (PCS_CAP, PCS_FLOOR))
    print("    min-fit = row has an attached policy_briefing release whose publisher")
    print("    matches the domain's ministry (advisory); min-fit&cap = it also matched.")
    for d, _ in sorted(DOMAIN_KEYWORDS.items(), key=lambda kv: -dom_rows[kv[0]]):
        n = dom_rows[d]
        cap_ratio = (100.0 * dom_cap[d] / n) if n else 0.0
        print("  %-22s rows=%-4d cap>=%d=%-4d floor<=%d=%-4d  min-fit=%-4d min-fit&cap=%-3d  (cap %.0f%%)"
              % (d, n, PCS_CAP, dom_cap[d], PCS_FLOOR, dom_floor[d],
                 dom_minfit[d], dom_minfit_cap[d], cap_ratio))
    print()

    # ---- SECTION 4: CATEGORY-READINESS VERDICT (advisory) -----------------
    print("=== 4. CATEGORY-READINESS VERDICT (advisory) ===")
    print("    high volume (rows>=%d) + decent match (cap share>=%.0f%%) -> category-ready;"
          % (VOLUME_READY_MIN, 100 * MATCH_OK_MIN_CAP_RATIO))
    print("    high volume + mostly floor -> present but weak (needs match work);")
    print("    thin -> needs seeds/source.")
    for d, _ in sorted(DOMAIN_KEYWORDS.items(), key=lambda kv: -dom_rows[kv[0]]):
        n = dom_rows[d]
        cap_ratio = (dom_cap[d] / n) if n else 0.0
        if n < VOLUME_READY_MIN:
            verdict = "needs seeds/source (thin)"
        elif cap_ratio >= MATCH_OK_MIN_CAP_RATIO or dom_minfit_cap[d] > 0:
            verdict = "category-ready (classify)"
        else:
            verdict = "present but weak (needs match work)"
        print("  %-22s %s" % (d, verdict))
    print()

    # statistics-specific flag (absent in the coverage probe — confirm here).
    stats_rows = dom_rows[STATS_DOMAIN]
    print("  [statistics flag]")
    if stats_rows == 0 and not any_stats_publisher:
        print("    ABSENT — no statistics-keyword rows AND no 통계청 publisher attached.")
        print("    -> warrants a dedicated statistics press-release source search.")
    elif stats_rows > 0 and not any_stats_publisher:
        print("    TEXT-ONLY — %d statistics-keyword row(s) exist but NO 통계청 official" % stats_rows)
        print("    release is attached anywhere -> covered only in news text, no official")
        print("    statistics source feeding us. A dedicated source would still help.")
    else:
        print("    PRESENT — 통계청 releases are attached to our rows (%d stat-keyword rows;"
              % stats_rows)
        print("    통계청 publisher seen). Statistics may be classify-existing-data.")
    print()
    print("  (All domain assignment above is advisory keyword/publisher inference, NOT a")
    print("   stored taxonomy; a production category would need a real classifier.)")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
