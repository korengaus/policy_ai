"""ECHO-INDEPENDENCE PROBE — READ-ONLY measurement of how many outlets in a
brain-map cluster are plausibly INDEPENDENT, to decide the shape of item ⑤
(echo-chamber / independent-source count).

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE.
No clustering / matcher / verdict / score / display change. No threshold
applied. CIRCULATION vocabulary only — nothing here implies truth or falsity.
No embedding/cosine work (item ④ was measurement-closed and rejected).

★NO OWNERSHIP MAP. This probe does NOT author, hardcode, or guess any media-
ownership/group mapping — a wrong owner mapping would be a fabricated fact.
It reports ONLY what the host string itself can prove:
  (a) exact-duplicate hosts (already collapsed by the set), and
  (b) registrable-domain collapse — a MECHANICAL eTLD+1 rule
      (news.example.co.kr and sports.example.co.kr -> example.co.kr).
What host strings CANNOT prove, and this probe never claims: two DIFFERENT
registrable domains owned by one group (e.g. a conglomerate's paper + TV
station on separate domains) remain counted as separate. That direction needs
a curated ownership dataset — a separate data-acquisition project.

MECHANICS BEING MEASURED (confirmed from code, 2026-07-22)
----------------------------------------------------------
  * outlet_count = len(distinct normalized original_url hosts across cluster
    members), fallback len(members) when no member URL is usable
    (build_brainmap_graph.py:301-306). Host normalization: lowercase netloc,
    creds/port dropped, leading www./m. stripped (normalize_outlet_host,
    build_brainmap_graph.py:140-154).
  * Rows with byte-identical embed text (title\nclaim_text) collapse into ONE
    node with their outlet hosts UNIONED (outlets_by_hash,
    build_brainmap_graph.py:481-493) — so cluster member NODES under-represent
    rows, but outlet_count already includes collapsed rows' hosts. This probe
    reproduces that collapse verbatim (build_embed_text + hash_text_for_cache).
  * Cluster = union-find component of the kNN(k=10) cosine graph at
    sim>=0.80 over title+claim embeddings; only components with >1 node get a
    cluster_id — SINGLETON nodes carry cluster_id null and are NOT clusters
    (build_brainmap_graph.py:273-284). Lineage across rebuilds =
    assign_lineage_ids, CONTAINMENT |prev∩new|/|prev| >= 0.5 mutual-best
    (build_brainmap_graph.py:372-409).
  * Display paths additionally hide outlet_count < 2: the spread payload and
    topic timeline return found:false / empty below 2 (api_server.py:1294-1296),
    and the spread-size map only includes outlet_count >= 2
    (api_server.py:1351-1353). So singletons surface NOWHERE as clusters.
  * near_anchor_outlet_count = distinct outlets among members whose title+claim
    cosine vs the cluster's EARLIEST member (anchor) >= 0.95 — the existing
    "near-identical phrasing" syndication tier (build_brainmap_graph.py:307-337).
  * The ONLY outlet-identity data stored per row is original_url
    (postgres_storage.py:231). There is NO publisher/outlet-name/media-group
    column anywhere in analysis_results. (source_candidates' `publisher` is
    the EVIDENCE document's publisher, not the article's outlet.)

WHAT IT PRINTS
--------------
  1. CORPUS: nodes, singleton nodes (count, % — surfaced nowhere), clusters,
     clusters with outlet_count<2 (also surfaced nowhere).
  2. SAMPLE: top --top clusters by outlet_count + --random random others
     (seeded; random, not id-front).
  3. OUTLET DISTRIBUTION over the sample: stored outlet_count vs reproduced
     distinct-host count (min/p25/median/p95/max) — any delta is reported.
  4. TOP --top-hosts clusters: the ACTUAL host list, so concentration can be
     eyeballed by a human.
  5. NEAR-ANCHOR SHARE: near_anchor_outlet_count / outlet_count distribution
     (how much of the spread is near-identical phrasing).
  6. HOST-COLLAPSE (Step C): distinct hosts vs distinct registrable domains
     per cluster — how much "N개 매체" would shrink on host-string evidence
     alone; % of clusters affected; the biggest shrinkers with before/after.
  7. An explicit CANNOT-PROVE note (cross-domain ownership).

SAFETY: engine.connect() only, no commit, no table creation. Lazy DB imports
so --selftest is fully offline. UTF-8 guarded prints. Reuses (imports, never
re-implements) normalize_outlet_host / build_embed_text / hash_text_for_cache
so the reproduction can never drift from the builder.

Usage (Render Worker Shell, after commit+push+redeploy+reopen Shell):
    PYTHONPATH=. python scripts/echo_independence_probe.py
    PYTHONPATH=. python scripts/echo_independence_probe.py --selftest  # offline
    ... --top 20 --random 200 --seed 42 --top-hosts 10   # (defaults shown)

Exit codes: 0 = report printed / engine or graph unavailable (reported);
1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
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


# Mechanical eTLD+1 rule for .kr second-level public suffixes. This is DNS
# structure, not an ownership assertion: sub.example.co.kr -> example.co.kr is
# provable from the string; example.co.kr vs example-tv.com is NOT collapsed.
_KR_PUBLIC_SLD = {
    "co", "or", "go", "ne", "re", "pe", "ac", "hs", "ms", "es", "sc", "kg", "mil",
}


def registrable_domain(host: str) -> str:
    """eTLD+1 from the host string alone. Empty/garbage -> "" (excluded by
    callers, mirroring normalize_outlet_host's empty-host rule)."""
    host = (host or "").strip().lower().strip(".")
    if not host or "." not in host:
        return ""
    parts = host.split(".")
    if parts[-1] == "kr" and len(parts) >= 3 and parts[-2] in _KR_PUBLIC_SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def p(message: str = "") -> None:
    print(message, flush=True)


def percentiles(values: list) -> dict:
    """min/p25/median/p95/max over a list (empty-safe)."""
    if not values:
        return {"min": None, "p25": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def at(q: float):
        return ordered[min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))]

    return {"min": ordered[0], "p25": at(0.25), "p50": at(0.50),
            "p95": at(0.95), "max": ordered[-1]}


