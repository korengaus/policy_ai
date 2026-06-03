"""M25a — offline, idempotent backfill of embedding_cache → embedding_vectors.

Copies existing JSON-cached embeddings (embedding_cache.vector_json) into the
typed pgvector table (embedding_vectors). Run MANUALLY by an operator after
deploy, only when PGVECTOR_ENABLED=true and the pgvector extension/table exist.
NOT run automatically anywhere.

Idempotent: upsert_embedding_vector uses ON CONFLICT (text_hash, provider,
model) DO UPDATE, so re-running is safe and re-copies nothing new. Follows
Lesson 2 — never injects explicit ids; SERIAL assigns them.

Usage (Render Worker Shell):
    PGVECTOR_ENABLED=true python scripts/backfill_embedding_vectors.py [--limit N]

Exit code 0 on success (including "nothing to do"); 1 on a hard precondition
failure (gate off, engine missing, pgvector unavailable). NEVER prints secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config  # noqa: E402
import sqlalchemy as sa  # noqa: E402
import postgres_storage as ps  # noqa: E402


def backfill(limit: int | None = None) -> int:
    """Copy embedding_cache rows into embedding_vectors. Returns the number of
    rows upserted. Preconditions are checked by the caller (main)."""
    engine = ps.get_engine()
    if engine is None:
        print("[backfill] no Postgres engine (DATABASE_URL / dual-write off) — abort")
        return 0
    # Make sure the typed table + extension exist before copying.
    if not ps._ensure_pgvector(engine):
        print("[backfill] pgvector not available (extension/table/package) — abort")
        return 0

    cache = ps.embedding_cache_table
    copied = 0
    with engine.connect() as conn:
        stmt = sa.select(
            cache.c.text_hash, cache.c.provider, cache.c.model,
            cache.c.dimensions, cache.c.vector_json,
            cache.c.text_preview, cache.c.created_at,
        )
        if limit:
            stmt = stmt.limit(int(limit))
        rows = conn.execute(stmt).fetchall()

    for row in rows:
        m = row._mapping
        try:
            vector = json.loads(m.get("vector_json") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(vector, list) or not vector:
            continue
        ok = ps.upsert_embedding_vector(
            text_hash=m.get("text_hash") or "",
            provider=m.get("provider") or "",
            model=m.get("model") or "",
            dimensions=int(m.get("dimensions") or len(vector)),
            embedding=[float(v) for v in vector],
            text_preview=m.get("text_preview") or "",
            created_at=m.get("created_at") or "",
        )
        if ok:
            copied += 1
    print(f"[backfill] upserted {copied} / {len(rows)} embedding_cache rows into embedding_vectors")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill embedding_vectors from embedding_cache (M25a).")
    parser.add_argument("--limit", type=int, default=None, help="max rows to copy (default: all)")
    args = parser.parse_args()

    if not config.pgvector_enabled():
        print("[backfill] PGVECTOR_ENABLED is false — refusing to run (set it true first)")
        return 1
    backfill(limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
