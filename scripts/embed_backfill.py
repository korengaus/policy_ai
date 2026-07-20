# BRAINMAP 2a — embed-only backfill: title+claim_text into embedding_cache.
# Modeled on scripts/content_nature_backfill.py's STRUCTURE (env-guard → lazy
# imports → batched loop → summary → --selftest) but embed-ONLY: it never
# writes analysis_results, so the twin's guarded UPDATE / reversibility id-log
# have no equivalent here — there is nothing to reverse (embedding_cache rows
# are additive metadata keyed by (text_hash, provider, model)).
#
# WHY: the 2c clustering experiment found claim_text-only vectors produce a
# grab-bag cluster (generic boilerplate claims). Fix = embed
# f"{title}\n{claim_text}".strip() (title first — it carries the event
# identity). Title is non-empty on all corpus rows, so the concat hash differs
# from the claim-only hash on every row → all-new cache entries; the
# (text_hash, provider, model) unique constraint protects the existing
# claim-only vectors. Additive, zero overwrite.
#
# SAFETY:
#   * Writes ONLY embedding_cache, via the EXISTING save path
#     (semantic_similarity._embed_with_cache → database.save_cached_embedding).
#     NO analysis_results write of any kind — verdict_label /
#     policy_alert_level / truth_claim / operator_review_required / score /
#     has_genuine untouched. Verdict-isolated metadata.
#   * REUSES verbatim: semantic_embeddings.get_active_provider() (provider) and
#     semantic_similarity._embed_with_cache(text, provider, cache_enabled=True)
#     (hash → cache-hit skip → get_embedding → save_cached_embedding). The
#     cache key (hash_text_for_cache = sha256(text.strip()) + provider + model)
#     is never re-implemented here.
#   * IDEMPOTENT by construction: _embed_with_cache's cache-hit path skips
#     already-embedded texts, so re-runs cost $0 and re-embed nothing.
#   * Embed-only = OpenAI embeddings, NO Anthropic, NO judge, NO crawler, NO
#     analyze pipeline. LOW-MEMORY: SELECTs only id, title, claim_text.
#   * Fail-closed: real + dry-run both refuse when the embedding provider is
#     unavailable (missing SEMANTIC_MATCHING_ENABLED / EMBEDDING_PROVIDER /
#     EMBEDDING_MODEL / OPENAI_API_KEY) — no silent deterministic fallback,
#     so cache keys always carry the real provider+model.
#   * Never prints DATABASE_URL or any API key.
#
# Run in the Render Worker Shell (env already present: DATABASE_URL,
# USE_POSTGRES_WRITE=true, OPENAI_API_KEY, SEMANTIC_MATCHING_ENABLED=true,
# EMBEDDING_PROVIDER=openai, EMBEDDING_MODEL=text-embedding-3-small).
# Offline logic check: --selftest (mock provider + in-memory cache; no DB, no
# network). Cost preview: --dry-run (hash + cache-existence check only; no API
# call, no write).

import argparse
import os
import sys
import time
from pathlib import Path

# Make the project root importable when launched as
# `python scripts/embed_backfill.py` (cwd=project root) without PYTHONPATH=.
# Mirrors the proven pattern in content_nature_backfill.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tunables (top-of-file, commented).
# ---------------------------------------------------------------------------
# Default rows per SELECT page (progress print per page; no commit needed —
# this script issues no DB write itself; cache saves happen inside
# database.save_cached_embedding per embedding).
DEFAULT_BATCH = 50
# Gentle pacing between rows (matches the content_nature twin; not a
# rate-limit workaround).
PACING_SECONDS = 0.05
# Rough per-NEW-embedding cost for the running-estimate line only.
# title+claim averages ~148 chars ≈ ~120 tokens × $0.02/1M (text-embedding-3-
# small) ≈ $0.0000024/row. Display-only — NOT used for any decision.
ROUGH_COST_PER_ROW = 0.0000025

# Read-only page fetch. Id-cursor pagination (`id > %s`) because no column
# marks embed progress — idempotency lives in the cache-hit skip, not in the
# SELECT. LOW-MEMORY: only id, title, claim_text (never source_candidates or
# any heavy blob).
SELECT_SQL = (
    "SELECT id, title, claim_text FROM analysis_results "
    "WHERE id > %s ORDER BY id LIMIT %s"
)


def build_embed_text(title, claim_text):
    """The 2a embed target: f\"{title}\\n{claim_text}\".strip().

    Empty/None claim_text → title only (the \\n is stripped away). Empty
    title AND empty claim → "" (caller skips the row). Kept as a tiny pure
    function so the selftest can pin the concat form.
    """
    title = (title or "").strip()
    claim = (claim_text or "").strip()
    return f"{title}\n{claim}".strip() if claim else title


