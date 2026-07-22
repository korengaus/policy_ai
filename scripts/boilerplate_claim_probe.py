"""BOILERPLATE-CLAIM PROBE — READ-ONLY diagnosis of how often a NON-CLAIM
boilerplate string (copyright notice, wire-service footer, photo caption) sits
where the frontend promotes a primary claim from, and how many rows would still
have a substantive claim left after rejecting boilerplate (re-selection
feasibility) vs. rows with NOTHING left (must be handled separately).

MEASUREMENT ONLY. SELECT only; no INSERT / UPDATE / DELETE. No re-extraction,
no re-analysis (permanently rejected), no splitter/matcher/verdict/display
change. Any eventual fix is DISPLAY-ONLY — stored data stays byte-identical.

EVIDENCE TRIGGER: id 11966 claim = "무단 전재-재배포, AI 학습 및 활용 금지>
2026년07월16일 17시53분 송고" (copyright footer); id 12035 = photo caption
ending in a reporter email. Both are checked explicitly every run.

WHAT "PROMOTED" MEANS HERE (mirror + honest caveat)
---------------------------------------------------
The frontend's substantiveClaimForPromotion (frontend/scripts/main.js:6617-6637)
walks normalized_claims[].claim_text FIRST, then claims[], returning the first
entry that survives sanitizeClaimText and is not a quote-lead. This probe
mirrors the POOL AND ORDER exactly (both are stored columns) but does NOT
re-implement sanitizeClaimText / claimLooksAlignedWithResult / quote-lead
(they depend on runtime result fields and a large display vocabulary — a
Python re-implementation would drift). ★CAVEAT PRINTED IN THE OUTPUT: the
"mirrored promotion pick" = first non-empty pool entry in the frontend's
order; claims[0] is also reported as the plain-baseline view.

BOILERPLATE DETECTION — derived from data, not just a guessed list
------------------------------------------------------------------
Two complementary detectors, reported separately:
  1. SEED MARKERS (from the two evidence rows + universal wire/copyright
     furniture): 무단 전재/무단전재, 재배포, 송고, ⓒ/©/Copyright, 저작권,
     AI 학습, 활용 금지, 무단 복제, 기사제보, reporter-email lines, 사진=/
     [사진] caption markers. Each marker's ACTUAL hit count + example ids is
     reported — a marker with 0 hits is reported as 0, not silently kept.
  2. REPEAT TEMPLATES (the data-derived part): each claim is normalized
     (digits -> '#', whitespace squeezed) and templates repeating across >=
     --min-repeat DISTINCT rows are listed with counts, flagged ★NEW when NO
     seed marker matches them — that column is exactly the boilerplate the
     seed list would have missed.
★Row-level buckets use SEED MARKERS ONLY. Repeats are DISCOVERY-ONLY output,
never a classifier: syndicated REAL claims also repeat verbatim across outlets
(the near-anchor work proved exact-text syndication exists), so repeat-based
classification would miscount real claims as furniture. The ★NEW rows are for
the human to read and, if warranted, extend the marker list in Phase 2.

WHAT IT PRINTS (denominators always shown)
------------------------------------------
  1. CORPUS + CAVEAT header.
  2. PROMOTED-CLAIM VIEW: rows whose mirrored promotion pick is boilerplate
     (count, %), same for claims[0].
  3. ALL-CLAIMS VIEW: rows with >=1 boilerplate claim anywhere; and of the
     rows whose promoted pick is boilerplate, how many still have >=1
     non-boilerplate claim (FIXABLE BY RE-SELECTION) vs not.
  4. THE "NOTHING LEFT" BUCKET: rows where EVERY claim is boilerplate —
     count, %, example ids (these cannot be fixed by re-selection).
  5. MARKER TABLE: per-marker rows hit + up to --examples example ids.
  6. REPEAT-TEMPLATE TABLE: top templates by distinct-row count, seed-hit vs
     ★NEW flag, one sample text each.
  7. EVIDENCE IDS (--include): their stored claims + per-claim verdicts.

SAFETY: engine.connect() only, no commit. Single SELECT of id/title/claims/
normalized_claims only (no heavy blobs). Lazy DB import; --selftest offline.

Usage (Render Worker Shell, after commit+push+redeploy+reopen Shell):
    PYTHONPATH=. python scripts/boilerplate_claim_probe.py
    PYTHONPATH=. python scripts/boilerplate_claim_probe.py --selftest
    ... --min-repeat 5 --examples 15 --include 11966,12035

Exit codes: 0 = report printed / engine unavailable; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# Seed markers: label -> compiled regex. Counts are reported per marker AS
# FOUND (0-hit markers print 0); the repeat-template table catches what this
# list misses, so the list is a starting point, not the definition.
SEED_MARKERS = [
    ("무단전재", re.compile(r"무단\s*전재")),
    ("재배포", re.compile(r"재배포")),
    ("송고", re.compile(r"\d\s*송고|송고\s*$|송고\b")),
    ("저작권", re.compile(r"저작권")),
    ("copyright-sign", re.compile(r"[ⓒ©]|copyright", re.IGNORECASE)),
    ("AI학습금지", re.compile(r"AI\s*학습")),
    ("활용금지", re.compile(r"활용\s*금지|이용\s*금지|사용\s*금지")),
    ("무단복제", re.compile(r"무단\s*복제")),
    ("기사제보", re.compile(r"기사\s*제보|제보는")),
    ("reporter-email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("사진캡션", re.compile(r"^\s*\[?사진\]?\s*=|\[사진\s*[^\]]*\]|촬영기자")),
]


def p(message: str = "") -> None:
    print(message, flush=True)


def _loads(raw) -> object:
    if raw in (None, ""):
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def claim_pool(normalized_raw, claims_raw) -> list:
    """The frontend promotion pool, SAME order (main.js:6619-6628):
    normalized_claims[].claim_text first, then claims[] strings. Trimmed,
    empties dropped. normalized_claims entries may be dicts or strings."""
    pool = []
    normalized = _loads(normalized_raw)
    if isinstance(normalized, list):
        for entry in normalized:
            if isinstance(entry, dict):
                pool.append(str(entry.get("claim_text") or ""))
            elif isinstance(entry, str):
                pool.append(entry)
    claims = _loads(claims_raw)
    if isinstance(claims, list):
        pool.extend(str(c or "") for c in claims if isinstance(c, str))
    return [text.strip() for text in pool if str(text or "").strip()]


def marker_hits(text: str) -> list:
    return [label for label, rx in SEED_MARKERS if rx.search(text)]


def template_of(text: str) -> str:
    """Digit-normalized, whitespace-squeezed template so '…07월16일 17시53분
    송고' and '…07월17일 09시01분 송고' collapse together."""
    collapsed = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", collapsed).strip()


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def scan(rows: list, min_repeat: int, examples: int,
         include_ids: list) -> dict:
    """Pure two-pass aggregation over (id, normalized_raw, claims_raw) rows."""
    pools = {}
    for row_id, normalized_raw, claims_raw in rows:
        pools[row_id] = claim_pool(normalized_raw, claims_raw)

    # Pass 1: template frequency across DISTINCT rows (DISCOVERY output only —
    # syndicated real claims also repeat, so repeats never classify).
    template_rows = defaultdict(set)
    for row_id, pool in pools.items():
        for text in pool:
            template_rows[template_of(text)].add(row_id)

    def is_boilerplate(text: str) -> bool:
        return bool(marker_hits(text))

    # Pass 2: row buckets.
    stats = {
        "rows": 0, "rows_with_pool": 0,
        "promoted_boiler": 0, "claims0_boiler": 0,
        "rows_any_boiler": 0, "promoted_fixable": 0, "promoted_unfixable": 0,
        "nothing_left": 0,
    }
    nothing_left_ids, promoted_examples = [], []
    marker_rowids = defaultdict(list)
    for row_id, pool in pools.items():
        stats["rows"] += 1
        if not pool:
            continue
        stats["rows_with_pool"] += 1
        flags = [is_boilerplate(text) for text in pool]
        for text in pool:
            for label in marker_hits(text):
                marker_rowids[label].append(row_id)
        if any(flags):
            stats["rows_any_boiler"] += 1
        if flags[0]:  # mirrored promotion pick = first pool entry (see caveat)
            stats["promoted_boiler"] += 1
            if len(promoted_examples) < examples:
                promoted_examples.append((row_id, pool[0]))
            if any(not f for f in flags):
                stats["promoted_fixable"] += 1
            else:
                stats["promoted_unfixable"] += 1
        # claims[0] baseline: last segment of the pool is claims[]; but the
        # plain-baseline view is simply the first claims[] entry.
        if all(flags):
            stats["nothing_left"] += 1
            if len(nothing_left_ids) < examples:
                nothing_left_ids.append(row_id)

    # claims[0] baseline needs the raw claims list again — recompute cheaply.
    claims0_boiler = 0
    for row_id, normalized_raw, claims_raw in rows:
        claims = _loads(claims_raw)
        first = ""
        if isinstance(claims, list):
            for c in claims:
                if isinstance(c, str) and c.strip():
                    first = c.strip()
                    break
        if first and is_boilerplate(first):
            claims0_boiler += 1
    stats["claims0_boiler"] = claims0_boiler

    top_templates = sorted(
        ((len(ids), t) for t, ids in template_rows.items() if len(ids) >= min_repeat),
        reverse=True,
    )[:30]
    include_report = {
        rid: [(text, is_boilerplate(text), marker_hits(text)) for text in pools.get(rid, [])]
        for rid in include_ids
    }
    return {
        "stats": stats,
        "marker_rowids": marker_rowids,
        "top_templates": top_templates,
        "template_rows": template_rows,
        "nothing_left_ids": nothing_left_ids,
        "promoted_examples": promoted_examples,
        "include_report": include_report,
    }


