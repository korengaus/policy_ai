# FLOOR-REMEASURE-PROBE — THROWAWAY read-only re-measurement of the floor funnel
# on the CURRENT (post-FSS, post-menu-fix) corpus, splitting floor rows into
# off-topic (retrieval target) vs on-topic-but-under-scored (threshold/tokenizer
# target). SELECT-only over analysis_results, pure scorer reads/recompute. NO
# writes, NO DDL, NO network. Imports ONLY pure leaf scorers (never enrich/fetch).
#
# WHY
# ---
# ⑥ matcher fix #1 (FSS URL-gate menu false-penalty) is shipped; FSS is NOT the
# floor solution (≤2 bodies). The next surgery is either (1) retrieval relevance
# (off-topic official bodies attached to claims) or (2) scorer-② threshold/
# tokenizer (on-topic bodies scoring just under strong≥75/medium≥55). This probe
# MEASURES which dominates the floor across ALL official sources (PB/law/FSS/
# crawl) so the strategist picks the next target.
#
# ★ The decisive split — off-topic vs on-topic-but-under-scored — is made by a
#   HUMAN reading printed claim+body blocks, NOT by the probe. Auto on/off-topic
#   classification has been WRONG this session (an auto HINT said "on-topic 117"
#   but a human read found most off-topic; id196/id206 precedent). The probe may
#   print an overlap HINT but labels it "HINT — human confirms" and never treats
#   it as the answer.
#
# PERFORMANCE (Phase 3-DIAG-fix)
# ------------------------------
# The first run HUNG (not a loop — pure slowness): PART B recomputed
# _sentence_match_score over 203 floor rows x candidates x sentences, and
# sanitize_text/repair_mojibake per token is too slow on the 1-CPU Worker Shell.
# This version: (1) SAMPLES the first FLOOR_SAMPLE_N floor rows for PART B/C
# (PART A still counts the FULL corpus); (2) PREFERS the persisted ②
# official_evidence_score / official_matched_sentences[0] off the candidate dict,
# recomputing via _sentence_match_score ONLY when the stored score is absent; and
# (3) CAPS any unavoidable recompute to RECOMPUTE_SENTENCE_CAP sentences and
# MAX_CANDS_PER_ROW candidates. ②'s url menu-fix changes classification only, not
# the stored official_evidence_score, so the stored score stays valid here.
#
# FLOOR DEFINITION (confirmed against policy_confidence.calculate_policy_confidence)
# --------------------------------------------------------------------------------
# The no-usable-official-doc / no-strong-Lane-B branch clamps
# policy_confidence_score = min(20, ...) AND sets verification_strength = "none"
# (policy_confidence.py:185-186). A row is treated as AT THE FLOOR when its stored
# policy_confidence_score <= FLOOR_MAX (20). verification_strength is also read and
# reported for transparency. This mirrors fss_match_probe's pcs read.
#
# PART A — corpus + M37-style funnel (candidates -> body>=300 -> scored>0 ->
#          medium>=55 -> strong>=75), with M37's 183->111->101->18->0 alongside.
# PART B — per SAMPLED floor row, best official candidate + FALL-REASON bucket
#          (i)-(v); bucket (iii) HUMAN-READ blocks; josa-effect count.
# PART C — bucket (iii) threshold sensitivity 50/52/55 (no-josa, production) AND
#          a josa-tokenizer counterfactual (② math recomputed with ③'s josa
#          tokenizer), cross-tabbed vs the human (a)/(b) placeholder, plus the
#          local-replica self-check. SENSITIVITY ONLY — no production change.

import os
import re
import json
import sys
import collections
from datetime import datetime, timedelta

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# --- PURE leaf imports (no network at import; no enrich/fetch) ----------------
from text_utils import sanitize_text
from official_evidence_resolution import (
    _sentence_match_score,         # ② per-sentence scorer (authoritative)
    _split_sentences,              # ②'s 25-450-char sentence window
    _classify_official_evidence,   # ② score->band
    _claim_text,                   # ②'s claim-field concatenation
    score_official_url,            # ②'s url gate (now with the menu guard)
    _tokens as oer_tokens,         # ②'s tokenizer — NO josa strip
    _numbers as oer_numbers,       # ②'s number extractor
    POLICY_KEYWORDS,
    ACTION_TERMS,
)
from official_source_body import (
    official_body_supports_claim,  # ③ whole-body scorer (josa) — counterfactual
    _tokens as osb_tokens,         # ③'s tokenizer — WITH josa strip
)


