"""M26.3 — concurrent ai_reasoner fan-out (gated off by default).

Mock-driven: NO live OpenAI/Anthropic calls. Phase A is mocked to return
synthetic phase_a dicts; run_ai_reasoning is mocked to a deterministic
per-item result. Drives the REAL analyze_pipeline so the actual fan-out +
serial fold-back code runs.

Proves: sequential (gate off) vs concurrent (gate on) parity of report_items /
counters; duplicate-article dedup ordering preserved; per-item ai_result maps
to the correct item (no index scramble); bounded pool; gate-off builds no
executor (Phase A sequential); verdict fields pass through unchanged.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


def _make_phase_a(*, title, summary, url, article_id):
    """A complete synthetic Phase-A result carrying every key the Phase-B
    fold-back / report assembly reads. Verdict fields are sentinels so we can
    assert they pass through untouched."""
    return {
        "index": 0,
        "total": 0,
        "news": {
            "title": title,
            "summary": summary,
            "published": "Mon, 02 Jun 2026 00:00:00 GMT",
            "google_link": "g:" + url,  # != original_url -> not a decode-failure
        },
        "original_url": url,
        "article_id": article_id,
        "article_body": "body " + title,
        "claims": [],
        "normalized_claims": [],
        "policy_claims": [],
        "memory_context": "ctx",  # frozen Phase-A snapshot
        "preliminary_topic": "프리토픽",
        "official_source_candidates": [],
        "official_evidence_results": [],
        "source_queries": [],
        "source_candidates": [],
        "evidence_snippets": [],
        "claim_evidence_map": {},
        "claim_evidence_quality_summary": [],
        "evidence_quality_summary": {},
        "contradiction_checks": [],
        "contradiction_summary": {},
        "bias_framing_analysis": [],
        "bias_framing_summary": {},
        "evidence_comparison": {},
        "policy_confidence": {"policy_confidence_score": 11},
        "policy_impact": {"impact_level": "low"},
        "final_decision": {"policy_alert_level": "LOW", "_sentinel": title},
        "verification_card": {"verdict_label": "draft_unverified", "_sentinel": title},
        "debug_summary": {},
        "news_collection_debug": {},
    }


def _fake_reason(news_title=None, **kwargs):
    """Deterministic per-item reasoning result keyed on the title, so we can
    detect any index/order scramble in the fold-back."""
    return {
        "ai_available": True,
        "ai_status": "ok",
        "ai_status_reason": "ok",
        "ai_model": "gpt-4o-mini",
        "one_line_summary": news_title,
        "main_policy_issue": news_title,
    }


_ENV_KEYS = (
    "AI_REASONER_CONCURRENCY_ENABLED",
    "AI_REASONER_MAX_CONCURRENCY",
    "MAX_PARALLEL_NEWS_ITEMS",
)


class _Pipeline:
    """Run the REAL analyze_pipeline with Phase A + IO mocked, returning the
    report. Phase A forced sequential (MAX_PARALLEL_NEWS_ITEMS=1) for
    determinism; only the M26.3 fan-out concurrency is the variable."""

    def __init__(self, phase_a_list, *, concurrency, max_conc="3"):
        self.phase_a_list = phase_a_list
        self.env = {
            "AI_REASONER_CONCURRENCY_ENABLED": "true" if concurrency else "false",
            "AI_REASONER_MAX_CONCURRENCY": max_conc,
            "MAX_PARALLEL_NEWS_ITEMS": "1",
        }
        self.captured = {}

    def run(self, executor_spy=None):
        saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k, v in self.env.items():
            os.environ[k] = v
        news_results = [{"title": f"n{i}"} for i in range(len(self.phase_a_list))]
        patches = [
            mock.patch.object(main, "load_policy_memory", side_effect=lambda: {}),
            mock.patch.object(main, "save_policy_memory"),
            mock.patch.object(
                main, "search_google_news_rss_with_meta",
                return_value={"results": news_results, "debug": {}},
            ),
            mock.patch.object(main, "_get_cached_analysis_report", return_value=None),
            mock.patch.object(
                main, "_process_news_item_phase_a",
                side_effect=list(self.phase_a_list),
            ),
            mock.patch.object(main, "run_ai_reasoning", side_effect=_fake_reason),
            mock.patch.object(main, "classify_policy_topic", return_value="T"),
            mock.patch.object(main, "_store_analysis_report"),
            mock.patch.object(
                main, "save_run_report", return_value=Path("reports/test.json"),
            ),
        ]
        if executor_spy is not None:
            patches.append(mock.patch.object(main, "ThreadPoolExecutor", executor_spy))
        try:
            for p in patches:
                p.start()
            return main.analyze_pipeline(query="q", max_news=len(news_results))
        finally:
            for p in patches:
                p.stop()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class ParityTests(unittest.TestCase):
    def _distinct_items(self):
        return [
            _make_phase_a(title="t0", summary="s0", url="u0", article_id="A0"),
            _make_phase_a(title="t1", summary="s1", url="u1", article_id="A1"),
            _make_phase_a(title="t2", summary="s2", url="u2", article_id="A2"),
        ]

    def test_sequential_vs_concurrent_parity(self):
        off = _Pipeline(self._distinct_items(), concurrency=False).run()
        on = _Pipeline(self._distinct_items(), concurrency=True).run()
        self.assertEqual(off["news_results"], on["news_results"])
        self.assertEqual(off["saved_event_count"], on["saved_event_count"])
        self.assertEqual(off["duplicate_count"], on["duplicate_count"])
        self.assertEqual(on["saved_event_count"], 3)
        self.assertEqual(on["duplicate_count"], 0)

    def test_per_item_result_maps_to_correct_item(self):
        report = _Pipeline(self._distinct_items(), concurrency=True).run()
        # The fold-back must attach item i's reasoning to item i (no scramble).
        for item in report["news_results"]:
            self.assertEqual(
                item["api_result"]["ai_status"], "ok",
            )
            self.assertEqual(
                item["ai_result"]["one_line_summary"], item["title"],
            )

    def test_verdict_fields_pass_through_unchanged(self):
        report = _Pipeline(self._distinct_items(), concurrency=True).run()
        for item in report["news_results"]:
            # Sentinel proves the verdict objects came straight from phase_a,
            # unmodified by the concurrency change.
            self.assertEqual(
                item["final_decision"]["_sentinel"], item["title"],
            )
            self.assertEqual(
                item["verification_card"]["_sentinel"], item["title"],
            )
            self.assertEqual(item["final_decision"]["policy_alert_level"], "LOW")


class DuplicateDedupOrderingTests(unittest.TestCase):
    def _dup_items(self):
        # Same article_id, different url + title -> M15 dedup keeps both, then
        # the Phase-B article_id dedup must flag the SECOND as duplicate.
        return [
            _make_phase_a(title="dup-a", summary="s", url="ua", article_id="SAME"),
            _make_phase_a(title="dup-b", summary="s", url="ub", article_id="SAME"),
        ]

    def test_duplicate_ordering_preserved_off_and_on(self):
        off = _Pipeline(self._dup_items(), concurrency=False).run()
        on = _Pipeline(self._dup_items(), concurrency=True).run()
        self.assertEqual(off["news_results"], on["news_results"])
        self.assertEqual(on["saved_event_count"], 1)
        self.assertEqual(on["duplicate_count"], 1)
        # First item saved, second flagged duplicate — order preserved.
        self.assertFalse(on["news_results"][0]["duplicate"])
        self.assertTrue(on["news_results"][1]["duplicate"])


class PoolAndGateTests(unittest.TestCase):
    def _items(self):
        return [
            _make_phase_a(title="t0", summary="s", url="u0", article_id="A0"),
            _make_phase_a(title="t1", summary="s", url="u1", article_id="A1"),
        ]

    def test_bounded_pool_uses_max_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor as RealTPE

        captured = {}

        def spy(*args, **kwargs):
            captured["max_workers"] = kwargs.get("max_workers")
            return RealTPE(*args, **kwargs)

        _Pipeline(self._items(), concurrency=True, max_conc="2").run(executor_spy=spy)
        self.assertEqual(captured["max_workers"], 2)

    def test_gate_off_builds_no_executor(self):
        # Phase A forced sequential (MAX_PARALLEL_NEWS_ITEMS=1) + concurrency
        # off -> no ThreadPoolExecutor is constructed anywhere.
        spy = mock.MagicMock(side_effect=AssertionError("executor must not be built"))
        # Should NOT raise: spy never called.
        report = _Pipeline(self._items(), concurrency=False).run(executor_spy=spy)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(report["saved_event_count"], 2)


if __name__ == "__main__":
    unittest.main()
