# BODY-2 FSS 보도자료 API + correct-board probe — READ-ONLY public GETs (PART A <=3, PART B <=2;
# 10s timeout, NO Playwright, NO retries, sequential). NO DB, no pipeline. Investigates the
# PREFERRED path-2 route (an official FSS press-release API, like policy_briefing/national_law)
# and locates the correct static 보도자료 board as a path-1 fallback.
#
# AUTH MODELS we already support (for PART C framing):
#   - national_law: a simple FSS-style OC key in env LAW_OC (config.law_oc()).
#   - policy_briefing: a data.go.kr serviceKey in env DATAGOKR_SERVICE_KEY (config.datagokr_service_key())
#     — ALREADY PRESENT. If FSS press releases live on data.go.kr, we may need NO new auth.
#
# KEYLESS test policy: if an endpoint is callable WITHOUT a key, do ONE test GET to see the
# response shape. If it needs a key, DO NOT attempt — just report the schema + auth requirement.
import os, re, sys
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}

API_DOC_URL = "https://www.fss.or.kr/fss/api/apiInquiryBodoInfo/view.do?menuNo=200281"
DATAGOKR_SEARCH = "https://www.data.go.kr/tcs/dss/selectDataSetList.do?keyword=" + \
                  requests.utils.quote("금융감독원 보도자료")
# PART B candidate press-release boards (the pager example pointed at B0000182/menuNo=200511).
BOARD_CANDIDATES = [
    "https://www.fss.or.kr/fss/bbs/B0000182/list.do?menuNo=200511",
    "https://www.fss.or.kr/fss/bbs/B0000206/list.do?menuNo=200218",
]

ENDPOINT_RE = re.compile(r"https?://[^\s\"'<>]*openapi[^\s\"'<>]*\.jsp", re.I)
ANY_JSP_RE = re.compile(r"https?://[^\s\"'<>]+\.jsp[^\s\"'<>]*", re.I)
AUTH_HINTS = ("인증키", "apikey", "api key", "발급", "serviceKey", "service key", "key=", "신청", "회원가입", "승인")
FORMAT_HINTS = {"xml": ("xml", "<response", "<result"), "json": ("json", "application/json", "{\"")}
PARAM_LABELS = ("파라미터", "요청변수", "요청 변수", "parameter", "request", "검색", "keyword", "날짜",
                "date", "페이지", "page", "pageno", "numofrows", "startdate", "enddate")
DATE_RE = re.compile(r"(20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2})")
DETAIL_RE = re.compile(r"(/fss/bbs/.*(view|nttid|bbsid)|view\.do|nttid=|bbsid=)", re.I)
BODY_SELECTORS = [".view_cont", ".bbs_view", ".board_view", ".view_content", ".bd_view",
                  ".cont_view", "#content", "#contents", "article", ".contents", "td.content"]


def dom(u):
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def get(u):
    resp = requests.get(u, headers=HEADERS, timeout=10, allow_redirects=True)
    ctype = resp.headers.get("Content-Type", "")
    return resp.status_code, (resp.text or ""), ctype


def clean(t):
    return re.sub(r"\s+", " ", (t or "")).strip()


def context_lines(text, needle, span=60):
    out = []
    low = text.lower()
    start = 0
    for _ in range(3):
        i = low.find(needle.lower(), start)
        if i < 0:
            break
        out.append(clean(text[max(0, i - span):i + span]))
        start = i + len(needle)
    return out


print("=" * 80)
print("BODY-2 FSS 보도자료 API + board probe (read-only)")
print("=" * 80)

# ===================== PART A — FSS 보도자료 API =====================
print("\n##### PART A — FSS 보도자료 API discovery #####")
print("API doc:", API_DOC_URL)
endpoints, params_found, auth_found, fmt_found = [], [], [], []
try:
    status, html, ctype = get(API_DOC_URL)
    print("  status=%s  ctype=%s  html_len=%d" % (status, ctype, len(html)))
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ")
    # (1) endpoint URL(s)
    endpoints = sorted(set(ENDPOINT_RE.findall(html)) or set(ANY_JSP_RE.findall(html)))
    print("  endpoint candidate(s):")
    for e in endpoints[:6]:
        print("     ", e[:110])
    if not endpoints:
        print("     (none auto-extracted — inspect the page's request-URL example manually)")
    # (2) request params (scan labels + any <table> header cells / code blocks)
    for lab in PARAM_LABELS:
        if lab.lower() in text.lower():
            params_found.append(lab)
    code_blocks = [clean(c.get_text(" "))[:200] for c in soup.find_all(["code", "pre"])][:4]
    print("  param/label hints present:", sorted(set(params_found)) or "NONE")
    if code_blocks:
        print("  code/pre blocks (request/response examples):")
        for c in code_blocks:
            print("     ", c)
    # (3) auth model
    for h in AUTH_HINTS:
        if h.lower() in text.lower():
            auth_found.append(h)
    print("  AUTH hints found:", sorted(set(auth_found)) or "NONE (may be keyless — verify)")
    for ctxt in context_lines(text, "인증키") or context_lines(text, "apiKey"):
        print("     auth-context:", ctxt)
    # (4) response format
    for fmt, sigs in FORMAT_HINTS.items():
        if any(s.lower() in html.lower() for s in sigs):
            fmt_found.append(fmt)
    print("  response format hints:", sorted(set(fmt_found)) or "unknown")
