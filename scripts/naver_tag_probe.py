#!/usr/bin/env python3
"""TITLE-JOIN Phase 1b — READ-ONLY Naver Search tag-distribution probe.

Run WHERE the Naver credentials live (Render shell):

    python scripts/naver_tag_probe.py

Selftest (no network, no credentials):

    python scripts/naver_tag_probe.py --selftest

Answers ONE decisive question: are the no-space sentence joins observed in
stored claim_text ("...확보한다는 계획이다.경북도는...") caused by BLOCK-level
HTML tags that providers/naver_search._strip_html deletes without inserting a
space — or does Naver's own description text arrive with the sentences already
joined? If the joins are native to Naver's text, no regex change helps and the
TITLE-JOIN item closes permanently.

READ-ONLY GUARANTEES:
  * No DB access, no file writes, no import of any collection module — the
    only repo file touched is scheduler.py, read via ast (no import) for the
    DEFAULT_QUERIES sample.
  * Small footprint: 5 seed queries + 2 decisive-case queries, default page
    size (10), one call each, ~0.4s pacing. Non-200 / transport errors print
    a message and skip — never crash, never retry-hammer.
  * NAVER_CLIENT_ID / NAVER_CLIENT_SECRET are read from the environment and
    NEVER printed, logged, or echoed — not even partially, not even hashed.
"""

from __future__ import annotations

import ast
import html
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

NAVER_NEWS_ENDPOINT = "https://openapi.naver.com/v1/search/news.json"
PACING_SECONDS = 0.4
TIMEOUT_SECONDS = 10.0
SEED_QUERY_COUNT = 5

# Same universe as the proposed (NOT implemented) _strip_html split.
BLOCK_TAGS = {
    "br", "p", "div", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr",
    "section", "article", "dl", "dt", "dd",
}
INLINE_TAGS = {"b", "i", "em", "strong", "span", "u", "sub", "sup",
               "mark", "small", "a", "font"}

# Tag with its name captured; mirrors the shape _TAG_RE deletes.
TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)[^>]*>")

# A join: Hangul + sentence-ending punctuation IMMEDIATELY followed by Hangul
# (no space). This is the exact signature measured in stored claim_text.
JOIN_RE = re.compile(r"(?<=[가-힣])[.!?…](?=[가-힣])")

# THE DECISIVE CASES — stored rows whose claim_text is joined but whose title
# is clean (Phase 1, 2026-07-24). Searching the title should re-surface the
# article; the raw description shows whether a tag sits at the join boundary.
# (Titles are hardcoded so the probe needs NO DB access.)
DECISIVE_CASES = [
    {
        "analysis_id": 13562,
        "title": "경북도, 국립경국대 의대 설립 총력…500병상 부속병원 추진",
        # boundary as stored: "...확보한다는 계획이다." + "경북도는 의과대학..."
        "joined_left": "계획이다",
        "joined_right": "경북도는",
    },
    {
        "analysis_id": 13563,
        "title": "경북도, 필수의료 공백 줄인다… 야간진료 확대·국립의대 추진",
        "joined_left": "계획이다",
        "joined_right": "경북도는",
    },
]

# Fallback if scheduler.py can't be ast-read (kept equal to its head entries).
FALLBACK_QUERIES = [
    "주택담보대출 규제", "스트레스 DSR 가계부채", "전세 공급 대책",
    "청년 정책 지원", "양도세 세제 개편",
]


def read_seed_queries(count: int = SEED_QUERY_COUNT):
    """First `count` scheduler.DEFAULT_QUERIES entries via ast — no import."""
    path = Path(__file__).resolve().parent.parent / "scheduler.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DEFAULT_QUERIES":
                        values = [ast.literal_eval(e) for e in node.value.elts]
                        return [v for v in values if isinstance(v, str)][:count]
    except Exception as exc:
        print(f"note: could not ast-read scheduler.py ({type(exc).__name__}); "
              "using the hardcoded fallback seed list")
    return FALLBACK_QUERIES[:count]


# ---------------------------------------------------------------------------
# Tag classification + join attribution (pure — exercised by --selftest)
# ---------------------------------------------------------------------------

def segment(raw):
    """Split raw HTML-ish text into [(kind, value)] where kind is 'tag'
    (value = lowercase tag name) or 'text' (value = entity-unescaped text)."""
    parts = []
    pos = 0
    for m in TAG_RE.finditer(raw or ""):
        if m.start() > pos:
            parts.append(("text", html.unescape(raw[pos:m.start()])))
        parts.append(("tag", m.group(1).lower()))
        pos = m.end()
    if pos < len(raw or ""):
        parts.append(("text", html.unescape(raw[pos:])))
    return parts


