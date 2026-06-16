# PB-EXISTENCE-PROBE — THROWAWAY read-only test of whether a policy_briefing (PB)
# document ACTUALLY EXISTS on korea.kr/data.go.kr for the recall-gap candidate
# floor rows. DB access is SELECT-only. The ONLY network is the EXISTING PB
# provider's korea.kr/data.go.kr API path (reused, not reimplemented). NO writes,
# NO DDL, NO crawl, NO LLM, NO other host.
#
# WHY (the fork this resolves)
# ----------------------------
# retrieval_recall_probe classified the 73 bucket-(ii) short-body floor rows:
# FOREIGN 7 / REMARK_FORECAST 9 / KR_OFFICIAL_CANDIDATE 29 / UNCLEAR 28, and ALL
# 29 KR_OFFICIAL rows had PB_CANDIDATE_PRESENT=False. But that only means OUR
# retrieval never surfaced a PB candidate — NOT that no PB doc exists. The fork:
#   * PB doc EXISTS on korea.kr but we didn't fetch it -> REAL recall-gap (a PB
#     keyword/date-window widening would help; worth a careful follow-up).
#   * PB doc does NOT exist                            -> honestly-LOW (STOP).
# This probe resolves it by querying the PB API read-only around each claim's date
# and reporting PB_DOC_FOUND + matched titles for operator relevance-eyeball.
#
# ★ DISCIPLINE: term derivation is transparent and the matched PB TITLES are
#   printed — a "found" doc may still be OFF-TOPIC, so the final recall-gap vs
#   honestly-LOW call is the OPERATOR's read, not the auto flag.
#
# PB IS DATE-WINDOWED BULK, NOT KEYWORD-SEARCH
# --------------------------------------------
# providers/policy_briefing.py fetches ALL ministries' releases in a <=3-day
# window (data.go.kr org 1371000) and selects client-side via _select_documents
# (claim-token overlap >=1). Production's fetch_and_build_policy_briefing_candidates
# anchors windows to NOW; for HISTORICAL rows we instead reuse the provider's real
# lower-level primitives — get_document_provider(...).fetch_press_releases(start,end)
# + _select_documents / _claim_tokens — over 3-day windows around each row's
# created_at (+/- WINDOW_DAYS). Windows are cached so a shared window is fetched
# once. This is the SAME read-only API + the SAME selection logic the pipeline uses.
#
# Floor predicate + bucket-(ii) def + the 4-way classifier are REPLICATED from
# scripts/retrieval_recall_probe.py (self-contained; counts should match).

import os
import json
import sys
import time
import collections
from datetime import datetime, timedelta, date

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# --- PURE imports -------------------------------------------------------------
from text_utils import sanitize_text
from official_evidence_resolution import (
    _classify_official_evidence,
    _claim_text,
    score_official_url,
)
# --- PB provider REAL primitives (reused read-only; NOT reimplemented) --------
from providers.policy_briefing import (
    get_document_provider,
    _select_documents,
    _claim_tokens,
    _doc_tokens,
    DEFAULT_NUM_OF_ROWS,
)


# ---------------------------------------------------------------------------
# Constants — floor/bucket REPLICATED from retrieval_recall_probe.py (must match).
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 0
FLOOR_MAX = 20
HAS_BODY_MIN_CHARS = 300
MEDIUM_SCORE_BAR = 55
STRONG_MEDIUM = {"strong_official_direct_support", "medium_official_contextual_support"}
OFFICIAL_TYPES = {"official_government", "public_institution"}

PB_MARKER = "policy_briefing_news_item_id"
LAW_MARKER = "national_law_mst"
FSS_MARKER = "fss_bodo_content_id"

# --- PB query window + politeness -------------------------------------------
# +/- WINDOW_DAYS around each row's created_at, fetched as 3-day API windows
# (data.go.kr caps a single call at 3 days). Windows are CACHED across rows, so
# the real call count is the number of DISTINCT 3-day windows, not rows*windows.
WINDOW_DAYS = 30
WINDOW_STEP = 3
NUM_OF_ROWS = DEFAULT_NUM_OF_ROWS          # mirror production single-page size
MAX_SELECT = 3                             # top PB matches to consider per row
SLEEP_SECONDS = 0.5                        # gentle pause after each DISTINCT fetch
MAX_WINDOW_FETCHES = 400                   # hard backstop on total API calls

