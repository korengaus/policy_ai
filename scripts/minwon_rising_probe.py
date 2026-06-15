# MINWON-PROBE Phase 1b — ACRC (국민권익위원회) civil-complaint rising-keyword API probe.
# READ-ONLY, THROWAWAY. Outbound HTTPS to data.go.kr ONLY. NO Postgres, NO pipeline,
# NO provider, NO repo-state change. One self-contained script — no abstractions.
#
# WHAT / WHY
# ----------
# Evaluating a NEW external source for hot-topic auto-detection: the ACRC
# civil-complaint big-data API (data.go.kr dataset 15101903, org 1140100, view
# minAnalsInfoView5, op /minRisingKeyword5 = 급등 키워드). Measures BEFORE any
# provider is built (web_search + fss lessons):
#   (a) CONTENT FIT  — policy-relevant terms (전세사기/보조금/대출/부동산) vs
#       daily-life complaint noise (층간소음/주차/쓰레기/포트홀)?
#   (b) GATEWAY STABILITY — clean responses from apis.data.go.kr/1140100 or
#       errors/timeouts? (M23 lesson: apis.data.go.kr/1170000 returned 500s.)
#
# PHASE 1b FIX — CONFIRMED SPEC (from the official Swagger; Phase 1 500'd on
# guessed params: we sent an 8-digit date, the API wants a 10-digit analysisTime).
#   serviceKey  : issued key. Spec example marks it "인증키(URL Encode)" — the
#                 ENCODING form. requests' params= dict would percent-encode it
#                 AGAIN (double-encoding breaks it). So this probe implements TWO
#                 modes and tries (a) then (b), printing which worked:
#                   mode a — serviceKey appended PRE-ENCODED to the URL string
#                            (NOT in params dict; no re-encoding).
#                   mode b — serviceKey placed in the params dict (DECODING key;
#                            requests encodes exactly once).
#   analysisTime: YYYYMMDDHH (TEN digits, includes hour) e.g. 2021050614. Swept
#                 across a few hours per recent date until items come back.
#   maxResult   : 30.
#   target      : analysis-target filter. pttn(일반민원) dfpt(고충민원)
#                 saeol(수집민원) prpl(제안) qna(정책Q&A). Run TWICE per chosen
#                 timestamp: ALL-types vs POLICY-LEANING (qna,prpl,dfpt) to see if
#                 type-filtering yields cleaner policy keywords.
#   dataType    : json.
# Response schema (confirmed): body.items.item = list of objects with fields
#   date, keyword, df, rank, prevRatio, prevDf.
#
# KEY HANDLING: read from env MINWON_API_KEY ONLY. NEVER hardcoded/printed/written.
# Operator sets it inline for ONE Worker-Shell run. No retry-storm, no brute-force.
#
# STOP-FIRST: Phase 1 is this probe only. No provider, no integration.

import os
import sys
import time
import json
from urllib.parse import urlencode
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

ENDPOINT_BASE = "https://apis.data.go.kr/1140100/minAnalsInfoView5"
RISING_OP = "minRisingKeyword5"
RISING_URL = f"{ENDPOINT_BASE}/{RISING_OP}"

TIMEOUT_SECONDS = 10
MAX_RESULT = 30          # spec param maxResult
TOP_N = 30               # how many to display per target run
RAW_SAMPLE = 15          # verbatim item dicts in the raw-sample section

# analysisTime hour-sweep (YYYYMMDDHH). Civil-complaint aggregation updates a few
# times a day; try a few hours per date, stop at the first non-empty.
SWEEP_HOURS = ["23", "18", "12", "09"]
N_DATES = 3              # most-recent N dates to sweep (yesterday back N)

# The two target runs (★ the head-to-head this milestone exists to compare).
TARGET_ALL = "pttn,dfpt,saeol,prpl,qna"     # all types, broadest
TARGET_POLICY = "qna,prpl,dfpt"             # policy-leaning: 정책Q&A + 제안 + 고충민원

# Korea Standard Time — complaints are KST-dated.
_KST = timezone(timedelta(hours=9))


