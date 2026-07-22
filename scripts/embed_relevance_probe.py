"""EMBED-RELEVANCE PROBE — READ-ONLY measurement of claim<->official-candidate
SEMANTIC relevance via real embedding cosine, to decide go/no-go on item ④
(an embedding floor at candidate selection).

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE.
No verdict / pipeline / matcher / display change. NO threshold is applied
anywhere — this only measures where a floor WOULD sit and what it WOULD drop.
External calls: embedding-API reads only (OpenAI text-embedding-3-small).

HOW THIS DIFFERS FROM scripts/relevance_gate_probe.py (7/18-19)
---------------------------------------------------------------
That probe measured the LEXICAL path and proved the concept-matching path is
non-functional (SEMANTIC_MATCHING_ENABLED default False, official-body
embeddings absent) — every signal it had was circular for keyword collisions.
This probe computes the actual embedding cosine( claim, candidate title ) at
run time, which is exactly the signal an item-④ gate would use.

DATA SHAPE (confirmed against live rows 8027 / 13591-13593 on 2026-07-22)
-------------------------------------------------------------------------
  * claims                 : JSON list of plain STRINGS (no claim_text column
                             on the live table — it predates that column).
  * source_candidates      : JSON list of dicts. Candidate title is `title`
                             (always present); official-like = `source_type`
                             in {official_government, public_institution}.
                             `raw_text` / `snippet` / `official_body_text` are
                             ABSENT (raw_text stripped 7/7) — title is the one
                             reliable text field. `reliability_reason` exists
                             (~60 chars, mostly boilerplate) — appended only
                             with --with-reason.
  * candidate.claim_index  : which claim the candidate was matched against —
                             used to pair each candidate with ITS claim.
  * genuine predicate      : source_reliability_summary
                             ["has_genuine_official_support"] (bool), old-row
                             fallback debug_summary.official_body_matches > 0
                             — the SAME predicate the top-line uses
                             (postgres_storage.py:1549-1557).
  * There is NO stored per-candidate "shown" flag — the display derives it at
    read time. We therefore report ALL official-like candidates AND,
    separately, the TOP-1 per card by the app's own 3-way score chain
    (official_evidence_score -> official_final_direct_match_score ->
    official_body_match_score), which mirrors what selection surfaces.

WHAT IT PRINTS
--------------
  1. CORPUS: cards sampled, official candidates, unique texts embedded, API calls.
  2. COSINE BUCKETS over ALL official candidates and over TOP-1-per-card:
     <0.15 clearly-irrelevant / 0.15-0.35 weak / 0.35-0.55 moderate / >0.55
     on-topic — counts + %. The decision number = % clearly-irrelevant.
  3. SPLIT by genuine vs non-genuine card (over-suppression check: where do
     GENUINE cards' candidate cosines sit? A floor that clips them is unsafe).
  4. EXAMPLES (~10/bucket): id + claim snippet + title + cosine, with included
     ids (8027 Sewol expected near-0) flagged, so separation can be eyeballed.
  5. SUGGESTED EMPIRICAL FLOOR — or "no clean separation" if genuine-card
     cosines overlap the irrelevant mass.

SAMPLING: ORDER BY random() over rows with >=1 official-like candidate,
cap --cards (default 800) to bound cost — NOT id-front (biased). Explicitly
includes --include ids (default 8027) on top of the random draw.

COST/RUNTIME (text-embedding-3-small, $0.02/1M tok): ~800 cards ≈ up to
~2000 unique claims + ~1500 unique titles ≈ ~3500 embed calls x ~50 tok
≈ 0.2M tok ≈ <$0.01. Calls are sequential (provider has no batch endpoint):
expect ~10-15 min. In-run cache dedupes repeated titles (laws repeat a lot).

SAFETY: engine.connect() only (never begin()), no commit. Lazy DB import so
--selftest is fully offline (uses the deterministic hash provider). UTF-8
guarded prints.

Usage (Render Worker Shell, after commit+push+redeploy+reopen Shell):
    SEMANTIC_MATCHING_ENABLED=true PYTHONPATH=. python scripts/embed_relevance_probe.py
    (OPENAI_API_KEY / EMBEDDING_PROVIDER=openai / EMBEDDING_MODEL are already
     Worker env; SEMANTIC_MATCHING_ENABLED must be set inline because its
     default False makes get_active_provider() return the disabled provider.)
    PYTHONPATH=. python scripts/embed_relevance_probe.py --selftest   # offline
    ... --cards 200            # cheaper trial run
    ... --include 8027,1234    # force-include known cases

Exit codes: 0 = report printed / provider or engine unavailable (reported);
1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import math
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


OFFICIAL_TYPES = {"official_government", "public_institution"}

# Bucket edges (low -> high). NOT a threshold applied anywhere — report bins only.
BUCKET_EDGES = (0.15, 0.35, 0.55)
BUCKET_LABELS = (
    "<0.15  clearly-irrelevant",
    "0.15-0.35  weak",
    "0.35-0.55  moderate",
    ">0.55  on-topic",
)
# Minimum claim length to count as "substantive" when claim_index is unusable.
MIN_CLAIM_CHARS = 15


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


def is_official_like(candidate: dict) -> bool:
    return str(candidate.get("source_type") or "") in OFFICIAL_TYPES


def match_score(candidate: dict) -> float:
    """The app's own 3-way fallback chain (source_reliability_agent.py:288,
    frontend/scripts/main.js:4346-4349) — used ONLY to pick the top-1 candidate
    per card so we can report "what selection surfaces" separately."""
    for key in (
        "official_evidence_score",
        "official_final_direct_match_score",
        "official_body_match_score",
    ):
        value = candidate.get(key)
        if value is not None and value != "":
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def is_genuine(srs_raw, debug_raw) -> bool:
    """The top-line predicate: persisted has_genuine_official_support bool,
    old-row fallback debug_summary.official_body_matches > 0
    (postgres_storage.py:1549-1557, main.js officialStatusLabel)."""
    srs = _loads(srs_raw)
    if isinstance(srs, dict):
        genuine = srs.get("has_genuine_official_support")
        if isinstance(genuine, bool):
            return genuine
    debug = _loads(debug_raw)
    if isinstance(debug, dict):
        try:
            return int(debug.get("official_body_matches") or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


def claim_for(candidate: dict, claims: list) -> str:
    """Pair the candidate with ITS claim via claim_index when valid; otherwise
    the first substantive claim string (claims are plain strings on the live
    table — claim_text never existed there)."""
    texts = [str(c or "").strip() for c in claims if isinstance(c, str)]
    idx = candidate.get("claim_index")
    if isinstance(idx, int) and 0 <= idx < len(texts) and len(texts[idx]) >= MIN_CLAIM_CHARS:
        return texts[idx]
    for text in texts:
        if len(text) >= MIN_CLAIM_CHARS:
            return text
    return texts[0] if texts else ""


def candidate_text(candidate: dict, with_reason: bool) -> str:
    """Title is the one reliable text field (raw_text stripped 7/7)."""
    title = str(candidate.get("title") or "").strip()
    if with_reason:
        reason = str(candidate.get("reliability_reason") or "").strip()
        if reason:
            return f"{title}\n{reason}"
    return title


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def bucket_index(value: float) -> int:
    for i, edge in enumerate(BUCKET_EDGES):
        if value < edge:
            return i
    return len(BUCKET_EDGES)


class EmbedCache:
    """In-run dedupe so repeated titles (laws recur constantly) and claims are
    embedded once. Counts API calls for the cost line."""

    def __init__(self, provider):
        self.provider = provider
        self.store: dict = {}
        self.calls = 0

    def get(self, text: str):
        key = text.strip()
        if not key:
            return None
        if key in self.store:
            return self.store[key]
        vector = self.provider.get_embedding(key)
        self.calls += 1
        self.store[key] = vector
        return vector


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def _bucket_table(counts: Counter, total: int, indent: str = "    ") -> None:
    for i, label in enumerate(BUCKET_LABELS):
        p(f"{indent}{label:<28}{counts[i]:>7}   {_pct(counts[i], total)}")


def analyze(records: list, examples_per_bucket: int) -> dict:
    """Pure aggregation over per-candidate records:
    {card_id, genuine, is_top1, included, cos, claim, title}."""
    all_counts, top1_counts = Counter(), Counter()
    split_counts = {True: Counter(), False: Counter()}  # genuine -> bucket
    genuine_cos, nongenuine_cos = [], []
    examples = defaultdict(list)

    for r in records:
        b = bucket_index(r["cos"])
        all_counts[b] += 1
        if r["is_top1"]:
            top1_counts[b] += 1
        split_counts[r["genuine"]][b] += 1
        (genuine_cos if r["genuine"] else nongenuine_cos).append(r["cos"])
        bucket_examples = examples[b]
        if r["included"] or len(bucket_examples) < examples_per_bucket * 3:
            bucket_examples.append(r)

    return {
        "all": all_counts,
        "top1": top1_counts,
        "split": split_counts,
        "genuine_cos": sorted(genuine_cos),
        "nongenuine_cos": sorted(nongenuine_cos),
        "examples": examples,
        "total": len(records),
        "top1_total": sum(top1_counts.values()),
    }


def _percentile(sorted_values: list, q: float) -> float:
    if not sorted_values:
        return float("nan")
    pos = min(len(sorted_values) - 1, max(0, int(q * (len(sorted_values) - 1))))
    return sorted_values[pos]


def suggest_floor(genuine_cos: list, nongenuine_cos: list) -> str:
    """Empirical floor suggestion: where the clearly-irrelevant mass sits vs
    the low tail (p05) of GENUINE cards' candidate cosines. Advisory text only
    — nothing is applied."""
    if not genuine_cos:
        return "no genuine-card candidates sampled — cannot bound over-suppression; no floor suggested"
    g05 = _percentile(genuine_cos, 0.05)
    g10 = _percentile(genuine_cos, 0.10)
    low_cut = BUCKET_EDGES[0]
    if g05 >= low_cut:
        floor = round(min(g05 * 0.8, low_cut), 3)
        return (
            f"genuine p05={g05:.3f} p10={g10:.3f} sit ABOVE the {low_cut} low cut -> "
            f"a floor around {floor} drops junk without touching genuine matches (validate examples by hand)"
        )
    return (
        f"NO CLEAN SEPARATION: genuine p05={g05:.3f} p10={g10:.3f} fall below the "
        f"{low_cut} low cut — a floor there would clip real matches (over-suppression risk); do not build on cosine alone"
    )


def report(stats: dict, meta: dict, examples_per_bucket: int) -> None:
    p("")
    p("=== 1. CORPUS ===")
    p(f"  cards sampled (random)             : {meta['cards_random']}")
    p(f"  cards force-included               : {meta['cards_included']}  (ids: {meta['included_ids']})")
    p(f"  cards genuine / non-genuine        : {meta['cards_genuine']} / {meta['cards_nongenuine']}")
    p(f"  official candidates measured       : {stats['total']}")
    p(f"  candidates skipped (no embedding)  : {meta['skipped']}")
    p(f"  unique texts embedded / API calls  : {meta['unique_texts']} / {meta['api_calls']}")

    p("")
    p("=== 2. COSINE BUCKETS ===")
    p("  ALL official candidates:")
    _bucket_table(stats["all"], stats["total"])
    p("  TOP-1 per card (what selection surfaces):")
    _bucket_table(stats["top1"], stats["top1_total"])
    irrelevant = stats["all"][0]
    p("")
    p(f"  >>> DECISION NUMBER: {_pct(irrelevant, stats['total']).strip()} of official candidates are")
    p(f"      clearly-irrelevant (cosine < {BUCKET_EDGES[0]}); top-1-only: {_pct(stats['top1'][0], stats['top1_total']).strip()}")

    p("")
    p("=== 3. GENUINE vs NON-GENUINE SPLIT (over-suppression check) ===")
    for flag, label in ((True, "GENUINE cards"), (False, "NON-GENUINE cards")):
        counts = stats["split"][flag]
        p(f"  {label} ({sum(counts.values())} candidates):")
        _bucket_table(counts, sum(counts.values()))
    g = stats["genuine_cos"]
    if g:
        p(f"  genuine-card cosine percentiles: p05={_percentile(g, 0.05):.3f} "
          f"p25={_percentile(g, 0.25):.3f} p50={_percentile(g, 0.50):.3f} p95={_percentile(g, 0.95):.3f}")

    p("")
    p(f"=== 4. EXAMPLES (up to {examples_per_bucket}/bucket; * = force-included id) ===")
    for i, label in enumerate(BUCKET_LABELS):
        p(f"  [{label}]")
        shown = 0
        bucket_examples = stats["examples"].get(i, [])
        # Force-included ids (e.g. 8027 Sewol) always print first.
        for ex in sorted(bucket_examples, key=lambda e: not e["included"]):
            if shown >= examples_per_bucket and not ex["included"]:
                continue
            mark = "*" if ex["included"] else " "
            p(f"   {mark}#{ex['card_id']}  cos={ex['cos']:.3f}  {'genuine' if ex['genuine'] else 'non-genuine'}")
            p(f"      claim: {ex['claim'][:90]}")
            p(f"      title: {ex['title'][:90]}")
            shown += 1
        if not bucket_examples:
            p("      (none)")

    p("")
    p("=== 5. SUGGESTED EMPIRICAL FLOOR (advisory only — nothing applied) ===")
    p(f"  {suggest_floor(stats['genuine_cos'], stats['nongenuine_cos'])}")


def run_live(cards: int, include_ids: list, examples_per_bucket: int, with_reason: bool) -> int:
    p("=== EMBED-RELEVANCE PROBE (READ-ONLY; SELECT + embedding-API reads only) ===")

    import semantic_embeddings

    provider = semantic_embeddings.get_active_provider()
    if not getattr(provider, "available", False):
        p(f"Embedding provider unavailable: {getattr(provider, 'reason', 'unknown')}")
        p("Run with SEMANTIC_MATCHING_ENABLED=true (plus Worker's OPENAI_API_KEY /")
        p("EMBEDDING_PROVIDER=openai / EMBEDDING_MODEL). Nothing was measured.")
        return 0
    p(f"provider={provider.name} model={getattr(provider, 'model', '?')}")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        return 0

    like_filter = (
        "(source_candidates LIKE '%official_government%' "
        "OR source_candidates LIKE '%public_institution%')"
    )
    columns = "id, claims, source_candidates, source_reliability_summary, debug_summary"
    with engine.connect() as conn:
        rows = list(
            conn.execute(
                sa.text(
                    f"SELECT {columns} FROM analysis_results "
                    f"WHERE {like_filter} ORDER BY random() LIMIT :cap"
                ).bindparams(cap=cards)
            ).all()
        )
        sampled_ids = {r[0] for r in rows}
        forced = [i for i in include_ids if i not in sampled_ids]
        if forced:
            rows.extend(
                conn.execute(
                    sa.text(
                        f"SELECT {columns} FROM analysis_results WHERE id = ANY(:ids)"
                    ).bindparams(ids=forced)
                ).all()
            )

    cache = EmbedCache(provider)
    records = []
    meta = {
        "cards_random": 0, "cards_included": 0, "cards_genuine": 0,
        "cards_nongenuine": 0, "skipped": 0,
        "included_ids": ",".join(str(i) for i in include_ids) or "-",
    }
    include_set = set(include_ids)

    for n, (row_id, claims_raw, cands_raw, srs_raw, debug_raw) in enumerate(rows, 1):
        candidates = _loads(cands_raw)
        claims = _loads(claims_raw)
        if not isinstance(candidates, list) or not isinstance(claims, list):
            continue
        official = [c for c in candidates if isinstance(c, dict) and is_official_like(c)]
        if not official:
            continue  # LIKE filter is approximate; re-check in Python

        included = row_id in include_set
        meta["cards_included" if included else "cards_random"] += 1
        genuine = is_genuine(srs_raw, debug_raw)
        meta["cards_genuine" if genuine else "cards_nongenuine"] += 1
        top1 = max(official, key=match_score)

        for candidate in official:
            claim = claim_for(candidate, claims)
            title = candidate_text(candidate, with_reason)
            claim_vec = cache.get(claim)
            title_vec = cache.get(title)
            if claim_vec is None or title_vec is None:
                meta["skipped"] += 1
                continue
            records.append({
                "card_id": row_id,
                "genuine": genuine,
                "is_top1": candidate is top1,
                "included": included,
                "cos": cosine(claim_vec, title_vec),
                "claim": claim,
                "title": str(candidate.get("title") or ""),
            })
        if n % 50 == 0:
            p(f"  ... {n}/{len(rows)} cards, {cache.calls} embed calls")

    meta["unique_texts"] = len(cache.store)
    meta["api_calls"] = cache.calls
    if not records:
        p("No candidates measured (all skipped or empty sample).")
        return 0
    report(analyze(records, examples_per_bucket), meta, examples_per_bucket)
    return 0


def _selftest() -> int:
    """Offline: deterministic provider + hand-built rows. No DB, no network."""
    import semantic_embeddings

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    check("bucket-low", bucket_index(0.05), 0)
    check("bucket-weak", bucket_index(0.20), 1)
    check("bucket-mod", bucket_index(0.40), 2)
    check("bucket-high", bucket_index(0.80), 3)
    check("bucket-edge", bucket_index(0.15), 1)

    check("genuine-bool", is_genuine('{"has_genuine_official_support": true}', None), True)
    check("genuine-fallback", is_genuine("{}", '{"official_body_matches": 2}'), True)
    check("genuine-neither", is_genuine("{}", '{"official_body_matches": 0}'), False)
    check("genuine-null", is_genuine(None, None), False)

    claims = ["짧다", "정부는 석유 최고가격제를 시행했다 물가 안정 목적", "두번째 실질 주장 텍스트입니다"]
    check("claim-by-index", claim_for({"claim_index": 2}, claims), claims[2])
    check("claim-index-short", claim_for({"claim_index": 0}, claims), claims[1])
    check("claim-no-index", claim_for({}, claims), claims[1])
    check("claim-empty", claim_for({}, []), "")

    check("cand-text-title", candidate_text({"title": "특별법", "reliability_reason": "r"}, False), "특별법")
    check("cand-text-reason", candidate_text({"title": "특별법", "reliability_reason": "r"}, True), "특별법\nr")

    check("cos-identical", round(cosine([1.0, 0.0], [1.0, 0.0]), 6), 1.0)
    check("cos-orthogonal", round(cosine([1.0, 0.0], [0.0, 1.0]), 6), 0.0)
    check("cos-zero-guard", cosine([0.0], [0.0]), 0.0)

    provider = semantic_embeddings.DeterministicHashEmbeddingProvider()
    cache = EmbedCache(provider)
    v1, v2 = cache.get("기준금리 인상"), cache.get("기준금리 인상")
    check("cache-dedupe", cache.calls, 1)
    check("cache-same-vector", v1 is v2, True)
    same = cosine(cache.get("한국은행 기준금리 인상 결정"), cache.get("한국은행 기준금리 인상"))
    diff = cosine(cache.get("한국은행 기준금리 인상 결정"), cache.get("세월호 참사 피해구제 특별법 시행령"))
    check("cosine-separates", same > diff, True)

    records = [
        {"card_id": 1, "genuine": True, "is_top1": True, "included": False,
         "cos": 0.7, "claim": "c", "title": "t"},
        {"card_id": 2, "genuine": False, "is_top1": True, "included": True,
         "cos": 0.05, "claim": "c", "title": "t"},
    ]
    stats = analyze(records, 5)
    check("analyze-total", stats["total"], 2)
    check("analyze-irrelevant", stats["all"][0], 1)
    check("analyze-top1-total", stats["top1_total"], 2)
    check("analyze-split-genuine", stats["split"][True][3], 1)
    check("floor-separated", "drops junk" in suggest_floor([0.5, 0.6, 0.7], [0.05]), True)
    check("floor-overlap", "NO CLEAN SEPARATION" in suggest_floor([0.05, 0.5], [0.05]), True)
    check("floor-no-genuine", "no floor suggested" in suggest_floor([], [0.05]), True)

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK (26 checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="EMBED-RELEVANCE probe (read-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check, no DB/network")
    parser.add_argument("--cards", type=int, default=800, help="random-sample card cap")
    parser.add_argument("--include", default="8027", help="comma-separated ids to force-include")
    parser.add_argument("--examples", type=int, default=10, help="examples per bucket")
    parser.add_argument("--with-reason", action="store_true",
                        help="append reliability_reason to the embedded candidate text")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    include_ids = [int(x) for x in str(args.include).split(",") if x.strip().isdigit()]
    return run_live(args.cards, include_ids, args.examples, args.with_reason)


if __name__ == "__main__":
    raise SystemExit(main())
