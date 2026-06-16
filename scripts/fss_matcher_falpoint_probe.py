# FSS-MATCHER-FALLPOINT-PROBE — THROWAWAY read-only diagnosis of WHERE each FSS
# press-release body falls below the matcher bar.
#
# SELECT-only over analysis_results, NO writes, NO DDL, NO network. Safe in the
# Render Worker Shell. Imports and calls ONLY the pure leaf scorer functions —
# it NEVER calls enrich_official_source_candidates_with_bodies /
# fetch_official_source_body (the only network paths).
#
# QUESTION THIS PROBE ANSWERS
# ---------------------------
# Phase-1 diagnosis established that an FSS candidate is touched by EXACTLY ONE
# scorer in production: ② _sentence_match_score (official_evidence_resolution.py),
# which sets official_body_match. Scorer ③ (official_body_supports_claim) NEVER
# runs on FSS (FSS candidates are appended AFTER enrich, main.py:766-768) and ①
# never sees an FSS body. The effective FSS match bar is:
#       official_evidence_score >= 55  (classification strong/medium)
#   AND len(raw_text) >= 300            (has_body gate)
#   AND url_status != weak/search-page.
# FSS supplied 329 bodies; official_body_match=True = 0/329. This probe locates,
# per FSS body, WHICH of the four conditions dropped it, and dumps the claim +
# first 300 chars of the body so a HUMAN — not the probe — decides whether each
# unmatched body is (a) on-topic-but-under-scored [matcher target] or (b)
# off-topic [retrieval problem]. (Precedent: auto OFF-TOPIC flags false-flagged
# before — id196/id206 — so the probe dumps evidence, a human judges.)
#
# It also recomputes ② from raw_text AND reads the persisted ② fields, reporting
# any drift (drift is itself a finding); computes ③ as a labeled COUNTERFACTUAL
# (the josa question) and ① as a labeled PROXY (on-topic signal); and prints a
# PB-matched comparison group through the SAME ② so the input-property
# difference between PB-pass and FSS-fail is visible side by side.
#
# WHAT IT TOUCHES
# ---------------
# Reads `analysis_results` (SELECT-only) via the same psycopg pattern as
# scripts/fss_match_probe.py:124-128. Modifies NO row, NO code. Issues NO
# INSERT/UPDATE/DELETE/DDL and makes NO network call.

import os
import json
import sys
import collections
from datetime import datetime, timedelta

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# --- PURE leaf-scorer imports (no network at import; no enrich/fetch) ---------
from text_utils import sanitize_text
from official_evidence_resolution import (
    _sentence_match_score,        # ② per-sentence scorer (the FSS gate)
    _split_sentences,             # ②'s 25-450-char sentence window
    _classify_official_evidence,  # ② score->band (>=75 strong, >=55 medium, ...)
    _claim_text,                  # ②'s claim-field concatenation
    score_official_url,           # ②'s url_status (weak_or_search_page test)
    _tokens as oer_tokens,        # ②'s tokenizer — NO josa strip
)
from official_source_body import (
    official_body_supports_claim,  # ③ whole-body scorer — COUNTERFACTUAL only
    _tokens as osb_tokens,         # ③'s tokenizer — WITH josa strip
)
from evidence_comparator import (
    _extract_keywords,  # ①'s keyword primitive — on-topic PROXY only
    _detect_concepts,   # ①'s concept primitive — on-topic PROXY only
)


# ---------------------------------------------------------------------------
# Tunable constants (commented, top-of-file).
# ---------------------------------------------------------------------------
# How many days back to scan. 0 = WHOLE CORPUS (default). When >0, windowed
# Python-side on created_at[:10] (YYYY-MM-DD), mirroring fss_match_probe.py.
LOOKBACK_DAYS = 0

# Stable primary-document markers (confirmed in the providers).
FSS_MARKER = "fss_bodo_content_id"
PB_MARKER = "policy_briefing_news_item_id"

# Effective ② match bar (read-only mirror of the live thresholds — the probe
# changes NOTHING; these are used only to LABEL the fall point).
MEDIUM_SCORE_BAR = 55          # _classify_official_evidence medium band
HAS_BODY_MIN_CHARS = 300       # _resolve_source has_body gate
SENTENCE_CAP = 80              # _resolve_source scores sentences[:80]
STRONG_MEDIUM = {"strong_official_direct_support", "medium_official_contextual_support"}

