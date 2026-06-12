# BODY-1 Phase 2 sizing probes — read-only, SELECT-only, no writes, no network, safe in Worker Shell.
# PROBE 1: how many of the 71 B-floor rows are realistically rescuable by Direction #1
#          (detail-link extraction on server-rendered institution domains).
# PROBE 2: mechanical confirmation of the 192 no_reason / un-enriched candidate population.
# Reads only stored source_candidates JSON; never re-runs resolve()/fetch/pipeline.
import os, json, collections
from urllib.parse import urlparse
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")
TARGET_REASONS = ("official_detail_missing", "official_detail_url_missing")  # what Direction #1 turns into bodies
SERVER_RENDERED = ("fsc.go.kr", "molit.go.kr", "gov.kr")                    # realistically fixable by #1
EXT_DOMAINS = SERVER_RENDERED + ("ibk.co.kr", "jeju.go.kr")                 # optimistic extension
HARD_DOMAINS = ("fss.or.kr", "bok.or.kr")                                   # JS search.do — structurally hard


def J(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def domain(u):
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "") or "(none)"
    except Exception:
        return "(parse_error)"


def cand_domain(c):
    # detail_missing candidates have url/official_detail_url BLANKED by enrich but
    # official_search_url RETAINED (official_source_body.py:698) -> use it as fallback
    # so the institution is recoverable instead of collapsing to "(none)".
    u = (c.get("official_detail_url") or c.get("official_body_url")
         or c.get("url") or c.get("official_search_url") or "")
    return domain(u)


def reason_of(c):
    r = c.get("official_body_failure_reason")
    return str(r) if r else "no_reason_recorded"


def has_body(c):
    try:
        return int(c.get("official_body_length") or 0) > 0
    except Exception:
        return False


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, source_candidates FROM analysis_results ORDER BY id")
    for rid, pcs, sc in cur.fetchall():
        rows.append((rid, pcs, J(sc) or []))

# ===================== PROBE 1 — row-level sizing =====================
n_bfloor = 0
n_core = n_ext = 0
unresc_only_hard = unresc_only_noreason_pb = unresc_other = 0
target_dom_cand = collections.Counter()      # detail_missing/url_missing candidate count by institution
target_dom_rows = collections.defaultdict(set)  # distinct B-floor rows per institution (target reasons)

for rid, pcs, cands in rows:
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    if not offs:
        continue
    if (pcs or 0) > 10 or any(has_body(c) for c in offs):
        continue  # not a B-floor row
    n_bfloor += 1
    nobody = [c for c in offs if not has_body(c)]
    # candidates Direction #1 targets (detail_missing/url_missing), by domain
    targets = [c for c in nobody if reason_of(c) in TARGET_REASONS]
    for c in targets:
        d = cand_domain(c)
        target_dom_cand[d] += 1
        target_dom_rows[d].add(rid)
    resc_core = any(cand_domain(c) in SERVER_RENDERED for c in targets)
    resc_ext = any(cand_domain(c) in EXT_DOMAINS for c in targets)
    if resc_core:
        n_core += 1
    if resc_ext:
        n_ext += 1
    else:
        # why is this row NOT rescuable even with ext domains?
        nobody_doms = {cand_domain(c) for c in nobody}
        nobody_reasons = {reason_of(c) for c in nobody}
        if nobody_doms and nobody_doms <= set(HARD_DOMAINS):
            unresc_only_hard += 1
        elif nobody_reasons <= {"no_reason_recorded"} or nobody_doms <= {"korea.kr"}:
            unresc_only_noreason_pb += 1
        else:
            unresc_other += 1

print("===== PROBE 1 — row-level sizing (of %d B-floor rows) =====" % n_bfloor)
print("N_rescuable_by_direction1_CORE (fsc/molit/gov, target reasons) :", n_core)
print("N_rescuable_by_direction1_EXT  (+ibk/jeju)                     :", n_ext)
print("N_unrescuable_by_#1 (= B-floor - EXT)                          :", n_bfloor - n_ext)
print("  ... of which only-hard-domains (fss/bok)                     :", unresc_only_hard)
print("  ... of which only no_reason / PB(korea.kr)                   :", unresc_only_noreason_pb)
print("  ... of which other                                           :", unresc_other)
print()
print("detail_missing/url_missing candidates by institution (B-floor rows):")
for d, n in target_dom_cand.most_common():
    print("  %-22s cand=%-4d distinct_rows=%d" % (d, n, len(target_dom_rows[d])))

# ===================== PROBE 2 — un-enriched / no_reason confirmation =====================
unenriched = []          # official candidate dict missing the 'official_body_length' key entirely
noreason_nobody = []     # official no-body candidate with no failure_reason recorded
rm_counter = collections.Counter()
dom_counter = collections.Counter()
enriched_example = None
for rid, pcs, cands in rows:
    for c in cands:
        if not isinstance(c, dict) or c.get("source_type") not in OFFICIAL_TYPES:
            continue
        if "official_body_length" not in c:
            unenriched.append((rid, c))
            rm_counter[c.get("retrieval_method")] += 1
            dom_counter[cand_domain(c)] += 1
        if not c.get("official_body_failure_reason") and not has_body(c):
            noreason_nobody.append((rid, c))
        if enriched_example is None and "official_body_failure_reason" in c:
            enriched_example = c

print()
print("===== PROBE 2 — un-enriched / no_reason confirmation =====")
print("official candidates MISSING 'official_body_length' key (un-enriched):", len(unenriched))
print("official no-body candidates with NO failure_reason (no_reason_nobody):", len(noreason_nobody))
print("un-enriched retrieval_method distribution:", dict(rm_counter))
print("un-enriched domain distribution(top10):", dict(dom_counter.most_common(10)))
print()
print("KEY SETS — un-enriched candidates (keys only, 3 examples):")
for rid, c in unenriched[:3]:
    print("  id=%s rm=%s :" % (rid, c.get("retrieval_method")), sorted(c.keys()))
print("KEY SETS — enriched candidate (keys only, 1 example for contrast):")
if enriched_example is not None:
    print("  rm=%s :" % enriched_example.get("retrieval_method"), sorted(enriched_example.keys()))
else:
    print("  (no enriched example with official_body_failure_reason found)")
