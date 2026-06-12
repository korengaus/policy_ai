# M37 Phase 1 read-only diagnostic — SELECT-only, no writes, no network, safe to run in Worker Shell
import os, json
import psycopg
from official_source_body import official_body_supports_claim, _tokens as body_tok, _numbers as body_num
from official_evidence_resolution import (_sentence_match_score, _split_sentences,
        _tokens as res_tok, _numbers as res_num, _claim_text)
url=os.environ["DATABASE_URL"].replace("postgresql+psycopg://","postgresql://").replace("postgresql+psycopg2://","postgresql://")
def J(s):
    try: return json.loads(s) if s else None
    except Exception: return None
sel=[]
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT id, claim_text, normalized_claims, source_candidates FROM analysis_results ORDER BY id")
    for rid, ctext, nc, sc in cur.fetchall():
        cands=J(sc) or []; ncl=J(nc) or []
        for s in cands:
            if not isinstance(s,dict): continue
            body=s.get("official_body_text") or ""
            if len(body)>=300:
                sel.append((len(body), rid, ctext, ncl, s, body))
sel.sort(key=lambda x: x[0], reverse=True)
sel=sel[:10]
if not sel:
    print("MARKER: no source_candidate with official_body_text>=300 stored — body text unavailable in DB")
for blen, rid, ctext, ncl, s, body in sel:
    ci=int(s.get("claim_index") or 0)
    claim = ncl[ci] if ci < len(ncl) else (ncl[0] if ncl else {"claim_text": ctext or ""})
    title=s.get("title") or s.get("official_detail_title") or ""
    # Scorer 3 (body, josa-normalized, whole-body, strong>=78)
    m3=official_body_supports_claim(dict(claim), f"{title} {body}")
    # Scorer 2 (resolution, single-sentence, no-josa, strong>=75)
    sents=_split_sentences(body)
    best=None
    for snt in sents[:80]:
        r=_sentence_match_score(dict(claim), snt, title)
        if best is None or r["official_evidence_score"]>best["official_evidence_score"]: best=r
    cnum=set(res_num(_claim_text(dict(claim)))); bnum=set(res_num(f"{title} {body}"))
    print("="*70)
    print(f"id={rid} body_len={blen} marker_PB={'policy_briefing_news_item_id' in s} marker_law={'national_law_mst' in s}")
    print(" CLAIM:", (_claim_text(dict(claim)))[:120])
    print(" [Scorer3 body] score=%s cls=%s supports=%s material=%s numbers=%s concepts=%s inst=%s"%(
        m3.get("match_score"), m3.get("official_direct_match_classification"), m3.get("supports"),
        len(m3.get("matched_terms") or []), m3.get("matched_numbers"), m3.get("matched_concepts"), m3.get("matched_institutions")))
    print("   dist_below_78:", max(0,78-int(m3.get("match_score") or 0)))
    if best:
        print(" [Scorer2 sentence] score=%s -> strong@75 dist=%s"%(best["official_evidence_score"], max(0,75-int(best["official_evidence_score"]))))
        print("   best_sentence:", best["sentence"][:120])
        print("   matched_terms=%s matched_numbers=%s policy=%s action=%s"%(
            len(best.get("matched_terms") or []), best.get("matched_numbers"),
            best.get("matched_policy_terms"), best.get("matched_action_terms")))
    print("   CLAIM_NUMBERS=%s  BODY_NUMBERS(sample)=%s  intersection=%s"%(
        sorted(cnum)[:8], sorted(bnum)[:8], sorted(cnum & bnum)[:8]))
    print("   stored: official_body_match=%s direct_match_score=%s cls=%s"%(
        s.get("official_body_match"), s.get("official_final_direct_match_score") or s.get("official_evidence_score"),
        s.get("official_direct_match_classification") or s.get("official_evidence_classification")))