# How many PB-matched rows to print in the comparison group.
PB_COMPARISON_N = 10

# First N chars of the cleaned body to dump for the human on/off-topic read.
BODY_PREVIEW_CHARS = 300

# Cap on per-body detail blocks (0 = print ALL unmatched FSS bodies). The whole
# point is that a human reads these, so default is ALL.
MAX_DETAIL_BLOCKS = 0


def _j(s):
    """Parse a JSON TEXT column, tolerant of NULL / malformed (-> None)."""
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


def _claim_for(item, claims):
    """The normalized claim this candidate was scored against (by claim_index)."""
    try:
        ci = int(item.get("claim_index") or 0)
    except (TypeError, ValueError):
        ci = 0
    if isinstance(claims, list) and 0 <= ci < len(claims) and _is_dict(claims[ci]):
        return claims[ci], ci
    return {}, ci


def _body_and_meta(item):
    """Mirror _resolve_source's body/title/url selection EXACTLY (read-only).
    For FSS/PB candidates the body lives in raw_text."""
    raw = item.get("official_body_text") or item.get("body_text") or item.get("raw_text") or ""
    body_text = sanitize_text(raw)
    title = item.get("title") or item.get("official_detail_title") or ""
    url = item.get("official_detail_url") or item.get("official_body_url") or item.get("url") or ""
    return body_text, sanitize_text(title), title, url


def _recompute_scorer_two(item, claim):
    """RE-RUN ② from raw_text, mirroring _resolve_source (no I/O). Returns a dict
    of recomputed signals + the gate booleans for the fall-point label."""
    body_text, source_title, title, url = _body_and_meta(item)
    sentences = _split_sentences(body_text)
    scored = sorted(
        (_sentence_match_score(claim, sentence, source_title) for sentence in sentences[:SENTENCE_CAP]),
        key=lambda match: (-match["official_evidence_score"], match["sentence"]),
    )
    best = scored[0] if scored else {}
    recomp_score = int(best.get("official_evidence_score") or 0)

    url_status = score_official_url(url, title).get("official_url_resolution_status")
    has_body = bool(body_text and len(body_text) >= HAS_BODY_MIN_CHARS)
    classification = _classify_official_evidence(recomp_score, has_body, url_status)
    recomp_match = classification in STRONG_MEDIUM

    # Fall-point gate booleans (independent — they can co-occur).
    zero_sentences = len(sentences) == 0
    short_body = not has_body                                   # len < 300
    weak_url = url_status == "weak_or_search_page"
    under_scored = (len(sentences) > 0) and has_body and (not weak_url) and recomp_score < MEDIUM_SCORE_BAR

    # Primary single label, by priority (i)->(iv); aggregates are counted
    # independently below so overlaps are not lost.
    if recomp_match:
        fall_point = "(none — would MATCH)"
    elif zero_sentences:
        fall_point = "(i) zero sentences in 25-450 window -> score 0"
    elif short_body:
        fall_point = "(ii) has_body len<300"
    elif weak_url:
        fall_point = "(iii) url_status weak_or_search_page"
    elif under_scored:
        fall_point = "(iv) sentences exist but ② score <55"
    else:
        fall_point = "(other — score>=55 but classified non-match)"

    return {
        "body_text": body_text,
        "body_len": len(body_text),
        "sentence_yield": len(sentences),
        "title": title,
        "url": url,
        "url_status": url_status,
        "has_body": has_body,
        "recomp_score": recomp_score,
        "recomp_classification": classification,
        "recomp_match": recomp_match,
        "best_sentence": str(best.get("sentence") or ""),
        "semantic_match_score": int(best.get("semantic_match_score") or 0),
        "policy_alignment_score": int(best.get("policy_alignment_score") or 0),
        "matched_terms": list(best.get("matched_terms") or []),
        "matched_numbers": list(best.get("matched_numbers") or []),
        "zero_sentences": zero_sentences,
        "short_body": short_body,
        "weak_url": weak_url,
        "under_scored": under_scored,
        "fall_point": fall_point,
    }


