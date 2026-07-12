"""BRAINMAP-OOM Slice 1 — bit-identity proof for the chunked top-k.

The OOM fix keeps the single full-matrix matmul (blocking the matmul itself
changes low-order BLAS float bits — measured ~4e-7 — so it would NOT be
bit-identical) and replaces the full `np.argsort(-S)` (an n×n -S copy plus
an n×n int64 output) with a row-block top-k loop (_knn_topk). This test
keeps a copy of the OLD full-matrix logic as the REFERENCE and asserts the
chunked path produces IDENTICAL results on the same synthetic vectors:
  * identical sims + neighbor indices (exact float32 equality — same ops),
  * identical edge sets (same sim>=threshold gate),
  * identical union-find clusters and stable_ids end-to-end via build_graph,
  * identity holds across block sizes (including block smaller than k and
    block > n) and on an exact-duplicate-vector tie case.
Also: the streaming vector loader (fetchmany + per-row float32) must produce
the same matrix as the old fetchall + Python-list path. No DB, no network.
"""

import hashlib
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
_SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import build_brainmap_graph as bbg  # noqa: E402


# ---------------------------------------------------------------------------
# REFERENCE — the old full-matrix kNN, copied verbatim from the pre-fix
# build_graph (S + fill_diagonal + full argsort + S-indexed edge loop).
# ---------------------------------------------------------------------------
def reference_full_matrix(Xn, kk, sim_threshold):
    S = Xn @ Xn.T
    np.fill_diagonal(S, -1.0)
    knn = np.argsort(-S, axis=1)[:, :kk]
    n = Xn.shape[0]
    uf = bbg._UnionFind(n)
    edge_set = {}
    for i in range(n):
        for j in knn[i]:
            j = int(j)
            if S[i, j] >= sim_threshold:
                uf.union(i, j)
                key = (i, j) if i < j else (j, i)
                edge_set.setdefault(key, float(S[i, j]))
    sims = np.take_along_axis(S, knn, axis=1)
    components = {}
    for i in range(n):
        components.setdefault(uf.find(i), []).append(i)
    clusters = frozenset(frozenset(m) for m in components.values() if len(m) > 1)
    return knn, sims.astype(np.float32), edge_set, clusters


def chunked_edges_and_clusters(Xn, kk, sim_threshold, block_rows):
    """The NEW path's edge loop, fed by _knn_topk (mirrors build_graph)."""
    n = Xn.shape[0]
    knn, sims = bbg._knn_topk(Xn, kk, block_rows=block_rows)
    uf = bbg._UnionFind(n)
    edge_set = {}
    for i in range(n):
        for col in range(kk):
            j = int(knn[i, col])
            if sims[i, col] >= sim_threshold:
                uf.union(i, j)
                key = (i, j) if i < j else (j, i)
                edge_set.setdefault(key, float(sims[i, col]))
    components = {}
    for i in range(n):
        components.setdefault(uf.find(i), []).append(i)
    clusters = frozenset(frozenset(m) for m in components.values() if len(m) > 1)
    return knn, sims, edge_set, clusters