TEST_CAP = 40                              # cap tested rows; full counts printed
CLAIM_PREVIEW = 100

# ---------------------------------------------------------------------------
# TRANSPARENT classification keyword sets (REPLICATED from retrieval_recall_probe).
# ---------------------------------------------------------------------------
FOREIGN_KEYWORDS = [
    "ECB", "BOE", "BOJ", "Fed", "FOMC", "연준", "연방준비", "유럽중앙은행", "영란은행",
    "인민은행", "일본은행", "IMF", "OECD", "월가", "뉴욕증시", "나스닥", "다우", "S&P",
    "트럼프", "파월", "라가르드", "바이든", "시진핑", "베센트", "옐런",
    "미국", "유럽", "영국", "일본", "중국", "독일", "프랑스", "유로존", "엔화", "위안화",
    "해외", "외신", "글로벌",
]
KR_AGENCY_KEYWORDS = [
    "금융위", "금융위원회", "금감원", "금융감독원", "국토부", "국토교통부",
    "기재부", "기획재정부", "한국은행", "한은", "국세청", "공정위", "공정거래위원회",
    "중기부", "중소벤처기업부", "고용부", "노동부", "행안부", "보건복지부", "복지부",
    "산업부", "과기부", "방통위", "예금보험공사", "예보", "금융당국", "당국", "정부",
]
KR_LEGISLATIVE_KEYWORDS = [
    "국회", "본회의", "가결", "의결", "통과", "특별법", "시행령", "시행규칙",
    "제정", "개정", "공포", "입법", "법안", "발의",
]
KR_ACTION_KEYWORDS = [
    "발표", "결정", "시행", "지원", "공급", "도입", "추진", "고시", "공고",
    "승인", "인가", "확정", "마련", "개편", "신설", "출시", "부과", "인상",
    "인하", "완화", "강화", "규제", "단속", "조사", "대책", "방안", "선정",
]
REMARK_FORECAST_KEYWORDS = [
    "발언", "전망", "내다봤다", "내다본다", "예상", "관측", "고민", "의견",
    "주장", "간담회", "기자간담회", "우려", "가능성", "분석", "평가", "제언",
    "조언", "촉구", "강조", "언급", "토론회", "세미나", "포럼", "연구원", "교수",
    "전문가", "애널리스트", "증권사", "리서치", "칼럼", "관계자",
]
# WIDENING NET: strong official-document signals used to pull UNCLEAR rows that
# nonetheless reference an enacted/announced/official-statistics action into the
# tested set (catches genuine docs the 4-way classifier dumped into UNCLEAR —
# e.g. 금감원 저축은행 실적, LH 자료, 양도세 종료). Legislative + action + stats.
OFFICIAL_DOC_SIGNALS = sorted(set(
    KR_LEGISLATIVE_KEYWORDS + KR_ACTION_KEYWORDS + [
        "통계", "실적", "동향", "지표", "발간", "공표", "보도자료", "브리핑", "종료", "연장",
    ]
))


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
    """REPLICATED from retrieval_recall_probe.py."""
    if not officials:
        return None, "(i) no official candidate"
    scored = [(int(c.get("official_evidence_score") or 0), c) for c in officials]
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


def _hits(text, keywords):
    return [k for k in keywords if k in text]


def _classify_claim(text):
    """REPLICATED 4-way classifier (FOREIGN -> KR_OFFICIAL -> REMARK -> UNCLEAR)."""
    foreign = _hits(text, FOREIGN_KEYWORDS)
    if foreign:
        return "FOREIGN"
    leg = _hits(text, KR_LEGISLATIVE_KEYWORDS)
    agency = _hits(text, KR_AGENCY_KEYWORDS)
    action = _hits(text, KR_ACTION_KEYWORDS)
    if leg or (agency and action):
        return "KR_OFFICIAL_CANDIDATE"
    if _hits(text, REMARK_FORECAST_KEYWORDS):
        return "REMARK_FORECAST"
    return "UNCLEAR"


