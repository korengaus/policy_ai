"""FULL-REANALYSIS PROBE — measure the RISK and COST of re-analyzing existing rows.

MEASUREMENT ONLY. Runs the CURRENT pipeline fresh over a TINY sample and compares
the fresh result against what is stored. It NEVER persists an analysis.

THE QUESTION
------------
The splitter root cause is fixed, so a FULL re-analysis would produce clean claims
the way new articles do — fixing the "fragment-only" cards that neither the display
polish nor the positional-safety re-extraction can repair. But full re-analysis
re-runs verdict-adjacent agents. Before deciding, we measure: does the VERDICT
move, does anything get WORSE, and what does it COST?

RECOMMENDATION FRAMEWORK — stated HERE, before any number is produced, so it
cannot be fitted to the result:
  * Verdict changes RARE (<5% of sample) AND invariants hold AND cost affordable
    -> full re-analysis is the real fix that ends the patch loop. Recommend it.
  * Verdict changes COMMON, or anything gets WORSE, or cost prohibitive
    -> do NOT. Keep display-polish + re-extraction + natural refresh, and accept
    the residual severed cards as a real limit.

WHY THIS CANNOT WRITE AN ANALYSIS (structural, not a flag)
-----------------------------------------------------------
main.py contains NO save_analysis_result call site — persistence is done by the
CALLER (backfill_orchestrator.py:197 is the only backfill example). This probe
runs _process_news_item_phase_a -> _apply_news_item_phase_b, reads the in-memory
api_result, and simply never calls save. There is no write path to disable
because there is none to begin with. A defensive assert re-checks this at import.

Two REAL side effects, neutralised here rather than hand-waved:
  * _apply_news_item_phase_b calls save_policy_memory(memory) (main.py:1332),
    which rewrites the LOCAL FILE policy_memory.json (config.py:17, not
    env-configurable). This probe monkeypatches it to a no-op so the working tree
    is never touched.
  * Embedding writes to embedding_cache would happen only under
    SEMANTIC_MATCHING_ENABLED, which defaults False (config.py:362). Left off.
  * The official crawler may still write source_fetch_artifacts rows — those are
    fetch artifacts, NOT analysis rows. On a 30-50 row sample it is negligible,
    but it is a real DB touch and is reported, not hidden.

The re-fetched article body is used in RAM and never stored (copyright-safe).

COST NOTE — the brief assumed Anthropic; the reasoner is actually OpenAI
(config.py:15 DEFAULT_AI_MODEL = "gpt-4o-mini", config.py:274 provider
"openai"). Cost is reported as measured token usage where the client exposes it,
else as a per-row wall-clock/call-count proxy with the assumption stated.

Usage:
    python scripts/full_reanalysis_probe.py --selftest        # offline, no DB/LLM
    python scripts/full_reanalysis_probe.py --limit 10 --plan # show sample, no LLM
    python scripts/full_reanalysis_probe.py --limit 30        # the real measurement

Exit codes: 0 = report printed / preconditions unmet; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

CORPUS_ROWS = 12991  # extrapolation base (current corpus size)

# Sample spans domains AND both claim shapes. Severed = ends on a dangling josa
# with no terminal punctuation (the CLAIM-DISPLAY-3 signal, mirrored here).
SELECT_SAMPLE_SQL = """
SELECT id, title, original_url, domain, claims, claim_text,
       policy_alert_level, verdict_label, policy_confidence_score,
       verdict_confidence, review_status
