# RETRIEVAL-SUPPLY-PROBE — THROWAWAY read-only diagnosis splitting the floor's
# RETRIEVAL failure into: (1) search-RECALL gap (a relevant official doc EXISTS
# but the pipeline didn't attach it) vs (2) genuinely NO official doc (-> honestly
# LOW, no surgery) vs (3) body-CLEANING/truncation bug (doc fetched but body is
# nav noise / <300 chars). FULLY NETWORK-FREE: SELECT-only DB, NO network at all,
# NO writes, NO DDL. (PART A — the PB recall test — was DROPPED; see note below.)
#
# WHY (from floor_remeasure_probe results)
# ----------------------------------------
# Of the N=40 floor sample, 87% fail for RETRIEVAL reasons: (i) NO official
# candidate 17/40, (ii) best body len<300 18/40; only (iii) scored-but-②<55 5/40,
# all human-read OFF-TOPIC (crawl lane attached the SAME "상호금융 제도개선 TF"
# release to 5 unrelated claims, body polluted with nav text). The matcher is NOT
# the wall — RETRIEVAL is. This probe splits the retrieval failure so the next
# surgery (recall-widening / crawl-cleaning / crawl-relevance-filter / accept-as-
# honestly-LOW) is chosen on evidence.
#
# ★★ PART A (PB recall test) DROPPED — why ★★
# The PB provider has NO keyword search: providers/policy_briefing.py fetches a
# DATE-WINDOWED bulk feed (<=3-day window) + client-side _select_documents
# overlap. A recall test would reconstruct each floor row's 3-day window and
# re-fetch — but floor rows are HISTORICAL and data.go.kr mostly will NOT serve
# far-past windows, so most rows came back INCONCLUSIVE (empty window != "no
# doc") — noise, not signal. PART A is therefore removed and recall is DEFERRED
# to a future probe over RECENT rows only. The "(i) no candidate" rows are read
# for now as plausibly honestly-LOW (no relevant central-ministry release
# in-window: market commentary / foreign quotes / opinion). This probe now makes
# ZERO network calls — SELECT-only DB.
#
# ★ The forced human-read discipline (auto-classification mis-split repeatedly
#   this session; id196/id206): every per-row call PRINTS claim + titles / full
#   short body for the human; HINTs are labeled "HINT — human confirms".

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

# --- PURE imports ------------------------------------------------------------
from text_utils import sanitize_text
from official_evidence_resolution import (
    _classify_official_evidence,
    _claim_text,
    score_official_url,
)
# NOTE: no provider/network imports — PART A (the only network path) was removed.


# ---------------------------------------------------------------------------
# Tunable constants.
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 0            # 0 = whole corpus
FLOOR_MAX = 20              # floor = policy_confidence_score <= 20 (min(20) clamp)

# Per-bucket sample size (PART B). PART C groups over ALL floor rows.
N_SAMPLE = 25

HAS_BODY_MIN_CHARS = 300
MEDIUM_SCORE_BAR = 55
STRONG_MEDIUM = {"strong_official_direct_support", "medium_official_contextual_support"}

PB_MARKER = "policy_briefing_news_item_id"
LAW_MARKER = "national_law_mst"
FSS_MARKER = "fss_bodo_content_id"
OFFICIAL_TYPES = {"official_government", "public_institution"}

# Nav/boilerplate signature seen polluting crawl bodies in floor_remeasure.
NAV_NOISE_MARKERS = [
    "홈으로", "알림마당", "인쇄하기", "페이스북", "트위터", "블로그",
    "FaceBook", "Facebook", "NaverBlog", "Naver", "Twitter", "카카오",
    "본문 바로가기", "메뉴 바로가기", "사이트맵",
]

# PART C: how many most-reused crawl candidates to show.
TOP_REUSED_N = 5
EXAMPLES_PER_DOC = 3
BODY_PREVIEW_CHARS = 300


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
    return sanitize_text(item.get("official_body_text") or item.get("body_text") or item.get("raw_text") or "")


def _cand_url(item):
    return item.get("official_detail_url") or item.get("official_body_url") or item.get("url") or ""


