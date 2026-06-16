# DOMAIN-COVERAGE-PROBE Phase 2 — source-ministry distribution probe.
# SELECT-only, no writes, no network, safe to run in the Render Worker Shell.
#
# QUESTION THIS PROBE ANSWERS
# ---------------------------
# Our verification text-matches a news claim against an OFFICIAL DOCUMENT BODY.
# The main official-document lane is 정책브리핑 (policy_briefing, data.go.kr org
# 1371000), an AGGREGATED feed of press releases from ALL central ministries that
# all share the SINGLE korea.kr URL domain. We are planning multi-domain expansion
# (statistics/welfare/labor/legal/...). KEY UNKNOWN: does our already-collected
# policy_briefing data ALREADY pull releases from MANY ministries (통계청/국토부/
# 복지부/고용부/...), or only a FEW?
#   * MANY ministries already flow in -> a new "category" is mostly a
#     DISPLAY/classification problem (data already here) -> cheap.
#   * Only a FEW -> under-covered domains need their own source added (like the
#     FSS press-release API) -> more work per domain.
# This probe MEASURES that from what we ALREADY store, so the multi-domain
# strategy is decided by data, not assumption (the "measure before surgery"
# principle).
#
# WHERE THE MINISTRY LIVES (Phase-1 finding)
# ------------------------------------------
# analysis_results.source_candidates is a JSON TEXT column. Each official
# candidate dict carries (providers/policy_briefing.py):
#   * publisher                    = the issuing ministry NAME in plain Korean
#                                    (from <NewsItem> MinisterCode -> ministry ->
#                                    candidate publisher; :230,:598). This is the
#                                    crux: korea.kr is ONE domain but publisher
#                                    tells 통계청 vs 국토부 vs 복지부 apart.
#   * policy_briefing_news_item_id = the STABLE per-release marker (never
#                                    overwritten by resolve/evaluate; the reason
#                                    body2_overlap.py keys PB detection on it
#                                    rather than retrieval_method/publisher).
#   * official_detail_url / url    = the canonical korea.kr URL (domain-level
#                                    only; NOT ministry-distinguishing).
# So we count PB releases by the STABLE id marker, dedup (the injector emits one
# candidate per claim_index x release, so the same release repeats within a row),
# then bucket the deduped releases by publisher. PB releases whose publisher is
# blank are reported as an explicit "(ministry unlabeled)" line — never guessed.
#
# WHAT IT TOUCHES
# ---------------
# Reads `analysis_results` (SELECT-only) via the same psycopg connection pattern
# as scripts/body2_overlap.py. Modifies NO row, NO pipeline code, NO config, NO
# frontend. Issues NO INSERT/UPDATE/DELETE/DDL and makes NO network call.

import os
import json
import sys
import collections
from datetime import datetime, timedelta
from urllib.parse import urlparse

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunable constants (commented, top-of-file).
# ---------------------------------------------------------------------------
# How many days back to scan. 0 = WHOLE CORPUS (the default — the strategic
# question is about everything we have accumulated). When >0, the window is
# applied Python-side on the first 10 chars (YYYY-MM-DD) of created_at, mirroring
# scripts/selfdb_keyword_probe.py.
LOOKBACK_DAYS = 0

# Official candidate source_types (mirrors body2_overlap.py).
OFFICIAL_TYPES = ("official_government", "public_institution")

# policy_briefing original_url domain — the single korea.kr lane that aggregates
# all central ministries. The per-ministry signal is publisher, NOT this domain.
PB_LANE_DOMAIN = "korea.kr"

# Section 4 interpretation map: target expansion domain -> substrings that, if
# present in a ministry publisher name, mark that domain as already represented.
# Advisory only (keyword match on the stored publisher string).
DOMAIN_MINISTRY_HINTS = (
    ("finance (금융)",      ("금융위", "금융감독", "금감원")),
    ("statistics (통계)",   ("통계청",)),
    ("welfare (복지)",      ("복지부", "보건복지")),
    ("labor (고용)",        ("고용노동", "고용부", "노동부")),
    ("land/transport (국토)", ("국토교통", "국토부")),
    ("tax (세제)",          ("기획재정", "국세청")),
    ("legal (법무/법제)",   ("법무부", "법제처")),
    ("central bank (통화)", ("한국은행", "한은")),
)


