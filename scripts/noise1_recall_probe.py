"""NOISE1-RECALL-PROBE — READ-ONLY, SELECT-only measurement of content_nature label
accuracy, to answer the Part B go/no-go question: is it safe to rank-down / badge
market_commercial rows, or would that silently demote genuine policy content?

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT/UPDATE/DELETE/ALTER/commit.
Touches no production code, no verdict logic, no pins, no classifier/config changes.
content_nature is a metadata-only column (NOISE1-A, OBSERVE mode) — reading it is safe.

WHY
---
NOISE-1 Part A stores a content_nature label (government_policy / market_commercial /
mixed_or_unclear) per row, metadata-only. Before Part B (rank-down + badge for
market_commercial AND genuine==False) we must measure classifier accuracy against ONE
hard bar: ZERO genuine-policy rows mislabeled market_commercial (silent recall loss is
the worst outcome). content_nature only labels NEW cards, so this probe also measures
whether the market_commercial sample is even big enough to judge.

METRICS
-------
  A. SAMPLE SIZE — over all rows that HAVE a content_nature label: total labeled, count
     per label, count per domain. Answers: is the market_commercial sample big enough to
     judge, or is the content_nature label backfill (CLASSIFY-2b) needed first?
  B. GO/NO-GO (the key check) — every row labeled market_commercial: id | domain |
     has_genuine | promo-verdict | title. has_genuine reads the REAL persisted
     has_genuine_official_support boolean (source_reliability_summary JSON; NOT a
     top-level column). promo-verdict cross-references the SAME two fields the
     classifier itself saw (title + the top-level claim_text column — see
     content_nature_classifier.classify_content_nature call site in main.py, which
     passes news["title"] + verification_card["claim_text"], NOT the claims/
     normalized_claims arrays other probes scan). FLAGGED = has_genuine True (hard
     evidence of genuine official support) OR promo-verdict empty (heuristic disagrees
     that the row is market-ish) — either is a candidate false positive to eyeball.
     Acceptance bar = ZERO market_commercial rows with has_genuine=True.
  C. REALESTATE FOCUS (the motivating domain) — realestate rows with a content_nature
     label: the label split; and of the realestate rows the promo-heuristic flags as
     market-ish, how many got market_commercial (caught) vs government_policy (missed).
  D. REVERSE / MISSES (informational, not a blocker) — labeled rows the promo-heuristic
     flags market-ish but content_nature says government_policy — potential misses (the
     SAFE direction: missing a market tag is safe; mislabeling policy is not). Count +
     up to 10 samples.

VERDICT (mechanical, from Metric A + B only)
---------------------------------------------
  TUNE               — any market_commercial row has has_genuine=True (hard evidence of
                        genuine policy content mislabeled; classifier needs adjustment).
  NEEDS-LABEL-BACKFILL — no such row, but the market_commercial sample is under
                        MIN_SAMPLE_FOR_JUDGMENT (a probe-chosen threshold, not a system
                        requirement) — too thin to certify Part B is recall-safe.
  GO                 — no has_genuine=True market_commercial row AND sample adequate.
  The promo-heuristic FLAGGED count (Metric B) is reported for manual eyeball but does
  NOT by itself drive the VERDICT — it is a soft substring/price-range signal, not
  ground truth (see extract_scope_probe.py), whereas has_genuine_official_support is the
  real persisted predicate the official-status box uses.

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  * content_nature: nullable TEXT column on analysis_results (postgres_storage.py:287,
    database.py:324/347). None when CONTENT_NATURE_ENABLED was off or classification
    hadn't run yet at insert time -- those rows are simply not counted in "labeled".
  * has_genuine_official_support lives INSIDE source_reliability_summary JSON (NOT a
    top-level column) -- postgres_storage.py:1453/1488.
  * SURPRISE: classify_content_nature (content_nature_classifier.py) is called in
    main.py with news.get("title") + verification_card.get("claim_text") -- the single
    top-level claim_text STRING column, not the claims/normalized_claims JSON ARRAYS
    that extract_scope_probe.py / realestate_seed_scope_probe.py scan for their (wider)
    promo-scoping metrics. This probe's promo cross-reference therefore scans title +
    claim_text (what the classifier actually saw), which differs from those two sibling
    probes by design -- flagged here for the strategist, not a bug.
  * PROMO_MARKERS / promo_hit REPLICATED VERBATIM from extract_scope_probe.py (same
    heuristic set realestate_seed_scope_probe.py replicates) -- not re-implemented, just
    copied so this script stays standalone. Still a HEURISTIC SCOPING SIGNAL, not a
    classifier/filter/verdict.

SAFETY: SELECT-only; engine.connect() (never begin()); no commit. Lazy DB import inside
the live path so --selftest is offline. ASCII-guarded prints. No source/config edits.

Usage (real run in the Render Worker Shell; safe alongside a running backfill --
SELECT-only, does not touch content_nature/classifier/backfill code paths):
    PYTHONPATH=. python scripts/noise1_recall_probe.py
    PYTHONPATH=. python scripts/noise1_recall_probe.py --selftest   # offline, no DB

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

LABELS = ("government_policy", "market_commercial", "mixed_or_unclear")

# Probe-chosen threshold for "is the market_commercial sample big enough to judge" --
# NOT a system requirement, just this probe's go/no-go bar for statistical confidence.
MIN_SAMPLE_FOR_JUDGMENT = 20

# --- HEURISTIC listing/promo markers -- REPLICATED VERBATIM from extract_scope_probe.py
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
    """Return the first matched marker (a heuristic listing/promo signal), else ''."""
    t = str(text or "")
    for marker in PROMO_MARKERS:
        if marker in t:
            return marker
    m = _PRICE_RANGE_RE.search(t)
    if m:
        return f"price-range:{m.group(0).strip()}"
    return ""


def row_promo_verdict(title, claim_text) -> str:
    """Cross-reference verdict over the SAME two fields the classifier saw."""
    for t in (title, claim_text):
        marker = promo_hit(t)
        if marker:
            return marker
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


def _domain_of(value) -> str:
    v = str(value or "").strip()
    return v or "(none)"


def has_genuine_flag(srs_raw) -> bool:
    """The REAL persisted has_genuine_official_support boolean (source_reliability_summary
    JSON -- NOT a top-level column). Missing/non-bool -> False."""
    obj = _json_obj(srs_raw)
    v = obj.get("has_genuine_official_support")
    return v if isinstance(v, bool) else False


def determine_verdict(mc_total: int, mc_has_genuine_true: int,
                       min_sample: int = MIN_SAMPLE_FOR_JUDGMENT) -> str:
    if mc_has_genuine_true > 0:
        return "TUNE"
    if mc_total < min_sample:
        return "NEEDS-LABEL-BACKFILL"
    return "GO"


# ---------------------------------------------------------------------------
# OFFLINE SELF-TEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== NOISE1-RECALL-PROBE -- OFFLINE SELF-TEST (no DB) ===")
    failures: list[str] = []

    def expect(check: str, label: str, got, want) -> None:
        ok = got == want
        p(f"  [{'PASS' if ok else 'FAIL'}] {check}: {label}  (got={got!r} want={want!r})")
        if not ok:
            failures.append(f"{check}:{label}")

    p("promo heuristic (pos/neg, replicated markers):")
    expect("PROMO", "'수자인 분양신청' -> hit",
           bool(promo_hit("'솔라시도 수자인 더퍼스트' 분양신청 시작")), True)
    expect("PROMO", "'5000만~6000만원' -> price-range hit",
           promo_hit("아파트 가격은 5000만~6000만원").startswith("price-range"), True)
    expect("PROMO", "'전세대출 금리 인하' -> no hit",
           promo_hit("정부가 전세대출 금리를 인하했다"), "")
    expect("PROMO", "'예산 500억원 투입' -> no hit (budget, not listing)",
           promo_hit("정부가 예산 500억원을 투입한다"), "")

    p("row_promo_verdict (title + claim_text, the fields the classifier saw):")
    expect("VERDICT", "market marker in title -> hit",
           bool(row_promo_verdict("'래미안' 분양가 공개", "일반 설명")), True)
    expect("VERDICT", "market marker in claim_text -> hit",
           bool(row_promo_verdict("평범한 제목", "월 임대료 50만~60만원")), True)
    expect("VERDICT", "no marker in either -> no hit",
           row_promo_verdict("정부 정책 발표", "전세대출 금리 인하"), "")

    p("has_genuine_flag (real has_genuine_official_support reuse):")
    expect("GEN", "True bool -> True",
           has_genuine_flag({"has_genuine_official_support": True}), True)
    expect("GEN", "False bool -> False",
           has_genuine_flag({"has_genuine_official_support": False}), False)
    expect("GEN", "missing key -> False",
           has_genuine_flag({}), False)
    expect("GEN", "JSON string input -> parsed",
           has_genuine_flag('{"has_genuine_official_support": true}'), True)
    expect("GEN", "non-bool value -> False (fail-safe)",
           has_genuine_flag({"has_genuine_official_support": "yes"}), False)

    p("determine_verdict:")
    expect("VERDICT-FN", "any has_genuine=True market_commercial -> TUNE",
           determine_verdict(50, 1), "TUNE")
    expect("VERDICT-FN", "0 has_genuine=True but thin sample -> NEEDS-LABEL-BACKFILL",
           determine_verdict(5, 0), "NEEDS-LABEL-BACKFILL")
    expect("VERDICT-FN", "0 has_genuine=True + adequate sample -> GO",
           determine_verdict(25, 0), "GO")
    expect("VERDICT-FN", "TUNE wins even with adequate sample",
           determine_verdict(100, 2), "TUNE")
    expect("VERDICT-FN", "boundary: exactly MIN_SAMPLE -> GO",
           determine_verdict(MIN_SAMPLE_FOR_JUDGMENT, 0), "GO")
    expect("VERDICT-FN", "boundary: one under MIN_SAMPLE -> NEEDS-LABEL-BACKFILL",
           determine_verdict(MIN_SAMPLE_FOR_JUDGMENT - 1, 0), "NEEDS-LABEL-BACKFILL")

    p("")
    if failures:
        p(f"=== SELF-TEST FAILED: {len(failures)} case(s): {failures} ===")
        return 1
    p("=== SELF-TEST PASSED: promo-heuristic / has_genuine reuse / verdict-fn proven ===")
    return 0


# ---------------------------------------------------------------------------
# LIVE PATH
# ---------------------------------------------------------------------------
def _sorted_counts(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))) or "(none)"


def run_live() -> int:
    p("=== NOISE1-RECALL-PROBE (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable -- set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    sql = ("SELECT id, domain, title, claim_text, content_nature, "
           "source_reliability_summary FROM analysis_results")
    with engine.connect() as conn:
        rows = conn.execute(sa.text(sql)).all()

    total_rows = len(rows)

    label_counts: dict = {}
    label_domain_counts: dict = {}
    mc_dump: list[dict] = []
    mc_has_genuine_true = 0
    flagged: list[dict] = []
    re_label_counts: dict = {}
    re_promo_caught = 0
    re_promo_missed = 0
    re_promo_other = 0
    miss_rows: list[dict] = []
    miss_count = 0

    for r in rows:
        m = r._mapping
        cn = m["content_nature"]
        if not cn:
            continue  # unlabeled row -- not counted in any "labeled" metric

        dom = _domain_of(m["domain"])
        label_counts[cn] = label_counts.get(cn, 0) + 1
        label_domain_counts.setdefault(cn, {})
        label_domain_counts[cn][dom] = label_domain_counts[cn].get(dom, 0) + 1

        verdict = row_promo_verdict(m["title"], m["claim_text"])
        genuine = has_genuine_flag(m["source_reliability_summary"])

        if cn == "market_commercial":
            mc_dump.append({
                "id": m["id"], "domain": dom, "has_genuine": genuine,
                "verdict": verdict, "title": m["title"],
            })
            if genuine:
                mc_has_genuine_true += 1
            if genuine or not verdict:
                flagged.append({
                    "id": m["id"], "domain": dom, "has_genuine": genuine,
                    "verdict": verdict, "title": m["title"],
                })

        if dom == "realestate":
            re_label_counts[cn] = re_label_counts.get(cn, 0) + 1
            if verdict:
                if cn == "market_commercial":
                    re_promo_caught += 1
                elif cn == "government_policy":
                    re_promo_missed += 1
                else:
                    re_promo_other += 1

        if verdict and cn == "government_policy":
            miss_count += 1
            if len(miss_rows) < 10:
                miss_rows.append({"id": m["id"], "domain": dom,
                                   "title": m["title"], "verdict": verdict})

    total_labeled = sum(label_counts.values())
    mc_total = label_counts.get("market_commercial", 0)

    # ---- Metric A -----------------------------------------------------------
    p("")
    p("=== METRIC A -- SAMPLE SIZE (rows with a content_nature label) ===")
    p(f"  total rows scanned  : {total_rows}")
    p(f"  total labeled       : {total_labeled}"
      + (f"  ({round(100 * total_labeled / total_rows)}% of corpus)" if total_rows else ""))
    p(f"  per-label counts    : {_sorted_counts(label_counts)}")
    for lbl in LABELS:
        if lbl in label_domain_counts:
            p(f"  {lbl} by domain: {_sorted_counts(label_domain_counts[lbl])}")
    verdict_a = ("adequate" if mc_total >= MIN_SAMPLE_FOR_JUDGMENT
                 else "TOO THIN -- label backfill (CLASSIFY-2b) likely needed first")
    p(f"  READ: market_commercial sample = {mc_total} (threshold={MIN_SAMPLE_FOR_JUDGMENT}) -> {verdict_a}")

    # ---- Metric B -----------------------------------------------------------
    p("")
    p("=== METRIC B -- GO/NO-GO: market_commercial rows (id | domain | has_genuine | "
      "promo-verdict | title) ===")
    for row in mc_dump:
        p(f"    {row['id']} | {row['domain']} | has_genuine={row['has_genuine']} | "
          f"verdict={row['verdict'] or '(no promo hit)'} | {_ascii(row['title'])[:100]}")
    if not mc_dump:
        p("    (none -- no rows labeled market_commercial)")
    p("")
    p(f"  FLAGGED (candidate false positive: has_genuine=True OR promo-verdict empty): "
      f"{len(flagged)}")
    for row in flagged:
        p(f"    FLAG {row['id']} | {row['domain']} | has_genuine={row['has_genuine']} | "
          f"verdict={row['verdict'] or '(no promo hit)'} | {_ascii(row['title'])[:100]}")
    p(f"  -- of which has_genuine=True (HARD evidence, breaches acceptance bar): "
      f"{mc_has_genuine_true}")
    p("  Acceptance bar = ZERO market_commercial rows with has_genuine=True.")

    # ---- Metric C -----------------------------------------------------------
    p("")
    p("=== METRIC C -- REALESTATE FOCUS (the motivating domain) ===")
    p(f"  realestate label split: {_sorted_counts(re_label_counts)}")
    p(f"  of realestate rows promo-flagged market-ish: caught(market_commercial)="
      f"{re_promo_caught}, missed(government_policy)={re_promo_missed}, "
      f"other(mixed_or_unclear)={re_promo_other}")

    # ---- Metric D -----------------------------------------------------------
    p("")
    p("=== METRIC D -- REVERSE/MISSES (informational; promo-flagged but labeled "
      "government_policy) ===")
    p(f"  miss count (all domains): {miss_count}")
    for row in miss_rows:
        p(f"    {row['id']} | {row['domain']} | verdict={row['verdict']} | "
          f"{_ascii(row['title'])[:100]}")
    if not miss_rows:
        p("    (none)")
    p("  (SAFE direction -- missing a market tag does not remove policy content; not a")
    p("   blocker for Part B.)")

    # ---- VERDICT --------------------------------------------------------------
    verdict = determine_verdict(mc_total, mc_has_genuine_true)
    p("")
    p(f"VERDICT: {verdict}")
    p("  GO = 0 has_genuine=True market_commercial rows + adequate sample (recall-safe).")
    p("  NEEDS-LABEL-BACKFILL = 0 has_genuine=True rows but market_commercial sample "
      f"< {MIN_SAMPLE_FOR_JUDGMENT}.")
    p("  TUNE = >=1 market_commercial row has has_genuine=True (policy mislabeled).")
    p("")
    p("NOTE: promo-verdict is a HEURISTIC scoping signal (substring/price-range), NOT a")
    p("verdict, classifier, or filter. Measurement only -- no fix, no Part B here.")
    p("")
    p("[Safety] READ-ONLY probe -- no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY content_nature label-accuracy probe (Part B go/no-go). "
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
