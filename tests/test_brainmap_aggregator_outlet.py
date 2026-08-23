# AGGREGATOR-NOT-OUTLET (106-APPLY) — an aggregator (news.google.com, stored
# only when the collector's decoder fails) is a directory, not an outlet, and
# must never count as a 매체. Pins the outlet union exclusion, the floor-1
# rule for all-aggregator clusters (the member-count fallback would INFLATE),
# the untouched no-URL fallback, and the graph_exclusion SELECT filter.
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
_SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import numpy as np  # noqa: E402

import build_brainmap_graph as bbg  # noqa: E402


def _two_cluster_vectors(sizes):
    """Near-identical vectors per cluster, orthogonal across clusters."""
    rows = []
    for c, size in enumerate(sizes):
        base = np.zeros(len(sizes) + 1, dtype=np.float32)
        base[c] = 1.0
        for i in range(size):
            v = base.copy()
            v[-1] = 0.001 * i  # tiny wiggle, cosine ~1 within the cluster
            rows.append(v)
    return np.asarray(rows, dtype=np.float32)


def _build(outlet_sets, sizes=(3, 2)):
    n = sum(sizes)
    X = _two_cluster_vectors(sizes)
    ids = list(range(1, n + 1))
    titles = ["t%d" % i for i in ids]
    graph = bbg.build_graph(ids, titles, ["기타"] * n, ["etc"] * n, X,
                            outlet_sets=outlet_sets, k=2, sim_threshold=0.9,
                            fresh_layout=True)
    by_size = sorted(graph["clusters"], key=lambda c: -c["size"])
    return by_size


class AggregatorOutletTests(unittest.TestCase):
    def test_aggregator_host_never_counts_as_outlet(self):
        # 3-node cluster: two real outlets + one undecoded aggregator URL.
        clusters = _build([{"a.com"}, {"b.com"}, {"news.google.com"},
                           {"c.com"}, {"d.com"}])
        self.assertEqual(clusters[0]["outlet_count"], 2)
        self.assertEqual(clusters[0]["size_label"], "2개 매체 보도 중")

    def test_all_aggregator_cluster_floors_to_one(self):
        # Whole-cluster decode failure (the 2026-07-22 outage shape): three
        # rows behind google redirects are AT LEAST one carrier, never "3개
        # 매체" via the member-count fallback.
        clusters = _build([{"news.google.com"}, {"news.google.com"},
                           {"news.google.com"}, {"c.com"}, {"d.com"}])
        self.assertEqual(clusters[0]["outlet_count"], 1)

    def test_no_url_cluster_keeps_member_count_fallback(self):
        # Pre-existing behavior for rows with NO usable URL: unchanged.
        clusters = _build([set(), set(), set(), {"c.com"}, {"d.com"}])
        self.assertEqual(clusters[0]["outlet_count"], 3)

    def test_real_outlets_unaffected(self):
        clusters = _build([{"a.com"}, {"b.com"}, {"c.com"},
                           {"d.com"}, {"e.com"}])
        self.assertEqual(clusters[0]["outlet_count"], 3)

    def test_syndication_tiers_exclude_aggregator(self):
        clusters = _build([{"a.com"}, {"a.com"}, {"news.google.com"},
                           {"c.com"}, {"d.com"}])
        self.assertEqual(clusters[0]["near_anchor_outlet_count"], 1)
        self.assertLessEqual(clusters[0]["exact_same_text_outlet_count"], 1)

    def test_distinct_outlets_helper(self):
        self.assertEqual(bbg._distinct_outlets({"news.google.com", "a.com", ""}),
                         {"a.com"})
        self.assertEqual(bbg._distinct_outlets({"news.google.com"}), set())

    def test_normalize_outlet_host_unchanged(self):
        self.assertEqual(bbg.normalize_outlet_host("https://www.yna.co.kr/x"), "yna.co.kr")
        self.assertEqual(bbg.normalize_outlet_host("https://m.khan.co.kr/x"), "khan.co.kr")
        # the host itself still normalizes — exclusion happens at the union,
        # so the "aggregator seen" signal survives for the floor rule.
        self.assertEqual(bbg.normalize_outlet_host(
            "https://news.google.com/rss/articles/x"), "news.google.com")


class GraphExclusionSelectTests(unittest.TestCase):
    def test_select_skips_marked_rows(self):
        self.assertIn("WHERE graph_exclusion IS NULL", bbg.SELECT_ROWS_SQL)


if __name__ == "__main__":
    unittest.main()