def report(result: dict, min_repeat: int, examples: int) -> None:
    stats = result["stats"]
    rows, with_pool = stats["rows"], stats["rows_with_pool"]
    p("")
    p("=== 1. CORPUS + CAVEAT ===")
    p(f"  rows scanned: {rows}; rows with a non-empty claim pool: {with_pool}")
    p("  ★CAVEAT: 'promoted pick' below = FIRST entry of the frontend's promotion")
    p("   pool (normalized_claims -> claims, main.js:6619-6628). The frontend's")
    p("   sanitize/alignment/quote-lead steps are NOT mirrored (runtime-dependent)")
    p("   — read these as pool-level counts, not exact screen counts.")

    p("")
    p("=== 2. PROMOTED-CLAIM VIEW ===")
    p(f"  mirrored promotion pick is boilerplate : {stats['promoted_boiler']}"
      f" / {with_pool}  ({_pct(stats['promoted_boiler'], with_pool)})")
    p(f"  claims[0] is boilerplate (baseline)    : {stats['claims0_boiler']}"
      f" / {with_pool}  ({_pct(stats['claims0_boiler'], with_pool)})")
    for row_id, text in result["promoted_examples"]:
        p(f"    #{row_id}  {text[:100]}")

    p("")
    p("=== 3. ALL-CLAIMS VIEW (re-selection feasibility) ===")
    p(f"  rows with >=1 boilerplate claim anywhere: {stats['rows_any_boiler']}"
      f" / {with_pool}  ({_pct(stats['rows_any_boiler'], with_pool)})")
    p(f"  of promoted-boilerplate rows: FIXABLE by re-selection (a non-boilerplate")
    p(f"  claim remains) {stats['promoted_fixable']} vs NOT fixable "
      f"{stats['promoted_unfixable']}")

    p("")
    p("=== 4. NOTHING-LEFT BUCKET (every claim is boilerplate) ===")
    p(f"  rows: {stats['nothing_left']} / {with_pool} "
      f"({_pct(stats['nothing_left'], with_pool)}) — re-selection CANNOT fix these;")
    p("  they need separate handling. Example ids: "
      + (", ".join(str(i) for i in result["nothing_left_ids"]) or "(none)"))

    p("")
    p(f"=== 5. SEED-MARKER TABLE (rows hit; up to {examples} example ids) ===")
    for label, _ in SEED_MARKERS:
        ids = result["marker_rowids"].get(label, [])
        unique = sorted(set(ids))
        p(f"    {label:<16}{len(unique):>6} rows   e.g. "
          + (", ".join(str(i) for i in unique[:examples]) or "-"))

    p("")
    p(f"=== 6. REPEAT-TEMPLATE TABLE (>= {min_repeat} distinct rows; ★NEW = no seed marker) ===")
    for count, template in result["top_templates"]:
        flag = "     " if marker_hits(template) else "★NEW "
        p(f"    {flag}{count:>5} rows  {template[:100]}")

    p("")
    p("=== 7. EVIDENCE IDS ===")
    for rid, entries in result["include_report"].items():
        p(f"  #{rid}:")
        if not entries:
            p("    (row not found or empty pool)")
        for text, boiler, hits in entries:
            p(f"    [{'BOILER' if boiler else 'ok    '}] {text[:100]}"
              + (f"  <- {','.join(hits)}" if hits else ""))