def _counterfactual_three(item, claim, body_text, title):
    """③ official_body_supports_claim — COUNTERFACTUAL (FSS never runs ③ in prod).
    Mirrors the enrich call shape: claim carries _official_title_for_match and the
    body arg is "title body"."""
    c = dict(claim or {})
    c["_official_title_for_match"] = title
    three = official_body_supports_claim(c, f"{title} {body_text}")
    return {
        "three_score": int(three.get("match_score") or 0),
        "three_supports": bool(three.get("supports")),
        "three_classification": three.get("official_direct_match_classification"),
    }


def _proxy_one(claim_text, body_text):
    """①'s keyword/concept primitives — on-topic PROXY (① never sees FSS in prod)."""
    keywords = _extract_keywords(claim_text)
    kw_overlap = [k for k in keywords if k in body_text]
    claim_concepts = _detect_concepts(claim_text)
    body_concepts = _detect_concepts(body_text)
    concept_overlap = sorted(set(claim_concepts) & set(body_concepts))
    return {
        "kw_overlap": kw_overlap,
        "kw_total": len(keywords),
        "concept_overlap": concept_overlap,
    }


def _token_intersections(claim_text, body_text):
    """|claim ∩ body| under BOTH tokenizers: ②'s (no josa) is production-relevant;
    ③'s (josa) feeds the josa counterfactual."""
    c_no = set(oer_tokens(claim_text))
    b_no = set(oer_tokens(body_text))
    inter_no = c_no & b_no
    c_jo = set(osb_tokens(claim_text))
    b_jo = set(osb_tokens(body_text))
    inter_jo = c_jo & b_jo
    material_no = sorted(t for t in inter_no if len(t) >= 3)
    return {
        "inter_no_josa": sorted(inter_no),
        "inter_no_josa_n": len(inter_no),
        "inter_josa_n": len(inter_jo),
        "material_no_josa": material_no,
    }


def _build_record(item, claims):
    """Full per-candidate read-out (FSS or PB)."""
    claim, ci = _claim_for(item, claims)
    claim_text = _claim_text(claim)
    two = _recompute_scorer_two(item, claim)
    three = _counterfactual_three(item, claim, two["body_text"], two["title"])
    proxy = _proxy_one(claim_text, two["body_text"])
    toks = _token_intersections(claim_text, two["body_text"])

    stored_score = int(item.get("official_evidence_score") or 0)
    oms = item.get("official_matched_sentences") or []
    stored_best_sentence_score = None
    if oms and _is_dict(oms[0]):
        try:
            stored_best_sentence_score = int(oms[0].get("score") or 0)
        except (TypeError, ValueError):
            stored_best_sentence_score = None
    stored_match = bool(item.get("official_body_match"))
    drift = two["recomp_score"] != stored_score

    # (a)/(b) HINT — NOT a verdict. A human confirms via the printed block.
    on_topic_hint = bool(proxy["concept_overlap"]) or len(toks["material_no_josa"]) >= 2

    rec = {
        "claim_index": ci,
        "claim_text": claim_text,
        "content_id": item.get(FSS_MARKER) or item.get(PB_MARKER) or "",
        "stored_score": stored_score,
        "stored_best_sentence_score": stored_best_sentence_score,
        "stored_match": stored_match,
        "drift": drift,
        "on_topic_hint": on_topic_hint,
    }
    rec.update(two)
    rec.update(three)
    rec.update(proxy)
    rec.update(toks)
    return rec


