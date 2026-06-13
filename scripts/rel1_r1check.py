# REL-1 R1 cutoff check — SELECT-only, no writes, no network, safe in Worker Shell.
# Purpose: rel1_diag scans ALL historical rows, so it still shows law.go.kr=14 wrong
# attachments. But R1 (the National-Law min-overlap rank-then-drop at injection) only
# affects rows ANALYZED AFTER last night's deploy — old rows keep their stored wrong docs.
# This probe splits the C-bucket law.go.kr attached-doc count by created_at relative to a
# cutoff timestamp (the R1 deploy time, passed at runtime) so we can see whether NEW rows
# stopped attaching law.go.kr wrong docs. Reads only stored JSON + created_at; re-runs nothing.
#
# Runtime arg (the R1 deploy time, ISO-8601, e.g. 2026-06-12T22:30:00+00:00):
#   - argv[1], OR env REL1_R1_CUTOFF (argv wins if both present).
#
# Expected reading:
#   - AFTER-cutoff law.go.kr attachments approach 0  => R1 works on new rows.
#   - BEFORE-cutoff unchanged (old stored wrong docs) => expected, NOT a regression.
#   - If only a handful of new C rows exist, the AFTER number is suggestive, not conclusive
#     (sample size is printed for exactly this reason).
import os, sys, json, re, collections
from datetime import datetime
from urllib.parse import urlparse
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

# ---- constants mirror rel1_diag EXACTLY so the law.go.kr count reconciles with it ----
OFFICIAL_TYPES = ("official_government", "public_institution")
C_SCORE_CUTOFF = 55          # C = has body, best match score < 55 (medium boundary)
UNRELATED_SHARED_TOKENS = 2  # < this many shared material tokens => topically unrelated (the "wrong" subset)
TARGET_DOMAIN = "law.go.kr"  # the R1 lane

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


def parse_dt(s):
    # created_at is stored as datetime.now(timezone.utc).isoformat(); the cutoff is
    # supplied in the same shape. Parse both so the split is real datetime ordering,
    # not fragile string sorting. Returns a naive datetime (tz dropped) so an aware
    # created_at and a naive cutoff still compare; both are UTC in this system.
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip())
    except Exception:
        try:
            dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        except Exception:
            return None
    return dt.replace(tzinfo=None)


# ---- cutoff from runtime (argv[1] wins, else env) ----
raw_cutoff = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REL1_R1_CUTOFF", "")).strip()
cutoff = parse_dt(raw_cutoff)
if cutoff is None:
    print("ERROR: no valid cutoff. Pass the R1 deploy time as argv[1] or env REL1_R1_CUTOFF")
    print("       (ISO-8601, e.g. 2026-06-12T22:30:00+00:00). Got: %r" % raw_cutoff)
    sys.exit(2)

rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, claim_text, normalized_claims, "
                "source_candidates, created_at "
                "FROM analysis_results ORDER BY id")
    for rid, pcs, ctext, nc, sc, cat in cur.fetchall():
        rows.append((rid, pcs, ctext or "", J(nc) or [], J(sc) or [], cat))

# Per side (BEFORE / AFTER cutoff): count C-bucket rows, law.go.kr attached docs,
# and the topically-unrelated ("wrong") subset of those law.go.kr attachments.
side = {
    "before": {"c_rows": 0, "law_attached": 0, "law_wrong": 0, "unparsed_dt": 0},
    "after":  {"c_rows": 0, "law_attached": 0, "law_wrong": 0, "unparsed_dt": 0},
}
n_unparsed_total = 0

for rid, pcs, ctext, ncl, cands, cat in rows:
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    if not offs:
        continue
    body_cands = [c for c in offs if has_body(c)]
    if not body_cands:
        continue
    row_best = max(best_score(c) for c in body_cands)
    if (pcs or 0) > 10 or row_best >= C_SCORE_CUTOFF:
        continue  # not a C-bucket row

    cdt = parse_dt(cat)
    if cdt is None:
        n_unparsed_total += 1
        # cannot place this row on a side; skip it from the split but count it once.
        continue
    key = "after" if cdt >= cutoff else "before"
    side[key]["c_rows"] += 1

    # attached doc = the body candidate that won (highest match score), same tie-break as rel1_diag
    attached = max(body_cands, key=lambda c: (best_score(c), len(cand_title(c))))
    if cand_dom(attached) != TARGET_DOMAIN:
        continue
    side[key]["law_attached"] += 1

    # is the law.go.kr attachment topically WRONG (shares < UNRELATED_SHARED_TOKENS)?
    ci = int(attached.get("claim_index") or 0)
    claim_s = ctext
    if ci < len(ncl) and isinstance(ncl[ci], dict):
        claim_s = " ".join(str(ncl[ci].get(k) or "") for k in
                           ("claim_text", "actor", "action", "target", "object")) or ctext
    shared = len(toks(claim_s) & toks(cand_title(attached)))
    if shared < UNRELATED_SHARED_TOKENS:
        side[key]["law_wrong"] += 1

b, a = side["before"], side["after"]
print("REL-1 R1 CUTOFF CHECK")
print("  cutoff (R1 deploy time, parsed naive-UTC):", cutoff.isoformat())
print("  raw cutoff arg:", repr(raw_cutoff), "(source: %s)" %
      ("argv[1]" if len(sys.argv) > 1 and sys.argv[1].strip() else "env REL1_R1_CUTOFF"))
print("  total analysis_results rows scanned:", len(rows))
print("  rows with unparseable created_at (excluded from split):", n_unparsed_total)
print()
print("=== C-bucket sample size by side (pcs<=10, body exists, best match score <55) ===")
print("  BEFORE cutoff  C rows:", b["c_rows"])
print("  AFTER  cutoff  C rows:", a["c_rows"], " <-- NEW-row sample size; small => suggestive only")
print()
print("=== law.go.kr attached-doc count (reconciles with rel1_diag section (c) total) ===")
print("  BEFORE cutoff  law.go.kr attached:", b["law_attached"],
      "(of which topically WRONG, shared<%d: %d)" % (UNRELATED_SHARED_TOKENS, b["law_wrong"]))
print("  AFTER  cutoff  law.go.kr attached:", a["law_attached"],
      "(of which topically WRONG, shared<%d: %d)" % (UNRELATED_SHARED_TOKENS, a["law_wrong"]))
print("  (rel1_diag's law.go.kr=N should equal BEFORE+AFTER law.go.kr attached =",
      b["law_attached"] + a["law_attached"], ")")
print()
print("READING: R1 confirmed iff AFTER law.go.kr attached -> ~0 while BEFORE is unchanged.")
print("         If AFTER C rows is tiny, the AFTER number is suggestive, not conclusive.")
