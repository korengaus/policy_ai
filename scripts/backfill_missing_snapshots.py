"""BACKFILL-MISSING-SNAPSHOTS — create brainmap_snapshots rows for brainmap_graph
rows that were never snapshotted, so topic-timeline trajectories span the full
history that already exists.

THE GAP
-------
snapshot_brainmap_growth.py only started appending at graph_ref 3, so the
earliest graphs (measured live: ids 1 and 2, generated 2026-07-06 and 07-08)
have NO snapshot rows at all. Every /api/topic-timeline trajectory therefore
starts at 7/11 and loses ~5 days of real, already-computed history. This is a
one-time, zero-LLM-cost repair: the graphs exist, only their snapshot rows are
missing.

Note the distinction from scripts/backfill_cluster_lineage.py: that one UPDATEd
the cluster_lineage_id COLUMN on rows that already existed. Graphs 1-2 have no
rows to update — this script INSERTs them.

LINEAGE CONSISTENCY — the thing that makes or breaks the trajectory
--------------------------------------------------------------------
A cluster present in graph 2 AND graph 3 must carry the SAME lineage_id in both,
or its trajectory splits into two disconnected stubs. Guaranteed by construction,
not by hope: this script threads ALL graphs oldest->newest through the SAME pure
assign_lineage_ids that backfill_cluster_lineage.py used (imported from
build_brainmap_graph — one implementation, never copied). Same function, same
inputs, same order => byte-identical lineage assignment.

Because "identical by construction" is a claim and not a measurement, the script
VERIFIES it: for every graph that ALREADY has snapshot rows, it compares the
freshly computed lineage against the stored cluster_lineage_id. Any mismatch
ABORTS the insert rather than writing rows that would fragment a trajectory.

WHAT IT WRITES (and nothing else)
----------------------------------
INSERT INTO brainmap_snapshots only — the same column list and the same
build_snapshot_rows() extraction snapshot_brainmap_growth.py uses (imported, not
reimplemented), so backfilled rows are shaped exactly like live ones. It NEVER
touches brainmap_graph or analysis_results, and performs no UPDATE or DELETE.

snapshot_date for a backfilled graph is the graph's OWN generated_at date (e.g.
2026-07-06), not today — these are historical observations and dating them today
would put the whole backfill on one fake date and flatten the trajectory it is
meant to widen. (Live runs date rows by run-day, which is why graph 3, generated
07-10, carries snapshot_date 07-11.)

IDEMPOTENT: a graph that already has snapshot rows is skipped entirely, so a
second run plans 0 inserts.

Usage (Joe runs this, after commit + push + Worker redeploy + reopen Shell):
    PYTHONPATH=. python scripts/backfill_missing_snapshots.py --selftest
    PYTHONPATH=. python scripts/backfill_missing_snapshots.py --dry-run
    PYTHONPATH=. python scripts/backfill_missing_snapshots.py

Exit codes: 0 = done / nothing missing / preconditions unmet; 1 = selftest failed
or a lineage-consistency mismatch blocked the write.
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

# The ONE lineage implementation and the ONE row builder — both imported.
from build_brainmap_graph import assign_lineage_ids  # noqa: E402
from snapshot_brainmap_growth import (  # noqa: E402
    ALTER_ADD_LINEAGE_SQL,
    CREATE_TABLE_SQL,
    INSERT_SQL,
    build_snapshot_rows,
)

SELECT_GRAPHS_SQL = "SELECT id, generated_at, graph_json FROM brainmap_graph ORDER BY id ASC"
SELECT_SNAPSHOTTED_REFS_SQL = "SELECT DISTINCT graph_ref FROM brainmap_snapshots"
SELECT_STORED_LINEAGE_SQL = (
    "SELECT graph_ref, cluster_stable_id, cluster_lineage_id "
    "FROM brainmap_snapshots WHERE cluster_lineage_id IS NOT NULL"
)


def p(message: str = "") -> None:
    print(message, flush=True)


def find_missing_graph_refs(all_refs, snapshotted_refs):
    """Pure: graph ids present in brainmap_graph but absent from
    brainmap_snapshots, oldest first."""
    have = set(snapshotted_refs or ())
    return [ref for ref in sorted(all_refs) if ref not in have]


def thread_all_graphs(graph_rows):
    """Pure: [(graph_ref, graph_dict), ...] OLDEST FIRST -> the same in-memory
    annotation walk backfill_cluster_lineage.thread_lineage performs. Each graph
    is annotated by assign_lineage_ids and then becomes the next step's
    prev_graph, so lineage chains across the whole span. Mutates the graph dicts
    in place (that is how build_snapshot_rows later sees lineage_id) and returns
    {(graph_ref, stable_id): lineage_id}."""
    mapping = {}
    prev_graph = None
    for graph_ref, graph in graph_rows:
        assign_lineage_ids(prev_graph, graph)
        for cluster in graph.get("clusters") or []:
            stable_id = cluster.get("stable_id")
            lineage_id = cluster.get("lineage_id")
            if stable_id and lineage_id:
                mapping[(graph_ref, stable_id)] = lineage_id
        prev_graph = graph
    return mapping


def check_lineage_consistency(mapping, stored_rows):
    """Pure: computed lineage vs what is ALREADY stored for snapshotted graphs.

    Returns (matched, mismatches) where mismatches is a list of
    (graph_ref, stable_id, stored, computed). A non-empty list means the
    threading no longer reproduces the earlier backfill — inserting would
    fragment trajectories, so the caller must abort."""
    matched = 0
    mismatches = []
    for graph_ref, stable_id, stored in stored_rows:
        computed = mapping.get((graph_ref, stable_id))
        if computed is None:
            continue
        if computed == stored:
            matched += 1
        else:
            mismatches.append((graph_ref, stable_id, stored, computed))
    return matched, mismatches


def spanning_lineage_samples(mapping, missing_refs, limit=3):
    """Pure: lineages that appear in BOTH a missing graph and a later already-
    snapshotted graph — the exact case whose ids must agree for a trajectory to
    connect. Returns [(lineage_id, [(graph_ref, stable_id), ...]), ...]."""
    by_lineage = {}
    for (graph_ref, stable_id), lineage_id in mapping.items():
        by_lineage.setdefault(lineage_id, []).append((graph_ref, stable_id))
    missing = set(missing_refs)
    samples = []
    for lineage_id, entries in sorted(by_lineage.items()):
        refs = {ref for ref, _ in entries}
        if refs & missing and refs - missing:
            samples.append((lineage_id, sorted(entries)))
        if len(samples) >= limit:
            break
    return samples


def _graph_date(generated_at) -> str:
    """The graph's OWN date (YYYY-MM-DD) — historical rows must not be dated
    today, which would flatten the trajectory this backfill exists to widen."""
    text = str(generated_at or "")
    return text[:10] if len(text) >= 10 else text


def run(dry_run: bool) -> int:
    p("=== BACKFILL-MISSING-SNAPSHOTS%s ===" % (" (DRY-RUN)" if dry_run else ""))

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
    from datetime import datetime, timezone

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(SELECT_GRAPHS_SQL)
                raw_graphs = cur.fetchall()
            except psycopg.errors.UndefinedTable:
                p("brainmap_graph does not exist — nothing to snapshot.")
                return 0
        graph_rows = []
        dates = {}
        for graph_ref, generated_at, graph_json in raw_graphs:
            try:
                graph = json.loads(graph_json)
            except (TypeError, ValueError):
                p("  [warn] graph_ref=%s holds invalid JSON — skipped." % graph_ref)
                continue
            graph_rows.append((graph_ref, graph))
            dates[graph_ref] = _graph_date(generated_at)
        if not graph_rows:
            return 0

        with conn.cursor() as cur:
            try:
                cur.execute(SELECT_SNAPSHOTTED_REFS_SQL)
                snapshotted = [r[0] for r in cur.fetchall()]
                cur.execute(SELECT_STORED_LINEAGE_SQL)
                stored_rows = cur.fetchall()
            except psycopg.errors.UndefinedTable:
                snapshotted, stored_rows = [], []
                conn.rollback()

        all_refs = [ref for ref, _ in graph_rows]
        missing = find_missing_graph_refs(all_refs, snapshotted)
        p("  brainmap_graph rows      : %s" % all_refs)
        p("  already snapshotted      : %s" % sorted(set(snapshotted)))
        p("  MISSING (to backfill)    : %s" % missing)
        if not missing:
            p("  Nothing missing — idempotent no-op.")
            return 0

        # Thread lineage across the WHOLE span, exactly as the earlier
        # cluster-lineage backfill did, so missing graphs get ids consistent
        # with the already-stored ones.
        mapping = thread_all_graphs(graph_rows)
        matched, mismatches = check_lineage_consistency(mapping, stored_rows)
        p("")
        p("  lineage consistency vs STORED rows: %d matched, %d MISMATCHED"
          % (matched, len(mismatches)))
        if mismatches:
            for graph_ref, stable_id, stored, computed in mismatches[:5]:
                p("    graph_ref=%s stable_id=%s stored=%s computed=%s"
                  % (graph_ref, stable_id, stored, computed))
            p("  ABORT — the threading no longer reproduces the stored lineage.")
            p("  Inserting would fragment trajectories. Investigate before rerun.")
            return 1

        p("")
        p("  spanning-lineage samples (must share ONE lineage_id across the")
        p("  missing graph and the later snapshotted one for the trajectory to")
        p("  connect):")
        samples = spanning_lineage_samples(mapping, missing)
        if not samples:
            p("    (none — no cluster survived from a missing graph into a")
            p("     snapshotted one; trajectories simply gain earlier points.)")
        for lineage_id, entries in samples:
            p("    lineage %s -> %s" % (lineage_id,
                                        ", ".join("graph %s" % ref for ref, _ in entries)))

        graph_by_ref = dict(graph_rows)
        created_at = datetime.now(timezone.utc).isoformat()
        planned = []
        for graph_ref in missing:
            graph = graph_by_ref.get(graph_ref) or {}
            snapshot_date = dates.get(graph_ref) or ""
            rows = build_snapshot_rows(graph, snapshot_date, graph_ref,
                                       str(snapshot_date))
            planned.append((graph_ref, snapshot_date, rows))
            with_lineage = sum(1 for r in rows if r["cluster_lineage_id"])
            p("")
            p("  graph_ref=%s date=%s -> %d snapshot rows (%d with lineage)"
              % (graph_ref, snapshot_date, len(rows), with_lineage))

        total = sum(len(rows) for _ref, _date, rows in planned)
        if dry_run:
            p("")
            p("  DRY-RUN — would INSERT %d rows across %d graph(s). No writes."
              % (total, len(planned)))
            return 0

        inserted = 0
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(ALTER_ADD_LINEAGE_SQL)
            for _graph_ref, _date, rows in planned:
                for row in rows:
                    cur.execute(INSERT_SQL, (
                        row["snapshot_date"], row["graph_ref"],
                        row["graph_generated_at"], row["cluster_stable_id"],
                        row["outlet_count"], row["member_count"],
                        row["cluster_lineage_id"], created_at,
                    ))
                    inserted += 1
        conn.commit()
        p("")
        p("  DONE — inserted %d brainmap_snapshots rows for graphs %s."
          % (inserted, missing))
        p("  Re-running is safe: those graphs now have rows and will be skipped.")
    return 0


def _selftest() -> int:
    failures = []

    def check(name, ok):
        p("  [%s] %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failures.append(name)

    def graph(spec):
        nodes, clusters = [], []
        for cid, (stable_id, member_ids) in spec.items():
            for member_id in member_ids:
                nodes.append({"id": member_id, "cluster_id": cid})
            clusters.append({"cluster_id": cid, "stable_id": stable_id,
                             "outlet_count": len(member_ids),
                             "size": len(member_ids)})
        return {"nodes": nodes, "clusters": clusters}

    # --- missing-graph detection ------------------------------------------
    check("detects graphs absent from snapshots",
          find_missing_graph_refs([1, 2, 3, 4], [3, 4]) == [1, 2])
    check("nothing missing -> empty (idempotent second run)",
          find_missing_graph_refs([1, 2, 3], [1, 2, 3]) == [])
    check("detection is general, not hardcoded to 1-2",
          find_missing_graph_refs([1, 2, 3, 9], [1, 3]) == [2, 9])

    # --- lineage threading across the span --------------------------------
    # Cluster A spans graphs 1->2->3 while GROWING (stable_id churns each step);
    # graph 3 is already snapshotted, graphs 1-2 are missing.
    g1 = graph({0: ("s1-aaa", [1, 2, 3])})
    g2 = graph({0: ("s2-bbb", [1, 2, 3, 4])})
    g3 = graph({0: ("s3-ccc", [1, 2, 3, 4, 5]), 1: ("s3-new", [90, 91])})
    mapping = thread_all_graphs([(1, g1), (2, g2), (3, g3)])
    check("grown cluster keeps ONE lineage across graphs 1-3",
          mapping[(1, "s1-aaa")] == mapping[(2, "s2-bbb")] == mapping[(3, "s3-ccc")])
    check("late-arriving cluster mints its own lineage",
          mapping[(3, "s3-new")] == "s3-new")

    # --- consistency check vs stored --------------------------------------
    lineage = mapping[(3, "s3-ccc")]
    matched, mismatches = check_lineage_consistency(
        mapping, [(3, "s3-ccc", lineage), (3, "s3-new", "s3-new")])
    check("computed lineage matches stored -> no mismatch",
          matched == 2 and not mismatches)
    _m, bad = check_lineage_consistency(mapping, [(3, "s3-ccc", "WRONG")])
    check("divergent stored lineage is reported as a mismatch",
          len(bad) == 1 and bad[0][2] == "WRONG")

    # --- the spanning sample (what the dry-run shows Joe) ------------------
    samples = spanning_lineage_samples(mapping, [1, 2])
    check("spanning lineage sample found (missing graph + snapshotted graph)",
          any(lin == lineage for lin, _ in samples))

    # --- row building reuses snapshot_brainmap_growth ---------------------
    rows = build_snapshot_rows(g2, "2026-07-08", 2, "2026-07-08")
    check("rows built for the missing graph carry the threaded lineage",
          len(rows) == 1 and rows[0]["cluster_lineage_id"] == lineage)
    check("row shape matches the live snapshot writer",
          set(rows[0]) == {"snapshot_date", "graph_ref", "graph_generated_at",
                           "cluster_stable_id", "outlet_count", "member_count",
                           "cluster_lineage_id"})
    check("snapshot_date is the GRAPH's date, not today",
          rows[0]["snapshot_date"] == "2026-07-08")
    check("graph date extraction from a timestamp",
          _graph_date("2026-07-06 09:59:34.874407+00:00") == "2026-07-06")

    # --- write-statement audit --------------------------------------------
    combined = (SELECT_GRAPHS_SQL + SELECT_SNAPSHOTTED_REFS_SQL
                + SELECT_STORED_LINEAGE_SQL + INSERT_SQL).upper()
    check("only INSERT writes; no UPDATE/DELETE/TRUNCATE",
          "INSERT INTO BRAINMAP_SNAPSHOTS" in INSERT_SQL.upper()
          and not any(word in combined
                      for word in ("UPDATE ", "DELETE ", "TRUNCATE",
                                   "ON CONFLICT")))
    check("never targets brainmap_graph or analysis_results for writing",
          "BRAINMAP_GRAPH" not in INSERT_SQL.upper()
          and "ANALYSIS_RESULTS" not in INSERT_SQL.upper())

    p("[selftest] %s" % ("PASS" if not failures else "FAIL: " + ", ".join(failures)))
    return 0 if not failures else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_missing_snapshots",
        description="Create brainmap_snapshots rows for never-snapshotted "
                    "brainmap_graph rows. INSERTs into brainmap_snapshots only.")
    parser.add_argument("--selftest", action="store_true",
                        help="OFFLINE logic check (synthetic graphs; no DB).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan + consistency check only; NO writes.")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