def _print_block(idx, rec, *, kind):
    print("-" * 78)
    print("[%s #%d] content_id=%s  claim_index=%d" % (kind, idx, rec["content_id"], rec["claim_index"]))
    print("  CLAIM (normalized, full):")
    print("    %s" % (rec["claim_text"] or "(empty)"))
    print("  FSS/PB title: %s" % (rec["title"] or "(none)"))
    print("  cleaned-body chars: %d   sentence-yield (25-450 window): %d"
          % (rec["body_len"], rec["sentence_yield"]))
    print("  ② RECOMPUTED: score=%d band=%s match=%s"
          % (rec["recomp_score"], rec["recomp_classification"], rec["recomp_match"]))
    print("     best sentence: %s" % (rec["best_sentence"][:200] or "(none)"))
    print("     sub-scores: semantic=%d policy_alignment=%d matched_terms=%s matched_numbers=%s"
          % (rec["semantic_match_score"], rec["policy_alignment_score"],
             rec["matched_terms"][:12], rec["matched_numbers"][:8]))
    print("  ② STORED: official_evidence_score=%s  matched_sentences[0].score=%s  official_body_match=%s"
          % (rec["stored_score"], rec["stored_best_sentence_score"], rec["stored_match"]))
    print("     DRIFT (recomputed != stored): %s" % rec["drift"])
    print("  ③ COUNTERFACTUAL (josa; FSS never runs ③ in prod): score=%d band=%s supports=%s"
          % (rec["three_score"], rec["three_classification"], rec["three_supports"]))
    print("  ① PROXY (on-topic; ① never sees FSS in prod): kw_overlap=%d/%d concepts=%s"
          % (len(rec["kw_overlap"]), rec["kw_total"], rec["concept_overlap"]))
    print("  token-intersection |claim ∩ body|: no-josa=%d (material>=3char=%d)  josa=%d"
          % (rec["inter_no_josa_n"], len(rec["material_no_josa"]), rec["inter_josa_n"]))
    print("  url_status=%s" % rec["url_status"])
    print("  FALL POINT: %s" % rec["fall_point"])
    print("  on/off-topic HINT (human confirms): %s"
          % ("looks ON-topic" if rec["on_topic_hint"] else "looks OFF-topic"))
    print("  BODY[:%d]: %s" % (BODY_PREVIEW_CHARS, rec["body_text"][:BODY_PREVIEW_CHARS]))


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
            "SELECT id, created_at, query, source_candidates, normalized_claims "
            "FROM analysis_results ORDER BY id"
        )
        for rid, created_at, query, sc, nc in cur.fetchall():
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue
            rows.append((rid, query, _j(sc) or [], _j(nc) or []))

    print("FSS-MATCHER-FALLPOINT-PROBE — read-only ② fall-point diagnosis for FSS bodies")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   (LOOKBACK_DAYS=%d; 0 = whole corpus)" % (scope, LOOKBACK_DAYS))
    print("  FSS marker: %s   PB marker: %s" % (FSS_MARKER, PB_MARKER))
    print("  effective ② bar: score>=%d AND body>=%d chars AND url not weak"
          % (MEDIUM_SCORE_BAR, HAS_BODY_MIN_CHARS))
    print()
    if not rows:
        print("  No rows in scope — nothing to diagnose.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted.")
        return 0

    # ---- collect FSS candidate records + PB-matched comparison records --------
    fss_records = []
    pb_matched_records = []
    for rid, query, cands, claims in rows:
        for c in cands:
            if not _is_dict(c):
                continue
            if FSS_MARKER in c:
                rec = _build_record(c, claims)
                rec["row_id"] = rid
                fss_records.append(rec)
            elif PB_MARKER in c and bool(c.get("official_body_match")):
                rec = _build_record(c, claims)
                rec["row_id"] = rid
                pb_matched_records.append(rec)

    # =======================================================================
    print("=== 1. FSS BODIES — per-candidate human-read blocks ===")
    print("  total FSS candidates (marker-located) reaching a claim: %d" % len(fss_records))
    print("  (each block below is evidence for a HUMAN to judge (a) on-topic vs (b) off-topic)")
    print()

    unmatched = [r for r in fss_records if not r["stored_match"]]
    matched_fss = [r for r in fss_records if r["stored_match"]]
    # Sort unmatched by the on-topic HINT (likely-on-topic first) so the human
    # reads the strongest matcher-target candidates first. HINT only — not a verdict.
    unmatched_sorted = sorted(
        unmatched,
        key=lambda r: (r["on_topic_hint"], len(r["material_no_josa"]), r["recomp_score"]),
        reverse=True,
    )
    limit = len(unmatched_sorted) if MAX_DETAIL_BLOCKS == 0 else min(MAX_DETAIL_BLOCKS, len(unmatched_sorted))
    for i, rec in enumerate(unmatched_sorted[:limit], start=1):
        _print_block(i, rec, kind="FSS-UNMATCHED")
    if limit < len(unmatched_sorted):
        print("  ... (+%d more unmatched FSS bodies; raise MAX_DETAIL_BLOCKS to see all)"
              % (len(unmatched_sorted) - limit))
    if matched_fss:
        print("-" * 78)
        print("  NOTE: %d FSS candidate(s) carry official_body_match=True (unexpected — "
              "Phase-1 expected 0). Printing them too:" % len(matched_fss))
        for i, rec in enumerate(matched_fss, start=1):
            _print_block(i, rec, kind="FSS-MATCHED")
    print()

    # =======================================================================
    print("=== 2. AGGREGATES (over all %d FSS candidates) ===" % len(fss_records))
    bands = collections.Counter(r["recomp_classification"] for r in fss_records)
    print("  ② band distribution (recomputed _classify_official_evidence):")
    for band in ("strong_official_direct_support", "medium_official_contextual_support",
                 "weak_official_candidate_only", "no_usable_official_detail"):
        print("    %-38s : %d" % (band, bands.get(band, 0)))
    other_bands = {k: v for k, v in bands.items()
                   if k not in {"strong_official_direct_support", "medium_official_contextual_support",
                                "weak_official_candidate_only", "no_usable_official_detail"}}
    if other_bands:
        print("    other bands: %s" % dict(other_bands))
    print()
    n_zero_sent = sum(1 for r in fss_records if r["zero_sentences"])
    n_short = sum(1 for r in fss_records if r["short_body"])
    n_weak_url = sum(1 for r in fss_records if r["weak_url"])
    n_underscored = sum(1 for r in fss_records if r["under_scored"])
    print("  ★ FALL-POINT CLASSES (counted INDEPENDENTLY — a body can hit several):")
    print("    (i)   zero-sentence-yield bodies (-> score 0, DISTINCT failure) : %d" % n_zero_sent)
    print("    (ii)  has_body len<300                                          : %d" % n_short)
    print("    (iii) url_status weak_or_search_page                            : %d" % n_weak_url)
    print("    (iv)  sentences exist + body>=300 + url ok, but ② score <55     : %d" % n_underscored)
    print()
    n_josa_lift = sum(1 for r in fss_records if (not r["recomp_match"]) and r["three_supports"])
    print("  josa counterfactual: fail ② (no josa) but pass ③ (josa)          : %d" % n_josa_lift)
    n_drift = sum(1 for r in fss_records if r["drift"])
    print("  recompute-vs-stored DRIFT (recomputed ② score != stored)        : %d" % n_drift)
    print()
    n_hint_on = sum(1 for r in unmatched if r["on_topic_hint"])
    n_hint_off = sum(1 for r in unmatched if not r["on_topic_hint"])
    print("  (a)/(b) HINT distribution over %d UNMATCHED bodies"
          " — HINT ONLY, human confirms via blocks above:" % len(unmatched))
    print("    looks ON-topic  (likely matcher-target / under-scored): %d" % n_hint_on)
    print("    looks OFF-topic (likely retrieval/relevance problem)  : %d" % n_hint_off)
    print()

    # =======================================================================
    print("=== 3. COMPARISON GROUP — PB candidates that DID match (same scorer ②) ===")
    print("  PB-matched candidates found: %d  (printing up to %d)"
          % (len(pb_matched_records), PB_COMPARISON_N))
    if not pb_matched_records:
        print("  (none — no PB candidate carries official_body_match=True in scope)")
    else:
        for i, rec in enumerate(pb_matched_records[:PB_COMPARISON_N], start=1):
            _print_block(i, rec, kind="PB-MATCHED")
        print("-" * 78)
        # Side-by-side input-property summary: PB-pass vs FSS-fail.
        def _avg(vals):
            vals = [v for v in vals if v is not None]
            return (sum(vals) / len(vals)) if vals else 0.0
        pb = pb_matched_records[:PB_COMPARISON_N]
        print("  INPUT-PROPERTY CONTRAST (mean) — PB-pass vs FSS-unmatched:")
        print("    cleaned-body chars   : PB=%.0f   FSS=%.0f"
              % (_avg([r["body_len"] for r in pb]), _avg([r["body_len"] for r in unmatched])))
        print("    sentence-yield       : PB=%.1f   FSS=%.1f"
              % (_avg([r["sentence_yield"] for r in pb]), _avg([r["sentence_yield"] for r in unmatched])))
        print("    token ∩ (no-josa)    : PB=%.1f   FSS=%.1f"
              % (_avg([r["inter_no_josa_n"] for r in pb]), _avg([r["inter_no_josa_n"] for r in unmatched])))
        print("    ② recomputed score   : PB=%.1f   FSS=%.1f"
              % (_avg([r["recomp_score"] for r in pb]), _avg([r["recomp_score"] for r in unmatched])))
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written, updated, or deleted; no network.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
