"""BUILD-BASIS (112-APPLY) — tests for the additive graph_generated_at field
on /api/claim, /api/cluster-spreads and /api/hero-pick.

Offline (the test_cluster_sizes_endpoint pattern): DB seams monkeypatched, no
Postgres, no network. The field's contract, each direction pinned:

  * PRESENT (top-level, the graph row's ISO generated_at string, verbatim)
    when the served data is the cached entry the timestamp was read with —
    pairing is by IDENTITY, so a timestamp can never describe someone else's
    counts;
  * ABSENT when the timestamp is unknown (old graph row without the key);
  * ABSENT when the served object is not the cache's (a patched seam);
  * every empty/error/found:false body stays byte-identical to before —
    additive means the old payloads did not move.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import api_server  # noqa: E402

TS = "2026-08-23T22:47:00+00:00"

FAKE_GRAPH = {
    "nodes": [
        {"id": 1, "cluster_id": 0, "title": "a"},
        {"id": 2, "cluster_id": 0, "title": "b"},
    ],
    "edges": [],
    "clusters": [
        {"cluster_id": 0, "stable_id": "abc123def456",
         "lineage_id": "abc123def456", "outlet_count": 3, "size": 2,
         "kind": "spread"},
    ],
}
FAKE_INDEXES = api_server._build_spread_indexes(FAKE_GRAPH)
FAKE_MEMBER_ROWS = [
    (1, "a", "https://x.example/1", "2026-08-01T09:00:00", "draft_needs_review",
     0.5, None),
    (2, "b", "https://y.example/2", "2026-08-03T10:00:00", "draft_needs_review",
     0.5, None),
]

_CACHE_KEYS = ("row_id", "indexes", "corpus", "generated_at")
_HERO_KEYS = ("row_id", "display", "urls", "hero", "generated_at")


class _CacheIsolationMixin:
    """Save/restore the module caches so these tests leak no state."""

    def setUp(self):
        self._spread_saved = {k: api_server._SPREAD_CACHE.get(k)
                              for k in _CACHE_KEYS}
        self._hero_saved = {k: api_server._TRENDING_DISPLAY_CACHE.get(k)
                            for k in _HERO_KEYS}

    def tearDown(self):
        api_server._SPREAD_CACHE.update(self._spread_saved)
        api_server._TRENDING_DISPLAY_CACHE.update(self._hero_saved)

    @property
    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)


class ClaimGraphGeneratedAtTests(_CacheIsolationMixin, unittest.TestCase):
    def _get_claim(self, generated_at):
        api_server._SPREAD_CACHE["indexes"] = FAKE_INDEXES
        api_server._SPREAD_CACHE["generated_at"] = generated_at
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES), \
             patch.object(api_server, "_fetch_claim_member_rows",
                          return_value=FAKE_MEMBER_ROWS):
            return self.client.get("/api/claim/abc123def456")

    def test_present_when_cached_pair(self):
        body = self._get_claim(TS).json()
        self.assertTrue(body["found"])
        self.assertEqual(body["graph_generated_at"], TS)
        # additive: the existing cluster block did not move or change shape
        self.assertEqual(body["cluster"]["outlet_count"], 3)

    def test_absent_when_timestamp_unknown(self):
        body = self._get_claim(None).json()
        self.assertTrue(body["found"])
        self.assertNotIn("graph_generated_at", body)

    def test_absent_when_indexes_not_the_cached_entry(self):
        api_server._SPREAD_CACHE["indexes"] = None
        api_server._SPREAD_CACHE["generated_at"] = TS
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES), \
             patch.object(api_server, "_fetch_claim_member_rows",
                          return_value=FAKE_MEMBER_ROWS):
            body = self.client.get("/api/claim/abc123def456").json()
        self.assertNotIn("graph_generated_at", body)

    def test_not_found_body_unchanged(self):
        api_server._SPREAD_CACHE["generated_at"] = TS
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES):
            body = self.client.get("/api/claim/ffffffffffff").json()
        self.assertEqual(body, {"found": False})


class ClusterSpreadsGraphGeneratedAtTests(_CacheIsolationMixin,
                                          unittest.TestCase):
    def _get(self, query, indexes, generated_at):
        api_server._SPREAD_CACHE["indexes"] = indexes
        api_server._SPREAD_CACHE["generated_at"] = generated_at
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=indexes), \
             patch.object(api_server, "_fetch_published_at_by_id",
                          return_value={1: "2026-08-01T09:00:00",
                                        2: "2026-08-03T10:00:00"}):
            return self.client.get(f"/api/cluster-spreads{query}")

    def test_present_top_level_only(self):
        body = self._get("?ids=1", FAKE_INDEXES, TS).json()
        self.assertEqual(body["graph_generated_at"], TS)
        # per-id entries untouched — no timestamp inside them
        self.assertNotIn("graph_generated_at", body["spreads"]["1"])

    def test_absent_when_timestamp_unknown(self):
        body = self._get("?ids=1", FAKE_INDEXES, None).json()
        self.assertNotIn("graph_generated_at", body)
        self.assertIn("1", body["spreads"])

    def test_empty_paths_stay_byte_identical(self):
        api_server._SPREAD_CACHE["generated_at"] = TS
        with patch.object(api_server, "_load_spread_indexes",
                          return_value=FAKE_INDEXES):
            no_match = self.client.get("/api/cluster-spreads?ids=9999")
            no_ids = self.client.get("/api/cluster-spreads")
        self.assertEqual(no_match.json(), {"spreads": {}})
        self.assertEqual(no_ids.json(), {"spreads": {}})


class HeroPickGraphGeneratedAtTests(_CacheIsolationMixin, unittest.TestCase):
    CANDIDATES = [{"stable_id": "abc123def456",
                   "representative_analysis_id": 1, "outlet_count": 3}]

    def _get(self, candidates, cached_hero, generated_at):
        api_server._TRENDING_DISPLAY_CACHE["hero"] = cached_hero
        api_server._TRENDING_DISPLAY_CACHE["generated_at"] = generated_at
        with patch.object(api_server, "_load_hero_pick_candidates",
                          return_value=candidates):
            return self.client.get("/api/hero-pick")

    def test_present_when_cached_pair(self):
        body = self._get(self.CANDIDATES, self.CANDIDATES, TS).json()
        self.assertEqual(body["candidates"], self.CANDIDATES)
        self.assertEqual(body["graph_generated_at"], TS)

    def test_absent_when_timestamp_unknown(self):
        body = self._get(self.CANDIDATES, self.CANDIDATES, None).json()
        self.assertEqual(body, {"candidates": self.CANDIDATES})

    def test_absent_when_candidates_not_the_cached_entry(self):
        body = self._get(self.CANDIDATES, None, TS).json()
        self.assertEqual(body, {"candidates": self.CANDIDATES})

    def test_empty_body_unchanged(self):
        body = self._get(None, None, TS).json()
        self.assertEqual(body, {"candidates": []})


if __name__ == "__main__":
    unittest.main()
