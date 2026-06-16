# FSS-URL-GATE-PROBE — THROWAWAY read-only probe answering TWO questions about
# why every FSS body dies at the matcher's URL gate.
#
# SELECT-only over analysis_results, NO writes, NO DDL, NO network. Imports and
# calls ONLY pure leaf functions — it NEVER calls enrich / fetch (network paths).
#
# BACKGROUND (from scripts/fss_matcher_falpoint_probe.py results)
# ---------------------------------------------------------------
# 329 FSS bodies, official_body_match=True = 0/329. The decisive aggregate:
# FALL-POINT (iii) url_status==weak_or_search_page = 329/329, while (iv)
# "sentences exist + body>=300 + url ok but score<55" = 0. EVERY FSS body dies at
# the URL gate (score_official_url on the fss.or.kr originUrl returns
# weak_or_search_page) BEFORE the sentence score matters. A human read of the
# blocks ALSO found most FSS bodies attached to a claim look OFF-TOPIC. Two
# problems may stack: (1) URL gate kills all FSS; (2) retrieval attaches
# off-topic FSS bodies. This probe SEPARATES them.
#
# PART A — URL-gate root cause: for every DISTINCT FSS originUrl, call the real
#   score_official_url and attribute the deciding branch (which predicate fired /
#   failed). Contrast with PB detail URLs that pass. Output: the exact predicate
#   fss.or.kr URLs fail, so the surgery is precise (NOT a blanket allowlist).
#
# PART B — counterfactual: re-run scorer ② (_sentence_match_score over
#   _split_sentences(raw_text)[:80], max) EXACTLY as _resolve_source does, but
#   feed _classify_official_evidence a FORCED non-weak url_status
#   ("detail_page_likely") so the URL block is lifted — has_body/len logic
#   UNCHANGED. Counts how many FSS bodies would then become official_body_match
#   (score>=55 AND len>=300), and dumps claim+body[:300] for each so a HUMAN
#   confirms on/off-topic. Answers: does a URL-gate fix ALONE drop the floor, or
#   is retrieval-relevance work ALSO required?
#
# ★ The forced-pass is MEASUREMENT ONLY — it changes NO production code, threshold
#   or scorer. Every PART-B number is labeled "counterfactual (url forced-pass),
#   not production." The probe does NOT auto-decide on/off-topic; it PRINTS blocks.
#
# WHAT IT TOUCHES: reads `analysis_results` (SELECT-only) via the same psycopg
# pattern as scripts/fss_match_probe.py:124-128. Modifies NO row, NO code. NO
# INSERT/UPDATE/DELETE/DDL, NO network.

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
    score_official_url,            # PART A — the URL gate (authoritative)
    _classify_official_evidence,   # url_status -> band; only "weak_or_search_page" special-cased
    _sentence_match_score,         # ② per-sentence scorer
    _split_sentences,              # ②'s 25-450-char sentence window
    _claim_text,                   # ②'s claim-field concatenation
    DETAIL_URL_SIGNALS,            # +28 detail-path predicate set
    WEAK_URL_SIGNALS,              # -30 weak/search predicate set
)
from official_metadata import (
    is_official_domain,                          # +25 official_domain predicate
    looks_like_official_search_or_index_url,     # -30 search/index predicate
)


# ---------------------------------------------------------------------------
# Tunable constants.
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 0            # 0 = whole corpus
FSS_MARKER = "fss_bodo_content_id"
PB_MARKER = "policy_briefing_news_item_id"

# ② match bar (read-only mirror — the probe changes NOTHING).
MEDIUM_SCORE_BAR = 55
HAS_BODY_MIN_CHARS = 300
SENTENCE_CAP = 80
STRONG_MEDIUM = {"strong_official_direct_support", "medium_official_contextual_support"}

# Forced non-weak url_status for the PART-B counterfactual. _classify_official_evidence
# special-cases ONLY "weak_or_search_page"; "detail_page_likely" (what PB detail
# pages get) falls straight through to the score-based bands. MEASUREMENT ONLY.
FORCED_URL_STATUS = "detail_page_likely"

# The +15 title bonus an official-content title can add in score_official_url.
TITLE_KEYWORDS = ["보도자료", "설명자료", "브리핑", "공고", "공지", "정책"]
MAX_TITLE_BONUS = 15

BODY_PREVIEW_CHARS = 300
PB_CONTRAST_N = 5            # PB URLs to show in PART A
MAX_CF_BLOCKS = 0           # 0 = print ALL counterfactual-match blocks


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


def _cand_url(item):
    """Mirror _resolve_source's url selection: official_detail_url -> official_body_url
    -> url. For FSS this is the originUrl (providers set official_detail_url=originUrl)."""
    return item.get("official_detail_url") or item.get("official_body_url") or item.get("url") or ""


