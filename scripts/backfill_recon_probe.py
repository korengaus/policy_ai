"""BACKFILL-RECON Phase 1 — READ-ONLY feasibility probe.

Measures the three unknowns that size a historical news-embedding backfill,
WITHOUT spending on embeddings or running the verification pipeline:

  (1) CURRENT RATE  — analysis_results rows/day over the last 14 days
                      (SELECT-only) so we know what cron already accumulates.
  (2) NAVER DEPTH   — how far back Naver news search actually returns per
                      keyword (Naver SEARCH reads only; no embeddings, no
                      pipeline). The Naver news API has NO date-range param and
                      caps at ~1000 most-recent items per query (start<=1000,
                      display<=100), so real depth = f(keyword volume).
  (3) COST TABLE    — static math from token estimate x published price; no
                      API call.

Run from repo root, in the Worker Shell that has DB + Naver creds:

    PYTHONPATH=. python scripts/backfill_recon_probe.py

SAFETY / SCOPE
--------------
- READ-ONLY. DB access is SELECT-only (sa.select); there is NO insert/update/
  delete/DDL. Naver access is the search GET endpoint only (metadata; no
  embedding, no body crawl, no pipeline). NO embeddings API call anywhere.
  NO production flag is flipped — the probe only READS config/creds already set.
- pin-OUT: lives under scripts/, adds zero log.* sites to pinned modules.
- The Naver depth probe issues a handful of search GETs (a few pages for 2
  sample keywords). Naver search is free/quota-cheap and returns metadata only.
  If NAVER_SEARCH_ENABLED is false or creds are missing, that section reports
  the reason and skips — it never crashes and never writes.

WHAT IT DOES NOT DO
-------------------
- Does NOT call get_embedding / embeddings.create (no OpenAI spend).
- Does NOT run analyze_pipeline / claim extraction / judge / official-doc.
- Does NOT write any row (no analysis_results, no embedding_cache/vectors).
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa


# ---------------------------------------------------------------------------
# (1) CURRENT RATE — SELECT-only over analysis_results.created_at
# ---------------------------------------------------------------------------

def _parse_dt(value):
    """Best-effort parse of a stored created_at into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # Normalize a trailing Z and try ISO first, then a couple of fallbacks.
    candidate = text.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(candidate) if fmt is None else datetime.strptime(text[:len(fmt) + 4], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def report_current_rate():
    print("=" * 72)
    print("(1) CURRENT RATE — analysis_results rows/day, last 14 days")
    print("=" * 72)
    try:
        from postgres_storage import get_engine, analysis_results_table
    except Exception as exc:
        print(f"  SKIP: cannot import postgres_storage ({type(exc).__name__}: {exc})")
        return
    engine = get_engine()
    if engine is None:
        print("  SKIP: get_engine() is None — no DB configured.")
        return

    t = analysis_results_table
    with engine.connect() as conn:  # SELECT-only
        rows = conn.execute(sa.select(t.c.id, t.c.created_at)).all()

    total = len(rows)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=14)
    per_day = Counter()
    parsed_ok = 0
    for _id, created_at in rows:
        dt = _parse_dt(created_at)
        if dt is None:
            continue
        parsed_ok += 1
        if dt >= cutoff:
            per_day[dt.date().isoformat()] += 1

    last14_total = sum(per_day.values())
    print(f"  Total rows in analysis_results: {total} (created_at parsed: {parsed_ok})")
    print(f"  Rows in last 14 days: {last14_total}  (~{last14_total / 14.0:.1f}/day)")
    print("  Per-day breakdown (last 14 days):")
    for day in sorted(per_day):
        print(f"    {day}  {per_day[day]:>4}")
    if not per_day:
        print("    (no rows with a parseable created_at in the window)")
    print()


# ---------------------------------------------------------------------------
# (2) NAVER DEPTH — Naver SEARCH reads only (no embeddings, no pipeline)
# ---------------------------------------------------------------------------

# One high-volume and one niche policy keyword from the existing seed lists
# (scheduler.DEFAULT_QUERIES / config._DEFAULT_HOT_TOPIC_SEEDS). High-volume
# keywords exhaust the 1000-item cap in days; niche keywords reach months back.
SAMPLE_KEYWORDS = ["최저임금", "장애인 지원"]
# Pages to sample (sort=date => newest-first). start+display<=1000 is the API
# ceiling; start=901/display=100 reads the OLDEST reachable items (901-1000).
SAMPLE_STARTS = [1, 251, 501, 751, 901]


