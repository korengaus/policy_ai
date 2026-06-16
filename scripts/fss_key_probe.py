# FSS-KEY-PROBE — THROWAWAY read-only probe of the FSS 보도자료 (bodoInfo) press-
# release API. Verifies the newly-issued FSS_API_KEY authenticates and returns
# FULL-BODY press releases BEFORE any provider is built (the established
# external-API-probe-before-provider pattern: web_search probe, fss screen-confirm).
#
# HARD SAFETY (enforced below)
# ----------------------------
#   * Reads FSS_API_KEY from os.environ ONLY — NEVER hard-coded, NEVER printed.
#   * READ-ONLY: GET requests to the FSS API only; NO DB, NO file writes, NO
#     pipeline / verdict / provider code touched. scripts/ only (pin-OUT).
#   * At most 2 GETs total (json then, on failure, xml) — respects the 30/day,
#     1-month-range limit. No retry storm.
#   * Clean failure: on non-200 / auth fail / zero items / transport error, print
#     a clear diagnostic and exit 0 (never crash).
#
# SCREEN-CONFIRMED SHAPE (to verify against reality):
#   endpoint  fss.or.kr/fss/kr/openApi/api/bodoInfo.jsp
#   params    apiType(xml/json) / startDate / endDate / authKey(32-char)
#   item      subject + contentsKor(FULL BODY) + publishOrg + originUrl + regDate

import os
import sys
from datetime import datetime, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


FSS_ENDPOINT = "https://www.fss.or.kr/fss/kr/openApi/api/bodoInfo.jsp"

# Browser-like UA — FSS/gov sites often require it (M23 law.go.kr lesson).
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}

TIMEOUT = 10            # seconds; mirrors body2_fss_api_probe.py
LOOKBACK_DAYS = 7       # one small recent window
PREVIEW_CHARS = 200     # body-preview length
MAX_ITEMS_SHOWN = 3     # first N items summarized

# Candidate field names (screen-confirmed primary + lenient fallbacks). The probe
# reports the ACTUAL keys it observes so the provider is built against reality.
SUBJECT_KEYS = ("subject", "title")
BODY_KEYS = ("contentsKor", "contents", "contentKor", "content")
ORG_KEYS = ("publishOrg", "org", "deptName")
URL_KEYS = ("originUrl", "url", "originalUrl")
DATE_KEYS = ("regDate", "date", "publishDate")


def _pick(d, keys):
    """First non-empty value among keys in dict d, else ''. Tolerant of non-dict."""
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return ""


def _clean(t):
    return " ".join(str(t or "").split())


def _date_window():
    end = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _safe_body_snippet(text, n=240):
    """A short, whitespace-collapsed snippet of an API error/body for diagnostics.
    Never the key (the key only ever lives in the params dict, never the body)."""
    return _clean(text)[:n]


def _summarize_items(items, fmt_label):
    """Print the safe per-item summary. items = list of dicts (json) or a list of
    dicts built from XML children. Returns the count summarized."""
    print("  items returned: %d" % len(items))
    if not items:
        print("  >>> ZERO items for this window — key may be valid but window empty,")
        print("      or auth/format issue. See diagnostic above. (Not necessarily a failure.)")
        return 0
    # Report the ACTUAL keys on the first item so the provider matches reality.
    first = items[0] if isinstance(items[0], dict) else {}
    print("  observed keys on first item: %s" % (sorted(first.keys()) if first else "(non-dict item)"))
    print("  --- first %d item(s) (safe fields only) ---" % min(MAX_ITEMS_SHOWN, len(items)))
    shown_preview = False
    for i, it in enumerate(items[:MAX_ITEMS_SHOWN]):
        subject = _clean(_pick(it, SUBJECT_KEYS))
        org = _clean(_pick(it, ORG_KEYS))
        regdate = _clean(_pick(it, DATE_KEYS))
        url = _clean(_pick(it, URL_KEYS))
        body = _pick(it, BODY_KEYS)
        body_len = len(str(body or ""))
        print("  [%d] subject : %s" % (i + 1, subject[:90] or "(none)"))
        print("      publishOrg=%s  regDate=%s" % (org or "(none)", regdate or "(none)"))
        print("      originUrl : %s" % (url[:110] or "(none)"))
        print("      contentsKor length: %d chars" % body_len)
        if not shown_preview and body_len > 0:
            print("      body preview (first %d chars): %s"
                  % (PREVIEW_CHARS, _clean(body)[:PREVIEW_CHARS]))
            shown_preview = True
    if not shown_preview:
        print("  >>> WARNING: no item carried a non-empty body (contentsKor) — the API may")
        print("      return list stubs here; the provider would need the detail/body field.")
    return min(MAX_ITEMS_SHOWN, len(items))


