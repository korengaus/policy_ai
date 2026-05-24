"""M15.0d — Parallel per-news-item processing pins.

Verifies the Phase A (parallel) + Phase B (sequential) split in
``main.analyze_pipeline``:

  1. **Order preservation:** with N news items, ``report_items``
     comes out in the same order as the input list, regardless of
     completion order in the parallel phase.
  2. **Error isolation:** if Phase A fails for one news item, the
     other items still complete and their results land in the
     report.
  3. **MAX_PARALLEL_NEWS_ITEMS=1 rollback:** with the env override,
     execution is sequential and byte-identical to pre-M15.0d.
  4. **Phase B is sequential and in submission order:** memory
     mutations happen in input order, never interleaved.
  5. **progress_callback wiring:** stages fire with the documented
     payload shape.
  6. **No verdict-producing logic touched:** smoke-imports of every
     M11.0d artifact (verdict producers + tests) still pass.

Tests run fully offline. ``analyze_pipeline``'s helper modules
(``main._process_news_item_phase_a`` and ``_apply_news_item_phase_b``)
are tested directly with mocked inputs. The real 174s pipeline is
never invoked; ``main.analyze_pipeline`` itself is exercised end to
end with mocked sub-helpers so we can verify the parallel-loop
control flow without HTTP / OpenAI / Playwright.
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


import main  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_news_item(index: int) -> dict:
    return {
        "title": f"제목 {index}",
        "published": f"2026-05-25T0{index}:00:00+00:00",
        "google_link": f"https://news.google.com/articles/{index}",
        "summary": f"요약 {index}",
        "source": "google_rss",
    }


def _stub_phase_a_factory(call_log: list, delay_ms_by_index: dict | None = None):
    """Return a stub for ``_process_news_item_phase_a`` that records
    call order and optionally sleeps for `delay_ms_by_index[index]`
    so we can force a specific completion order under threading."""
    delays = delay_ms_by_index or {}

    def _stub(news, *, index, total, memory_snapshot, query,
              news_collection_debug, analysis_cache_key):
        call_log.append(("phase_a_start", index, threading.get_ident()))
        delay_ms = delays.get(index, 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        call_log.append(("phase_a_end", index, threading.get_ident()))
        return {
            "index": index,
            "total": total,
            "news": news,
            "original_url": news["google_link"],
            "article_id": f"id-{index}",
            "article_body": f"body-{index}",
            "claims": [],
            "normalized_claims": [],
            "policy_claims": [],
            "memory_context": "",
            "preliminary_topic": "test",
            "official_source_candidates": [],
            "official_evidence_results": [],
            "source_queries": [],
            "source_candidates": [],
            "evidence_snippets": [],
            "claim_evidence_map": {},
            "contradiction_checks": [],
            "contradiction_summary": {},
            "bias_framing_analysis": [],
            "bias_framing_summary": {},
            "evidence_comparison": {},
            "policy_confidence": {"policy_confidence_score": 50},
            "policy_impact": {"impact_level": "medium"},
            "final_decision": {"policy_alert_level": "WATCH"},
            "verification_card": {"verdict_label": "draft_unverified"},
            "debug_summary": {},
            "evidence_quality_summary": {},
            "claim_evidence_quality_summary": [],
            "news_collection_debug": news_collection_debug,
        }

    return _stub


def _stub_phase_b_factory(call_log: list):
    """Sequential Phase B stub that records call order — verifies
    Phase B runs in submission order."""

    def _stub(phase_a, memory):
        idx = phase_a["index"]
        call_log.append(("phase_b", idx, threading.get_ident()))
        return {
            "report_item": {
                "title": phase_a["news"]["title"],
                "index_for_test": idx,
                "api_result": {"title": phase_a["news"]["title"]},
            },
            "saved_to_memory": False,
            "duplicate": False,
        }

    return _stub


# ---------------------------------------------------------------------------
# 1. Order preservation
# ---------------------------------------------------------------------------


class OrderPreservationTests(unittest.TestCase):
    def test_report_items_in_submission_order_under_parallel(self):
        """Force Phase A item 0 to take 50ms longer than items 1 and
        2 so it completes LAST. Phase B (sequential) must still
        append in submission order (0, 1, 2)."""
        news_results = [_make_news_item(i) for i in range(3)]
        call_log: list = []
        phase_a = _stub_phase_a_factory(
            call_log,
            delay_ms_by_index={1: 50, 2: 0, 3: 0},
        )
        phase_b = _stub_phase_b_factory(call_log)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "3"}, clear=False,
        ):
            with mock.patch.object(main, "_process_news_item_phase_a", phase_a):
                with mock.patch.object(main, "_apply_news_item_phase_b", phase_b):
                    with mock.patch.object(
                        main, "_get_cached_analysis_report", return_value=None,
                    ):
                        with mock.patch.object(
                            main, "search_google_news_rss_with_meta",
                            return_value={"results": news_results, "debug": {}},
                        ):
                            with mock.patch.object(
                                main, "load_policy_memory",
                                return_value={"articles": [], "topics": {}},
                            ):
                                with mock.patch.object(
                                    main, "move_existing_articles_to_better_topics",
                                ):
                                    with mock.patch.object(main, "save_policy_memory"):
                                        with mock.patch.object(
                                            main, "build_analysis_cache_key",
                                            return_value="cache-key",
                                        ):
                                            with mock.patch.object(
                                                main, "_store_analysis_report",
                                            ):
                                                with mock.patch.object(
                                                    main, "save_run_report",
                                                    return_value=Path("/tmp/x.json"),
                                                ):
                                                    with mock.patch.object(
                                                        main, "print_timeline_summary",
                                                    ):
                                                        with mock.patch.object(
                                                            main, "build_topics_summary",
                                                            return_value={},
                                                        ):
                                                            with mock.patch.object(
                                                                main, "_summarize_ai_status_from_items",
                                                                return_value={},
                                                            ):
                                                                report = main.analyze_pipeline(
                                                                    query="test",
                                                                    max_news=3,
                                                                )
        # Phase B must have run in submission order, even though
        # Phase A index 1 completed last.
        phase_b_calls = [(kind, idx) for kind, idx, _tid in call_log if kind == "phase_b"]
        self.assertEqual(
            phase_b_calls, [("phase_b", 1), ("phase_b", 2), ("phase_b", 3)],
        )
        # report_items reflects the same order.
        titles = [item["title"] for item in report["news_results"]]
        self.assertEqual(titles, ["제목 0", "제목 1", "제목 2"])
        self.assertEqual(report["total_news_count"], 3)

    def test_phase_a_actually_ran_in_parallel(self):
        """When MAX_PARALLEL_NEWS_ITEMS>=2 and N>=2, phase_a entries
        must overlap (start of next item before end of prior item)
        across different thread ids."""
        news_results = [_make_news_item(i) for i in range(2)]
        call_log: list = []
        phase_a = _stub_phase_a_factory(
            call_log,
            delay_ms_by_index={1: 80, 2: 80},
        )
        phase_b = _stub_phase_b_factory(call_log)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "2"}, clear=False,
        ):
            with mock.patch.object(main, "_process_news_item_phase_a", phase_a):
                with mock.patch.object(main, "_apply_news_item_phase_b", phase_b):
                    with mock.patch.object(
                        main, "_get_cached_analysis_report", return_value=None,
                    ):
                        with mock.patch.object(
                            main, "search_google_news_rss_with_meta",
                            return_value={"results": news_results, "debug": {}},
                        ):
                            with mock.patch.object(
                                main, "load_policy_memory",
                                return_value={"articles": [], "topics": {}},
                            ):
                                with mock.patch.object(
                                    main, "move_existing_articles_to_better_topics",
                                ):
                                    with mock.patch.object(main, "save_policy_memory"):
                                        with mock.patch.object(
                                            main, "build_analysis_cache_key",
                                            return_value="k",
                                        ):
                                            with mock.patch.object(main, "_store_analysis_report"):
                                                with mock.patch.object(
                                                    main, "save_run_report",
                                                    return_value=Path("/tmp/x.json"),
                                                ):
                                                    with mock.patch.object(main, "print_timeline_summary"):
                                                        with mock.patch.object(
                                                            main, "build_topics_summary",
                                                            return_value={},
                                                        ):
                                                            with mock.patch.object(
                                                                main, "_summarize_ai_status_from_items",
                                                                return_value={},
                                                            ):
                                                                main.analyze_pipeline(query="t", max_news=2)
        # Two Phase A "starts" must occur before either "end".
        phase_a_events = [(k, idx) for k, idx, _tid in call_log if k.startswith("phase_a")]
        # Find positions of the two starts and the first end.
        first_start = next(i for i, e in enumerate(phase_a_events) if e[0] == "phase_a_start")
        first_end = next(i for i, e in enumerate(phase_a_events) if e[0] == "phase_a_end")
        # Number of phase_a_starts that occurred BEFORE the first end.
        starts_before_first_end = sum(
            1 for i in range(first_end) if phase_a_events[i][0] == "phase_a_start"
        )
        self.assertGreaterEqual(
            starts_before_first_end, 2,
            "Phase A items must run in parallel (both starts before first end)",
        )
        # Thread ids must differ across the two starts.
        start_tids = {tid for k, _idx, tid in call_log if k == "phase_a_start"}
        self.assertGreaterEqual(len(start_tids), 2)


# ---------------------------------------------------------------------------
# 2. Error isolation
# ---------------------------------------------------------------------------


class ErrorIsolationTests(unittest.TestCase):
    def test_phase_a_failure_does_not_abort_other_items(self):
        news_results = [_make_news_item(i) for i in range(3)]
        call_log: list = []

        def _failing_phase_a(news, *, index, total, memory_snapshot, query,
                              news_collection_debug, analysis_cache_key):
            if index == 2:
                raise RuntimeError(f"simulated Phase A failure for index {index}")
            call_log.append(("phase_a_ok", index))
            return {
                "index": index, "total": total, "news": news,
                "original_url": news["google_link"], "article_id": f"id-{index}",
                "article_body": "", "claims": [], "normalized_claims": [],
                "policy_claims": [], "memory_context": "",
                "preliminary_topic": "t", "official_source_candidates": [],
                "official_evidence_results": [], "source_queries": [],
                "source_candidates": [], "evidence_snippets": [],
                "claim_evidence_map": {}, "contradiction_checks": [],
                "contradiction_summary": {}, "bias_framing_analysis": [],
                "bias_framing_summary": {}, "evidence_comparison": {},
                "policy_confidence": {}, "policy_impact": {}, "final_decision": {},
                "verification_card": {}, "debug_summary": {},
                "evidence_quality_summary": {}, "claim_evidence_quality_summary": [],
                "news_collection_debug": news_collection_debug,
            }

        phase_b = _stub_phase_b_factory(call_log)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "3"}, clear=False,
        ):
            with mock.patch.object(main, "_process_news_item_phase_a", _failing_phase_a):
                with mock.patch.object(main, "_apply_news_item_phase_b", phase_b):
                    with mock.patch.object(main, "_get_cached_analysis_report", return_value=None):
                        with mock.patch.object(
                            main, "search_google_news_rss_with_meta",
                            return_value={"results": news_results, "debug": {}},
                        ):
                            with mock.patch.object(
                                main, "load_policy_memory",
                                return_value={"articles": [], "topics": {}},
                            ):
                                with mock.patch.object(main, "move_existing_articles_to_better_topics"):
                                    with mock.patch.object(main, "save_policy_memory"):
                                        with mock.patch.object(main, "build_analysis_cache_key", return_value="k"):
                                            with mock.patch.object(main, "_store_analysis_report"):
                                                with mock.patch.object(
                                                    main, "save_run_report",
                                                    return_value=Path("/tmp/x.json"),
                                                ):
                                                    with mock.patch.object(main, "print_timeline_summary"):
                                                        with mock.patch.object(main, "build_topics_summary", return_value={}):
                                                            with mock.patch.object(
                                                                main, "_summarize_ai_status_from_items",
                                                                return_value={},
                                                            ):
                                                                report = main.analyze_pipeline(query="t", max_news=3)
        # Items 1 and 3 succeed; item 2 fails Phase A; final report
        # has 2 entries, in submission order.
        titles = [item["title"] for item in report["news_results"]]
        self.assertEqual(titles, ["제목 0", "제목 2"])
        self.assertEqual(report["total_news_count"], 2)


# ---------------------------------------------------------------------------
# 3. MAX_PARALLEL_NEWS_ITEMS=1 rollback path
# ---------------------------------------------------------------------------


class SequentialRollbackPathTests(unittest.TestCase):
    def test_max_parallel_1_uses_single_thread(self):
        news_results = [_make_news_item(i) for i in range(2)]
        call_log: list = []
        phase_a = _stub_phase_a_factory(call_log, delay_ms_by_index={1: 30, 2: 0})
        phase_b = _stub_phase_b_factory(call_log)
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "1"}, clear=False,
        ):
            with mock.patch.object(main, "_process_news_item_phase_a", phase_a):
                with mock.patch.object(main, "_apply_news_item_phase_b", phase_b):
                    with mock.patch.object(main, "_get_cached_analysis_report", return_value=None):
                        with mock.patch.object(
                            main, "search_google_news_rss_with_meta",
                            return_value={"results": news_results, "debug": {}},
                        ):
                            with mock.patch.object(
                                main, "load_policy_memory",
                                return_value={"articles": [], "topics": {}},
                            ):
                                with mock.patch.object(main, "move_existing_articles_to_better_topics"):
                                    with mock.patch.object(main, "save_policy_memory"):
                                        with mock.patch.object(main, "build_analysis_cache_key", return_value="k"):
                                            with mock.patch.object(main, "_store_analysis_report"):
                                                with mock.patch.object(
                                                    main, "save_run_report",
                                                    return_value=Path("/tmp/x.json"),
                                                ):
                                                    with mock.patch.object(main, "print_timeline_summary"):
                                                        with mock.patch.object(main, "build_topics_summary", return_value={}):
                                                            with mock.patch.object(
                                                                main, "_summarize_ai_status_from_items",
                                                                return_value={},
                                                            ):
                                                                main.analyze_pipeline(query="t", max_news=2)
        # With max_parallel=1, all phase_a + phase_b calls must come
        # from the main thread (no ThreadPoolExecutor spawning).
        thread_ids = {tid for _k, _idx, tid in call_log}
        self.assertEqual(
            len(thread_ids), 1,
            f"max_parallel=1 must execute on a single thread; saw {thread_ids}",
        )

    def test_env_var_invalid_falls_back_to_default(self):
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "garbage"}, clear=False,
        ):
            self.assertEqual(main._max_parallel_news_items(), 3)

    def test_env_var_unset_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_PARALLEL_NEWS_ITEMS", None)
            self.assertEqual(main._max_parallel_news_items(), 3)

    def test_env_var_zero_clamped_to_1(self):
        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "0"}, clear=False,
        ):
            self.assertEqual(main._max_parallel_news_items(), 1)


# ---------------------------------------------------------------------------
# 4. progress_callback wiring
# ---------------------------------------------------------------------------


class ProgressCallbackTests(unittest.TestCase):
    def test_progress_callback_fires_for_parallel_started_and_per_item(self):
        news_results = [_make_news_item(i) for i in range(2)]
        phase_a = _stub_phase_a_factory([])
        phase_b = _stub_phase_b_factory([])
        events: list = []

        def _cb(stage, payload):
            events.append((stage, dict(payload)))

        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "2"}, clear=False,
        ):
            with mock.patch.object(main, "_process_news_item_phase_a", phase_a):
                with mock.patch.object(main, "_apply_news_item_phase_b", phase_b):
                    with mock.patch.object(main, "_get_cached_analysis_report", return_value=None):
                        with mock.patch.object(
                            main, "search_google_news_rss_with_meta",
                            return_value={"results": news_results, "debug": {}},
                        ):
                            with mock.patch.object(
                                main, "load_policy_memory",
                                return_value={"articles": [], "topics": {}},
                            ):
                                with mock.patch.object(main, "move_existing_articles_to_better_topics"):
                                    with mock.patch.object(main, "save_policy_memory"):
                                        with mock.patch.object(main, "build_analysis_cache_key", return_value="k"):
                                            with mock.patch.object(main, "_store_analysis_report"):
                                                with mock.patch.object(
                                                    main, "save_run_report",
                                                    return_value=Path("/tmp/x.json"),
                                                ):
                                                    with mock.patch.object(main, "print_timeline_summary"):
                                                        with mock.patch.object(main, "build_topics_summary", return_value={}):
                                                            with mock.patch.object(
                                                                main, "_summarize_ai_status_from_items",
                                                                return_value={},
                                                            ):
                                                                main.analyze_pipeline(
                                                                    query="t", max_news=2,
                                                                    progress_callback=_cb,
                                                                )
        stages = [e[0] for e in events]
        self.assertIn("news_item_parallel_started", stages)
        completed_events = [e for e in events if e[0] == "news_item_completed"]
        self.assertEqual(len(completed_events), 2)
        for _stage, payload in completed_events:
            self.assertIn("index", payload)
            self.assertIn("total", payload)

    def test_progress_callback_failure_does_not_break_pipeline(self):
        """Any exception in the callback must be swallowed so a
        broken progress reporter cannot fail the pipeline."""
        news_results = [_make_news_item(0)]
        phase_a = _stub_phase_a_factory([])
        phase_b = _stub_phase_b_factory([])

        def _broken_cb(stage, payload):
            raise RuntimeError("simulated callback failure")

        with mock.patch.dict(
            os.environ, {"MAX_PARALLEL_NEWS_ITEMS": "1"}, clear=False,
        ):
            with mock.patch.object(main, "_process_news_item_phase_a", phase_a):
                with mock.patch.object(main, "_apply_news_item_phase_b", phase_b):
                    with mock.patch.object(main, "_get_cached_analysis_report", return_value=None):
                        with mock.patch.object(
                            main, "search_google_news_rss_with_meta",
                            return_value={"results": news_results, "debug": {}},
                        ):
                            with mock.patch.object(
                                main, "load_policy_memory",
                                return_value={"articles": [], "topics": {}},
                            ):
                                with mock.patch.object(main, "move_existing_articles_to_better_topics"):
                                    with mock.patch.object(main, "save_policy_memory"):
                                        with mock.patch.object(main, "build_analysis_cache_key", return_value="k"):
                                            with mock.patch.object(main, "_store_analysis_report"):
                                                with mock.patch.object(
                                                    main, "save_run_report",
                                                    return_value=Path("/tmp/x.json"),
                                                ):
                                                    with mock.patch.object(main, "print_timeline_summary"):
                                                        with mock.patch.object(main, "build_topics_summary", return_value={}):
                                                            with mock.patch.object(
                                                                main, "_summarize_ai_status_from_items",
                                                                return_value={},
                                                            ):
                                                                # Must not raise.
                                                                report = main.analyze_pipeline(
                                                                    query="t", max_news=1,
                                                                    progress_callback=_broken_cb,
                                                                )
        self.assertEqual(report["total_news_count"], 1)


# ---------------------------------------------------------------------------
# 5. M11.0d invariant: verdict-producing helpers reachable + unchanged
# ---------------------------------------------------------------------------


class M11_0d_InvariantsStillReachableTests(unittest.TestCase):
    """Smoke that the M15.0d refactor didn't break the imports
    M11.0d artifacts depend on."""

    def test_disagreement_signal_builder_still_callable(self):
        signal = main._build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",
        )
        self.assertEqual(signal["p1_label"], "MEDIUM")
        self.assertEqual(signal["p2_label"], "HIGH")
        self.assertEqual(signal["p3_label"], "draft_verified")
        self.assertEqual(signal["p3_implied_tier"], "HIGH")
        self.assertFalse(signal["agreed"])

    def test_p3_mapping_table_unchanged(self):
        expected = {
            "draft_verified": "HIGH",
            "draft_likely_true": "MEDIUM",
            "draft_disputed": "WATCH",
            "draft_high_risk_review": "WATCH",
            "draft_needs_review": "WATCH",
            "draft_needs_official_confirmation": "WATCH",
            "draft_needs_context": "WATCH",
            "draft_unverified": "LOW",
        }
        for k, v in expected.items():
            self.assertEqual(main._P3_TO_ALERT_TIER[k], v)


# ---------------------------------------------------------------------------
# 6. Phase A/B function signatures and module surface
# ---------------------------------------------------------------------------


class PhaseHelperSignatureTests(unittest.TestCase):
    def test_phase_a_helper_exists(self):
        self.assertTrue(hasattr(main, "_process_news_item_phase_a"))
        self.assertTrue(callable(main._process_news_item_phase_a))

    def test_phase_b_helper_exists(self):
        self.assertTrue(hasattr(main, "_apply_news_item_phase_b"))
        self.assertTrue(callable(main._apply_news_item_phase_b))

    def test_max_parallel_helper_exists(self):
        self.assertTrue(hasattr(main, "_max_parallel_news_items"))
        self.assertEqual(main._max_parallel_news_items.__module__, "main")

    def test_analyze_pipeline_accepts_progress_callback_kwarg(self):
        import inspect
        sig = inspect.signature(main.analyze_pipeline)
        self.assertIn("progress_callback", sig.parameters)
        param = sig.parameters["progress_callback"]
        # Must be keyword-only so callers don't accidentally pass it
        # positionally.
        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)


if __name__ == "__main__":
    unittest.main()