def run_backfill(conn, provider, batch, max_rows, dry_run,
                 embed_fn=None, cache_lookup=None, pacing=PACING_SECONDS,
                 start_id=0):
    """Drive the batched embed backfill against an already-open connection.

    `start_id` seeds the id cursor: only ids > start_id are scanned (default 0
    = the whole table, i.e. the pre-existing behavior). Lets the operator drain
    a backlog in slices, one process per slice, without re-walking the head.
    `provider` is an active EmbeddingProvider. `embed_fn` / `cache_lookup` are
    injected so the selftest can pass fakes; in main they default to the REAL
    semantic_similarity._embed_with_cache and database.get_cached_embedding
    (lazy imports — this module stays import-side-effect-free).
    Returns (embedded, cache_hits, would_embed, skipped_empty, failed).
    """
    if embed_fn is None:
        from semantic_similarity import _embed_with_cache as embed_fn
    if cache_lookup is None:
        from database import get_cached_embedding as cache_lookup
    from semantic_embeddings import hash_text_for_cache

    embedded = cache_hits = would_embed = skipped_empty = failed = 0
    total = 0
    page_no = 0
    last_id = start_id
    while True:
        if max_rows is not None and total >= max_rows:
            break
        limit = batch if max_rows is None else min(batch, max_rows - total)
        if limit <= 0:
            break
        with conn.cursor() as cur:
            cur.execute(SELECT_SQL, (last_id, limit))
            rows = cur.fetchall()
        if not rows:
            break
        page_no += 1
        for rid, title, claim_text in rows:
            last_id = max(last_id, rid)
            total += 1
            text = build_embed_text(title, claim_text)
            if not text:
                skipped_empty += 1
                continue
            if dry_run:
                # Hash + cache-existence check ONLY — no API call, no write.
                text_hash = hash_text_for_cache(text)
                if cache_lookup(text_hash, provider.name, provider.model) is not None:
                    cache_hits += 1
                else:
                    would_embed += 1
                continue
            # REAL mode — _embed_with_cache does hash → cache-hit skip →
            # provider.get_embedding → database.save_cached_embedding.
            vector, cache_hit = embed_fn(text, provider, cache_enabled=True)
            if vector is None:
                failed += 1
            elif cache_hit:
                cache_hits += 1
            else:
                embedded += 1
            time.sleep(pacing)
        print("[embed-backfill] page %d: %d rows seen (running total %d)"
              "  embedded=%d cache_hit=%d%s  ~$%.4f"
              % (page_no, len(rows), total, embedded, cache_hits,
                 (" would_embed=%d" % would_embed) if dry_run else "",
                 embedded * ROUGH_COST_PER_ROW))

    # ---- Final summary -----------------------------------------------------
    print()
    print("=== SUMMARY%s ===" % (" (DRY-RUN — no API call, no write)" if dry_run else ""))
    print("  rows seen                 : %d" % total)
    if dry_run:
        print("  would embed (cache miss)  : %d" % would_embed)
        print("  already cached (skip)     : %d" % cache_hits)
        print("  projected spend           : ~$%.4f" % (would_embed * ROUGH_COST_PER_ROW))
    else:
        print("  newly embedded            : %d" % embedded)
        print("  cache hits (skipped, $0)  : %d" % cache_hits)
        print("  embed failures (None)     : %d" % failed)
        print("  rough estimated spend     : ~$%.4f" % (embedded * ROUGH_COST_PER_ROW))
    print("  skipped empty title+claim : %d" % skipped_empty)
    print()
    print("[Safety] Wrote ONLY embedding_cache via the existing "
          "_embed_with_cache -> save_cached_embedding path. No "
          "analysis_results write; no verdict/scoring/label field touched; "
          "no secrets printed.")
    return embedded, cache_hits, would_embed, skipped_empty, failed


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — mock provider + in-memory cache (no DB, no network).
# Patches semantic_similarity.database with a fake so the REAL
# _embed_with_cache runs end-to-end against an in-memory store, proving the
# reuse path (hash → cache-hit skip → get_embedding → save) without any I/O.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        assert sql == SELECT_SQL, "unexpected SQL in selftest: %r" % sql
        last_id, limit = params
        picked = sorted((r for r in self._rows if r[0] > last_id))[:limit]
        self._result = picked

    def fetchall(self):
        return list(self._result or [])


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


class _MockProvider:
    """Fixed-vector provider; records every text it was asked to embed."""
    name = "mock"
    model = "mock-model"
    available = True

    def __init__(self):
        self.embedded_texts = []

    def get_embedding(self, text):
        self.embedded_texts.append(text)
        return [1.0, 0.0, 0.0]


class _FakeDatabaseModule:
    """In-memory stand-in for the two database.* calls _embed_with_cache makes."""

    def __init__(self):
        self.store = {}
        self.saves = 0

    def get_cached_embedding(self, text_hash, provider, model):
        return self.store.get((text_hash, provider, model))

    def save_cached_embedding(self, text_hash, provider, model, vector,
                              text_preview=""):
        self.store[(text_hash, provider, model)] = list(vector)
        self.saves += 1
        return True


