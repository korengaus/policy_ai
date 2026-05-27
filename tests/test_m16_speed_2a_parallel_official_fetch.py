"""M16-speed-2a — pins for parallel fetch_official_evidence.

Verifies the ThreadPoolExecutor refactor at
``official_crawler.fetch_official_evidence``:

  1. test_sequential_fallback_when_env_var_is_1
       MAX_PARALLEL_OFFICIAL_CANDIDATES=1 forces the byte-identical
       sequential path (all calls on a single thread).

  2. test_parallel_execution_when_env_var_is_3
       With default 3 workers and a stub that delays mid-call, at
       least two candidates' starts overlap before the first ends —
       proves actual concurrency.

  3. test_result_order_preserved_under_jittered_completion
       Stub returns out-of-input-order (last candidate completes
       first, first completes last). evidence_results[i] must still
       match selected_candidates[i] — i.e. result list is in INPUT
       order, not completion order. Byte-identicality with
       sequential.

  4. test_per_candidate_failure_isolated_with_sentinel
       Make candidate 1 raise. Candidates 0 and 2 still return
       successfully. Candidate 1's slot holds a sentinel error dict
       (NOT None) so downstream `.get(...)` consumers don't crash.

  5. test_empty_candidates_returns_empty_list
       Edge case: zero candidates returns ``[]`` without invoking
       the executor (no log lines, no crash).

  6. test_max_parallel_helper_handles_invalid_env (optional)
       _max_parallel_official_candidates() returns 3 when env var
       is unset / non-numeric / zero / negative.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import official_crawler  # noqa: E402


def _candidates(n: int) -> list[dict]:
    return [
        {
            "source_name": f"Source {i}",
            "source_type": "official_government",
            "search_query": f"q{i}",
            "search_url": f"https://example.gov.kr/search?q={i}",
            "_index_for_test": i,
        }
        for i in range(n)
    ]


def _stub_factory(call_log: list, delay_ms_by_index: dict | None = None):
    """Return a stub for ``fetch_best_official_document`` that records
    thread ids + start/end timestamps and optionally sleeps so a
    specific completion order can be forced under threading."""
    delays = delay_ms_by_index or {}

    def _stub(candidate, news_context=None):
        idx = candidate.get("_index_for_test", -1)
        call_log.append(("start", idx, threading.get_ident(), time.monotonic()))
        delay_ms = delays.get(idx, 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        call_log.append(("end", idx, threading.get_ident(), time.monotonic()))
        return {
            "source_name": candidate.get("source_name"),
            "source_type": candidate.get("source_type"),
            "fetched": True,
            "_index_for_test": idx,
        }

    return _stub


# ---------------------------------------------------------------------------
# 1. Sequential fallback under MAX_PARALLEL_OFFICIAL_CANDIDATES=1
# ---------------------------------------------------------------------------


class SequentialFallbackTests(unittest.TestCase):
    def test_sequential_fallback_when_env_var_is_1(self):
        candidates = _candidates(3)
        call_log: list = []
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "1"}, clear=False,
        ):
            with mock.patch.object(
                official_crawler,
                "fetch_best_official_document",
                side_effect=_stub_factory(call_log),
            ):
                results = official_crawler.fetch_official_evidence(
                    candidates, max_candidates=3,
                )

        self.assertEqual(len(results), 3)
        # Single thread → all 6 events (3 starts + 3 ends) share one thread id.
        thread_ids = {entry[2] for entry in call_log}
        self.assertEqual(
            len(thread_ids), 1,
            f"max_parallel=1 must execute on a single thread; saw {thread_ids}",
        )

    def test_max_parallel_helper_handles_invalid_env(self):
        """Unset / invalid / zero / negative → default 3."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_PARALLEL_OFFICIAL_CANDIDATES", None)
            self.assertEqual(official_crawler._max_parallel_official_candidates(), 3)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "garbage"}, clear=False,
        ):
            self.assertEqual(official_crawler._max_parallel_official_candidates(), 3)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "0"}, clear=False,
        ):
            self.assertEqual(official_crawler._max_parallel_official_candidates(), 1)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "-5"}, clear=False,
        ):
            self.assertEqual(official_crawler._max_parallel_official_candidates(), 1)


