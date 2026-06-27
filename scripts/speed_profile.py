"""SPEED-1 Phase 1 — READ-ONLY per-stage timing harness for one analysis.

Measures WHERE the ~1m45s of a single search/analysis goes, so we know what (if
anything) to optimize. It runs the REAL pipeline once (main.analyze_pipeline — the
same path the site uses) and wraps each major stage with a perf_counter timer, then
prints a per-stage breakdown sorted longest-first.

NON-PERSISTING: it calls main.analyze_pipeline DIRECTLY (not the api_server /analyze
endpoint). analyze_pipeline does NOT write to Postgres — the DB row is written by
api_server.analyze(), which this probe bypasses. So NO analysis_results row and NO
live-site card are created. The only side effects are LOCAL files (.cache/ analysis
cache + the memory store), which are harmless and not shown on the site.

It does NOT change pipeline behavior/scoring/verdict logic — the wrappers only TIME
the functions and call straight through (read-only instrumentation, applied at
runtime via attribute wrapping; no pipeline file is edited).

COLD vs WARM: a FRESH query (not analyzed in the last 30 min) bypasses ANALYSIS_CACHE
+ NEWS_CACHE → the full cold path (matches a novel user search). Re-running the SAME
query within 30 min hits the analysis cache and returns near-instantly (which itself
proves caching is a lever). Use a fresh/unique query for a cold measurement.

Run in the Render Worker Shell (it needs API keys + Playwright, present there):
    git log --oneline -1
    PYTHONPATH=. python scripts/speed_profile.py --query "최저임금"
    PYTHONPATH=. python scripts/speed_profile.py --query "최저임금" --max-news 1
"""

from __future__ import annotations

import argparse
import functools
import sys
import time
from collections import OrderedDict
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# label -> [total_seconds, call_count]. A function called in a LOOP (per-news /
# per-source / per-claim) accumulates here, and call_count reveals the multiplier.
_TIMINGS: "OrderedDict[str, list]" = OrderedDict()


