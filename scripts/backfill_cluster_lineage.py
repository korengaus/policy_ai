"""BACKFILL-CLUSTER-LINEAGE — one-time threading of durable lineage ids across the
EXISTING brainmap_graph history, written ONLY into brainmap_snapshots' new
cluster_lineage_id column.

WHAT IT DOES
------------
Reads every brainmap_graph row OLDEST -> NEWEST and threads them through the SAME
assign_lineage_ids used at build time (imported from build_brainmap_graph — one
implementation, not a copy), producing a (graph_ref, cluster_stable_id) ->
lineage_id mapping. Then UPDATEs ONLY the cluster_lineage_id column of matching
brainmap_snapshots rows.

WHAT IT NEVER DOES
------------------
  * NEVER mutates brainmap_graph rows — existing history stays byte-identical.
    The lineage annotation inside graph_json starts with the first post-change
    build; history gets lineage ONLY via the snapshots column.
  * NEVER touches analysis_results, any verdict/honesty field, or any other
    column of brainmap_snapshots. The single UPDATE statement sets
    cluster_lineage_id and nothing else, keyed on (graph_ref, cluster_stable_id).

IDEMPOTENT AND RE-RUNNABLE
--------------------------
The lineage computation is deterministic (same graphs -> same mapping), and the
UPDATE overwrites with the same values on a re-run. Snapshot rows whose
(graph_ref, stable_id) is not in the mapping (e.g. a graph_ref that no longer
exists) are left untouched and reported, never guessed.

Chaining detail: assign_lineage_ids reads a prev cluster's lineage_id, falling
back to stable_id. Since stored graph_json rows carry no lineage_id, this script
annotates each parsed graph IN MEMORY as it walks (the function mutates its
new_graph argument), so lineage chains across all rows exactly as it would have
had the builder carried it live from the start.

Usage (Joe runs this ONCE in the Render Worker Shell, after review):
    PYTHONPATH=. python scripts/backfill_cluster_lineage.py --dry-run   # preview
    PYTHONPATH=. python scripts/backfill_cluster_lineage.py            # write
    PYTHONPATH=. python scripts/backfill_cluster_lineage.py --selftest # offline

Exit codes: 0 = done / preconditions unmet (with guidance); 1 = selftest failed
or unexpected data shape.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
for entry in (str(_PROJECT_ROOT), str(_SCRIPTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# The ONE lineage implementation — never copied. Pure function; importing the
# builder module is safe (numpy imports live inside its build functions).
from build_brainmap_graph import assign_lineage_ids  # noqa: E402
from snapshot_brainmap_growth import ALTER_ADD_LINEAGE_SQL  # noqa: E402

SELECT_GRAPHS_SQL = (
    "SELECT id, graph_json FROM brainmap_graph ORDER BY id ASC"
)
SELECT_SNAPSHOT_KEYS_SQL = (
    "SELECT id, graph_ref, cluster_stable_id, cluster_lineage_id "
    "FROM brainmap_snapshots"
)
# The ONLY data write: the new column, nothing else, keyed on the snapshot
# row's own (graph_ref, cluster_stable_id).
UPDATE_SQL = (
    "UPDATE brainmap_snapshots SET cluster_lineage_id = %s "
    "WHERE graph_ref = %s AND cluster_stable_id = %s"
)


def p(message: str = "") -> None:
    print(message, flush=True)


def thread_lineage(graph_rows):
    """Pure: [(graph_ref, graph_dict), ...] OLDEST FIRST ->
    ({(graph_ref, stable_id): lineage_id}, per-graph stats list).

    Walks the history exactly as the live builder would have: each graph is
    annotated in memory by assign_lineage_ids and then serves as the next
    step's prev_graph, so lineage chains across the whole span."""
    mapping = {}
    stats = []
    prev_graph = None
    for graph_ref, graph in graph_rows:
        step = assign_lineage_ids(prev_graph, graph)
        for cluster in graph.get("clusters") or []:
            stable_id = cluster.get("stable_id")
            lineage_id = cluster.get("lineage_id")
            if stable_id and lineage_id:
                mapping[(graph_ref, stable_id)] = lineage_id
        stats.append({"graph_ref": graph_ref,
                      "clusters": len(graph.get("clusters") or []),
                      **step})
        prev_graph = graph
    return mapping, stats


