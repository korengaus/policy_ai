# MINWON-PROBE Phase 1 — ACRC (국민권익위원회) civil-complaint rising-keyword API probe.
# READ-ONLY, THROWAWAY. Outbound HTTPS to data.go.kr ONLY. NO Postgres, NO pipeline,
# NO provider, NO repo-state change. One self-contained script — no abstractions.
#
# WHAT / WHY
# ----------
# We are evaluating a NEW external source for hot-topic auto-detection: the ACRC
# civil-complaint big-data API (data.go.kr dataset 15101903,
# "민원빅데이터_분석정보_API_2022", org 1140100, view minAnalsInfoView5). The
# candidate operation is /minRisingKeyword5 (급등 키워드 = keywords that surged
# vs. the previous day). This probe MEASURES two things BEFORE any provider is
# built (the web_search + fss lessons: estimate vs measured diverged wildly):
#   (a) CONTENT FIT  — are the rising keywords POLICY-relevant (전세사기/보조금/
#       대출/부동산) or daily-life complaint noise (층간소음/주차/쓰레기/포트홀)?
#   (b) GATEWAY STABILITY — does apis.data.go.kr/1140100 return clean responses,
#       or errors/timeouts? (M23 lesson: apis.data.go.kr/1170000 returned 500s.)
#
# KEY HANDLING (no secrets in repo)
# ---------------------------------
# Service key is read from env MINWON_API_KEY ONLY. NEVER hardcoded, NEVER printed,
# NEVER written to a file. The operator sets it inline for ONE Worker-Shell run.
# data.go.kr keys come in Encoding / Decoding forms; this probe passes the key via
# the requests `params` dict (which single-encodes it — same as
# providers/policy_briefing.py). If the gateway returns a service-key error, it
# prints a one-line hint to try the OTHER key form. It does NOT loop / brute-force.
#
# PARAM NAMES (honest discovery, not silent guessing)
# ---------------------------------------------------
# The exact date-param name for /minRisingKeyword5 is not certain from outside the
# spec, so SECTION 1 TRIES a small bounded set of candidate names ONCE each against
# the most-recent date and PRINTS which (if any) produced parseable items, plus the
# raw response when none do — so the operator can correct the name. No retry-storm.
#
# STOP-FIRST: Phase 1 is this probe only. No provider, no integration.

import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone

import requests

# Browser User-Agent — government gateways often bot-block the default UA (M23).
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json,application/xml;q=0.9,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}

# Base view per the dataset; the rising-keyword operation is appended.
ENDPOINT_BASE = "https://apis.data.go.kr/1140100/minAnalsInfoView5"
RISING_OP = "minRisingKeyword5"
RISING_URL = f"{ENDPOINT_BASE}/{RISING_OP}"

# Secondary op — "오늘의 민원 이슈" (today's civil-complaint issue, sentence-form).
# Its exact path is NOT confirmed from outside the spec, so SECTION 1b best-effort
# TRIES these candidate operation names ONCE on the most-recent date and reports
# which (if any) responded. Focus stays on /minRisingKeyword5.
SECONDARY_OP_CANDIDATES = [
    "minTodayIssue5", "minTodayMinwonIssue5", "minIssueView5",
    "minTodayKeyword5", "minToalIssue5",
]

# Candidate date-param names tried in SECTION 1 (printed, not silently guessed).
CANDIDATE_DATE_PARAMS = ["target_date", "searchDt", "baseDt", "base_date", "stdDt", "regDt"]

TIMEOUT_SECONDS = 10
NUM_OF_ROWS = 50
TOP_N_PER_DATE = 15
N_DATES = 5  # most-recent N dates to scan (API may lag — a few may be empty)

# Korea Standard Time — complaints are KST-dated.
_KST = timezone(timedelta(hours=9))


# Cron seed queries — COPIED read-only (do NOT import scheduler.py; it is pin-IN
# for the 331/16 log pins). Authority for IN-SEED vs OUT-OF-SEED. Update this copy
# if scheduler.DEFAULT_QUERIES changes. (Equal to DEFAULT_QUERIES at HEAD b1f172fb53.)
SEED_QUERIES = [
    "주택담보대출 규제",
    "스트레스 DSR 가계부채",
    "전세 공급 대책",
    "청년 정책 지원",
    "양도세 세제 개편",
    "소상공인 지원",
    "복지 예산",
]

