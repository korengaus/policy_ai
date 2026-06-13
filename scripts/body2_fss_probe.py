# BODY-2 FSS board anatomy probe — READ-ONLY: <=3 public GETs (the 보도자료 list + 1-2 detail
# pages), 10s timeout, NO Playwright, NO retries, sequential. NO DB, no pipeline. Maps the FSS
# board so a board->detail->body integration can be built correctly:
#   (a) list structure: per-release detail URL + title + date (the extraction pattern),
#   (b) one detail page: is the BODY in static HTML (requests+bs4, no Playwright)? which selector
#       holds it? does it clear the 300-char floor?
#   (c) pagination / search capability: can the list be fetched by recency (pager) or by KEYWORD
#       (a search param)? — the pivotal question for whether matching is clean or needs new logic.
# Inspected from the list HTML (no extra GET) to stay within the <=3-page budget.
import os, re, sys
from urllib.parse import urlparse, urljoin, parse_qs

import requests
from bs4 import BeautifulSoup

BOARD_URL = (sys.argv[1] if len(sys.argv) > 1 else
             os.environ.get("FSS_BOARD_URL",
                            "https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218")).strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}

# FSS board detail-link signal (a /fss/bbs/.../view.do?nttId=... style detail URL).
DETAIL_RE = re.compile(r"(/fss/bbs/.*(view|nttid|bbsid)|view\.do|nttid=|bbsid=)", re.I)
DATE_RE = re.compile(r"(20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2})")
# candidate body containers seen on Korean gov board detail pages (tried in order).
BODY_SELECTORS = [
    ".view_cont", ".bbs_view", ".board_view", ".view_content", ".bd_view", ".cont_view",
    ".board_con", ".view_con", "#content .cont", ".fss_view", "div.view", "td.content",
    "#contents", "#content", "article", ".contents",
]
PAGER_HINTS = ("pageindex", "currentpageno", "pageno", "cpage", "page=", "pageunit")
SEARCH_INPUT_HINTS = ("srchkeyword", "searchkeyword", "searchwrd", "searchcnd", "keyword",
                      "query", "srchtxt", "searchval", "schword")