# ---------------------------------------------------------------------------
# Tunable constants.
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 0            # 0 = whole corpus

# Floor: policy_confidence_score clamped to min(20). A row is AT the floor when
# stored pcs <= FLOOR_MAX (the clamp output). See module header.
FLOOR_MAX = 20

# ② bands / gates (read-only mirror — the probe changes NOTHING).
MEDIUM_SCORE_BAR = 55
STRONG_SCORE_BAR = 75
HAS_BODY_MIN_CHARS = 300

# --- performance bounds (Phase 3-DIAG-fix) ---------------------------------
# PART B/C process only the first FLOOR_SAMPLE_N floor rows; PART A counts ALL.
FLOOR_SAMPLE_N = 40
# When recompute is unavoidable (stored ② score absent), cap the sentence loop.
RECOMPUTE_SENTENCE_CAP = 30
# Cap official candidates inspected per floor row.
MAX_CANDS_PER_ROW = 8

# Markers (provider = candidate provenance).
PB_MARKER = "policy_briefing_news_item_id"
LAW_MARKER = "national_law_mst"
FSS_MARKER = "fss_bodo_content_id"

OFFICIAL_TYPES = {"official_government", "public_institution"}

# Material-term whitelist used inside _sentence_match_score (:268-272) — mirrored
# so the local tokenizer-swap replica matches production byte-for-byte on no-josa.
_SHORT_MATERIAL = {"금리", "전세", "대출", "주택", "규제", "지원", "세금", "수사"}

# PART-C sensitivity thresholds (LOCAL only — not a production change).
SENSITIVITY_THRESHOLDS = [50, 52, 55]

BODY_PREVIEW_CHARS = 300
M37_FUNNEL = "183 -> 111 -> 101 -> 18 -> 0  (corpus 201, pre-FSS)"