def _best_and_bucket(officials):
    """STORED-PREFERRED best official candidate + fall-reason bucket (no recompute;
    all from persisted fields). Returns (best_item, bucket_str) or (None, '(i)...')."""
    if not officials:
        return None, "(i) no official candidate"
    scored = []
    for c in officials:
        score = int(c.get("official_evidence_score") or 0)
        scored.append((score, c))
    score, best = max(scored, key=lambda t: t[0])
    if "official_body_length" in best:
        body_len = int(best.get("official_body_length") or 0)
    else:
        body_len = len(_raw_body(best))
    has_body = body_len >= HAS_BODY_MIN_CHARS
    url_status = score_official_url(_cand_url(best), best.get("title") or "").get("official_url_resolution_status")
    url_ok = url_status != "weak_or_search_page"
    classification = _classify_official_evidence(score, has_body, url_status)
    match = classification in STRONG_MEDIUM
    if match:
        bucket = "(iv) recompute-match but stored-floored"
    elif not has_body:
        bucket = "(ii) best body len<300 (too short)"
    elif not url_ok:
        bucket = "(v) best body>=300 but url weak"
    elif score < MEDIUM_SCORE_BAR:
        bucket = "(iii) scored, body>=300, url ok, ②<55"
    else:
        bucket = "(vi) other"
    return best, bucket


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe needs the DB (local PowerShell or Worker Shell).")
        return 0
    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))

    cutoff = ""
    if LOOKBACK_DAYS and LOOKBACK_DAYS > 0:
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    rows = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, query, policy_confidence_score, "
            "source_candidates, normalized_claims FROM analysis_results ORDER BY id"
        )
        for rid, created_at, query, pcs, sc, nc in cur.fetchall():
            day = _row_date(created_at)
            if cutoff and day and day < cutoff:
                continue
            rows.append((rid, created_at, query, pcs, _j(sc) or [], _j(nc) or []))

    print("RETRIEVAL-SUPPLY-PROBE — read-only: recall-gap vs no-doc vs cleaning-bug")
    scope = "whole corpus" if not cutoff else ("created_at >= %s" % cutoff)
    print("  scope: %s   floor: pcs<=%d   N_SAMPLE=%d per bucket" % (scope, FLOOR_MAX, N_SAMPLE))
    print()
    if not rows:
        print("  No rows in scope.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; no writes; PB-API read only.")
        return 0

    # ---- floor rows + buckets (stored-preferred, cheap) ------------------
    def _pcs_val(pcs):
        try:
            return int(pcs) if pcs is not None else None
        except (TypeError, ValueError):
            return None

    bucket_i = []      # (rid, created_at, query, claims)
    bucket_ii = []     # (rid, query, best_item, claims)
    n_floor = 0
    for rid, created_at, query, pcs, cands, claims in rows:
        v = _pcs_val(pcs)
        if v is None or v > FLOOR_MAX:
            continue
        n_floor += 1
        officials = [c for c in cands if _is_dict(c) and c.get("source_type") in OFFICIAL_TYPES]
        best, bucket = _best_and_bucket(officials)
        if bucket.startswith("(i)"):
            bucket_i.append((rid, created_at, query, claims))
        elif bucket.startswith("(ii)"):
            bucket_ii.append((rid, query, best, claims))

    print("  floor rows: %d   bucket-(i) no-candidate: %d   bucket-(ii) short-body: %d"
          % (n_floor, len(bucket_i), len(bucket_ii)))
    print()

    # =======================================================================
    # PART A (PB recall test) REMOVED — historical PB windows are INCONCLUSIVE
    # (see header). This probe now makes ZERO network calls. Recall is deferred to
    # a future RECENT-rows-only probe; "(i) no candidate" rows are read for now as
    # plausibly honestly-LOW. The bucket-(i) count is still reported above.
    print("=== PART A — REMOVED (PB recall deferred; see header). bucket-(i) count shown above. ===")
    print()

    # =======================================================================
    print("=== PART B — bucket (ii) 'short body': cleaning/truncation vs genuinely-short ===")
    sample_ii = bucket_ii[:N_SAMPLE]
    print("  sampling %d of %d bucket-(ii) rows (FULL short body printed; SELECT-only)"
          % (len(sample_ii), len(bucket_ii)))
    abc_hint = collections.Counter()
    for rid, query, best, claims in sample_ii:
        claim, _ci = _claim_for(best, claims)
        claim_text = _claim_text(claim)
        body = _raw_body(best)
        kind = _source_kind(best)
        nav_hits = [m for m in NAV_NOISE_MARKERS if m in body]
        # HINT: (a) nav-noise dominated, (b) truncated real text, (c) genuinely short
        if nav_hits and len(body) < 200:
            hint = "(a) nav-noise?"
        elif body and body[-1] not in ".。!?。" and len(body) >= 150:
            hint = "(b) truncated?"
        else:
            hint = "(c) genuinely-short?"
        abc_hint[hint] += 1
        print("  " + "-" * 76)
        print("  [ii] row=%s  query=%s  source=%s" % (rid, str(query or "")[:50], kind))
        print("    CLAIM (full): %s" % (claim_text or "(empty)"))
        print("    cand title: %s" % (str(best.get("title") or "")[:90] or "(none)"))
        print("    stored body len=%d  nav-noise markers hit: %s"
              % (int(best.get("official_body_length") or len(body)), nav_hits or "none"))
        print("    HINT (human confirms): %s" % hint)
        print("    FULL BODY: %s" % (body or "(empty)"))
    print()
    print("  PART B aggregate (HINT — human confirms via bodies above):")
    for k, n in abc_hint.most_common():
        print("    %-22s : %d" % (k, n))
    print()

    # =======================================================================
    print("=== PART C — crawl mis-attachment: same wrong doc reused across claims ===")
    print("  (ALL floor rows; crawl-lane = official candidate with NO PB/law/FSS marker)")
    # group crawl candidates by normalized title; count DISTINCT claims attached.
    by_title = collections.defaultdict(lambda: {"claims": [], "example_item": None, "urls": set()})
    for rid, created_at, query, pcs, cands, claims in rows:
        v = _pcs_val(pcs)
        if v is None or v > FLOOR_MAX:
            continue
        for c in cands:
            if not _is_dict(c) or c.get("source_type") not in OFFICIAL_TYPES:
                continue
            if _source_kind(c) != "CRAWL":
                continue
            title = sanitize_text(c.get("title") or "")[:120]
            if not title:
                continue
            claim, ci = _claim_for(c, claims)
            ct = _claim_text(claim)
            entry = by_title[title]
            entry["claims"].append((rid, ci, ct))
            entry["urls"].add(_cand_url(c))
            if entry["example_item"] is None:
                entry["example_item"] = c

    ranked = sorted(by_title.items(), key=lambda kv: len({(r, i) for r, i, _ in kv[1]["claims"]}), reverse=True)
    if not ranked:
        print("  (no crawl-lane official candidates on floor rows)")
    else:
        print("  top %d most-reused crawl candidates (by DISTINCT claims attached):" % TOP_REUSED_N)
        for title, entry in ranked[:TOP_REUSED_N]:
            distinct = {(r, i) for r, i, _ in entry["claims"]}
            print("  " + "-" * 76)
            print("  TITLE: %s" % title)
            print("    distinct claims attached: %d   (total attachments: %d)"
                  % (len(distinct), len(entry["claims"])))
            seen = set()
            shown = 0
            for r, i, ct in entry["claims"]:
                if (r, i) in seen:
                    continue
                seen.add((r, i))
                print("      e.g. row %s claim#%d: %s" % (r, i, (ct or "")[:80]))
                shown += 1
                if shown >= EXAMPLES_PER_DOC:
                    break
        # why is the single most-reused doc selected? (cheap stored fields)
        top_title, top_entry = ranked[0]
        it = top_entry["example_item"] or {}
        print("  " + "-" * 76)
        print("  WHY the top doc is selected (stored fields, most-reused='%s'):" % top_title[:60])
        for k in ("retrieval_method", "official_evidence_score", "official_evidence_classification",
                  "official_url_resolution_status", "official_document_relevance_score",
                  "official_body_length", "official_body_match"):
            if k in it:
                print("    %-34s = %s" % (k, it.get(k)))
        print("    url(s): %s" % (sorted(top_entry["urls"])[:2]))
    print()

    print("=== CLOSING — which retrieval lever does the DATA point to? ===")
    print("  bucket-(i) no-candidate=%d, bucket-(ii) short-body=%d of %d floor rows."
          % (len(bucket_i), len(bucket_ii), n_floor))
    print("  - bucket-(i) no-candidate: recall test DEFERRED (PART A removed; historical PB")
    print("    windows inconclusive). Read for now as plausibly honestly-LOW; revisit on RECENT rows.")
    print("  - PART B '(a) nav-noise' dominant -> CRAWL BODY-EXTRACTION/CLEANING bug.")
    print("  - PART B '(b) truncated' dominant -> TRUNCATION bug.   '(c)' -> genuinely short notices.")
    print("  - PART C high reuse counts        -> CRAWL-RELEVANCE-FILTER (same wrong doc spammed).")
    print("  ★ The recall-gap/no-doc and (a)/(b)/(c) calls are DEFERRED to your read of the")
    print("    printed titles/bodies above — the HINTs are NOT trusted (auto mis-split precedent).")
    print()

    print("[Safety] READ-ONLY — SELECT-only DB; ZERO network; no writes/DDL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
