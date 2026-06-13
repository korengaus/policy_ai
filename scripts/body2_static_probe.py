# BODY-2 Phase 1 STATIC-ALTERNATIVE probe — READ-ONLY: optional single SELECT (no writes) +
# <=2 read-only HTTP GETs per institution of PUBLIC government board/list/RSS pages (same UA/
# headers the crawler uses, 10s timeout, NO Playwright, NO retries, sequential). NO DB writes,
# no pipeline/resolve. For each crawl-lane institution that currently fails to yield a body, it
# tests whether a STATIC, server-rendered alternative to the JS *search* page exists (path 1):
#   - a 보도자료/게시판 board/list page whose detail links are in the RAW HTML, or
#   - an RSS/Atom feed whose <item>/<entry><link> are the detail URLs.
# Verdict per institution: STATIC_ALT_FOUND (path 1 viable, cheap, no OOM) / NO_STATIC_ALT
# (only path 2 API or path 3 Playwright) / API_KNOWN (path 2 candidate). Safe in Worker Shell.
#
# Reuses the loose detail-link heuristic from scripts/body1_render_probe.py verbatim so the two
# probes agree on what "a detail link" is.
import os, re, json, collections
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

# Same heuristic as body1_render_probe.py (kept verbatim for cross-probe agreement).
DETAIL_RE = re.compile(
    r"(view|detail|dtl|/bbs/|/board/|/brd/|/news/|/usr/|nttid|articleno|bbsseq|nttsn|nttseq|"
    r"board_no|art_id|boardseq|seq=|idx=|nttsn=|/no01010)", re.I)
LISTY_RE = re.compile(r"(search|/list|main\.do|/main|/home|login|sitemap|menu|paging)", re.I)

# B-floor distinct-row impact (already measured in BODY-1) — drives the priority ordering.
ROW_IMPACT = {
    "fss.or.kr": 31, "moef.go.kr": 21, "molit.go.kr": 21, "fsc.go.kr": 18, "mss.go.kr": 11,
    "police.go.kr": 10, "khug.or.kr": 7, "nts.go.kr": 6, "moj.go.kr": 6, "ftc.go.kr": 3,
    "gov.kr": 0,
}

# Known path-2 (API) coverage. The policy_briefing provider (data.go.kr org 1371000) is an
# AGGREGATED feed of CENTRAL-MINISTRY press releases — so central ministries below are very
# likely ALREADY reachable via path 2 (korea.kr lane), independent of any per-site crawl.
# fss/bok are NOT central ministries (financial regulator / central bank) so they are NOT in
# 1371000 and genuinely need path 1/2/3. national_law (law.go.kr) is the other working API.
API_NOTE = {
    "fsc.go.kr": "central-gov press releases likely in policy_briefing(1371000) — verify coverage",
    "moef.go.kr": "central ministry — likely in policy_briefing(1371000)",
    "mss.go.kr": "central ministry — likely in policy_briefing(1371000)",
    "moj.go.kr": "central ministry — likely in policy_briefing(1371000)",
    "nts.go.kr": "central ministry — likely in policy_briefing(1371000)",
    "ftc.go.kr": "central ministry — likely in policy_briefing(1371000)",
    "police.go.kr": "central-gov — possibly in policy_briefing(1371000); verify",
    "fss.or.kr": "NOT a central ministry — not in 1371000; check data.go.kr open API for FSS",
    "bok.or.kr": "central bank — not in 1371000; BOK exposes its own RSS/ECOS API",
    "khug.or.kr": "public institution — check data.go.kr open API",
    "molit.go.kr": "central ministry — likely in policy_briefing(1371000)",
    "gov.kr": "korea.kr aggregated feed = policy_briefing(1371000), ALREADY path 2",
}

# Candidate STATIC alternatives per institution (<=2 GETs each). Board/list 보도자료 pages come
# from the body1_render_probe fallbacks (real, already-seen-parseable for fsc/molit boards);
# the second candidate is a best-guess RSS/feed endpoint to TEST (status reported, never assumed).
CANDIDATES = {
    "fss.or.kr": [
        ("board_보도자료", "https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218"),
        ("rss_guess",     "https://www.fss.or.kr/fss/rss/board.do?bbsId=B0000188"),
    ],
    "moef.go.kr": [
        ("board_보도자료", "https://www.moef.go.kr/nw/nes/nesdta.do?menuNo=4010100"),
        ("rss_guess",     "https://www.moef.go.kr/com/cmm/RSSList.do?bbsId=MOSFBBS_000000000028"),
    ],
    "molit.go.kr": [
        ("board_보도자료", "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp"),
        ("rss_guess",     "https://www.molit.go.kr/rss/news.xml"),
    ],
    "fsc.go.kr": [
        ("board_보도자료", "https://www.fsc.go.kr/no010101"),
        ("rss_guess",     "https://www.fsc.go.kr/rss/no010101.xml"),
    ],
    "mss.go.kr": [
        ("board_보도자료", "https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=86"),
    ],
    "police.go.kr": [
        ("board_보도자료", "https://www.police.go.kr/user/nd54882.do"),
    ],
    "khug.or.kr": [
        ("board",         "https://www.khug.or.kr/hug/web/ig/ig/igdc/list.jsp"),
    ],
    "nts.go.kr": [
        ("board_보도자료", "https://www.nts.go.kr/nts/na/ntt/selectNttList.do?mi=40643&bbsId=137821"),
    ],
    "moj.go.kr": [
        ("board_보도자료", "https://www.moj.go.kr/moj/221/subview.do"),
    ],
    "ftc.go.kr": [
        ("board_보도자료", "https://www.ftc.go.kr/www/selectReportUserList.do?key=10"),
    ],
    "bok.or.kr": [
        ("board_보도자료", "https://www.bok.or.kr/portal/bbs/B0000338/list.do?menuNo=200690"),
        ("rss_guess",     "https://www.bok.or.kr/portal/main/rssView.do?bbsCd=B0000338"),
    ],
    "gov.kr": [
        ("press_list",    "https://www.korea.kr/news/pressReleaseList.do"),
    ],
}


