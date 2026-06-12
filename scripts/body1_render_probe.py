# BODY-1 Phase 2 Step 0 — render-probe. READ-ONLY: one SELECT (no writes) + <=11 read-only HTTP GETs
# of PUBLIC government search/list pages (same UA/headers the crawler uses, 10s timeout, NO Playwright,
# NO retries). NO DB writes, no pipeline/resolve. Classifies each generic-routed institution as
# SERVER_RENDERED_PARSEABLE / SERVER_RENDERED_BUT_GENERIC_MISSES / JS_RENDERED_EMPTY / FETCH_FAILED
# so we know which need a SITE_RULES entry vs are structurally hard. Safe to run in the Worker Shell.
import os, json, re, collections
from urllib.parse import urlparse, urljoin

import psycopg
import requests
from bs4 import BeautifulSoup

from official_site_parsers import extract_links_for_site, _rendered_link_result, get_site_key

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

# generic-routed institutions to classify + two known-good server-rendered baselines.
TARGETS = ["moef.go.kr", "mss.go.kr", "police.go.kr", "nts.go.kr", "khug.or.kr",
           "moj.go.kr", "ftc.go.kr", "korea.kr"]
BASELINES = ["fsc.go.kr", "molit.go.kr"]
ALL_DOMAINS = TARGETS + BASELINES

# constructed fallbacks ONLY if the DB has no stored official_search_url for the domain.
FALLBACKS = {
    "moef.go.kr": "https://www.moef.go.kr/nw/nes/nesdta.do?menuNo=4010100",
    "mss.go.kr": "https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=86",
    "police.go.kr": "https://www.police.go.kr/user/nd54882.do",
    "nts.go.kr": "https://www.nts.go.kr/nts/na/ntt/selectNttList.do?mi=40643&bbsId=137821",
    "khug.or.kr": "https://www.khug.or.kr/hug/web/ig/ig/igdc/list.jsp",
    "moj.go.kr": "https://www.moj.go.kr/moj/221/subview.do",
    "ftc.go.kr": "https://www.ftc.go.kr/www/selectReportUserList.do?key=10",
    "korea.kr": "https://www.korea.kr/news/pressReleaseList.do",
    "fsc.go.kr": "https://www.fsc.go.kr/no010101",
    "molit.go.kr": "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}

# loose "looks like a detail/view/board link" heuristic, independent of SITE_RULES.
DETAIL_RE = re.compile(
    r"(view|detail|dtl|/bbs/|/board/|/brd/|/news/|/usr/|nttid|articleno|bbsseq|nttsn|nttseq|"
    r"board_no|art_id|boardseq|seq=|idx=|nttsn=|/no01010)", re.I)
LISTY_RE = re.compile(r"(search|/list|main\.do|/main|/home|login|sitemap|menu|paging)", re.I)


def J(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def dom(u):
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


# --- pick a representative real stored official_search_url per domain (SELECT-only) ---
by_domain = collections.defaultdict(collections.Counter)
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT source_candidates FROM analysis_results ORDER BY id")
    for (sc,) in cur.fetchall():
        for c in (J(sc) or []):
            if not isinstance(c, dict):
                continue
            if c.get("source_type") not in ("official_government", "public_institution"):
                continue
            u = c.get("official_search_url") or c.get("url") or ""
            d = dom(u)
            if d in ALL_DOMAINS and u.startswith("http"):
                by_domain[d][u] += 1


def pick_url(d):
    if by_domain.get(d):
        return by_domain[d].most_common(1)[0][0], "stored_db"
    return FALLBACKS.get(d, ""), "fallback_constructed"


def loose_detail_links(soup, base):
    out = []
    seen = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            continue
        absu = urljoin(base, href)
        if absu in seen:
            continue
        seen.add(absu)
        if dom(absu) != dom(base):
            continue
        if LISTY_RE.search(absu) and not DETAIL_RE.search(absu):
            continue
        if DETAIL_RE.search(absu):
            out.append(absu)
    return out


print("=" * 72)
print("BODY-1 render-probe — server-rendered vs JS classification (read-only)")
print("=" * 72)

for d in ALL_DOMAINS:
    target_url, src = pick_url(d)
    tag = "[BASELINE]" if d in BASELINES else ""
    print("\n#### %s %s  (url_source=%s)" % (d, tag, src))
    if not target_url:
        print("  VERDICT: FETCH_FAILED (no stored url and no fallback)")
        continue
    print("  url:", target_url[:110])
    try:
        resp = requests.get(target_url, headers=HEADERS, timeout=10, allow_redirects=True)
        status = resp.status_code
        html = resp.text or ""
    except Exception as exc:
        print("  GET error: %s: %s" % (type(exc).__name__, str(exc)[:80]))
        print("  VERDICT: FETCH_FAILED")
        continue

    soup = BeautifulSoup(html, "html.parser")
    # (a) current parser rules — static-anchor path AND rendered-selector path
    try:
        static_links = extract_links_for_site(html, target_url, "", "", max_links=10) or []
    except Exception as exc:
        static_links = []
        print("  extract_links_for_site error:", type(exc).__name__)
    try:
        rendered = _rendered_link_result(html, target_url, "", "", 10,
                                         site_key=get_site_key(target_url, "")).get("links", [])
    except Exception as exc:
        rendered = []
        print("  _rendered_link_result error:", type(exc).__name__)
    rules_count = max(len(static_links), len(rendered))
    # (b) loose heuristic
    loose = loose_detail_links(soup, target_url)
    loose_count = len(loose)

    if status != 200:
        verdict = "FETCH_FAILED"
    elif rules_count >= 1:
        verdict = "SERVER_RENDERED_PARSEABLE"
    elif loose_count >= 3:
        verdict = "SERVER_RENDERED_BUT_GENERIC_MISSES"
    else:
        verdict = "JS_RENDERED_EMPTY"

    print("  status=%s  html_len=%d  total_anchors=%d" % (status, len(html), len(soup.find_all('a'))))
    print("  current_rules: static=%d rendered=%d (rules_count=%d)" % (len(static_links), len(rendered), rules_count))
    print("  loose_heuristic_detail_links=%d" % loose_count)
    print("  examples(loose, <=3):")
    for h in loose[:3]:
        print("     ", h[:80])
    print("  VERDICT:", verdict)

print("\n" + "=" * 72)
print("KEY: SERVER_RENDERED_BUT_GENERIC_MISSES => add a SITE_RULES entry (pin-OUT, cheap).")
print("     SERVER_RENDERED_PARSEABLE => already extractable (check why crawl still missed: cap?).")
print("     JS_RENDERED_EMPTY => structurally hard (like fss/bok), defer.")
print("=" * 72)
