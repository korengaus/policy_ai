# REL-1 R2 TYPE-FLIP de-escalation probe — SELECT-only DB read; recomputes evidence_type
# via the REAL production functions; no writes, no network. Sizes the verdict-LABEL risk of
# the proposed evidence_extraction_agent.py:502 re-order (prefer higher claim<->title topic
# relevance) BEFORE any code change.
#
# Anatomy recap (Phase 1): the wrong korea.kr doc surfaces ONLY via evidence_snippets
# (evidence_extraction_agent.py:494-513 sort -> _source_body_snippets). policy_confidence_score
# is NOT affected (verdict producers read official_evidence_results + primary_document_match>=75,
# never the sub-55 source_candidates). BUT _verdict_label (verification_card.py:447-466) branches
# on evidence_snippets' evidence_type COUNTS, so swapping the surfaced official doc CAN move the
# LABEL. This probe measures, over the 23 SELECTION rows, how many actually flip the label.
#
# FIDELITY (stated explicitly, per the task):
#   * evidence_type assignment: the REAL evidence_extraction_agent._source_body_snippets is
#     imported and run (so the sub-55 official_reference override at :420-421 is exact).
#   * official snippet selection: the REAL evidence_extraction sort key (:502-509) is replicated,
#     and the proposed key prepends a title-overlap term — both run through _source_body_snippets.
#   * _verdict_label: REPLICATED (not called) because evidence_comparison / official_sources are
#     not stored columns. The replica evaluates branches 421-466 EXACTLY using stored
#     contradiction_summary / bias_framing_summary / policy_confidence_score / verification_strength
#     and the recomputed snippet counts. It does NOT evaluate branch 418 (evidence_comparison
#     conflict) nor 468-478 (need evidence_comparison) -> those return "FALLTHROUGH"/are guarded:
#       - 418 conflict is handled by the PREEMPTION GUARD: a row is only counted as a confirmed
#         flip when the replica reproduces the STORED verdict_label for the BEFORE state
#         (label_before == stored verdict_label). If the replica can't reproduce prod's current
#         label, the row is reported as replica_uncertain (flagged, NOT counted as a confirmed
#         flip) — conservative.
#       - 468-478 are swap-invariant (no snippet input) so a before/after that both reach them
#         are equal -> display-only.
import os, sys, json, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from providers.policy_briefing import _claim_tokens, _clean_token, _TOKEN_RE  # noqa: F401
from evidence_extraction_agent import _source_body_snippets                    # REAL evidence_type path
from verification_card import _STRONG_VERIFICATION_STRENGTHS                    # frozenset({"medium","high"})

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")
C_SCORE_CUTOFF = 55
KOREA_DOMAIN = "korea.kr"
OFFICIAL_BODY_METHOD = "official_body_sentence_overlap"   # extraction_method tag for official snippets
# Stored labels that can ONLY come from a PRE-snippet branch the replica can't fully evaluate
# (418 conflict / 438 confirmed). If prod's current label is one of these, the swap is
# pre-empted upstream of the snippet branches -> swap-invariant -> never a flip.
PRESNIPPET_CONFLICT_LABELS = {"draft_disputed", "draft_high_risk_review"}

BROAD_DOMAIN_TOKENS = frozenset({
    "대출", "금융", "부동산", "정책", "지원", "대책", "규제", "제도", "방안", "계획",
    "관리", "강화", "확대", "추진", "개선", "발표", "정부", "시장", "경제", "제한",
})
_JOSA = ("으로서", "으로써", "에서는", "에게서", "으로", "에서", "에게", "한테", "까지",
         "부터", "에는", "에도", "이라", "라는", "이는", "을", "를", "은", "는", "이",
         "가", "의", "에", "로", "와", "과", "도", "만", "랑")


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


def dom(u):
    from urllib.parse import urlparse
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "") or "(none)"
    except Exception:
        return "(none)"


def cand_dom(c):
    return dom(c.get("official_detail_url") or c.get("url") or c.get("official_search_url") or "")


def josa_strip(t):
    for j in sorted(_JOSA, key=len, reverse=True):
        if t.endswith(j) and len(t) - len(j) >= 2:
            return t[: len(t) - len(j)]
    return t


def title_tokens(title):
    return {c for tok in _TOKEN_RE.findall(title or "") if (c := _clean_token(tok)) is not None}


def topic_tokens(token_set):
    out = set()
    for t in token_set:
        s = josa_strip(t)
        if len(s) >= 2 and s not in BROAD_DOMAIN_TOKENS:
            out.add(s)
    return out


# ---- REAL evidence_extraction sort key (:502-509) and the PROPOSED (title-relevance) variant ----
def ee_key(c):
    # evidence_extraction_agent.py:502-509 (verbatim shape)
    return (not bool(c.get("official_body_match")),
            -best_score(c),
            c.get("publisher") or "",
            c.get("url") or "")