def dom(u):
    try:
        return (urlparse(u or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def get(u):
    resp = requests.get(u, headers=HEADERS, timeout=10, allow_redirects=True)
    return resp.status_code, (resp.text or "")


def clean(t):
    return re.sub(r"\s+", " ", (t or "")).strip()


# ---------- (a) LIST STRUCTURE ----------
print("=" * 78)
print("BODY-2 FSS board anatomy probe (read-only)")
print("=" * 78)
print("LIST URL:", BOARD_URL)
try:
    status, html = get(BOARD_URL)
except Exception as exc:
    print("  GET error: %s: %s" % (type(exc).__name__, str(exc)[:80]))
    sys.exit(1)
print("  status=%s  html_len=%d" % (status, len(html)))
soup = BeautifulSoup(html, "html.parser")
anchors = soup.find_all("a")
print("  total_anchors=%d" % len(anchors))

items = []
seen = set()
for a in anchors:
    href = (a.get("href") or "").strip()
    onclick = (a.get("onclick") or "").strip()
    title = clean(a.get_text())
    absu = urljoin(BOARD_URL, href) if href and not href.lower().startswith(("#", "javascript")) else ""
    is_detail = bool(absu and dom(absu) == dom(BOARD_URL) and DETAIL_RE.search(absu))
    # capture onclick-id detail links too (goView('123') style) so we know if links are JS-only
    onclick_id = ""
    m = re.search(r"(?:goView|fn_view|view)\D*(\d{3,})", onclick)
    if m:
        onclick_id = m.group(1)
    if not is_detail and not onclick_id:
        continue
    if not title or len(title) < 6:
        continue
    key = absu or onclick_id
    if key in seen:
        continue
    seen.add(key)
    # date: nearest ancestor row text
    row = a.find_parent(["tr", "li", "div"])
    date_m = DATE_RE.search(clean(row.get_text())) if row else None
    items.append({"title": title[:70], "url": absu, "onclick_id": onclick_id,
                  "date": date_m.group(1) if date_m else ""})

href_detail = sum(1 for it in items if it["url"])
onclick_only = sum(1 for it in items if not it["url"] and it["onclick_id"])
print("  detail-candidates: href-based=%d  onclick-only(JS)=%d" % (href_detail, onclick_only))
print("  --- top 10 list items (title / date / url) ---")
for it in items[:10]:
    print("   - %-44s %s" % (it["title"], it["date"]))
    print("       %s" % (it["url"][:96] if it["url"] else "(onclick id=%s, no href -> JS nav)" % it["onclick_id"]))

# ---------- (c) PAGINATION / SEARCH capability (inspected from list HTML; no extra GET) ----------
print("\n--- (c) pagination / search capability (inferred from list HTML) ---")
forms = soup.find_all("form")
search_inputs = []
for f in forms:
    for inp in f.find_all(["input", "select"]):
        nm = (inp.get("name") or "").lower()
        if any(h in nm for h in SEARCH_INPUT_HINTS):
            search_inputs.append(inp.get("name"))
pager_links = [urljoin(BOARD_URL, (a.get("href") or "")) for a in anchors
               if any(h in ((a.get("href") or "") + (a.get("onclick") or "")).lower() for h in PAGER_HINTS)]
base_params = parse_qs(urlparse(BOARD_URL).query)
print("  list-url params:", {k: v[0] for k, v in base_params.items()})
print("  search-form input names found:", sorted(set(search_inputs)) or "NONE")
print("  pager-link signals found:", len(pager_links), "(examples:", [p[:70] for p in pager_links[:2]], ")")
print("  => KEYWORD-SEARCHABLE board?  %s" % (
    "LIKELY (search input present -> clean catalog swap possible)" if search_inputs
    else "NO obvious search input -> DATE-ORDERED only (needs board-mode matching)"))

# ---------- (b) DETAIL BODY extraction (1-2 detail pages) ----------
print("\n--- (b) detail-page body extraction (static, no Playwright) ---")
detail_urls = [it["url"] for it in items if it["url"]][:2]
if not detail_urls:
    print("  NO href-based detail URLs to follow (links are onclick/JS -> detail fetch would need")
    print("  the view-URL pattern reconstructed from the board's JS, or Playwright). STOP.")
else:
    for durl in detail_urls:
        print("\n  DETAIL:", durl[:96])
        try:
            dstatus, dhtml = get(durl)
        except Exception as exc:
            print("    GET error: %s: %s" % (type(exc).__name__, str(exc)[:70]))
            continue
        dsoup = BeautifulSoup(dhtml, "html.parser")
        best_sel, best_text = None, ""
        for sel in BODY_SELECTORS:
            node = dsoup.select_one(sel)
            if node:
                txt = clean(node.get_text(" "))
                if len(txt) > len(best_text):
                    best_text, best_sel = txt, sel
        # fallback: largest <div>/<td> text block
        if len(best_text) < 300:
            for node in dsoup.find_all(["div", "td", "article"]):
                txt = clean(node.get_text(" "))
                if len(txt) > len(best_text):
                    best_text, best_sel = txt, "(largest-block fallback)"
        print("    status=%s  best_selector=%s  body_len=%d  >=300_floor=%s"
              % (dstatus, best_sel, len(best_text), len(best_text) >= 300))
        print("    first200:", best_text[:200])

print("\n" + "=" * 78)
print("READINGS for integration:")
print("  - href-based detail links + a static body selector + >=300 chars  => requests+bs4 path,")
print("    NO Playwright (light). onclick-only links or sub-300 body => harder (view-URL rebuild).")
print("  - search input present => point fss catalog search_url_base at the board SEARCH URL")
print("    (clean swap; the crawl's relevance gate then matches as for fsc/molit).")
print("  - NO search input => board is DATE-ORDERED; the crawl appends the claim query to")
print("    search_url_base (official_source_search.build_official_search_url) which a board ignores")
print("    -> needs board-mode code, and recall is limited to releases recent enough to be on the")
print("    fetched page. Consider FSS data.go.kr open API (keyword-searchable path 2) instead.")
print("=" * 78)
