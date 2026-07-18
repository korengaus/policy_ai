# BRAINMAP-SNAPSHOT Slice 1 — operator-run passive-accumulation script:
# append ONE dated growth-snapshot row PER CLUSTER of the newest
# brainmap_graph build into a self-created `brainmap_snapshots` table
# (stable_id + outlet_count + member_count + snapshot_date + provenance).
#
# WHY: cluster-growth history is not retroactive — every un-snapshotted
# build is lost forever. These rows are the historical backing the future
# paid trend/velocity surfaces (§27b blueprint, passive-accumulation item 2)
# will need. Run after each brainmap rebuild.
#
# USAGE (operator, LOCAL machine or Worker Shell — DATABASE_URL at the
# external Postgres, USE_POSTGRES_WRITE=true):
#   python scripts/snapshot_brainmap_growth.py --dry-run   # preview, no write
#   python scripts/snapshot_brainmap_growth.py             # append today's batch
#   python scripts/snapshot_brainmap_growth.py --force     # append even if the
#                                                          # same graph was
#                                                          # snapshotted today
#   python scripts/snapshot_brainmap_growth.py --selftest  # offline check
#
# SAFETY:
#   * Writes ONLY the `brainmap_snapshots` table (additive, self-created via
#     CREATE TABLE IF NOT EXISTS — the weekly_reports / brainmap_graph
#     precedent; materializes on the first real run, NOT at deploy).
#   * APPEND-ONLY: every run INSERTs a new dated batch. There is no UPDATE,
#     no DELETE, no upsert anywhere in this file — prior rows are history
#     and must never change. Re-running on a NEW build the same day is fine
#     (denser series). Re-running on the SAME build the same day is skipped
#     as an exact-duplicate batch unless --force.
#   * VERDICT-FREE BY CONSTRUCTION: reads brainmap_graph.graph_json ONLY —
#     stable_id / outlet_count / cluster membership counts. No verdict_label /
#     policy_confidence_score / truth_claim / any scoring column is ever
#     selected, and the stored rows carry NO generated prose at all (ids,
#     counts, dates only), so there is no string surface for verdict
#     vocabulary to leak into.
#   * Fail-closed: refuses without DATABASE_URL; refuses to write without
#     USE_POSTGRES_WRITE=true (--dry-run needs only DATABASE_URL).
#     Never prints DATABASE_URL or any API key.
#   * No numpy — this reads the already-built graph JSON.

import argparse
import json
import os
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

# Same single-row read the weekly generator and the spread endpoint use.
SELECT_NEWEST_GRAPH_SQL = (
    "SELECT id, generated_at, graph_json FROM brainmap_graph "
    "ORDER BY id DESC LIMIT 1"
)