def proposed_key(claim_topic):
    # same key with a title-overlap term prepended (the R2-redefined re-order)
    def key(c):
        t_ov = len(claim_topic & topic_tokens(title_tokens(cand_title(c))))
        return (not bool(c.get("official_body_match")),
                -t_ov,
                -best_score(c),
                c.get("publisher") or "",
                c.get("url") or "")
    return key


def official_portion_counts(ncl, cands, sort_key):
    """Replicate evidence_extraction's official-body block (:494-513) for a given sort key and
    return a Counter of evidence_type over the surfaced official snippets. Uses the REAL
    _source_body_snippets so the sub-55 official_reference override is exact."""
    counts = collections.Counter()
    for index, claim in enumerate(ncl or []):
        obs = [
            c for c in cands
            if isinstance(c, dict)
            and c.get("claim_index") == index
            and c.get("source_type") in OFFICIAL_TYPES
            and c.get("raw_text_available")
            and (c.get("official_body_text") or c.get("body_text") or c.get("raw_text"))
        ]
        obs.sort(key=sort_key)
        for source in obs[:2]:
            for snip in _source_body_snippets(index, claim if isinstance(claim, dict) else {}, source):
                counts[snip.get("evidence_type")] += 1
    return counts


def vlabel_replica(conf, vstrength, contr, bias, dsupport, offref, insnip, claim_count):
    """EXACT replica of verification_card._verdict_label branches 421-466. Branch 418
    (evidence_comparison conflict) and 468-478 are not evaluable from stored columns:
    418 -> handled by the preemption guard; 468-478 -> 'FALLTHROUGH' (swap-invariant)."""
    possible = int(contr.get("possible_contradiction_count") or 0)
    confirmed = int(contr.get("confirmed_contradiction_count") or contr.get("likely_contradiction_count") or 0)
    high_framing = int(bias.get("high_framing_count") or 0)
    official_conf = int(contr.get("needs_official_confirmation_count") or 0)
    insufficient_claim = int(contr.get("insufficient_evidence_count") or 0)
    if high_framing and confirmed:
        return "draft_high_risk_review"
    if high_framing:
        return "draft_needs_review"
    if confirmed:
        return "draft_disputed"
    if possible:
        return "draft_needs_review"
    if claim_count and official_conf >= max(1, claim_count // 2):
        return "draft_needs_official_confirmation"
    if claim_count and insufficient_claim >= max(1, claim_count // 2):
        return "draft_needs_context"
    # ---- snippet branches (:456-466): the ONLY ones the swap can move ----
    if claim_count and dsupport >= claim_count and conf >= 60 and vstrength in _STRONG_VERIFICATION_STRENGTHS:
        return "draft_verified"
    if offref > 0 and dsupport == 0:
        return "draft_needs_official_confirmation"
    if insnip > 0:
        return "draft_needs_context"
    return "FALLTHROUGH"


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, verification_strength, verdict_label, "
                "normalized_claims, source_candidates, evidence_snippets, "
                "contradiction_summary, bias_framing_summary "
                "FROM analysis_results ORDER BY id")
    for rid, pcs, vstr, vlabel, nc, sc, es, cs, bs in cur.fetchall():
        rows.append((rid, pcs, vstr, vlabel, J(nc) or [], J(sc) or [],
                     J(es) or [], J(cs) or {}, J(bs) or {}))

n_cbucket = 0
n_selection = 0
n_display_only = 0
n_label_flip = 0
n_replica_uncertain = 0
n_official_recon_ok = 0
n_replica_agree = 0
flips = []

for rid, pcs, vstr, vlabel, ncl, cands, stored_snips, contr, bias in rows:
    if (pcs or 0) > 10:
        continue
    offs = [c for c in cands if isinstance(c, dict) and c.get("source_type") in OFFICIAL_TYPES]
    body_cands = [c for c in offs if has_body(c)]
    if not body_cands:
        continue
    if max(best_score(c) for c in body_cands) >= C_SCORE_CUTOFF:
        continue
    attached = max(body_cands, key=lambda c: (best_score(c), len(cand_title(c))))
    if cand_dom(attached) != KOREA_DOMAIN:
        continue
    n_cbucket += 1

    # ---- SELECTION test, identical to r2_root (row-level title-overlap) for reconciliation ----
    claim_topic = topic_tokens(_claim_tokens(ncl))

    def t_ov(c):
        return len(claim_topic & topic_tokens(title_tokens(cand_title(c))))

    winner_n = t_ov(attached)
    others = [c for c in body_cands if c is not attached]
    best_other_n = max((t_ov(c) for c in others), default=0)
    max_any = max([winner_n] + [t_ov(c) for c in others]) if others else winner_n
    if not (max_any >= 1 and best_other_n > winner_n):
        continue  # RECALL or MIXED -> not a SELECTION row
    n_selection += 1

    # ---- recompute official snippet evidence_type counts: current sort vs proposed sort ----
    cur_official = official_portion_counts(ncl, cands, ee_key)
    prop_official = official_portion_counts(ncl, cands, proposed_key(claim_topic))

    # stored truth: full snippet counts + the official portion actually surfaced
    stored_total = collections.Counter(s.get("evidence_type") for s in stored_snips)
    stored_official = collections.Counter(
        s.get("evidence_type") for s in stored_snips
        if s.get("extraction_method") == OFFICIAL_BODY_METHOD
    )
    if cur_official == stored_official:
        n_official_recon_ok += 1

    # before = production truth; after = swap the official portion (stored_official -> prop_official)
    before_counts = stored_total
    after_counts = (stored_total - stored_official) + prop_official

    def trip(counts):
        return (counts.get("direct_support", 0),
                counts.get("official_reference", 0),
                counts.get("insufficient_evidence", 0))

    db, ob, ib = trip(before_counts)
    da, oa, ia = trip(after_counts)
    claim_count = len(ncl)
    conf = int(pcs or 0)
    label_before = vlabel_replica(conf, vstr, contr, bias, db, ob, ib, claim_count)
    label_after = vlabel_replica(conf, vstr, contr, bias, da, oa, ia, claim_count)

    if label_before == (vlabel or ""):
        n_replica_agree += 1

    if label_before == label_after:
        n_display_only += 1
        continue
    # labels differ in the replica -> apply preemption / fidelity guards
    if (vlabel or "") in PRESNIPPET_CONFLICT_LABELS:
        n_display_only += 1   # prod decided pre-snippet (conflict/confirmed) -> swap-invariant
        continue
    if label_before != (vlabel or ""):
        n_replica_uncertain += 1   # replica can't reproduce prod's current label -> don't trust the flip
        flips.append((rid, "UNCERTAIN", vlabel, label_before, label_after,
                      (db, ob, ib), (da, oa, ia)))
        continue
    n_label_flip += 1
    flips.append((rid, "FLIP", vlabel, label_before, label_after, (db, ob, ib), (da, oa, ia)))


print("REL-1 R2 TYPE-FLIP de-escalation probe")
print("  evidence_type via REAL evidence_extraction_agent._source_body_snippets;")
print("  _verdict_label REPLICATED (branches 421-466 exact; 418 via preemption guard; 468-478 swap-invariant).")
print()
print("=== reconciliation ===")
print("  C-bucket korea.kr rows (expect ~31)     :", n_cbucket)
print("  SELECTION rows (expect ~23, r2_root)    :", n_selection)
print("  official-portion recompute == stored    : %d / %d  (fidelity of the real-fn replay)"
      % (n_official_recon_ok, n_selection))
print("  replica reproduces stored verdict_label : %d / %d  (replica fidelity on BEFORE state)"
      % (n_replica_agree, n_selection))
print()
print("=== THE TWO COUNTS ===")
print("  N_display_only (same evidence_type counts -> label unchanged -> LIGHT) :", n_display_only)
print("  N_label_flip   (counts change AND replica reproduces current label)    :", n_label_flip)
print("  N_replica_uncertain (counts change but replica != stored label; FLAG)  :", n_replica_uncertain)
print("  check: display_only + label_flip + replica_uncertain == SELECTION  -> %d == %d"
      % (n_display_only + n_label_flip + n_replica_uncertain, n_selection))
print()
print("=== per-flip detail (FLIP = trustworthy; UNCERTAIN = needs manual review) ===")
print("  (id, kind, stored_label, replica_before, replica_after, before(D,O,I), after(D,O,I))")
print("   D=direct_support  O=official_reference  I=insufficient_evidence")
for f in flips:
    print("  ", f)
if not flips:
    print("   (none) — every SELECTION row is display-only: label invariant under the re-order.")
print()
print("=== DECISION ===")
print("  N_label_flip ~0  -> R2-redefined is a LIGHT display-correctness re-order")
print("                      (provider/extraction unit tests + spot-check; minimal verdict risk).")
print("  N_label_flip >0  -> run the FULL verdict-adjacent guard (42-row fixture + verdict-adjacent")
print("                      regression + high-row >=70 guard) and inspect each flip for correctness:")
print("                      a flip FROM draft_needs_official_confirmation TO a better-supported label")
print("                      on a genuinely-promoted doc is an IMPROVEMENT; the opposite is a regression.")
print("  Any UNCERTAIN rows are reported for manual before/after inspection (replica could not")
print("  reproduce prod's current label from stored columns alone — likely an 418-conflict path).")
