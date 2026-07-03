"""BACKFILL-ORCH — bounded, verdict-isolated Naver-lane backfill orchestrator (pin-OUT).

Implements _backfill_orch_design.md §1 EXACTLY: for ONE seed query, page the existing
Naver provider (sort=sim) and, per item, run the zero-cost gates IN ORDER —
in-run seen-set (make_article_id) → intake reject (_reject_title_reason: opinion/
obituary/political_subject) → DB dedupe (result_exists_by_url) — ALL BEFORE any LLM
spend; only then call the EXISTING per-item analyze pair
(main._process_news_item_phase_a → main._apply_news_item_phase_b), tag
``debug_summary["ingest_origin"]``, and persist via ``database.save_analysis_result``
(mirroring pipeline_worker.py:139 including its duplicate-status handling). The
phase_a reference (which holds the fetched article body in memory) is dropped after
each item — the body is never persisted (structural; see the design §2).

VERDICT-ISOLATED: Phase A/B are reused byte-identical — box / has_genuine / score /
verdict_label / policy_alert_level / the judge are untouched code paths. COPYRIGHT-SAFE:
TIER-F article originals are processed then discarded (no stored field holds a body).
BOUNDED: max_pages (== the hard Naver call cap for the single query), a hard analyzed-item
cap, and a per-step sleep. EXPLICIT-RUN ONLY: importing this module runs nothing; the CLI
requires --run (without it, the plan is printed and nothing executes). IDEMPOTENT:
re-running the same params creates 0 new rows (three dedupe layers). NEVER auto-deletes.

This module is pin-OUT (not in MIGRATED_FILES): all backfill logging lives here; no
pin-IN file gains a log site (331/16 unaffected). No main.py edit — the Phase-A kwargs
(analysis_cache_key / news_collection_debug) are synthesized with honest values.

Usage (Joe's Worker-Shell step AFTER commit+deploy+Export — never automatic):
    PYTHONPATH=. python backfill_orchestrator.py                 # prints the plan, runs NOTHING
    PYTHONPATH=. python backfill_orchestrator.py --selftest      # offline gate-ORDER check
    PYTHONPATH=. python backfill_orchestrator.py --run           # the bounded pilot (explicit)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from structured_logging import get_logger

log = get_logger(__name__)


# Pilot defaults (_backfill_orch_design.md §4) — overridable via CLI args.
PILOT_QUERY = "복지 예산"
PILOT_MAX_PAGES = 3
PILOT_ITEM_CAP = 30
PILOT_INGEST_ORIGIN = "backfill_pilot"
PILOT_SLEEP_S = 1.0
_DISPLAY = 100  # items per provider call (provider clamps to its MAX_DISPLAY)


def _p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def run_backfill(
    query: str = PILOT_QUERY,
    max_pages: int = PILOT_MAX_PAGES,
    item_cap: int = PILOT_ITEM_CAP,
    ingest_origin: str = PILOT_INGEST_ORIGIN,
    sleep_s: float = PILOT_SLEEP_S,
) -> dict:
    """Run ONE bounded backfill pass for ``query``. Returns a summary counter dict.

    All pipeline imports are lazy (inside this function) so importing the module is
    side-effect-free and cheap. Per-item analysis failures are counted and skipped —
    one bad article never aborts the run.
    """
    # Lazy imports — reused verbatim, never re-implemented.
    from providers import get_search_provider
    from news_collector import _reject_title_reason
    from database import get_result_id_by_url, result_exists_by_url, save_analysis_result
    from memory_store import load_policy_memory, make_article_id
    from text_utils import sanitize_data
    import main as pipeline_main

    summary = {
        "query": query, "max_pages": max_pages, "item_cap": item_cap,
        "ingest_origin": ingest_origin, "naver_calls": 0, "pages_with_items": 0,
        "items_returned": 0, "skipped_seen": 0, "skipped_rejected": 0,
        "skipped_existing": 0, "analyzed": 0, "saved_new": 0,
        "save_duplicates": 0, "item_failures": 0, "saved_ids": [],
    }

    provider = get_search_provider("naver")
    if not getattr(provider, "available", False):
        _p(f"Naver provider unavailable: {getattr(provider, 'reason', 'unknown')} — nothing run.")
        summary["error"] = getattr(provider, "reason", "provider unavailable")
        return summary

    _p(f"[Backfill] START query={_ascii(query)} max_pages={max_pages} (== Naver call cap) "
       f"item_cap={item_cap} ingest_origin={_ascii(ingest_origin)} sleep_s={sleep_s}")
    log.info(
        "[Backfill] run start",
        extra={"backfill_query": query, "max_pages": max_pages,
               "item_cap": item_cap, "ingest_origin": ingest_origin},
    )

    memory = load_policy_memory()  # once; Phase A reads it, Phase B mutates it serially
    seen_ids: set = set()

    for page in range(max_pages):
        if summary["analyzed"] >= item_cap:
            break
        start = 1 + page * _DISPLAY
        summary["naver_calls"] += 1
        result = provider.search(query, limit=_DISPLAY, start=start, sort="sim")
        hits = result.get("items") or []
        if not hits:
            _p(f"[Backfill] page {page + 1}: no items — paging exhausted.")
            break
        summary["pages_with_items"] += 1

        for item in hits:
            if summary["analyzed"] >= item_cap:
                _p(f"[Backfill] hard item cap ({item_cap}) reached — stopping.")
                break
            title = (item.get("title") or "").strip()
            url = (item.get("original_url") or "").strip()
            if not title or not url:
                continue
            summary["items_returned"] += 1

            # --- ZERO-COST GATES, IN ORDER (all before any LLM spend) -----------
            # Gate 1: in-run seen-set (same-run repeat of the same article).
            _seen_key = make_article_id(title, url)
            if _seen_key in seen_ids:
                summary["skipped_seen"] += 1
                continue
            seen_ids.add(_seen_key)
            # Gate 2: intake reject — opinion/obituary/political_subject (reused).
            reject_reason = _reject_title_reason(title, query=query)
            if reject_reason:
                summary["skipped_rejected"] += 1
                log.info(
                    "[Backfill] rejected at intake",
                    extra={"reject_reason": reject_reason, "backfill_title": title[:120]},
                )
                continue
            # Gate 3: DB dedupe — the M38 key; a stored URL costs nothing (reused).
            if result_exists_by_url(url):
                summary["skipped_existing"] += 1
                continue

            # --- THE SPEND: the existing per-item analyze pair, unchanged --------
            try:
                phase_a = pipeline_main._process_news_item_phase_a(
                    item,
                    index=summary["analyzed"] + 1,
                    total=item_cap,
                    memory_snapshot=memory,
                    query=query,
                    # Honest synthesized values (design §1 / surprise #1): backfill has
                    # no news-response cache; the source really is naver_api.
                    news_collection_debug={
                        "news_cache_hit": False,
                        "news_cache_key": None,
                        "news_cache_ttl_seconds": None,
                        "news_collection_mode": "backfill",
                        "collection_source": "naver_api",
                    },
                    analysis_cache_key=f"backfill:{query}:p{page + 1}",
                )
                out = pipeline_main._apply_news_item_phase_b(phase_a, memory)
                summary["analyzed"] += 1

                api_result = (out.get("report_item") or {}).get("api_result") or {}
                if api_result:
                    # Provenance tag — additive debug_summary JSON key (design §5).
                    ds = api_result.get("debug_summary")
                    if isinstance(ds, dict):
                        ds["ingest_origin"] = ingest_origin
                    # Persist — mirrors pipeline_worker._persist_results (:137-157):
                    # sanitize, save, handle the duplicate status the same way.
                    api_result = sanitize_data(api_result)
                    save_status = save_analysis_result(api_result, query=query)
                    if save_status.get("duplicate"):
                        summary["save_duplicates"] += 1
                        try:
                            existing = get_result_id_by_url(api_result.get("original_url") or "")
                            if existing is not None:
                                summary["saved_ids"].append(int(existing))
                        except Exception:  # noqa: BLE001 — id lookup is best-effort
                            log.warning("[Backfill] dedup id lookup failed",
                                        extra={"backfill_query": query})
                    else:
                        new_id = save_status.get("id")
                        if new_id is not None:
                            summary["saved_new"] += 1
                            summary["saved_ids"].append(int(new_id))
            except Exception as exc:  # noqa: BLE001 — one bad article never aborts the run
                summary["item_failures"] += 1
                log.warning(
                    "[Backfill] item failed; continuing",
                    extra={"exception_type": type(exc).__name__,
                           "exception_message": str(exc)[:300],
                           "backfill_title": title[:120]},
                )
            finally:
                # Copyright discard, belt-and-braces (design §2): the fetched article
                # body lives only inside phase_a; drop the reference immediately.
                phase_a = None
                out = None

            time.sleep(sleep_s)  # pace the LLM APIs between analyzed items

        time.sleep(sleep_s)  # pace the Naver API between pages

    _p(f"[Backfill] DONE {json.dumps({k: v for k, v in summary.items() if k != 'saved_ids'}, ensure_ascii=True)}")
    _p(f"[Backfill] saved ids: {summary['saved_ids']}")
    log.info("[Backfill] run done", extra={k: v for k, v in summary.items() if k != "saved_ids"})
    return summary


def _run_selftest() -> int:
    """Offline structural check (no network / DB / LLM): the zero-cost gates appear
    in run_backfill's source IN ORDER, all BEFORE the Phase-A call, and the save
    mirrors the pipeline_worker pattern. Import-safety is implied by running at all."""
    import inspect

    src = inspect.getsource(run_backfill)
    markers = [
        "_seen_key",                     # gate 1: in-run seen-set
        "_reject_title_reason(title",    # gate 2: intake reject
        "result_exists_by_url(url",      # gate 3: DB dedupe
        "_process_news_item_phase_a",    # the spend starts here
        "_apply_news_item_phase_b",
        'ds["ingest_origin"]',           # provenance tag before save
        "save_analysis_result(",         # the pipeline_worker save pattern
    ]
    positions = [src.find(m) for m in markers]
    failures = []
    for name, pos in zip(markers, positions):
        if pos == -1:
            failures.append(f"marker missing from run_backfill: {name}")
    if not failures and positions != sorted(positions):
        failures.append(f"gate/call ORDER wrong: {list(zip(markers, positions))}")
    if failures:
        _p("SELFTEST: FAIL")
        for f in failures:
            _p(f"  - {f}")
        return 1
    _p("SELFTEST: PASS — gates (seen-set -> reject -> exists) precede Phase A; "
       "ingest_origin tag precedes save; save mirrors pipeline_worker.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded Naver-lane backfill orchestrator. Runs NOTHING without --run "
                    "(prints the plan). --selftest = offline gate-order check.",
    )
    parser.add_argument("--run", action="store_true",
                        help="Actually execute the bounded backfill (explicit operator step).")
    parser.add_argument("--selftest", action="store_true",
                        help="Offline structural gate-order check (no network / DB / LLM).")
    parser.add_argument("--query", default=PILOT_QUERY)
    parser.add_argument("--max-pages", type=int, default=PILOT_MAX_PAGES)
    parser.add_argument("--cap", type=int, default=PILOT_ITEM_CAP)
    parser.add_argument("--ingest-origin", default=PILOT_INGEST_ORIGIN)
    parser.add_argument("--sleep", type=float, default=PILOT_SLEEP_S)
    args = parser.parse_args()

    if args.selftest:
        return _run_selftest()
    if not args.run:
        _p("PLAN (nothing executed — pass --run to execute):")
        _p(f"  query={_ascii(args.query)} max_pages={args.max_pages} cap={args.cap} "
           f"ingest_origin={_ascii(args.ingest_origin)} sleep_s={args.sleep}")
        _p("  Gates before spend: seen-set -> _reject_title_reason -> result_exists_by_url.")
        _p("  Then: Phase A -> Phase B -> ingest_origin tag -> save_analysis_result.")
        return 0

    run_backfill(
        query=args.query,
        max_pages=args.max_pages,
        item_cap=args.cap,
        ingest_origin=args.ingest_origin,
        sleep_s=args.sleep,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