def _timed(label, fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            dt = time.perf_counter() - t0
            slot = _TIMINGS.setdefault(label, [0.0, 0])
            slot[0] += dt
            slot[1] += 1
    return wrapper


def _wrap(module, name):
    """Wrap module.name with a timer if it exists and is callable. Returns the
    label wrapped, or None."""
    fn = getattr(module, name, None)
    if fn is None or not callable(fn):
        return None
    setattr(module, name, _timed(name, fn))
    return name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="speed_profile")
    parser.add_argument("--query", type=str, default="최저임금")
    parser.add_argument("--max-news", type=int, default=1)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    import main as pipeline
    import llm_judge

    # The heavy stages, in pipeline order. Each is referenced by
    # _process_news_item_phase_a / analyze_pipeline as a module global on `main`,
    # so wrapping main.<name> intercepts the real call. Names not present are
    # reported as skipped (e.g. lazily-imported providers).
    main_stages = [
        # --- news + article ---
        "search_google_news_rss_with_meta",   # Google News RSS + URL decode + naver  (NETWORK)
        "fetch_article_body",                 # news article body fetch               (NETWORK)
        # --- claim / topic ---
        "normalize_claims",                   # claim extraction                      (CPU or LLM)
        "classify_policy_topic",              # topic classification                  (CPU or LLM)
        # --- official retrieval (prime suspects) ---
        "generate_official_source_candidates",# build official queries/candidates     (NETWORK?)
        "fetch_official_evidence",            # OFFICIAL CRAWL (gov.kr / Playwright)   (NETWORK/CRAWL)
        "enrich_official_source_candidates_with_bodies",  # official page body fetch  (NETWORK/CRAWL)
        "resolve_official_evidence",          # sentence matching                     (CPU)
        "evaluate_source_candidates",         # reliability scoring                   (CPU)
        # --- evidence / signals ---
        "extract_evidence_snippets",          # snippet extraction (+ embeddings?)    (CPU/NETWORK)
        "run_contradiction_checks",           # contradiction                         (CPU)
        "analyze_bias_framing",               # bias/framing                          (CPU)
        "compare_news_with_official_evidence",# comparison                            (CPU)
        # --- scoring / card ---
        "calculate_policy_confidence",        # confidence                            (CPU)
        "make_final_decision",                # decision                              (CPU)
        "build_verification_card",            # card assembly                         (CPU)
        "calibrate_final_decision",           # P2 calibration                        (CPU)
        # --- LLM reasoning (suspect) ---
        "run_ai_reasoning",                   # ai_reasoner LLM call (OpenAI/Claude)  (LLM)
    ]
    wrapped, skipped = [], []
    for name in main_stages:
        (wrapped if _wrap(pipeline, name) else skipped).append(name)

    # LLM judge (prejudge) lives in the llm_judge module — wrap its likely call
    # entry points so a gated judge LLM call is attributed, not hidden in remainder.
    judge_wrapped = []
    for name in ("run_prejudge", "run_llm_judge", "run_judge", "judge",
                 "call_llm_judge", "evaluate", "request_judge"):
        fn = getattr(llm_judge, name, None)
        if callable(fn):
            setattr(llm_judge, name, _timed("llm_judge." + name, fn))
            judge_wrapped.append(name)

    print("=" * 84)
    print(f"SPEED-1 profile — query={args.query!r} max_news={args.max_news}")
    print("=" * 84)
    print(f"wrapped main stages ({len(wrapped)}): {wrapped}")
    if skipped:
        print(f"NOT FOUND on main (skipped): {skipped}")
    print(f"wrapped llm_judge fns: {judge_wrapped or '(none found — judge time, if any, falls in remainder)'}")
    print("NOTE: lazily-imported providers (policy_briefing/national_law/fss) and any")
    print("uninstrumented work appear in the REMAINDER line below.\n")
    print("running analysis (cold path if the query is fresh)...\n")

    wall0 = time.perf_counter()
    try:
        report = pipeline.analyze_pipeline(query=args.query, max_news=args.max_news)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: analyze_pipeline raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    wall = time.perf_counter() - wall0

    items = sorted(_TIMINGS.items(), key=lambda kv: kv[1][0], reverse=True)
    instrumented = sum(v[0] for v in _TIMINGS.values())
    remainder = max(0.0, wall - instrumented)

    print("=" * 84)
    print(f"PER-STAGE BREAKDOWN (longest first)   total wall = {wall:7.2f} s")
    print("=" * 84)
    print(f"  {'seconds':>9}  {'calls':>5}  {'%wall':>6}  stage")
    for name, (secs, calls) in items:
        print(f"  {secs:9.2f}  {calls:5d}  {secs/wall*100 if wall else 0:5.1f}%  {name}")
    print(f"  {remainder:9.2f}  {'-':>5}  {remainder/wall*100 if wall else 0:5.1f}%  "
          f"REMAINDER (providers / framework / uninstrumented)")
    print("-" * 84)
    print(f"  {instrumented:9.2f}  {'':>5}  {instrumented/wall*100 if wall else 0:5.1f}%  "
          f"sum of instrumented stages")

    # surface a couple of decisive facts
    news_n = len((report or {}).get("news_results") or [])
    print("\n" + "=" * 84)
    print("READOUT")
    print("=" * 84)
    if items:
        top, (tsec, _) = items[0]
        print(f"  DOMINANT stage: {top}  ({tsec:.1f} s, {tsec/wall*100 if wall else 0:.0f}% of wall)")
    print(f"  news items processed: {news_n}")
    print("  CACHES: ANALYSIS_CACHE (.cache, 30min, query+news), NEWS_CACHE (30min),")
    print("          GNEWSDECODER_CACHE (24h). A FRESH query is the cold path; a SAME-query")
    print("          re-run within 30min hits ANALYSIS_CACHE and returns ~instantly.")
    print("  NO Postgres row / live-site card was written (analyze_pipeline persists nothing;")
    print("  only local .cache/ + memory files were touched).")
    print("  Tip: re-run with the SAME query to measure the WARM (cache-hit) path and confirm")
    print("       caching is the lever (expect a few seconds vs the cold full run).")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
