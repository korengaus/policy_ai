# BRAINMAP 2c — compute + persist the brain-map cluster graph.
#
# Reads the title+claim embeddings (written by scripts/embed_backfill.py) from
# embedding_cache, builds the PROVEN 2c-experiment graph (cosine kNN k=10,
# edges kept at sim>=0.80, union-find connected components = clusters),
# computes numpy PCA-2D layout coords, picks degree-based cluster labels, and
# writes the WHOLE graph as ONE JSON row into the additive `brainmap_graph`
# table (self-created via CREATE TABLE IF NOT EXISTS — mirrors
# embedding_vectors' create-on-demand pattern; postgres_storage.py untouched,
# no Alembic). The /api/brainmap endpoint (2d) reads the newest row.
#
# ★RUN LOCATION: LOCAL machine or the Render Worker (BRAINMAP-OOM Slice 1:
# numpy is now in requirements.txt; blocked top-k + streaming vector load
# cut peak memory to ~330MB at 7,600 rows / ~620MB at 10k — the old
# argsort(-S) transients (~925MB on top of everything) OOM-killed the 2GB
# Worker. The single N×N float32 S matrix remains the dominant term
# (~1.6GB at 20k rows — revisit blocking the matmul then, which trades
# ~4e-7 sim bits and therefore possibly a few stable_ids).
# Point DATABASE_URL at the external Postgres and set USE_POSTGRES_WRITE=true.
# Takes ~30s for ~2.3k nodes.
#
# SAFETY:
#   * Writes ONLY the brainmap_graph table (additive metadata). NO
#     analysis_results write of any kind — verdict_label / policy_alert_level
#     / truth_claim / operator_review_required / score / has_genuine
#     untouched. Verdict-isolated.
#   * HONESTY BOUNDARY baked into the stored artifact: every cluster carries
#     kind="spread" and size_label="N개 매체 보도 중" (circulating across N
#     DISTINCT outlets — normalized original_url hosts, so one outlet
#     publishing twice is ONE 매체; falls back to the member-row count only
#     when no member URL is usable). Each cluster also carries a
#     rebuild-stable stable_id (short sha256 of the sorted member row ids)
#     alongside the positional cluster_id the frontend keys on.
#     NO 검증 / confirmed / verified vocabulary anywhere in the
#     generated labels — cluster size is SPREAD, never verification. A big
#     cluster of a false rumor renders as "widely circulating" only.
#   * REUSES verbatim: build_embed_text (scripts/embed_backfill.py) and
#     hash_text_for_cache (semantic_embeddings.py) — the cache key is never
#     re-derived. The kNN/union-find logic is the proven 2c experiment logic.
#   * Idempotent: each run INSERTs a complete fresh row; the API reads the
#     newest. Old rows are free history/rollback.
#   * Fail-closed: real mode refuses without USE_POSTGRES_WRITE=true (so a
#     local run can't silently write SQLite) and without DATABASE_URL.
#   * Never prints DATABASE_URL or any API key.
#
# Offline logic check: --selftest (synthetic 8-d vectors, in-memory fake DB).
# Cost-free preview: --dry-run (full compute + shape stats, NO CREATE TABLE,
# NO INSERT).

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Project root importable when launched as `python scripts/build_brainmap_graph.py`
# (cwd=project root) without PYTHONPATH=. — mirrors embed_backfill.py. The
# scripts/ dir itself is sys.path[0] when run this way, so `from embed_backfill
# import build_embed_text` resolves too.
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


# ---------------------------------------------------------------------------
# Tunables — the 2c-RECHECK winning parameters. Change ONLY with a new
# measured experiment; params are stored in the artifact for auditability.
# ---------------------------------------------------------------------------
KNN_K = 10
SIM_THRESHOLD = 0.80
EMBED_PROVIDER = "openai"
EMBED_MODEL = "text-embedding-3-small"
# BRAINMAP-OOM Slice 1: top-k row-block size. Memory-only tunable — the old
# `np.argsort(-S)` added 2 extra N×N arrays (-S copy + int64 argsort output,
# ~693MB transient at 7,600 rows) on top of S and OOM-killed the 2GB Worker.
# The blocked top-k adds only block×N at a time. Any value >=1 produces the
# bit-IDENTICAL graph (the matmul itself is NOT blocked — see _knn_topk).
KNN_BLOCK_ROWS = 512

