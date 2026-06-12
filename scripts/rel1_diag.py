# REL-1 Phase 1 read-only diagnostic — SELECT-only, no writes, no network, safe in Worker Shell.
# Target: the C bucket = floor rows (pcs<=10) where a body WAS collected but the match is weak
# (best official body-match score < 55). Measures whether the ATTACHED document is topically the
# WRONG document for the claim (the snippet-B smoking gun), how concentrated the wrong docs are,
# and whether a more-related candidate existed. Reads only stored JSON; never re-runs anything.
import os, json, re, collections
from urllib.parse import urlparse
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")
C_SCORE_CUTOFF = 55          # mirrors m37 funnel: C = has body, best match score < 55 (medium boundary)
UNRELATED_SHARED_TOKENS = 2  # < this many shared material tokens (claim vs doc title) => topically unrelated

# small generic-word stoplist so trivial overlaps don't mask topical mismatch
STOP = {"관련", "정책", "대한", "위한", "이번", "오늘", "발표", "공식", "지원", "계획", "추진",
        "확대", "강화", "개선", "방안", "대책", "보도자료", "설명자료", "브리핑", "정부", "올해",
        "내용", "예정", "기자", "뉴스", "최근", "이상", "통해", "위원회", "기준"}


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


def toks(s):
    out = set()
    for t in re.findall(r"[가-힣A-Za-z0-9]{2,}", s or ""):
        if t.lower() in STOP or t in STOP or t.isdigit():
            continue
        out.add(t)
    return out


def best_score(c):
    try:
        return int(c.get("official_final_direct_match_score")
                   or c.get("official_evidence_score")
                   or c.get("official_body_match_score") or 0)
    except Exception:
        return 0


def has_body(c):
    try:
        return int(c.get("official_body_length") or 0) > 0
    except Exception:
        return False


def cand_title(c):
    return (c.get("official_detail_title") or c.get("title") or c.get("publisher") or "").strip()


def cand_dom(c):
    return dom(c.get("official_detail_url") or c.get("url") or c.get("official_search_url") or "")


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, claim_text, normalized_claims, source_candidates "
                "FROM analysis_results ORDER BY id")
    for rid, pcs, ctext, nc, sc in cur.fetchall():
        rows.append((rid, pcs, ctext or "", J(nc) or [], J(sc) or []))

n_c = 0
shared_hist = collections.Counter()
n_unrelated = 0
attached_title_rows = collections.defaultdict(set)   # title -> set(row ids) : shared-wrong-doc detection
attached_dom = collections.Counter()
ncand_hist = collections.Counter()
better_cand_existed = 0
examples = []

for rid, pcs, ctext, ncl, cands in rows:
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    if not offs:
        continue
    body_cands = [c for c in offs if has_body(c)]
    if not body_cands:
        continue
    row_best = max(best_score(c) for c in body_cands)
    if (pcs or 0) > 10 or row_best >= C_SCORE_CUTOFF:
        continue  # not a C-bucket row
    n_c += 1

    # the "attached" doc = the body candidate that won (highest match score)
    attached = max(body_cands, key=lambda c: (best_score(c), len(cand_title(c))))
    # use the claim tied to this candidate if available, else the row claim_text
    ci = int(attached.get("claim_index") or 0)
    claim_s = ctext
    if ci < len(ncl) and isinstance(ncl[ci], dict):
        claim_s = " ".join(str(ncl[ci].get(k) or "") for k in ("claim_text", "actor", "action", "target", "object")) or ctext
    ct = toks(claim_s)
    at = toks(cand_title(attached))
    shared = len(ct & at)
    shared_hist[min(shared, 3)] += 1
    if shared < UNRELATED_SHARED_TOKENS:
        n_unrelated += 1
    title_key = (cand_title(attached) or "(no title)")[:60]
    attached_title_rows[title_key].add(rid)
    attached_dom[cand_dom(attached)] += 1
    ncand_hist[min(len(offs), 10)] += 1

    # (d) did a MORE topically-related candidate exist (any official candidate whose title
    # shares more claim tokens than the attached one)?
    best_other = max((len(ct & toks(cand_title(c))) for c in offs if c is not attached), default=0)
    if best_other > shared:
        better_cand_existed += 1

    if len(examples) < 10:
        examples.append((rid, claim_s[:55], title_key[:48], shared, cand_dom(attached), row_best,
                         len(offs), len(body_cands)))

print("C_BUCKET_ROWS (pcs<=10, body exists, best match score <55):", n_c)
print()
print("=== (a) claim<->attached-doc-title shared material tokens ===")
print("  shared_token_hist {0,1,2,3+}:", {k: shared_hist[k] for k in sorted(shared_hist)})
print("  TOPICALLY_UNRELATED (shared <%d): %d / %d (%.0f%%)" % (
    UNRELATED_SHARED_TOKENS, n_unrelated, n_c, 100.0 * n_unrelated / n_c if n_c else 0))
print()
print("=== (b) shared-wrong-doc concentration: attached titles by distinct C rows ===")
for title, rset in sorted(attached_title_rows.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:10]:
    print("  rows=%-3d  %s" % (len(rset), title))
print()
print("=== (c) attached-doc institution domain distribution (C rows) ===")
for d, n in attached_dom.most_common(12):
    print("  %-22s %d" % (d, n))
print()
print("=== (d) cap / selection signal ===")
print("  official_candidates_per_row hist (cap=5 sources x claims):", {k: ncand_hist[k] for k in sorted(ncand_hist)})
print("  rows where a MORE related candidate existed than the attached doc: %d / %d" % (better_cand_existed, n_c))
print()
print("=== EXAMPLES (<=10 C rows) ===")
print("  (id, claim[:55], attached_title[:48], shared_toks, attached_dom, best_score, n_off, n_body)")
for e in examples:
    print("  ", e)
