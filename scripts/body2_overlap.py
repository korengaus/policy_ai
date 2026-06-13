# BODY-2 incremental-value probe — SELECT-only, no writes, no network, safe in Worker Shell.
# Question: of the 71 B-floor rows (pcs<=10, official candidate present, NO official body), how
# many would a STATIC board-crawl ACTUALLY add that policy_briefing(1371000, the korea.kr lane)
# doesn't already reach? We do NOT want to build 7 redundant board crawlers when the genuine new
# supply may be just fss (not in 1371000) + a couple others.
#
# IMPORTANT body-floor interaction (stated, not hidden): a policy_briefing candidate carries
# official_body_length = len(body) (providers/policy_briefing.py:604). When that body is
# non-empty it has_body=True and would lift the row OUT of the no-body B-floor. So a B-floor row
# can only carry a PB candidate whose body is EMPTY (length 0). This probe therefore reports PB
# presence AND splits PB candidates by empty/non-empty body, so "PB already reached this row" is
# interpreted correctly: PB reached it but supplied no usable body (a recall/matching gap), which
# is a DIFFERENT track from board-crawl supply.
import os, json, collections
from urllib.parse import urlparse
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")

# From the body2_static_probe run:
STATIC_ALT = {"fss.or.kr", "molit.go.kr", "fsc.go.kr", "mss.go.kr", "moj.go.kr", "ftc.go.kr", "bok.or.kr"}
NO_STATIC = {"moef.go.kr", "police.go.kr", "khug.or.kr", "nts.go.kr"}
# fss/bok are NOT central ministries -> NOT in policy_briefing(1371000) -> the clearest new supply.
NON_1371000 = {"fss.or.kr", "bok.or.kr", "khug.or.kr"}
PB_LANE = {"korea.kr"}            # policy_briefing original_url domain (path 2)
API_LANE = {"law.go.kr"}          # national_law (path 2)


def J(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def dom(u):
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "") or "(none)"
    except Exception:
        return "(none)"


def has_body(c):
    try:
        return int(c.get("official_body_length") or 0) > 0
    except Exception:
        return False


def cand_dom(c):
    u = (c.get("official_detail_url") or c.get("official_body_url")
         or c.get("url") or c.get("official_search_url") or "")
    return dom(u)


def is_pb(c):
    # retrieval_method is the primary marker; policy_briefing_news_item_id is the STABLE marker
    # (never overwritten by resolve/evaluate) — include it so a resolve-touched PB candidate is
    # still recognized. Reported both strict and inclusive below.
    return (c.get("retrieval_method") == "policy_briefing_api") or ("policy_briefing_news_item_id" in c)


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, source_candidates FROM analysis_results ORDER BY id")
    for rid, pcs, sc in cur.fetchall():
        rows.append((rid, pcs, J(sc) or []))

n_bfloor = 0
n_pb_strict = 0           # rows with >=1 candidate whose retrieval_method == policy_briefing_api
n_pb_inclusive = 0        # rows with >=1 PB candidate by retrieval_method OR stable marker
n_pb_with_body = 0        # B-floor rows carrying a PB candidate that DOES have a body (should be ~0)
n_zero_pb = 0
cls_counts = collections.Counter()      # PB_ALREADY_PRESENT / NON_PB_STATIC / NON_PB_NO_STATIC
zero_pb_domain_rows = collections.defaultdict(set)   # domain -> distinct zero-PB row ids
fss_rows = set()                        # B-floor rows where fss is a candidate
fss_rows_non_pb = set()                 # ... and the row has NO PB candidate (genuinely new)
non_pb_static_rows = set()
non1371000_non_pb_rows = set()          # zero-PB rows touching fss/bok/khug

