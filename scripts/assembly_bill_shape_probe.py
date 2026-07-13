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
# WHAT IT DOES (read-only GETs, nothing else) — three calls, robust
# (a failed call prints its exact error envelope and the probe CONTINUES):
#   1. 발의법률안 (nzmimeepazxkubdpn) — REQUIRES AGE (대수). We pass AGE=22
#      (the current 대). Prints ALL field names + one full sample row, so we
#      see the metadata shape (BILL_ID / BILL_NAME / PROC_RESULT / COMMITTEE /
#      PROPOSER / DETAIL_LINK expected).
#   2. ★DECISIVE: 의안 상세정보 (BILLINFODETAIL) with the sample row's BILL_ID.
#      Prints ALL field names + the full row — the question is whether it
#      carries 제안이유/주요내용 (or PROPOSAL_REASON / MAIN_CONTENT-like
#      content fields). This is what makes 국회 a real primary-doc source or
#      not.
#   3. If either response carried a DETAIL_LINK / LINK_URL, GET it and report
#      whether the HTML holds a 제안이유/주요내용 block (and note JS-rendered /
#      empty-to-curl pages — the registry already flags browser_automation).
#
# SAFETY: NO DB access, NO pipeline import, NO write of any kind. Prints the
# API key NEVER. Three GETs total. pin-OUT scripts/*; 331/16 unaffected.

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

OPENAPI_BASE = "https://open.assembly.go.kr/portal/openapi"
# 발의법률안 (member-proposed bills) — REQUIRES AGE (대수).
DATASET_BILLS = "nzmimeepazxkubdpn"
# 의안 상세정보 — REQUIRES a bill id (documented param BILL_ID).
DATASET_DETAIL = "BILLINFODETAIL"
# Current 대 (22nd National Assembly, 2024-2028). Overridable via env for a
# probe against an older 대 if needed.
DEFAULT_AGE = "22"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# Long-Korean-text heuristic: a field whose value runs past this length is
# substantive content, not a label/status code.
CONTENT_LEN_HINT = 120
CONTENT_MARKERS = ("제안이유", "주요내용")
# Field-name hints for the decisive content check (Korean APIs vary the
# romanization; substring match, case-insensitive).
CONTENT_FIELD_HINTS = ("REASON", "CONTENT", "SUMMARY", "제안이유", "주요내용")
# Fields that may carry a follow-on detail URL.
LINK_FIELD_HINTS = ("DETAIL_LINK", "LINK_URL", "BILL_URL")


def _build_url(dataset, key, extra=None):
    """Compose an open.assembly.go.kr OpenAPI URL. Standard family params:
    KEY / Type=json / pIndex / pSize, plus any dataset-required extras."""
    params = {"KEY": key, "Type": "json", "pIndex": 1, "pSize": 3}
    if extra:
        params.update(extra)
    return "%s/%s?%s" % (OPENAPI_BASE, dataset, urllib.parse.urlencode(params))


def _redact(url, key):
    return url.replace(key, "<KEY>") if key else url


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_rows(data, dataset):
    """The OpenAPI family nests rows as
    {dataset: [{"head": [...]}, {"row": [...]}]}. Returns the row list (or [])
    and, when present, the error envelope dict."""
    if isinstance(data, dict) and "RESULT" in data:
        return [], data["RESULT"]
    for block in (data.get(dataset) or []) if isinstance(data, dict) else []:
        if isinstance(block, dict) and isinstance(block.get("row"), list):
            return block["row"], None
    return [], None


def _print_row(row):
    """Print every field of one row (full value) and return (content_fields,
    link_url) discovered by the long-text + field-name heuristics."""
    content_fields = []
    link_url = None
    for field, value in row.items():
        text = str(value if value is not None else "")
        upper = field.upper()
        is_content = (len(text) >= CONTENT_LEN_HINT
                      or any(h in upper or h in field for h in CONTENT_FIELD_HINTS))
        marker = "   <-- CONTENT?" if is_content else ""
        if is_content:
            content_fields.append(field)
        print("  %-24s = %.200s%s" % (field, text.replace("\n", " "), marker))
        if link_url is None and text.startswith("http") and any(
                h in upper for h in LINK_FIELD_HINTS):
            link_url = text
    return content_fields, link_url