# Read-only corpus fetch. LOW-MEMORY: only the 6 metadata columns the graph
# needs — never source_candidates / debug_summary / any heavy blob.
# (original_url feeds the distinct-outlet count only; never stored verbatim.)
SELECT_ROWS_SQL = (
    "SELECT id, title, domain, content_nature, claim_text, original_url "
    "FROM analysis_results ORDER BY id"
)
# Read-only vector fetch (whole cache page for the provider+model; filtered
# in Python against the corpus keys — same approach the experiments proved).
SELECT_VECTORS_SQL = (
    "SELECT text_hash, vector_json FROM embedding_cache "
    "WHERE provider = %s AND model = %s"
)

# The ONLY write this script performs — an additive, self-created table.
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS brainmap_graph ("
    "id SERIAL PRIMARY KEY, "
    "generated_at TIMESTAMPTZ, "
    "params_json TEXT, "
    "graph_json TEXT)"
)
INSERT_SQL = (
    "INSERT INTO brainmap_graph (generated_at, params_json, graph_json) "
    "VALUES (%s, %s, %s)"
)

# Honesty boundary — vocabulary that must NEVER appear in generated labels.
# (Node titles are journalist-written passthrough data; the ban applies to
# every string THIS script generates: kind, size_label, params.)
FORBIDDEN_LABEL_VOCAB = ("검증", "confirmed", "verified")