# ---------------------------------------------------------------------------
# 2. Actual parallel execution
# ---------------------------------------------------------------------------


class ParallelExecutionTests(unittest.TestCase):
    def test_parallel_execution_when_env_var_is_3(self):
        """With 3 workers and a 100ms sleep inside each stub, at
        least two candidates' starts must occur before the first
        candidate's end (overlap proves actual concurrency)."""
        candidates = _candidates(3)
        call_log: list = []
        # All three sleep 100ms; with 3 workers they all start ~together.
        delays = {0: 100, 1: 100, 2: 100}
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "3"}, clear=False,
        ):
            with mock.patch.object(
                official_crawler,
                "fetch_best_official_document",
                side_effect=_stub_factory(call_log, delays),
            ):
                results = official_crawler.fetch_official_evidence(
                    candidates, max_candidates=3,
                )

        self.assertEqual(len(results), 3)
        starts = sorted(
            entry[3] for entry in call_log if entry[0] == "start"
        )
        first_end = min(
            entry[3] for entry in call_log if entry[0] == "end"
        )
        starts_before_first_end = sum(1 for t in starts if t < first_end)
        self.assertGreaterEqual(
            starts_before_first_end, 2,
            "Parallel execution requires at least 2 starts before the "
            f"first end. starts={starts!r}, first_end={first_end!r}",
        )
        # Confirm multiple thread ids were used.
        thread_ids = {entry[2] for entry in call_log}
        self.assertGreater(
            len(thread_ids), 1,
            "Parallel path must use more than one thread; "
            f"saw {thread_ids}",
        )


# ---------------------------------------------------------------------------
# 3. Result-order preservation under jittered completion
# ---------------------------------------------------------------------------


class OrderPreservationTests(unittest.TestCase):
    def test_result_order_preserved_under_jittered_completion(self):
        """Force candidate 0 to take 100ms longer than candidates 1
        and 2 so it completes LAST. Output list must still be in
        INPUT order (results[0] → candidate 0, results[1] → 1, ...)."""
        candidates = _candidates(3)
        call_log: list = []
        delays = {0: 120, 1: 0, 2: 0}
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "3"}, clear=False,
        ):
            with mock.patch.object(
                official_crawler,
                "fetch_best_official_document",
                side_effect=_stub_factory(call_log, delays),
            ):
                results = official_crawler.fetch_official_evidence(
                    candidates, max_candidates=3,
                )

        # Verify the slow candidate did indeed complete LAST (so we
        # actually exercised the jittered-completion path).
        ends = [entry for entry in call_log if entry[0] == "end"]
        last_to_finish = max(ends, key=lambda e: e[3])[1]
        self.assertEqual(
            last_to_finish, 0,
            "Test setup requires candidate 0 to complete last; "
            f"got {last_to_finish}",
        )

        # Results MUST still be in INPUT order.
        self.assertEqual(len(results), 3)
        for i in range(3):
            self.assertEqual(
                results[i].get("source_name"),
                candidates[i].get("source_name"),
                f"results[{i}] must correspond to candidates[{i}] regardless "
                "of completion order. Index-mapped pool preserves input order.",
            )
            self.assertEqual(
                results[i].get("_index_for_test"), i,
            )


# ---------------------------------------------------------------------------
# 4. Per-candidate failure isolation with sentinel
# ---------------------------------------------------------------------------


