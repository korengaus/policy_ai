# M37 Phase 1 read-only diagnostic — SELECT-only, no writes, no network, safe to run in Worker Shell
import os, json, collections
import psycopg
url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://","postgresql://")
def J(s):
    try: return json.loads(s) if s else None
    except Exception: return None
rows=[]
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("""SELECT id, policy_confidence_score, verification_strength, verdict_label,
                          source_reliability_summary, debug_summary, source_candidates
                   FROM analysis_results ORDER BY id""")
    cols=[c.name for c in cur.description]
    rows=[dict(zip(cols,r)) for r in cur.fetchall()]
print("TOTAL_ROWS", len(rows))
conf=collections.Counter(); stage=collections.Counter(); bodylen=collections.Counter()
clsdist=collections.Counter(); cap70=[]; floor_stage=collections.Counter()
n_cand=n_body=n_scored=n_medium=n_strong=0
for r in rows:
    ds=J(r["debug_summary"]) or {}; srs=J(r["source_reliability_summary"]) or {}
    sc=J(r["source_candidates"]) or []
    pcs=r["policy_confidence_score"]
    # confidence buckets
    b = ("<=10" if (pcs or 0)<=10 else "11-20" if pcs<=20 else "21-69" if pcs<70 else "==70" if pcs==70 else "71-100")
    conf[b]+=1
    offs=[s for s in sc if isinstance(s,dict) and s.get("source_type") in ("official_government","public_institution")]
    cand = (ds.get("official_body_candidates") or ds.get("official_sources_count") or len(offs) or 0)
    bodies=[s for s in offs if int(s.get("official_body_length") or 0)>0]
    scored=[s for s in offs if (s.get("official_final_direct_match_score") or s.get("official_evidence_score"))]
    has_cand=cand>0 or bool(offs); has_body=bool(bodies); has_scored=bool(scored)
    if has_cand: n_cand+=1
    if has_body: n_body+=1
    if has_scored: n_scored+=1
    for s in offs:
        clsdist[str(s.get("official_direct_match_classification") or s.get("official_evidence_classification") or "none")]+=1
    if any((s.get("official_direct_match_classification") or s.get("official_evidence_classification"))=="medium_official_contextual_support" for s in offs): n_medium+=1
    if any((s.get("official_direct_match_classification") or s.get("official_evidence_classification"))=="strong_official_direct_support" for s in offs): n_strong+=1
    for s in bodies:
        L=int(s.get("official_body_length") or 0)
        bodylen[("0" if L==0 else "1-500" if L<=500 else "501-2000" if L<=2000 else ">2000")]+=1
    # floor breakdown (<=10)
    if (pcs or 0)<=10:
        if not has_cand: floor_stage["A_no_candidate"]+=1
        elif not has_body: floor_stage["B_candidates_no_body"]+=1
        else:
            best=max([int(s.get("official_final_direct_match_score") or s.get("official_evidence_score") or 0) for s in offs] or [0])
            floor_stage["D_near_miss_70-74" if 70<=best<75 else ("D_near_miss_55-69" if 55<=best<70 else "C_body_low_score")]+=1
    # cap-70 reconciliation
    if pcs==70:
        marker = any(("policy_briefing_news_item_id" in s) or ("national_law_mst" in s) for s in sc if isinstance(s,dict))
        strongcls = [s for s in offs if (s.get("official_direct_match_classification") or s.get("official_evidence_classification"))=="strong_official_direct_support"]
        cap70.append((r["id"], marker, len(strongcls), max([int(s.get("official_final_direct_match_score") or s.get("official_evidence_score") or 0) for s in offs] or [0])))
print("CONFIDENCE_BUCKETS", dict(conf))
print("FUNNEL candidates->bodies->scored->medium->strong",
      n_cand, n_body, n_scored, n_medium, n_strong)
print("BODYLEN_DIST", dict(bodylen))
print("CLASSIFICATION_DIST(per-candidate)", dict(clsdist))
print("FLOOR(<=10)_STAGE_BREAKDOWN", dict(floor_stage))
print("CAP70_COUNT", len(cap70))
print("CAP70_marker_true", sum(1 for c in cap70 if c[1]), "/", len(cap70))
print("CAP70_examples(id,marker,strong_cls_count,best_score)", cap70[:10])