def _windows_for(created_d, today):
    """3-day (start,end) YYYYMMDD windows covering created_d +/- WINDOW_DAYS.
    Skips windows that start in the future (no data); end clamped to today."""
    wins = []
    off = -WINDOW_DAYS
    while off <= WINDOW_DAYS:
        start = created_d + timedelta(days=off)
        end = start + timedelta(days=WINDOW_STEP - 1)
        if start <= today:
            if end > today:
                end = today
            wins.append((start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        off += WINDOW_STEP
    return wins


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe needs the DB (local with $env: or Worker Shell).")
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

    print("PB-EXISTENCE-PROBE — read-only: does a PB doc actually exist for recall-gap rows?")
    print("  floor + bucket-(ii) + classifier REPLICATED from retrieval_recall_probe.py")
    print("  PB entry point reused: providers.policy_briefing.get_document_provider(...)"
          ".fetch_press_releases(start,end) + _select_documents  (REAL production primitives)")
    print("  PB window: created_at +/- %d days as %d-day API windows (cached); num_of_rows=%d"
          % (WINDOW_DAYS, WINDOW_STEP, NUM_OF_ROWS))
    print()
    if not rows:
        print("  No rows in scope.")
        print("\n[Safety] READ-ONLY — DB SELECT-only; network = PB provider API only; no writes/DDL.")
        return 0

    def _pcs_val(pcs):
        try:
            return int(pcs) if pcs is not None else None
        except (TypeError, ValueError):
            return None

    # ---- floor + bucket-(ii) + classify + SELECT tested set --------------
    bucket_ii = []
    for rid, created_at, query, pcs, cands, claims in rows:
        v = _pcs_val(pcs)
        if v is None or v > FLOOR_MAX:
            continue
        officials = [c for c in cands if _is_dict(c) and c.get("source_type") in OFFICIAL_TYPES]
        best, bucket = _best_and_bucket(officials)
        if not bucket.startswith("(ii)"):
            continue
        claim, _ci = _claim_for(best, claims)
        claim_text = _claim_text(claim)
        blob = sanitize_text("%s %s" % (claim_text, query or ""))
        label = _classify_claim(blob)
        bucket_ii.append((rid, created_at, query, claim, claim_text, label, blob))

    # tested set: KR_OFFICIAL_CANDIDATE OR (UNCLEAR with an official-doc signal)
    selected = []
    for rec in bucket_ii:
        rid, created_at, query, claim, claim_text, label, blob = rec
        if label == "KR_OFFICIAL_CANDIDATE":
            selected.append((rec, "KR_OFFICIAL_CANDIDATE"))
        elif label == "UNCLEAR" and _hits(blob, OFFICIAL_DOC_SIGNALS):
            selected.append((rec, "UNCLEAR+officialsignal"))

    print("  bucket-(ii) rows: %d" % len(bucket_ii))
    print("  selected for PB-existence test (KR_OFFICIAL + UNCLEAR-with-signal): %d (cap %d)"
          % (len(selected), TEST_CAP))
    print()

    provider = get_document_provider("policy_briefing")
    if not getattr(provider, "available", False):
        print("  PB provider NOT available: %s" % getattr(provider, "reason", "unknown"))
        print("  -> set POLICY_BRIEFING_ENABLED=true and DATAGOKR_SERVICE_KEY to run the PB test.")
        print("\n[Safety] READ-ONLY — DB SELECT-only; network = PB provider API only; no writes/DDL.")
        return 0

    today = date.today()
    window_cache = {}     # (start,end) -> list[normalized doc]
    fetches = 0

    def _fetch_window(start, end):
        nonlocal fetches
        key = (start, end)
        if key in window_cache:
            return window_cache[key]
        if fetches >= MAX_WINDOW_FETCHES:
            window_cache[key] = []
            return []
        result = provider.fetch_press_releases(start_date=start, end_date=end, num_of_rows=NUM_OF_ROWS)
        docs = result.get("documents") or []
        window_cache[key] = docs
        fetches += 1
        time.sleep(SLEEP_SECONDS)
        return docs

    # ---- per-row PB existence test ---------------------------------------
    print("=== PB-existence per row (detail capped at %d; counts cover all selected) ===" % TEST_CAP)
    results = []
    for idx, (rec, why) in enumerate(selected):
        rid, created_at, query, claim, claim_text, label, blob = rec
        # created date
        if isinstance(created_at, datetime):
            cdate = created_at.date()
        else:
            try:
                cdate = datetime.strptime(_row_date(created_at), "%Y-%m-%d").date()
            except Exception:
                cdate = today
        # gather docs across this row's windows (cached/deduped)
        docs = {}
        for (s, e) in _windows_for(cdate, today):
            for d in _fetch_window(s, e):
                did = d.get("id") or d.get("original_url") or (d.get("title") or "")
                if did and did not in docs:
                    docs[did] = d
        doclist = list(docs.values())
        selected_docs = _select_documents(doclist, [claim], max_releases=MAX_SELECT) if doclist else []
        found = bool(selected_docs)
        ctoks = _claim_tokens([claim])
        terms = sorted(ctoks)[:6]
        titles = []
        for d in selected_docs[:2]:
            ov = len(_doc_tokens(d) & ctoks)
            titles.append("(ovlp=%d) %s" % (ov, str(d.get("title") or "")[:80]))
        results.append((rid, label, why, found, len(doclist)))

        if idx < TEST_CAP:
            print("  " + "-" * 76)
            print("  [%d] row=%s  class=%s (%s)  PB_DOC_FOUND=%s  (docs in windows=%d)"
                  % (idx + 1, rid, label, why, found, len(doclist)))
            print("      claim: %s" % ((claim_text or "(empty)")[:CLAIM_PREVIEW]))
            print("      PB overlap terms (claim tokens): %s" % terms)
            if titles:
                for t in titles:
                    print("      matched PB: %s" % t)
            else:
                print("      matched PB: (none)")
    if len(selected) > TEST_CAP:
        print("  ... (+%d more selected rows tested; counts below cover all)" % (len(selected) - TEST_CAP))
    print()
    print("  distinct PB API window fetches: %d (cap %d)" % (fetches, MAX_WINDOW_FETCHES))
    print()

    # ---- aggregates + closing --------------------------------------------
    n_found = sum(1 for r in results if r[3])
    n_not = sum(1 for r in results if not r[3])
    print("=== AGGREGATE (all %d selected rows) ===" % len(results))
    print("  PB_DOC_FOUND=True  (a PB release exists in-window we failed to surface): %d" % n_found)
    print("  PB_DOC_FOUND=False (no PB release matched in-window)                   : %d" % n_not)
    by_class = collections.Counter((r[1], r[3]) for r in results)
    for (lab, fnd), n in sorted(by_class.items()):
        print("    %-26s found=%s : %d" % (lab, fnd, n))
    print()

    print("=== CLOSING — recall-gap vs honestly-LOW (operator eyeballs the PB titles) ===")
    print("  PB_DOC_FOUND=True = %d / %d selected." % (n_found, len(results)))
    print("  - Meaningful True count -> a REAL recall-gap: PB docs exist on korea.kr that our")
    print("    retrieval didn't surface => a PB recall-widening lever is worth a separate,")
    print("    careful follow-up (verdict-isolated, low risk).")
    print("  - True count ~0 -> floor confirmed HONESTLY-LOW for these rows => STOP is correct.")
    print("  ★ A 'found' doc may still be OFF-TOPIC — the matched PB TITLES above must be")
    print("    eyeballed; the final recall-gap vs honestly-LOW call is the operator's, not the flag.")
    print()

    print("[Safety] READ-ONLY — DB SELECT-only; network = PB provider's korea.kr/data.go.kr API ONLY"
          " (reused, not reimplemented); no crawl/LLM/other host; no writes/DDL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