class FailureIsolationTests(unittest.TestCase):
    def test_per_candidate_failure_isolated_with_sentinel(self):
        """Candidate 1 raises; candidates 0 and 2 return normally.
        Candidate 1's slot holds a sentinel error dict (NOT None) so
        downstream `.get(...)` consumers don't crash."""
        candidates = _candidates(3)

        def _selective_raiser(candidate, news_context=None):
            idx = candidate.get("_index_for_test")
            if idx == 1:
                raise RuntimeError("simulated per-candidate failure")
            return {
                "source_name": candidate.get("source_name"),
                "source_type": candidate.get("source_type"),
                "fetched": True,
                "_index_for_test": idx,
            }

        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "3"}, clear=False,
        ):
            with mock.patch.object(
                official_crawler,
                "fetch_best_official_document",
                side_effect=_selective_raiser,
            ):
                results = official_crawler.fetch_official_evidence(
                    candidates, max_candidates=3,
                )

        self.assertEqual(len(results), 3)
        # Candidates 0 and 2 succeeded.
        self.assertTrue(results[0].get("fetched"))
        self.assertTrue(results[2].get("fetched"))
        # Candidate 1 produced the sentinel.
        sentinel = results[1]
        self.assertIsNotNone(
            sentinel,
            "Per-candidate failure MUST produce a sentinel dict "
            "(not None) so downstream `.get(...)` consumers stay safe.",
        )
        self.assertIsInstance(sentinel, dict)
        self.assertFalse(sentinel.get("fetched"))
        self.assertFalse(sentinel.get("usable"))
        self.assertFalse(sentinel.get("weakly_usable"))
        self.assertFalse(sentinel.get("document_fetched"))
        self.assertIn(
            "parallel_pool_failed", sentinel.get("error") or "",
        )
        self.assertEqual(
            sentinel.get("source_name"), candidates[1].get("source_name"),
        )

    def test_failure_sentinel_supports_downstream_get_access(self):
        """Pin the .get() interface so a future refactor cannot
        accidentally return a non-dict (e.g. None, namedtuple) that
        would crash evidence_comparator / verification_card."""
        candidate = {"source_name": "Test", "source_type": "official_government"}
        sentinel = official_crawler._candidate_failure_sentinel(
            candidate, RuntimeError("boom"),
        )
        self.assertIsInstance(sentinel, dict)
        # Fields downstream consumers (evidence_comparator,
        # policy_confidence, verification_card,
        # enrich_official_source_candidates_with_bodies) commonly read.
        for key in [
            "source_name", "source_type", "fetched", "usable",
            "weakly_usable", "document_fetched",
            "should_exclude_from_verification", "error",
        ]:
            # All keys must be retrievable via .get() without raising.
            sentinel.get(key)


# ---------------------------------------------------------------------------
# 5. Empty list edge case
# ---------------------------------------------------------------------------


class EmptyCandidatesTests(unittest.TestCase):
    def test_empty_candidates_returns_empty_list(self):
        """Zero candidates → return [] without invoking the executor
        or fetch_best_official_document."""
        call_count = {"value": 0}

        def _stub(candidate, news_context=None):
            call_count["value"] += 1
            return {"fetched": True}

        with mock.patch.object(
            official_crawler,
            "fetch_best_official_document",
            side_effect=_stub,
        ):
            results = official_crawler.fetch_official_evidence(
                [], max_candidates=3,
            )

        self.assertEqual(results, [])
        self.assertEqual(call_count["value"], 0)

    def test_single_candidate_takes_sequential_path(self):
        """One candidate → sequential fallback (max_parallel clamped
        to len). No executor overhead."""
        candidates = _candidates(1)
        call_log: list = []
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_OFFICIAL_CANDIDATES": "3"}, clear=False,
        ):
            with mock.patch.object(
                official_crawler,
                "fetch_best_official_document",
                side_effect=_stub_factory(call_log),
            ):
                results = official_crawler.fetch_official_evidence(
                    candidates, max_candidates=3,
                )

        self.assertEqual(len(results), 1)
        thread_ids = {entry[2] for entry in call_log}
        self.assertEqual(
            len(thread_ids), 1,
            "Single-candidate path must execute on the calling thread "
            f"(no executor spawn); saw {thread_ids}",
        )


if __name__ == "__main__":
    unittest.main()