def _call(dataset, key, extra, label):
    """One OpenAPI GET. Prints the redacted URL, all fields of the first row,
    and never raises — on any failure it prints and returns ([], None)."""
    url = _build_url(dataset, key, extra)
    print("  GET %s" % _redact(url, key))
    try:
        body = _get(url)
        data = json.loads(body)
    except Exception as exc:  # noqa: BLE001 — probe reports and CONTINUES
        print("  [%s] request/parse failed: %s" % (label, type(exc).__name__))
        return [], None
    rows, err = _extract_rows(data, dataset)
    if err is not None:
        print("  [%s] API error envelope: %s"
              % (label, json.dumps(err, ensure_ascii=False)))
        return [], None
    if not rows:
        print("  [%s] no rows — raw head: %.400s" % (label, body))
        return [], None
    content_fields, link_url = _print_row(rows[0])
    print("  candidate content fields: %s"
          % (content_fields or "NONE (metadata/status only)"))
    return rows, link_url


def main() -> int:
    key = (os.environ.get("ASSEMBLY_API_KEY") or "").strip()
    if not key:
        print("ASSEMBLY_API_KEY not set — register a free key at "
              "https://open.assembly.go.kr (인증키 신청) and set the env var.")
        return 0
    age = (os.environ.get("ASSEMBLY_AGE") or DEFAULT_AGE).strip() or DEFAULT_AGE

    # --- Call 1: 발의법률안 (requires AGE) ---------------------------------
    print("== 1. 발의법률안 (nzmimeepazxkubdpn) — AGE=%s ==" % age)
    rows, link1 = _call(DATASET_BILLS, key, {"AGE": age}, "bills")

    # --- Call 2 (DECISIVE): 의안 상세정보 with BILL_ID ---------------------
    print("\n== 2. ★의안 상세정보 (BILLINFODETAIL) — content check ==")
    bill_id = None
    if rows:
        first = rows[0]
        # Prefer BILL_ID; fall back to any BILL*ID-shaped field.
        bill_id = first.get("BILL_ID") or first.get("BILL_NO")
        if bill_id is None:
            for field, value in first.items():
                if "BILL" in field.upper() and "ID" in field.upper():
                    bill_id = value
                    break
    link2 = None
    if not bill_id:
        print("  no BILL_ID in the call-1 sample row — cannot query detail. "
              "(Run call 1 successfully first; check its printed fields.)")
    else:
        print("  using BILL_ID=%s" % bill_id)
        detail_rows, link2 = _call(DATASET_DETAIL, key, {"BILL_ID": bill_id},
                                   "detail")
        if detail_rows:
            blob = json.dumps(detail_rows[0], ensure_ascii=False)
            hit = [m for m in CONTENT_MARKERS if m in blob]
            print("  DECISIVE: 제안이유/주요내용 markers in detail row: %s"
                  % (hit or "NONE — detail is metadata/status only"))

    # --- Call 3: detail-link HTML fallback --------------------------------
    print("\n== 3. detail-link HTML (crawl fallback) ==")
    link_url = link1 or link2
    if not link_url:
        print("  no DETAIL_LINK/LINK_URL in either response — skipping.")
    else:
        print("  GET %s" % link_url)
        try:
            html = _get(link_url)
        except Exception as exc:  # noqa: BLE001
            print("  GET failed: %s" % type(exc).__name__)
            html = ""
        if html:
            found = [m for m in CONTENT_MARKERS if m in html]
            print("  HTTP OK, %d bytes; markers in HTML: %s"
                  % (len(html), found or "NONE"))
            if found:
                idx = html.find(found[0])
                section = re.sub(r"<[^>]+>", " ", html[idx:idx + 4000])
                section = re.sub(r"\s+", " ", section).strip()
                print("  section excerpt (%d chars): %.200s..."
                      % (len(section), section))
            elif len(html) < 2000:
                print("  page is near-empty to curl — likely JS-rendered "
                      "(browser_automation, as the registry already flags).")

    print("\nVERDICT INPUT: call 2's field list is decisive. If BILLINFODETAIL "
          "(or the call-3 HTML) carries 제안이유/주요내용 prose, 국회 is a NEW "
          "primary-doc type -> integrate behind ASSEMBLY_ENABLED. If only "
          "title+status, metadata-only stands and it stays deferred.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
