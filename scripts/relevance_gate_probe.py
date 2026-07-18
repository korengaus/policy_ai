"""RELEVANCE-GATE PROBE — READ-ONLY, SELECT-only measurement of keyword-collision
false matches between a card's claim and its attached OFFICIAL document candidate.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE / ALTER /
commit. Touches no pipeline, verdict, honesty, or display code. Prints numbers only.

THE BUG BEING MEASURED
----------------------
An oil-price/inflation card ("정부 석유 최고가격제 없었으면 물가 3.6%") had the Sewol-ferry
special-act enforcement decree attached as a 공식 약한 후보 — a pure keyword collision on
shared strings (최고가격 / 지원 / 특별법) with zero topical relevance. The honesty layer held
(공식 약한 후보 · low score · 사람 검토 대기), but the source-relevance is poor. Before
building a relevance gate we must measure HOW OFTEN this happens and where the empirical
floor sits.

WHAT SIGNAL THIS PROBE USES — AND ITS ONE HONEST LIMITATION
------------------------------------------------------------
There is NO stored claim<->official-doc semantic similarity, and none can be derived from
stored data alone in the general case:
  * embedding_cache (postgres_storage.py:318-332) keys only on text_hash — no
    analysis_results FK, no candidate URL, and text_preview is the first 200 chars only.
  * scripts/embed_backfill.py embeds "title\\nclaim_text" — the NEWS side only. The
    official document text is never embedded by any backfill.
  * Official body chunks ARE embedded at runtime (semantic_similarity.py:157-180), but
    only when SEMANTIC_MATCHING_ENABLED is true, and it defaults to False (config.py:361).
So every relevance signal available here is LEXICAL:
  * official_document_relevance_score — keyword/concept substring overlap
    (official_relevance.py:170-245). Nominally 0-100, CAN GO NEGATIVE after penalties.
  * official_evidence_score -> official_final_direct_match_score ->
    official_body_match_score — the app's own 3-way fallback chain
    (source_reliability_agent.py:288, frontend/scripts/main.js:4346-4349). Term/number
    overlap counters; the "semantic_match_score" component is a misnomer, not embeddings.

>>> CIRCULARITY CAVEAT, stated up front: a keyword collision scores HIGH on a keyword
>>> metric by construction. A lexical score alone therefore CANNOT separate "collided on
>>> strings" from "genuinely relevant". This probe works around that by measuring the
>>> COLLISION SIGNATURE instead: a candidate that scored lexical points yet still fell
>>> through to 공식 약한 후보 (classification neither strong nor medium AND
>>> official_body_match falsy) and matched ZERO material policy concepts. High lexical
>>> score + zero concept overlap + weak fall-through is the Sewol shape. Buckets below
>>> are reported on that composite, NOT on the raw lexical score alone.

WHAT IT PRINTS
--------------
  1. CORPUS: rows scanned, rows with >=1 official-like candidate, total official candidates.
  2. BUCKETS over official candidates: strong / moderate / weak-but-concept-backed /
     CLEARLY-IRRELEVANT (the collision signature), counts + % of official candidates.
  3. HISTOGRAM of official_document_relevance_score in 10-point bins, split by
     has-concepts vs zero-concepts, so a natural gap (if any) is visible for a floor.
  4. EXAMPLES from the clearly-irrelevant bucket: claim snippet (claims[0]) + candidate
     title + scores + the actual colliding terms/concepts, to eyeball vs the Sewol case.
  5. STAKES: % of ALL rows and % of rows-with-official-candidates that would be touched
     if clearly-irrelevant candidates were dropped, and how many rows would lose their
     LAST official candidate entirely (the destructive-change risk).
  6. EMBEDDING AVAILABILITY: counts embedding_cache rows so a future semantic gate can be
     costed. Reports honestly that the official side is absent.

FIELD-NAME NOTES (confirmed against the write paths)
-----------------------------------------------------
  source_candidates (postgres_storage.py:256) is JSON TEXT — a list of candidate dicts.
  Candidate-level key is official_document_relevance_score (NOT document_relevance_score,
  which lives on official_evidence_results rows) and official_evidence_grade (NOT
  evidence_grade). Written by official_source_body.py:691-766 and
  official_evidence_resolution.py:390-413.
  Official-like = source_type in {official_government, public_institution}.

SAFETY: SELECT-only, engine.connect() (never begin()), no commit. Paged by id cursor
because source_candidates is ~1.2MB/row (postgres_storage.py:1394). Lazy DB import so
--selftest is fully offline. UTF-8 guarded prints.

Usage (in the Render Worker Shell, after commit + push + Worker redeploy + reopen Shell):
    PYTHONPATH=. python scripts/relevance_gate_probe.py
    PYTHONPATH=. python scripts/relevance_gate_probe.py --selftest      # offline, no DB
    PYTHONPATH=. python scripts/relevance_gate_probe.py --max-rows 2000 # cap the scan
    PYTHONPATH=. python scripts/relevance_gate_probe.py --examples 20   # more examples

Exit codes: 0 = report printed / engine unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


OFFICIAL_TYPES = {"official_government", "public_institution"}
STRONG = "strong_official_direct_support"
MEDIUM = "medium_official_contextual_support"

# Buckets, in the order they are reported.
B_STRONG = "strong"
B_MODERATE = "moderate"
B_WEAK_OK = "weak_concept_backed"
B_IRRELEVANT = "clearly_irrelevant"


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


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def is_official_like(candidate: dict) -> bool:
    return str(candidate.get("source_type") or "") in OFFICIAL_TYPES


def match_score(candidate: dict) -> float:
    """The app's own 3-way fallback chain (source_reliability_agent.py:288,
    frontend/scripts/main.js:4346-4349). Replicated so counts agree with the UI."""
    for key in (
        "official_evidence_score",
        "official_final_direct_match_score",
        "official_body_match_score",
    ):
        value = candidate.get(key)
        if value is not None and value != "":
            return _num(value)
    return 0.0


def concept_count(candidate: dict) -> int:
    """Material policy concepts the official body shared with the claim. This is the
    signal that a keyword collision does NOT produce: the Sewol decree shares raw
    strings but no policy concept with an oil-price claim."""
    concepts = candidate.get("official_body_matched_concepts")
    if isinstance(concepts, (list, tuple, set)):
        return len([c for c in concepts if str(c or "").strip()])
    return 0


def classify(candidate: dict) -> str:
    """Mirror the frontend sourceTrace() precedence (main.js:4345-4357), then split the
    공식 약한 후보 fall-through by whether ANY material concept was shared."""
    classification = str(
        candidate.get("official_evidence_classification")
        or candidate.get("official_direct_match_classification")
        or ""
    )
    body_match = bool(candidate.get("official_body_match"))
    score = match_score(candidate)

    if classification == STRONG or (body_match and score >= 75):
        return B_STRONG
    if classification == MEDIUM or body_match:
        return B_MODERATE
    # Fall-through == 공식 약한 후보. Zero shared concepts is the collision signature.
    return B_WEAK_OK if concept_count(candidate) > 0 else B_IRRELEVANT


def relevance_bin(score: float) -> str:
    """10-point bins over official_document_relevance_score. Negative is a real value
    here (penalties in official_relevance.py can push below zero), not a data error."""
    if score < 0:
        return "<0"
    if score >= 100:
        return "100+"
    low = int(score // 10) * 10
    return f"{low:>3}-{low + 9}"


def summarize_row(row_id, claims_raw, candidates_raw) -> dict:
    """Pure: turn one stored row into per-candidate bucket tallies + example records."""
    candidates = _loads(candidates_raw)
    if not isinstance(candidates, list):
        candidates = []
    official = [c for c in candidates if isinstance(c, dict) and is_official_like(c)]

    claims = _loads(claims_raw)
    claim_text = ""
    if isinstance(claims, list) and claims:
        first = claims[0]
        claim_text = str(first.get("claim_text") if isinstance(first, dict) else first or "")

    buckets = Counter()
    bins = Counter()
    examples = []
    for candidate in official:
        bucket = classify(candidate)
        buckets[bucket] += 1
        rel = _num(candidate.get("official_document_relevance_score"), 0.0)
        bins[(relevance_bin(rel), concept_count(candidate) > 0)] += 1
        if bucket == B_IRRELEVANT:
            examples.append(
                {
                    "row_id": row_id,
                    "claim": claim_text[:160],
                    "doc_title": str(candidate.get("title") or "")[:120],
                    "relevance": rel,
                    "match_score": match_score(candidate),
                    "grade": str(candidate.get("official_evidence_grade") or ""),
                    "terms": [str(t) for t in (candidate.get("official_body_matched_terms") or [])][:8],
                    "concepts": [str(c) for c in (candidate.get("official_body_matched_concepts") or [])][:8],
                }
            )

    return {
        "official_total": len(official),
        "buckets": buckets,
        "bins": bins,
        "examples": examples,
        # A row loses its LAST official candidate if every one is clearly-irrelevant.
        "would_be_emptied": bool(official) and buckets[B_IRRELEVANT] == len(official),
        "has_official": bool(official),
    }


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def report(totals: dict, examples: list, max_examples: int) -> None:
    rows = totals["rows"]
    rows_official = totals["rows_official"]
    cand_total = totals["cand_total"]
    buckets = totals["buckets"]
    bins = totals["bins"]

    p("")
    p("=== 1. CORPUS ===")
    p(f"  rows scanned                       : {rows}")
    p(f"  rows with >=1 official candidate   : {rows_official}  ({_pct(rows_official, rows)} of rows)")
    p(f"  official candidates total          : {cand_total}")

    p("")
    p("=== 2. BUCKETS (over official candidates) ===")
    p("    bucket                    count        % of official candidates")
    for key, label in (
        (B_STRONG, "strong (직접 근거)"),
        (B_MODERATE, "moderate (맥락 근거)"),
        (B_WEAK_OK, "weak but concept-backed"),
        (B_IRRELEVANT, "CLEARLY-IRRELEVANT"),
    ):
        p(f"    {label:<26}{buckets[key]:>7}        {_pct(buckets[key], cand_total)}")

    p("")
    p("=== 3. HISTOGRAM of official_document_relevance_score ===")
    p("    (split by whether ANY material policy concept was shared — a natural gap")
    p("     between the two columns is where a relevance floor could sit)")
    p("    bin          with-concepts   zero-concepts")
    seen_bins = sorted({b for (b, _) in bins}, key=lambda b: (b == "<0" and -1 or 0, b))
    for b in seen_bins:
        p(f"    {b:<12}{bins[(b, True)]:>13}{bins[(b, False)]:>16}")

    p("")
    p(f"=== 4. EXAMPLES from CLEARLY-IRRELEVANT (showing up to {max_examples}) ===")
    if not examples:
        p("    (none — the collision signature did not fire on this corpus)")
    for ex in examples[:max_examples]:
        p("")
        p(f"    row #{ex['row_id']}  relevance={ex['relevance']:.0f}  match={ex['match_score']:.0f}  grade={ex['grade'] or '-'}")
        p(f"      claim : {ex['claim']}")
        p(f"      doc   : {ex['doc_title']}")
        p(f"      terms : {', '.join(ex['terms']) or '(none)'}")
        p(f"      concpt: {', '.join(ex['concepts']) or '(none)'}")

    p("")
    p("=== 5. STAKES if clearly-irrelevant candidates were dropped ===")
    irrelevant = buckets[B_IRRELEVANT]
    p(f"  official candidates dropped        : {irrelevant}  ({_pct(irrelevant, cand_total)} of official candidates)")
    p(f"  rows touched (>=1 dropped)         : {totals['rows_touched']}  ({_pct(totals['rows_touched'], rows)} of ALL rows)")
    p(f"                                       ({_pct(totals['rows_touched'], rows_official)} of rows-with-official)")
    p(f"  rows losing their LAST official    : {totals['rows_emptied']}  ({_pct(totals['rows_emptied'], rows)} of ALL rows)")
    p("  ^ that last number is the risk line: those cards would go from 공식 약한 후보")
    p("    to no official candidate at all. Read it before choosing a floor.")

    p("")
    p("=== 6. EMBEDDING AVAILABILITY (for a future SEMANTIC gate) ===")
    p(f"  embedding_cache rows               : {totals['embed_rows']}")
    p(f"  embedding_cache rows, official-body: {totals['embed_official']}")
    p("  NOTE: the news side is embedded as 'title\\nclaim_text' by scripts/embed_backfill.py.")
    p("  If the official-body count is 0, SEMANTIC_MATCHING_ENABLED was off for this corpus")
    p("  (config.py:361 default False) and a true claim<->doc cosine CANNOT be computed from")
    p("  stored data — it needs a fresh embedding pass over official_body_text (which IS")
    p("  stored, 5000-char capped, in source_candidates). That is a cost to budget, not a")
    p("  free query.")

    p("")
    p("=== READ THIS BEFORE ACTING ===")
    p("  Every number above is LEXICAL. A keyword collision scores high on a keyword")
    p("  metric by construction, so the CLEARLY-IRRELEVANT bucket is defined by the")
    p("  collision SIGNATURE (weak fall-through + zero shared concepts), not by a low")
    p("  score. Treat section 3's gap as a candidate floor to VALIDATE against section 4's")
    p("  examples by hand — not as a threshold to ship unexamined.")


def run_live(max_rows: int, page: int, max_examples: int) -> int:
    p("=== RELEVANCE-GATE PROBE (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    totals = {
        "rows": 0,
        "rows_official": 0,
        "cand_total": 0,
        "rows_touched": 0,
        "rows_emptied": 0,
        "buckets": Counter(),
        "bins": Counter(),
        "embed_rows": 0,
        "embed_official": 0,
    }
    examples: list = []
    last_id = 0

    with engine.connect() as conn:
        while True:
            if max_rows and totals["rows"] >= max_rows:
                break
            limit = min(page, max_rows - totals["rows"]) if max_rows else page
            rows = conn.execute(
                sa.text(
                    "SELECT id, claims, source_candidates FROM analysis_results "
                    "WHERE id > :last ORDER BY id LIMIT :lim"
                ).bindparams(last=last_id, lim=limit)
            ).all()
            if not rows:
                break

            for row_id, claims_raw, candidates_raw in rows:
                last_id = row_id
                totals["rows"] += 1
                summary = summarize_row(row_id, claims_raw, candidates_raw)
                if not summary["has_official"]:
                    continue
                totals["rows_official"] += 1
                totals["cand_total"] += summary["official_total"]
                totals["buckets"].update(summary["buckets"])
                totals["bins"].update(summary["bins"])
                if summary["buckets"][B_IRRELEVANT]:
                    totals["rows_touched"] += 1
                if summary["would_be_emptied"]:
                    totals["rows_emptied"] += 1
                if len(examples) < max_examples * 4:
                    examples.extend(summary["examples"])

            p(f"  ... scanned {totals['rows']} rows (last id {last_id})")

        # Embedding availability, for costing a future semantic gate.
        try:
            totals["embed_rows"] = int(
                conn.execute(sa.text("SELECT count(*) FROM embedding_cache")).scalar() or 0
            )
            totals["embed_official"] = int(
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM embedding_cache "
                        "WHERE text_preview ILIKE :pattern"
                    ).bindparams(pattern="%official_body%")
                ).scalar()
                or 0
            )
        except Exception as exc:  # table may not exist on some environments
            p(f"  (embedding_cache probe skipped: {exc})")

    report(totals, examples, max_examples)
    return 0


def _selftest() -> int:
    """Offline check of the bucket logic against hand-built candidates."""
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    check("strong-by-classification", classify({"official_evidence_classification": STRONG}), B_STRONG)
    check(
        "strong-by-score",
        classify({"official_body_match": True, "official_evidence_score": 80}),
        B_STRONG,
    )
    check("medium-by-classification", classify({"official_evidence_classification": MEDIUM}), B_MODERATE)
    check(
        "medium-by-body-match",
        classify({"official_body_match": True, "official_evidence_score": 10}),
        B_MODERATE,
    )
    # The Sewol shape: official-like, fell through to weak, zero shared concepts.
    check(
        "sewol-collision",
        classify(
            {
                "source_type": "official_government",
                "official_body_match": False,
                "official_document_relevance_score": 55,
                "official_body_matched_terms": ["최고가격", "지원", "특별법"],
                "official_body_matched_concepts": [],
            }
        ),
        B_IRRELEVANT,
    )
    check(
        "weak-but-concept-backed",
        classify({"official_body_match": False, "official_body_matched_concepts": ["물가"]}),
        B_WEAK_OK,
    )
    # Fallback chain order.
    check("score-chain-primary", match_score({"official_evidence_score": 42}), 42.0)
    check(
        "score-chain-secondary",
        match_score({"official_final_direct_match_score": 33}),
        33.0,
    )
    check("score-chain-tertiary", match_score({"official_body_match_score": 21}), 21.0)
    check("score-chain-empty", match_score({}), 0.0)
    # Negative relevance is a real value, not an error.
    check("bin-negative", relevance_bin(-30.0), "<0")
    check("bin-mid", relevance_bin(55.0), " 50-59")
    check("bin-cap", relevance_bin(140.0), "100+")
    # Row summary end-to-end.
    summary = summarize_row(
        7,
        json.dumps(["정부는 석유 최고가격제를 시행했다."]),
        json.dumps(
            [
                {"source_type": "official_government", "official_body_matched_concepts": []},
                {"source_type": "established_news", "official_body_match": True},
            ]
        ),
    )
    check("row-official-count", summary["official_total"], 1)
    check("row-would-empty", summary["would_be_emptied"], True)
    check("row-example-claim", summary["examples"][0]["claim"][:6], "정부는 석유")

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK (13 checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RELEVANCE-GATE probe (SELECT-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check, no DB")
    parser.add_argument("--max-rows", type=int, default=0, help="cap rows scanned (0 = all)")
    parser.add_argument("--page", type=int, default=200, help="id-cursor page size")
    parser.add_argument("--examples", type=int, default=10, help="examples to print")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    return run_live(args.max_rows, args.page, args.examples)


if __name__ == "__main__":
    raise SystemExit(main())