# The ONLY write this script performs — an additive, self-created table
# (mirrors weekly_reports' create-on-demand pattern verbatim).
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS brainmap_snapshots ("
    "id SERIAL PRIMARY KEY, "
    "snapshot_date TEXT, "
    "graph_ref INTEGER, "
    "graph_generated_at TEXT, "
    "cluster_stable_id TEXT, "
    "outlet_count INTEGER, "
    "member_count INTEGER, "
    "cluster_lineage_id TEXT, "
    "created_at TEXT)"
)
# STABLE-CLUSTER-ID — additive, nullable column for tables created before the
# lineage change (CREATE IF NOT EXISTS never alters an existing table). Old
# rows stay NULL until scripts/backfill_cluster_lineage.py threads them.
ALTER_ADD_LINEAGE_SQL = (
    "ALTER TABLE brainmap_snapshots "
    "ADD COLUMN IF NOT EXISTS cluster_lineage_id TEXT"
)
INSERT_SQL = (
    "INSERT INTO brainmap_snapshots "
    "(snapshot_date, graph_ref, graph_generated_at, cluster_stable_id, "
    "outlet_count, member_count, cluster_lineage_id, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)
# Exact-duplicate-batch guard only (same graph already snapshotted today).
# Deliberately NOT a per-cluster upsert key — append-only time series.
SELECT_EXISTING_BATCH_SQL = (
    "SELECT id FROM brainmap_snapshots "
    "WHERE snapshot_date = %s AND graph_ref = %s LIMIT 1"
)


def build_snapshot_rows(graph, snapshot_date, graph_ref, graph_generated_at):
    """Pure compute: graph JSON dict -> list of per-cluster snapshot rows.

    member_count is counted from the node list (authoritative membership),
    falling back to the cluster's stored ``size`` when a graph carries no
    node array (defensive; current builds always include nodes). Clusters
    without a stable_id are skipped — a growth series needs a stable key.

    STABLE-CLUSTER-ID: cluster_lineage_id passes through the cluster's
    lineage_id when the graph carries one (post-lineage builds), else None —
    pre-lineage graph rows snapshot with a NULL lineage, exactly what the
    one-time backfill fills in later.
    """
    members_by_cluster = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        if cid is None or node.get("id") is None:
            continue
        members_by_cluster[cid] = members_by_cluster.get(cid, 0) + 1

    rows = []
    for cluster in graph.get("clusters") or []:
        stable_id = cluster.get("stable_id")
        if not stable_id:
            continue
        cid = cluster.get("cluster_id")
        member_count = members_by_cluster.get(cid) or cluster.get("size") or 0
        rows.append({
            "snapshot_date": snapshot_date,
            "graph_ref": graph_ref,
            "graph_generated_at": graph_generated_at,
            "cluster_stable_id": stable_id,
            "outlet_count": int(cluster.get("outlet_count") or 0),
            "member_count": int(member_count),
            "cluster_lineage_id": cluster.get("lineage_id") or None,
        })
    return rows


def append_batch(existing_rows, new_rows, force=False):
    """Pure append semantics (unit-tested + selftested): returns the store
    AFTER a run. Prior rows are NEVER mutated or removed — the only two
    outcomes are [existing + new] or, when the exact batch (snapshot_date,
    graph_ref) already exists and force is False, [existing] unchanged."""
    if not new_rows:
        return list(existing_rows), False
    if not force and any(
        r["snapshot_date"] == new_rows[0]["snapshot_date"]
        and r["graph_ref"] == new_rows[0]["graph_ref"]
        for r in existing_rows
    ):
        return list(existing_rows), False
    return list(existing_rows) + list(new_rows), True


def print_batch(rows, sample=5):
    print("[snapshot] %d cluster rows for snapshot_date=%s graph_ref=%s"
          % (len(rows),
             rows[0]["snapshot_date"] if rows else "-",
             rows[0]["graph_ref"] if rows else "-"))
    for row in rows[:sample]:
        print("  %s outlets=%d members=%d"
              % (row["cluster_stable_id"], row["outlet_count"],
                 row["member_count"]))
    if len(rows) > sample:
        print("  ... (+%d more)" % (len(rows) - sample))


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic graph. No DB, no network.
# ---------------------------------------------------------------------------

def _synthetic_graph():
    return {
        "nodes": [
            {"id": 11, "cluster_id": 0, "title": "a"},
            {"id": 12, "cluster_id": 0, "title": "b"},
            {"id": 13, "cluster_id": 0, "title": "c"},
            {"id": 21, "cluster_id": 1, "title": "d"},
            {"id": 22, "cluster_id": 1, "title": "e"},
            {"id": 30, "cluster_id": None, "title": "noise"},
        ],
        "clusters": [
            {"cluster_id": 0, "stable_id": "aaa111aaa111", "outlet_count": 4,
             "size": 3},
            {"cluster_id": 1, "stable_id": "bbb222bbb222", "outlet_count": 2,
             "size": 2},
            {"cluster_id": 2, "stable_id": "", "outlet_count": 9, "size": 1},
        ],
    }


def run_selftest() -> int:
    failures = []

    def check(name, ok):
        print("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    rows = build_snapshot_rows(_synthetic_graph(), "2026-07-11", 7, "g-at")
    check("skips clusters without stable_id (2 of 3 kept)", len(rows) == 2)
    by_sid = {r["cluster_stable_id"]: r for r in rows}
    check("stable_id extracted",
          set(by_sid) == {"aaa111aaa111", "bbb222bbb222"})
    check("outlet_count extracted",
          by_sid["aaa111aaa111"]["outlet_count"] == 4
          and by_sid["bbb222bbb222"]["outlet_count"] == 2)
    check("member_count counted from nodes",
          by_sid["aaa111aaa111"]["member_count"] == 3
          and by_sid["bbb222bbb222"]["member_count"] == 2)
    check("row shape complete",
          all(set(r) == {"snapshot_date", "graph_ref", "graph_generated_at",
                         "cluster_stable_id", "outlet_count", "member_count",
                         "cluster_lineage_id"}
              for r in rows))
    # STABLE-CLUSTER-ID: lineage passes through when present; pre-lineage
    # graphs (no lineage_id key — this synthetic one) snapshot as None.
    check("lineage None for pre-lineage graphs",
          all(r["cluster_lineage_id"] is None for r in rows))
    lineage_graph = _synthetic_graph()
    lineage_graph["clusters"][0]["lineage_id"] = "lin-000000aa"
    lineage_rows = build_snapshot_rows(lineage_graph, "2026-07-11", 7, "g-at")
    lineage_by_sid = {r["cluster_stable_id"]: r for r in lineage_rows}
    check("lineage passes through when the graph carries it",
          lineage_by_sid["aaa111aaa111"]["cluster_lineage_id"] == "lin-000000aa"
          and lineage_by_sid["bbb222bbb222"]["cluster_lineage_id"] is None)
    check("provenance carried",
          all(r["snapshot_date"] == "2026-07-11" and r["graph_ref"] == 7
              and r["graph_generated_at"] == "g-at" for r in rows))

    # member_count falls back to cluster size when the graph has no nodes.
    bare = {"clusters": [{"cluster_id": 0, "stable_id": "ccc333ccc333",
                          "outlet_count": 1, "size": 5}]}
    fallback = build_snapshot_rows(bare, "2026-07-11", 8, "")
    check("member_count falls back to cluster size",
          fallback[0]["member_count"] == 5)

    # APPEND semantics: a second run ADDS rows; prior rows stay untouched.
    day1 = build_snapshot_rows(_synthetic_graph(), "2026-07-10", 6, "g1")
    store, wrote1 = append_batch([], day1)
    day1_frozen = json.dumps(store, sort_keys=True)
    day2 = build_snapshot_rows(_synthetic_graph(), "2026-07-11", 7, "g2")
    store2, wrote2 = append_batch(store, day2)
    check("first run writes", wrote1 and len(store) == 2)
    check("second run APPENDS (2 -> 4 rows)", wrote2 and len(store2) == 4)
    check("prior rows untouched by second run",
          json.dumps(store2[:2], sort_keys=True) == day1_frozen)
    # Exact-duplicate batch (same date + graph_ref) skips unless --force.
    store3, wrote3 = append_batch(store2, day2)
    check("exact duplicate batch skipped", not wrote3 and len(store3) == 4)
    store4, wrote4 = append_batch(store2, day2, force=True)
    check("--force appends the duplicate batch", wrote4 and len(store4) == 6)

    # No UPDATE/DELETE/UPSERT in any SQL this script can execute. (The lineage
    # ALTER is ADD COLUMN IF NOT EXISTS — schema-additive, still append-only.)
    sql_constants = (SELECT_NEWEST_GRAPH_SQL, CREATE_TABLE_SQL, INSERT_SQL,
                     SELECT_EXISTING_BATCH_SQL, ALTER_ADD_LINEAGE_SQL)
    check("append-only SQL (no UPDATE/DELETE/UPSERT statement)",
          not any(keyword in statement.upper()
                  for statement in sql_constants
                  for keyword in ("UPDATE", "DELETE", "ON CONFLICT")))

    print("[selftest] %s" % ("PASS" if not failures else
                             "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="snapshot_brainmap_growth",
        description="Append one dated growth-snapshot row per cluster of the "
                    "newest brainmap_graph into brainmap_snapshots.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graph; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + print the batch; NO CREATE TABLE, NO INSERT.")
    parser.add_argument("--force", action="store_true",
                        help="Append even if this graph was already "
                             "snapshotted today (exact-duplicate batch).")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — point it at the external Postgres.")
        return 0
    if not args.dry_run and os.environ.get(
            "USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it "
              "true, or use --dry-run.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    now = datetime.now(timezone.utc)
    snapshot_date = now.date().isoformat()
    created_at = now.isoformat()
    print("SNAPSHOT-BRAINMAP-GROWTH — snapshot_date=%s%s"
          % (snapshot_date, " (DRY-RUN)" if args.dry_run else ""))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_NEWEST_GRAPH_SQL)
            graph_row = cur.fetchone()
        if not graph_row:
            print("[snapshot] no brainmap_graph row — run "
                  "scripts/build_brainmap_graph.py first.")
            return 1
        graph_ref, graph_generated_at, graph_json = graph_row
        try:
            graph = json.loads(graph_json)
        except (TypeError, ValueError):
            print("[snapshot] newest brainmap_graph row holds invalid JSON — "
                  "aborting.")
            return 1

        rows = build_snapshot_rows(graph, snapshot_date, graph_ref,
                                   str(graph_generated_at or ""))
        if not rows:
            print("[snapshot] graph has no clusters with a stable_id — "
                  "nothing to snapshot.")
            return 0
        print_batch(rows)
        if args.dry_run:
            print("[snapshot] DRY-RUN — no CREATE TABLE, no INSERT.")
            return 0

        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            # STABLE-CLUSTER-ID: additive nullable column for pre-existing
            # tables; a no-op (IF NOT EXISTS) on fresh ones.
            cur.execute(ALTER_ADD_LINEAGE_SQL)
            cur.execute(SELECT_EXISTING_BATCH_SQL, (snapshot_date, graph_ref))
            if cur.fetchone() and not args.force:
                print("[snapshot] graph_ref=%s already snapshotted on %s — "
                      "skipping exact-duplicate batch (use --force to append "
                      "anyway)." % (graph_ref, snapshot_date))
                return 0
            for row in rows:
                cur.execute(INSERT_SQL, (
                    row["snapshot_date"], row["graph_ref"],
                    row["graph_generated_at"], row["cluster_stable_id"],
                    row["outlet_count"], row["member_count"],
                    row["cluster_lineage_id"], created_at,
                ))
        conn.commit()
        print("[snapshot] appended %d brainmap_snapshots rows "
              "(snapshot_date=%s, graph_ref=%s)"
              % (len(rows), snapshot_date, graph_ref))
    return 0


if __name__ == "__main__":
    sys.exit(main())
