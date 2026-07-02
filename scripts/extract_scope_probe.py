"""EXTRACT-SCOPE-PROBE — READ-ONLY, SELECT-only corpus measurement of (1) how many cards
expose internal search-query text, and (2) how widespread ad/listing-style claim
extraction is, before deciding the scope of any fix. FOUND != WIDESPREAD.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE / ALTER /
commit. Touches no production code, no verdict logic, no pins. The Metric-2 markers are a
HEURISTIC SCOPING SIGNAL — they are NOT a stored classifier, NOT a filter, NOT a verdict.

WHY
---
Card id=568 ('반도체 훈풍' realestate) showed (a) claim extraction pulled promotional /
listing content ("'솔라시도 수자인 더퍼스트' 분양신청", "아파트 가격은 5000만~6000만원",
"월 임대료 50만~60만원") as factual claims, and (b) the advanced "생성된 쿼리" section renders
internal source_queries[].query strings ("site:molit.go.kr OR ..."). Before any fix we must
know whether each is one row, a realestate-domain pattern, or corpus-wide.

METRICS
-------
  1. QUERY EXPOSURE — rows whose stored source_queries is a NON-EMPTY list (the exact
     display trigger: renderSourceQueries renders the "claim #N · <purpose> · <query>"
     lines whenever source_queries is a non-empty array). Per-domain breakdown.
  2. LISTING/PROMO CLAIMS — a HEURISTIC scan over normalized_claims[].claim_text + claims[]
     for listing/promo markers (분양/청약/견본주택/모델하우스/임대료/매매가/평당/분양가/입주/
     계약금·중도금·잔금/complex-name tokens) OR a price-RANGE pattern (…만~…만원 / …억~…).
     Count rows with >=1 such claim; per-domain; up to 15 examples (id | domain | claim).
  3. DOMAIN CONCENTRATION — the Metric-2 matched-row domain distribution + the realestate
     BASE RATE (matched realestate rows / total realestate rows).
  4. CONTEXT for id=568 — collection_source + news_collection_mode (from debug_summary) +
     the stored `query` keyword that pulled it (top-level column). Read-only context to tell
     an intake-selection question from a pure extraction question.

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  TOP-LEVEL columns: id, domain, query, claims (JSON TEXT), normalized_claims (JSON TEXT),
  source_queries (JSON TEXT), debug_summary (JSON TEXT).
  collection_source + news_collection_mode live INSIDE debug_summary JSON (NOT columns).
  source_queries[].query is the generated search string; renderSourceQueries triggers on
  the array being non-empty (purpose/claim_index are per-row labels, not the trigger).

SAFETY: SELECT-only; engine.connect() (never begin()); no commit. Lazy DB import inside the
live path so --selftest is offline. ASCII-guarded prints.

Usage (real run in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/extract_scope_probe.py
    PYTHONPATH=. python scripts/extract_scope_probe.py --selftest   # offline, no DB

Exit codes: 0 = dump printed / engine unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

CONTEXT_ID = 568

# --- HEURISTIC listing/promo markers (SCOPING ONLY — not a classifier/filter) ---------
# Listing-specific vocabulary + apartment-brand/complex tokens. Deliberately excludes
# bare 억원/만원 (legitimate policy budgets) — only a price RANGE or listing context counts.
PROMO_MARKERS = (
    "분양", "청약", "견본주택", "모델하우스", "분양신청", "분양가", "입주자모집",
    "계약금", "중도금", "잔금", "평당", "매매가", "임대료", "전용면적", "입주",
    # apartment brand / complex-name tokens (listing signals)
    "수자인", "자이", "푸르지오", "힐스테이트", "래미안", "더퍼스트", "e편한세상",
    "이편한세상", "아이파크", "롯데캐슬", "위브", "더샵", "센트럴파크",
)
# price-RANGE / per-unit-rent patterns typical of listings (NOT bare 억원 budgets).
_PRICE_RANGE_RE = re.compile(
    r"\d+\s*만\s*~\s*\d+\s*만원"            # 5000만~6000만원
    r"|\d+\s*억\s*~\s*\d+\s*억"             # 3억~5억
    r"|월\s*임대료"                          # 월 임대료 …
    r"|보증금\s*\d"                          # 보증금 5000…
    r"|\d+\s*만원\s*(?:대|선)"               # 6000만원대 / 6000만원 선
)


def promo_hit(text: str) -> str:
    """Return the first matched marker (a heuristic listing/promo signal), else ''."""
    t = str(text or "")
    for marker in PROMO_MARKERS:
        if marker in t:
            return marker
    m = _PRICE_RANGE_RE.search(t)
    if m:
        return f"price-range:{m.group(0).strip()}"
    return ""


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _json_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value or not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except Exception:  # noqa: BLE001
        return []
    return parsed if isinstance(parsed, list) else []


def _domain_of(value) -> str:
    v = str(value or "").strip()
    return v or "(none)"


def claim_texts(normalized: list, claims: list) -> list[str]:
    """All claim strings for a row: normalized_claims[].claim_text + plain claims[]."""
    out = []
    for c in normalized or []:
        if isinstance(c, dict):
            t = c.get("claim_text")
            if t:
                out.append(str(t))
    for c in claims or []:
        if isinstance(c, str) and c:
            out.append(c)
        elif isinstance(c, dict) and c.get("sentence"):
            out.append(str(c.get("sentence")))
    return out


def first_promo_claim(texts: list[str]) -> tuple[str, str]:
    """(matched_claim, marker) for the first claim with a promo hit, else ('','')."""
    for t in texts:
        marker = promo_hit(t)
        if marker:
            return t, marker
    return "", ""


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== EXTRACT-SCOPE-PROBE — OFFLINE SELF-TEST (no DB) ===")
    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    # Metric 2 heuristic — positives
    p("Metric 2 promo-heuristic (positives):")
    expect("PROMO", "'수자인 더퍼스트 분양신청' -> hit",
           bool(promo_hit("'솔라시도 수자인 더퍼스트' 분양신청 시작")), True)
    expect("PROMO", "'5000만~6000만원' -> price-range hit",
           promo_hit("아파트 가격은 5000만~6000만원").startswith("price-range"), True)
    expect("PROMO", "'월 임대료 50만~60만원' -> hit",
           bool(promo_hit("월 임대료 50만~60만원")), True)
    # Metric 2 heuristic — negatives (policy claims must NOT fire)
    p("Metric 2 promo-heuristic (negatives — policy claims):")
    expect("PROMO", "'전세대출 금리 인하' -> no hit",
           promo_hit("정부가 전세대출 금리를 인하했다"), "")
    expect("PROMO", "'예산 500억원 투입' -> no hit (bare 억원 is a budget, not a listing)",
           promo_hit("정부가 예산 500억원을 투입한다"), "")
    expect("PROMO", "'최저임금 만원 돌파' -> no hit (bare 만원, no range)",
           promo_hit("최저임금이 만원을 돌파했다"), "")

    # claim_texts aggregation
    p("claim aggregation:")
    texts = claim_texts(
        [{"claim_text": "월 임대료 50만~60만원"}, {"claim_text": "정부 규제 발표"}],
        ["'수자인' 분양신청", "일반 문장"],
    )
    expect("AGG", "4 claim strings aggregated", len(texts), 4)
    claim, marker = first_promo_claim(texts)
    expect("AGG", "first promo claim found", bool(claim and marker), True)

    # Metric 1 trigger — non-empty source_queries
    p("Metric 1 query-exposure trigger:")
    expect("QEXP", "non-empty source_queries -> exposed",
           len(_json_list('[{"query":"site:molit.go.kr","purpose":"primary_source"}]')) > 0, True)
    expect("QEXP", "empty source_queries -> not exposed",
           len(_json_list("[]")) > 0, False)

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: promo-heuristic (pos/neg) / aggregation / query-trigger proven ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH
# ---------------------------------------------------------------------------
def _sorted_counts(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))) or "(none)"


def run_live() -> int:
    p("=== EXTRACT-SCOPE-PROBE (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    sql = ("SELECT id, domain, query, claims, normalized_claims, source_queries, "
           "debug_summary FROM analysis_results")
    with engine.connect() as conn:
        rows = conn.execute(sa.text(sql)).all()

    total = len(rows)
    # Metric 1
    q_exposed = 0
    q_by_domain: dict = {}
    # Metric 2
    promo_rows = 0
    promo_by_domain: dict = {}
    promo_examples: list[dict] = []
    # Metric 3
    domain_totals: dict = {}
    # Metric 4
    ctx_row = None

    for r in rows:
        m = r._mapping
        dom = _domain_of(m["domain"])
        domain_totals[dom] = domain_totals.get(dom, 0) + 1

        if len(_json_list(m["source_queries"])) > 0:
            q_exposed += 1
            q_by_domain[dom] = q_by_domain.get(dom, 0) + 1

        texts = claim_texts(_json_list(m["normalized_claims"]), _json_list(m["claims"]))
        claim, marker = first_promo_claim(texts)
        if claim:
            promo_rows += 1
            promo_by_domain[dom] = promo_by_domain.get(dom, 0) + 1
            if len(promo_examples) < 15:
                promo_examples.append({"id": m["id"], "domain": dom,
                                       "claim": claim, "marker": marker})

        if m["id"] == CONTEXT_ID:
            ctx_row = m

    # ---- Metric 1 -----------------------------------------------------------
    p("")
    p("=== METRIC 1 — QUERY EXPOSURE (rows rendering the '생성된 쿼리 · site:...' lines) ===")
    p(f"  total rows scanned                : {total}")
    p(f"  rows with NON-EMPTY source_queries: {q_exposed}"
      + (f"  ({round(100 * q_exposed / total)}% of corpus)" if total else ""))
    p(f"  by domain                         : {_sorted_counts(q_by_domain)}")
    p("  (This is the DISPLAY-surface count — every such row exposes the internal query")
    p("   text in the advanced section, regardless of extraction quality.)")

    # ---- Metric 2 -----------------------------------------------------------
    p("")
    p("=== METRIC 2 — LISTING/PROMO CLAIMS (HEURISTIC signal, NOT a verdict) ===")
    p(f"  rows with >=1 promo/listing-heuristic claim: {promo_rows}"
      + (f"  ({round(100 * promo_rows / total)}% of corpus)" if total else ""))
    p(f"  by domain                                  : {_sorted_counts(promo_by_domain)}")
    p("  examples (up to 15) — id | domain | marker | claim:")
    for ex in promo_examples:
        p(f"    {ex['id']} | {ex['domain']} | {ex['marker']} | {_ascii(ex['claim'])[:110]}")
    if not promo_examples:
        p("    (none)")

    # ---- Metric 3 -----------------------------------------------------------
    p("")
    p("=== METRIC 3 — DOMAIN CONCENTRATION of Metric-2 matches ===")
    p("  domain | matched | total | rate")
    for dom in sorted(set(list(promo_by_domain) + list(domain_totals)),
                      key=lambda d: (-promo_by_domain.get(d, 0), d)):
        matched = promo_by_domain.get(dom, 0)
        dtot = domain_totals.get(dom, 0)
        if matched == 0:
            continue
        rate = f"{round(100 * matched / dtot)}%" if dtot else "n/a"
        p(f"    {dom} | {matched} | {dtot} | {rate}")
    re_matched = promo_by_domain.get("realestate", 0)
    re_total = domain_totals.get("realestate", 0)
    p(f"  REALESTATE BASE RATE: {re_matched}/{re_total}"
      + (f" = {round(100 * re_matched / re_total)}% of realestate rows" if re_total else " (no realestate rows)"))
    p(f"  non-realestate matched rows: {promo_rows - re_matched}"
      + " (>0 => pattern is corpus-wide, not realestate-only)")

    # ---- Metric 4 -----------------------------------------------------------
    p("")
    p(f"=== METRIC 4 — CONTEXT for id={CONTEXT_ID} (how the article entered) ===")
    if ctx_row is None:
        p(f"  id={CONTEXT_ID} not found in the corpus.")
    else:
        debug = _json_obj(ctx_row["debug_summary"])
        p(f"  domain              : {_ascii(_domain_of(ctx_row['domain']))}")
        p(f"  query (keyword)     : {_ascii(ctx_row['query'])}")
        p(f"  collection_source   : {_ascii(debug.get('collection_source'))}")
        p(f"  news_collection_mode: {_ascii(debug.get('news_collection_mode'))}")
        ctx_texts = claim_texts(_json_list(ctx_row["normalized_claims"]),
                                _json_list(ctx_row["claims"]))
        cclaim, cmarker = first_promo_claim(ctx_texts)
        p(f"  source_queries count: {len(_json_list(ctx_row['source_queries']))}")
        p(f"  first promo claim   : marker={cmarker!r} claim={_ascii(cclaim)[:110]}")
        p("  (collection_source/mode tell whether id=568 is an INTAKE-selection question")
        p("   (a marketing article was picked up) or purely an EXTRACTION question.)")

    p("")
    p("NOTE: Metric 2 is a HEURISTIC scoping signal (substring/price-range markers), NOT a")
    p("verdict, classifier, or filter. It scopes 'how widespread' — it does NOT decide any")
    p("row. No fix proposed; nothing written.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY corpus scope measurement of query-exposure + listing/promo "
                    "claim extraction. Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