def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed (body2_overlap.py)."""
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _dom(u):
    """Lowercased netloc of a URL with the www. prefix stripped (body2_overlap.py)."""
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "") or "(none)"
    except Exception:
        return "(none)"


def _cand_dom(c):
    """Best-effort institution domain of a candidate (body2_overlap.py order)."""
    u = (c.get("official_detail_url") or c.get("official_body_url")
         or c.get("url") or c.get("official_search_url") or "")
    return _dom(u)


def _pb_news_id(c):
    """The STABLE policy_briefing per-release id, or '' if this is not a PB
    candidate. Keyed on policy_briefing_news_item_id (never overwritten by
    resolve/evaluate) — the same stable marker body2_overlap.py relies on."""
    return str(c.get("policy_briefing_news_item_id") or "").strip()


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

    # SELECT-only. Pull id + created_at + the source_candidates JSON; everything
    # is aggregated Python-side.
    rows = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, source_candidates "
            "FROM analysis_results ORDER BY id"
        )
        for rid, created_at, sc in cur.fetchall():
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue  # outside the lookback window
            rows.append((rid, day, _j(sc) or []))

    print("DOMAIN-COVERAGE-PROBE Phase 2 — source-ministry distribution (READ-ONLY)")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   (LOOKBACK_DAYS=%d; 0 = whole corpus)" % (scope, LOOKBACK_DAYS))
    print()

    # ---- aggregation ------------------------------------------------------
    rows_with_official = 0
    domain_rows = collections.defaultdict(set)        # domain -> distinct row ids
    domain_releases = collections.defaultdict(set)    # domain -> distinct (PB) ids
    domain_cand_count = collections.Counter()         # domain -> raw candidate count

    pb_rows = set()                                   # rows carrying >=1 PB release
    global_pb_ids = set()                             # distinct PB news_item_ids
    ministry_rows = collections.defaultdict(set)      # publisher -> distinct row ids
    ministry_releases = collections.defaultdict(set)  # publisher -> distinct PB ids
    UNLABELED = "(ministry unlabeled)"

    for rid, _day, cands in rows:
        offs = [c for c in cands
                if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
        if not offs:
            continue
        rows_with_official += 1

        # Per-row PB dedup set so a row with N claims (N copies of one release)
        # counts each release once for this row.
        row_pb_ids = set()
        for c in offs:
            d = _cand_dom(c)
            domain_cand_count[d] += 1
            domain_rows[d].add(rid)

            pb_id = _pb_news_id(c)
            if pb_id:
                domain_releases[d].add(pb_id)
                row_pb_ids.add(pb_id)
                if pb_id not in global_pb_ids:
                    # First time we see this release globally -> attribute its
                    # ministry once. publisher is read from this candidate; if a
                    # later copy disagreed we still keep the first-seen label
                    # (releases are 1:1 with a ministry).
                    publisher = str(c.get("publisher") or "").strip() or UNLABELED
                    ministry_releases[publisher].add(pb_id)
                global_pb_ids.add(pb_id)
                # row attribution: bucket this row under the release's ministry.
                publisher = str(c.get("publisher") or "").strip() or UNLABELED
                ministry_rows[publisher].add(rid)

        if row_pb_ids:
            pb_rows.add(rid)

    # ---- SECTION 1: CORPUS SIZE -------------------------------------------
    print("=== 1. CORPUS SIZE ===")
    print("  rows scanned (in scope)                     :", len(rows))
    print("  rows with >=1 official candidate            :", rows_with_official)
    print("  distinct policy_briefing releases (by id)   :", len(global_pb_ids))
    print("  rows carrying >=1 policy_briefing release   :", len(pb_rows))
    if not rows:
        print("\n  No rows in scope — nothing to aggregate.")
        print("\n[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
        return 0
    print()

    # ---- SECTION 2: INSTITUTION / DOMAIN DISTRIBUTION ---------------------
    print("=== 2. INSTITUTION / DOMAIN DISTRIBUTION (official candidates) ===")
    print("    (bucket = URL netloc; korea.kr is the policy_briefing aggregator lane —")
    print("     one domain, MANY ministries; per-ministry split is Section 3.)")
    if not domain_rows:
        print("  (no official candidate carried a usable URL)")
    for d, rset in sorted(domain_rows.items(), key=lambda kv: -len(kv[1])):
        rel = len(domain_releases.get(d, ()))
        rel_note = ("  releases=%d" % rel) if rel else ""
        lane = "  <- PB lane" if d == PB_LANE_DOMAIN else ""
        print("  %-16s rows=%-4d cands=%-5d%s%s"
              % (d, len(rset), domain_cand_count[d], rel_note, lane))
    print()

    # ---- SECTION 3: PER-MINISTRY BREAKDOWN (the key one) ------------------
    print("=== 3. PER-MINISTRY BREAKDOWN (within policy_briefing releases) ===")
    print("    (PB releases identified by the STABLE policy_briefing_news_item_id;")
    print("     deduped per-row and globally; bucketed by stored `publisher`.)")
    if not global_pb_ids:
        print("  No policy_briefing releases found in scope.")
        print("  >>> Cannot assess per-ministry coverage from stored data — either no PB")
        print("      candidates were persisted, or the stable id marker is absent. This is")
        print("      itself a finding: ministry would have to be re-derived from text.")
    else:
        ranked = sorted(
            ministry_releases.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        labeled = [(m, ids) for m, ids in ranked if m != UNLABELED]
        unlabeled_ids = ministry_releases.get(UNLABELED, set())
        n_distinct_min = len(labeled)
        print("  distinct ministries with a label            :", n_distinct_min)
        print("  releases / rows per ministry (release-deduped):")
        for m, ids in labeled:
            print("    %-14s releases=%-4d rows=%-4d"
                  % (m, len(ids), len(ministry_rows.get(m, ()))))
        if unlabeled_ids:
            print("    %-14s releases=%-4d rows=%-4d"
                  % (UNLABELED, len(unlabeled_ids), len(ministry_rows.get(UNLABELED, ()))))
            pct = 100.0 * len(unlabeled_ids) / len(global_pb_ids)
            print("  NOTE: %d of %d PB releases (%.0f%%) carry a BLANK publisher. publisher is"
                  % (len(unlabeled_ids), len(global_pb_ids), pct))
            print("        set at injection and MAY be dropped/overwritten downstream; these")
            print("        are reported honestly as unlabeled, not guessed.")
        else:
            print("  (every PB release carried a non-blank publisher — clean ministry labels.)")
    print()

    # ---- SECTION 4: INTERPRETATION HOOKS ---------------------------------
    print("=== 4. INTERPRETATION HOOKS (advisory — represented vs absent) ===")
    print("    A target expansion domain is 'represented' when a stored publisher name")
    print("    contains its ministry keyword. Represented + high volume -> a new category")
    print("    is mostly 'classify data we already collect'. Absent/thin -> 'add a source'")
    print("    (like the FSS press-release API).")
    labels_present = {m for m in ministry_releases if m != UNLABELED}
    for domain_label, hints in DOMAIN_MINISTRY_HINTS:
        hit_releases = 0
        hit_names = []
        for m in labels_present:
            if any(h in m for h in hints):
                hit_releases += len(ministry_releases.get(m, ()))
                hit_names.append(m)
        if hit_names:
            print("  %-22s REPRESENTED  releases=%-4d  (%s)"
                  % (domain_label, hit_releases, ", ".join(sorted(hit_names))))
        else:
            print("  %-22s ABSENT       (no stored publisher matches — likely needs its own source)"
                  % domain_label)
    print()
    print("  READ: count the REPRESENTED domains with real release volume — those are")
    print("  'classify existing data' (cheap). ABSENT/thin ones need a dedicated source.")
    print("  (Advisory keyword match on stored publisher; not an authoritative taxonomy.)")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