def normalize_outlet_host(url):
    """Outlet identity for the distinct-매체 count: the normalized host of
    original_url. Lowercase netloc, credentials/port dropped, leading
    "www." / "m." stripped (m.khan.co.kr -> khan.co.kr, www.yna.co.kr ->
    yna.co.kr). Missing/unparseable URL -> "" — callers EXCLUDE empties, so
    a blank URL never counts as an outlet."""
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except ValueError:
        return ""
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    for prefix in ("www.", "m.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


# ---------------------------------------------------------------------------
# Union-find — ported verbatim from the proven 2c experiment.
# ---------------------------------------------------------------------------
class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _pca_2d(X):
    """numpy PCA-2D: center, top-2 SVD components, project, min-max normalize
    each axis to [0,1]. Deterministic. Degenerate axis (all-equal) -> 0.5."""
    import numpy as np

    centered = X - X.mean(axis=0, keepdims=True)
    # full_matrices=False keeps this cheap: (n,2) from (n,1536).
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    coords = centered @ vt[:2].T
    lo, hi = coords.min(axis=0), coords.max(axis=0)
    span = hi - lo
    out = np.full_like(coords, 0.5)
    for axis in range(coords.shape[1]):
        if span[axis] > 0:
            out[:, axis] = (coords[:, axis] - lo[axis]) / span[axis]
    return out


def _knn_topk(Xn, kk, block_rows=KNN_BLOCK_ROWS):
    """BRAINMAP-OOM Slice 1: memory-bounded top-k cosine neighbors.

    Xn is the (n, d) float32 row-normalized matrix. The similarity matrix is
    computed ONCE with the exact same full `Xn @ Xn.T` + fill_diagonal(-1.0)
    as the old code — BLAS produces different low-order float bits for a
    row-block matmul (measured ~4e-7), which would break bit-identity, so
    the matmul is deliberately NOT blocked. What IS blocked is the old OOM
    killer: `np.argsort(-S, axis=1)` materialized a full n×n negated copy
    PLUS a full n×n int64 argsort output (~693MB transient at 7,600 rows on
    top of S). Here each row block sorts only block_rows×n at a time; per
    row the full argsort (NOT argpartition) keeps tie order bit-identical.
    Returns (knn, sims) of shape (n, kk). Peak extra memory beyond S:
    block_rows×n float32 (-block) + block_rows×n int64 (argsort)."""
    import numpy as np

    n = Xn.shape[0]
    S = Xn @ Xn.T
    np.fill_diagonal(S, -1.0)
    knn = np.empty((n, kk), dtype=np.int64)
    sims = np.empty((n, kk), dtype=np.float32)
    for start in range(0, n, block_rows):
        stop = min(start + block_rows, n)
        block = S[start:stop]  # view — no copy
        order = np.argsort(-block, axis=1)[:, :kk]
        knn[start:stop] = order
        sims[start:stop] = np.take_along_axis(block, order, axis=1)
    return knn, sims


def build_graph(ids, titles, domains, content_natures, X,
                outlet_sets=None, k=KNN_K, sim_threshold=SIM_THRESHOLD):
    """Pure compute: kNN(k) cosine graph -> edges at sim>=threshold ->
    union-find components -> PCA-2D coords -> degree-based cluster labels.
    Returns the graph dict (without generated_at/params — main adds those).
    `X` is an already-loaded (n, dims) float matrix aligned with `ids`.
    `outlet_sets` (optional) is a per-node set of normalized outlet hosts
    aligned with `ids` — feeds the DISTINCT-outlet size_label; when absent
    the label falls back to the member-row count (legacy behavior)."""
    import numpy as np

    n = len(ids)
    Xn = np.asarray(X, dtype=np.float32)
    Xn = Xn / (np.linalg.norm(Xn, axis=1, keepdims=True) + 1e-12)
    kk = max(0, min(k, n - 1))
    # BRAINMAP-OOM Slice 1: chunked kNN — same cosine sims, same full-argsort
    # top-k (incl. tie order), same self-mask (-1.0 on the own index) as the
    # old full-matrix code, computed in row blocks so peak memory is block×N
    # instead of 3×N×N. Bit-identity is pinned by tests/test_brainmap_knn_
    # chunked.py against a full-matrix reference.
    knn, sims = _knn_topk(Xn, kk)

    uf = _UnionFind(n)
    edge_set = {}
    for i in range(n):
        for col in range(kk):
            j = int(knn[i, col])
            if sims[i, col] >= sim_threshold:
                uf.union(i, j)
                key = (i, j) if i < j else (j, i)
                edge_set.setdefault(key, float(sims[i, col]))

    degree = [0] * n
    for (i, j) in edge_set:
        degree[i] += 1
        degree[j] += 1

    # Components -> cluster ids (size>1 only, numbered by size desc; singletons
    # get cluster_id null). Deterministic ordering: size desc, then min row id.
    comp_members = {}
    for i in range(n):
        comp_members.setdefault(uf.find(i), []).append(i)
    multi = sorted(
        (members for members in comp_members.values() if len(members) > 1),
        key=lambda m: (-len(m), min(ids[i] for i in m)),
    )
    cluster_of = {}
    clusters = []
    for cluster_id, members in enumerate(multi):
        for i in members:
            cluster_of[i] = cluster_id
        # Label = highest-degree node's title (most kNN-connected = most
        # central phrasing); tie-break shortest title, then lowest row id.
        rep = min(members, key=lambda i: (-degree[i], len(titles[i] or ""), ids[i]))
        # Rebuild-stable identity: short sha256 of the sorted member row ids.
        # Same membership across rebuilds -> same stable_id. ADDITIVE — the
        # positional cluster_id above stays for the existing frontend contract.
        member_row_ids = sorted(ids[i] for i in members)
        stable_id = hashlib.sha256(
            ",".join(str(rid) for rid in member_row_ids).encode("utf-8")
        ).hexdigest()[:12]
        # HONESTY: "N개 매체" counts DISTINCT normalized outlets, not member
        # rows — one outlet publishing twice is ONE 매체. Blank hosts are
        # excluded; if no member has a usable URL, fall back to the member
        # count (the pre-existing behavior; never inflates vs. it).
        outlets = set()
        if outlet_sets:
            for i in members:
                outlets.update(outlet_sets[i] or ())
        outlets.discard("")
        outlet_count = len(outlets) or len(members)
        clusters.append({
            "cluster_id": cluster_id,
            "stable_id": stable_id,
            "label_title": titles[rep] or "",
            "size": len(members),
            "outlet_count": outlet_count,
            # HONESTY: spread ("circulating across N outlets"), never 검증.
            "size_label": "%d개 매체 보도 중" % outlet_count,
            "kind": "spread",
        })

    coords = _pca_2d(Xn)
    nodes = []
    for i in range(n):
        nodes.append({
            "id": ids[i],
            "title": titles[i] or "",
            "cluster_id": cluster_of.get(i),
            "x": round(float(coords[i, 0]), 4),
            "y": round(float(coords[i, 1]), 4),
            "domain": domains[i],
            "content_nature": content_natures[i],
        })
    edges = [
        {"src": ids[i], "dst": ids[j], "sim": round(sim, 4)}
        for (i, j), sim in sorted(edge_set.items())
    ]
    return {"nodes": nodes, "edges": edges, "clusters": clusters}


def load_corpus_vectors(conn):
    """Read-only: corpus rows + their title+claim vectors from embedding_cache.
    Key construction is REUSED verbatim (build_embed_text + hash_text_for_cache).
    Returns (ids, titles, domains, content_natures, vectors, outlet_sets,
    missing_count)."""
    from embed_backfill import build_embed_text
    from semantic_embeddings import hash_text_for_cache

    with conn.cursor() as cur:
        cur.execute(SELECT_ROWS_SQL)
        rows = cur.fetchall()
    wanted = {}  # text_hash -> first row index (exact-dup texts collapse)
    # text_hash -> outlet hosts across ALL rows sharing that text (dup-text
    # rows collapse to one node, but their outlets must still be counted —
    # a verbatim republication by a second outlet is a second 매체).
    outlets_by_hash = {}
    for i, (rid, title, domain, cn, claim, url) in enumerate(rows):
        text = build_embed_text(title, claim)
        if text:
            text_hash = hash_text_for_cache(text)
            wanted.setdefault(text_hash, i)
            host = normalize_outlet_host(url)
            if host:
                outlets_by_hash.setdefault(text_hash, set()).add(host)

    # BRAINMAP-OOM Slice 1: STREAM the vector page (fetchmany batches) and
    # parse each hit straight into a float32 array — the old fetchall() held
    # every vector_json string (~240MB) AND every parsed Python-float list
    # (~370MB at 7,600 rows) simultaneously. Same rows, same cursor order,
    # same first-hit-per-hash rule, same float64->float32 rounding as the old
    # np.asarray(list, float32) in build_graph — values are identical.
    import numpy as np

    vec_by_hash = {}
    with conn.cursor() as cur:
        cur.execute(SELECT_VECTORS_SQL, (EMBED_PROVIDER, EMBED_MODEL))
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                break
            for text_hash, vector_json in batch:
                if text_hash in wanted and text_hash not in vec_by_hash:
                    try:
                        vec = json.loads(vector_json)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(vec, list) and vec:
                        vec_by_hash[text_hash] = np.asarray(vec,
                                                            dtype=np.float32)

    ids, titles, domains, cns, vectors, outlet_sets = [], [], [], [], [], []
    for text_hash, i in wanted.items():
        vec = vec_by_hash.get(text_hash)
        if vec is None:
            continue
        rid, title, domain, cn, _claim, _url = rows[i]
        ids.append(rid)
        titles.append(title)
        domains.append(domain)
        cns.append(cn)
        vectors.append(vec)
        outlet_sets.append(outlets_by_hash.get(text_hash, set()))
    missing = len(wanted) - len(ids)
    print("[brainmap] corpus rows=%d unique texts=%d vectors resolved=%d missing=%d"
          % (len(rows), len(wanted), len(ids), missing))
    return ids, titles, domains, cns, vectors, outlet_sets, missing


def print_stats(graph):
    nodes, edges, clusters = graph["nodes"], graph["edges"], graph["clusters"]
    singletons = sum(1 for node in nodes if node["cluster_id"] is None)
    print("[brainmap] nodes=%d edges=%d clusters=%d singletons=%d largest=%d"
          % (len(nodes), len(edges), len(clusters), singletons,
             clusters[0]["size"] if clusters else 0))
    for cluster in clusters[:5]:
        print("  top: [%s] %s — %s"
              % (cluster["size_label"], (cluster["label_title"] or "")[:60],
                 cluster["kind"]))


def maybe_write(conn, generated_at, params, graph, dry_run):
    """The ONLY write path: CREATE TABLE IF NOT EXISTS brainmap_graph + INSERT
    one fresh row. Skipped entirely on --dry-run."""
    if dry_run:
        print("[brainmap] DRY-RUN — no CREATE TABLE, no INSERT.")
        return False
    payload = dict(graph)
    payload["generated_at"] = generated_at
    payload["params"] = params
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(INSERT_SQL, (
            generated_at,
            json.dumps(params, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
        ))
    conn.commit()
    print("[brainmap] wrote 1 brainmap_graph row (generated_at=%s)" % generated_at)
    return True


# ---------------------------------------------------------------------------
# OFFLINE SELFTEST — synthetic 8-d vectors, in-memory fake conn. No DB, no
# network, no embedding API. Exercises build_graph + maybe_write's dry-run.
# ---------------------------------------------------------------------------
class _FakeWriteConn:
    """Records every execute/commit so the selftest can assert dry-run purity."""

    def __init__(self):
        self.executed = []
        self.commits = 0

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                conn.executed.append(sql)

        return _Cur()

    def commit(self):
        self.commits += 1


def run_selftest() -> int:
    print("=== BUILD-BRAINMAP-GRAPH --selftest (offline; no DB, no network) ===")

    # 12 synthetic 8-d vectors: group A (4 near-identical around axis 0),
    # group B (3 around axis 1), group C (hub-and-spokes around axis 2 —
    # spokes similar to the HUB but NOT to each other, so degree picks the
    # hub), plus 2 far-off singletons (axes 4, 5).
    def unit(axis, extra=None, w=0.0):
        vec = [0.0] * 8
        vec[axis] = 1.0
        if extra is not None:
            vec[extra] = w
        return vec

    vectors = [
        unit(0), unit(0, 6, 0.05), unit(0, 7, 0.05), unit(0, 6, -0.05),   # A: ids 1-4
        unit(1), unit(1, 6, 0.05), unit(1, 7, 0.05),                      # B: ids 5-7
        unit(2),                    # C hub: id 8 (cos(hub,spoke)=0.82)
        unit(2, 3, 0.7),            # C spoke1: id 9 (cos(s1,s2)=0.34 < 0.80)
        unit(2, 3, -0.7),           # C spoke2: id 10
        unit(4), unit(5),           # singletons: ids 11, 12
    ]
    ids = list(range(1, 13))
    titles = ["A제목-%d" % i for i in (1, 2, 3, 4)] + \
             ["B제목-%d" % i for i in (1, 2, 3)] + \
             ["긴-허브-제목-이지만-최다연결", "짧은가지1", "짧은가지2"] + \
             ["외톨이1", "외톨이2"]
    domains = ["d%d" % (i % 3) for i in range(12)]
    cns = ["government_policy"] * 12
    # Outlet fixture: A carries a mobile+www pair of the SAME outlet plus a
    # blank URL -> 2 distinct outlets (not 4 rows); B has 3 distinct; C is
    # ONE outlet published thrice -> 1.
    urls = [
        "https://m.khan.co.kr/a", "https://www.khan.co.kr/b",
        "https://yna.co.kr/c", "",                                      # A
        "https://a.com/1", "https://b.com/2", "https://c.com/3",       # B
        "https://one.kr/hub", "https://one.kr/s1", "https://one.kr/s2",  # C
        "https://solo1.kr/x", "https://solo2.kr/y",                     # singletons
    ]
    outlet_sets = [{h for h in (normalize_outlet_host(u),) if h} for u in urls]

    graph = build_graph(ids, titles, domains, cns, vectors, outlet_sets, k=3)

    by_id = {node["id"]: node for node in graph["nodes"]}
    # (a) 3 clusters; the two far-off vectors stay singletons.
    cluster_a = {by_id[i]["cluster_id"] for i in (1, 2, 3, 4)}
    cluster_b = {by_id[i]["cluster_id"] for i in (5, 6, 7)}
    cluster_c = {by_id[i]["cluster_id"] for i in (8, 9, 10)}
    a_ok = (len(graph["clusters"]) == 3
            and len(cluster_a) == 1 and len(cluster_b) == 1 and len(cluster_c) == 1
            and len({tuple(cluster_a), tuple(cluster_b), tuple(cluster_c)}) == 3
            and by_id[11]["cluster_id"] is None and by_id[12]["cluster_id"] is None)
    print("  [%s] (a) 3 groups -> 3 clusters; far-off nodes stay singletons"
          % ("ok" if a_ok else "xx"))
    # (b) PCA coords all within [0,1].
    b_ok = all(0.0 <= node["x"] <= 1.0 and 0.0 <= node["y"] <= 1.0
               for node in graph["nodes"])
    print("  [%s] (b) PCA-2D coords normalized to [0,1]" % ("ok" if b_ok else "xx"))
    # (c) group C's label = the HUB's (highest-degree) title, though longest.
    c_meta = next(c for c in graph["clusters"]
                  if c["cluster_id"] == by_id[8]["cluster_id"])
    c_ok = c_meta["label_title"] == "긴-허브-제목-이지만-최다연결"
    print("  [%s] (c) cluster label = highest-degree node's title" % ("ok" if c_ok else "xx"))
    # (d) honest DISTINCT-outlet size label + kind on every cluster:
    # A = 4 rows but 2 outlets (m./www. same outlet + blank excluded),
    # B = 3 outlets, C = 3 rows but 1 outlet.
    a_meta = next(c for c in graph["clusters"]
                  if c["cluster_id"] == by_id[1]["cluster_id"])
    b_meta = next(c for c in graph["clusters"]
                  if c["cluster_id"] == by_id[5]["cluster_id"])
    d_ok = (all(c["size_label"] == "%d개 매체 보도 중" % c["outlet_count"]
                and c["kind"] == "spread" for c in graph["clusters"])
            and a_meta["size"] == 4 and a_meta["outlet_count"] == 2
            and b_meta["outlet_count"] == 3
            and c_meta["size"] == 3 and c_meta["outlet_count"] == 1)
    print("  [%s] (d) size_label counts DISTINCT outlets (A:4rows->2, C:3rows->1)"
          % ("ok" if d_ok else "xx"))
    # (e) --dry-run writes nothing.
    fake_conn = _FakeWriteConn()
    wrote = maybe_write(fake_conn, "2000-01-01T00:00:00+00:00",
                        {"k": KNN_K}, graph, dry_run=True)
    e_ok = (wrote is False and fake_conn.executed == [] and fake_conn.commits == 0)
    print("  [%s] (e) dry-run: no CREATE TABLE, no INSERT, no commit" % ("ok" if e_ok else "xx"))
    # (f) no 검증/confirmed/verified vocabulary in the artifact.
    blob = json.dumps(graph, ensure_ascii=False)
    f_ok = not any(word in blob for word in FORBIDDEN_LABEL_VOCAB)
    print("  [%s] (f) no 검증/confirmed/verified vocabulary in graph_json"
          % ("ok" if f_ok else "xx"))
    # (g) outlet normalization: m./www. collapse to the registrable host,
    # blank excluded.
    spec_urls = ["https://m.khan.co.kr/a", "https://www.khan.co.kr/b",
                 "https://yna.co.kr/c", ""]
    spec_hosts = {normalize_outlet_host(u) for u in spec_urls} - {""}
    g_ok = spec_hosts == {"khan.co.kr", "yna.co.kr"}
    print("  [%s] (g) normalize_outlet_host: m./www. -> canonical, blank excluded"
          % ("ok" if g_ok else "xx"))
    # (h) stable_id: 12-hex sha256 of the sorted member row ids — identical
    # on a rebuild with the same membership (A = rows 1,2,3,4).
    graph2 = build_graph(ids, titles, domains, cns, vectors, outlet_sets, k=3)
    expect_a = hashlib.sha256(b"1,2,3,4").hexdigest()[:12]
    h_ok = (a_meta["stable_id"] == expect_a
            and all(len(c["stable_id"]) == 12 for c in graph["clusters"])
            and sorted(c["stable_id"] for c in graph["clusters"])
            == sorted(c["stable_id"] for c in graph2["clusters"]))
    print("  [%s] (h) stable_id = sha256(sorted member ids)[:12], rebuild-stable"
          % ("ok" if h_ok else "xx"))

    ok = all([a_ok, b_ok, c_ok, d_ok, e_ok, f_ok, g_ok, h_ok])
    print()
    print("SELFTEST: %s" % ("PASS (clusters + coords + degree label + distinct-"
                            "outlet label + dry-run purity + honesty vocab + "
                            "outlet normalization + stable_id)" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_brainmap_graph",
        description="Compute the brain-map cluster graph (title+claim kNN "
                    "k=10 sim>=0.80, union-find clusters, PCA-2D coords, "
                    "spread labels) and write ONE JSON row into the additive "
                    "brainmap_graph table.",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="Run the OFFLINE logic check (synthetic vectors, fake conn).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Full compute + shape stats; NO CREATE TABLE, NO INSERT.")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    # --- Env guards: NO DB connect when preconditions fail. -------------------
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        print("DATABASE_URL not set — run LOCALLY with it pointed at the "
              "external Postgres (this script is local-only; numpy is not on "
              "the Render services).")
        return 0
    if not args.dry_run and os.environ.get("USE_POSTGRES_WRITE", "").strip().lower() != "true":
        print("USE_POSTGRES_WRITE is not 'true' — refusing to write. Set it "
              "true so the graph row goes to the shared Postgres (never a "
              "silent SQLite fallback), or use --dry-run.")
        return 0
    try:
        import numpy  # noqa: F401 — local-only dep; fail early with guidance
    except ImportError:
        print("numpy not installed — this script runs on Joe's LOCAL machine "
              "(pip install numpy); it is deliberately NOT in requirements.txt.")
        return 1

    import psycopg

    url = (raw_url.replace("postgresql+psycopg://", "postgresql://")
                  .replace("postgresql+psycopg2://", "postgresql://"))
    generated_at = datetime.now(timezone.utc).isoformat()
    params = {"k": KNN_K, "sim": SIM_THRESHOLD, "embed": "title+claim",
              "provider": EMBED_PROVIDER, "model": EMBED_MODEL}
    print("BUILD-BRAINMAP-GRAPH — k=%d sim>=%.2f embed=title+claim (%s/%s)"
          % (KNN_K, SIM_THRESHOLD, EMBED_PROVIDER, EMBED_MODEL))
    with psycopg.connect(url) as conn:
        (ids, titles, domains, cns, vectors, outlet_sets,
         _missing) = load_corpus_vectors(conn)
        if not ids:
            print("[brainmap] no vectors resolved — run scripts/embed_backfill.py first.")
            return 1
        graph = build_graph(ids, titles, domains, cns, vectors, outlet_sets)
        print_stats(graph)
        # Honesty guard at write time too: refuse to persist if any GENERATED
        # label string carries verdict vocabulary (titles are passthrough).
        generated_strings = [c["size_label"] + c["kind"] for c in graph["clusters"]]
        if any(word in s for s in generated_strings for word in FORBIDDEN_LABEL_VOCAB):
            print("[brainmap] HONESTY GUARD tripped — generated labels carry "
                  "verdict vocabulary; refusing to write.")
            return 1
        maybe_write(conn, generated_at, params, graph, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
