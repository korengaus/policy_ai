"""ECHO-CHAMBER PROBE — READ-ONLY, SELECT-only measurement of reprint inflation and
whether a meaningful DISTINCT-OUTLET (independent-source) count exists today.

MEASUREMENT ONLY. Every DB statement is a SELECT; no INSERT / UPDATE / DELETE / ALTER /
commit. Touches no pipeline, verdict, honesty, or display code. Prints numbers only.
Same discipline as scripts/relevance_gate_probe.py.

THE TWO QUESTIONS
-----------------
(1) ECHO CHAMBER — cluster significance is inflated by reprints: 50 outlets copying one
    wire story is 50 spread but nowhere near 50 independent sources. How bad is it?
(2) FEATURE GATE — the deferred SCORE-CLARITY idea of showing an independent-source
    count next to 근거 수준 is only worth building if a meaningful count exists for
    ENOUGH cards, INCLUDING singletons. The count matters most on a 1-source card, so a
    feature that is absent exactly there is the some-cards-only inconsistency we already
    hit twice (CLAIM-DISPLAY-2). This probe answers that BEFORE we build.

WHERE THE DATA LIVES (confirmed against the write/read paths)
-------------------------------------------------------------
  * Postgres table brainmap_graph(id, generated_at, params_json, graph_json), created and
    written ONLY by scripts/build_brainmap_graph.py:106-116, :436-454. No JSON file on
    disk. Canonical read = newest row: api_server.py:760-773 does exactly
    SELECT graph_json FROM brainmap_graph ORDER BY id DESC LIMIT 1.
  * graph_json = {nodes, edges, clusters, generated_at, params}.
  * Per-CLUSTER keys (build_brainmap_graph.py:321-335): cluster_id, stable_id,
    label_title, size (member ROW count), outlet_count (DISTINCT normalized hosts),
    near_anchor_outlet_count (distinct outlets >=0.95 cosine to the earliest report, i.e.
    syndicated reprints), exact_same_text_outlet_count, anchor_analysis_id,
    syndication_sim_threshold, size_label, kind, syndication_framing.
  * >>> There is NO members key and NO hosts key on a cluster record. Per-member hosts are
    NOT persisted (build_brainmap_graph.py:93 — "original_url feeds the distinct-outlet
    count only; never stored verbatim"). Membership lives on NODES:
    nodes[i] = {id, title, cluster_id, x, y, domain, content_nature}, so the member->row
    mapping IS recoverable and hosts can be RE-DERIVED by joining node.id to
    analysis_results.original_url (column at postgres_storage.py:231).

THREE MEASUREMENT CAVEATS, STATED UP FRONT
-------------------------------------------
  (a) DEDUPE UNDERCOUNT. Nodes are deduped by embed-text hash
      (build_brainmap_graph.py:376): exact-duplicate title+claim rows collapse into ONE
      node keeping only the first row id, though their outlets were already unioned into
      the persisted outlet_count. So a host count RE-DERIVED from node ids is <= the
      stored outlet_count. This probe reports BOTH and their delta rather than pretending
      they agree.
  (b) SINGLETONS ARE NOT CLUSTERS. build_brainmap_graph.py:273-276 keeps only
      len(members) > 1, so size-1 components have no cluster record; their nodes carry
      cluster_id None. Separately, api_server.py:1152 gates SERVING on outlet_count >= 2,
      so a 3-row cluster all from one host exists in storage but renders nothing. These
      are two DIFFERENT exclusions and are counted separately below.
  (c) HOST != OWNER. normalize_outlet_host collapses m./www. but knows nothing about
      ownership. Yonhap -> member papers, or a chaebol media group, counts as N
      independent outlets when it is arguably one. There is NO ownership map in this repo,
      so any "independent source" claim carries this error. Section 5 estimates its size
      using a wire-service presence heuristic and does NOT pretend that is a real fix.

WHAT IT PRINTS
--------------
  0. FRESHNESS: newest brainmap_graph id + generated_at. The weekly spine (render.yaml
     cron, Sundays 19:00 UTC) rebuilds it, is stop-on-failure and has a DB-size skip, so
     the graph can silently be weeks stale. Read this before trusting anything below.
  1. CLUSTER DISTRIBUTIONS: histograms of (a) size = raw member rows, (b) outlet_count =
     distinct hosts, (c) independent = outlet_count - near_anchor_outlet_count.
  2. DIVERGENCE / REPRINT INFLATION: how often the three disagree, and by how much.
  3. SINGLETON COVERAGE: what fraction of stored rows are in NO multi-member cluster, and
     what fraction of clusters are hidden by the serve-time outlet_count >= 2 gate.
  4. RE-DERIVED HOSTS: distinct hosts per cluster recomputed from analysis_results
     .original_url, compared against the stored outlet_count (the (a) caveat, measured).
  5. HOST-vs-OWNER: share of clusters containing a known wire host alongside others.
  6. STAKES: on how many DISPLAYED cards would "N개 서로 다른 매체" actually appear vs be
     absent -> is this a consistent feature or a some-cards-only feature?

SAFETY: SELECT-only, engine.connect() (never begin()), no commit. Lazy DB import so
--selftest is fully offline. UTF-8 guarded prints.

Usage (in the Render Worker Shell, after commit + push + Worker redeploy + reopen Shell):
    PYTHONPATH=. python scripts/echo_chamber_probe.py
    PYTHONPATH=. python scripts/echo_chamber_probe.py --selftest    # offline, no DB
    PYTHONPATH=. python scripts/echo_chamber_probe.py --no-hosts    # skip section 4 join

Exit codes: 0 = report printed / graph unavailable / selftest passed; 1 = selftest failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def normalize_outlet_host(url):
    """VERBATIM copy of build_brainmap_graph.py:135-149 so this probe produces
    bit-identical outlet identity without importing the builder (which owns the only
    write path in the codebase — not something a read-only probe should import).
    _selftest asserts this stays in sync with the original."""
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except ValueError:
        return ""
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    for prefix in ("www.", "m.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


# Korean wire services. Presence of one of these in a cluster alongside other hosts is a
# WEAK signal that the "independent" outlets are carrying the same wire copy. This is a
# heuristic for SIZING the host!=owner error, NOT an ownership map and NOT a gate.
WIRE_HOSTS = {
    "yna.co.kr",
    "yonhapnews.co.kr",
    "newsis.com",
    "news1.kr",
    "newspim.com",
    "edaily.co.kr",
}

# Heavy-syndication thresholds reported in section 2.
HEAVY_FRACTIONS = (0.5, 0.8, 1.0)


def p(message: str = "") -> None:
    print(message, flush=True)


def _int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def independent_estimate(cluster: dict) -> int:
    """outlet_count - near_anchor_outlet_count, floored at 0. Both operands are already
    DISTINCT-outlet sets (build_brainmap_graph.py:296-320), so the difference is 'outlets
    whose phrasing was NOT near-identical to the earliest report'. Floor because the two
    counts are computed over different sets and can in principle cross."""
    return max(0, _int(cluster.get("outlet_count")) - _int(cluster.get("near_anchor_outlet_count")))


def count_bucket(value: int) -> str:
    """Log-ish buckets — cluster sizes are heavily skewed, so linear bins hide the tail."""
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 2:
        return "2"
    if value <= 4:
        return "3-4"
    if value <= 9:
        return "5-9"
    if value <= 19:
        return "10-19"
    if value <= 49:
        return "20-49"
    return "50+"


_BUCKET_ORDER = ["0", "1", "2", "3-4", "5-9", "10-19", "20-49", "50+"]


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "  n/a"


def _histogram(title: str, counter: Counter, total: int) -> None:
    p(f"    {title}")
    for bucket in _BUCKET_ORDER:
        if counter.get(bucket):
            p(f"      {bucket:<8}{counter[bucket]:>7}   {_pct(counter[bucket], total)}")


def analyze_clusters(clusters: list) -> dict:
    """Pure: cluster-level distributions + divergence tallies."""
    size_hist = Counter()
    outlet_hist = Counter()
    indep_hist = Counter()
    inflated_rows = 0        # outlet_count < size  (raw rows overcount distinct outlets)
    inflated_amount = 0
    heavy = {f: 0 for f in HEAVY_FRACTIONS}
    indep_lt2 = 0            # would show no meaningful "서로 다른 매체" number
    served = 0               # passes the api_server.py:1152 outlet_count >= 2 gate
    rows_in_clusters = 0
    rows_in_served = 0

    for cluster in clusters:
        size = _int(cluster.get("size"))
        outlets = _int(cluster.get("outlet_count"))
        near = _int(cluster.get("near_anchor_outlet_count"))
        indep = independent_estimate(cluster)

        size_hist[count_bucket(size)] += 1
        outlet_hist[count_bucket(outlets)] += 1
        indep_hist[count_bucket(indep)] += 1
        rows_in_clusters += size

        if outlets < size:
            inflated_rows += 1
            inflated_amount += size - outlets
        if outlets > 0:
            for fraction in HEAVY_FRACTIONS:
                if near >= fraction * outlets:
                    heavy[fraction] += 1
        if indep < 2:
            indep_lt2 += 1
        if outlets >= 2:
            served += 1
            rows_in_served += size

    return {
        "clusters": len(clusters),
        "size_hist": size_hist,
        "outlet_hist": outlet_hist,
        "indep_hist": indep_hist,
        "inflated_rows": inflated_rows,
        "inflated_amount": inflated_amount,
        "heavy": heavy,
        "indep_lt2": indep_lt2,
        "served": served,
        "rows_in_clusters": rows_in_clusters,
        "rows_in_served": rows_in_served,
    }


def analyze_nodes(nodes: list) -> dict:
    """Pure: node-level membership. cluster_id None == singleton (no multi-member
    cluster), per the len(members) > 1 filter at build_brainmap_graph.py:273-276."""
    clustered_ids = set()
    singleton_ids = set()
    members_by_cluster = defaultdict(list)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if node.get("cluster_id") is None:
            singleton_ids.add(node_id)
        else:
            clustered_ids.add(node_id)
            members_by_cluster[node["cluster_id"]].append(node_id)
    return {
        "nodes": len(nodes),
        "clustered_ids": clustered_ids,
        "singleton_ids": singleton_ids,
        "members_by_cluster": members_by_cluster,
    }


def report_clusters(stats: dict) -> None:
    total = stats["clusters"]
    p("")
    p("=== 1. CLUSTER DISTRIBUTIONS ===")
    p(f"  multi-member clusters in graph: {total}")
    p("")
    _histogram("(a) size  = raw member ROWS", stats["size_hist"], total)
    p("")
    _histogram("(b) outlet_count = DISTINCT hosts", stats["outlet_hist"], total)
    p("")
    _histogram("(c) independent = outlet_count - near_anchor_outlet_count",
               stats["indep_hist"], total)

    p("")
    p("=== 2. DIVERGENCE / REPRINT INFLATION ===")
    p(f"  clusters where outlet_count < size : {stats['inflated_rows']}  ({_pct(stats['inflated_rows'], total)})")
    p(f"    ^ raw member rows overcount distinct outlets; total excess rows = {stats['inflated_amount']}")
    for fraction in HEAVY_FRACTIONS:
        label = f"near_anchor >= {int(fraction * 100)}% of outlets"
        p(f"  {label:<36}: {stats['heavy'][fraction]:>6}  ({_pct(stats['heavy'][fraction], total)})")
    p("    ^ THIS is the echo-chamber magnitude: outlets whose phrasing is near-identical")
    p("      to the earliest report, i.e. carrying the same copy rather than reporting.")
    p(f"  clusters with independent < 2      : {stats['indep_lt2']}  ({_pct(stats['indep_lt2'], total)})")
    p("    ^ these could NOT honestly show a '서로 다른 매체 N' number above 1.")


def report_coverage(stats: dict, node_stats: dict, row_total: int) -> None:
    clustered = len(node_stats["clustered_ids"])
    singleton = len(node_stats["singleton_ids"])
    absent = max(0, row_total - node_stats["nodes"])

    p("")
    p("=== 3. SINGLETON / COVERAGE ===")
    p(f"  analysis_results rows total         : {row_total}")
    p(f"  rows present as graph nodes         : {node_stats['nodes']}  ({_pct(node_stats['nodes'], row_total)})")
    p(f"    of which IN a multi-member cluster: {clustered}  ({_pct(clustered, row_total)} of ALL rows)")
    p(f"    of which SINGLETON (cluster_id N) : {singleton}  ({_pct(singleton, row_total)} of ALL rows)")
    p(f"  rows ABSENT from the graph entirely : {absent}  ({_pct(absent, row_total)} of ALL rows)")
    p("    ^ never embedded, or collapsed by the exact-duplicate-text dedupe")
    p("      (build_brainmap_graph.py:376). Not the same population as singletons.")
    p("")
    p(f"  clusters passing the SERVE gate outlet_count>=2 : {stats['served']}  ({_pct(stats['served'], stats['clusters'])} of clusters)")
    p(f"  member rows inside those served clusters        : {stats['rows_in_served']}  ({_pct(stats['rows_in_served'], row_total)} of ALL rows)")
    p("    ^ api_server.py:1152. A multi-row cluster confined to ONE host exists in")
    p("      storage but renders nothing — a separate exclusion from singletons.")


def report_hosts(host_stats: dict) -> None:
    p("")
    p("=== 4. RE-DERIVED HOSTS vs STORED outlet_count ===")
    if not host_stats:
        p("    (skipped)")
        return
    compared = host_stats["compared"]
    p(f"  clusters compared                   : {compared}")
    p(f"    re-derived == stored              : {host_stats['equal']}  ({_pct(host_stats['equal'], compared)})")
    p(f"    re-derived <  stored (dedupe loss): {host_stats['under']}  ({_pct(host_stats['under'], compared)})")
    p(f"    re-derived >  stored (unexpected) : {host_stats['over']}  ({_pct(host_stats['over'], compared)})")
    p(f"  total re-derived host shortfall     : {host_stats['shortfall']}")
    p("    ^ expected to be > 0: exact-duplicate-text rows were collapsed out of nodes")
    p("      BEFORE persistence, but their outlets were already unioned into the stored")
    p("      count. A node-join can never fully reproduce it. Size the gap, don't fix it.")
    p(f"  rows with blank/unparseable host    : {host_stats['blank_hosts']}")
    p("    ^ these never count as an outlet (normalize_outlet_host returns ''), but the")
    p("      builder falls back to len(members) when NO member has a usable URL")
    p("      (build_brainmap_graph.py:301) — that path silently labels a ROW count as")
    p("      an outlet count. Rows below show how exposed we are to it.")
    p(f"  clusters where EVERY member host is blank : {host_stats['all_blank_clusters']}")

    p("")
    p("=== 5. HOST vs OWNER (the honesty limit on 'independent') ===")
    p(f"  clusters containing a known wire host + >=1 other : {host_stats['wire_mixed']}  ({_pct(host_stats['wire_mixed'], compared)})")
    p(f"  clusters that are wire-host ONLY                  : {host_stats['wire_only']}")
    p("  Top hosts by cluster participation:")
    for host, count in host_stats["top_hosts"]:
        p(f"    {host:<28}{count:>6}")
    p("  CAVEAT: there is NO ownership map in this repo. normalize_outlet_host collapses")
    p("  m./www. and nothing else, so Yonhap -> member papers, or two titles under one")
    p("  media group, count as independent outlets. The wire-mixed % above is a LOWER")
    p("  BOUND on that error, not a measurement of it. Any UI string must therefore say")
    p("  '서로 다른 매체' (different outlets), never '독립 매체' (independent outlets).")


def report_stakes(stats: dict, node_stats: dict, row_total: int) -> None:
    shown = stats["rows_in_served"]
    p("")
    p("=== 6. STAKES for 'N개 서로 다른 매체' on the card ===")
    p(f"  cards where a count >=2 could appear : {shown}  ({_pct(shown, row_total)} of ALL rows)")
    p(f"  cards where it would be ABSENT       : {row_total - shown}  ({_pct(row_total - shown, row_total)} of ALL rows)")
    p("")
    p("  READ THIS BEFORE BUILDING:")
    p("  The count is absent exactly on singleton cards — the 1-source cards where a")
    p("  reader most needs to know the claim rests on ONE outlet. If the absent share")
    p("  above is large, shipping this produces a some-cards-only feature: the same")
    p("  inconsistency complaint that started CLAIM-DISPLAY-2. Two honest ways out:")
    p("    (i)  render an explicit '1개 매체' on singletons instead of nothing, which")
    p("         requires a per-row outlet identity that does NOT depend on the graph;")
    p("    (ii) do not ship the count until singleton coverage is solved.")
    p("  Decide from the numbers above, not from the cluster-side numbers alone.")


def run_live(do_hosts: bool, top_n: int) -> int:
    p("=== ECHO-CHAMBER PROBE (READ-ONLY, SELECT-only) ===")

    import postgres_storage
    import sqlalchemy as sa

    engine = postgres_storage.get_engine()
    if engine is None:
        p("Engine unavailable - set USE_POSTGRES_WRITE=true and DATABASE_URL.")
        p("(Run --selftest for the offline logic check.)")
        return 0

    with engine.connect() as conn:
        try:
            head = conn.execute(
                sa.text("SELECT id, generated_at FROM brainmap_graph ORDER BY id DESC LIMIT 1")
            ).first()
        except Exception as exc:
            p(f"brainmap_graph unreadable ({exc}). Has scripts/build_brainmap_graph.py run?")
            return 0
        if not head:
            p("brainmap_graph is empty - the weekly spine has not built a graph yet.")
            return 0

        graph_id, generated_at = head[0], head[1]
        p("")
        p("=== 0. FRESHNESS ===")
        p(f"  newest brainmap_graph id : {graph_id}")
        p(f"  generated_at             : {generated_at}")
        p("  ^ rebuilt by the weekly spine (render.yaml cron, Sundays 19:00 UTC). That")
        p("    spine is stop-on-failure and skips entirely near the DB size cap, so a")
        p("    stale row can serve for weeks. If this date is old, every number below")
        p("    describes the corpus as of THAT date, not today.")

        raw = conn.execute(
            sa.text("SELECT graph_json FROM brainmap_graph WHERE id = :i").bindparams(i=graph_id)
        ).scalar()
        graph = json.loads(raw) if raw else {}
        clusters = [c for c in (graph.get("clusters") or []) if isinstance(c, dict)]
        nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]

        stats = analyze_clusters(clusters)
        node_stats = analyze_nodes(nodes)
        row_total = _int(
            conn.execute(sa.text("SELECT count(*) FROM analysis_results")).scalar()
        )

        host_stats = {}
        if do_hosts and node_stats["members_by_cluster"]:
            host_stats = _rederive_hosts(conn, sa, clusters, node_stats, top_n)

    report_clusters(stats)
    report_coverage(stats, node_stats, row_total)
    report_hosts(host_stats)
    report_stakes(stats, node_stats, row_total)
    return 0


def _rederive_hosts(conn, sa, clusters: list, node_stats: dict, top_n: int) -> dict:
    """Join node ids -> analysis_results.original_url and re-apply normalize_outlet_host.
    Paged so we never load the whole corpus at once."""
    wanted = sorted(node_stats["clustered_ids"])
    url_by_id = {}
    page = 1000
    for start in range(0, len(wanted), page):
        chunk = wanted[start:start + page]
        rows = conn.execute(
            sa.text("SELECT id, original_url FROM analysis_results WHERE id = ANY(:ids)")
            .bindparams(sa.bindparam("ids", value=chunk))
        ).all()
        for row_id, url in rows:
            url_by_id[row_id] = url

    by_cluster_id = {c.get("cluster_id"): c for c in clusters}
    equal = under = over = shortfall = 0
    blank_hosts = all_blank_clusters = 0
    wire_mixed = wire_only = compared = 0
    host_participation = Counter()

    for cluster_id, member_ids in node_stats["members_by_cluster"].items():
        cluster = by_cluster_id.get(cluster_id)
        if not cluster:
            continue
        compared += 1
        hosts = set()
        for member_id in member_ids:
            host = normalize_outlet_host(url_by_id.get(member_id))
            if host:
                hosts.add(host)
            else:
                blank_hosts += 1
        if not hosts:
            all_blank_clusters += 1
        host_participation.update(hosts)

        stored = _int(cluster.get("outlet_count"))
        if len(hosts) == stored:
            equal += 1
        elif len(hosts) < stored:
            under += 1
            shortfall += stored - len(hosts)
        else:
            over += 1

        wire = hosts & WIRE_HOSTS
        if wire and hosts - WIRE_HOSTS:
            wire_mixed += 1
        elif wire and not (hosts - WIRE_HOSTS):
            wire_only += 1

    return {
        "compared": compared,
        "equal": equal,
        "under": under,
        "over": over,
        "shortfall": shortfall,
        "blank_hosts": blank_hosts,
        "all_blank_clusters": all_blank_clusters,
        "wire_mixed": wire_mixed,
        "wire_only": wire_only,
        "top_hosts": host_participation.most_common(top_n),
    }


def _selftest() -> int:
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # normalize_outlet_host must stay bit-identical to the builder's copy.
    check("host-www", normalize_outlet_host("https://www.yna.co.kr/view/1"), "yna.co.kr")
    check("host-m", normalize_outlet_host("https://m.khan.co.kr/a"), "khan.co.kr")
    check("host-port-creds", normalize_outlet_host("https://u:p@News.Example.com:8443/x"), "news.example.com")
    check("host-blank", normalize_outlet_host(""), "")
    check("host-none", normalize_outlet_host(None), "")
    try:
        from build_brainmap_graph import normalize_outlet_host as original  # type: ignore
    except Exception:
        try:
            sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
            from build_brainmap_graph import normalize_outlet_host as original  # type: ignore
        except Exception:
            original = None
    if original is not None:
        for url in ("https://www.yna.co.kr/x", "http://m.news1.kr/y", "", None, ":://bad"):
            check(f"host-parity[{url!r}]", normalize_outlet_host(url), original(url))
    else:
        p("  (parity check skipped: build_brainmap_graph not importable offline)")

    # independent estimate floors at 0 and never goes negative.
    check("indep-normal", independent_estimate({"outlet_count": 10, "near_anchor_outlet_count": 4}), 6)
    check("indep-floor", independent_estimate({"outlet_count": 3, "near_anchor_outlet_count": 9}), 0)
    check("indep-missing", independent_estimate({}), 0)

    check("bucket-0", count_bucket(0), "0")
    check("bucket-1", count_bucket(1), "1")
    check("bucket-4", count_bucket(4), "3-4")
    check("bucket-50", count_bucket(50), "50+")

    # Cluster analysis end-to-end: one heavily-syndicated cluster, one clean.
    stats = analyze_clusters([
        {"cluster_id": 0, "size": 50, "outlet_count": 20, "near_anchor_outlet_count": 19},
        {"cluster_id": 1, "size": 3, "outlet_count": 3, "near_anchor_outlet_count": 0},
        {"cluster_id": 2, "size": 4, "outlet_count": 1, "near_anchor_outlet_count": 0},
    ])
    check("clusters", stats["clusters"], 3)
    check("inflated-rows", stats["inflated_rows"], 2)          # clusters 0 and 2
    check("inflated-amount", stats["inflated_amount"], 33)     # 30 + 3
    check("heavy-80pct", stats["heavy"][0.8], 1)               # cluster 0 only
    # Clusters 0 AND 2 both land under 2: cluster 0 has 20 outlets but 19 of them are
    # near-anchor reprints, so its independent estimate collapses to 1. That collapse is
    # the whole point of the probe — a 50-row, 20-outlet cluster is ONE source.
    check("indep-lt2", stats["indep_lt2"], 2)
    check("served", stats["served"], 2)                        # clusters 0 and 1
    check("rows-served", stats["rows_in_served"], 53)

    # Node analysis: singleton vs clustered split.
    node_stats = analyze_nodes([
        {"id": 1, "cluster_id": 0},
        {"id": 2, "cluster_id": 0},
        {"id": 3, "cluster_id": None},
    ])
    check("clustered", sorted(node_stats["clustered_ids"]), [1, 2])
    check("singletons", sorted(node_stats["singleton_ids"]), [3])
    check("members-of-0", sorted(node_stats["members_by_cluster"][0]), [1, 2])

    if failures:
        for failure in failures:
            p(f"FAIL {failure}")
        return 1
    p("selftest OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ECHO-CHAMBER probe (SELECT-only).")
    parser.add_argument("--selftest", action="store_true", help="offline logic check, no DB")
    parser.add_argument("--no-hosts", action="store_true", help="skip the host re-derivation join")
    parser.add_argument("--top", type=int, default=15, help="top hosts to list")
    args = parser.parse_args()

    if args.selftest:
        return _selftest()
    return run_live(not args.no_hosts, args.top)


if __name__ == "__main__":
    raise SystemExit(main())
