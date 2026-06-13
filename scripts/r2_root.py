# REL-1 R2 ROOT diagnostic — SELECT-only, no writes, no network, safe in Worker Shell.
# Context: r2_pbcheck PART A/B/C REFUTED both PB injection gates (body-overlap and title-
# overlap) — good and wrong PB rows do not separate on either, so we will NOT tighten the PB
# injection gate. Before choosing the fix lane, this probe finds the ROOT of each wrong
# korea.kr attachment:
#   - SELECTION problem (lighter fix): a genuinely more title-relevant PB release WAS present
#     among the row's candidates but LOST selection to a worse one -> fix ranking/selection.
#   - RECALL problem (heavier fix): NO candidate on the row reached >=1 specific title overlap
#     -> the right release was never fetched -> display-layer honest hide now + a separate
#     live PB-recall probe (keyword endpoint / wider window) scoped later.
#   - MIXED/ambiguous: the winner already had the best (or tied) title overlap but is still a
#     C-bucket wrong attachment (topically off despite being the most title-relevant fetched).
#
# Import discipline mirrors r2_pbcheck.py: the provider's OWN _claim_tokens / _doc_tokens /
# _clean_token / _TOKEN_RE are imported, never reimplemented, so probe == filter. The TITLE-
# overlap logic (josa-strip + BROAD-exclude) is byte-identical to r2_pbcheck PART C. Reads
# only stored rows; re-runs nothing.
import os, sys, json, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from providers.policy_briefing import _claim_tokens, _doc_tokens, _clean_token, _TOKEN_RE  # noqa: F401

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

OFFICIAL_TYPES = ("official_government", "public_institution")
PB_RETRIEVAL_METHOD = "policy_briefing_api"
C_SCORE_CUTOFF = 55          # C-bucket = has body, best match score < 55 (mirrors rel1_diag / r2_pbcheck)
KOREA_DOMAIN = "korea.kr"    # the PB-injected wrong-attachment lane (rel1_diag korea.kr=31)

# Same candidate BROAD set as r2_pbcheck (printed there); kept identical so PART C title-overlap
# numbers reconcile across the two probes.
BROAD_DOMAIN_TOKENS = frozenset({
    "대출", "금융", "부동산", "정책", "지원", "대책", "규제", "제도", "방안", "계획",
    "관리", "강화", "확대", "추진", "개선", "발표", "정부", "시장", "경제", "제한",
})

# Identical _JOSA / strip rule as r2_pbcheck PART C.
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


def cand_as_doc(c):
    return {"title": c.get("title") or "", "body": c.get("raw_text") or ""}


def dom(u):
    from urllib.parse import urlparse
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "") or "(none)"
    except Exception:
        return "(none)"


def cand_dom(c):
    return dom(c.get("official_detail_url") or c.get("url") or c.get("official_search_url") or "")


def josa_suffix(t):
    for j in sorted(_JOSA, key=len, reverse=True):
        if t.endswith(j) and len(t) - len(j) >= 2:
            return j
    return ""


def josa_strip(t):
    j = josa_suffix(t)
    return t[: len(t) - len(j)] if j else t


def title_tokens(title):
    # title-ONLY via the provider's _TOKEN_RE -> _clean_token path (the PART C tokenizer).
    return {
        cleaned
        for token in _TOKEN_RE.findall(title or "")
        if (cleaned := _clean_token(token)) is not None
    }


def topic_tokens(token_set):
    # josa-strip, drop BROAD + sub-2-char stems -> the SPECIFIC-topic set (PART C logic).
    out = set()
    for t in token_set:
        s = josa_strip(t)
        if len(s) >= 2 and s not in BROAD_DOMAIN_TOKENS:
            out.add(s)
    return out


rows = []
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, policy_confidence_score, claim_text, normalized_claims, source_candidates "
                "FROM analysis_results ORDER BY id")
    for rid, pcs, ctext, nc, sc in cur.fetchall():
        rows.append((rid, pcs, ctext or "", J(nc) or [], J(sc) or []))