def plan_updates(mapping, snapshot_rows):
    """Pure: decide which snapshot rows need an UPDATE.

    Returns (updates, already_correct, unmatched) where updates is
    [(lineage_id, graph_ref, stable_id), ...]. Rows already holding the
    correct lineage are skipped (idempotent re-run = 0 updates); rows whose
    key is not in the mapping are left alone and counted, never guessed."""
    updates = []
    already_correct = 0
    unmatched = 0
    seen_keys = set()
    for _row_id, graph_ref, stable_id, current in snapshot_rows:
        key = (graph_ref, stable_id)
        lineage_id = mapping.get(key)
        if lineage_id is None:
            unmatched += 1
            continue
        if current == lineage_id:
            already_correct += 1
            continue
        if key not in seen_keys:  # one UPDATE covers duplicate-batch rows
            seen_keys.add(key)
            updates.append((lineage_id, graph_ref, stable_id))
        else:
            # A --force duplicate batch shares the key; the single UPDATE
            # statement already rewrites every row with this key.
            pass
    return updates, already_correct, unmatched


def run(dry_run: bool) -> int:
    p("=== BACKFILL-CLUSTER-LINEAGE%s ===" % (" (DRY-RUN)" if dry_run else ""))

    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        p("DATABASE_URL not set — run in the Render Worker Shell.")
        return 0
    if not dry_run and os.environ.get(
            "USE_POSTGRES_WRITE", "").strip().lower() != "true":
        p("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it true, "
          "or use --dry-run.")
        return 0

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(SELECT_GRAPHS_SQL)
                raw_graphs = cur.fetchall()
            except psycopg.errors.UndefinedTable:
                p("brainmap_graph does not exist — nothing to thread.")
                return 0
        graph_rows = []
        for graph_ref, graph_json in raw_graphs:
            try:
                graph = json.loads(graph_json)
            except (TypeError, ValueError):
                p("  [warn] graph_ref=%s holds invalid JSON — skipped (its "
                  "snapshot rows will report as unmatched)." % graph_ref)
                continue
            graph_rows.append((graph_ref, graph))
        p("[lineage] %d brainmap_graph rows loaded (oldest -> newest)"
          % len(graph_rows))
        if not graph_rows:
            return 0

        mapping, stats = thread_lineage(graph_rows)
        for step in stats:
            p("  graph_ref=%-5s clusters=%-4d carried=%-4d minted=%-4d "
              "merged_away=%d"
              % (step["graph_ref"], step["clusters"], step["carried"],
                 step["minted"], step["merged_away"]))
        distinct_lineages = len(set(mapping.values()))
        p("[lineage] mapping: %d (graph, cluster) pairs -> %d distinct lineages"
          % (len(mapping), distinct_lineages))

        with conn.cursor() as cur:
            try:
                cur.execute(SELECT_SNAPSHOT_KEYS_SQL)
                snapshot_rows = cur.fetchall()
            except psycopg.errors.UndefinedColumn:
                conn.rollback()
                if dry_run:
                    p("[lineage] cluster_lineage_id column absent (dry-run "
                      "does not ALTER) — treating every row as needing an "
                      "update.")
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "SELECT id, graph_ref, cluster_stable_id, NULL "
                            "FROM brainmap_snapshots")
                        snapshot_rows = cur2.fetchall()
                else:
                    with conn.cursor() as cur2:
                        cur2.execute(ALTER_ADD_LINEAGE_SQL)
                    conn.commit()
                    with conn.cursor() as cur2:
                        cur2.execute(SELECT_SNAPSHOT_KEYS_SQL)
                        snapshot_rows = cur2.fetchall()
            except psycopg.errors.UndefinedTable:
                p("brainmap_snapshots does not exist — nothing to update. "
                  "(The mapping above is still the full lineage preview.)")
                return 0

        updates, already_correct, unmatched = plan_updates(mapping, snapshot_rows)
        p("[lineage] snapshot rows: %d total | %d already correct | "
          "%d to update | %d unmatched (left NULL, listed above if their "
          "graph was skipped)"
          % (len(snapshot_rows), already_correct, len(updates), unmatched))

        if dry_run:
            for lineage_id, graph_ref, stable_id in updates[:10]:
                p("  would set %s  (graph_ref=%s, stable_id=%s)"
                  % (lineage_id, graph_ref, stable_id))
            if len(updates) > 10:
                p("  ... (+%d more)" % (len(updates) - 10))
            p("[lineage] DRY-RUN — no ALTER, no UPDATE.")
            return 0

        updated_rows = 0
        with conn.cursor() as cur:
            cur.execute(ALTER_ADD_LINEAGE_SQL)  # no-op if present
            for lineage_id, graph_ref, stable_id in updates:
                cur.execute(UPDATE_SQL, (lineage_id, graph_ref, stable_id))
                updated_rows += cur.rowcount
        conn.commit()
        p("[lineage] DONE — %d UPDATE statements, %d snapshot rows now carry "
          "a lineage id. Re-running is safe (idempotent: next run reports "
          "them as already correct)." % (len(updates), updated_rows))
    return 0


