"""TEMPORAL-MAP PROBE — READ-ONLY, SELECT-only measurement of whether temporal cluster
tracking shows real signal, and how much work a STABLE CLUSTER ID actually is.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE / ALTER /
commit. Touches no pipeline, verdict, honesty, or display code. Prints numbers only.
Same discipline as scripts/relevance_gate_probe.py and scripts/echo_chamber_probe.py.

THE DECISION THIS FEEDS
-----------------------
Temporal tracking needs cluster identity that survives a rebuild. The question is whether
assigning one is DAYS of work (promote an already-near-stable key) or WEEKS (build
cross-snapshot matching + churn handling). That number decides pre-launch vs post-launch.

THE CENTRAL FINDING THIS PROBE IS BUILT TO MEASURE
--------------------------------------------------
Clusters ALREADY carry a stable_id (build_brainmap_graph.py:285-291):

    member_row_ids = sorted(ids[i] for i in members)
    stable_id = sha256(",".join(str(rid) for rid in member_row_ids))[:12]

It hashes the FULL member set. So it is stable against a re-run over unchanged data —
an idempotency key — but it changes the moment a cluster GAINS OR LOSES A SINGLE MEMBER.
Growth is precisely what temporal tracking exists to observe, so stable_id is by
construction NOT a temporal identity. Two consequences worth measuring, not assuming:

  * brainmap_snapshots (scripts/snapshot_brainmap_growth.py:63-73) is keyed on
    cluster_stable_id and is append-only. If stable_ids rarely survive a rebuild, that
    time series is ALREADY silently broken for lineage: a growing cluster appears as a
    sequence of unrelated one-row entries rather than one trajectory. Section 1 measures
    the survival rate directly instead of trusting the "rebuild-stable" name.
  * The real identity signal must come from MEMBER OVERLAP (or anchor_analysis_id).
    Sections 2-4 measure how well those work, which is what sizes the job.

WHERE THE DATA LIVES (confirmed against write/read paths)
---------------------------------------------------------
  * brainmap_graph(id, generated_at, params_json, graph_json) — one row per rebuild,
    written only by scripts/build_brainmap_graph.py:106-116, :436-454. History is
    RETAINED (old rows are never deleted), which is what makes this probe possible.
  * graph_json = {nodes, edges, clusters, generated_at, params}.
    Cluster keys: cluster_id (POSITIONAL, size-desc — renumbered every rebuild, useless
    as identity), stable_id, anchor_analysis_id, size, outlet_count,
    near_anchor_outlet_count, label_title.
    Membership is NOT on the cluster record; it lives on nodes[].cluster_id, so member
    sets must be reconstructed per snapshot (same as api_server.py:734-741 does).
  * brainmap_snapshots(id, snapshot_date, graph_ref, graph_generated_at,
    cluster_stable_id, outlet_count, member_count, created_at) — counts only, no members.

WHAT IT PRINTS
--------------
  0. SNAPSHOT AVAILABILITY: brainmap_graph row count, generated_at range, gaps between
     rebuilds. If there are fewer than 2 usable snapshots, temporal tracking CANNOT be
     validated at all yet — regardless of stable IDs — and the probe says so and stops
     drawing conclusions rather than inventing them.
  1. stable_id SURVIVAL across consecutive rebuilds + the brainmap_snapshots lineage
     check (how many cluster_stable_ids ever appear in more than one snapshot).
  2. MATCHABILITY per consecutive pair, by three candidate identity keys:
       (a) stable_id exact match, (b) anchor_analysis_id match, (c) member-set Jaccard
     best match. Outcomes classified 1:1 / SPLIT / MERGE / VANISHED / NEW.
  3. EXAMPLE TRAJECTORIES: clusters chained across >=3 snapshots by member overlap,
     showing size and outlet_count over time — the actual signal check.
  4. CHURN: among confidently-matched pairs, growth (members added, none lost) vs
     RESHUFFLE (members both added AND lost). Reshuffle is re-clustering nondeterminism,
     which a stable ID alone does NOT fix — it contaminates the temporal signal itself.
  5. WORK ESTIMATE: a rule stated up front and then applied to the measured numbers.

SAFETY: SELECT-only, engine.connect() (never begin()), no commit. Snapshots are loaded
ONE PAIR AT A TIME and reduced to compact per-cluster summaries (the raw ~441KB graph
blob is released immediately) so memory stays flat on the 2GB Worker. Lazy DB import so
--selftest is fully offline. UTF-8 guarded prints.

Usage (in the Render Worker Shell, after commit + push + Worker redeploy + reopen Shell):
    PYTHONPATH=. python scripts/temporal_map_probe.py
    PYTHONPATH=. python scripts/temporal_map_probe.py --selftest    # offline, no DB
    PYTHONPATH=. python scripts/temporal_map_probe.py --max-snapshots 6

Exit codes: 0 = report printed / not enough snapshots / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# A pair is a confident 1:1 successor when the member-set Jaccard clears this AND the
# match is MUTUAL (each is the other's best). Chosen to be strict enough that "1:1" in
# the output means what a reader assumes it means.
JACCARD_STRONG = 0.5
# Below this, a best match is treated as no real successor at all.
JACCARD_WEAK = 0.2


def p(message: str = "") -> None:
    print(message, flush=True)


def _int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def jaccard(left: frozenset, right: frozenset) -> float:
    if not left and not right:
        return 0.0
    union = len(left | right)
    return (len(left & right) / union) if union else 0.0


def summarize_snapshot(graph: dict) -> list:
    """Pure: graph JSON -> compact per-cluster summaries. Member sets are rebuilt from
    nodes[].cluster_id because cluster records carry no member list. The caller drops the
    raw graph right after this returns, so nothing large is retained."""
    members = defaultdict(set)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cluster_id = node.get("cluster_id")
        if cluster_id is not None:
            members[cluster_id].add(node.get("id"))

    summaries = []
    for cluster in graph.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        cluster_id = cluster.get("cluster_id")
        summaries.append(
            {
                "cluster_id": cluster_id,
                "stable_id": str(cluster.get("stable_id") or ""),
                "anchor": cluster.get("anchor_analysis_id"),
                "size": _int(cluster.get("size")),
                "outlet_count": _int(cluster.get("outlet_count")),
                "label": str(cluster.get("label_title") or "")[:70],
                "members": frozenset(members.get(cluster_id, set())),
            }
        )
    return summaries


def match_snapshots(prev: list, curr: list) -> dict:
    """Pure: classify every cluster in `prev` against its best successor in `curr`.

    Mutual-best + Jaccard >= JACCARD_STRONG  -> one_to_one
    Best is strong but NOT mutual            -> merge   (several prev -> one curr)
    Strong match exists but prev also splits -> split   (prev overlaps >1 curr strongly)
    Best Jaccard < JACCARD_WEAK              -> vanished
    """
    best_for_prev = {}
    for i, left in enumerate(prev):
        best_j, best_score = -1, 0.0
        strong_hits = 0
        for j, right in enumerate(curr):
            score = jaccard(left["members"], right["members"])
            if score >= JACCARD_STRONG:
                strong_hits += 1
            if score > best_score:
                best_j, best_score = j, score
        best_for_prev[i] = (best_j, best_score, strong_hits)

    best_for_curr = {}
    for j, right in enumerate(curr):
        best_i, best_score = -1, 0.0
        for i, left in enumerate(prev):
            score = jaccard(left["members"], right["members"])
            if score > best_score:
                best_i, best_score = i, score
        best_for_curr[j] = (best_i, best_score)

    outcomes = Counter()
    pairs = []
    claimed_curr = Counter()
    for i, (j, score, strong_hits) in best_for_prev.items():
        if j >= 0 and score >= JACCARD_STRONG:
            claimed_curr[j] += 1

    for i, (j, score, strong_hits) in best_for_prev.items():
        if j < 0 or score < JACCARD_WEAK:
            outcomes["vanished"] += 1
            continue
        if score < JACCARD_STRONG:
            outcomes["weak"] += 1
            continue
        if strong_hits > 1:
            outcomes["split"] += 1
            continue
        if claimed_curr[j] > 1:
            outcomes["merge"] += 1
            continue
        if best_for_curr[j][0] == i:
            outcomes["one_to_one"] += 1
            pairs.append((i, j, score))
        else:
            outcomes["merge"] += 1

    matched_curr = {j for _, j, _ in pairs}
    outcomes["new"] = len(curr) - len(matched_curr)

    stable_prev = {c["stable_id"] for c in prev if c["stable_id"]}
    stable_curr = {c["stable_id"] for c in curr if c["stable_id"]}
    anchors_prev = {c["anchor"] for c in prev if c["anchor"] is not None}
    anchors_curr = {c["anchor"] for c in curr if c["anchor"] is not None}

    return {
        "prev_n": len(prev),
        "curr_n": len(curr),
        "outcomes": outcomes,
        "pairs": pairs,
        "stable_survived": len(stable_prev & stable_curr),
        "stable_prev": len(stable_prev),
        "anchor_survived": len(anchors_prev & anchors_curr),
        "anchor_prev": len(anchors_prev),
    }


def churn_for_pairs(prev: list, curr: list, pairs: list) -> dict:
    """Pure: among confidently-matched pairs, separate real growth from reshuffle."""
    grew = shrank = reshuffled = identical = 0
    added_total = lost_total = 0
    for i, j, _score in pairs:
        before, after = prev[i]["members"], curr[j]["members"]
        added, lost = after - before, before - after
        added_total += len(added)
        lost_total += len(lost)
        if not added and not lost:
            identical += 1
        elif added and lost:
            reshuffled += 1
        elif added:
            grew += 1
        else:
            shrank += 1
    return {
        "pairs": len(pairs),
        "identical": identical,
        "grew": grew,
        "shrank": shrank,
        "reshuffled": reshuffled,
        "added_total": added_total,
        "lost_total": lost_total,
    }


def build_trajectories(snapshots: list, limit: int) -> list:
    """Pure: chain clusters across consecutive snapshots by mutual-best member overlap.
    Returns the longest chains first — those are the demonstrable temporal signal."""
    if len(snapshots) < 2:
        return []
    chains = []
    links = []
    for index in range(len(snapshots) - 1):
        result = match_snapshots(snapshots[index]["clusters"], snapshots[index + 1]["clusters"])
        links.append({i: j for i, j, _ in result["pairs"]})

    started = set()
    for start_index in range(len(snapshots) - 1):
        for i in range(len(snapshots[start_index]["clusters"])):
            if (start_index, i) in started:
                continue
            chain = [(start_index, i)]
            index, node = start_index, i
            while index < len(links) and node in links[index]:
                node = links[index][node]
                index += 1
                chain.append((index, node))
                started.add((index, node))
            if len(chain) >= 3:
                chains.append(chain)
    chains.sort(key=len, reverse=True)
    return chains[:limit]


def report_work_estimate(agg: dict, snapshot_count: int) -> None:
    p("")
    p("=== 5. WORK ESTIMATE for stable cluster IDs ===")
    p("  RULE (stated before the numbers, so it is not fitted to them):")
    p("    DAYS   - an existing key already survives rebuilds for most clusters, so the")
    p("             job is to PERSIST and promote it (schema + backfill + read path).")
    p("    ~1-2wk - member overlap matches reliably (1:1 >= 60%) but no existing key does,")
    p("             so cross-snapshot matching must be written, plus a lineage table.")
    p("    WEEKS  - matching itself is unreliable (1:1 < 60%) or reshuffle churn is high,")
    p("             in which case a stable ID papers over a nondeterministic clustering")
    p("             and the real fix is upstream, not an ID scheme.")
    p("")
    if snapshot_count < 2:
        p("  VERDICT: NOT ESTIMABLE. Fewer than 2 snapshots exist, so nothing above was")
        p("  measured. Any estimate now would be invention. The weekly spine writes one")
        p("  brainmap_graph row per run — wait for at least 3 rebuilds, then re-run this.")
        return

    pairs_total = agg["outcomes"].get("one_to_one", 0)
    prev_total = agg["prev_total"]
    one_to_one_rate = (100.0 * pairs_total / prev_total) if prev_total else 0.0
    stable_rate = (100.0 * agg["stable_survived"] / agg["stable_prev"]) if agg["stable_prev"] else 0.0
    anchor_rate = (100.0 * agg["anchor_survived"] / agg["anchor_prev"]) if agg["anchor_prev"] else 0.0
    reshuffle_rate = (100.0 * agg["churn"]["reshuffled"] / agg["churn"]["pairs"]) if agg["churn"]["pairs"] else 0.0

    p(f"  measured: stable_id survival {stable_rate:.1f}% | anchor survival {anchor_rate:.1f}%")
    p(f"            member-overlap 1:1 {one_to_one_rate:.1f}% | reshuffle churn {reshuffle_rate:.1f}%")
    p("")
    if reshuffle_rate >= 40.0:
        p("  VERDICT: WEEKS - and an ID scheme is the WRONG first fix.")
        p("  Reshuffle churn is high: clusters swap members in BOTH directions between")
        p("  rebuilds, so the membership itself is unstable. A stable ID would faithfully")
        p("  track a boundary that is moving for pipeline reasons, not real-world ones,")
        p("  and the temporal map would show motion that is not news. Fix determinism")
        p("  upstream first. Do NOT block launch on this.")
    elif anchor_rate >= 80.0 or stable_rate >= 80.0:
        p("  VERDICT: DAYS. An existing key already survives most rebuilds, so the work is")
        p("  to persist it as the lineage key and read it back - schema + backfill + read")
        p("  path, no new matching logic. Pre-launch is realistic IF the signal in")
        p("  sections 3-4 is real.")
    elif one_to_one_rate >= 60.0:
        p("  VERDICT: ~1-2 WEEKS. No existing key survives, but member overlap matches")
        p("  reliably, so cross-snapshot matching plus a lineage table is a real but")
        p("  bounded build. Pre-launch is a judgement call, not a given - it is not a")
        p("  one-line promotion of an existing field.")
    else:
        p("  VERDICT: WEEKS. Neither an existing key nor member overlap matches reliably.")
        p("  Cluster identity would have to be re-derived with tolerance for split/merge,")
        p("  and every downstream number inherits that ambiguity. Post-launch.")

    p("")
    p("  In ALL cases note: stable_id (build_brainmap_graph.py:285-291) hashes the FULL")
    p("  member set, so it changes whenever a cluster grows. It is an idempotency key,")
    p("  never a temporal identity - and brainmap_snapshots is keyed on it today.")
    p("  Section 1 shows what that already costs the existing time series.")


def run_live(max_snapshots: int, examples: int) -> int:
    p("=== TEMPORAL-MAP PROBE (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    with engine.connect() as conn:
        try:
            heads = conn.execute(
                sa.text("SELECT id, generated_at FROM brainmap_graph ORDER BY id DESC LIMIT :n")
                .bindparams(n=max_snapshots)
            ).all()
        except Exception as exc:
            p(f"brainmap_graph unreadable ({exc}). Has scripts/build_brainmap_graph.py run?")
            return 0

        p("")
        p("=== 0. SNAPSHOT AVAILABILITY ===")
        total = _int(conn.execute(sa.text("SELECT count(*) FROM brainmap_graph")).scalar())
        p(f"  brainmap_graph rows total : {total}")
        if not heads:
            p("  (table empty - the weekly spine has not built a graph yet)")
            report_work_estimate({"outcomes": Counter(), "prev_total": 0, "stable_survived": 0,
                                  "stable_prev": 0, "anchor_survived": 0, "anchor_prev": 0,
                                  "churn": churn_for_pairs([], [], [])}, 0)
            return 0
        heads = list(reversed(heads))  # oldest -> newest
        for row_id, generated_at in heads:
            p(f"    id={row_id:<6} generated_at={generated_at}")
        if total < 2:
            p("")
            p("  >>> Fewer than 2 snapshots exist. Temporal tracking cannot be validated")
            p("      at all yet, with or without stable IDs. Everything below is skipped")
            p("      rather than computed against a single point.")
            report_work_estimate({"outcomes": Counter(), "prev_total": 0, "stable_survived": 0,
                                  "stable_prev": 0, "anchor_survived": 0, "anchor_prev": 0,
                                  "churn": churn_for_pairs([], [], [])}, total)
            return 0

        p("")
        p("=== 1. stable_id LINEAGE (existing time series health) ===")
        try:
            snap_rows = _int(conn.execute(sa.text("SELECT count(*) FROM brainmap_snapshots")).scalar())
            snap_ids = _int(conn.execute(
                sa.text("SELECT count(DISTINCT cluster_stable_id) FROM brainmap_snapshots")).scalar())
            snap_multi = _int(conn.execute(sa.text(
                "SELECT count(*) FROM (SELECT cluster_stable_id FROM brainmap_snapshots "
                "GROUP BY cluster_stable_id HAVING count(DISTINCT graph_ref) > 1) t")).scalar())
            p(f"  brainmap_snapshots rows            : {snap_rows}")
            p(f"  distinct cluster_stable_id         : {snap_ids}")
            p(f"  stable_ids seen in >1 graph        : {snap_multi}  ({_pct(snap_multi, snap_ids)})")
            p("  ^ that last % IS the lineage survival rate of the CURRENT time series.")
            p("    If it is near zero, brainmap_snapshots cannot express a trajectory")
            p("    today: each rebuild of a growing cluster writes a brand-new id.")
        except Exception as exc:
            p(f"  (brainmap_snapshots unreadable: {exc})")

        # Load snapshots one at a time, reduce, release.
        snapshots = []
        for row_id, generated_at in heads:
            raw = conn.execute(
                sa.text("SELECT graph_json FROM brainmap_graph WHERE id = :i").bindparams(i=row_id)
            ).scalar()
            graph = json.loads(raw) if raw else {}
            snapshots.append({
                "id": row_id,
                "generated_at": generated_at,
                "clusters": summarize_snapshot(graph),
            })
            del raw, graph

    agg = {
        "outcomes": Counter(),
        "prev_total": 0,
        "stable_survived": 0,
        "stable_prev": 0,
        "anchor_survived": 0,
        "anchor_prev": 0,
    }
    churn_agg = {"pairs": 0, "identical": 0, "grew": 0, "shrank": 0, "reshuffled": 0,
                 "added_total": 0, "lost_total": 0}

    p("")
    p("=== 2. MATCHABILITY across consecutive rebuilds ===")
    for index in range(len(snapshots) - 1):
        prev, curr = snapshots[index], snapshots[index + 1]
        result = match_snapshots(prev["clusters"], curr["clusters"])
        churn = churn_for_pairs(prev["clusters"], curr["clusters"], result["pairs"])
        agg["outcomes"].update(result["outcomes"])
        agg["prev_total"] += result["prev_n"]
        agg["stable_survived"] += result["stable_survived"]
        agg["stable_prev"] += result["stable_prev"]
        agg["anchor_survived"] += result["anchor_survived"]
        agg["anchor_prev"] += result["anchor_prev"]
        for key in churn_agg:
            churn_agg[key] += churn[key]

        p("")
        p(f"  graph {prev['id']} -> {curr['id']}   clusters {result['prev_n']} -> {result['curr_n']}")
        p(f"    (a) stable_id survived    : {result['stable_survived']:>5} / {result['stable_prev']}  {_pct(result['stable_survived'], result['stable_prev'])}")
        p(f"    (b) anchor_id survived    : {result['anchor_survived']:>5} / {result['anchor_prev']}  {_pct(result['anchor_survived'], result['anchor_prev'])}")
        p(f"    (c) member-overlap 1:1    : {result['outcomes']['one_to_one']:>5} / {result['prev_n']}  {_pct(result['outcomes']['one_to_one'], result['prev_n'])}")
        for key in ("split", "merge", "weak", "vanished", "new"):
            p(f"        {key:<10}            : {result['outcomes'][key]:>5}")

    agg["churn"] = churn_agg

    p("")
    p(f"=== 3. EXAMPLE TRAJECTORIES (chains across >=3 snapshots, up to {examples}) ===")
    chains = build_trajectories(snapshots, examples)
    if not chains:
        p("  (none - no cluster held a confident 1:1 successor across 3+ snapshots.)")
        p("  With <3 snapshots this is expected and says nothing about the signal.")
    for chain in chains:
        first_index, first_i = chain[0]
        p("")
        p(f"  {snapshots[first_index]['clusters'][first_i]['label'] or '(no label)'}")
        for index, node in chain:
            cluster = snapshots[index]["clusters"][node]
            p(f"    graph {snapshots[index]['id']:<6} {str(snapshots[index]['generated_at'])[:10]:<12}"
              f" size={cluster['size']:<5} outlets={cluster['outlet_count']:<4} stable_id={cluster['stable_id']}")
        p("    ^ compare the stable_id column across rows: if it changes while the cluster")
        p("      is plainly the same story, that is the prerequisite problem, visible.")

    p("")
    p("=== 4. CHURN among confidently-matched pairs ===")
    pairs = churn_agg["pairs"]
    p(f"  matched pairs              : {pairs}")
    p(f"    identical membership     : {churn_agg['identical']:>6}  {_pct(churn_agg['identical'], pairs)}")
    p(f"    grew only (members added): {churn_agg['grew']:>6}  {_pct(churn_agg['grew'], pairs)}")
    p(f"    shrank only              : {churn_agg['shrank']:>6}  {_pct(churn_agg['shrank'], pairs)}")
    p(f"    RESHUFFLED (added+lost)  : {churn_agg['reshuffled']:>6}  {_pct(churn_agg['reshuffled'], pairs)}")
    p(f"  members added total        : {churn_agg['added_total']}")
    p(f"  members lost total         : {churn_agg['lost_total']}")
    p("  ^ 'grew only' is real-world signal. 'RESHUFFLED' is re-clustering")
    p("    nondeterminism: the same story trading members in both directions between")
    p("    rebuilds. A stable ID does NOT fix reshuffle - it would just attach a durable")
    p("    name to a boundary that keeps moving for non-news reasons.")

    report_work_estimate(agg, len(snapshots))
    return 0


def _selftest() -> int:
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    check("jaccard-same", jaccard(frozenset({1, 2}), frozenset({1, 2})), 1.0)
    check("jaccard-none", jaccard(frozenset({1}), frozenset({2})), 0.0)
    check("jaccard-half", jaccard(frozenset({1, 2}), frozenset({2, 3})), 1 / 3)
    check("jaccard-empty", jaccard(frozenset(), frozenset()), 0.0)

    graph = {
        "nodes": [
            {"id": 1, "cluster_id": 0}, {"id": 2, "cluster_id": 0},
            {"id": 3, "cluster_id": 1}, {"id": 4, "cluster_id": None},
        ],
        "clusters": [
            {"cluster_id": 0, "stable_id": "aaa", "anchor_analysis_id": 1, "size": 2, "outlet_count": 2},
            {"cluster_id": 1, "stable_id": "bbb", "anchor_analysis_id": 3, "size": 1, "outlet_count": 1},
        ],
    }
    summary = summarize_snapshot(graph)
    check("summary-count", len(summary), 2)
    check("summary-members", summary[0]["members"], frozenset({1, 2}))
    check("summary-singleton-excluded", 4 in summary[0]["members"] or 4 in summary[1]["members"], False)

    # Growth: cluster 0 gains a member. stable_id MUST churn while overlap stays strong —
    # this is the whole thesis of the probe, asserted rather than assumed.
    prev = [{"cluster_id": 0, "stable_id": "aaa", "anchor": 1, "size": 2, "outlet_count": 2,
             "label": "x", "members": frozenset({1, 2})}]
    curr = [{"cluster_id": 0, "stable_id": "zzz", "anchor": 1, "size": 3, "outlet_count": 3,
             "label": "x", "members": frozenset({1, 2, 5})}]
    result = match_snapshots(prev, curr)
    check("growth-1to1", result["outcomes"]["one_to_one"], 1)
    check("growth-stable-churned", result["stable_survived"], 0)
    check("growth-anchor-held", result["anchor_survived"], 1)
    churn = churn_for_pairs(prev, curr, result["pairs"])
    check("growth-grew", churn["grew"], 1)
    check("growth-reshuffled", churn["reshuffled"], 0)

    # Vanish: no overlap at all.
    gone = match_snapshots(prev, [{"cluster_id": 0, "stable_id": "q", "anchor": 9, "size": 2,
                                   "outlet_count": 2, "label": "y", "members": frozenset({7, 8})}])
    check("vanished", gone["outcomes"]["vanished"], 1)
    check("new", gone["outcomes"]["new"], 1)

    # Reshuffle: members swapped both ways but overlap still strong.
    resh_prev = [{"cluster_id": 0, "stable_id": "a", "anchor": 1, "size": 4, "outlet_count": 4,
                  "label": "z", "members": frozenset({1, 2, 3, 4})}]
    resh_curr = [{"cluster_id": 0, "stable_id": "b", "anchor": 1, "size": 4, "outlet_count": 4,
                  "label": "z", "members": frozenset({1, 2, 3, 9})}]
    resh = match_snapshots(resh_prev, resh_curr)
    resh_churn = churn_for_pairs(resh_prev, resh_curr, resh["pairs"])
    check("reshuffle-detected", resh_churn["reshuffled"], 1)
    check("reshuffle-not-growth", resh_churn["grew"], 0)

    # Split: one prev cluster strongly overlaps two curr clusters.
    split_prev = [{"cluster_id": 0, "stable_id": "a", "anchor": 1, "size": 4, "outlet_count": 4,
                   "label": "s", "members": frozenset({1, 2, 3, 4})}]
    split_curr = [
        {"cluster_id": 0, "stable_id": "b", "anchor": 1, "size": 3, "outlet_count": 3,
         "label": "s", "members": frozenset({1, 2, 3})},
        {"cluster_id": 1, "stable_id": "c", "anchor": 4, "size": 3, "outlet_count": 3,
         "label": "s", "members": frozenset({1, 2, 4})},
    ]
    check("split", match_snapshots(split_prev, split_curr)["outcomes"]["split"], 1)

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TEMPORAL-MAP probe (SELECT-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check, no DB")
    parser.add_argument("--max-snapshots", type=int, default=8, help="newest N graphs to compare")
    parser.add_argument("--examples", type=int, default=5, help="trajectories to print")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    return run_live(args.max_snapshots, args.examples)


if __name__ == "__main__":
    raise SystemExit(main())