def _cand_title(item):
    return item.get("title") or item.get("official_detail_title") or ""


def _url_branch_breakdown(url, title=""):
    """Re-derive each score_official_url branch using the REAL imported predicates,
    so the deciding rule is attributable per URL. Mirrors score_official_url's
    branch order EXACTLY; the summed/clamped total is self-checked against the
    authoritative score_official_url below."""
    nu = (url or "").lower()
    tt = sanitize_text(title or "")
    contribs = []
    if is_official_domain(url):
        contribs.append(("official_domain", +25))
    if nu.endswith(".pdf") or ".pdf" in nu:
        contribs.append(("pdf_policy_document", +12))
    detail_hits = [s for s in DETAIL_URL_SIGNALS if s in nu]
    if detail_hits:
        contribs.append(("detail_url_pattern[%s]" % detail_hits[0], +28))
    if re.search(r"\d{4,}", nu):
        contribs.append(("numeric_detail_id", +10))
    if any(k in tt for k in TITLE_KEYWORDS):
        contribs.append(("official_content_title", +15))
    looks = looks_like_official_search_or_index_url(url)
    weak_hits = [s for s in WEAK_URL_SIGNALS if s in nu]
    if looks or weak_hits:
        tag = "search_or_index_like"
        if looks:
            tag += "[looks_like_search/index]"
        if weak_hits:
            tag += "[WEAK=%s]" % weak_hits[0]
        contribs.append((tag, -30))
    if not url:
        contribs.append(("url_missing", -40))
    return contribs, bool(detail_hits), bool(re.search(r"\d{4,}", nu)), looks, weak_hits


def _deciding_rule(url, title=""):
    """One-line plain-English reason for the URL's status, derived from the real
    branches. Names the dominant predicate that keeps the score under threshold."""
    contribs, has_detail, has_numeric, looks, weak_hits = _url_branch_breakdown(url, title)
    pos = sum(v for _, v in contribs if v > 0)
    neg = sum(v for _, v in contribs if v < 0)
    if looks or weak_hits:
        why = "search/index-like penalty (-30) fires"
        if weak_hits:
            why += " via WEAK signal '%s'" % weak_hits[0]
        elif looks:
            why += " (bare-domain or search/list/index marker)"
        return why
    if not has_detail and not has_numeric:
        return ("no detail-path (DETAIL_URL_SIGNALS) and no \\d{4,} id -> only "
                "official_domain(+25) at most, below the 35 threshold")
    if not has_detail:
        return "no detail-path (DETAIL_URL_SIGNALS) match; positives sum=%d (<35)" % pos
    return "positives sum=%d, penalties sum=%d -> net below 35" % (pos, neg)


def _recompute_two_forced(item, claim):
    """Re-run ② from raw_text EXACTLY as _resolve_source, but classify with a
    FORCED non-weak url_status. has_body/len logic untouched. Returns counterfactual
    band + counterfactual match, plus the production (real-url) band for contrast."""
    raw = item.get("official_body_text") or item.get("body_text") or item.get("raw_text") or ""
    body_text = sanitize_text(raw)
    title = _cand_title(item)
    source_title = sanitize_text(title)
    url = _cand_url(item)

    sentences = _split_sentences(body_text)
    scored = sorted(
        (_sentence_match_score(claim, s, source_title) for s in sentences[:SENTENCE_CAP]),
        key=lambda m: (-m["official_evidence_score"], m["sentence"]),
    )
    best = scored[0] if scored else {}
    recomp_score = int(best.get("official_evidence_score") or 0)
    has_body = bool(body_text and len(body_text) >= HAS_BODY_MIN_CHARS)

    real_url_status = score_official_url(url, title).get("official_url_resolution_status")
    real_band = _classify_official_evidence(recomp_score, has_body, real_url_status)
    cf_band = _classify_official_evidence(recomp_score, has_body, FORCED_URL_STATUS)
    cf_match = cf_band in STRONG_MEDIUM

    # Residual reason if STILL not a match even with url forced.
    if cf_match:
        residual = ""
    elif not has_body:
        residual = "len<%d" % HAS_BODY_MIN_CHARS
    elif recomp_score < MEDIUM_SCORE_BAR:
        residual = "score<%d (topicality) even with url forced" % MEDIUM_SCORE_BAR
    else:
        residual = "other"

    return {
        "body_text": body_text,
        "body_len": len(body_text),
        "sentence_yield": len(sentences),
        "title": title,
        "url": url,
        "real_url_status": real_url_status,
        "has_body": has_body,
        "recomp_score": recomp_score,
        "real_band": real_band,
        "cf_band": cf_band,
        "cf_match": cf_match,
        "residual": residual,
        "best_sentence": str(best.get("sentence") or ""),
        "matched_terms": list(best.get("matched_terms") or []),
    }