def dom(u):
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def loose_detail_links(soup, base):
    out, seen = [], set()
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


def rss_detail_links(text):
    """If the body looks like RSS/Atom, return the <item>/<entry> detail links (<link>)."""
    if not text:
        return []
    head = text[:2000].lower()
    if not ("<rss" in head or "<feed" in head or "<item" in text[:20000].lower() or "<entry" in text[:20000].lower()):
        return []
    links = re.findall(r"<link[^>]*>(.*?)</link>", text, re.I | re.S)         # RSS <link>text</link>
    links += re.findall(r"<link[^>]+href=[\"']([^\"']+)[\"']", text, re.I)    # Atom <link href=".."/>
    out = []
    for raw in links:
        u = raw.strip()
        if u.startswith("http") and dom(u):
            out.append(u)
    return out


# Optional: report the stored JS search_url per domain for contrast (SELECT-only; skipped if no DB).
stored_search = {}
db_url = os.environ.get("DATABASE_URL")
if db_url:
    try:
        import psycopg
        url = db_url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("SELECT source_candidates FROM analysis_results ORDER BY id")
            counter = collections.defaultdict(collections.Counter)
            for (sc,) in cur.fetchall():
                try:
                    cands = json.loads(sc) if sc else []
                except Exception:
                    cands = []
                for c in cands if isinstance(cands, list) else []:
                    if not isinstance(c, dict) or c.get("source_type") not in ("official_government", "public_institution"):
                        continue
                    u = c.get("official_search_url") or c.get("url") or ""
                    d = dom(u)
                    if d in CANDIDATES and u.startswith("http"):
                        counter[d][u] += 1
            for d, ctr in counter.items():
                stored_search[d] = ctr.most_common(1)[0][0]
    except Exception as exc:
        print("  (DB read skipped: %s: %s)" % (type(exc).__name__, str(exc)[:80]))


print("=" * 78)
print("BODY-2 static-alternative probe (read-only) — ordered by B-floor row impact")
print("=" * 78)

ordered = sorted(CANDIDATES, key=lambda d: -ROW_IMPACT.get(d, 0))
summary = {}
for d in ordered:
    print("\n#### %-12s rows=%-3d   path2_note: %s" % (d, ROW_IMPACT.get(d, 0), API_NOTE.get(d, "")))
    if d in stored_search:
        print("  (stored JS search_url: %s)" % stored_search[d][:90])
    best_verdict = "NO_STATIC_ALT"
    for label, cand in CANDIDATES[d]:
        try:
            resp = requests.get(cand, headers=HEADERS, timeout=10, allow_redirects=True)
            status, text = resp.status_code, (resp.text or "")
        except Exception as exc:
            print("  - %-14s GET error: %s: %s" % (label, type(exc).__name__, str(exc)[:60]))
            continue
        rss = rss_detail_links(text)
        if rss:
            n, kind, examples = len(rss), "rss_links", rss[:3]
        else:
            soup = BeautifulSoup(text, "html.parser")
            loose = loose_detail_links(soup, cand)
            n, kind, examples = len(loose), "loose_detail_links", loose[:3]
        verdict = "STATIC_ALT_FOUND" if n >= 3 else ("WEAK(1-2)" if n >= 1 else "EMPTY")
        if verdict == "STATIC_ALT_FOUND":
            best_verdict = "STATIC_ALT_FOUND"
        elif verdict == "WEAK(1-2)" and best_verdict == "NO_STATIC_ALT":
            best_verdict = "WEAK(1-2)"
        print("  - %-14s status=%-3s %-18s n=%-3d -> %s" % (label, status, kind, n, verdict))
        print("    url:", cand[:92])
        for ex in examples:
            print("       ", ex[:88])
    summary[d] = best_verdict
    print("  INSTITUTION VERDICT:", best_verdict, "| path2:", API_NOTE.get(d, ""))

print("\n" + "=" * 78)
print("SUMMARY (institution -> static-path verdict; rows = B-floor impact)")
print("=" * 78)
recoverable_static = 0
for d in ordered:
    v = summary.get(d, "NO_STATIC_ALT")
    if v == "STATIC_ALT_FOUND":
        recoverable_static += ROW_IMPACT.get(d, 0)
    print("  %-12s rows=%-3d  %-18s  path2_note: %s" % (d, ROW_IMPACT.get(d, 0), v, API_NOTE.get(d, "")))
print("\n  B-floor rows behind a STATIC_ALT_FOUND institution (path-1 jackpot): %d" % recoverable_static)
print("  NOTE: a static-found body only LIFTS the floor if it then MATCHES the claim")
print("        (the R2/matcher caveat) — recovered bodies may still sit low until the")
print("        matcher/relevance handles them. And central ministries flagged above may")
print("        ALREADY be covered by policy_briefing(1371000) path 2 — verify before crawling.")
