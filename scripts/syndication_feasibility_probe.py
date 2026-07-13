# SYNDICATION-STAT B5d Phase 1 — READ-ONLY feasibility probe (pin-OUT).
#
# QUESTION: within a cluster, can stored data (title+claim embeddings +
# published_at + exact-text collapse) separate "near-identical to the
# earliest report" from "independent phrasing"? This probe MEASURES the
# within-cluster similarity distribution so the near-copy threshold (and
# whether the stat is honest at all) is decided from data, not vibes.
#
# Joe runs it once in the Render Worker Shell (SELECT-only, no writes):
#     PYTHONPATH=. python scripts/syndication_feasibility_probe.py
#     PYTHONPATH=. python scripts/syndication_feasibility_probe.py --clusters 10
#
# WHAT IT PRINTS:
#   1. published_at coverage (the first-article anchor quality) — overall.
#   2. For the top-N largest clusters of the NEWEST brainmap_graph row:
#      - members + how many have a usable published_at (anchor coverage);
#      - the earliest member (published_at, min-id fallback) = the anchor;
#      - EXACT-tier: members sharing the anchor's exact title+claim text
#        hash are ALREADY collapsed into one node at graph build
#        (load_corpus_vectors first-hit rule) — so exact verbatim
#        republication shows up as node outlet_sets, printed here per node;
#      - NEAR-tier: cosine of every member's title+claim vector vs the
#        anchor's, bucketed (>=0.98 / >=0.95 / >=0.90 / >=0.85 / <0.85),
#        with the member titles of the >=0.95 bucket printed so a human can
#        judge whether ">=0.95 on title+claim" honestly reads as
#        near-identical phrasing (the overclaim check).
#
# HONESTY: similarity here is TITLE+CLAIM phrasing (full text is
# structurally absent post-DB-RECLAIM — bodies are never persisted). Any
# shipped stat must therefore claim "near-identical title/claim phrasing to
# the earliest report", never "copied article". This probe only measures;
# it makes no such claim itself. Verdict-free: no verdict column is read.
#
# SAFETY: SELECT-only; reads analysis_results (id, title, claim_text,
# published_at, original_url), brainmap_graph (newest graph_json),
# embedding_cache (vectors for the member hashes). Pure-python cosine — no
# numpy. Never prints DATABASE_URL. pin-OUT scripts/*; 331/16 unaffected.

import argparse
import json
import math
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

EMBED_PROVIDER = "openai"
EMBED_MODEL = "text-embedding-3-small"
BUCKETS = (0.98, 0.95, 0.90, 0.85)