# Policy-relevance buckets (substring match). The economy/finance/housing/welfare/
# tax/policy vocabulary our site actually verifies.
_POLICY_MARKERS = (
    "대출", "금리", "DSR", "가계부채", "전세", "전세사기", "주택", "부동산", "주담대",
    "분양", "청약", "임대", "양도세", "종부세", "재건축", "재개발", "LTV",
    "복지", "지원금", "보조금", "수당", "연금", "바우처", "취약계층", "기초생활",
    "소상공인", "자영업", "중소기업", "세금", "세제", "공제", "감면", "과세",
    "금융", "보험", "예금", "신용", "규제", "정책", "지원", "공급", "대책", "보조",
    "고용", "일자리", "최저임금", "임금체불", "노동", "건강보험", "국민연금",
)

# Daily-life civil-complaint buckets — the local-life noise we do NOT verify.
_DAILYLIFE_MARKERS = (
    "층간소음", "소음", "주차", "불법주정차", "주정차", "쓰레기", "포트홀", "악취",
    "가로등", "보도블록", "도로", "신호등", "횡단보도", "가로수", "반려동물", "유기견",
    "흡연", "담배", "노상", "현수막", "전단", "방역", "모기", "해충", "맨홀",
    "하수구", "정화조", "민원실", "택배", "배달", "공원", "놀이터", "벤치",
    "에어컨", "난방", "누수", "곰팡이", "층간", "주민", "아파트관리", "관리비",
)


def _classify(keyword, denylist):
    """POLICY / DAILY_LIFE / PERSON / UNCLASSIFIED for one keyword. PERSON =
    denylist (politician/election/obituary/securities/foreign) — defamation-risk."""
    k = keyword or ""
    if denylist and any(m in k for m in denylist):
        return "PERSON"
    if any(m in k for m in _POLICY_MARKERS):
        return "POLICY"
    if any(m in k for m in _DAILYLIFE_MARKERS):
        return "DAILY_LIFE"
    return "UNCLASSIFIED"


def _build_seed_tokens():
    toks = set()
    for phrase in SEED_QUERIES:
        toks.add(phrase.strip().lower())
        for word in phrase.split():
            if len(word) >= 2:
                toks.add(word.lower())
    return toks


_SEED_TOKENS = _build_seed_tokens()


def _is_in_seed(keyword):
    k = (keyword or "").lower()
    if not k:
        return False
    return any(k in st or st in k for st in _SEED_TOKENS)


# Optional denylist import (read-only) for the PERSON/NAME bucket. Degrades to a
# note if unimportable; never reimplemented here.
def _load_denylist():
    try:
        from hot_topics import _DENYLIST as dl  # type: ignore
        return tuple(dl), "hot_topics._DENYLIST"
    except Exception:
        try:
            from news_collector import OBITUARY_MARKERS as om  # type: ignore
            return tuple(om), "news_collector.OBITUARY_MARKERS"
        except Exception:
            return (), "none"


def _looks_like_key_error(text):
    """Service-key error signatures from data.go.kr (XML or JSON)."""
    t = (text or "").upper()
    return any(sig in t for sig in (
        "SERVICE_KEY_IS_NOT_REGISTERED", "SERVICEKEY", "SERVICE KEY",
        "NOT_REGISTERED", "UNREGISTERED", "등록되지 않은", "활용 신청",
        "INVALID_REQUEST_PARAMETER_ERROR", "NO_OPENAPI_SERVICE_ERROR",
    )) or "REGISTERED" in t


def _http_get(url, params):
    """GET with browser UA + 10s timeout. Returns dict; never raises."""
    start = time.time()
    try:
        resp = requests.get(url, params=params, headers=HEADERS,
                            timeout=TIMEOUT_SECONDS, allow_redirects=True)
        elapsed = time.time() - start
        return {
            "ok": True, "status": resp.status_code, "elapsed": elapsed,
            "ctype": resp.headers.get("Content-Type", ""), "text": resp.text or "",
        }
    except Exception as exc:
        return {
            "ok": False, "status": None, "elapsed": time.time() - start,
            "ctype": "", "text": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]),
        }


