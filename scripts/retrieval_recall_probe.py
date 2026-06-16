# RETRIEVAL-RECALL-PROBE — THROWAWAY read-only classification of bucket-(ii)
# "short-body" floor rows into honestly-LOW vs recall-gap, with a stored-only PB
# cross-check. SELECT-only over analysis_results. ZERO network, ZERO writes/DDL.
#
# WHY
# ---
# The floor (~210/274 rows, pcs<=20) is a RETRIEVAL problem, not a matcher
# problem. retrieval_supply_probe split it into bucket-(i) no-candidate and
# bucket-(ii) short-body (best official body len<300). SIX fix 2 stopped MINTING
# the bodyless "official_search_url_candidate" for NEW rows, but the deeper
# question stands: of the bucket-(ii) rows, how many are HONESTLY-LOW (foreign /
# spoken-remark / forecast / opinion -> no Korean official document exists, floor
# is correct) vs RECALL-GAP (a concrete Korean government/agency action / law /
# announcement -> an official doc plausibly EXISTS but retrieval attached only a
# bodyless candidate). This probe DATA-CLASSIFIES every bucket-(ii) row and, for
# the recall-gap candidates, cross-checks whether a policy_briefing candidate was
# ALREADY present in the row's stored source_candidates (ZERO network).
#
# ★ DISCIPLINE (auto-split mis-fired before; id196/id206): the keyword heuristics
#   are TRANSPARENT (listed below) and the CLAIM text is printed beside every
#   classification. The honestly-LOW vs recall-gap call is the OPERATOR's read —
#   the probe prints evidence, never asserts the answer.
#
# Floor predicate + bucket-(ii) definition are REPLICATED from
# scripts/retrieval_supply_probe.py (same floor pcs<=FLOOR_MAX, same
# _best_and_bucket) so the counts MATCH that probe. (Replicated, not imported, to
# keep this script self-contained — see the note printed in the header.)

import os
import json
import sys
from datetime import datetime, timedelta

import psycopg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# --- PURE imports (no network) -----------------------------------------------
from text_utils import sanitize_text
from official_evidence_resolution import (
    _classify_official_evidence,
    _claim_text,
    score_official_url,
)


# ---------------------------------------------------------------------------
# Tunable constants — REPLICATED from retrieval_supply_probe.py (must match).
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 0            # 0 = whole corpus
FLOOR_MAX = 20              # floor = policy_confidence_score <= 20 (min(20) clamp)
HAS_BODY_MIN_CHARS = 300
MEDIUM_SCORE_BAR = 55
STRONG_MEDIUM = {"strong_official_direct_support", "medium_official_contextual_support"}
OFFICIAL_TYPES = {"official_government", "public_institution"}

# Stored primary-document markers (real key names — verified in the providers).
PB_MARKER = "policy_briefing_news_item_id"   # providers/policy_briefing.py
LAW_MARKER = "national_law_mst"
FSS_MARKER = "fss_bodo_content_id"

DETAIL_CAP = 40             # cap printed per-row detail; FULL counts for all rows
CLAIM_PREVIEW = 120

# ---------------------------------------------------------------------------
# TRANSPARENT classification keyword sets (operator verifies against printed claim).
# ---------------------------------------------------------------------------
# FOREIGN — claim is about a foreign entity / central bank / official => honestly-LOW
# (no Korean official document). Focused on foreign GOV/central-bank/official names,
# NOT generic finance words, to avoid over-classifying KR FX-policy claims.
FOREIGN_KEYWORDS = [
    "ECB", "BOE", "BOJ", "Fed", "FOMC", "연준", "연방준비", "유럽중앙은행", "영란은행",
    "인민은행", "일본은행", "IMF", "OECD", "월가", "뉴욕증시", "나스닥", "다우", "S&P",
    "트럼프", "파월", "라가르드", "바이든", "시진핑", "베센트", "옐런",
    "미국", "유럽", "영국", "일본", "중국", "독일", "프랑스", "유로존", "엔화", "위안화",
    "해외", "외신", "글로벌",
]