COVERAGE_SQL = (
    "SELECT COUNT(*), "
    "SUM(CASE WHEN published_at IS NOT NULL AND published_at <> '' "
    "THEN 1 ELSE 0 END) FROM analysis_results"
)
NEWEST_GRAPH_SQL = (
    "SELECT id, graph_json FROM brainmap_graph ORDER BY id DESC LIMIT 1"
)
MEMBERS_SQL = (
    "SELECT id, title, claim_text, published_at, original_url "
    "FROM analysis_results WHERE id = ANY(%s)"
)
VECTORS_SQL = (
    "SELECT text_hash, vector_json FROM embedding_cache "
    "WHERE provider = %s AND model = %s AND text_hash = ANY(%s)"
)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(prog="syndication_feasibility_probe")
    parser.add_argument("--clusters", type=int, default=6,
                        help="How many largest clusters to sample (default 6).")
    parser.add_argument("--max-members", type=int, default=80,
                        help="Member cap per cluster (default 80).")
    args = parser.parse_args()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0

    import psycopg
    from embed_backfill import build_embed_text
    from semantic_embeddings import hash_text_for_cache

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    print("SYNDICATION FEASIBILITY PROBE — SELECT-only\n")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(COVERAGE_SQL)
            total, dated = cur.fetchone()
        print("== 1. published_at coverage (first-article anchor) ==")
        print("  rows=%d dated=%d (%.1f%%)\n"
              % (total, dated or 0, 100.0 * (dated or 0) / total if total else 0))

        with conn.cursor() as cur:
            cur.execute(NEWEST_GRAPH_SQL)
            row = cur.fetchone()
        if not row or not row[1]:
            print("no brainmap_graph row — build the graph first.")
            return 1
        graph = json.loads(row[1])
        print("== 2. within-cluster similarity vs earliest member "
              "(graph row id=%s) ==" % row[0])

        members_by_cid = {}
        for node in graph.get("nodes") or []:
            if node.get("cluster_id") is not None and node.get("id") is not None:
                members_by_cid.setdefault(node["cluster_id"], []).append(node["id"])
        clusters = sorted(
            (c for c in graph.get("clusters") or [] if c.get("cluster_id") is not None),
            key=lambda c: -(c.get("size") or 0))[:args.clusters]

        for cluster in clusters:
            member_ids = members_by_cid.get(cluster["cluster_id"], [])[:args.max_members]
            if len(member_ids) < 2:
                continue
            with conn.cursor() as cur:
                cur.execute(MEMBERS_SQL, (member_ids,))
                rows = cur.fetchall()
            by_id = {r[0]: r for r in rows}
            dated_members = [(r[3], r[0]) for r in rows if r[3]]
            anchor_id = min(dated_members)[1] if dated_members else min(by_id)
            anchor = by_id[anchor_id]

            # hash per member (exact-tier: same hash == same title+claim text)
            hashes = {}
            for rid, title, claim, _pub, _u in rows:
                text = build_embed_text(title, claim)
                if text:
                    hashes[rid] = hash_text_for_cache(text)
            wanted = sorted(set(hashes.values()))
            with conn.cursor() as cur:
                cur.execute(VECTORS_SQL, (EMBED_PROVIDER, EMBED_MODEL, wanted))
                vec_rows = cur.fetchall()
            vectors = {}
            for text_hash, vector_json in vec_rows:
                try:
                    vec = json.loads(vector_json)
                    if isinstance(vec, list) and vec:
                        vectors[text_hash] = vec
                except (TypeError, ValueError):
                    continue

            anchor_hash = hashes.get(anchor_id)
            anchor_vec = vectors.get(anchor_hash)
            print("\n-- cluster %s [%s] size=%d outlets=%s --"
                  % (cluster.get("stable_id"), (cluster.get("label_title") or "")[:48],
                     cluster.get("size") or 0, cluster.get("outlet_count")))
            print("   anchor id=%s published_at=%.19s title=%.60s"
                  % (anchor_id, str(anchor[3]), anchor[1] or ""))
            print("   members=%d dated=%d (%.0f%%) vectors_resolved=%d"
                  % (len(rows), len(dated_members),
                     100.0 * len(dated_members) / len(rows),
                     sum(1 for rid in hashes if hashes[rid] in vectors)))
            if anchor_vec is None:
                print("   anchor vector missing — cluster skipped.")
                continue

            exact = [rid for rid in hashes
                     if rid != anchor_id and hashes[rid] == anchor_hash]
            counts = {b: 0 for b in BUCKETS}
            below = 0
            high_pairs = []
            for rid, text_hash in hashes.items():
                if rid == anchor_id:
                    continue
                vec = vectors.get(text_hash)
                if vec is None:
                    continue
                sim = 1.0 if text_hash == anchor_hash else cosine(anchor_vec, vec)
                placed = False
                for b in BUCKETS:
                    if sim >= b:
                        counts[b] += 1
                        placed = True
                        break
                if not placed:
                    below += 1
                if sim >= 0.95:
                    high_pairs.append((sim, rid, by_id[rid][1] or ""))
            print("   exact-same title+claim text (collapsed nodes): %d" % len(exact))
            print("   sim vs anchor: >=0.98:%d  0.95-0.98:%d  0.90-0.95:%d  "
                  "0.85-0.90:%d  <0.85:%d"
                  % (counts[0.98], counts[0.95], counts[0.90], counts[0.85], below))
            for sim, rid, title in sorted(high_pairs, reverse=True)[:8]:
                print("     %.4f id=%-6s %.62s" % (sim, rid, title))

    print("\n[Probe] SELECT-only; nothing written; no verdict column read. "
          "Judge the >=0.95 titles above: do they READ as near-identical "
          "phrasing? That answers whether the stat ships honestly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