def _fmt_dist(d: dict) -> str:
    return (f"min={d['min']} p25={d['p25']} median={d['p50']} "
            f"p95={d['p95']} max={d['max']}")


def collapse_stats(host_sets_by_cluster: dict) -> dict:
    """Step C, pure: per cluster, distinct hosts vs distinct registrable
    domains. Returns per-cluster rows + aggregate shrink stats."""
    rows = []
    for key, hosts in host_sets_by_cluster.items():
        hosts = {h for h in hosts if h}
        domains = {registrable_domain(h) for h in hosts}
        domains.discard("")
        if not hosts:
            continue
        rows.append({
            "key": key,
            "hosts": len(hosts),
            "registrable": len(domains) or len(hosts),
            "delta": len(hosts) - (len(domains) or len(hosts)),
        })
    affected = [r for r in rows if r["delta"] > 0]
    return {
        "rows": rows,
        "affected": len(affected),
        "affected_pct": (100.0 * len(affected) / len(rows)) if rows else 0.0,
        "delta_dist": percentiles([r["delta"] for r in rows]),
        "biggest": sorted(rows, key=lambda r: -r["delta"])[:10],
    }


def sample_clusters(clusters: list, top: int, rand_n: int, seed) -> tuple:
    """Top-N by outlet_count plus a seeded random draw from the rest.
    Random, not id-front — id-front samples biased earlier probes."""
    ranked = sorted(clusters, key=lambda c: -(c.get("outlet_count") or 0))
    top_clusters = ranked[:top]
    rest = ranked[top:]
    rng = random.Random(seed)
    random_clusters = rng.sample(rest, min(rand_n, len(rest)))
    return top_clusters, random_clusters


def member_nodes_by_cluster(graph: dict) -> dict:
    """Membership lives on NODES (same reconstruction api_server's
    _build_spread_indexes and the builder's lineage matcher use)."""
    members = defaultdict(list)
    for node in (graph or {}).get("nodes") or []:
        if node.get("cluster_id") is not None and node.get("id") is not None:
            members[node["cluster_id"]].append(node["id"])
    return members