def _selftest() -> int:
    failures = []

    def check(name, ok):
        print("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    def graph(spec):
        nodes, clusters = [], []
        for cid, (stable_id, member_ids) in spec.items():
            for member_id in member_ids:
                nodes.append({"id": member_id, "cluster_id": cid})
            clusters.append({"cluster_id": cid, "stable_id": stable_id})
        return {"nodes": nodes, "clusters": clusters}

    # Three-step history: cluster grows twice (stable_id churns each step),
    # a second cluster appears at step 2 and grows at step 3.
    g1 = graph({0: ("s1-aaa", [1, 2, 3])})
    g2 = graph({0: ("s2-bbb", [1, 2, 3, 4]), 1: ("s2-new", [50, 51])})
    g3 = graph({0: ("s3-ccc", [1, 2, 3, 4, 5]), 1: ("s3-ddd", [50, 51, 52])})
    mapping, stats = thread_lineage([(101, g1), (102, g2), (103, g3)])

    check("grown cluster keeps ONE lineage across all three graphs",
          mapping[(101, "s1-aaa")] == "s1-aaa"
          and mapping[(102, "s2-bbb")] == "s1-aaa"
          and mapping[(103, "s3-ccc")] == "s1-aaa")
    check("late-arriving cluster mints at first sight then carries",
          mapping[(102, "s2-new")] == "s2-new"
          and mapping[(103, "s3-ddd")] == "s2-new")
    check("distinct lineages = 2", len(set(mapping.values())) == 2)
    check("per-graph stats sane",
          stats[0]["minted"] == 1 and stats[1]["carried"] == 1
          and stats[1]["minted"] == 1 and stats[2]["carried"] == 2)

    # Deterministic: re-threading the same graphs yields the same mapping.
    # (fresh dicts — thread_lineage annotates its inputs in place)
    g1b = graph({0: ("s1-aaa", [1, 2, 3])})
    g2b = graph({0: ("s2-bbb", [1, 2, 3, 4]), 1: ("s2-new", [50, 51])})
    g3b = graph({0: ("s3-ccc", [1, 2, 3, 4, 5]), 1: ("s3-ddd", [50, 51, 52])})
    mapping2, _ = thread_lineage([(101, g1b), (102, g2b), (103, g3b)])
    check("idempotent: identical mapping on re-run", mapping == mapping2)

    # plan_updates: NULL -> update; correct -> skip; unknown key -> unmatched;
    # duplicate-batch rows share one UPDATE.
    snapshot_rows = [
        (1, 101, "s1-aaa", None),          # needs update
        (2, 102, "s2-bbb", "s1-aaa"),      # already correct
        (3, 103, "s3-ccc", None),          # needs update
        (4, 103, "s3-ccc", None),          # duplicate batch — same key
        (5, 999, "gone", None),            # unmatched graph_ref
    ]
    updates, already_correct, unmatched = plan_updates(mapping, snapshot_rows)
    check("update plan: 2 keys, 1 correct, 1 unmatched",
          len(updates) == 2 and already_correct == 1 and unmatched == 1)
    check("update rows carry (lineage, graph_ref, stable_id)",
          ("s1-aaa", 101, "s1-aaa") in updates
          and ("s1-aaa", 103, "s3-ccc") in updates)
    # Second pass after applying: everything correct, zero updates.
    applied = [(r[0], r[1], r[2], mapping.get((r[1], r[2]), r[3]))
               for r in snapshot_rows]
    updates2, correct2, unmatched2 = plan_updates(mapping, applied)
    check("re-run after apply: 0 updates, all correct",
          not updates2 and correct2 == 4 and unmatched2 == 1)

    # The single write statement touches ONLY the new column.
    check("UPDATE sets cluster_lineage_id and nothing else",
          UPDATE_SQL.count("SET") == 1
          and "cluster_lineage_id = %s" in UPDATE_SQL
          and "analysis_results" not in UPDATE_SQL
          and "brainmap_graph" not in UPDATE_SQL.replace(
              "brainmap_snapshots", ""))

    print("[selftest] %s" % ("PASS" if not failures else
                             "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_cluster_lineage",
        description="One-time lineage threading across existing brainmap_graph "
                    "history; writes ONLY brainmap_snapshots.cluster_lineage_id.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graphs; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute + print the full plan; NO ALTER, NO UPDATE.")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