def _parse_items(text):
    """Parse a data.go.kr response (JSON OR XML) into (result_code, result_msg,
    [item dicts], kind). Each item dict = {field_tag: text}. Never raises."""
    txt = (text or "").strip()
    # Try JSON first (dataset is JSON+XML; we request type=json).
    try:
        data = json.loads(txt)
        resp = data.get("response", data) if isinstance(data, dict) else {}
        header = resp.get("header", {}) if isinstance(resp, dict) else {}
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        rc = str(header.get("resultCode", "")).strip()
        rm = str(header.get("resultMsg", "")).strip()
        raw_items = []
        items_node = body.get("items") if isinstance(body, dict) else None
        if isinstance(items_node, dict):
            raw_items = items_node.get("item", [])
        elif isinstance(items_node, list):
            raw_items = items_node
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        out = []
        for it in raw_items or []:
            if isinstance(it, dict):
                out.append({str(k): ("" if v is None else str(v)) for k, v in it.items()})
        return rc, rm, out, "json"
    except Exception:
        pass
    # Fall back to XML.
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(txt)
        header = root.find(".//header")
        rc = (header.findtext("resultCode") if header is not None else "") or ""
        rm = (header.findtext("resultMsg") if header is not None else "") or ""
        # OpenAPI gateway error envelope (cmmMsgHeader) — no body/header pair.
        if header is None:
            rc = root.findtext(".//returnReasonCode") or rc
            rm = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or rm
        out = []
        for item in root.findall(".//item"):
            d = {}
            for child in list(item):
                tag = child.tag.split("}")[-1]
                d[tag] = (child.text or "").strip()
            if d:
                out.append(d)
        return rc.strip(), rm.strip(), out, "xml"
    except Exception:
        return "", "PARSE_ERROR", [], "none"


def _keyword_of(item):
    """Best-effort: pull the keyword string out of an item dict whose schema we
    don't know yet. Prefer a field whose tag looks keyword-ish; else the longest
    Hangul-bearing value."""
    if not item:
        return ""
    for k, v in item.items():
        kl = k.lower()
        if any(s in kl for s in ("keyword", "word", "sword", "rising", "issue", "kwrd")):
            if v:
                return v
    # fallback: longest value that contains Hangul
    best = ""
    for v in item.values():
        if any("가" <= ch <= "힣" for ch in v) and len(v) > len(best):
            best = v
    return best


