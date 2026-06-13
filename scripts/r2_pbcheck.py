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
C_SCORE_CUTOFF = 55                          # C-bucket = has body, best match score < 55 (mirrors rel1_diag)
KOREA_DOMAIN = "korea.kr"                    # the PB-injected wrong-attachment lane (rel1_diag korea.kr=31)

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


def dom(u):
    from urllib.parse import urlparse
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "") or "(none)"
    except Exception:
        return "(none)"


def cand_dom(c):
    return dom(c.get("official_detail_url") or c.get("url") or c.get("official_search_url") or "")


# Diagnostic-only josa detector: flags overlap tokens that are a content stem + a trailing
# particle (가능성을 / 우려가) that the provider tokenizer did NOT strip, so they survive as
# bogus "specific" tokens. Read-only signal — it changes nothing; it tells us whether the
# real fix is josa-stripping (a tokenizer change) rather than just expanding BROAD.
_JOSA = ("으로서", "으로써", "에서는", "에게서", "으로", "에서", "에게", "한테", "까지",
         "부터", "에는", "에도", "이라", "라는", "이는", "을", "를", "은", "는", "이",
         "가", "의", "에", "로", "와", "과", "도", "만", "랑")


def josa_suffix(t):
    for j in sorted(_JOSA, key=len, reverse=True):  # longest particle first
        if t.endswith(j) and len(t) - len(j) >= 2:   # require a >=2-char content stem
            return j
    return ""


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


# ===================== PART B — the TARGET: wrong korea.kr C-bucket rows =====================
# C-bucket (pcs<=10, body exists, best match <55) whose ATTACHED winning doc is korea.kr —
# the ~31 PB-injected wrong-attachment lane R2 is trying to cut. Same overlap recompute via
# the provider's own tokenizers, so PART A (good) and PART B (wrong) histograms are directly
# comparable on one threshold. The lever only works if the two populations are SEPARABLE.
hist_b = collections.Counter()      # {">=2 specific", "1 specific", "0 specific"}
per_row_b = []                      # (id, pcs, overlap_set, #specific, #broad)
tok_freq = collections.Counter()    # frequency of overlap tokens across wrong korea.kr rows
n_wrong = 0                         # C-bucket korea.kr-attached rows
n_wrong_pb = 0                      # ... tagged policy_briefing_api (sanity: should ~= n_wrong)
n_broad_only = 0                    # 0-specific rows whose overlap is entirely BROAD words
n_empty_overlap = 0                 # 0-specific rows whose recomputed overlap is empty

for rid, pcs, ctext, ncl, cands in rows:
    if (pcs or 0) > 10:
        continue
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    body_cands = [c for c in offs if has_body(c)]
    if not body_cands:
        continue
    if max(best_score(c) for c in body_cands) >= C_SCORE_CUTOFF:
        continue  # not a C-bucket row
    attached = max(body_cands, key=lambda c: (best_score(c), len(cand_title(c))))
    if cand_dom(attached) != KOREA_DOMAIN:
        continue  # not the korea.kr wrong-attachment lane
    n_wrong += 1
    if (attached.get("retrieval_method") or "") == PB_RETRIEVAL_METHOD:
        n_wrong_pb += 1

    overlap = _claim_tokens(ncl) & _doc_tokens(cand_as_doc(attached))
    for t in overlap:
        tok_freq[t] += 1
    specific = overlap - BROAD_DOMAIN_TOKENS
    n_spec = len(specific)
    if n_spec >= 2:
        hist_b[">=2 specific"] += 1
    elif n_spec == 1:
        hist_b["1 specific"] += 1
    else:
        hist_b["0 specific"] += 1
        if overlap:
            n_broad_only += 1
        else:
            n_empty_overlap += 1
    per_row_b.append((rid, pcs, sorted(overlap), n_spec, len(overlap & BROAD_DOMAIN_TOKENS)))