for rid, pcs, cands in rows:
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    if not offs:
        continue
    if (pcs or 0) > 10 or any(has_body(c) for c in offs):
        continue  # not a B-floor row
    n_bfloor += 1

    pb_strict = any(c.get("retrieval_method") == "policy_briefing_api" for c in offs)
    pb_incl = any(is_pb(c) for c in offs)
    if pb_strict:
        n_pb_strict += 1
    if pb_incl:
        n_pb_inclusive += 1
    if any(is_pb(c) and has_body(c) for c in offs):
        n_pb_with_body += 1

    domains = {cand_dom(c) for c in offs}
    if "fss.or.kr" in domains:
        fss_rows.add(rid)

    if pb_incl:
        cls_counts["PB_ALREADY_PRESENT"] += 1
    else:
        n_zero_pb += 1
        for d in domains:
            zero_pb_domain_rows[d].add(rid)
        if "fss.or.kr" in domains:
            fss_rows_non_pb.add(rid)
        if domains & NON_1371000:
            non1371000_non_pb_rows.add(rid)
        if domains & STATIC_ALT:
            cls_counts["NON_PB_STATIC"] += 1
            non_pb_static_rows.add(rid)
        else:
            cls_counts["NON_PB_NO_STATIC"] += 1


print("BODY-2 incremental-value probe — board-crawl supply vs policy_briefing overlap")
print("  (over the B-floor: pcs<=10, official candidate present, NO official body)")
print()
print("=== (a) does path 2 (policy_briefing) already reach the B-floor rows? ===")
print("  B-floor rows                                  :", n_bfloor)
print("  ... with >=1 PB candidate (retrieval_method)  :", n_pb_strict)
print("  ... with >=1 PB candidate (incl stable marker):", n_pb_inclusive)
print("  ... carrying a PB candidate WITH a body       : %d  (expect ~0: a PB body lifts the row off the floor)" % n_pb_with_body)
print("  ... with ZERO PB candidate                    :", n_zero_pb)
print()
print("=== (b) ZERO-PB rows: which institution domains are present (distinct rows) ===")
print("     (these are the institutions whose board-crawl would add GENUINELY NEW supply)")
for d, rset in sorted(zero_pb_domain_rows.items(), key=lambda kv: -len(kv[1])):
    tag = "STATIC_ALT" if d in STATIC_ALT else ("NO_STATIC" if d in NO_STATIC else
          ("PB_LANE" if d in PB_LANE else ("API_LANE" if d in API_LANE else "")))
    star = " <- non-1371000 (clearest new supply)" if d in NON_1371000 else ""
    print("  %-14s rows=%-3d  %-11s%s" % (d, len(rset), tag, star))
print()
print("=== (c) cross-tab: each B-floor row classified ===")
for k in ("PB_ALREADY_PRESENT", "NON_PB_STATIC", "NON_PB_NO_STATIC"):
    print("  %-20s : %d" % (k, cls_counts[k]))
print("  (check: sum == B-floor -> %d == %d)" %
      (cls_counts["PB_ALREADY_PRESENT"] + cls_counts["NON_PB_STATIC"] + cls_counts["NON_PB_NO_STATIC"], n_bfloor))
print()
print("=== (d) the headline numbers ===")
print("  N_rows_where_fss_is_candidate (total)              :", len(fss_rows))
print("  N_rows_where_fss_is_candidate AND no PB (new)      :", len(fss_rows_non_pb))
print("  N_rows_non-1371000 static (fss/bok/khug), no PB    :", len(non1371000_non_pb_rows))
print("  N_rows_genuinely_addable_by_board_crawl (NON_PB_STATIC):", len(non_pb_static_rows))
print()
print("HONEST READ:")
print("  - PB_ALREADY_PRESENT rows are STILL on the floor -> PB reached them but supplied no")
print("    usable/matched body. That is the policy_briefing RECALL/MATCHING track, NOT board")
print("    supply -> a board crawl for those institutions is redundant with path 2.")
print("  - NON_PB_STATIC (esp. fss/bok, non-1371000) is the REAL new supply a board crawl adds.")
print("  - NON_PB_NO_STATIC needs an API (path 2) or Playwright (path 3), not a board crawl.")
print("  - Build board crawlers ONLY for the institutions driving NON_PB_STATIC (fss first);")
print("    do NOT build the central-ministry boards already covered by policy_briefing.")