def tag_names(raw):
    return [m.group(1).lower() for m in TAG_RE.finditer(raw or "")]


def classify_field(raw):
    """'block' | 'inline_only' | 'other_only' | 'none' for one field."""
    tags = set(tag_names(raw))
    if tags & BLOCK_TAGS:
        return "block"
    if tags and tags <= INLINE_TAGS:
        return "inline_only"
    if tags:
        return "other_only"
    return "none"


def scan_joins(raw):
    """Find join points in the TAG-DELETED text and attribute each one.

    Reproduces _strip_html's tag deletion (tags -> "", entities unescaped),
    finds every JOIN_RE hit, and reports whether >=1 tag sat between the
    punctuation and the following Hangul in the RAW text (tag_caused=True,
    with the tag names) or whether the two characters were adjacent in one
    raw text run (a join native to Naver's own text)."""
    parts = segment(raw)
    chars, origin = [], []
    for si, (kind, value) in enumerate(parts):
        if kind == "text":
            for ch in value:
                chars.append(ch)
                origin.append(si)
    stripped = "".join(chars)
    joins = []
    for m in JOIN_RE.finditer(stripped):
        p = m.start()
        seg_a, seg_b = origin[p], origin[p + 1]
        tags_between = [parts[i][1] for i in range(seg_a + 1, seg_b)
                        if parts[i][0] == "tag"]
        joins.append({
            "context": stripped[max(0, p - 18):p + 19],
            "tag_caused": bool(tags_between),
            "tags": tags_between,
        })
    return joins


# ---------------------------------------------------------------------------
# Live probe
# ---------------------------------------------------------------------------