def _try_json(params):
    """ONE GET with apiType=json. Returns (ok, items, note). Never raises."""
    p = dict(params, apiType="json")
    try:
        resp = requests.get(FSS_ENDPOINT, params=p, headers=HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        return False, [], "transport error: %s: %s" % (type(exc).__name__, str(exc)[:120])
    status = resp.status_code
    ctype = resp.headers.get("Content-Type", "")
    print("  [json attempt] status=%s content-type=%s len=%d" % (status, ctype, len(resp.text or "")))
    if status != 200:
        return False, [], "HTTP %s; body: %s" % (status, _safe_body_snippet(resp.text))
    try:
        data = resp.json()
    except Exception as exc:
        return False, [], ("not JSON (%s); first240: %s"
                           % (type(exc).__name__, _safe_body_snippet(resp.text)))
    # Locate the item list — JSON gov APIs nest under varying keys; search common
    # shapes, then any list-of-dicts value, and report what we found.
    items = _extract_items_from_json(data)
    if items is None:
        return False, [], ("JSON parsed but no item-list found; top-level keys: %s"
                           % (sorted(data.keys()) if isinstance(data, dict) else type(data).__name__))
    return True, items, "json OK"


def _extract_items_from_json(data):
    """Best-effort: return a list of item dicts from a parsed JSON body, or None.
    Reports nothing here (caller reports); pure structure walk."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return None
    # Common gov-API nestings to try in order.
    for key in ("result", "results", "list", "items", "data", "body", "response"):
        v = data.get(key)
        if isinstance(v, list) and any(isinstance(x, dict) for x in v):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            inner = _extract_items_from_json(v)
            if inner:
                return inner
    # Fallback: the first value that is a list of dicts.
    for v in data.values():
        if isinstance(v, list) and any(isinstance(x, dict) for x in v):
            return [x for x in v if isinstance(x, dict)]
    return None


def _try_xml(params):
    """ONE GET with apiType=xml, parsed by stdlib. Returns (ok, items, note)."""
    import xml.etree.ElementTree as ET

    p = dict(params, apiType="xml")
    try:
        resp = requests.get(FSS_ENDPOINT, params=p, headers=HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        return False, [], "transport error: %s: %s" % (type(exc).__name__, str(exc)[:120])
    status = resp.status_code
    ctype = resp.headers.get("Content-Type", "")
    print("  [xml attempt]  status=%s content-type=%s len=%d" % (status, ctype, len(resp.text or "")))
    if status != 200:
        return False, [], "HTTP %s; body: %s" % (status, _safe_body_snippet(resp.text))
    try:
        root = ET.fromstring(resp.text or "")
    except Exception as exc:
        return False, [], ("not parseable XML (%s); first240: %s"
                           % (type(exc).__name__, _safe_body_snippet(resp.text)))
    # Find the repeated leaf element that looks like an item: pick the tag whose
    # element has the most child fields and occurs more than once, else any
    # element that has both a subject-ish and a body-ish child.
    items = _extract_items_from_xml(root)
    if not items:
        return False, [], ("XML parsed but no repeated item element found; root tag=<%s>, "
                           "child tags: %s" % (root.tag, sorted({c.tag for c in root})))
    return True, items, "xml OK"


def _extract_items_from_xml(root):
    """Return a list of {childtag: text} dicts for the most plausible repeated
    item element. Best-effort; reports via caller."""
    # Count parent elements by the multiplicity of their children's tags.
    candidates = []
    for parent in root.iter():
        kids = list(parent)
        if len(kids) >= 3:  # an item usually has several fields
            candidates.append(parent)
    # Group siblings: find a tag that repeats under some common grandparent.
    best = []
    for parent in root.iter():
        children = list(parent)
        tag_counts = {}
        for c in children:
            tag_counts[c.tag] = tag_counts.get(c.tag, 0) + 1
        repeated = [t for t, n in tag_counts.items() if n >= 1]
        # collect children that themselves have >=3 sub-children (item-like)
        item_like = [c for c in children if len(list(c)) >= 3]
        if len(item_like) > len(best):
            best = item_like
    if not best and candidates:
        # single item case
        best = candidates[:1]
    items = []
    for el in best:
        d = {}
        for field in el:
            d[field.tag] = (field.text or "").strip()
        if d:
            items.append(d)
    return items


def main() -> int:
    key = os.environ.get("FSS_API_KEY")
    if not key:
        print("FSS-KEY-PROBE: FSS_API_KEY not set in this environment.")
        print("  This probe must run in the Render Worker Shell where FSS_API_KEY lives.")
        print("  (Key is read from env only and never printed.)")
        return 0

    start_date, end_date = _date_window()
    # The key lives ONLY in this params dict — never printed, never in any body.
    params = {"startDate": start_date, "endDate": end_date, "authKey": key}

    print("=" * 78)
    print("FSS-KEY-PROBE — read-only FSS bodoInfo press-release API check")
    print("=" * 78)
    print("  endpoint   :", FSS_ENDPOINT)
    print("  date window: %s -> %s  (YYYYMMDD; last %d days)" % (start_date, end_date, LOOKBACK_DAYS))
    print("  authKey    : present in env (length %d; value NOT printed)" % len(key))
    print("  plan       : apiType=json first, then ONE xml fallback on failure (<=2 GETs)")
    print()

    # --- Attempt 1: JSON -------------------------------------------------
    print("Attempt 1 — apiType=json")
    ok, items, note = _try_json(params)
    print("  result: %s" % note)
    if ok:
        print()
        print("AUTH: appears to succeed (HTTP 200 + parseable item list).")
        n = _summarize_items(items, "json")
        print()
        print("OBSERVED SHAPE: format=JSON; %d item(s); summarized %d. Build the provider"
              % (len(items), n))
        print("  against the 'observed keys' line above (not the screen guess).")
        print()
        print("[Safety] READ-ONLY probe — no DB, no writes; key read from env, never printed.")
        return 0

    # --- Attempt 2: XML fallback ----------------------------------------
    print()
    print("Attempt 2 — apiType=xml (json failed: %s)" % note)
    ok, items, note2 = _try_xml(params)
    print("  result: %s" % note2)
    if ok:
        print()
        print("AUTH: appears to succeed (HTTP 200 + parseable XML items).")
        n = _summarize_items(items, "xml")
        print()
        print("OBSERVED SHAPE: format=XML; %d item(s); summarized %d. Build the provider"
              % (len(items), n))
        print("  against the 'observed keys' line above (not the screen guess).")
        print()
        print("[Safety] READ-ONLY probe — no DB, no writes; key read from env, never printed.")
        return 0

    # --- Both attempts failed — clean diagnostic, exit 0 ----------------
    print()
    print("BOTH attempts failed (no usable item list).")
    print("  json note: %s" % note)
    print("  xml  note: %s" % note2)
    print("  Likely causes: wrong date format, auth rejected, endpoint moved, or empty")
    print("  window. The status/body snippets above are the API's own response — use them")
    print("  to adjust (date format / auth param name) before building the provider.")
    print()
    print("[Safety] READ-ONLY probe — no DB, no writes; key read from env, never printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