def report_naver_depth():
    print("=" * 72)
    print("(2) NAVER DEPTH — months-back & items/keyword (search reads only)")
    print("=" * 72)
    print("  NOTE: Naver news API has NO from/to date filter; sort=date pages")
    print("  newest-first and the API caps at start<=1000, display<=100, so the")
    print("  deepest reachable item per keyword is rank ~1000.")
    try:
        from providers.naver_search import NaverNewsSearchProvider
    except Exception as exc:
        print(f"  SKIP: cannot import NaverNewsSearchProvider ({type(exc).__name__})")
        return

    provider = NaverNewsSearchProvider()
    if not getattr(provider, "available", False):
        print(f"  SKIP: Naver provider unavailable — {getattr(provider, 'reason', 'unknown')}")
        print("  (set NAVER_SEARCH_ENABLED=true + NAVER_CLIENT_ID/SECRET to probe depth)")
        return

    for keyword in SAMPLE_KEYWORDS:
        print(f"\n  keyword: {keyword!r}  (sort=date)")
        oldest = None
        newest = None
        any_items = False
        for start in SAMPLE_STARTS:
            # SearchProviderResult is a TypedDict (plain dict at runtime) -> .get.
            result = provider.search(keyword, limit=100, start=start, sort="date")
            items = (result.get("items") if isinstance(result, dict) else None) or []
            dates = []
            for it in items:
                iso = (it.get("published_at") or "") if isinstance(it, dict) else ""
                if iso:
                    dates.append(iso)
            count = len(items)
            if count:
                any_items = True
            page_oldest = min(dates) if dates else "-"
            page_newest = max(dates) if dates else "-"
            if dates:
                lo, hi = min(dates), max(dates)
                oldest = lo if oldest is None else min(oldest, lo)
                newest = hi if newest is None else max(newest, hi)
            print(f"    start={start:>4} items={count:>3} "
                  f"page_newest={page_newest:<27} page_oldest={page_oldest}")
        if any_items and oldest and newest:
            print(f"    -> reachable span: {oldest}  ..  {newest}")
        elif not any_items:
            print("    -> no items returned (keyword may be too sparse, or API empty)")
    print()


# ---------------------------------------------------------------------------
# (3) COST TABLE — static math; NO API call.
# ---------------------------------------------------------------------------

# text-embedding-3-small published price (Jan 2026): ~$0.02 per 1M tokens.
PRICE_PER_1M_TOKENS = 0.02
# Standalone backfill embeds news (title + Naver description). Korean tokenizes
# at ~1 token / 1.2-1.7 chars under cl100k_base; a title+description is usually
# 120-260 chars. We show a conservative band: 250 (typical) and 500 (long).
TOKENS_PER_ITEM_BANDS = [250, 500]
ITEM_COUNTS = [1_000, 5_000, 10_000, 20_000]


def report_cost_table():
    print("=" * 72)
    print("(3) COST TABLE — text-embedding-3-small @ $%.2f / 1M tokens" % PRICE_PER_1M_TOKENS)
    print("=" * 72)
    print("  (standalone embed of title+description; NO pipeline tokens)")
    for tpi in TOKENS_PER_ITEM_BANDS:
        print(f"\n  Assuming ~{tpi} tokens/item:")
        print(f"    {'items':>8}  {'tokens':>12}  {'cost':>9}")
        for n in ITEM_COUNTS:
            tokens = n * tpi
            cost = tokens / 1_000_000 * PRICE_PER_1M_TOKENS
            print(f"    {n:>8}  {tokens:>12}  ${cost:>8.4f}")
    print()


# ---------------------------------------------------------------------------
# (extra) EMBEDDING PATH — confirm standalone-possible from config snapshot.
# ---------------------------------------------------------------------------

def report_embedding_path():
    print("=" * 72)
    print("(extra) EMBEDDING PATH — standalone surface check (no calls)")
    print("=" * 72)
    try:
        import config
        snap = config.describe_semantic_config()
        print("  semantic config snapshot:")
        for k, v in snap.items():
            print(f"    {k}: {v}")
    except Exception as exc:
        print(f"  (config snapshot unavailable: {type(exc).__name__})")
    print("  Standalone embed surface: semantic_embeddings.get_active_provider()")
    print("    .get_embedding(text) / .get_embeddings([texts]) — raw text in,")
    print("    vector out; NO claim-extraction/judge/official-doc coupling.")
    print("  Vector stores: embedding_cache (JSON, always) +/- embedding_vectors")
    print("    (pgvector Vector(1536), gated on PGVECTOR_ENABLED).")
    print()


def main():
    print("BACKFILL-RECON Phase 1 — READ-ONLY (no embeddings, no writes, no spend)\n")
    report_embedding_path()
    report_current_rate()
    report_naver_depth()
    report_cost_table()
    print("Done. (read-only; no rows written, no embeddings called)")


if __name__ == "__main__":
    main()