def report_sampled(label: str, sampled: list, host_sets: dict,
                   top_hosts: int = 0) -> None:
    stored = [c.get("outlet_count") or 0 for c in sampled]
    reproduced = [len(host_sets.get(c.get("cluster_id"), ())) for c in sampled]
    deltas = [s - r for s, r in zip(stored, reproduced)]
    p(f"  [{label}] {len(sampled)} clusters")
    p(f"    stored outlet_count       : {_fmt_dist(percentiles(stored))}")
    p(f"    reproduced distinct hosts : {_fmt_dist(percentiles(reproduced))}")
    if any(deltas):
        p(f"    stored-vs-reproduced delta: {_fmt_dist(percentiles(deltas))}  "
          "(nonzero = reproduction imperfect — report, do not trust Step C on those)")
    ratios = []
    for c in sampled:
        near = c.get("near_anchor_outlet_count")
        total = c.get("outlet_count") or 0
        if isinstance(near, int) and total >= 2:
            ratios.append(round(near / total, 3))
    if ratios:
        p(f"    near-anchor share (near/total, outlet_count>=2): "
          f"{_fmt_dist(percentiles(ratios))}")
    for c in sampled[:top_hosts]:
        hosts = sorted(host_sets.get(c.get("cluster_id"), ()))
        p(f"    -- #{c.get('stable_id')} \"{(c.get('label_title') or '')[:60]}\"")
        p(f"       outlet_count={c.get('outlet_count')} "
          f"near_anchor={c.get('near_anchor_outlet_count')} "
          f"hosts({len(hosts)}): {', '.join(hosts)}")


def run_live(top: int, rand_n: int, seed, top_hosts: int) -> int:
    p("=== ECHO-INDEPENDENCE PROBE (READ-ONLY, SELECT-only) ===")
    p("  vocabulary: circulation/spread only — no truth/falsity implication.")

    import postgres_storage
    import sqlalchemy as sa
    from build_brainmap_graph import normalize_outlet_host
    from embed_backfill import build_embed_text
    from semantic_embeddings import hash_text_for_cache

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        return 0

    with engine.connect() as conn:
        graph_raw = conn.execute(sa.text(
            "SELECT graph_json FROM brainmap_graph ORDER BY id DESC LIMIT 1"
        )).scalar()
        if not graph_raw:
            p("No brainmap_graph row — nothing to measure.")
            return 0
        # One pass over row metadata to reproduce the builder's exact-dup-text
        # collapse (outlets_by_hash) so per-cluster HOST SETS (which the graph
        # does not store — it stores counts only) can be rebuilt faithfully.
        rows = conn.execute(sa.text(
            "SELECT id, title, claim_text, original_url FROM analysis_results"
        )).all()

    graph = json.loads(graph_raw)
    clusters = graph.get("clusters") or []
    nodes = graph.get("nodes") or []
    members = member_nodes_by_cluster(graph)

    # Reproduce outlets_by_hash verbatim (build_brainmap_graph.py:481-493).
    hash_by_id, outlets_by_hash = {}, {}
    for rid, title, claim, url in rows:
        text = build_embed_text(title, claim)
        if not text:
            continue
        text_hash = hash_text_for_cache(text)
        hash_by_id[rid] = text_hash
        host = normalize_outlet_host(url)
        if host:
            outlets_by_hash.setdefault(text_hash, set()).add(host)

    host_sets = {}  # cluster_id -> set of hosts
    for cid, node_ids in members.items():
        hosts = set()
        for nid in node_ids:
            hosts.update(outlets_by_hash.get(hash_by_id.get(nid), ()))
        host_sets[cid] = hosts

    singletons = sum(1 for n in nodes if n.get("cluster_id") is None)
    sub2 = sum(1 for c in clusters if (c.get("outlet_count") or 0) < 2)
    p("")
    p("=== 1. CORPUS ===")
    p(f"  corpus rows                      : {len(rows)}")
    p(f"  graph nodes (dup-texts collapsed): {len(nodes)}")
    p(f"  singleton nodes (cluster_id null): {singletons}  "
      f"({100.0 * singletons / len(nodes):.1f}% of nodes) — surface NOWHERE as clusters")
    p(f"  clusters (>1 node)               : {len(clusters)}")
    p(f"  clusters with outlet_count<2     : {sub2} — hidden by the display gates "
      "(api_server.py:1295,1352)")

    top_clusters, random_clusters = sample_clusters(clusters, top, rand_n, seed)
    p("")
    p(f"=== 2-5. SAMPLE (top {len(top_clusters)} by outlet_count "
      f"+ random {len(random_clusters)}, seed={seed}) ===")
    report_sampled(f"TOP {len(top_clusters)}", top_clusters, host_sets,
                   top_hosts=top_hosts)
    report_sampled(f"RANDOM {len(random_clusters)}", random_clusters, host_sets)

    p("")
    p("=== 6. HOST-COLLAPSE (registrable-domain, host-string evidence only) ===")
    sampled_ids = {c.get("cluster_id") for c in top_clusters + random_clusters}
    stats = collapse_stats({cid: host_sets.get(cid, set()) for cid in sampled_ids
                            if cid in host_sets})
    p(f"  clusters where the count shrinks : {stats['affected']} "
      f"({stats['affected_pct']:.1f}% of sampled)")
    p(f"  shrink amount (hosts - domains)  : {_fmt_dist(stats['delta_dist'])}")
    for r in stats["biggest"]:
        if r["delta"] > 0:
            p(f"    cluster {r['key']}: {r['hosts']} hosts -> {r['registrable']} registrable domains")

    p("")
    p("=== 7. WHAT THIS CANNOT PROVE ===")
    p("  Host strings cannot reveal common OWNERSHIP across different registrable")
    p("  domains. Any further collapse needs a curated, sourced ownership dataset —")
    p("  a separate data project. Nothing here asserts group membership.")
    return 0