def run_selftest() -> int:
    import semantic_similarity
    from semantic_embeddings import hash_text_for_cache

    print("=== EMBED-BACKFILL --selftest (offline; no DB, no network) ===")
    rows = [
        (1, "제목A", "주장A"),          # normal → embeds "제목A\n주장A"
        (2, "제목B", ""),               # empty claim → embeds title only
        (3, "제목C", "주장C"),          # pre-seeded in cache → cache-hit skip
        (4, "", ""),                    # empty both → skipped, never embedded
    ]

    fake_db = _FakeDatabaseModule()
    provider = _MockProvider()
    # Pre-seed id=3's concat key so the REAL _embed_with_cache hits the cache.
    seeded_hash = hash_text_for_cache("제목C\n주장C")
    fake_db.store[(seeded_hash, provider.name, provider.model)] = [9.0, 9.0, 9.0]

    orig_db = semantic_similarity.database
    semantic_similarity.database = fake_db
    try:
        embedded, cache_hits, _, skipped_empty, failed = run_backfill(
            _FakeConn(rows), provider, batch=2, max_rows=None, dry_run=False,
            pacing=0,
        )
    finally:
        semantic_similarity.database = orig_db

    # (a) concat form is title\nclaim.
    concat_ok = "제목A\n주장A" in provider.embedded_texts
    print("  [%s] (a) embed text is f\"{title}\\n{claim_text}\"" % ("ok" if concat_ok else "xx"))
    # (b) empty-claim rows embed title-only.
    title_only_ok = "제목B" in provider.embedded_texts
    print("  [%s] (b) empty claim_text -> title-only embed" % ("ok" if title_only_ok else "xx"))
    # (c) cache-hit rows are skipped (idempotency): id=3 never re-embedded,
    #     its stored vector untouched, and exactly 2 new saves happened.
    id3_skipped = ("제목C\n주장C" not in provider.embedded_texts
                   and cache_hits == 1
                   and fake_db.store[(seeded_hash, provider.name, provider.model)] == [9.0, 9.0, 9.0]
                   and fake_db.saves == 2 and embedded == 2)
    print("  [%s] (c) cache-hit row skipped, not re-embedded (idempotent)" % ("ok" if id3_skipped else "xx"))
    empty_ok = skipped_empty == 1 and failed == 0
    print("  [%s]     empty title+claim row skipped cleanly" % ("ok" if empty_ok else "xx"))

    # (d) --dry-run writes nothing and calls no embedding API.
    fake_db2 = _FakeDatabaseModule()
    fake_db2.store[(seeded_hash, provider.name, provider.model)] = [9.0, 9.0, 9.0]
    provider2 = _MockProvider()
    _, hits2, would2, _, _ = run_backfill(
        _FakeConn(rows), provider2, batch=10, max_rows=None, dry_run=True,
        cache_lookup=fake_db2.get_cached_embedding, pacing=0,
    )
    dry_ok = (provider2.embedded_texts == [] and fake_db2.saves == 0
              and would2 == 2 and hits2 == 1)
    print("  [%s] (d) dry-run: no API call, no write (would_embed=2, cached=1)" % ("ok" if dry_ok else "xx"))

    ok = all([concat_ok, title_only_ok, id3_skipped, empty_ok, dry_ok])
    print()
    print("SELFTEST: %s" % ("PASS (concat form + title-only + cache-hit skip + dry-run no-write)"
                            if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="embed_backfill",
        description="Embed title+claim_text of every analysis_results row into "
                    "embedding_cache (additive metadata; no analysis_results "
                    "write; idempotent via the cache-hit skip).",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE logic check (mock provider + in-memory cache).")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help="Rows per SELECT page. Default %d." % DEFAULT_BATCH)
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Optional total cap for this run. Default: all rows.")
    parser.add_argument("--start-id", type=int, default=0,
                        help="Resume from this id cursor; only ids > this are "
                             "scanned. Default 0 (whole table).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Hash + cache-existence check only; NO API call, NO write.")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if args.limit <= 0:
        print("--limit must be positive.")
        return 2

    # --- Env guards: NO DB connect / NO API call when preconditions fail. ----
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell (or locally "
              "with $env:DATABASE_URL pointed at the external DB).")
        return 0
    if os.environ.get("USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — cache saves would fall back to "
              "local SQLite instead of the shared Postgres embedding_cache. "
              "Set USE_POSTGRES_WRITE=true (the Worker already has it) and retry.")
        return 0

    # Lazy imports (after env guards) so importing this module never connects.
    import psycopg
    from semantic_embeddings import get_active_provider

    provider = get_active_provider()
    if not provider.available:
        # Fail closed — never silently embed with a wrong/deterministic
        # provider (the cache key carries provider+model).
        print("Embedding provider unavailable: %s" % (provider.reason or provider.name))
        print("Need SEMANTIC_MATCHING_ENABLED=true, EMBEDDING_PROVIDER=openai, "
              "EMBEDDING_MODEL=text-embedding-3-small, OPENAI_API_KEY set "
              "(the Worker already has all four).")
        return 1

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("EMBED-BACKFILL — title+claim_text into embedding_cache "
          "(provider=%s model=%s)" % (provider.name, provider.model))
    print("  batch=%d  max_rows=%s  dry_run=%s"
          % (args.limit, args.max_rows if args.max_rows is not None else "all",
             args.dry_run))
    print()
    with psycopg.connect(url) as conn:
        run_backfill(conn, provider, args.limit, args.max_rows, args.dry_run,
                     start_id=args.start_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
