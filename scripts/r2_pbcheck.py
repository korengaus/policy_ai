# REL-1 R2 Phase 1 probe — SELECT-only, no writes, no network, safe in Worker Shell.
# Purpose: decide R2's PB-gate tightening lever from evidence on the GOOD high-confidence
# rows. R2 wants to tighten providers/policy_briefing._select_documents (today
# MIN_CLAIM_TOKEN_OVERLAP=1, so one generic finance word qualifies a release) to cut the
# korea.kr wrong-doc lane. The recall risk: PB is the finance lifeline (FIN-5/6/7; the ~36
# pcs>=70 rows lean on PB injecting real 금융위 releases). Before picking N we must see, for
# each PB-BACKED high row, whether its good injected release shares >=2 SPECIFIC tokens (so a
# broad-word-excluded >=2 gate keeps it) or only 1-specific / broad-only (so tightening kills it).
#
# This probe re-uses the PROVIDER'S OWN tokenizers (_claim_tokens / _doc_tokens / _clean_token,
# imported — never reimplemented) so the probe and the real filter cannot drift. It only READS
# stored rows; it re-runs nothing. Decision rule is printed; N is NOT picked here.
import os, sys, json, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
# IMPORT the provider's real tokenizers — do NOT reimplement, so probe == filter.
from providers.policy_briefing import _claim_tokens, _doc_tokens, _clean_token  # noqa: F401

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")
PB_RETRIEVAL_METHOD = "policy_briefing_api"  # tag set by to_official_source_candidates
HIGH_CONF_CUTOFF = 70                        # pcs >= 70 = the high-confidence rows R2 must protect

# Candidate broad-domain token set for the R2 Lever-B/C exclusion. These are the words
# that appear in nearly EVERY central-ministry finance/policy release, so sharing one of
# them is NOT topical signal. Printed in the output so it can be eyeballed and revised;
# this is the set R2 would (if Lever B/C chosen) strip from the overlap-GATE computation
# ONLY (never from the matcher, never from STOPWORDS_RELEVANCE). NOT yet committed anywhere.
BROAD_DOMAIN_TOKENS = frozenset({
    "대출", "금융", "부동산", "정책", "지원", "대책", "규제", "제도", "방안", "계획",
    "관리", "강화", "확대", "추진", "개선", "발표", "정부", "시장", "경제", "제한",
})


def J(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


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


def cand_as_doc(c):
    # _doc_tokens expects {'title', 'body'}; a PB candidate stores the body in raw_text
    # (to_official_source_candidates: "raw_text": doc.get("body")). Reconstruct that shape
    # so the provider tokenizer sees exactly what it saw at injection time.
    return {"title": c.get("title") or "", "body": c.get("raw_text") or ""}


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, claim_text, normalized_claims, source_candidates "
                "FROM analysis_results ORDER BY id")
    for rid, pcs, ctext, nc, sc in cur.fetchall():
        rows.append((rid, pcs, ctext or "", J(nc) or [], J(sc) or []))

hist = collections.Counter()      # {">=2 specific", "1 specific", "0 specific"}
per_row = []                      # (id, pcs, overlap_set, n_specific, n_broad)
n_high = 0                        # pcs>=70 rows with an official body winner
n_pb_backed = 0                   # ... whose winner is PB

for rid, pcs, ctext, ncl, cands in rows:
    if (pcs or 0) < HIGH_CONF_CUTOFF:
        continue
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    body_cands = [c for c in offs if has_body(c)]
    if not body_cands:
        continue  # no official body winner to speak of
    n_high += 1

    # winning official candidate = highest match score (same tie-break as rel1_diag/rel1_r1check)
    winner = max(body_cands, key=lambda c: (best_score(c), len(cand_title(c))))
    if (winner.get("retrieval_method") or "") != PB_RETRIEVAL_METHOD:
        continue  # not PB-backed -> tightening the PB gate cannot harm this row -> skip
    n_pb_backed += 1

    # Recompute the gate overlap with the PROVIDER's own tokenizers, exactly as
    # _select_documents would: claim tokens over the FULL normalized_claims set vs the
    # winning release's doc tokens. This is the same set the >=1 gate is computed on today.
    claim_tok = _claim_tokens(ncl)
    doc_tok = _doc_tokens(cand_as_doc(winner))
    overlap = claim_tok & doc_tok

    specific = overlap - BROAD_DOMAIN_TOKENS
    broad = overlap & BROAD_DOMAIN_TOKENS
    n_spec = len(specific)
    if n_spec >= 2:
        hist[">=2 specific"] += 1
    elif n_spec == 1:
        hist["1 specific"] += 1
    else:
        hist["0 specific"] += 1

    per_row.append((rid, pcs, sorted(overlap), n_spec, len(broad)))


print("REL-1 R2 PHASE 1 PROBE — PB-backed high-confidence row token evidence")
print()
print("=== BROAD_DOMAIN_TOKENS candidate set used (eyeball / revise) ===")
print("  ", sorted(BROAD_DOMAIN_TOKENS))
print()
print("=== sample size ===")
print("  pcs>=%d rows with an official body winner   : %d" % (HIGH_CONF_CUTOFF, n_high))
print("  ... of which PB-backed (winner=%s): %d  <-- the rows R2 tightening could harm"
      % (PB_RETRIEVAL_METHOD, n_pb_backed))
print()
print("=== histogram of PB-backed high rows by #SPECIFIC overlap tokens ===")
for k in (">=2 specific", "1 specific", "0 specific"):
    print("  %-13s : %d" % (k, hist[k]))
print()
print("=== per-row evidence (up to 15 PB-backed high rows) ===")
print("  (id, pcs, overlap_token_set, #specific, #broad)")
for r in per_row[:15]:
    print("  ", r)
if len(per_row) > 15:
    print("  ... (%d more PB-backed high rows not shown)" % (len(per_row) - 15))
print()
print("=== DECISION RULE (N is NOT picked here — read the histogram first) ===")
print("  - almost all PB-backed high rows have >=2 specific  -> Lever C safe (broad-exclude AND >=2).")
print("  - many are exactly 1 specific (+broad)              -> Lever B (broad-exclude, keep >=1 specific);")
print("                                                          >=2 would kill these good rows.")
print("  - a meaningful share are 0 specific (broad-only)    -> ANY injection-gate tightening loses")
print("                                                          recall -> escalate: R2 defers to the")
print("                                                          matcher / official_relevance R3 (out of scope).")