except Exception as exc:
    print("  GET error: %s: %s" % (type(exc).__name__, str(exc)[:80]))

# Optional ONE keyless test GET — only if an endpoint exists AND no auth hint was seen.
if endpoints and not auth_found:
    test = endpoints[0]
    print("\n  KEYLESS TEST GET (no auth hint seen):", test[:100])
    try:
        st, body, ct = get(test)
        print("    status=%s ctype=%s len=%d" % (st, ct, len(body)))
        print("    first240:", clean(body)[:240])
    except Exception as exc:
        print("    test GET error: %s: %s" % (type(exc).__name__, str(exc)[:70]))
else:
    print("\n  (no keyless test: %s) -> report auth requirement, do NOT call with a key here."
          % ("auth hint present" if auth_found else "no endpoint auto-extracted"))

# data.go.kr cross-check (M23 lesson: check the institution AND data.go.kr).
print("\n  data.go.kr cross-check:", DATAGOKR_SEARCH[:90])
try:
    st, dhtml, _ = get(DATAGOKR_SEARCH)
    dsoup = BeautifulSoup(dhtml, "html.parser")
    titles = [clean(a.get_text()) for a in dsoup.find_all("a")
              if "보도" in a.get_text() or "금융감독" in a.get_text() or "FSS" in a.get_text()]
    titles = [t for t in titles if 6 <= len(t) <= 80][:8]
    print("    status=%s  candidate dataset titles:" % st)
    for t in titles:
        print("       ", t)
    if not titles:
        print("       (none matched — search data.go.kr manually for 'FSS 보도자료')")
    print("    NOTE: if a data.go.kr dataset exists, auth = DATAGOKR_SERVICE_KEY (ALREADY present).")
except Exception as exc:
    print("    data.go.kr GET error: %s: %s" % (type(exc).__name__, str(exc)[:70]))

# ===================== PART B — correct 보도자료 board (fallback) =====================
print("\n##### PART B — correct FSS 보도자료 board (path-1 fallback) #####")
chosen_detail = ""
for cand in BOARD_CANDIDATES[:2]:
    print("\n  board candidate:", cand)
    try:
        st, bhtml, _ = get(cand)
    except Exception as exc:
        print("    GET error: %s: %s" % (type(exc).__name__, str(exc)[:70]))
        continue
    bsoup = BeautifulSoup(bhtml, "html.parser")
    titles, detail_urls = [], []
    for a in bsoup.find_all("a"):
        t = clean(a.get_text())
        href = (a.get("href") or "").strip()
        absu = urljoin(cand, href) if href and not href.lower().startswith(("#", "javascript")) else ""
        if t and len(t) >= 6 and absu and dom(absu) == dom(cand) and DETAIL_RE.search(absu):
            titles.append(t)
            detail_urls.append(absu)
    api_like = sum(1 for t in titles if "api" in t.lower())
    bodo_like = sum(1 for t in titles if ("보도" in t or "발표" in t or "안내" in t or "결과" in t))
    print("    status=%s detail_links=%d  api_like_titles=%d  press_like_titles=%d"
          % (st, len(titles), api_like, bodo_like))
    for t in titles[:6]:
        print("       -", t[:64])
    verdict = "PRESS_RELEASE_BOARD" if (titles and api_like <= len(titles) // 4 and bodo_like >= 1) \
        else ("API_CATALOG (wrong board)" if api_like else "UNCLEAR")
    print("    BOARD VERDICT:", verdict)
    if verdict == "PRESS_RELEASE_BOARD" and detail_urls and not chosen_detail:
        chosen_detail = detail_urls[0]

if chosen_detail:
    print("\n  detail body check:", chosen_detail[:96])
    try:
        st, dhtml, _ = get(chosen_detail)
        dsoup = BeautifulSoup(dhtml, "html.parser")
        best_sel, best_text = None, ""
        for sel in BODY_SELECTORS:
            node = dsoup.select_one(sel)
            if node and len(clean(node.get_text(" "))) > len(best_text):
                best_text, best_sel = clean(node.get_text(" ")), sel
        if len(best_text) < 300:
            for node in dsoup.find_all(["div", "td", "article"]):
                txt = clean(node.get_text(" "))
                if len(txt) > len(best_text):
                    best_text, best_sel = txt, "(largest-block fallback)"
        print("    status=%s selector=%s body_len=%d >=300=%s" % (st, best_sel, len(best_text), len(best_text) >= 300))
        print("    first200:", best_text[:200])
    except Exception as exc:
        print("    detail GET error: %s: %s" % (type(exc).__name__, str(exc)[:70]))
else:
    print("\n  (no confirmed press-release board among candidates -> search FSS site for the real")
    print("   보도자료 bbsId/menuNo, or prefer the PART A API route.)")

print("\n" + "=" * 80)
print("RECOMMENDATION INPUTS:")
print("  - PART A endpoint + auth: keyless or LAW_OC-style key or DATAGOKR_SERVICE_KEY(already have)")
print("    + keyword/date params => CLEAN path-2 provider (like policy_briefing/national_law),")
print("    keyword-driven, no recency/wrong-doc problem. PREFER THIS if auth is obtainable.")
print("  - PART B board only matters as fallback if the API needs unobtainable registration.")
print("  - 46 FSS floor rows: a keyword API can address the rows that have a real matching FSS")
print("    release; rows with no FSS release stay floor regardless (not a supply problem).")
print("=" * 80)