def _selftest() -> int:
    """Offline: pure-logic checks. No DB, no network."""
    from build_brainmap_graph import normalize_outlet_host

    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # Reused normalizer behaves as documented (build_brainmap_graph.py:140-154).
    check("norm-mobile", normalize_outlet_host("https://m.khan.co.kr/a/1"), "khan.co.kr")
    check("norm-www", normalize_outlet_host("http://www.yna.co.kr/x"), "yna.co.kr")
    check("norm-empty", normalize_outlet_host(None), "")

    # Registrable-domain rule: mechanical eTLD+1, .kr public SLDs respected.
    check("reg-kr-sub", registrable_domain("news.example.co.kr"), "example.co.kr")
    check("reg-kr-flat", registrable_domain("example.co.kr"), "example.co.kr")
    check("reg-go-kr", registrable_domain("epeople.go.kr"), "epeople.go.kr")
    check("reg-com-sub", registrable_domain("biz.example.com"), "example.com")
    check("reg-plain-kr", registrable_domain("example.kr"), "example.kr")
    check("reg-garbage", registrable_domain(""), "")
    check("reg-no-dot", registrable_domain("localhost"), "")
    # ★Different registrable domains are NEVER merged (no ownership guessing).
    check("reg-no-ownership",
          registrable_domain("example.co.kr") == registrable_domain("example-tv.com"),
          False)

    check("pct-empty", percentiles([])["p50"], None)
    check("pct-basic", percentiles([1, 2, 3, 4, 5])["p50"], 3)
    check("pct-minmax", (percentiles([7])["min"], percentiles([7])["max"]), (7, 7))

    stats = collapse_stats({
        "a": {"news.foo.co.kr", "sports.foo.co.kr", "bar.com"},  # 3 hosts -> 2 domains
        "b": {"x.com", "y.com"},                                  # no shrink
        "c": set(),                                               # skipped
    })
    check("collapse-rows", len(stats["rows"]), 2)
    check("collapse-affected", stats["affected"], 1)
    row_a = next(r for r in stats["rows"] if r["key"] == "a")
    check("collapse-a", (row_a["hosts"], row_a["registrable"], row_a["delta"]), (3, 2, 1))

    clusters = [{"cluster_id": i, "outlet_count": i} for i in range(30)]
    top_clusters, random_clusters = sample_clusters(clusters, 5, 10, seed=42)
    check("sample-top", [c["outlet_count"] for c in top_clusters], [29, 28, 27, 26, 25])
    check("sample-rand-n", len(random_clusters), 10)
    check("sample-no-overlap",
          {c["cluster_id"] for c in top_clusters}
          & {c["cluster_id"] for c in random_clusters}, set())
    again = sample_clusters(clusters, 5, 10, seed=42)[1]
    check("sample-deterministic",
          [c["cluster_id"] for c in again],
          [c["cluster_id"] for c in random_clusters])

    graph = {"nodes": [
        {"id": 1, "cluster_id": 0}, {"id": 2, "cluster_id": 0},
        {"id": 3, "cluster_id": None}, {"id": 4, "cluster_id": 1},
    ]}
    members = member_nodes_by_cluster(graph)
    check("members", {k: sorted(v) for k, v in members.items()}, {0: [1, 2], 1: [4]})

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK (23 checks)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ECHO-INDEPENDENCE probe (read-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check")
    parser.add_argument("--top", type=int, default=20, help="top-N clusters by outlet_count")
    parser.add_argument("--random", type=int, default=200, help="random clusters beyond top-N")
    parser.add_argument("--seed", type=int, default=42, help="random-sample seed (reproducible)")
    parser.add_argument("--top-hosts", type=int, default=10,
                        help="how many top clusters print their full host list")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    return run_live(args.top, args.random, args.seed, args.top_hosts)


if __name__ == "__main__":
    raise SystemExit(main())