# KR agency names — a concrete Korean government/agency.
KR_AGENCY_KEYWORDS = [
    "금융위", "금융위원회", "금감원", "금융감독원", "국토부", "국토교통부",
    "기재부", "기획재정부", "한국은행", "한은", "국세청", "공정위", "공정거래위원회",
    "중기부", "중소벤처기업부", "고용부", "노동부", "행안부", "보건복지부", "복지부",
    "산업부", "과기부", "방통위", "예금보험공사", "예보", "금융당국", "당국", "정부",
]
# KR legislative / law markers — a concrete enacted/animated law action.
KR_LEGISLATIVE_KEYWORDS = [
    "국회", "본회의", "가결", "의결", "통과", "특별법", "시행령", "시행규칙",
    "제정", "개정", "공포", "입법", "법안", "발의",
]
# KR concrete-action verbs — an enacted/announced policy action (NOT a remark).
KR_ACTION_KEYWORDS = [
    "발표", "결정", "시행", "지원", "공급", "도입", "추진", "고시", "공고",
    "승인", "인가", "확정", "마련", "개편", "신설", "출시", "부과", "인상",
    "인하", "완화", "강화", "규제", "단속", "조사", "대책", "방안", "선정",
]
# REMARK / FORECAST / OPINION markers — soft, no enacted policy => honestly-LOW.
REMARK_FORECAST_KEYWORDS = [
    "발언", "전망", "내다봤다", "내다본다", "예상", "관측", "고민", "의견",
    "주장", "간담회", "기자간담회", "우려", "가능성", "분석", "평가", "제언",
    "조언", "촉구", "강조", "언급", "토론회", "세미나", "포럼", "연구원", "교수",
    "전문가", "애널리스트", "증권사", "리서치", "칼럼", "관계자",
]


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
    """REPLICATED from retrieval_supply_probe.py: STORED-PREFERRED best official
    candidate + fall-reason bucket (all from persisted fields)."""
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
    """Transparent keyword classification. Precedence:
      1) FOREIGN  — any foreign-entity keyword.
      2) KR_OFFICIAL_CANDIDATE — a concrete KR action: any legislative keyword, OR
         (a KR agency AND a concrete-action verb). Beats REMARK only when a
         concrete action is present.
      3) REMARK_FORECAST — any remark/forecast/opinion keyword.
      4) UNCLEAR — none matched.
    Returns (label, matched_keywords_for_transparency)."""
    foreign = _hits(text, FOREIGN_KEYWORDS)
    if foreign:
        return "FOREIGN", foreign
    leg = _hits(text, KR_LEGISLATIVE_KEYWORDS)
    agency = _hits(text, KR_AGENCY_KEYWORDS)
    action = _hits(text, KR_ACTION_KEYWORDS)
    if leg or (agency and action):
        return "KR_OFFICIAL_CANDIDATE", sorted(set(leg + agency + action))
    remark = _hits(text, REMARK_FORECAST_KEYWORDS)
    if remark:
        return "REMARK_FORECAST", remark
    return "UNCLEAR", []


def _pb_present(cands):
    """ZERO-network PB cross-check: did ANY stored candidate on this row carry the
    policy_briefing marker (even one that didn't win)?"""
    return any(_is_dict(c) and PB_MARKER in c for c in cands)


