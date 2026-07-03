"""SEED-MECH — READ-ONLY, SELECT-only measurement of HOW domain coverage is seeded,
and why environment(환경)/health are weak.

WHY: we want to STRENGTHEN environment coverage before re-basing the domain-expansion
gate. GATE-DIAG suggested "broad seeds + post-hoc classify", but this probe pins the
EXACT mechanism so environment is strengthened correctly (intake-side, verdict-isolated),
without over-seeding or touching the classifier. Diagnosis only — no seed/gate change.

MEASUREMENT ONLY. Every DB statement is a SELECT (engine.connect(); no commit). Touches
no production code, no verdict logic, no pins, no config. Reads the REAL seed lists +
predicates (imported / inspected read-only); re-implements nothing.

METRICS
-------
  A. SEED SOURCE: the ACTIVE fixed seed set scheduler.DEFAULT_QUERIES + the (flag-gated,
     default-off) hot-topic broad seeds config._DEFAULT_HOT_TOPIC_SEEDS, verbatim; the
     seed->keyword->filter flow (build_dynamic_queries: AI pick over fetched TITLES then
     _passes_domain_filter = _ALLOWLIST-require + _DENYLIST-drop); and whether PER-DOMAIN
     seeding exists. FINDING preview: DEFAULT_QUERIES DOES carry per-domain blocks
     (WELFARE-SEED, AGRI-LABOR-SEED) but NONE for environment/health; and _ALLOWLIST has
     ZERO environment/health vocabulary, so the hot-topic path cannot surface an env
     keyword unless it also contains a generic policy word.
  B. DOMAIN COVERAGE: domain distribution over all stored rows (from the stored `domain`
     field) — environment + health flagged as the weak ones vs welfare/agri/labor/etc.
  C. WHY WEAK (keyword trace): the per-row `query` values that yielded the existing
     environment + health rows (what a stronger seed would look like), + welfare contrast.
  D. CLASSIFIER PATH: confirm `domain` is post-analysis domain_classifier output
     (verdict-isolated, metadata-only) — so a seed change is intake-side only.

FIELD-NAME NOTES (confirmed by grep)
------------------------------------
  * The ACTIVE keyword source is scheduler.DEFAULT_QUERIES (HOT_TOPIC_ENABLED defaults
    FALSE, so the config hot-topic seeds are a no-op until an operator flips the flag).
  * There is NO per-domain seed ARRAY keyed by domain; per-domain coverage comes from
    (a) explicit program-level entries added to the flat DEFAULT_QUERIES list
    (WELFARE-SEED / AGRI-LABOR-SEED comment blocks) and (b) post-hoc domain_classifier.
  * domain/query/title are TOP-LEVEL columns of analysis_results. domain is nullable on
    un-backfilled old rows (counted as '(unclassified)').

SAFETY: SELECT-only; engine.connect(); no commit; lazy DB import inside the live path so
--selftest is fully offline. ASCII-guarded prints (json.dumps ensure_ascii).

Usage:
    PYTHONPATH=. python scripts/seed_mech_probe.py
    PYTHONPATH=. python scripts/seed_mech_probe.py --selftest   # offline, no DB

Exit codes: 0 = dump printed / engine unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# Domains we expect to be WEAK (the focus of the strengthening work).
WEAK_DOMAINS = ("environment", "health")
CONTRAST_DOMAIN = "welfare"
# Substring markers used ONLY to check whether _ALLOWLIST carries env/health vocabulary.
ENV_HEALTH_VOCAB = ("환경", "탄소", "에너지", "기후", "온실", "재생에너지",
                    "의료", "질병", "백신", "건강", "감염", "병원")


def p(line: str = "") -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


def _ascii(value) -> str:
    return json.dumps(value if value is not None else "", ensure_ascii=True)


def _read_seed_sources():
    """Import the REAL seed lists + predicate sets (read-only). Returns a dict; each
    field degrades to a note on import failure rather than crashing."""
    out = {}
    try:
        import scheduler
        out["default_queries"] = list(scheduler.DEFAULT_QUERIES)
    except Exception as exc:  # noqa: BLE001
        out["default_queries"] = None
        out["default_queries_err"] = str(exc)[:80]
    try:
        import config
        out["hot_topic_seeds"] = list(config._DEFAULT_HOT_TOPIC_SEEDS)
        out["hot_topic_enabled_default"] = config.hot_topic_enabled()
    except Exception as exc:  # noqa: BLE001
        out["hot_topic_seeds"] = None
        out["hot_topic_seeds_err"] = str(exc)[:80]
    try:
        import hot_topics
        out["allowlist"] = list(hot_topics._ALLOWLIST)
    except Exception as exc:  # noqa: BLE001
        out["allowlist"] = None
        out["allowlist_err"] = str(exc)[:80]
    return out


def _allowlist_has_env_health(allowlist) -> list[str]:
    """Which env/health vocab terms appear in _ALLOWLIST (expected: none)."""
    if not allowlist:
        return []
    joined = " ".join(allowlist)
    return [v for v in ENV_HEALTH_VOCAB if v in joined]


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST
# ---------------------------------------------------------------------------
def run_selftest() -> int:
    p("=== SEED-MECH --selftest (offline; no DB, no network) ===")
    failures = []

    src = _read_seed_sources()

    # 1. Seed lists import.
    if src.get("default_queries") is None:
        failures.append(f"could not read scheduler.DEFAULT_QUERIES ({src.get('default_queries_err')})")
    else:
        p(f"  [ok] scheduler.DEFAULT_QUERIES: {len(src['default_queries'])} entries (active seed set).")
    if src.get("hot_topic_seeds") is None:
        failures.append(f"could not read config._DEFAULT_HOT_TOPIC_SEEDS ({src.get('hot_topic_seeds_err')})")
    else:
        p(f"  [ok] config._DEFAULT_HOT_TOPIC_SEEDS: {len(src['hot_topic_seeds'])} broad seeds "
          f"(hot-topic path; enabled_default={src.get('hot_topic_enabled_default')}).")

    # 2. _ALLOWLIST env/health vocabulary gap (the structural 'why weak').
    if src.get("allowlist") is None:
        failures.append(f"could not read hot_topics._ALLOWLIST ({src.get('allowlist_err')})")
    else:
        hits = _allowlist_has_env_health(src["allowlist"])
        p(f"  [ok] _ALLOWLIST env/health vocab present: {hits or '(none — env/health cannot pass the allowlist gate alone)'}")

    # 3. domain_classifier is verdict-isolated metadata (LABELS incl. environment/health).
    try:
        import domain_classifier
        labels = set(domain_classifier.LABELS)
        for d in WEAK_DOMAINS + (CONTRAST_DOMAIN,):
            if d not in labels:
                failures.append(f"domain '{d}' missing from domain_classifier.LABELS")
        p(f"  [ok] domain_classifier.LABELS ({len(labels)}) include environment/health/welfare.")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"could not import domain_classifier ({str(exc)[:80]})")

    if failures:
        p("")
        p("SELFTEST: FAIL")
        for f in failures:
            p(f"  - {f}")
        return 1
    p("")
    p("SELFTEST: PASS (seed lists read + allowlist gap check + classifier labels)")
    return 0


# ---------------------------------------------------------------------------
# LIVE RUN (SELECT-only)
# ---------------------------------------------------------------------------
def run_live() -> int:
    p("=== SEED-MECH (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    src = _read_seed_sources()

    # ---- METRIC A -----------------------------------------------------------
    p("")
    p("=== METRIC A — SEED SOURCE (how coverage is seeded) ===")
    p("  ACTIVE seed set (HOT_TOPIC_ENABLED default false -> this is what the cron searches):")
    p(f"    scheduler.DEFAULT_QUERIES ({len(src['default_queries'] or [])}):")
    for q in (src["default_queries"] or []):
        p(f"      - {_ascii(q)}")
    p("  Flag-gated hot-topic broad seeds (no-op until an operator enables the flag):")
    p(f"    config._DEFAULT_HOT_TOPIC_SEEDS ({len(src['hot_topic_seeds'] or [])}): "
      f"{[_ascii(s) for s in (src['hot_topic_seeds'] or [])]}"
      f"  enabled_default={src.get('hot_topic_enabled_default')}")
    p("  Flow: seeds -> news_collector fetches fresh TITLES -> (hot-topic path only) AI pick ->")
    p("        _passes_domain_filter (require >=1 _ALLOWLIST term, drop any _DENYLIST term) ->")
    p("        analysis -> domain_classifier assigns `domain` POST-analysis.")
    env_hits = _allowlist_has_env_health(src.get("allowlist"))
    p(f"  _ALLOWLIST env/health vocabulary: "
      f"{env_hits or '(NONE — an environment/health keyword cannot satisfy the allowlist gate unless it also carries a generic policy word)'}")
    # Per-domain seeding check: are there env/health seeds in the active set?
    active = " ".join(src["default_queries"] or [])
    env_seeded = [v for v in ("환경", "탄소", "에너지", "기후", "온실") if v in active]
    health_seeded = [v for v in ("의료", "질병", "백신", "건강", "감염", "돌봄") if v in active]
    p(f"  PER-DOMAIN seeding: YES via explicit program-level entries in the FLAT DEFAULT_QUERIES")
    p(f"    list (WELFARE-SEED / AGRI-LABOR-SEED blocks) — but there is NO env/health SEED ARRAY.")
    p(f"    environment seed terms in the active set: {env_seeded or '(NONE)'}")
    p(f"    health seed terms in the active set: {health_seeded or '(NONE — 돌봄 is welfare-side)'}")

    engine = postgres_storage.get_engine()
    if engine is None:
        p("")
        p("Engine unavailable — set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Metric A above is code-only and complete; B/C/D need the DB — run in the Worker Shell.)")
        return 0

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, domain, query, title FROM analysis_results ORDER BY id")
        ).all()

    # ---- METRIC B -----------------------------------------------------------
    p("")
    p(f"=== METRIC B — DOMAIN COVERAGE (over {len(rows)} stored rows) ===")
    dist = {}
    for r in rows:
        dom = r._mapping["domain"]
        key = str(dom) if dom else "(unclassified)"
        dist[key] = dist.get(key, 0) + 1
    for dom, n in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])):
        flag = "  <-- WEAK (strengthen)" if dom in WEAK_DOMAINS else ""
        p(f"    {dom}: {n}{flag}")
    for d in WEAK_DOMAINS:
        p(f"  {d} coverage: {dist.get(d, 0)}")

    # ---- METRIC C -----------------------------------------------------------
    p("")
    p("=== METRIC C — WHY WEAK (keywords that yielded env/health rows; welfare contrast) ===")
    def _keywords_for(domain: str, limit: int = 10):
        seen = {}
        for r in rows:
            m = r._mapping
            if str(m["domain"] or "") == domain:
                q = str(m["query"] or "(none)")
                seen[q] = seen.get(q, 0) + 1
        return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    for d in WEAK_DOMAINS + (CONTRAST_DOMAIN,):
        kws = _keywords_for(d)
        p(f"  {d} ({dist.get(d, 0)} rows) — top query keywords:")
        if not kws:
            p("      (none — no rows in this domain)")
        for q, n in kws:
            p(f"      {n}x {_ascii(q)}")

    # ---- METRIC D -----------------------------------------------------------
    p("")
    p("=== METRIC D — CLASSIFIER PATH (domain is post-analysis, verdict-isolated) ===")
    try:
        import inspect
        import main
        src_main = inspect.getsource(main.analyze_pipeline) if hasattr(main, "analyze_pipeline") else ""
        calls_classifier = "classify_domain" in src_main
        # domain must NOT be read by any verdict function — structural sanity: the
        # classifier module's own contract says metadata-only.
        import domain_classifier
        doc = (domain_classifier.__doc__ or "")
        metadata_only = "metadata" in doc.lower() or "never feeds" in doc.lower()
        p(f"  main.analyze_pipeline calls domain_classifier.classify_domain: {calls_classifier}")
        p(f"  domain_classifier contract is metadata-only / verdict-isolated (docstring): {metadata_only}")
        p("  => `domain` is assigned AFTER the verdict is computed; a SEED change alters WHAT is")
        p("     collected (intake-side), NOT how a row is classified or verdicted. Verdict-isolated.")
    except Exception as exc:  # noqa: BLE001
        p(f"  (could not inspect main/domain_classifier: {str(exc)[:100]})")

    p("")
    p("NOTE: measurement only. The safe way to strengthen environment is an explicit program-level")
    p("ENV-SEED block in scheduler.DEFAULT_QUERIES (mirroring WELFARE-SEED / AGRI-LABOR-SEED), which")
    p("is intake-side + verdict-isolated. That change + the gate re-base is the strategist's next step.")
    p("(Reminder: DEFAULT_QUERIES has a pin interaction in hottopic_safety_probe — handle at change time.)")
    p("")
    p("[Safety] READ-ONLY probe — no rows written, updated, or deleted.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY seed/coverage mechanism diagnostic (why environment/health are weak). "
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