# Cron seed queries — COPIED read-only (do NOT import scheduler.py; it is pin-IN
# for the 331/16 log pins). Authority for IN-SEED vs OUT-OF-SEED. Update this copy
# if scheduler.DEFAULT_QUERIES changes. (Equal to DEFAULT_QUERIES at HEAD 80577b62c4.)
SEED_QUERIES = [
    "주택담보대출 규제",
    "스트레스 DSR 가계부채",
    "전세 공급 대책",
    "청년 정책 지원",
    "양도세 세제 개편",
    "소상공인 지원",
    "복지 예산",
]

# Policy-relevance buckets (substring match) — economy/finance/housing/welfare/
# tax/policy vocabulary our site actually verifies.
_POLICY_MARKERS = (
    "대출", "금리", "DSR", "가계부채", "전세", "전세사기", "주택", "부동산", "주담대",
    "분양", "청약", "임대", "양도세", "종부세", "재건축", "재개발", "LTV",
    "복지", "지원금", "보조금", "수당", "연금", "바우처", "취약계층", "기초생활",
    "소상공인", "자영업", "중소기업", "세금", "세제", "공제", "감면", "과세",
    "금융", "보험", "예금", "신용", "규제", "정책", "지원", "공급", "대책", "보조",
    "고용", "일자리", "최저임금", "임금체불", "노동", "건강보험", "국민연금",
)

# Daily-life civil-complaint buckets — local-life noise we do NOT verify.
_DAILYLIFE_MARKERS = (
    "층간소음", "소음", "주차", "불법주정차", "주정차", "쓰레기", "포트홀", "악취",
    "가로등", "보도블록", "도로", "신호등", "횡단보도", "가로수", "반려동물", "유기견",
    "흡연", "담배", "노상", "현수막", "전단", "방역", "모기", "해충", "맨홀",
    "하수구", "정화조", "민원실", "택배", "배달", "공원", "놀이터", "벤치",
    "에어컨", "난방", "누수", "곰팡이", "층간", "주민", "아파트관리", "관리비",
)


def _classify(keyword, denylist):
    """POLICY / DAILY_LIFE / PERSON / UNCLASSIFIED. PERSON = denylist
    (politician/election/obituary/securities/foreign) — defamation-risk."""
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


def _load_denylist():
    """Read-only import for the PERSON/NAME bucket; degrades to a note if
    unimportable; never reimplemented here."""
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
        "SERVICE_KEY_IS_NOT_REGISTERED", "NOT_REGISTERED", "UNREGISTERED",
        "등록되지 않은", "활용 신청", "SERVICEKEYANDDATA", "INVALID_KEY",
    )) or "REGISTERED" in t


