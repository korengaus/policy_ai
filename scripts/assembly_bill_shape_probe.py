# ASSEMBLY-SOURCE B5c Phase 1 — READ-ONLY shape probe (pin-OUT, GET-only).
#
# DECISIVE QUESTION: does 국회 bill data give us verifiable CONTENT
# (제안이유/주요내용 — the substantive body a claim can be matched against),
# or only metadata (의안명 + 상태)? V1 principle: a source joins only when it
# provides a NEW type of primary official document, so this answer gates the
# whole integration.
#
# The 열린국회정보 API needs a (free) key: https://open.assembly.go.kr
# -> 인증키 신청. Then Joe runs (LOCAL or Worker Shell, either fine):
#
#     set ASSEMBLY_API_KEY=<key>            # PowerShell: $env:ASSEMBLY_API_KEY="<key>"
#     python scripts/assembly_bill_shape_probe.py
#
# WHAT IT DOES (read-only GETs, nothing else):
#   1. Calls the 국회의원 발의법률안 dataset (nzmimeepazxkubdpn) for a few
#      rows and prints EVERY field name + a truncated value — so we see
#      exactly what the API carries (BILL_NAME / PROC_RESULT / COMMITTEE /
#      PROPOSER expected; the question is whether any 제안이유/주요내용-like
#      long-text field exists).
#   2. Follows one row's LINK_URL (the likms bill-detail page) and searches
#      the HTML for the "제안이유 및 주요내용" block — the fallback content
#      path if the API itself is metadata-only. Prints found/not + excerpt
#      length (NOT the full text — this is a shape probe, not ingestion).
#
# SAFETY: NO DB access, NO pipeline import, NO write of any kind. Prints the
# API key NEVER. Two to three GETs total. pin-OUT scripts/*; 331/16 unaffected.

import json
import os
import re
import sys
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

API_BASE = "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# Long-Korean-text heuristic: a field whose value runs past this length is
# substantive content, not a label/status code.
CONTENT_LEN_HINT = 120
CONTENT_MARKERS = ("제안이유", "주요내용")


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main() -> int:
    key = (os.environ.get("ASSEMBLY_API_KEY") or "").strip()
    if not key:
        print("ASSEMBLY_API_KEY not set — register a free key at "
              "https://open.assembly.go.kr (인증키 신청) and set the env var.")
        return 0

    params = urllib.parse.urlencode(
        {"KEY": key, "Type": "json", "pIndex": 1, "pSize": 3})
    print("== 1. 국회의원 발의법률안 dataset field shape ==")
    try:
        body = _get("%s?%s" % (API_BASE, params))
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001 — probe reports and stops
        print("API call failed: %s" % type(exc).__name__)
        return 1
    if "RESULT" in data:  # error envelope (bad key / quota)
        print("API error envelope: %s" % json.dumps(data, ensure_ascii=False))
        return 1

    rows = []
    for block in data.get("nzmimeepazxkubdpn") or []:
        if isinstance(block, dict) and isinstance(block.get("row"), list):
            rows = block["row"]
            break
    if not rows:
        print("no rows in response — dataset shape changed? raw head:")
        print(body[:500])
        return 1

    link_url = None
    content_fields = []
    for field, value in rows[0].items():
        text = str(value or "")
        marker = ""
        if len(text) >= CONTENT_LEN_HINT or any(
                m in field for m in CONTENT_MARKERS):
            content_fields.append(field)
            marker = "   <-- LONG TEXT (substantive content?)"
        print("  %-22s = %.90s%s" % (field, text.replace("\n", " "), marker))
        if field.upper() == "LINK_URL" and text.startswith("http"):
            link_url = text
    print("\n  candidate content fields: %s"
          % (content_fields or "NONE — API is metadata+status only"))

    print("\n== 2. likms detail page (LINK_URL) — the crawl fallback ==")
    if not link_url:
        print("  no LINK_URL field — cannot probe the detail page.")
        return 0
    try:
        html = _get(link_url)
    except Exception as exc:  # noqa: BLE001
        print("  GET %s failed: %s" % (link_url, type(exc).__name__))
        return 1
    found = [m for m in CONTENT_MARKERS if m in html]
    print("  %s -> HTTP OK, %d bytes" % (link_url, len(html)))
    print("  markers found in HTML: %s" % (found or "NONE"))
    if found:
        # Rough length of the section after the first marker (shape only).
        idx = html.find(found[0])
        section = re.sub(r"<[^>]+>", " ", html[idx:idx + 4000])
        section = re.sub(r"\s+", " ", section).strip()
        print("  section excerpt (%d chars stripped): %.200s..."
              % (len(section), section))
        print("\nVERDICT INPUT: bill CONTENT is reachable (API field or "
              "likms HTML above). If the excerpt reads as 제안이유/주요내용 "
              "prose, integration is feasible.")
    else:
        print("\nVERDICT INPUT: no content markers — the detail page is "
              "likely JS-rendered (browser_automation, as the registry "
              "already flags) or content moved. Metadata-only stands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