def main() -> int:
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — this probe needs the DB (Worker Shell).")
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
            rows.append((rid, query, pcs, _j(sc) or [], _j(nc) or []))

    print("RETRIEVAL-RECALL-PROBE — read-only: bucket-(ii) honestly-LOW vs recall-gap + PB cross-check")
    print("  floor predicate + bucket-(ii) def REPLICATED from retrieval_supply_probe.py (counts should match)")
    print("  scope: %s   floor: pcs<=%d" % ("whole corpus" if not cutoff else ("created_at>=%s" % cutoff), FLOOR_MAX))
    print()
    if not rows:
        print("  No rows in scope.")
        print("\n[Safety] READ-ONLY probe — SELECT-only; ZERO network; no writes/DDL.")
        return 0

    def _pcs_val(pcs):
        try:
            return int(pcs) if pcs is not None else None
        except (TypeError, ValueError):
            return None

    # ---- floor rows + bucket-(ii) subset (replicated predicate) ----------
    n_floor = 0
    bucket_ii = []   # (rid, query, best, claims, cands)
    for rid, query, pcs, cands, claims in rows:
        v = _pcs_val(pcs)
        if v is None or v > FLOOR_MAX:
            continue
        n_floor += 1
        officials = [c for c in cands if _is_dict(c) and c.get("source_type") in OFFICIAL_TYPES]
        best, bucket = _best_and_bucket(officials)
        if bucket.startswith("(ii)"):
            bucket_ii.append((rid, query, best, claims, cands))

    print("  floor rows (pcs<=%d): %d" % (FLOOR_MAX, n_floor))
    print("  bucket-(ii) short-body rows: %d" % len(bucket_ii))
    print()

    # ---- classify every bucket-(ii) row ----------------------------------
    records = []
    for rid, query, best, claims, cands in bucket_ii:
        claim, _ci = _claim_for(best, claims)
        claim_text = _claim_text(claim)
        blob = sanitize_text("%s %s" % (claim_text, query or ""))
        label, matched = _classify_claim(blob)
        pb = _pb_present(cands) if label == "KR_OFFICIAL_CANDIDATE" else None
        records.append((rid, query, label, pb, claim_text, matched))

    # ---- per-row detail (capped) -----------------------------------------
    print("=== bucket-(ii) per-row classification (detail capped at %d; counts cover ALL) ===" % DETAIL_CAP)
    for i, (rid, query, label, pb, claim_text, matched) in enumerate(records[:DETAIL_CAP], start=1):
        pb_str = "" if pb is None else ("  PB_PRESENT=%s" % pb)
        print("  " + "-" * 76)
        print("  [%d] row=%s  class=%s%s" % (i, rid, label, pb_str))
        print("      query: %s" % (str(query or "")[:90]))
        print("      claim: %s" % ((claim_text or "(empty)")[:CLAIM_PREVIEW]))
        if matched:
            print("      matched keywords (transparency): %s" % matched[:12])
    if len(records) > DETAIL_CAP:
        print("  ... (+%d more bucket-(ii) rows; counts below cover all)" % (len(records) - DETAIL_CAP))
    print()

    # ---- aggregates ------------------------------------------------------
    import collections
    label_counts = collections.Counter(r[2] for r in records)
    print("=== AGGREGATE counts (ALL %d bucket-(ii) rows) ===" % len(records))
    for lab in ("FOREIGN", "REMARK_FORECAST", "KR_OFFICIAL_CANDIDATE", "UNCLEAR"):
        print("  %-22s : %d" % (lab, label_counts.get(lab, 0)))
    print()
    kr = [r for r in records if r[2] == "KR_OFFICIAL_CANDIDATE"]
    pb_true = sum(1 for r in kr if r[3] is True)
    pb_false = sum(1 for r in kr if r[3] is False)
    print("  within KR_OFFICIAL_CANDIDATE (%d):" % len(kr))
    print("    PB_CANDIDATE_PRESENT=True  (PB reached row, body didn't win/match): %d" % pb_true)
    print("    PB_CANDIDATE_PRESENT=False (PB never surfaced a candidate)        : %d" % pb_false)
    print()

    # ---- closing ---------------------------------------------------------
    honest = label_counts.get("FOREIGN", 0) + label_counts.get("REMARK_FORECAST", 0)
    print("=== CLOSING — which lever does the DATA point to? (operator confirms via printed claims) ===")
    print("  honestly-LOW signal (FOREIGN + REMARK_FORECAST) = %d / %d bucket-(ii)" % (honest, len(records)))
    print("  recall-gap candidates (KR_OFFICIAL_CANDIDATE)   = %d / %d" % (len(kr), len(records)))
    print("  - If FOREIGN+REMARK_FORECAST dominate -> floor is mostly HONEST LOW; little to fix.")
    print("  - If KR_OFFICIAL_CANDIDATE is a meaningful share AND many are PB_PRESENT=False")
    print("    -> PB never reached those claims = a real recall lever (PB keyword/date-window")
    print("       widening worth investigating).")
    print("  - KR_OFFICIAL_CANDIDATE with PB_PRESENT=True -> PB reached the row but its body")
    print("    didn't match/win = a matching/selection question, NOT a pure recall-supply gap.")
    print("  ★ The honestly-LOW vs recall-gap call is DEFERRED to the operator's read of the")
    print("    printed claims above — the keyword HINTs are NOT trusted as the answer.")
    print()

    print("[Safety] READ-ONLY probe — SELECT-only; ZERO network; no writes/DDL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
