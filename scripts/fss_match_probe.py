# FSS-MATCH-PROBE — THROWAWAY read-only probe of the FSS provider's floor impact.
# SELECT-only over analysis_results, no writes, no network, safe in the Worker Shell.
#
# QUESTION THIS PROBE ANSWERS (the crux: FSS's SOLE contribution)
# --------------------------------------------------------------
# The FSS provider (providers/fss_press_release.py) injects FSS press-release
# candidates via Lane-A, each carrying the STABLE marker fss_bodo_content_id
# (retrieval_method "fss_bodo_api"), written into analysis_results.source_candidates
# JSON alongside policy_briefing_news_item_id / national_law_mst.
#
# A live screen test ("금융위 가계대출") came off the floor (pcs 90) on the body
# "2026년 5월 가계대출 동향(잠정)", BUT the DISPLAY domain was korea.kr/fsc.go.kr —
# so the screen cannot prove whether the lift came from FSS or from policy_briefing
# (korea.kr), which may carry the same release. The screen is AMBIGUOUS.
#
# The ONLY unambiguous signal is the MARKER: a candidate carrying fss_bodo_content_id
# was injected by the FSS provider, period — regardless of how the display renders.
# This probe measures FSS's contribution by the MARKER, not the screen.
#
# HONESTY DISCIPLINE (enforced in output)
# ---------------------------------------
#   * marker present (fss_bodo_content_id)  = "FSS SUPPLIED a candidate to this row".
#   * official_body_match=True on an FSS-marked candidate = the resolve-computed
#     "FSS body ACTUALLY matched" signal (set downstream by resolve_official_evidence,
#     NOT by the provider). This is the closest read-only proxy to FSS-attributable
#     lift — reported as such, never conflated with the row-level pcs.
#   * row off-floor (pcs > 10) = a SEPARATE row-level outcome that may be driven by
#     FSS OR by another source (PB / law / crawl). Marker-on-an-off-floor-row is
#     correlational disambiguation, NOT proof FSS caused the lift.
#   * the CLEAN causal test is FSS-off vs FSS-on on the SAME row — named as the
#     standing caveat; this probe is the best read-only proxy.
#
# WHAT IT TOUCHES
# ---------------
# Reads `analysis_results` (SELECT-only) via the same psycopg pattern as
# scripts/body2_overlap.py / domain_usability_probe.py. Modifies NO row, NO code.
# Issues NO INSERT/UPDATE/DELETE/DDL and makes NO network call.

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
# How many days back to scan. 0 = WHOLE CORPUS (default). When >0, windowed
# Python-side on created_at[:10] (YYYY-MM-DD), mirroring domain_usability_probe.py.
LOOKBACK_DAYS = 0

# Floor threshold (mirrors observe_daily.py / the M37 floor convention): a row is
# ON the floor when pcs <= FLOOR; OFF the floor when pcs > FLOOR.
FLOOR = 10

# Stable primary-document marker keys (confirmed in the providers). Presence of a
# key on a candidate dict = that provider supplied the candidate.
FSS_MARKER = "fss_bodo_content_id"
PB_MARKER = "policy_briefing_news_item_id"
LAW_MARKER = "national_law_mst"

# How many sample off-floor FSS-marked rows to print.
SAMPLE_N = 5


