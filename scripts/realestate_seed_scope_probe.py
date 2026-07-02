"""REALESTATE-SEED-SCOPE — READ-ONLY, SELECT-only anatomy of the realestate domain:
which seed keywords pull market/promo articles vs genuine government-policy articles, to
scope whether the issue is KEYWORD-DESIGN (a few leaky keywords) or intrinsic
("realestate = policy + market mixed"). MEASURE BEFORE SURGERY; found != widespread.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT/UPDATE/DELETE/ALTER/commit.
Touches no production code, no verdict logic, no pins. The promo markers are a HEURISTIC
SCOPING SIGNAL (replicated verbatim from extract_scope_probe) — NOT a classifier/filter/
verdict. The genuine check reuses the REAL predicate (extract_primary_document_match +
the persisted has_genuine_official_support the official-status box reads).

METRICS
-------
  A. KEYWORD -> PROMO RATE: for every realestate row, the per-row keyword (the top-level
     `query` column = the keyword/search that ran the row) x whether it has >=1 promo/
     listing-heuristic claim. Table: keyword | total | promo | rate, ranked by promo count.
  B. POLICY vs MARKET: realestate rows cross-tabbed — promo-claim presence x genuine
     (has_genuine_official_support True OR a real primary-document match). Split:
     'genuine-policy-ish' (genuine/primary True) vs 'market/promo-ish' (promo AND NOT
     genuine) vs 'other'. Plus how promo correlates with genuine==False.
  C. DOMAIN CONTRAST: the same domain-level promo rate for welfare + labor — are THEIR
     keywords ~0% promo, or also leaky? (Tells keyword-design-leak from topic-mixed.)
  D. SEED LIST: the configured broad seeds (config.hot_topic_seed_queries() /
     _DEFAULT_HOT_TOPIC_SEEDS), printed as-defined, with the Metric-A caveat that per-row
     keywords are AI-generated/dynamic (stored in `query`), not a fixed per-domain list.

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  * Domain enum (domain_classifier.py:33): finance/welfare/agriculture/labor/health/
    environment/SMB/realestate/statistics/기타-미분류. Metric A/B filter domain='realestate';
    C contrasts 'welfare'+'labor'. domain is a top-level column (nullable on un-backfilled
    old rows -> those are simply not counted; noted).
  * SURPRISE: there is NO separate stored 'seed keyword' field per row and NO keyword field
    in debug_summary. The per-row keyword IS the top-level `query` column (for hot-topic
    rows it holds the AI-selected keyword, e.g. '부동산 사기' for id=568). Metric A reads it.
  * has_genuine_official_support lives INSIDE source_reliability_summary JSON; the primary-
    document match is computed by the REAL extract_primary_document_match over
    source_candidates.

SAFETY: SELECT-only; engine.connect() (never begin()); no commit. Lazy DB import inside the
live path so --selftest is offline. ASCII-guarded prints.

Usage (real run in the Render Worker Shell after commit):
    PYTHONPATH=. python scripts/realestate_seed_scope_probe.py
    PYTHONPATH=. python scripts/realestate_seed_scope_probe.py --selftest   # offline, no DB

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

# Real genuine-match predicate (pure; no DB/network at import).
from official_evidence_resolution import extract_primary_document_match  # noqa: E402

REALESTATE = "realestate"
CONTRAST_DOMAINS = ("welfare", "labor")
OFFICIAL_TYPES = {"official_government", "public_institution"}

# --- HEURISTIC listing/promo markers — REPLICATED VERBATIM from extract_scope_probe.py
# (SCOPING ONLY; not a classifier/filter). Excludes bare 억원/만원 (policy budgets).
PROMO_MARKERS = (
    "분양", "청약", "견본주택", "모델하우스", "분양신청", "분양가", "입주자모집",
    "계약금", "중도금", "잔금", "평당", "매매가", "임대료", "전용면적", "입주",
    "수자인", "자이", "푸르지오", "힐스테이트", "래미안", "더퍼스트", "e편한세상",
    "이편한세상", "아이파크", "롯데캐슬", "위브", "더샵", "센트럴파크",
)
_PRICE_RANGE_RE = re.compile(
    r"\d+\s*만\s*~\s*\d+\s*만원"
    r"|\d+\s*억\s*~\s*\d+\s*억"
    r"|월\s*임대료"
    r"|보증금\s*\d"
    r"|\d+\s*만원\s*(?:대|선)"
)


def promo_hit(text: str) -> str:
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


def claim_texts(normalized: list, claims: list) -> list[str]:
    out = []
    for c in normalized or []:
        if isinstance(c, dict) and c.get("claim_text"):
            out.append(str(c.get("claim_text")))
    for c in claims or []:
        if isinstance(c, str) and c:
            out.append(c)
        elif isinstance(c, dict) and c.get("sentence"):
            out.append(str(c.get("sentence")))
    return out


def row_has_promo(normalized: list, claims: list) -> str:
    for t in claim_texts(normalized, claims):
        marker = promo_hit(t)
        if marker:
            return marker
    return ""


def row_is_genuine(srs: dict, candidates: list) -> bool:
    """Reuse the REAL genuine axis: the persisted has_genuine_official_support boolean
    (the official-status box's predicate) OR a real primary-document match."""
    s = srs or {}
    genuine = (s.get("has_genuine_official_support")
               if isinstance(s.get("has_genuine_official_support"), bool)
               else False)
    if genuine:
        return True
    return extract_primary_document_match(candidates or []) is not None


def classify_row(promo_marker: str, genuine: bool) -> str:
    if genuine:
        return "genuine_policy_ish"
    if promo_marker:
        return "market_promo_ish"
    return "other"


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== REALESTATE-SEED-SCOPE — OFFLINE SELF-TEST (no DB) ===")
    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    p("promo heuristic (pos/neg, replicated markers):")
    expect("PROMO", "'수자인 분양신청' -> hit",
           bool(promo_hit("'수자인' 분양신청 시작")), True)
    expect("PROMO", "'5000만~6000만원' -> hit",
           promo_hit("아파트 가격 5000만~6000만원").startswith("price-range"), True)
    expect("PROMO", "'전세대출 금리 인하' -> no hit",
           promo_hit("전세대출 금리 인하"), "")
    expect("PROMO", "'예산 500억원' -> no hit (budget, not listing)",
           promo_hit("예산 500억원 투입"), "")

    p("row_has_promo aggregation:")
    expect("ROW", "promo in normalized claim",
           bool(row_has_promo([{"claim_text": "월 임대료 50만~60만원"}], [])), True)
    expect("ROW", "no promo in policy claims",
           row_has_promo([{"claim_text": "정부 규제 발표"}], ["금리 인하"]), "")

    p("genuine axis (real extract_primary_document_match reuse):")
    weak = [{"source_type": "official_government",
             "official_evidence_classification": "weak_official_candidate_only"}]
    expect("GEN", "has_genuine False + weak cands -> not genuine",
           row_is_genuine({"has_genuine_official_support": False}, weak), False)
    expect("GEN", "has_genuine True -> genuine",
           row_is_genuine({"has_genuine_official_support": True}, weak), True)
    strong_pb = [{"source_type": "official_government", "policy_briefing_news_item_id": "x",
                  "official_body_match": True,
                  "official_evidence_classification": "strong_official_direct_support",
                  "official_evidence_score": 80}]
    expect("GEN", "primary-doc match -> genuine even if has_genuine absent",
           row_is_genuine({}, strong_pb), True)

    p("Metric B split:")
    expect("SPLIT", "genuine True -> policy_ish",
           classify_row("분양", True), "genuine_policy_ish")
    expect("SPLIT", "promo + genuine False -> market_ish",
           classify_row("분양", False), "market_promo_ish")
    expect("SPLIT", "no promo + genuine False -> other",
           classify_row("", False), "other")

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: promo / aggregation / genuine-reuse / B-split proven ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH
# ---------------------------------------------------------------------------
def _domain_promo_rate(rows) -> tuple[int, int, dict]:
    """(total, promo_rows, per_keyword) for a set of rows."""
    total = 0
    promo_rows = 0
    per_keyword: dict = {}
    for r in rows:
        m = r._mapping
        total += 1
        kw = str(m["query"] or "(none)").strip() or "(none)"
        marker = row_has_promo(_json_list(m["normalized_claims"]), _json_list(m["claims"]))
        agg = per_keyword.setdefault(kw, {"total": 0, "promo": 0})
        agg["total"] += 1
        if marker:
            promo_rows += 1
            agg["promo"] += 1
    return total, promo_rows, per_keyword


def run_live() -> int:
    p("=== REALESTATE-SEED-SCOPE (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    base_cols = ("id, query, domain, claims, normalized_claims, source_candidates, "
                 "source_reliability_summary")
    with engine.connect() as conn:
        re_rows = conn.execute(
            sa.text(f"SELECT {base_cols} FROM analysis_results WHERE domain = :d")
            .bindparams(d=REALESTATE)
        ).all()
        contrast_rows = {}
        for dom in CONTRAST_DOMAINS:
            contrast_rows[dom] = conn.execute(
                sa.text(f"SELECT {base_cols} FROM analysis_results WHERE domain = :d")
                .bindparams(d=dom)
            ).all()

    # ---- METRIC A -----------------------------------------------------------
    total, promo_rows, per_keyword = _domain_promo_rate(re_rows)
    p("")
    p("=== METRIC A — realestate KEYWORD -> PROMO RATE (per-row `query` column) ===")
    p(f"  realestate rows (domain-classified): {total}")
    p(f"  rows with >=1 promo/listing claim  : {promo_rows}"
      + (f"  ({round(100 * promo_rows / total)}%)" if total else ""))
    p("  keyword | total | promo | rate   (ranked by promo count, top 25)")
    ranked = sorted(per_keyword.items(),
                    key=lambda kv: (-kv[1]["promo"], -kv[1]["total"], kv[0]))
    for kw, agg in ranked[:25]:
        rate = f"{round(100 * agg['promo'] / agg['total'])}%" if agg["total"] else "n/a"
        p(f"    {_ascii(kw)} | {agg['total']} | {agg['promo']} | {rate}")
    top_offenders = [kw for kw, agg in ranked if agg["promo"] > 0][:5]
    p(f"  top offenders (keywords with promo rows): {[_ascii(k) for k in top_offenders] or '(none)'}")

    # ---- METRIC B -----------------------------------------------------------
    p("")
    p("=== METRIC B — realestate POLICY vs MARKET split (real genuine predicate) ===")
    split = {"genuine_policy_ish": 0, "market_promo_ish": 0, "other": 0}
    promo_and_not_genuine = 0
    promo_and_genuine = 0
    promo_total = 0
    for r in re_rows:
        m = r._mapping
        marker = row_has_promo(_json_list(m["normalized_claims"]), _json_list(m["claims"]))
        genuine = row_is_genuine(_json_obj(m["source_reliability_summary"]),
                                 _json_list(m["source_candidates"]))
        split[classify_row(marker, genuine)] += 1
        if marker:
            promo_total += 1
            if genuine:
                promo_and_genuine += 1
            else:
                promo_and_not_genuine += 1
    p(f"  genuine-policy-ish (genuine/primary True): {split['genuine_policy_ish']}")
    p(f"  market/promo-ish (promo AND genuine False): {split['market_promo_ish']}")
    p(f"  other (no promo, not genuine)             : {split['other']}")
    p(f"  promo-claim correlation with genuine: promo&genuine=False={promo_and_not_genuine} "
      f"vs promo&genuine=True={promo_and_genuine} (of {promo_total} promo rows)")
    p("  => a high promo&genuine=False share means listing content lands as non-genuine")
    p("     'market/promo' rows (the id=568 pattern), not as verified policy.")

    # ---- METRIC C -----------------------------------------------------------
    p("")
    p("=== METRIC C — DOMAIN CONTRAST (welfare, labor promo rates) ===")
    p(f"  realestate: {total} rows, {promo_rows} promo"
      + (f" ({round(100 * promo_rows / total)}%)" if total else ""))
    for dom in CONTRAST_DOMAINS:
        dtot, dpromo, dkw = _domain_promo_rate(contrast_rows[dom])
        rate = f"{round(100 * dpromo / dtot)}%" if dtot else "n/a"
        top = sorted(dkw.items(), key=lambda kv: (-kv[1]["promo"], kv[0]))
        top_kw = _ascii(top[0][0]) if top and top[0][1]["promo"] > 0 else "(none)"
        p(f"  {dom}: {dtot} rows, {dpromo} promo ({rate}); top promo keyword: {top_kw}")
    p("  => if welfare/labor are ~0% while realestate is high, the leak is REALESTATE-")
    p("     specific (keyword/topic), not a corpus-wide extraction flaw.")

    # ---- METRIC D -----------------------------------------------------------
    p("")
    p("=== METRIC D — configured seed keywords (as defined in the repo) ===")
    try:
        import config
        seeds = config.hot_topic_seed_queries()
    except Exception as exc:  # noqa: BLE001
        seeds = []
        p(f"  (could not read config.hot_topic_seed_queries: {exc})")
    p(f"  _DEFAULT_HOT_TOPIC_SEEDS / hot_topic_seed_queries(): {[_ascii(s) for s in seeds]}")
    re_seeds = [s for s in seeds if "부동산" in s or "주택" in s or "전세" in s]
    p(f"  realestate-relevant configured seed(s): {[_ascii(s) for s in re_seeds] or '(none)'}")
    configured = set(seeds)
    a_offenders_configured = [k for k in top_offenders if k in configured]
    p(f"  Metric-A offenders that are CONFIGURED seeds: "
      f"{[_ascii(k) for k in a_offenders_configured] or '(none — offenders are dynamic/AI keywords, not fixed seeds)'}")
    p("  NOTE: per-row keywords (Metric A `query`) are AI-selected/dynamic (hot_topics),")
    p("  NOT a fixed per-domain seed list — so most offenders won't literally match the")
    p("  broad configured seeds. The mapping shows whether a configured seed is itself leaky.")

    p("")
    p("NOTE: the promo markers are a HEURISTIC scoping signal (substring/price-range), NOT")
    p("a verdict, classifier, or filter. Diagnosis only; nothing written, nothing proposed.")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY realestate seed/keyword anatomy (promo vs policy). "
                    "Use --selftest for the offline logic check.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE synthetic-case logic check (no DB / network).")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