def _claim_for(item, claims):
    try:
        ci = int(item.get("claim_index") or 0)
    except (TypeError, ValueError):
        ci = 0
    if isinstance(claims, list) and 0 <= ci < len(claims) and _is_dict(claims[ci]):
        return claims[ci], ci
    return {}, ci


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

    print("FSS-URL-GATE-PROBE — read-only: WHY fss.or.kr fails the URL gate + score-if-passed")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   (LOOKBACK_DAYS=%d; 0 = whole corpus)" % (scope, LOOKBACK_DAYS))
    print("  forced non-weak url_status for PART-B counterfactual: %s" % FORCED_URL_STATUS)
    print()
    if not rows:
        print("  No rows in scope — nothing to diagnose.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted.")
        return 0

    # ---- collect FSS candidates + distinct URLs; PB URLs for contrast ---------
    fss_cands = []        # (item, claim, claim_index)
    fss_url_first_title = {}   # distinct FSS url -> a representative title
    pb_urls = {}          # distinct PB url -> representative title
    for rid, query, cands, claims in rows:
        for c in cands:
            if not _is_dict(c):
                continue
            if FSS_MARKER in c:
                claim, ci = _claim_for(c, claims)
                fss_cands.append((c, claim, ci, rid))
                u = _cand_url(c)
                fss_url_first_title.setdefault(u, _cand_title(c))
            elif PB_MARKER in c:
                u = _cand_url(c)
                if u:
                    pb_urls.setdefault(u, _cand_title(c))

    # =======================================================================
    print("=== PART A — URL-GATE ROOT CAUSE (distinct FSS originUrls) ===")
    print("  distinct FSS originUrls: %d   (title bonus EXCLUDED to isolate URL shape)"
          % len(fss_url_first_title))
    print()
    status_counter = collections.Counter()
    for u, title in sorted(fss_url_first_title.items()):
        res = score_official_url(u, "")              # title="" isolates URL contribution
        url_score = int(res.get("official_url_score") or 0)
        url_status = res.get("official_url_resolution_status")
        reasons = res.get("official_url_resolution_reasons") or []
        status_counter[url_status] += 1
        contribs, _hd, _hn, _looks, _weak = _url_branch_breakdown(u, "")
        self_sum = max(0, min(100, sum(v for _, v in contribs)))
        could_pass_with_title = (url_score + MAX_TITLE_BONUS) >= 35
        print("  URL: %s" % (u or "(empty)"))
        print("    url_status=%s  url_score=%d  reasons=%s" % (url_status, url_score, reasons))
        print("    branch breakdown: %s" % (
            ", ".join("%s%+d" % (name, val) for name, val in contribs) or "(no branch fired)"))
        print("    self-check sum=%d (matches url_score=%s)" % (self_sum, self_sum == url_score))
        print("    deciding rule: %s" % _deciding_rule(u, ""))
        print("    would +15 title bonus clear the 35 gate? %s" % could_pass_with_title)
        print()
    print("  FSS url_status tally (title-excluded): %s" % dict(status_counter))
    print()

    print("  --- contrast: PB originUrls that PASS the gate ---")
    shown = 0
    for u, title in sorted(pb_urls.items()):
        res = score_official_url(u, "")
        st = res.get("official_url_resolution_status")
        if st == "weak_or_search_page":
            continue
        contribs, _hd, _hn, _looks, _weak = _url_branch_breakdown(u, "")
        print("  PB URL: %s" % u)
        print("    url_status=%s  url_score=%d  branches=%s"
              % (st, int(res.get("official_url_score") or 0),
                 ", ".join("%s%+d" % (n, v) for n, v in contribs)))
        shown += 1
        if shown >= PB_CONTRAST_N:
            break
    if shown == 0:
        print("  (no PB url in scope passes the gate — unexpected; check PB injection)")
    print()
    print("  >>> PRECISE PREDICATE (read from the breakdowns above): fss.or.kr URLs")
    print("      lack the +28 DETAIL_URL_SIGNALS detail-path match (and/or trigger the")
    print("      -30 search/index penalty), so the official_domain(+25) base — if it")
    print("      even fires — stays under the 35 candidate_needs_body_check threshold.")
    print("      Confirm against the per-URL 'deciding rule' lines (live values win).")
    print()

    # =======================================================================
    print("=== PART B — COUNTERFACTUAL: score if URL gate were forced to pass ===")
    print("  ★ counterfactual (url forced-pass to '%s'), NOT production. has_body/len"
          " unchanged." % FORCED_URL_STATUS)
    print("  total FSS bodies reaching a claim: %d" % len(fss_cands))
    print()

    cf_records = []
    for item, claim, ci, rid in fss_cands:
        rec = _recompute_two_forced(item, claim)
        rec["claim_text"] = _claim_text(claim)
        rec["claim_index"] = ci
        rec["row_id"] = rid
        rec["content_id"] = item.get(FSS_MARKER) or ""
        cf_records.append(rec)

    cf_matches = [r for r in cf_records if r["cf_match"]]
    print("  N FSS bodies that BECOME official_body_match under forced-pass URL: %d / %d"
          % (len(cf_matches), len(cf_records)))
    print("    (= score>=%d AND len>=%d, ignoring the URL block) — counterfactual"
          % (MEDIUM_SCORE_BAR, HAS_BODY_MIN_CHARS))
    print()

    print("  --- per counterfactual-match block (HUMAN judges on/off-topic) ---")
    if not cf_matches:
        print("  (none — a URL-gate fix ALONE would recover ZERO matches; retrieval")
        print("   relevance and/or scoring is the binding constraint, not just the URL)")
    else:
        limit = len(cf_matches) if MAX_CF_BLOCKS == 0 else min(MAX_CF_BLOCKS, len(cf_matches))
        for i, rec in enumerate(cf_matches[:limit], start=1):
            print("-" * 78)
            print("  [CF-MATCH #%d] content_id=%s  claim_index=%d  row=%s"
                  % (i, rec["content_id"], rec["claim_index"], rec["row_id"]))
            print("    CLAIM (full): %s" % (rec["claim_text"] or "(empty)"))
            print("    FSS title: %s" % (rec["title"] or "(none)"))
            print("    ② recomputed score=%d  forced url_status=%s  counterfactual band=%s"
                  % (rec["recomp_score"], FORCED_URL_STATUS, rec["cf_band"]))
            print("    (production real_url_status=%s -> real band=%s)"
                  % (rec["real_url_status"], rec["real_band"]))
            print("    best sentence: %s" % (rec["best_sentence"][:200] or "(none)"))
            print("    matched_terms: %s" % rec["matched_terms"][:12])
            print("    cleaned-body chars=%d  sentence-yield=%d" % (rec["body_len"], rec["sentence_yield"]))
            print("    BODY[:%d]: %s" % (BODY_PREVIEW_CHARS, rec["body_text"][:BODY_PREVIEW_CHARS]))
        if limit < len(cf_matches):
            print("  ... (+%d more CF-match blocks; raise MAX_CF_BLOCKS)" % (len(cf_matches) - limit))
    print()

    # ---- aggregates --------------------------------------------------------
    print("  --- PART-B aggregates (counterfactual, url forced-pass) ---")
    cf_bands = collections.Counter(r["cf_band"] for r in cf_records)
    print("  counterfactual band distribution (url no longer blocks):")
    for band in ("strong_official_direct_support", "medium_official_contextual_support",
                 "weak_official_candidate_only", "no_usable_official_detail"):
        print("    %-38s : %d" % (band, cf_bands.get(band, 0)))
    other = {k: v for k, v in cf_bands.items()
             if k not in {"strong_official_direct_support", "medium_official_contextual_support",
                          "weak_official_candidate_only", "no_usable_official_detail"}}
    if other:
        print("    other: %s" % dict(other))
    print()
    residual_short = sum(1 for r in cf_records if r["residual"].startswith("len<"))
    residual_score = sum(1 for r in cf_records if r["residual"].startswith("score<"))
    print("  residual STILL failing with URL forced (a URL fix would NOT recover these):")
    print("    len<%d                              : %d" % (HAS_BODY_MIN_CHARS, residual_short))
    print("    score<%d (topicality) url-forced    : %d" % (MEDIUM_SCORE_BAR, residual_score))
    print()

    # (a)/(b) HINT for the counterfactual-matches only — human confirms via blocks.
    print("  (a)/(b) on/off-topic split: HINT ONLY — confirm via the printed CF blocks.")
    print("    counterfactual-matches to read by hand: %d" % len(cf_matches))
    print()

    print("=== CLOSING TWO NUMBERS ===")
    print("  URL-fix-alone would recover %d match(es) (of %d FSS bodies);"
          % (len(cf_matches), len(cf_records)))
    print("  of those %d, a HUMAN must confirm how many look genuinely on-topic via the"
          % len(cf_matches))
    print("  CF blocks above (auto on/off-topic is NOT trusted — false-flag precedent).")
    print("  Bodies still failing for a NON-URL reason: %d (len<%d=%d, score<%d=%d)."
          % (residual_short + residual_score, HAS_BODY_MIN_CHARS, residual_short,
             MEDIUM_SCORE_BAR, residual_score))
    print("  => If recovered≈0, a URL fix alone does NOT drop the floor; retrieval-")
    print("     relevance work (REL-1-style filter on FSS) is ALSO required.")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; no rows written/updated/deleted; no network.")
    print("[Safety] PART-B forced-pass is MEASUREMENT ONLY — no production code/threshold changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