def main():
    api_key = os.environ.get("MINWON_API_KEY", "").strip()
    if not api_key:
        print("ERROR: MINWON_API_KEY is not set.")
        print("Set it inline for ONE Worker-Shell run (bash):")
        print('  MINWON_API_KEY="<paste-key>" python scripts/minwon_rising_probe.py')
        print("Do NOT paste the key in chat. Do NOT register it in Render env yet.")
        return 1

    denylist, denylist_src = _load_denylist()

    # Recent dates in KST (yesterday back N), YYYYMMDD. The API may lag a day or
    # two, so several recent dates are tried.
    today_kst = datetime.now(_KST).date()
    dates = [(today_kst - timedelta(days=d)).strftime("%Y%m%d") for d in range(1, N_DATES + 1)]

    print("=" * 80)
    print("MINWON-PROBE Phase 1 — ACRC rising-keyword API probe (READ-ONLY, throwaway)")
    print("=" * 80)
    print("endpoint :", RISING_URL)
    print("op       :", RISING_OP, "(dataset 15101903 / org 1140100 / minAnalsInfoView5)")
    print("key      : present (read from MINWON_API_KEY; never printed)")
    print("dates    :", ", ".join(dates), "(KST, yesterday back %d)" % N_DATES)
    print("denylist : %s (%d markers) for PERSON/NAME bucket" % (denylist_src, len(denylist)))
    print()

    # ---- SECTION 1: CONNECTION / GATEWAY CHECK + date-param discovery -------
    print("=== 1. CONNECTION / GATEWAY CHECK ===")
    base_params = {"serviceKey": api_key, "pageNo": 1, "numOfRows": NUM_OF_ROWS, "type": "json"}
    probe_date = dates[0]
    working_param = None
    key_error_seen = False

    print("  (1a) date-param discovery on %s — trying candidate names once each:" % probe_date)
    discovery_raw = {}  # param -> raw text (for printing if none work)
    for pname in CANDIDATE_DATE_PARAMS:
        params = dict(base_params)
        params[pname] = probe_date
        r = _http_get(RISING_URL, params)
        if not r["ok"]:
            print("    %-12s -> TRANSPORT ERROR %s" % (pname, r.get("error")))
            continue
        rc, rm, items, kind = _parse_items(r["text"])
        if _looks_like_key_error(r["text"]):
            key_error_seen = True
        n = len(items)
        print("    %-12s -> HTTP %s  %.2fs  parse=%s  resultCode=%r resultMsg=%r  items=%d"
              % (pname, r["status"], r["elapsed"], kind, rc, rm[:40], n))
        discovery_raw[pname] = r["text"]
        if n > 0 and working_param is None:
            working_param = pname
            break  # first param name that yields items wins; stop (no brute-force)

    if working_param:
        print("  => WORKING date-param: %r" % working_param)
    else:
        print("  => NO candidate date-param produced items. Raw response (first 600 chars)")
        print("     of the FIRST attempt — inspect for the real param name / error:")
        if discovery_raw:
            first_txt = next(iter(discovery_raw.values()))
            print("     " + " ".join(first_txt[:600].split()))
        if key_error_seen:
            print("  !! SERVICE-KEY ERROR detected. data.go.kr keys have TWO forms:")
            print("     try the OTHER form (Encoding <-> Decoding) of MINWON_API_KEY. Do NOT loop.")

    # Per-date status table using the working param (or the first candidate if none
    # worked, so the table still shows gateway behaviour per date).
    use_param = working_param or CANDIDATE_DATE_PARAMS[0]
    print("\n  (1b) per-date gateway behaviour (param=%r):" % use_param)
    per_date_items = {}
    for d in dates:
        params = dict(base_params)
        params[use_param] = d
        r = _http_get(RISING_URL, params)
        if not r["ok"]:
            print("    %s -> TRANSPORT ERROR %s" % (d, r.get("error")))
            per_date_items[d] = []
            continue
        rc, rm, items, kind = _parse_items(r["text"])
        per_date_items[d] = items
        flag = ""
        if _looks_like_key_error(r["text"]):
            flag = "  [KEY-ERROR?]"
        print("    %s -> HTTP %s  %.2fs  ctype=%s  parse=%s  rc=%r  items=%d%s"
              % (d, r["status"], r["elapsed"], (r["ctype"] or "")[:24], kind, rc, len(items), flag))
    print()

    # ---- SECTION 1b: secondary "오늘의 민원 이슈" op discovery (best-effort) --
    print("=== 1c. SECONDARY OP DISCOVERY — '오늘의 민원 이슈' (best-effort) ===")
    print("  exact path unknown from outside spec; trying candidate op names once on %s:" % probe_date)
    secondary_found = None
    for op in SECONDARY_OP_CANDIDATES:
        url = f"{ENDPOINT_BASE}/{op}"
        params = dict(base_params)
        params[use_param] = probe_date
        r = _http_get(url, params)
        if not r["ok"]:
            print("    %-22s -> TRANSPORT ERROR %s" % (op, r.get("error")))
            continue
        rc, rm, items, kind = _parse_items(r["text"])
        no_svc = "NO_OPENAPI_SERVICE" in (r["text"] or "").upper()
        print("    %-22s -> HTTP %s  parse=%s  rc=%r  items=%d%s"
              % (op, r["status"], kind, rc, len(items), "  [no such service]" if no_svc else ""))
        if len(items) > 0 and secondary_found is None:
            secondary_found = op
    if secondary_found:
        print("  => secondary op responding with items: %r" % secondary_found)
    else:
        print("  => no secondary 'today issue' op resolved from the candidate list above.")
        print("     (Operator: confirm the exact op path in the dataset Swagger if this matters.)")
    print()

    # Gather all keywords across dates for sections 2-5.
    first_good_date = next((d for d in dates if per_date_items.get(d)), None)

    # ---- SECTION 2: RAW RESPONSE SAMPLE -----------------------------------
    print("=== 2. RAW RESPONSE SAMPLE (one successful date, verbatim items) ===")
    if first_good_date:
        print("  date=%s — first %d item dicts (full field set, so the schema is visible):"
              % (first_good_date, TOP_N_PER_DATE))
        for i, it in enumerate(per_date_items[first_good_date][:TOP_N_PER_DATE]):
            print("    [%2d] %s" % (i, it))
    else:
        print("  (no date returned items — cannot sample schema. See Section 1 raw output.)")
    print()

    # ---- SECTION 3: RISING KEYWORDS BY DATE -------------------------------
    print("=== 3. RISING KEYWORDS BY DATE (top %d per date) ===" % TOP_N_PER_DATE)
    all_keywords = []  # (date, keyword)
    for d in dates:
        items = per_date_items.get(d) or []
        kws = [_keyword_of(it) for it in items]
        kws = [k for k in kws if k]
        for k in kws:
            all_keywords.append((d, k))
        shown = kws[:TOP_N_PER_DATE]
        print("  %s (%d kw): %s" % (d, len(kws), ", ".join(shown) if shown else "(none)"))
    print()

    # ---- SECTION 4: POLICY-FIT CLASSIFICATION (the key measurement) -------
    print("=== 4. POLICY-FIT CLASSIFICATION (across all returned keywords) ===")
    distinct_kw = sorted({k for _, k in all_keywords})
    buckets = {"POLICY": [], "DAILY_LIFE": [], "PERSON": [], "UNCLASSIFIED": []}
    for k in distinct_kw:
        buckets[_classify(k, denylist)].append(k)
    total = len(distinct_kw)
    print("  distinct keywords across all dates:", total)
    for label in ("POLICY", "DAILY_LIFE", "PERSON", "UNCLASSIFIED"):
        lst = buckets[label]
        print("  %-13s (%d): %s" % (label, len(lst), ", ".join(lst[:30]) if lst else "(none)"))
    pol = len(buckets["POLICY"])
    pol_pct = (100.0 * pol / total) if total else 0.0
    print("  >>> POLICY-RELEVANT: %d of %d distinct keywords (%.0f%%)" % (pol, total, pol_pct))
    print("  (UNCLASSIFIED = neither policy nor known daily-life marker; operator eyeballs.)")
    print()

    # ---- SECTION 5: OVERLAP WITH OUR SEEDS --------------------------------
    print("=== 5. OVERLAP WITH OUR CRON SEEDS ===")
    in_seed = [k for k in distinct_kw if _is_in_seed(k)]
    out_seed = [k for k in distinct_kw if not _is_in_seed(k)]
    out_policy = [k for k in out_seed if _classify(k, denylist) == "POLICY"]
    print("  IN-SEED  (already covered by DEFAULT_QUERIES): %d" % len(in_seed))
    print("    " + (", ".join(in_seed[:30]) if in_seed else "(none)"))
    print("  OUT-OF-SEED (potential NEW signal): %d" % len(out_seed))
    print("    " + (", ".join(out_seed[:30]) if out_seed else "(none)"))
    print("  OUT-OF-SEED AND POLICY-RELEVANT: %d" % len(out_policy))
    print("    " + (", ".join(out_policy[:30]) if out_policy else "(none)"))
    print()

    # ---- SECTION 6: VERDICT -----------------------------------------------
    print("=== 6. VERDICT ===")
    dates_with_items = sum(1 for d in dates if per_date_items.get(d))
    stable = dates_with_items >= max(1, N_DATES // 2) and not key_error_seen
    print("  (a) GATEWAY: %d/%d dates returned parseable items; key-error seen=%s -> %s"
          % (dates_with_items, N_DATES, key_error_seen,
             "looks STABLE" if stable else "UNSTABLE / needs key-form or param fix"))
    print("  (b) CONTENT FIT: %d/%d distinct keywords policy-relevant (%.0f%%)"
          % (pol, total, pol_pct))
    print("  (c) NEW SIGNAL — POLICY-RELEVANT-and-OUT-OF-SEED keyword count = %d  <<< headline"
          % len(out_policy))
    if total == 0:
        verdict = "INCONCLUSIVE — no items parsed (fix param name / key form, then re-run)."
    elif not stable:
        verdict = "MARGINAL/UNUSABLE on stability — resolve gateway/key/param first."
    elif pol_pct < 25:
        verdict = ("UNUSABLE on content — dominated by daily-life complaint noise; "
                   "policy share too low for our hot-topic engine.")
    elif len(out_policy) == 0:
        verdict = ("MARGINAL — policy terms appear but none beyond our existing seeds; "
                   "little NEW signal over what the cron already collects.")
    else:
        verdict = ("PROMISING — stable gateway, real policy share, and %d out-of-seed "
                   "policy term(s). A provider MAY be worth building (behind the PERSON/"
                   "NAME denylist filter)." % len(out_policy))
    print("  PLAIN READ:", verdict)
    print()
    print("[Safety] READ-ONLY throwaway probe — outbound HTTPS only; no DB, no writes, "
          "no provider, key never printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