def _j(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _row_date(created_at) -> str:
    if created_at is None:
        return ""
    s = str(created_at)
    return s[:10] if len(s) >= 10 else ""


def _is_dict(c):
    return isinstance(c, dict)


def _source_kind(item):
    if PB_MARKER in item:
        return "PB"
    if LAW_MARKER in item:
        return "LAW"
    if FSS_MARKER in item:
        return "FSS"
    return "CRAWL"


def _claim_for(item, claims):
    try:
        ci = int(item.get("claim_index") or 0)
    except (TypeError, ValueError):
        ci = 0
    if isinstance(claims, list) and 0 <= ci < len(claims) and _is_dict(claims[ci]):
        return claims[ci], ci
    return {}, ci


def _raw_body(item):
    """Mirror _resolve_source's body selection (read-only)."""
    return sanitize_text(item.get("official_body_text") or item.get("body_text") or item.get("raw_text") or "")


def _local_sentence_score(claim_text, sentence, source_title, tokenizer):
    """Local replica of _sentence_match_score's official_evidence_score that takes
    a TOKENIZER function, so passing oer_tokens reproduces production (self-checked)
    and passing osb_tokens gives the josa counterfactual. Mirrors
    official_evidence_resolution.py:260-302 EXACTLY except the term tokenizer.
    Numbers/policy/action terms are tokenizer-independent (substring / _numbers)."""
    sentence_text = sanitize_text(sentence or "")
    combined_text = sanitize_text(f"{source_title} {sentence_text}")
    claim_terms = set(tokenizer(claim_text))
    body_terms = set(tokenizer(combined_text))
    matched_terms = sorted(claim_terms & body_terms)
    material_terms = [t for t in matched_terms if len(t) >= 3 or t in _SHORT_MATERIAL]
    claim_numbers = oer_numbers(claim_text)
    matched_numbers = sorted(claim_numbers & oer_numbers(combined_text))
    action_matches = sorted(t for t in ACTION_TERMS if t in claim_text and t in combined_text)
    policy_matches = sorted(t for t in POLICY_KEYWORDS if t in claim_text and t in combined_text)
    semantic = min(100, len(material_terms) * 11 + len(policy_matches) * 8 + len(matched_numbers) * 15)
    policy_alignment = min(100, len(policy_matches) * 15 + len(action_matches) * 12 + len(matched_numbers) * 12)
    if source_title and any(t in source_title for t in policy_matches[:3]):
        policy_alignment = min(100, policy_alignment + 10)
    final = round(semantic * 0.45 + policy_alignment * 0.4 + min(100, len(material_terms) * 12) * 0.15)
    return final


def _capped_recompute_scores(body_text, source_title, claim, claim_text, *, need_josa):
    """The SLOW path, CAPPED to RECOMPUTE_SENTENCE_CAP sentences. Returns
    (real_best, real_best_sentence, oer_local_best, josa_best, n_sentences).
    real_best uses the authoritative _sentence_match_score; oer_local_best/josa_best
    use the local tokenizer-swap replica (computed only when need_josa)."""
    sentences = _split_sentences(body_text)[:RECOMPUTE_SENTENCE_CAP]
    real_best = 0
    real_best_sentence = ""
    oer_local_best = 0
    josa_best = 0
    for s in sentences:
        real = int(_sentence_match_score(claim, s, source_title).get("official_evidence_score") or 0)
        if real > real_best:
            real_best = real
            real_best_sentence = sanitize_text(s)
        if need_josa:
            ol = _local_sentence_score(claim_text, s, source_title, oer_tokens)
            if ol > oer_local_best:
                oer_local_best = ol
            jl = _local_sentence_score(claim_text, s, source_title, osb_tokens)
            if jl > josa_best:
                josa_best = jl
    return real_best, real_best_sentence, oer_local_best, josa_best, len(sentences)


def _read_candidate(item, claim, *, need_detail=False):
    """STORED-PREFERRED candidate read (fast path). Reads the persisted ②
    official_evidence_score / official_matched_sentences[0] off the candidate dict
    (mirrors fss_matcher_falpoint_probe); recomputes via _sentence_match_score ONLY
    when the stored score is ABSENT, and then capped. url/has_body gates are cheap.
    Detail fields (body text, token overlap, ③ josa) computed only when need_detail."""
    claim_text = _claim_text(claim)
    title = item.get("title") or item.get("official_detail_title") or ""
    source_title = sanitize_text(title)
    url = item.get("official_detail_url") or item.get("official_body_url") or item.get("url") or ""
    url_status = score_official_url(url, title).get("official_url_resolution_status")
    url_ok = url_status != "weak_or_search_page"

    if "official_body_length" in item:
        body_len = int(item.get("official_body_length") or 0)
    else:
        body_len = len(_raw_body(item))
    has_body = body_len >= HAS_BODY_MIN_CHARS

    if "official_evidence_score" in item:
        score_source = "stored"
        real_score = int(item.get("official_evidence_score") or 0)
        oms = item.get("official_matched_sentences") or []
        real_best_sentence = sanitize_text(oms[0].get("sentence") or "") if (oms and _is_dict(oms[0])) else ""
    else:
        score_source = "recomputed"
        real_score, real_best_sentence, _ol, _jl, _sy = _capped_recompute_scores(
            _raw_body(item), source_title, claim, claim_text, need_josa=False
        )

    classification = _classify_official_evidence(real_score, has_body, url_status)
    match = classification in {"strong_official_direct_support", "medium_official_contextual_support"}

    rec = {
        "kind": _source_kind(item),
        "title": title,
        "source_title": source_title,
        "url": url,
        "url_status": url_status,
        "url_ok": url_ok,
        "body_len": body_len,
        "has_body": has_body,
        "real_score": real_score,
        "real_best_sentence": real_best_sentence,
        "score_source": score_source,
        "classification": classification,
        "match": match,
        "claim": claim,
        "claim_text": claim_text,
        "stored_match": bool(item.get("official_body_match")),
        "_item": item,
        # detail fields (filled when need_detail)
        "body_text": "",
        "inter_n": 0,
        "material_inter_n": 0,
        "sentence_yield": 0,
        "three_supports": False,
        "three_score": 0,
        "josa_score": 0,
    }

    if need_detail:
        body_text = _raw_body(item)
        rec["body_text"] = body_text
        ctoks = set(oer_tokens(claim_text))
        btoks = set(oer_tokens(body_text))
        inter = ctoks & btoks
        rec["inter_n"] = len(inter)
        rec["material_inter_n"] = len([t for t in inter if len(t) >= 3])
        rec["sentence_yield"] = len(_split_sentences(body_text))
        if not real_best_sentence and body_text:
            _rb, rbs, _ol, _jl, _sy = _capped_recompute_scores(
                body_text, source_title, claim, claim_text, need_josa=False
            )
            rec["real_best_sentence"] = rbs
        c = dict(claim or {})
        c["_official_title_for_match"] = title
        three = official_body_supports_claim(c, f"{title} {body_text}")
        rec["three_supports"] = bool(three.get("supports"))
        rec["three_score"] = int(three.get("match_score") or 0)
    return rec


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe must run in the Render Worker Shell.")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    cutoff = ""
    if LOOKBACK_DAYS and LOOKBACK_DAYS > 0:
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    rows = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, query, policy_confidence_score, verification_strength, "
            "source_candidates, normalized_claims FROM analysis_results ORDER BY id"
        )
        for rid, created_at, query, pcs, vstr, sc, nc in cur.fetchall():
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue
            rows.append((rid, query, pcs, vstr, _j(sc) or [], _j(nc) or []))

    print("FLOOR-REMEASURE-PROBE — read-only floor funnel + off-topic vs under-scored split")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   (LOOKBACK_DAYS=%d; 0 = whole corpus)" % (scope, LOOKBACK_DAYS))
    print("  floor: policy_confidence_score <= %d  (the min(20) clamp; vstr='none' branch)" % FLOOR_MAX)
    print("  perf: PART A counts ALL rows; PART B/C SAMPLE first %d floor rows; stored ②"
          " preferred, recompute capped at %d sentences / %d cands per row."
          % (FLOOR_SAMPLE_N, RECOMPUTE_SENTENCE_CAP, MAX_CANDS_PER_ROW))
    print()
    if not rows:
        print("  No rows in scope — nothing to measure.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted.")
        return 0

    # ---- classify floor vs off-floor -------------------------------------
    def _pcs_val(pcs):
        try:
            return int(pcs) if pcs is not None else None
        except (TypeError, ValueError):
            return None

    floor_rows = []
    off_floor = 0
    for rid, query, pcs, vstr, cands, claims in rows:
        v = _pcs_val(pcs)
        if v is not None and v <= FLOOR_MAX:
            floor_rows.append((rid, query, v, vstr, cands, claims))
        else:
            off_floor += 1

    # =======================================================================
    print("=== PART A — corpus + M37-style funnel (ALL official candidates) ===")
    print("  corpus rows in scope        : %d" % len(rows))
    print("  rows AT the floor (pcs<=%d) : %d" % (FLOOR_MAX, len(floor_rows)))
    print("  rows OFF the floor          : %d" % off_floor)
    vstr_counter = collections.Counter((r[3] or "(none-col)") for r in floor_rows)
    print("  floor-row verification_strength tally: %s" % dict(vstr_counter))
    print()

    # Funnel over ALL official candidates across ALL rows (stored ② preferred).
    n_candidates = 0
    n_have_body = 0
    n_scored = 0
    n_medium = 0
    n_strong = 0
    n_stored = 0
    n_recomputed = 0
    for rid, query, pcs, vstr, cands, claims in rows:
        for c in cands:
            if not _is_dict(c) or c.get("source_type") not in OFFICIAL_TYPES:
                continue
            claim, _ci = _claim_for(c, claims)
            rec = _read_candidate(c, claim)
            n_candidates += 1
            if rec["score_source"] == "stored":
                n_stored += 1
            else:
                n_recomputed += 1
            if rec["has_body"]:
                n_have_body += 1
            if rec["real_score"] > 0:
                n_scored += 1
            if rec["real_score"] >= MEDIUM_SCORE_BAR:
                n_medium += 1
            if rec["real_score"] >= STRONG_SCORE_BAR:
                n_strong += 1

    print("  CURRENT funnel (official candidates):")
    print("    candidates                 : %d" % n_candidates)
    print("    -> body>=%d (have-body)    : %d" % (HAS_BODY_MIN_CHARS, n_have_body))
    print("    -> ② scored (>0)           : %d" % n_scored)
    print("    -> medium (②>=%d)          : %d" % (MEDIUM_SCORE_BAR, n_medium))
    print("    -> strong (②>=%d)          : %d" % (STRONG_SCORE_BAR, n_strong))
    print("  score source: stored=%d recomputed(capped)=%d" % (n_stored, n_recomputed))
    print("  M37 funnel (for SHAPE compare): %s" % M37_FUNNEL)
    print()

    # =======================================================================
    floor_sample = floor_rows[:FLOOR_SAMPLE_N]
    print("=== PART B — floor rows: best official candidate + fall-reason bucket ===")
    print("  PART B/C computed over a SAMPLE of N=%d of %d floor rows."
          % (len(floor_sample), len(floor_rows)))
    buckets = collections.Counter()
    bucket_iii_rows = []     # for the human-read blocks + PART C
    josa_effect = 0
    table = []
    for rid, query, pcs, vstr, cands, claims in floor_sample:
        officials = [c for c in cands if _is_dict(c) and c.get("source_type") in OFFICIAL_TYPES][:MAX_CANDS_PER_ROW]
        if not officials:
            buckets["(i) no official candidate"] += 1
            table.append((rid, query, "-", 0, "-", False, 0, 0, 0))
            continue
        recs = [_read_candidate(c, _claim_for(c, claims)[0]) for c in officials]
        best_lite = max(recs, key=lambda r: r["real_score"])
        # enrich the chosen best with detail (body/overlap/③) for the table + blocks
        best = _read_candidate(best_lite["_item"], best_lite["claim"], need_detail=True)
        table.append((rid, query, best["kind"], best["real_score"], best["classification"],
                      best["match"], best["material_inter_n"], best["body_len"], best["sentence_yield"]))

        if best["match"]:
            buckets["(iv) recompute-match but stored-floored (anomaly/FSS-post-fix)"] += 1
        elif not best["has_body"]:
            buckets["(ii) best body len<300 (too short)"] += 1
        elif not best["url_ok"]:
            buckets["(v) best body>=300 but url_status weak (gate-blocked)"] += 1
        elif best["real_score"] < MEDIUM_SCORE_BAR:
            buckets["(iii) scored, body>=300, url ok, but ②<55 (SPLIT BY HAND)"] += 1
            bucket_iii_rows.append((rid, query, best))
            if not best["match"] and best["three_supports"]:
                josa_effect += 1
        else:
            buckets["(vi) other"] += 1

    print("  floor rows in sample with a best-candidate read: %d" % len(floor_sample))
    print("  %-58s | %4s | %-34s | %5s" % ("row | query", "②", "band", "ovlp"))
    print("  " + "-" * 110)
    for rid, query, kind, score, band, match, ovlp, blen, syield in table:
        print("  %-6s %-8s %-40s | %4d | %-34s | %3d  (len=%d sy=%d match=%s)"
              % (rid, kind, str(query or "")[:40], score, str(band)[:34], ovlp, blen, syield, match))
    print()
    print("  FALL-REASON bucket counts (over the N=%d sample):" % len(floor_sample))
    for label in (
        "(i) no official candidate",
        "(ii) best body len<300 (too short)",
        "(iii) scored, body>=300, url ok, but ②<55 (SPLIT BY HAND)",
        "(iv) recompute-match but stored-floored (anomaly/FSS-post-fix)",
        "(v) best body>=300 but url_status weak (gate-blocked)",
        "(vi) other",
    ):
        if buckets.get(label):
            print("    %-58s : %d" % (label, buckets[label]))
    print("  josa-effect (bucket-iii best fails ② but passes ③ josa): %d" % josa_effect)
    print()

    # bucket (iii) HUMAN-READ blocks
    print("  --- bucket (iii) HUMAN-READ blocks (you split (a) on-topic vs (b) off-topic) ---")
    if not bucket_iii_rows:
        print("  (none in bucket (iii))")
    for i, (rid, query, best) in enumerate(bucket_iii_rows, start=1):
        hint = "ON-topic?" if best["material_inter_n"] >= 2 else "OFF-topic?"
        print("  " + "-" * 76)
        print("  [iii #%d] row=%s  query=%s  source=%s" % (i, rid, str(query or "")[:50], best["kind"]))
        print("    CLAIM (full): %s" % (best["claim_text"] or "(empty)"))
        print("    cand title: %s" % (best["title"] or "(none)"))
        print("    ② score=%d (band=%s, src=%s)  best sentence: %s"
              % (best["real_score"], best["classification"], best["score_source"], best["real_best_sentence"][:160]))
        print("    token overlap |claim∩body| material(>=3char)=%d  total=%d  body_len=%d  sy=%d"
              % (best["material_inter_n"], best["inter_n"], best["body_len"], best["sentence_yield"]))
        print("    ③ josa: supports=%s score=%d"
              % (best["three_supports"], best["three_score"]))
        print("    HINT (human confirms): %s" % hint)
        print("    BODY[:%d]: %s" % (BODY_PREVIEW_CHARS, best["body_text"][:BODY_PREVIEW_CHARS]))
    print()

    # =======================================================================
    print("=== PART C — bucket (iii) threshold sensitivity (SENSITIVITY ONLY, no change) ===")
    if not bucket_iii_rows:
        print("  (no bucket (iii) rows to size)")
    else:
        # Capped ② recompute over the (small) bucket-iii sample: josa counterfactual
        # + local-replica self-check. real_score above may be stored; josa_score is
        # the capped recompute — labeled accordingly.
        selfcheck_ok = 0
        selfcheck_bad = []
        for rid, query, best in bucket_iii_rows:
            real_c, _rs, oer_c, josa_c, _sy = _capped_recompute_scores(
                best["body_text"], best["source_title"], best["claim"], best["claim_text"], need_josa=True
            )
            best["josa_score"] = josa_c
            best["real_capped"] = real_c
            if real_c == oer_c:
                selfcheck_ok += 1
            else:
                selfcheck_bad.append((rid, real_c, oer_c))

        print("  how many bucket-(iii) best candidates reach a match at hypothetical ② bars:")
        print("    (production tokenizer, NO josa; real_score = stored where available)")
        for thr in SENSITIVITY_THRESHOLDS:
            n = sum(1 for _, _, b in bucket_iii_rows if b["real_score"] >= thr)
            print("      ②>=%-3d : %d / %d" % (thr, n, len(bucket_iii_rows)))
        print("  same, with the JOSA-tokenizer counterfactual (② math, ③'s josa _tokens, capped):")
        for thr in SENSITIVITY_THRESHOLDS:
            n = sum(1 for _, _, b in bucket_iii_rows if b["josa_score"] >= thr)
            print("      ②(josa)>=%-3d : %d / %d" % (thr, n, len(bucket_iii_rows)))
        print("  ③ josa whole-body would-pass count (alt tokenizer lever): %d / %d"
              % (sum(1 for _, _, b in bucket_iii_rows if b["three_supports"]), len(bucket_iii_rows)))
        print("  local-replica vs real _sentence_match_score self-check (capped): %d/%d agree"
              % (selfcheck_ok, len(bucket_iii_rows)))
        if selfcheck_bad:
            print("  ★ MISMATCH (local ② replica drifted) — josa counterfactual with caution:")
            for rid, rc, oc in selfcheck_bad[:8]:
                print("      row %s real_capped=%d local_oer=%d" % (rid, rc, oc))
        print()
        print("  cross-tab placeholder (HINT only — replace with your (a)/(b) hand-read):")
        print("    %-10s | %-8s | %-12s | %-12s" % ("hint", "②score", "②>=50?", "②josa>=55?"))
        for rid, query, b in bucket_iii_rows:
            hint = "ON?" if b["material_inter_n"] >= 2 else "OFF?"
            print("    row %-6s %-4s | %6d | %-12s | %-12s"
                  % (rid, hint, b["real_score"], b["real_score"] >= 50, b["josa_score"] >= 55))
    print()

    # =======================================================================
    print("=== CLOSING — which lever does the DATA point to? (over the N=%d sample) ===" % len(floor_sample))
    n_iii = buckets.get("(iii) scored, body>=300, url ok, but ②<55 (SPLIT BY HAND)", 0)
    n_i = buckets.get("(i) no official candidate", 0)
    n_ii = buckets.get("(ii) best body len<300 (too short)", 0)
    print("  floor sample: total=%d | (i)no-candidate=%d | (ii)short=%d | (iii)under-bar=%d | josa-rescuable=%d"
          % (len(floor_sample), n_i, n_ii, n_iii, josa_effect))
    print("  - (i) dominates  -> RETRIEVAL supply (no official body reaches the claim).")
    print("  - (iii) dominates AND human reads them OFF-topic -> RETRIEVAL RELEVANCE (wrong body attached).")
    print("  - (iii) dominates AND human reads them ON-topic  -> THRESHOLD/TOKENIZER (matcher under-scores).")
    print("  - josa-rescuable large -> a josa-strip in ②'s tokenizer is a concrete lever.")
    print("  ★ The (a)/(b) call is DEFERRED to your read of the bucket-(iii) blocks above —")
    print("    the overlap HINT is NOT trusted (auto-classification mis-split this session).")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted; no network.")
    print("[Safety] All PART-C thresholds/tokenizers are LOCAL to this probe — production unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
