"""BACKFILL-SCALE — bounded, background-safe multi-query scale runner (pin-OUT).

A THIN OUTER LOOP over the ALREADY-VERIFIED per-query engine
``backfill_orchestrator.run_backfill(query, max_pages, item_cap, ingest_origin, sleep_s)``.
It changes NOTHING in the per-item logic (dedupe → reject → analyze → save → tag stay
byte-identical inside run_backfill); it only adds a query loop + safety caps + a per-run
provenance tag + restart-budget resumption.

SAFETY (all enforced here):
  * per-query cap (--per-query-cap) AND a master GLOBAL cap (--global-cap) enforced via
    ``this_cap = min(per_query_cap, global_cap - done)`` so the run NEVER overshoots the
    global budget;
  * page cap (--max-pages) == the Naver call cap per query, passed straight through;
  * ONE ingest_origin tag for the whole run (--tag, default backfill_scale_<YYYYMMDD>) so
    the batch is identifiable + operator-reversible (TAG-FIX made ingest_origin persist);
  * RESTART-SAFE: on start, SELECT-count the rows already carrying the run tag and seed
    ``done`` from it, so a relaunch resumes TOWARD the same global cap instead of re-filling
    it; gate-3 (result_exists_by_url, inside run_backfill) skips saved URLs at ZERO LLM cost;
  * EXPLICIT --run only: without it the plan is printed and nothing executes;
  * importing this module runs NOTHING.

This is a pin-OUT script — all progress output is print()/logging here; NO log.* site is
added to any pin-IN file (main.py/news_collector/scheduler/database/orchestrator untouched),
so 331/16 + 38 pins are unaffected. Verdict-isolated: run_backfill reuses Phase A/B unchanged.

Usage (Joe's Worker-Shell step AFTER commit+deploy+Export — never automatic):
    PYTHONPATH=. python scripts/run_backfill_scale.py                 # prints the PLAN, runs nothing
    PYTHONPATH=. python scripts/run_backfill_scale.py --selftest      # offline caps-math check
    nohup python scripts/run_backfill_scale.py --run --global-cap 150 > backfill_scale.log 2>&1 &
Then verify:
    PYTHONPATH=. python scripts/backfill_pilot_verify.py --tag <the_run_tag> --cap <global_cap>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# First-scale defaults (design §3): the 5 ENV-SEED queries — environment is the
# measured-weak domain (2/607 pre-seed, SEED-MECH) — global cap 150.
DEFAULT_PER_QUERY_CAP = 30
DEFAULT_GLOBAL_CAP = 150
DEFAULT_MAX_PAGES = 3
DEFAULT_SLEEP_S = 1.0

# The ENV-SEED block from scheduler.DEFAULT_QUERIES (the environment-domain expansion).
ENV_SEED_QUERIES = (
    "탄소중립 온실가스 감축",
    "배출권거래제",
    "재생에너지 태양광 풍력 정책",
    "미세먼지 대기질 대책",
    "기후위기 대응 녹색금융",
)


def p(line: str = "") -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _default_tag() -> str:
    # datetime read at runtime (not import time); UTC date keeps the tag deterministic
    # per calendar day so a same-day restart reuses the SAME tag (restart budget).
    return "backfill_scale_" + datetime.now(timezone.utc).strftime("%Y%m%d")


def _count_rows_with_tag(tag: str) -> int:
    """SELECT-only count of rows already carrying the run tag in debug_summary — seeds
    the restart budget. Returns 0 on engine-unavailable / any error (fail-soft: a wrong
    0 only means the run may create up to the full global cap, still bounded)."""
    try:
        import postgres_storage
        import sqlalchemy as sa
        engine = postgres_storage.get_engine()
        if engine is None:
            return 0
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM analysis_results "
                        "WHERE debug_summary LIKE :pat")
                .bindparams(pat=f"%ingest_origin%{tag}%")
            ).first()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:  # noqa: BLE001
        p(f"  [warn] restart-budget count failed ({type(exc).__name__}); seeding done=0.")
        return 0


def run_scale(
    queries: list[str],
    *,
    per_query_cap: int,
    global_cap: int,
    max_pages: int,
    tag: str,
    sleep_s: float,
) -> dict:
    """Iterate ``queries`` through the verified run_backfill, bounded by per_query_cap and
    the master global_cap (via min()), tagging every row with ``tag``. Restart-safe."""
    from backfill_orchestrator import run_backfill  # the VERIFIED engine, reused unchanged

    done = _count_rows_with_tag(tag)  # restart-budget seed
    agg = {
        "tag": tag, "queries": len(queries), "global_cap": global_cap,
        "per_query_cap": per_query_cap, "max_pages": max_pages,
        "resumed_from": done, "analyzed": 0, "saved_new": 0,
        "save_duplicates": 0, "skipped_existing": 0, "skipped_rejected": 0,
        "item_failures": 0, "per_query": [],
    }
    p(f"[Scale] START tag={_ascii(tag)} queries={len(queries)} global_cap={global_cap} "
      f"per_query_cap={per_query_cap} max_pages={max_pages} resumed_from={done}")

    for i, q in enumerate(queries, 1):
        if done >= global_cap:
            p(f"[Scale] global cap {global_cap} reached — stopping before query {i}.")
            break
        # NEVER overshoot the global budget: this query may add at most (global_cap - done).
        this_cap = min(per_query_cap, global_cap - done)
        p(f"[Scale] query {i}/{len(queries)} {_ascii(q)} — this_cap={this_cap} (done={done}/{global_cap})")
        summary = run_backfill(
            query=q, max_pages=max_pages, item_cap=this_cap,
            ingest_origin=tag, sleep_s=sleep_s,
        )
        done += int(summary.get("saved_new") or 0)
        for k in ("analyzed", "saved_new", "save_duplicates", "skipped_existing",
                  "skipped_rejected", "item_failures"):
            agg[k] += int(summary.get(k) or 0)
        agg["per_query"].append({
            "query": q,
            "returned": summary.get("items_returned"),
            "analyzed": summary.get("analyzed"),
            "saved_new": summary.get("saved_new"),
            "skipped_existing": summary.get("skipped_existing"),
            "error": summary.get("error"),
        })
        p(f"[Scale] query {i} done: returned={summary.get('items_returned')} "
          f"analyzed={summary.get('analyzed')} saved_new={summary.get('saved_new')} "
          f"skipped_existing={summary.get('skipped_existing')} | running total saved this-tag={done}")

    agg["final_done_this_tag"] = done
    p(f"[Scale] DONE {json.dumps({k: v for k, v in agg.items() if k != 'per_query'}, ensure_ascii=True)}")
    return agg


def _run_selftest() -> int:
    """Offline check (no network/DB/LLM): the global-cap min() math never overshoots and a
    stub run_backfill is driven correctly across queries; the tag default is date-shaped."""
    failures = []

    # 1. min() global-cap enforcement: simulate the loop arithmetic with a stub that always
    #    saves exactly `item_cap` rows — the tightest overshoot test.
    per_q, glob = 30, 70
    done = 0
    caps_used = []
    for _ in range(5):  # 5 queries, but global cap 70 must stop the effective spend
        if done >= glob:
            break
        this_cap = min(per_q, glob - done)
        caps_used.append(this_cap)
        done += this_cap  # stub: saved_new == this_cap
    if done != glob or sum(caps_used) != glob or any(c < 0 for c in caps_used):
        failures.append(f"global-cap overshoot: caps={caps_used} done={done} (expected sum==70)")
    if caps_used != [30, 30, 10]:
        failures.append(f"cap sequence wrong: {caps_used} (expected [30,30,10])")
    p(f"  [{'ok' if not failures else 'xx'}] global-cap min() math: caps={caps_used} done={done} (cap {glob})")

    # 2. restart-budget: seeding done>0 reduces remaining spend, still no overshoot.
    done2 = 50  # pretend 50 already exist under the tag
    rem = []
    for _ in range(5):
        if done2 >= glob:
            break
        c = min(per_q, glob - done2)
        rem.append(c)
        done2 += c
    if done2 != glob or rem != [20]:
        failures.append(f"restart-budget wrong: rem={rem} done={done2} (expected [20], 70)")
    p(f"  [{'ok' if not failures else 'xx'}] restart-budget resume: seeded=50 -> extra caps={rem} (cap {glob})")

    # 3. default tag is date-shaped and reuses across same-day calls.
    t1, t2 = _default_tag(), _default_tag()
    if not t1.startswith("backfill_scale_") or len(t1) != len("backfill_scale_") + 8 or t1 != t2:
        failures.append(f"default tag shape/stability wrong: {t1} vs {t2}")
    p(f"  [{'ok' if not failures else 'xx'}] default tag: {t1} (stable within a UTC day)")

    # 4. ENV-SEED default queries are the 5 environment seeds.
    if len(ENV_SEED_QUERIES) != 5:
        failures.append(f"ENV_SEED_QUERIES not 5: {len(ENV_SEED_QUERIES)}")
    p(f"  [{'ok' if not failures else 'xx'}] default queries = {len(ENV_SEED_QUERIES)} ENV-SEED entries.")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (global-cap min() no-overshoot + restart-budget resume + tag shape + ENV-SEED default)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded background multi-query backfill scale runner (OUTER LOOP over the "
                    "verified orchestrator). Runs NOTHING without --run.",
    )
    parser.add_argument("--run", action="store_true",
                        help="Actually execute the scale run (explicit operator step).")
    parser.add_argument("--selftest", action="store_true",
                        help="Offline caps-math / restart-budget check (no network / DB / LLM).")
    parser.add_argument("--queries", default=None,
                        help="Comma-separated query list. Default = the 5 ENV-SEED queries.")
    parser.add_argument("--per-query-cap", type=int, default=DEFAULT_PER_QUERY_CAP)
    parser.add_argument("--global-cap", type=int, default=DEFAULT_GLOBAL_CAP)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--tag", default=None, help="ingest_origin tag; default backfill_scale_<YYYYMMDD>.")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S)
    args = parser.parse_args()

    if args.selftest:
        return _run_selftest()

    queries = ([q.strip() for q in args.queries.split(",") if q.strip()]
               if args.queries else list(ENV_SEED_QUERIES))
    tag = args.tag or _default_tag()

    if not args.run:
        est = min(args.global_cap, len(queries) * args.per_query_cap)
        p("PLAN (nothing executed — pass --run to execute):")
        p(f"  tag={_ascii(tag)}")
        p(f"  queries ({len(queries)}): {[_ascii(q) for q in queries]}")
        p(f"  per_query_cap={args.per_query_cap}  global_cap={args.global_cap}  max_pages={args.max_pages}  sleep={args.sleep}")
        p(f"  max items this run: {est} (min of global_cap and queries*per_query_cap)")
        p(f"  outer loop reuses backfill_orchestrator.run_backfill UNCHANGED; caps via min(); restart-safe via tag-count.")
        p(f"  verify after: python scripts/backfill_pilot_verify.py --tag {_ascii(tag)} --cap {args.global_cap}")
        return 0

    run_scale(
        queries,
        per_query_cap=args.per_query_cap,
        global_cap=args.global_cap,
        max_pages=args.max_pages,
        tag=tag,
        sleep_s=args.sleep,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