def fetch(query, client_id, client_secret):
    """One live search call. Returns (items, error_message_or_None)."""
    import requests

    try:
        response = requests.get(
            NAVER_NEWS_ENDPOINT,
            headers={"X-Naver-Client-Id": client_id,
                     "X-Naver-Client-Secret": client_secret},
            params={"query": query, "sort": "sim"},
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return [], f"transport error: {type(exc).__name__}"
    if response.status_code == 429:
        return [], "HTTP 429 (rate limited) — wait a minute and re-run"
    if response.status_code != 200:
        return [], f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except Exception as exc:
        return [], f"json parse failed: {type(exc).__name__}"
    items = payload.get("items")
    if not isinstance(items, list):
        return [], "missing items array"
    return [i for i in items if isinstance(i, dict)], None


def normalized(text):
    """Tag-free, entity-free, whitespace-free form for title matching."""
    no_tags = TAG_RE.sub("", text or "")
    return re.sub(r"\s+", "", html.unescape(no_tags))


def run_live():
    client_id = (os.getenv("NAVER_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        print("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET not set in this "
              "environment — run this in the Render shell where they live. "
              "Nothing was called.")
        return 1

    queries = read_seed_queries()
    print(f"probe queries (from scheduler.DEFAULT_QUERIES): {queries}")

    tag_counts = {"title": Counter(), "description": Counter()}
    item_class = {"title": Counter(), "description": Counter()}
    all_joins = []
    total_items = 0

    for query in queries:
        items, error = fetch(query, client_id, client_secret)
        if error:
            print(f"  [{query}] SKIPPED: {error}")
            time.sleep(PACING_SECONDS)
            continue
        print(f"  [{query}] {len(items)} items")
        total_items += len(items)
        for item in items:
            for field in ("title", "description"):
                raw = item.get(field) or ""
                tag_counts[field].update(tag_names(raw))
                item_class[field][classify_field(raw)] += 1
            for join in scan_joins(item.get("description") or ""):
                all_joins.append(join)
        time.sleep(PACING_SECONDS)

    print(f"\n=== TAG DISTRIBUTION over {total_items} items ===")
    for field in ("title", "description"):
        print(f"{field}: distinct tags {dict(tag_counts[field]) or '(none)'}")
        c = item_class[field]
        print(f"  items with a BLOCK tag: {c['block']} | inline-only: "
              f"{c['inline_only']} | other-only: {c['other_only']} | "
              f"no tags: {c['none']}")

    print("\n=== DECISIVE TEST: the 경국대 rows, re-fetched from source ===")
    for case in DECISIVE_CASES:
        items, error = fetch(case["title"], client_id, client_secret)
        time.sleep(PACING_SECONDS)
        if error:
            print(f"  id {case['analysis_id']}: SKIPPED: {error}")
            continue
        want = normalized(case["title"])
        match = next((i for i in items
                      if normalized(i.get("title")) == want
                      or want in normalized(i.get("title"))
                      or normalized(i.get("title")) in want), None)
        if match is None:
            print(f"  id {case['analysis_id']}: no title match in "
                  f"{len(items)} results (article may have aged out)")
            continue
        raw_desc = match.get("description") or ""
        print(f"  id {case['analysis_id']} RAW title      : {match.get('title')}")
        print(f"  id {case['analysis_id']} RAW description: {raw_desc}")
        joins = scan_joins(raw_desc)
        boundary = [j for j in joins
                    if case["joined_left"][-2:] in j["context"]
                    and case["joined_right"][:2] in j["context"]]
        for join in joins:
            marker = " <-- stored-claim boundary" if join in boundary else ""
            cause = (f"TAG DELETION ({', '.join(join['tags'])})"
                     if join["tag_caused"] else "NATIVE in source text")
            print(f"    join …{join['context']}… -> {cause}{marker}")
        if not joins:
            print("    no join points in this description — the stored join "
                  "did not come from this field's tags")
        all_joins.extend(joins)

    print("\n=== JOIN-POINT SCAN (all fetched descriptions) ===")
    caused = [j for j in all_joins if j["tag_caused"]]
    native = [j for j in all_joins if not j["tag_caused"]]
    print(f"joins caused by tag deletion: {len(caused)} "
          f"(tags: {Counter(t for j in caused for t in j['tags']) or '{}'})")
    print(f"joins already present in source text: {len(native)}")
    for join in native[:5]:
        print(f"  native example: …{join['context']}…")

    if caused:
        print("\nCONCLUSION: block tags present at join points")
    elif native:
        print("\nCONCLUSION: joins are native to Naver text")
    else:
        print("\nCONCLUSION: no join points in this sample — larger sample "
              "needed before deciding")
    return 0


# ---------------------------------------------------------------------------
# Selftest — pure logic, zero network
# ---------------------------------------------------------------------------

def run_selftest():
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # 1. inline <b> around a keyword: no split, inline-only classification.
    raw = "정부가 <b>전세대출</b> 규제를 강화한다고 밝혔다."
    check("inline tags", tag_names(raw), ["b", "b"])
    check("inline class", classify_field(raw), "inline_only")
    check("inline no joins", scan_joins(raw), [])

    # 2. block <br> between sentences: join attributed to TAG DELETION.
    raw = "확보한다는 계획이다.<br>경북도는 착수했다."
    check("br class", classify_field(raw), "block")
    joins = scan_joins(raw)
    check("br join count", len(joins), 1)
    check("br join caused", joins[0]["tag_caused"], True)
    check("br join tags", joins[0]["tags"], ["br"])

    # 3. native join (no tags at all): attributed to source text.
    raw = "확보한다는 계획이다.경북도는 착수했다."
    check("native class", classify_field(raw), "none")
    joins = scan_joins(raw)
    check("native join count", len(joins), 1)
    check("native join caused", joins[0]["tag_caused"], False)

    # 4. spaced sentences: no join reported.
    check("spaced no joins", scan_joins("계획이다. 경북도는 착수했다."), [])

    # 5. entities unescape without creating fake tags.
    raw = "&lt;관계부처&gt; 협의 결과를 밝혔다.&quot;인용&quot;"
    check("entity tags", tag_names(raw), [])
    check("entity class", classify_field(raw), "none")

    # 6. mixed: inline tag at the boundary does NOT excuse a native join…
    raw = "밝혔다.<b>강조</b>된 문장"
    joins = scan_joins(raw)
    check("inline-at-boundary count", len(joins), 1)
    check("inline-at-boundary caused", joins[0]["tag_caused"], True)
    check("inline-at-boundary tags", joins[0]["tags"], ["b"])

    # 7. unknown tag classification.
    check("other class", classify_field("본문 <custom>x</custom>"), "other_only")

    # 8. seed queries readable from scheduler.py (repo checkout only).
    seeds = read_seed_queries()
    check("seed count", len(seeds), SEED_QUERY_COUNT)
    check("seed type", all(isinstance(s, str) and s for s in seeds), True)

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print(f"SELFTEST PASSED (8 groups; seeds = {seeds})")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(run_selftest() if "--selftest" in sys.argv else run_live())