def run_live(min_repeat: int, examples: int, include_ids: list) -> int:
    p("=== BOILERPLATE-CLAIM PROBE (READ-ONLY, SELECT-only, full corpus) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        return 0

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, normalized_claims, claims FROM analysis_results ORDER BY id"
        )).all()
    result = scan(rows, min_repeat, examples, include_ids)
    report(result, min_repeat, examples)
    return 0


def _selftest() -> int:
    """Offline logic check — no DB, no network."""
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    boiler_11966 = "무단 전재-재배포, AI 학습 및 활용 금지> 2026년07월16일 17시53분 송고"
    caption_12035 = "16일 서울 도심에서 열린 행사 모습. reporter@news.co.kr"
    real = "정부는 2027년 최저임금을 3.7% 인상하기로 결정했다"

    check("marker-copyright", "무단전재" in marker_hits(boiler_11966), True)
    check("marker-songo", "송고" in marker_hits(boiler_11966), True)
    check("marker-email", marker_hits(caption_12035), ["reporter-email"])
    check("marker-clean", marker_hits(real), [])

    check("template-digits", template_of("07월16일 17시53분 송고"),
          template_of("07월17일 09시01분 송고"))
    check("template-differs", template_of(real) == template_of(boiler_11966), False)

    check("pool-order",
          claim_pool(json.dumps([{"claim_text": "정규화 주장"}]),
                     json.dumps(["원시 주장"])),
          ["정규화 주장", "원시 주장"])
    check("pool-strings", claim_pool(json.dumps(["직접 문자열"]), None), ["직접 문자열"])
    check("pool-empty", claim_pool(None, None), [])

    # End-to-end: repeated marker-free footer across 5 rows must surface as
    # ★NEW in the template table WITHOUT classifying its rows as boilerplate
    # (repeats are discovery-only — syndicated real claims also repeat).
    footer_a = "본 기사는 자동으로 작성되어 1차 배열되었습니다 01시"
    rows = [
        (1, None, json.dumps([boiler_11966, real])),          # fixable
        (2, None, json.dumps([boiler_11966])),                # nothing left
        (3, None, json.dumps([real])),                        # clean
    ] + [(10 + i, None, json.dumps([footer_a.replace("01시", f"{i}시"), real]))
         for i in range(5)]                                   # repeated template
    result = scan(rows, min_repeat=5, examples=5, include_ids=[2])
    stats = result["stats"]
    check("rows", stats["rows"], 8)
    check("promoted-boiler", stats["promoted_boiler"], 2)     # ids 1, 2 only
    check("fixable", stats["promoted_fixable"], 1)            # id 1 has `real` left
    check("unfixable", stats["promoted_unfixable"], 1)        # id 2 has nothing
    check("nothing-left", stats["nothing_left"], 1)
    check("nothing-left-ids", result["nothing_left_ids"], [2])
    check("claims0", stats["claims0_boiler"], 2)
    new_templates = [t for _, t in result["top_templates"] if not marker_hits(t)]
    check("template-discovered", len(new_templates) >= 1, True)
    check("repeat-not-classifier", result["stats"]["rows_any_boiler"], 2)
    check("include-flag", result["include_report"][2][0][1], True)

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK (18 checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BOILERPLATE-CLAIM probe (read-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check")
    parser.add_argument("--min-repeat", type=int, default=5,
                        help="distinct-row repeats for the DISCOVERY template table")
    parser.add_argument("--examples", type=int, default=15, help="example ids per bucket")
    parser.add_argument("--include", default="11966,12035",
                        help="comma-separated evidence ids to report in full")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    include_ids = [int(x) for x in str(args.include).split(",") if x.strip().isdigit()]
    return run_live(args.min_repeat, args.examples, include_ids)


if __name__ == "__main__":
    raise SystemExit(main())
