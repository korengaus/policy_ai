"""M13.1b-obs (2026-05-26) — LLM observability pins.

Covers:

  * Cost-estimation formula correctness (gpt-4o-mini per-1K rate +
    unknown-model None fallback).
  * Aggregator math (accumulation, caller separation, p95, ring-buffer
    cap, reset).
  * ai_reasoner.completed log fields after a successful Responses API
    call + aggregator population on the same path.
  * ai_reasoner.failed log on the broad-except path (preserving the
    M11.7c broad-except contract).
  * Stub-mode graceful skip (no API key → aggregator untouched).
  * llm_judge aggregator hook fires from _emit_cost_log.

11 tests total. Mocks ``openai.OpenAI`` everywhere — no real network.
"""

from __future__ import annotations

import logging
import os
import statistics
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Log capture helper (same shape as M11.7a-2 / M11.7c suites)
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name_prefix or record.name.startswith(
            self._name_prefix + "."
        ):
            self.records.append(record)


def _attach(logger_name: str) -> _CapturingHandler:
    logger = logging.getLogger(logger_name)
    handler = _CapturingHandler(logger_name)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.DEBUG:
        logger.setLevel(logging.DEBUG)
    return handler


def _detach(logger_name: str, handler: logging.Handler) -> None:
    logging.getLogger(logger_name).removeHandler(handler)


def _records_with_event(records, event_name):
    return [r for r in records if r.getMessage() == event_name]


# ---------------------------------------------------------------------------
# 1-2: Cost calculation
# ---------------------------------------------------------------------------


class CostCalculationTests(unittest.TestCase):
    """gpt-4o-mini cost formula: input_tokens * $0.000150/1K +
    output_tokens * $0.000600/1K. Verified against the OpenAI public
    pricing page on 2026-05-26."""

    def test_gpt_4o_mini_cost_formula(self):
        from llm_observability import estimate_cost_usd

        # 1000 input + 1000 output → 0.000150 + 0.000600 = 0.00075
        self.assertAlmostEqual(
            estimate_cost_usd("gpt-4o-mini", 1000, 1000),
            0.00075,
            places=6,
        )
        # 500 input + 2000 output → 0.000075 + 0.0012 = 0.001275
        self.assertAlmostEqual(
            estimate_cost_usd("gpt-4o-mini", 500, 2000),
            0.001275,
            places=6,
        )
        # 0 tokens → 0 cost
        self.assertEqual(estimate_cost_usd("gpt-4o-mini", 0, 0), 0.0)

    def test_unknown_model_returns_none(self):
        from llm_observability import estimate_cost_usd

        self.assertIsNone(estimate_cost_usd("gpt-5-imaginary", 1000, 1000))
        self.assertIsNone(estimate_cost_usd("", 1000, 1000))


# ---------------------------------------------------------------------------
# 3-7: Aggregator
# ---------------------------------------------------------------------------