# ============================== OUTPUT ==============================
print("REL-1 R2 PHASE 1 PROBE — good-row recall lifeline (A) vs wrong-doc target (B)")
print()
print("=== BROAD_DOMAIN_TOKENS candidate set used (eyeball / revise per PART B freq) ===")
print("  ", sorted(BROAD_DOMAIN_TOKENS))
print()
print("=== PART A — PB-backed high rows (pcs>=%d): the recall lifeline to PROTECT ===" % HIGH_CONF_CUTOFF)
print("  pcs>=%d rows with an official body winner   : %d" % (HIGH_CONF_CUTOFF, n_high))
print("  ... of which PB-backed (winner=%s): %d  <-- rows R2 tightening could harm"
      % (PB_RETRIEVAL_METHOD, n_pb_backed))
print("  histogram by #SPECIFIC overlap tokens:")
for k in (">=2 specific", "1 specific", "0 specific"):
    print("    %-13s : %d" % (k, hist[k]))
print("  per-row (up to 15): (id, pcs, overlap_set, #specific, #broad)")
for r in per_row[:15]:
    print("    ", r)
if len(per_row) > 15:
    print("     ... (%d more)" % (len(per_row) - 15))
print()
print("=== PART B — wrong korea.kr C-bucket rows (pcs<=10, body, match<55): the TARGET to CUT ===")
print("  korea.kr-attached C-bucket rows (the ~31 lane)   : %d" % n_wrong)
print("  ... tagged policy_briefing_api (sanity)          : %d" % n_wrong_pb)
print("  (a) survival breakdown under a broad-excluded gate:")
print("      overlap is BROAD-only (dies under Lever B/C)  : %d" % n_broad_only)
print("      recomputed overlap empty (already 0)          : %d" % n_empty_overlap)
print("      has >=1 NON-broad token (survives Lever B)    : %d" % (hist_b["1 specific"] + hist_b[">=2 specific"]))
print("  (b) histogram by #SPECIFIC overlap tokens (compare directly to PART A):")
for k in (">=2 specific", "1 specific", "0 specific"):
    print("    %-13s : %d" % (k, hist_b[k]))
print("  per-row (up to 15): (id, pcs, overlap_set, #specific, #broad)")
for r in per_row_b[:15]:
    print("    ", r)
if len(per_row_b) > 15:
    print("     ... (%d more)" % (len(per_row_b) - 15))
print()
print("  (c) overlap-token FREQUENCY across wrong korea.kr rows (top 25) —")
print("      [B]=already in BROAD, [josa]=trailing particle not stripped. High-freq NON-[B]")
print("      tokens here are the generic-noise residue to ADD to BROAD (or josa-strip):")
for tok, n in tok_freq.most_common(25):
    flags = []
    if tok in BROAD_DOMAIN_TOKENS:
        flags.append("B")
    j = josa_suffix(tok)
    if j:
        flags.append("josa:%s" % j)
    print("    %-3d %-12s %s" % (n, tok, ("[" + ",".join(flags) + "]") if flags else ""))
n_josa = sum(1 for t in tok_freq if josa_suffix(t))
print("  distinct overlap tokens: %d ; of which josa-suffixed: %d %s" % (
    len(tok_freq), n_josa,
    "(many -> josa-stripping needed, not just BROAD expansion)" if n_josa >= max(3, len(tok_freq) // 5) else ""))
print()
print("=== DECISION FRAMING (N NOT picked here — read both histograms + the freq table) ===")
print("  Lever C/B is viable IFF the two histograms are SEPARABLE by the threshold:")
print("    good rows (PART A) cluster at >=2 specific  AND  wrong rows (PART B) at <2 / broad-only.")
print("  - If PART B wrong rows ALSO show >=2 'specific' that are actually generic-noise")
print("    (가능성을/우려가 type), the threshold does NOT separate them -> EXPAND BROAD to cover")
print("    the high-freq non-[B] residue (and/or josa-strip), then re-run this probe before picking N.")
print("  - If after a clean BROAD/josa pass good rows stay >=2 and wrong rows fall to <2 -> Lever C.")
print("  - If wrong rows are mostly broad-only already -> Lever B (broad-exclude, keep >=1 specific) suffices.")
print("  - If good rows themselves drop below 2 specific under the clean pass -> escalate (defer to")
print("    matcher / official_relevance R3, out of scope) rather than lose the recall lifeline.")