def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed (-> [])."""
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _row_date(created_at) -> str:
    if created_at is None:
        return ""
    s = str(created_at)
    return s[:10] if len(s) >= 10 else ""


def _is_dict(c):
    return isinstance(c, dict)


def _fss_cands(cands):
    """FSS-marked candidate dicts on a row (marker = provider supplied it)."""
    return [c for c in cands if _is_dict(c) and FSS_MARKER in c]


def _body_len(c):
    try:
        return int(c.get("official_body_length") or 0)
    except (TypeError, ValueError):
        return 0


def _matched(c):
    """resolve-computed 'official body actually matched' signal on the candidate.
    Set downstream by resolve_official_evidence (NOT the provider)."""
    return bool(c.get("official_body_match"))


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

    rows = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, query, policy_confidence_score, source_candidates "
            "FROM analysis_results ORDER BY id"
        )
        for rid, created_at, query, pcs, sc in cur.fetchall():
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue
            rows.append((rid, query, pcs, _j(sc) or []))

    print("FSS-MATCH-PROBE — read-only FSS provider reach + off-floor correlation")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   (LOOKBACK_DAYS=%d; 0 = whole corpus)  floor threshold=%d"
          % (scope, LOOKBACK_DAYS, FLOOR))
    print("  marker: %s  (presence = FSS provider supplied a candidate to the row)" % FSS_MARKER)
    print()

    # ---- SECTION 1: CORPUS ------------------------------------------------
    print("=== 1. CORPUS ===")
    print("  rows scanned (in scope)                     :", len(rows))
    n_pcs = sum(1 for _, _, pcs, _ in rows if pcs is not None)
    print("  rows with policy_confidence_score present   :", n_pcs)
    if not rows:
        print("\n  No rows in scope — nothing to aggregate.")
        print("\n[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
        return 0
    print()

    # ---- aggregation ------------------------------------------------------
    fss_reach_rows = 0           # rows with >=1 FSS-marked candidate
    fss_body_rows = 0            # FSS-reached rows with >=1 FSS cand carrying a body
    fss_matched_rows = 0         # FSS-reached rows with >=1 FSS cand official_body_match=True
    n_fss_cands_total = 0
    n_fss_cands_body = 0
    n_fss_cands_matched = 0

    # off-floor marker disambiguation
    offfloor_total = 0
    cls = collections.Counter()  # FSS_ONLY / PB_PRESENT / LAW_PRESENT / FSS_PB_BOTH / NONE
    fss_offfloor_rows = []       # for the sample

    for rid, query, pcs, cands in rows:
        fcands = _fss_cands(cands)
        has_fss = bool(fcands)
        has_pb = any(_is_dict(c) and PB_MARKER in c for c in cands)
        has_law = any(_is_dict(c) and LAW_MARKER in c for c in cands)

        if has_fss:
            fss_reach_rows += 1
            n_fss_cands_total += len(fcands)
            body_cands = [c for c in fcands if _body_len(c) > 0]
            matched_cands = [c for c in fcands if _matched(c)]
            n_fss_cands_body += len(body_cands)
            n_fss_cands_matched += len(matched_cands)
            if body_cands:
                fss_body_rows += 1
            if matched_cands:
                fss_matched_rows += 1

        try:
            pcs_val = int(pcs) if pcs is not None else None
        except (TypeError, ValueError):
            pcs_val = None
        is_offfloor = pcs_val is not None and pcs_val > FLOOR
        if not is_offfloor:
            continue
        offfloor_total += 1
        if has_fss and has_pb:
            cls["FSS_PB_BOTH"] += 1
        elif has_fss and not has_pb and not has_law:
            cls["FSS_ONLY"] += 1
        elif has_pb:
            cls["PB_PRESENT"] += 1
        elif has_law:
            cls["LAW_PRESENT"] += 1
        else:
            cls["NONE"] += 1
        if has_fss:
            fss_offfloor_rows.append((rid, query, pcs_val, has_pb, has_law, fcands))

    pct = lambda n: (100.0 * n / len(rows)) if rows else 0.0

    # ---- SECTION 2: FSS PROVIDER REACH ------------------------------------
    print("=== 2. FSS PROVIDER REACH (rows the FSS provider injected into) ===")
    print("  rows with >=1 FSS-marked candidate          : %d  (%.1f%% of corpus)"
          % (fss_reach_rows, pct(fss_reach_rows)))
    print("  total FSS-marked candidates across those rows: %d" % n_fss_cands_total)
    if fss_reach_rows == 0:
        print("  >>> The FSS provider has NOT injected into any scanned row. Either")
        print("      FSS_ENABLED was off when these rows were analyzed, the windows")
        print("      returned nothing claim-relevant, or these rows predate the provider.")
    print()

    # ---- SECTION 3: FSS BODY PRESENCE & MATCH -----------------------------
    print("=== 3. FSS BODY PRESENCE & MATCH (among FSS-reached rows) ===")
    print("  PROVIDER-REACH signal (provider-set):")
    print("    FSS-reached rows with an FSS candidate carrying a body (len>0): %d"
          % fss_body_rows)
    print("    FSS-marked candidates carrying a body (len>0)                : %d of %d"
          % (n_fss_cands_body, n_fss_cands_total))
    print("  MATCH signal (resolve-computed downstream, NOT provider-set):")
    print("    FSS-reached rows with an FSS candidate official_body_match=True: %d"
          % fss_matched_rows)
    print("    FSS-marked candidates with official_body_match=True            : %d of %d"
          % (n_fss_cands_matched, n_fss_cands_total))
    print("  (NOTE: 'carries a body' = provider reach; 'official_body_match=True' is the")
    print("   resolve-computed proxy for FSS body actually matching — distinct from the")
    print("   row-level pcs, which any source could drive.)")
    print()

    # ---- SECTION 4: FSS-vs-OTHER ON OFF-FLOOR ROWS ------------------------
    print("=== 4. OFF-FLOOR ROWS by primary-doc marker (pcs > %d) ===" % FLOOR)
    print("  off-floor rows total                        :", offfloor_total)
    print("  FSS-only marker present (no PB, no law)      : %d   <- FSS-attributable (strongest)"
          % cls["FSS_ONLY"])
    print("  FSS + PB both present                        : %d   <- ambiguous (PB could be lifter)"
          % cls["FSS_PB_BOTH"])
    print("  PB present (regardless of FSS-absence)       : %d" % cls["PB_PRESENT"])
    print("  law present                                  : %d" % cls["LAW_PRESENT"])
    print("  no primary-doc marker                        : %d   (lifted by crawl lane / other)"
          % cls["NONE"])
    print("  CAVEAT: marker presence != proof of WHICH source the matcher used for the")
    print("  lift. This is correlational disambiguation, not a controlled A/B.")
    print()

    # ---- SECTION 5: SAMPLE ------------------------------------------------
    print("=== 5. SAMPLE off-floor rows carrying the FSS marker (eyeball) ===")
    if not fss_offfloor_rows:
        print("  (none — no off-floor row carries an FSS marker in scope)")
    else:
        for rid, query, pcs_val, has_pb, has_law, fcands in fss_offfloor_rows[:SAMPLE_N]:
            markers = ["FSS"]
            if has_pb:
                markers.append("PB")
            if has_law:
                markers.append("law")
            # pick the FSS candidate with the longest body for the eyeball line
            best = max(fcands, key=_body_len) if fcands else {}
            subj = str(best.get("title") or "")[:70]
            print("  row %s | pcs=%s | markers=%s | query=%s"
                  % (rid, pcs_val, "+".join(markers), str(query or "")[:40]))
            print("      FSS subject: %s" % (subj or "(none)"))
            print("      FSS body len=%d  official_body_match=%s"
                  % (_body_len(best), bool(best.get("official_body_match"))))
        if len(fss_offfloor_rows) > SAMPLE_N:
            print("  ... (+%d more off-floor FSS-marked rows)" % (len(fss_offfloor_rows) - SAMPLE_N))
    print()

    # ---- SECTION 6: HONEST VERDICT LINE -----------------------------------
    print("=== 6. HONEST VERDICT ===")
    print("  FSS provider injected into %d row(s); %d off-floor row(s) carry the FSS marker;"
          % (fss_reach_rows, cls["FSS_ONLY"] + cls["FSS_PB_BOTH"]))
    print("  of those, %d have FSS as the ONLY primary-doc marker (FSS-attributable) vs"
          % cls["FSS_ONLY"])
    print("  %d that ALSO carry PB (ambiguous — PB could be the lifter)." % cls["FSS_PB_BOTH"])
    print("  Separately, %d FSS-marked candidate(s) carry official_body_match=True (the"
          % n_fss_cands_matched)
    print("  resolve-computed proxy for FSS body actually matching).")
    print("  CAVEAT: marker presence = 'FSS supplied a candidate'; off-floor = a separate")
    print("  row-level outcome (any source). The clean causal test is FSS-OFF vs FSS-ON on")
    print("  the SAME row; marker-correlation here is the best read-only proxy.")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