n_selection = 0
n_recall = 0
n_mixed = 0
n_wrong = 0
per_row = []

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

    # claim TITLE-topic set computed once per row (PART C: full normalized_claims set)
    claim_topic = topic_tokens(_claim_tokens(ncl))

    def title_ov(c):
        return claim_topic & topic_tokens(title_tokens(cand_title(c)))

    winner_ov = title_ov(attached)
    winner_n = len(winner_ov)

    # enumerate ALL OTHER official candidates (the full list, not just the winner)
    others = [c for c in offs if c is not attached]
    best_other = None
    best_other_n = 0
    for c in others:
        n = len(title_ov(c))
        if n > best_other_n:
            best_other_n = n
            best_other = c

    # max title-overlap achieved by ANY candidate on the row (winner or other)
    max_any = max([winner_n] + [len(title_ov(c)) for c in others]) if others else winner_n

    n_pb_cands = sum(1 for c in offs if (c.get("retrieval_method") or "") == PB_RETRIEVAL_METHOD)

    # ---- classification (mutually exclusive; recall checked first) ----
    if max_any < 1:
        cls = "RECALL"          # no candidate reached >=1 specific title overlap
        n_recall += 1
    elif best_other_n > winner_n:
        cls = "SELECTION"       # a more title-relevant release existed but lost selection
        n_selection += 1
    else:
        cls = "MIXED"           # winner already had best/tied title overlap yet still wrong
        n_mixed += 1

    per_row.append((
        rid, n_pb_cands, cand_title(attached)[:40], winner_n, best_other_n,
        (cand_title(best_other)[:40] if best_other is not None else "(none)"), cls,
        sorted(best_other_ov := title_ov(best_other)) if best_other is not None else [],
    ))


print("REL-1 R2 ROOT — why each wrong korea.kr attachment happened (selection vs recall)")
print("  TITLE-overlap logic identical to r2_pbcheck PART C (josa-strip + BROAD-exclude).")
print("  BROAD set:", sorted(BROAD_DOMAIN_TOKENS))
print()
print("=== aggregate over the wrong korea.kr C-bucket lane ===")
print("  wrong korea.kr C-bucket rows (the ~31 lane)      :", n_wrong)
print("  N_SELECTION (a non-winner had STRICTLY higher title-overlap than winner):", n_selection)
print("  N_RECALL    (NO candidate reached >=1 specific title-overlap)           :", n_recall)
print("  N_MIXED     (winner already had best/tied title-overlap, still wrong)   :", n_mixed)
print()
print("  RECALL rows need a LIVE PB-recall probe follow-up (keyword endpoint / wider window):")
print("    we cannot query PB live here, so all %d RECALL rows are flagged for that probe." % n_recall)
print()
print("=== per-row (up to 20): (id, n_pb_cands, winner_title[:40], winner_n, best_other_n, best_other_title[:40], class) ===")
for r in per_row[:20]:
    rid, npb, wt, wn, bon, bot, cls, botoks = r
    print("  id=%-5s npb=%-3d w_n=%d bo_n=%d [%s]" % (rid, npb, wn, bon, cls))
    print("        winner: %s" % wt)
    print("        best_other: %s  toks=%s" % (bot, botoks))
if len(per_row) > 20:
    print("  ... (%d more wrong rows not shown)" % (len(per_row) - 20))
print()
print("=== READING (lane to pick — N is the evidence) ===")
print("  - N_SELECTION dominates -> a better release was fetched but lost: fix SELECTION")
print("    (rank candidates by title-overlap; lighter, provider-local / selection-layer, removal-free).")
print("  - N_RECALL dominates    -> the right release was never fetched: a display-layer honest hide")
print("    ('no topically-matched official source') is the interim, while a heavier PB-recall fix")
print("    (keyword endpoint / wider window) is scoped separately via the live PB-recall probe.")
print("  - N_MIXED dominates     -> title-overlap alone can't rank the right doc to the top either;")
print("    the discriminator is downstream (matcher / display), not selection -> lean display-layer.")