def make_clustered_vectors(n=300, dim=32, groups=6, seed=7):
    """Synthetic corpus: `groups` tight noise-clusters (high intra-sim, so
    the 0.80 gate fires) + a spread of loose outliers, row-normalized f32."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(groups, dim))
    rows = []
    for i in range(n):
        if i < n * 4 // 5:
            base = centers[i % groups]
            rows.append(base + rng.normal(scale=0.05, size=dim))
        else:
            rows.append(rng.normal(size=dim))
    X = np.asarray(rows, dtype=np.float32)
    return X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)


class ChunkedKnnBitIdentityTests(unittest.TestCase):
    def assert_identical(self, Xn, kk, threshold, block_rows):
        ref_knn, ref_sims, ref_edges, ref_clusters = reference_full_matrix(
            Xn, kk, threshold)
        new_knn, new_sims, new_edges, new_clusters = chunked_edges_and_clusters(
            Xn, kk, threshold, block_rows)
        self.assertTrue(np.array_equal(ref_knn, new_knn),
                        "neighbor indices differ (incl. tie order)")
        self.assertTrue(np.array_equal(ref_sims, new_sims),
                        "similarities differ (expected exact f32 equality)")
        self.assertEqual(ref_edges, new_edges)
        self.assertEqual(ref_clusters, new_clusters)

    def test_identical_on_clustered_corpus(self):
        Xn = make_clustered_vectors()
        self.assert_identical(Xn, kk=10, threshold=0.80, block_rows=64)

    def test_identical_across_block_sizes(self):
        Xn = make_clustered_vectors(n=137, seed=11)
        for block_rows in (1, 7, 64, 137, 500):  # incl. non-divisor, > n
            self.assert_identical(Xn, kk=10, threshold=0.80,
                                  block_rows=block_rows)

    def test_identical_with_exact_duplicate_ties(self):
        # Exact-duplicate vectors force sim==1.0 ties — the case where
        # argpartition would have broken tie order. Full argsort must match.
        base = make_clustered_vectors(n=40, seed=3)
        Xn = np.vstack([base, base[:10]])  # 10 exact duplicates
        self.assert_identical(Xn, kk=5, threshold=0.80, block_rows=16)

    def test_end_to_end_build_graph_matches_reference_clusters(self):
        # Same invariants through the REAL build_graph: cluster member sets
        # and stable_ids must equal the reference's union-find components.
        Xn = make_clustered_vectors(n=200, seed=23)
        ids = [1000 + i for i in range(Xn.shape[0])]
        titles = ["t%d" % i for i in range(Xn.shape[0])]
        domains = ["d"] * Xn.shape[0]
        cns = ["government_policy"] * Xn.shape[0]
        graph = bbg.build_graph(ids, titles, domains, cns, Xn, None,
                                k=10, sim_threshold=0.80)
        got_members = frozenset(
            frozenset(node["id"] for node in graph["nodes"]
                      if node["cluster_id"] == cluster["cluster_id"])
            for cluster in graph["clusters"])
        _, _, _, ref_clusters = reference_full_matrix(Xn, 10, 0.80)
        ref_members = frozenset(frozenset(ids[i] for i in m)
                                for m in ref_clusters)
        self.assertEqual(got_members, ref_members)
        for cluster in graph["clusters"]:
            member_ids = sorted(node["id"] for node in graph["nodes"]
                                if node["cluster_id"] == cluster["cluster_id"])
            expect = hashlib.sha256(
                ",".join(str(r) for r in member_ids).encode("utf-8")
            ).hexdigest()[:12]
            self.assertEqual(cluster["stable_id"], expect)


# ---------------------------------------------------------------------------
# Streaming loader vs fetchall reference.
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._pending = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "FROM analysis_results" in sql:
            self._pending = list(self._conn.rows)
        else:
            self._pending = list(self._conn.vector_rows)

    def fetchall(self):
        out, self._pending = self._pending, []
        return out

    def fetchmany(self, size):
        out, self._pending = self._pending[:size], self._pending[size:]
        return out


class _FakeConn:
    def __init__(self, rows, vector_rows):
        self.rows = rows
        self.vector_rows = vector_rows

    def cursor(self):
        return _FakeCursor(self)


class StreamingLoaderTests(unittest.TestCase):
    def test_streaming_loader_matches_fetchall_reference(self):
        import json as _json

        from embed_backfill import build_embed_text
        from semantic_embeddings import hash_text_for_cache

        rng = np.random.default_rng(5)
        rows, vector_rows, expect = [], [], {}
        for i in range(1, 8):
            title, claim = "제목-%d" % i, "주장-%d" % i
            text_hash = hash_text_for_cache(build_embed_text(title, claim))
            vec = rng.normal(size=16).tolist()
            rows.append((i, title, "d", "government_policy", claim,
                         "https://o%d.kr/x" % i))
            if i != 4:  # row 4 has NO cached vector -> counted missing
                vector_rows.append((text_hash, _json.dumps(vec)))
                expect[i] = np.asarray(vec, dtype=np.float32)
        vector_rows.append(("unrelated-hash", _json.dumps([0.1] * 16)))
        vector_rows.append((vector_rows[0][0], _json.dumps([9.9] * 16)))  # dup hash: first wins
        vector_rows.append(("bad-json-hash", "{not json"))

        (ids, titles, domains, cns, vectors, outlet_sets,
         missing) = bbg.load_corpus_vectors(_FakeConn(rows, vector_rows))

        self.assertEqual(ids, [1, 2, 3, 5, 6, 7])
        self.assertEqual(missing, 1)
        for rid, vec in zip(ids, vectors):
            self.assertEqual(vec.dtype, np.float32)
            self.assertTrue(np.array_equal(vec, expect[rid]))
        # First-hit-per-hash: row 1 kept its ORIGINAL vector, not the 9.9 dup.
        self.assertTrue(np.array_equal(vectors[0], expect[1]))
        self.assertEqual(titles[0], "제목-1")
        self.assertEqual(outlet_sets[0], {"o1.kr"})


if __name__ == "__main__":
    unittest.main()