class AggregatorTests(unittest.TestCase):

    def setUp(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()

    def tearDown(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()

    def test_record_call_accumulates(self):
        from llm_observability import (
            record_llm_call,
            get_metrics_snapshot,
        )

        # M13.1c — signature-only update: pass provider="openai".
        # Top-level snapshot fields (asserted below) remain sums
        # across providers, so semantic behavior is unchanged.
        record_llm_call(
            caller="llm_judge", model="gpt-4o-mini",
            input_tokens=500, output_tokens=200,
            estimated_cost_usd=0.000195, latency_ms=400, success=True,
            provider="openai",
        )
        record_llm_call(
            caller="llm_judge", model="gpt-4o-mini",
            input_tokens=300, output_tokens=100,
            estimated_cost_usd=0.000105, latency_ms=350, success=True,
            provider="openai",
        )

        snap = get_metrics_snapshot()
        self.assertIn("llm_judge", snap)
        m = snap["llm_judge"]
        self.assertEqual(m["total_calls"], 2)
        self.assertEqual(m["successful_calls"], 2)
        self.assertEqual(m["total_input_tokens"], 800)
        self.assertEqual(m["total_output_tokens"], 300)
        self.assertAlmostEqual(
            m["total_estimated_cost_usd"], 0.0003, places=6,
        )

    def test_caller_separation(self):
        from llm_observability import (
            record_llm_call,
            get_metrics_snapshot,
        )

        # M13.1c — signature-only update: pass provider="openai".
        record_llm_call(
            caller="llm_judge", model="gpt-4o-mini",
            input_tokens=100, output_tokens=50,
            estimated_cost_usd=0.0000450, latency_ms=400, success=True,
            provider="openai",
        )
        record_llm_call(
            caller="ai_reasoner", model="gpt-4o-mini",
            input_tokens=2000, output_tokens=500,
            estimated_cost_usd=0.0006, latency_ms=1500, success=True,
            provider="openai",
        )

        snap = get_metrics_snapshot()
        self.assertEqual(set(snap.keys()), {"llm_judge", "ai_reasoner"})
        self.assertEqual(snap["llm_judge"]["total_input_tokens"], 100)
        self.assertEqual(snap["ai_reasoner"]["total_input_tokens"], 2000)
        # Two callers' state must not bleed into each other.
        self.assertEqual(snap["llm_judge"]["total_calls"], 1)
        self.assertEqual(snap["ai_reasoner"]["total_calls"], 1)

    def test_p95_latency_correctness(self):
        from llm_observability import (
            record_llm_call,
            get_metrics_snapshot,
        )

        # 100 latencies 1..100; p95 should be ≈95 (linear interp).
        for latency in range(1, 101):
            record_llm_call(
                caller="llm_judge", model="gpt-4o-mini",
                input_tokens=0, output_tokens=0,
                estimated_cost_usd=0.0, latency_ms=latency, success=True,
                provider="openai",  # M13.1c signature-only update
            )

        snap = get_metrics_snapshot()
        m = snap["llm_judge"]
        # avg of 1..100 is 50.5 → rounded to 51 (banker's rounding) /
        # 50 — accept ±1 to keep the assertion stable across versions.
        self.assertIn(m["avg_latency_ms"], (50, 51))
        # p50 around 50, p95 around 95.
        self.assertGreaterEqual(m["p50_latency_ms"], 48)
        self.assertLessEqual(m["p50_latency_ms"], 52)
        self.assertGreaterEqual(m["p95_latency_ms"], 93)
        self.assertLessEqual(m["p95_latency_ms"], 97)
        self.assertEqual(m["latency_sample_count"], 100)

    def test_latency_ring_buffer_cap(self):
        from llm_observability import (
            record_llm_call,
            get_metrics_snapshot,
        )

        for i in range(1500):
            record_llm_call(
                caller="llm_judge", model="gpt-4o-mini",
                input_tokens=0, output_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=i,  # monotonically increasing
                success=True,
                provider="openai",  # M13.1c signature-only update
            )

        snap = get_metrics_snapshot()
        # Buffer capped at 1000 — the OLDEST 500 (latency_ms 0..499)
        # are dropped; the kept window is 500..1499.
        self.assertEqual(snap["llm_judge"]["latency_sample_count"], 1000)
        # total_calls still counts every push (we trimmed the latency
        # ring-buffer, not the call counter).
        self.assertEqual(snap["llm_judge"]["total_calls"], 1500)
        self.assertEqual(snap["llm_judge"]["successful_calls"], 1500)

    def test_reset_for_tests_clears_state(self):
        from llm_observability import (
            record_llm_call,
            get_metrics_snapshot,
            reset_metrics_for_tests,
        )

        record_llm_call(
            caller="llm_judge", model="gpt-4o-mini",
            input_tokens=100, output_tokens=50,
            estimated_cost_usd=0.0001, latency_ms=400, success=True,
            provider="openai",  # M13.1c signature-only update
        )
        self.assertIn("llm_judge", get_metrics_snapshot())

        reset_metrics_for_tests()
        self.assertEqual(get_metrics_snapshot(), {})


# ---------------------------------------------------------------------------
# 8-9: ai_reasoner log emission
# ---------------------------------------------------------------------------


class _FakeUsage:
    """Mimics openai Responses-API usage block."""

    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    """Mimics openai responses.create return value."""

    def __init__(self, output_text: str, usage: _FakeUsage):
        self.output_text = output_text
        self.usage = usage


class LogEmissionTests(unittest.TestCase):

    LOGGER_NAME = "ai_reasoner"

    def setUp(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()
        self.handler = _attach(self.LOGGER_NAME)
        self._saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key"

    def tearDown(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()
        _detach(self.LOGGER_NAME, self.handler)
        if self._saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved_key

    def test_ai_reasoner_completed_log_fields(self):
        import ai_reasoner
        from llm_observability import get_metrics_snapshot

        fake_client = mock.MagicMock()
        valid_json = '{"one_line_summary": "테스트", "policy_signal_detected": true}'
        fake_client.responses.create.return_value = _FakeResponse(
            output_text=valid_json,
            usage=_FakeUsage(input_tokens=1234, output_tokens=567),
        )

        with mock.patch.object(
            ai_reasoner, "get_openai_client",
            return_value=(fake_client, None),
        ):
            result = ai_reasoner.run_ai_reasoning(
                news_title="t", news_summary="s", article_body="b",
                policy_claims=[], memory_context="",
            )

        self.assertTrue(result.get("ai_available"))

        records = _records_with_event(
            self.handler.records, "ai_reasoner.completed",
        )
        self.assertEqual(
            len(records), 1,
            f"Expected exactly one ai_reasoner.completed log, got "
            f"{[r.getMessage() for r in self.handler.records]!r}",
        )
        record = records[0]
        self.assertEqual(record.levelno, logging.INFO)
        # Required fields per Phase 1 contract.
        self.assertEqual(getattr(record, "action"), "reasoning")
        self.assertEqual(getattr(record, "provider"), "openai")
        self.assertFalse(getattr(record, "fell_back"))
        self.assertEqual(getattr(record, "input_tokens"), 1234)
        self.assertEqual(getattr(record, "output_tokens"), 567)
        self.assertIsNotNone(getattr(record, "estimated_cost_usd"))
        self.assertGreaterEqual(getattr(record, "latency_ms"), 0)
        self.assertTrue(getattr(record, "model"))  # non-empty string

        # Aggregator also populated.
        snap = get_metrics_snapshot()
        self.assertIn("ai_reasoner", snap)
        self.assertEqual(snap["ai_reasoner"]["successful_calls"], 1)
        self.assertEqual(snap["ai_reasoner"]["total_input_tokens"], 1234)
        self.assertEqual(snap["ai_reasoner"]["total_output_tokens"], 567)

    def test_ai_reasoner_failed_log_on_exception(self):
        """When client.responses.create raises, the broad ``except
        Exception`` catches and emits ai_reasoner.failed (additive
        warning; M11.7c broad-except policy preserved)."""
        import ai_reasoner
        from llm_observability import get_metrics_snapshot

        fake_client = mock.MagicMock()
        fake_client.responses.create.side_effect = RuntimeError(
            "simulated openai SDK explosion",
        )

        with mock.patch.object(
            ai_reasoner, "get_openai_client",
            return_value=(fake_client, None),
        ):
            result = ai_reasoner.run_ai_reasoning(
                news_title="t", news_summary="s", article_body="b",
                policy_claims=[], memory_context="",
            )

        # Existing business-logic contract preserved.
        self.assertFalse(result.get("ai_available"))
        self.assertEqual(result.get("ai_status_reason"), "api_call_failed")

        records = _records_with_event(
            self.handler.records, "ai_reasoner.failed",
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(getattr(record, "reason"), "api_call_failed")
        self.assertEqual(
            getattr(record, "exception_type"), "RuntimeError",
        )

        # Aggregator must NOT show a successful call.
        snap = get_metrics_snapshot()
        if "ai_reasoner" in snap:
            self.assertEqual(snap["ai_reasoner"]["successful_calls"], 0)


# ---------------------------------------------------------------------------
# 10: Stub graceful skip
# ---------------------------------------------------------------------------


class StubModeGracefulTests(unittest.TestCase):
    """When OPENAI_API_KEY is unset, run_ai_reasoning short-circuits to
    the unavailable path BEFORE any API call. The aggregator must not
    register a call attempt — observability stays cleanly tied to
    real API invocations."""

    def setUp(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()
        self._saved_key = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()
        if self._saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved_key

    def test_unavailable_path_does_not_touch_aggregator(self):
        import ai_reasoner
        from llm_observability import get_metrics_snapshot

        result = ai_reasoner.run_ai_reasoning(
            news_title="t", news_summary="s", article_body="b",
            policy_claims=[], memory_context="",
        )
        self.assertFalse(result.get("ai_available"))
        self.assertEqual(result.get("ai_status"), "unavailable")
        self.assertEqual(get_metrics_snapshot(), {})


# ---------------------------------------------------------------------------
# 11: llm_judge aggregator hook
# ---------------------------------------------------------------------------


class LlmJudgeAggregatorHookTests(unittest.TestCase):
    """The existing _emit_cost_log in llm_judge.py now also pushes
    into the aggregator. This pin guards against accidental
    regression of that hook."""

    def setUp(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()

    def tearDown(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()

    def test_judge_records_into_aggregator_via_emit_cost_log(self):
        import llm_judge
        from llm_observability import get_metrics_snapshot

        # Construct a synthetic provider whose call() returns a
        # well-formed LLMResponse with tokens — the validator will
        # parse a confirm response. The aggregator hook fires inside
        # _emit_cost_log AFTER the validator runs.
        valid_judge_json = (
            '{"action": "confirm", "new_label": null, '
            '"reason_ko": "ok", "evidence_gaps": []}'
        )

        class _FakeProvider(llm_judge.ReasoningProvider):
            name = "openai"

            def is_available(self):
                return True

            def call(self, request):
                return llm_judge.LLMResponse(
                    raw_text=valid_judge_json,
                    model="gpt-4o-mini",
                    provider="openai",
                    success=True,
                    latency_ms=345,
                    input_tokens=400,
                    output_tokens=80,
                )

        verdict = llm_judge.run_judge(
            llm_judge.JudgeInput(current_label="draft_verified"),
            providers=[_FakeProvider()],
            model="gpt-4o-mini",
        )
        self.assertEqual(verdict.action, "confirm")

        snap = get_metrics_snapshot()
        self.assertIn(
            "llm_judge", snap,
            "_emit_cost_log must push to the aggregator under "
            "caller='llm_judge' — M13.1b-obs hook missing.",
        )
        m = snap["llm_judge"]
        self.assertEqual(m["total_calls"], 1)
        self.assertEqual(m["successful_calls"], 1)
        self.assertEqual(m["total_input_tokens"], 400)
        self.assertEqual(m["total_output_tokens"], 80)
        # 400 input * 0.000150 + 80 output * 0.000600 / 1000 each
        # = 0.00006 + 0.000048 = 0.000108
        self.assertAlmostEqual(
            m["total_estimated_cost_usd"], 0.000108, places=6,
        )


if __name__ == "__main__":
    unittest.main()
