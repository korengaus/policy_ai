# BODY-1 Phase 1 read-only diagnostic — SELECT-only, no writes, no network, safe to run in Worker Shell
# Aggregates WHY official candidates never produced a stored body, over all rows and
# specifically the B-stage floor rows (confidence<=10, candidate-but-no-body). Reads only
# stored source_candidates JSON; never re-runs resolve()/fetch/pipeline.
import os, json, collections
from urllib.parse import urlparse
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")
HAS_BODY_FLOOR = 300  # _resolve_source / fetch_official_source_body has_body threshold

# Cheap URL-shape heuristic (no network): mirrors official_evidence_resolution
# DETAIL_URL_SIGNALS / WEAK_URL_SIGNALS without importing matcher modules.
DETAIL_SIGNALS = ("/view", "view", "detail", "article", "/board", "/bbs", "brd", "/news/",
                  "/press", "press", "briefing", "announce", "notice", "/report", "document",
                  "download", ".pdf")
WEAK_SIGNALS = ("search", "list", "main", "index", "category", "menu", "portal", "login",
                "sitemap", "minwon", "keyword", "query=", "srchtxt")


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


def url_shape(u):
    low = (u or "").lower()
    if not low:
        return "no_url"
    weak = any(sig in low for sig in WEAK_SIGNALS)
    detail = any(sig in low for sig in DETAIL_SIGNALS)
    if detail and not weak:
        return "detail_like"
    if weak and not detail:
        return "list_or_search_like"
    if weak and detail:
        return "mixed"
    return "plain_or_unknown"


def has_body(cand):
    # mirrors m37_snippet_a "bodies" definition: official_body_length > 0
    try:
        return int(cand.get("official_body_length") or 0) > 0
    except Exception:
        return False


def reason_of(cand):
    r = cand.get("official_body_failure_reason")
    if r:
        return str(r)
    # collapse HTTP status / exception-typed reasons into families for the dist
    return "no_reason_recorded"


def reason_family(r):
    if r.startswith("http_status_"):
        return "http_status_4xx_5xx"
    if r.startswith("official_body_fetch_failed"):
        return "official_body_fetch_failed"
    if r.startswith("official_body_parse_failed"):
        return "official_body_parse_failed"
    return r


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, source_candidates FROM analysis_results ORDER BY id")
    for rid, pcs, sc in cur.fetchall():
        rows.append((rid, pcs, J(sc) or []))

print("TOTAL_ROWS", len(rows))

# global counters
n_off = n_off_body = n_off_nobody = 0
reason_all = collections.Counter()
reason_bfloor = collections.Counter()
noreason_type = collections.Counter()
noreason_domain = collections.Counter()
type_total = collections.Counter(); type_body = collections.Counter()
domain_total = collections.Counter(); domain_body = collections.Counter()
shape_nobody = collections.Counter()
under300 = collections.Counter()  # 0 < len < 300, by reason_family
under300_total = 0
examples = []

# B-stage floor rows = pcs<=10, >=1 official candidate, 0 official candidates with body
n_bfloor_rows = 0

for rid, pcs, cands in rows:
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    if not offs:
        continue
    row_has_body = any(has_body(c) for c in offs)
    is_bfloor = (pcs or 0) <= 10 and offs and not row_has_body
    if is_bfloor:
        n_bfloor_rows += 1
    for c in offs:
        n_off += 1
        b = has_body(c)
        u = c.get("official_detail_url") or c.get("official_body_url") or c.get("url") or ""
        dom = domain(u)
        type_total[c.get("source_type")] += 1
        domain_total[dom] += 1
        try:
            blen = int(c.get("official_body_length") or 0)
        except Exception:
            blen = 0
        if b:
            n_off_body += 1
            type_body[c.get("source_type")] += 1
            domain_body[dom] += 1
            if 0 < blen < HAS_BODY_FLOOR:
                under300_total += 1
                under300[reason_family(reason_of(c))] += 1
        else:
            n_off_nobody += 1
            fam = reason_family(reason_of(c))
            reason_all[fam] += 1
            shape_nobody[url_shape(u)] += 1
            if fam == "no_reason_recorded":
                noreason_type[c.get("source_type")] += 1
                noreason_domain[dom] += 1
            if is_bfloor:
                reason_bfloor[fam] += 1
                if len(examples) < 10:
                    examples.append((rid, c.get("source_type"), dom, fam, blen, url_shape(u), (u or "")[:80]))

print("OFFICIAL_CANDIDATES total=%d with_body=%d no_body=%d" % (n_off, n_off_body, n_off_nobody))
print("B_FLOOR_ROWS(pcs<=10, candidate-but-no-body)", n_bfloor_rows)
print()
print("=== FAILURE_REASON_DIST (ALL official no-body candidates) ===")
for r, n in reason_all.most_common():
    print("  %-34s %d" % (r, n))
print()
print("=== FAILURE_REASON_DIST (B-FLOOR rows only) ===")
for r, n in reason_bfloor.most_common():
    print("  %-34s %d" % (r, n))
print()
print("=== NO_REASON_RECORDED breakdown (silent failures) ===")
print("  by_source_type:", dict(noreason_type))
print("  by_domain(top15):", dict(collections.Counter(noreason_domain).most_common(15)))
print()
print("=== PER_SOURCE_TYPE body-success rate ===")
for t, tot in type_total.most_common():
    bod = type_body[t]
    print("  %-22s body %d/%d (%.0f%%)" % (str(t), bod, tot, 100.0 * bod / tot if tot else 0))
print()
print("=== PER_DOMAIN body-success rate (top 15 by candidate count) ===")
for d, tot in domain_total.most_common(15):
    bod = domain_body[d]
    print("  %-26s body %d/%d (%.0f%%)" % (d, bod, tot, 100.0 * bod / tot if tot else 0))
print()
print("=== URL_SHAPE of no-body official candidates ===")
print(" ", dict(shape_nobody))
print()
print("=== BODY-EXISTS-BUT-UNDER-300-FLOOR (fetched then discarded by has_body) ===")
print("  count=%d by_reason=%s" % (under300_total, dict(under300)))
print()
print("=== EXAMPLES (<=10 B-floor no-body candidates) ===")
print("  (id, source_type, domain, reason, body_len, url_shape, url[:80])")
for e in examples:
    print("  ", e)