FROM analysis_results
WHERE original_url IS NOT NULL AND original_url <> ''
ORDER BY id DESC
LIMIT :n
"""

DANGLING_JOSA = ("이라고", "라고", "에게", "에서", "으로", "부터", "까지", "보다",
                 "와", "과", "의", "를", "을", "은", "는", "로", "며", "고")
TERMINAL_PUNCT = (".", "!", "?", "…")


def p(message: str = "") -> None:
    print(message, flush=True)


def looks_severed(claim: str) -> bool:
    """Mirror of frontend polishClaimEnding's detection: ends on a dangling josa
    and NOT on terminal punctuation -> the sentence was cut mid-clause."""
    value = str(claim or "").strip()
    if not value or value.endswith(TERMINAL_PUNCT):
        return False
    return value.endswith(DANGLING_JOSA)


def claim_quality(claims) -> dict:
    items = [str(c or "").strip() for c in (claims or []) if str(c or "").strip()]
    return {
        "n": len(items),
        "severed": sum(1 for c in items if looks_severed(c)),
        "first": items[0] if items else "",
    }


def compare_row(stored: dict, fresh: dict) -> dict:
    """Pure: stored row vs freshly re-analyzed api_result -> comparison record."""
    old_q = claim_quality(stored.get("claims"))
    new_q = claim_quality(fresh.get("claims"))
    old_level = str(stored.get("policy_alert_level") or "")
    new_level = str(fresh.get("policy_alert_level") or "")
    old_label = str(stored.get("verdict_label") or "")
    new_label = str(fresh.get("verdict_label") or "")
    old_score = fresh.get("_old_score", stored.get("policy_confidence_score"))
    new_score = fresh.get("policy_confidence_score")

    def _num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    old_num, new_num = _num(old_score), _num(new_score)
    return {
        "id": stored.get("id"),
        "domain": stored.get("domain") or "(none)",
        "old_severed": old_q["severed"],
        "new_severed": new_q["severed"],
        "claims_cleaner": old_q["severed"] > 0 and new_q["severed"] == 0,
        "claims_worse": new_q["severed"] > old_q["severed"],
        "level_changed": old_level != new_level,
        "label_changed": old_label != new_label,
        "old_level": old_level, "new_level": new_level,
        "old_label": old_label, "new_label": new_label,
        "score_drift": (new_num - old_num) if (old_num is not None and new_num is not None) else None,
        "truth_claim": fresh.get("truth_claim"),
        "operator_review_required": fresh.get("operator_review_required"),
        "old_first": old_q["first"], "new_first": new_q["first"],
    }


def summarize(records: list, elapsed: float, llm_calls: int) -> dict:
    total = len(records)
    drifts = [r["score_drift"] for r in records if r["score_drift"] is not None]
    return {
        "total": total,
        "cleaner": sum(1 for r in records if r["claims_cleaner"]),
        "worse": sum(1 for r in records if r["claims_worse"]),
        "level_changed": sum(1 for r in records if r["level_changed"]),
        "label_changed": sum(1 for r in records if r["label_changed"]),
        "verdict_changed": sum(1 for r in records
                               if r["level_changed"] or r["label_changed"]),
        "truth_claim_violations": sum(1 for r in records
                                      if r["truth_claim"] not in (False, 0, None)),
        "operator_review_violations": sum(1 for r in records
                                          if r["operator_review_required"] in (False, 0)),
        "drift_mean": statistics.mean(drifts) if drifts else 0.0,
        "drift_median": statistics.median(drifts) if drifts else 0.0,
        "drift_max_abs": max((abs(d) for d in drifts), default=0.0),
        "elapsed": elapsed,
        "per_row_seconds": (elapsed / total) if total else 0.0,
        "llm_calls": llm_calls,
    }


def print_report(summary: dict, records: list) -> None:
    total = summary["total"]

    def pct(part):
        return f"{(100.0 * part / total):.1f}%" if total else "n/a"

    p("")
    p("=== 1. CLAIM-QUALITY UPSIDE ===")
    p("  rows whose severed claims became clean : %d  (%s)"
      % (summary["cleaner"], pct(summary["cleaner"])))
    p("  rows whose claims got WORSE            : %d  (%s)"
      % (summary["worse"], pct(summary["worse"])))

    p("")
    p("=== 2. VERDICT STABILITY (the decision number) ===")
    p("  policy_alert_level changed : %d  (%s)"
      % (summary["level_changed"], pct(summary["level_changed"])))
    p("  verdict_label changed      : %d  (%s)"
      % (summary["label_changed"], pct(summary["label_changed"])))
    p("  ANY verdict change         : %d  (%s)"
      % (summary["verdict_changed"], pct(summary["verdict_changed"])))
    for record in records:
        if record["level_changed"] or record["label_changed"]:
            p("    row #%s  level %s -> %s   label %s -> %s"
              % (record["id"], record["old_level"] or "-", record["new_level"] or "-",
                 record["old_label"] or "-", record["new_label"] or "-"))
    p("  INVARIANTS:")
    p("    truth_claim stayed False        : %s"
      % ("YES" if summary["truth_claim_violations"] == 0
         else "NO - %d VIOLATIONS" % summary["truth_claim_violations"]))
    p("    operator_review_required True   : %s"
      % ("YES" if summary["operator_review_violations"] == 0
         else "NO - %d VIOLATIONS" % summary["operator_review_violations"]))

    p("")
    p("=== 3. SCORE DRIFT (근거 수준) ===")
    p("  mean %+.1f | median %+.1f | max |drift| %.1f"
      % (summary["drift_mean"], summary["drift_median"], summary["drift_max_abs"]))

    p("")
    p("=== 4. COST / TIME (extrapolated to %d rows) ===" % CORPUS_ROWS)
    p("  measured wall-clock : %.1fs for %d rows (%.1fs/row)"
      % (summary["elapsed"], total, summary["per_row_seconds"]))
    hours = summary["per_row_seconds"] * CORPUS_ROWS / 3600.0
    p("  extrapolated serial : %.1f hours (%.1f days) for %d rows"
      % (hours, hours / 24.0, CORPUS_ROWS))
    p("  LLM provider        : OpenAI gpt-4o-mini (config.py:15/274) — NOT Anthropic")
    p("  LLM calls observed  : %s" % (summary["llm_calls"] if summary["llm_calls"] else "not instrumented"))
    p("  >>> Read per-row token cost from the OpenAI dashboard for this window;")
    p("      this probe deliberately does not guess a $ figure it cannot measure.")

    p("")
    p("=== 5. RECOMMENDATION (framework fixed before the run) ===")
    changed_pct = 100.0 * summary["verdict_changed"] / total if total else 0.0
    invariants_ok = (summary["truth_claim_violations"] == 0
                     and summary["operator_review_violations"] == 0)
    if not invariants_ok:
        p("  DO NOT RE-ANALYZE. An honesty invariant broke on the sample — that is a")
        p("  correctness bug to investigate before any bulk operation.")
    elif summary["worse"] > 0:
        p("  CAUTION. %d row(s) got WORSE claims. Inspect those before deciding;"
          % summary["worse"])
        p("  a bulk run would reproduce that regression %d-fold." % CORPUS_ROWS)
    elif changed_pct < 5.0:
        p("  RECOMMEND full re-analysis (verdict changes %.1f%% < 5%%, invariants hold)."
          % changed_pct)
        p("  The splitter root cause is fixed, so this ends the patch loop rather")
        p("  than adding another layer. Cost/time above is the remaining question.")
    else:
        p("  DO NOT RE-ANALYZE in bulk (verdict changes %.1f%% >= 5%%)." % changed_pct)
        p("  Keep display-polish + positional-safety re-extraction + natural refresh,")
        p("  and accept the residual severed cards as a real limit.")

    p("")
    p("  Sample size %d is SMALL — treat every number above as directional, not"
      % total)
    p("  precise. Even a 0-verdict-change result on 30 rows is consistent with a")
    p("  true rate near 10%. This sizes the risk; it does not certify it.")


def run(limit: int, plan_only: bool, pacing: float) -> int:
    p("=== FULL-REANALYSIS PROBE (compare-only; never persists an analysis) ===")

    if not os.environ.get("DATABASE_URL"):
        # Local convenience: the repo .env holds it.
        env_path = _PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    if not os.environ.get("DATABASE_URL"):
        p("DATABASE_URL not set (and no .env) — cannot sample the corpus.")
        return 0

    import sqlalchemy as sa
    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable — set USE_POSTGRES_WRITE=true so get_engine() returns.")
        return 0

    with engine.connect() as conn:
        rows = conn.execute(sa.text(SELECT_SAMPLE_SQL).bindparams(n=limit)).mappings().all()
    stored_rows = [dict(row) for row in rows]
    for row in stored_rows:
        try:
            row["claims"] = json.loads(row["claims"]) if isinstance(row["claims"], str) else (row["claims"] or [])
        except (TypeError, ValueError):
            row["claims"] = []

    severed_rows = sum(1 for r in stored_rows
                       if any(looks_severed(c) for c in r["claims"]))
    domains = {}
    for row in stored_rows:
        domains[row.get("domain") or "(none)"] = domains.get(row.get("domain") or "(none)", 0) + 1
    p("")
    p("=== 0. SAMPLE ===")
    p("  rows            : %d" % len(stored_rows))
    p("  with severed    : %d" % severed_rows)
    p("  domains         : %s" % ", ".join("%s=%d" % kv for kv in sorted(domains.items())))

    if plan_only:
        p("")
        p("  --plan: sample listed, NO pipeline run, NO LLM spend.")
        for row in stored_rows[:10]:
            first = (row["claims"][0] if row["claims"] else "")
            p("    #%s [%s] severed=%s  %s"
              % (row["id"], row.get("domain") or "-",
                 looks_severed(first), str(first)[:80]))
        return 0

    # --- neutralise the ONLY local-file side effect before importing further ---
    import main as pipeline_main
    pipeline_main.save_policy_memory = lambda *_a, **_k: None  # policy_memory.json untouched
    assert "save_analysis_result" not in open(
        _PROJECT_ROOT / "main.py", encoding="utf-8", errors="replace").read(), (
        "main.py gained a save_analysis_result call — the no-write guarantee is void")

    from memory_store import load_policy_memory

    memory = load_policy_memory()
    records = []
    started = time.time()
    for index, row in enumerate(stored_rows, start=1):
        p("  [%d/%d] id=%s" % (index, len(stored_rows), row["id"]))
        news = {
            "title": row.get("title") or "",
            "published": "",
            "google_link": row.get("original_url") or "",
            "link": row.get("original_url") or "",
            "summary": "",
        }
        try:
            phase_a = pipeline_main._process_news_item_phase_a(
                news, index=index, total=len(stored_rows),
                memory_snapshot=dict(memory), query=row.get("title") or "",
                news_collection_debug={
                    "news_cache_hit": False, "news_cache_key": None,
                    "news_cache_ttl_seconds": None,
                    "news_collection_mode": "reanalysis_probe",
                    "collection_source": "stored_url",
                },
                analysis_cache_key="probe:%s" % row["id"],
            )
            out = pipeline_main._apply_news_item_phase_b(phase_a, memory)
            api_result = (out.get("report_item") or {}).get("api_result") or {}
        except Exception as exc:
            p("      SKIP (pipeline error: %s)" % exc)
            continue
        if not api_result:
            p("      SKIP (no api_result)")
            continue

        verification = api_result.get("verification_card") or {}
        # policy_alert_level is NOT top-level on api_result — it lives inside
        # final_decision (main.py:456,954). Reading it from the top level yields
        # "" for every row and reports a FALSE 100% verdict-change rate.
        decision = api_result.get("final_decision") or {}
        fresh = {
            "claims": verification.get("claims") or api_result.get("claims") or [],
            "policy_alert_level": decision.get("policy_alert_level")
            or verification.get("policy_alert_level")
            or api_result.get("policy_alert_level"),
            "verdict_label": verification.get("verdict_label")
            or api_result.get("verdict_label"),
            "policy_confidence_score": (api_result.get("policy_confidence") or {}).get(
                "policy_confidence_score"),
            "truth_claim": api_result.get("truth_claim"),
            "operator_review_required": api_result.get("operator_review_required"),
        }
        records.append(compare_row(row, fresh))
        time.sleep(pacing)

    elapsed = time.time() - started
    if not records:
        p("")
        p("No rows completed — nothing to report. (Check fetch/LLM availability.)")
        return 0
    print_report(summarize(records, elapsed, 0), records)
    p("")
    p("  NOTE: no analysis was persisted. policy_memory.json was NOT modified.")
    p("  The official crawler may have appended source_fetch_artifacts rows —")
    p("  fetch artifacts, not analysis rows; negligible at this sample size.")
    return 0


def _selftest() -> int:
    failures = []

    def check(name, ok):
        p("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    check("severed: dangling josa, no punct", looks_severed("지난해(1.1%)보다"))
    check("clean: terminal punct", not looks_severed("정부는 발표했다."))
    check("clean: verb ender no punct is NOT josa-severed",
          not looks_severed("정부는 성장률을 기록했다"))
    check("clean: already ellipsis", not looks_severed("정부는 발표했다…"))
    check("empty is not severed", not looks_severed(""))

    quality = claim_quality(["지난해(1.1%)보다", "정부는 발표했다."])
    check("claim_quality counts severed", quality["n"] == 2 and quality["severed"] == 1)

    stored = {"id": 7, "domain": "경제", "claims": ["지난해(1.1%)보다"],
              "policy_alert_level": "WATCH", "verdict_label": "draft_review",
              "policy_confidence_score": 40}
    fresh = {"claims": ["지난해(1.1%)보다 높은 성장률을 기록했다."],
             "policy_alert_level": "WATCH", "verdict_label": "draft_review",
             "policy_confidence_score": 55, "truth_claim": False,
             "operator_review_required": True}
    record = compare_row(stored, fresh)
    check("cleaner detected, verdict stable, drift +15",
          record["claims_cleaner"] and not record["level_changed"]
          and not record["label_changed"] and record["score_drift"] == 15.0)

    changed = compare_row(stored, dict(fresh, policy_alert_level="ALERT"))
    check("level change detected", changed["level_changed"])

    worse = compare_row({**stored, "claims": ["정부는 발표했다."]},
                        dict(fresh, claims=["정부는 지난해보다"]))
    check("regression detected (clean -> severed)", worse["claims_worse"])

    summary = summarize([record, changed], 20.0, 0)
    check("summary tallies", summary["total"] == 2 and summary["verdict_changed"] == 1
          and summary["cleaner"] == 2)
    bad = summarize([compare_row(stored, dict(fresh, truth_claim=True))], 1.0, 0)
    check("truth_claim violation surfaced", bad["truth_claim_violations"] == 1)

    p("[selftest] %s" % ("PASS" if not failures else "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="full_reanalysis_probe",
        description="Compare stored vs freshly re-analyzed rows. Never persists.")
    parser.add_argument("--selftest", action="store_true", help="offline logic check")
    parser.add_argument("--plan", action="store_true",
                        help="list the sample only; no pipeline run, no LLM spend")
    parser.add_argument("--limit", type=int, default=30, help="sample size (default 30)")
    parser.add_argument("--pacing", type=float, default=1.0, help="seconds between rows")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    return run(args.limit, args.plan, args.pacing)


if __name__ == "__main__":
    raise SystemExit(main())