def _http_raw(url, params):
    """GET with browser UA + 10s timeout. params may be None (mode a builds the
    full URL itself). Returns a dict; never raises."""
    start = time.time()
    try:
        resp = requests.get(url, params=params, headers=HEADERS,
                            timeout=TIMEOUT_SECONDS, allow_redirects=True)
        return {"ok": True, "status": resp.status_code, "elapsed": time.time() - start,
                "ctype": resp.headers.get("Content-Type", ""), "text": resp.text or ""}
    except Exception as exc:
        return {"ok": False, "status": None, "elapsed": time.time() - start, "ctype": "",
                "text": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def _request(params_no_key, key, mode):
    """serviceKey double-encoding guard.
       mode 'a' — append PRE-ENCODED serviceKey to the URL string (not in params
                  dict; other params url-encoded once). Use the ENCODING key.
       mode 'b' — serviceKey in the params dict (requests encodes once). Use the
                  DECODING key.
    The same env value is tried both ways so the probe is robust to which form
    the operator pasted."""
    if mode == "a":
        full = RISING_URL + "?serviceKey=" + key + "&" + urlencode(params_no_key)
        return _http_raw(full, None)
    merged = dict(params_no_key)
    merged["serviceKey"] = key
    return _http_raw(RISING_URL, merged)


def _parse_items(text):
    """Parse a data.go.kr response (JSON OR XML) into
    (result_code, result_msg, [item dicts], kind). Never raises."""
    txt = (text or "").strip()
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
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(txt)
        header = root.find(".//header")
        rc = (header.findtext("resultCode") if header is not None else "") or ""
        rm = (header.findtext("resultMsg") if header is not None else "") or ""
        if header is None:  # OpenAPI gateway error envelope (cmmMsgHeader)
            rc = root.findtext(".//returnReasonCode") or rc
            rm = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or rm
        out = []
        for item in root.findall(".//item"):
            d = {}
            for child in list(item):
                d[child.tag.split("}")[-1]] = (child.text or "").strip()
            if d:
                out.append(d)
        return rc.strip(), rm.strip(), out, "xml"
    except Exception:
        return "", "PARSE_ERROR", [], "none"


def _fetch(analysis_time, target, key, mode):
    """One /minRisingKeyword5 call. Returns a normalized result dict."""
    params_no_key = {
        "analysisTime": analysis_time,
        "maxResult": MAX_RESULT,
        "target": target,
        "dataType": "json",
    }
    r = _request(params_no_key, key, mode)
    if not r["ok"]:
        return {"status": None, "elapsed": r["elapsed"], "ctype": "", "rc": "", "rm": "",
                "items": [], "kind": "none", "keyerr": False, "text": "", "error": r.get("error")}
    rc, rm, items, kind = _parse_items(r["text"])
    return {"status": r["status"], "elapsed": r["elapsed"], "ctype": r["ctype"], "rc": rc,
            "rm": rm, "items": items, "kind": kind, "keyerr": _looks_like_key_error(r["text"]),
            "text": r["text"], "error": None}


def _kw_row(item):
    """Confirmed schema accessor: (keyword, df, rank, prevRatio)."""
    return (item.get("keyword", ""), item.get("df", ""),
            item.get("rank", ""), item.get("prevRatio", ""))


def _bucket_report(label, items, denylist):
    """Print POLICY/DAILY_LIFE/PERSON/UNCLASSIFIED for one target run; return
    (policy_count, total, out_of_seed_policy_list)."""
    distinct = sorted({(it.get("keyword") or "") for it in items} - {""})
    buckets = {"POLICY": [], "DAILY_LIFE": [], "PERSON": [], "UNCLASSIFIED": []}
    for k in distinct:
        buckets[_classify(k, denylist)].append(k)
    total = len(distinct)
    pol = len(buckets["POLICY"])
    pol_pct = (100.0 * pol / total) if total else 0.0
    print("  [%s] distinct keywords: %d" % (label, total))
    for b in ("POLICY", "DAILY_LIFE", "PERSON", "UNCLASSIFIED"):
        lst = buckets[b]
        print("    %-13s (%d): %s" % (b, len(lst), ", ".join(lst[:30]) if lst else "(none)"))
    print("    >>> POLICY-RELEVANT: %d/%d (%.0f%%)" % (pol, total, pol_pct))
    out_policy = [k for k in buckets["POLICY"] if not _is_in_seed(k)]
    return pol, total, pol_pct, out_policy


def main():
    api_key = os.environ.get("MINWON_API_KEY", "").strip()
    if not api_key:
        print("ERROR: MINWON_API_KEY is not set.")
        print("Set it inline for ONE Worker-Shell run (bash):")
        print('  MINWON_API_KEY="<paste-key>" python scripts/minwon_rising_probe.py')
        print("Do NOT paste the key in chat. Do NOT register it in Render env yet.")
        return 1

    denylist, denylist_src = _load_denylist()

    today_kst = datetime.now(_KST).date()
    dates = [(today_kst - timedelta(days=d)).strftime("%Y%m%d") for d in range(1, N_DATES + 1)]

    print("=" * 80)
    print("MINWON-PROBE Phase 1b — ACRC rising-keyword API probe (READ-ONLY, throwaway)")
    print("=" * 80)
    print("endpoint :", RISING_URL)
    print("params   : serviceKey(dual-mode), analysisTime=YYYYMMDDHH, maxResult=%d, target, dataType=json"
          % MAX_RESULT)
    print("key      : present (read from MINWON_API_KEY; never printed)")
    print("dates    :", ", ".join(dates), "(KST) x hours", SWEEP_HOURS)
    print("targets  : ALL=%r  vs  POLICY-LEANING=%r" % (TARGET_ALL, TARGET_POLICY))
    print("denylist : %s (%d markers)" % (denylist_src, len(denylist)))
    print()

    # ---- SECTION 1: CONNECTION / GATEWAY CHECK ----------------------------
    # Resolve serviceKey mode (a then b) on the first timestamp, then hour-sweep
    # the recent dates with the broad target to find a non-empty timestamp.
    print("=== 1. CONNECTION / GATEWAY CHECK ===")
    working_mode = None
    key_error_seen = False
    first_at = dates[0] + SWEEP_HOURS[0]
    print("  (1a) serviceKey mode resolution on analysisTime=%s, target=ALL:" % first_at)
    for mode in ("a", "b"):
        f = _fetch(first_at, TARGET_ALL, api_key, mode)
        desc = ("a=URL-appended (ENCODING key)" if mode == "a"
                else "b=params-dict (DECODING key)")
        if f["error"]:
            print("    mode %s [%s] -> TRANSPORT ERROR %s" % (mode, desc, f["error"]))
            continue
        print("    mode %s [%s] -> HTTP %s  %.2fs  parse=%s  rc=%r  rm=%r  items=%d%s"
              % (mode, desc, f["status"], f["elapsed"], f["kind"], f["rc"], f["rm"][:36],
                 len(f["items"]), "  [KEY-ERROR?]" if f["keyerr"] else ""))
        if f["keyerr"]:
            key_error_seen = True
        if f["kind"] != "none" and not f["keyerr"] and (len(f["items"]) > 0 or f["rm"] == "NORMAL SERVICE."):
            working_mode = mode
            break
    if working_mode:
        print("  => WORKING serviceKey mode: %r" % working_mode)
    else:
        print("  => neither mode cleanly succeeded on the first timestamp.")
        if key_error_seen:
            print("  !! SERVICE-KEY ERROR. Try the OTHER key form (Encoding <-> Decoding) in")
            print("     MINWON_API_KEY. mode a expects the ENCODING form; mode b the DECODING")
            print("     form. Do NOT loop. (Proceeding with mode 'b' for the rest, best-effort.)")
        working_mode = working_mode or "b"

    # Hour-sweep with the broad target to find a non-empty (date,hour).
    print("\n  (1b) hour-sweep (mode=%r, target=ALL) until items appear per date:" % working_mode)
    chosen_at = None
    for d in dates:
        for h in SWEEP_HOURS:
            at = d + h
            f = _fetch(at, TARGET_ALL, api_key, working_mode)
            if f["error"]:
                print("    %s -> TRANSPORT ERROR %s" % (at, f["error"]))
                continue
            print("    %s -> HTTP %s  %.2fs  ctype=%s  parse=%s  rc=%r  rm=%r  items=%d"
                  % (at, f["status"], f["elapsed"], (f["ctype"] or "")[:22], f["kind"],
                     f["rc"], f["rm"][:28], len(f["items"])))
            if f["keyerr"]:
                key_error_seen = True
            if len(f["items"]) > 0:
                chosen_at = at
                break
        if chosen_at:
            break
    if chosen_at:
        print("  => chosen analysisTime for the head-to-head: %s" % chosen_at)
    else:
        print("  => no timestamp returned items. Sections 2-6 will be empty; check key/spec above.")
    print()

    # Run the two target sets on the SAME chosen timestamp (fair comparison).
    run_all = _fetch(chosen_at, TARGET_ALL, api_key, working_mode) if chosen_at else None
    run_pol = _fetch(chosen_at, TARGET_POLICY, api_key, working_mode) if chosen_at else None
    items_all = run_all["items"] if run_all else []
    items_pol = run_pol["items"] if run_pol else []

    # ---- SECTION 2: RAW RESPONSE SAMPLE -----------------------------------
    print("=== 2. RAW RESPONSE SAMPLE (verbatim item objects, so schema is visible) ===")
    if items_all:
        print("  analysisTime=%s target=ALL — first %d items:" % (chosen_at, RAW_SAMPLE))
        for i, it in enumerate(items_all[:RAW_SAMPLE]):
            print("    [%2d] %s" % (i, it))
    else:
        print("  (no items on the chosen timestamp for target=ALL — see Section 1.)")
    print()

    # ---- SECTION 3: RISING KEYWORDS (two target runs) ---------------------
    print("=== 3. RISING KEYWORDS — top %d (keyword | df | rank | prevRatio) ===" % TOP_N)
    for label, run, items in (("ALL-types", run_all, items_all),
                              ("POLICY-leaning", run_pol, items_pol)):
        hdr = "rc=%r rm=%r" % (run["rc"], run["rm"]) if run else "no run"
        print("  --- target=%s  (%s)  count=%d ---" % (label, hdr, len(items)))
        if not items:
            print("    (none)")
            continue
        for it in items[:TOP_N]:
            kw, df, rank, pr = _kw_row(it)
            print("    %-20s df=%-6s rank=%-4s prevRatio=%s" % (kw[:20], df, rank, pr))
    print()

    # ---- SECTION 4: POLICY-FIT (per target run) ---------------------------
    print("=== 4. POLICY-FIT CLASSIFICATION (all-types vs policy-leaning) ===")
    pol_all = pol_polrun = None
    out_policy_all = out_policy_pol = []
    if items_all or items_pol:
        pol_a, tot_a, pct_a, out_policy_all = _bucket_report("ALL-types", items_all, denylist)
        pol_p, tot_p, pct_p, out_policy_pol = _bucket_report("POLICY-leaning", items_pol, denylist)
        pol_all, pol_polrun = pct_a, pct_p
        improved = (pct_p - pct_a)
        print("  >>> target-filter effect: policy %% ALL=%.0f%% -> POLICY-leaning=%.0f%%  (%+.0f pts)"
              % (pct_a, pct_p, improved))
    else:
        print("  (no items to classify.)")
    print()

    # ---- SECTION 5: OVERLAP WITH SEEDS (per target run) -------------------
    print("=== 5. OVERLAP WITH OUR CRON SEEDS ===")
    for label, items, out_policy in (("ALL-types", items_all, out_policy_all),
                                     ("POLICY-leaning", items_pol, out_policy_pol)):
        distinct = sorted({(it.get("keyword") or "") for it in items} - {""})
        in_seed = [k for k in distinct if _is_in_seed(k)]
        out_seed = [k for k in distinct if not _is_in_seed(k)]
        print("  --- target=%s ---" % label)
        print("    IN-SEED (%d): %s" % (len(in_seed), ", ".join(in_seed[:30]) if in_seed else "(none)"))
        print("    OUT-OF-SEED (%d): %s" % (len(out_seed), ", ".join(out_seed[:30]) if out_seed else "(none)"))
        print("    OUT-OF-SEED AND POLICY-RELEVANT (%d): %s"
              % (len(out_policy), ", ".join(out_policy[:30]) if out_policy else "(none)"))
    print()

    # ---- SECTION 6: VERDICT -----------------------------------------------
    print("=== 6. VERDICT ===")
    stable = bool(chosen_at) and not key_error_seen
    headline = len(out_policy_pol) if out_policy_pol else len(out_policy_all)
    print("  (a) GATEWAY: chosen_at=%s  key-error-seen=%s  serviceKey-mode=%r -> %s"
          % (chosen_at, key_error_seen, working_mode,
             "looks STABLE" if stable else "UNSTABLE / fix key-form or spec"))
    if pol_all is not None:
        print("  (b) CONTENT FIT: policy%% ALL=%.0f%%  POLICY-leaning=%.0f%%  (filter %+.0f pts)"
              % (pol_all, pol_polrun, pol_polrun - pol_all))
    else:
        print("  (b) CONTENT FIT: no items parsed.")
    print("  (c) NEW SIGNAL — POLICY-AND-OUT-OF-SEED count = %d  <<< headline" % headline)
    if not chosen_at:
        verdict = "INCONCLUSIVE — no items parsed (fix key form / spec, then re-run)."
    elif not stable:
        verdict = "MARGINAL/UNUSABLE on stability — resolve gateway/key first."
    elif (pol_polrun or 0) < 25 and (pol_all or 0) < 25:
        verdict = ("UNUSABLE on content — dominated by daily-life complaint noise; "
                   "policy share too low even with the policy-leaning target filter.")
    elif headline == 0:
        verdict = ("MARGINAL — policy terms appear but none beyond our existing seeds; "
                   "little NEW signal over what the cron already collects.")
    else:
        verdict = ("PROMISING — stable gateway, real policy share, and %d out-of-seed "
                   "policy term(s). A provider MAY be worth building (behind the PERSON/"
                   "NAME denylist filter)." % headline)
    if pol_all is not None and (pol_polrun - pol_all) >= 10:
        verdict += " Target-filtering MEANINGFULLY improves the policy ratio (use qna,prpl,dfpt)."
    elif pol_all is not None:
        verdict += " Target-filtering does NOT meaningfully change the policy ratio."
    print("  PLAIN READ:", verdict)
    print()
    print("[Safety] READ-ONLY throwaway probe — outbound HTTPS only; no DB, no writes, "
          "no provider, key never printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
