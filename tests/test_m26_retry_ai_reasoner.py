"""M26-retry — ai_reasoner OpenAI client retry/timeout caps.

Mock-driven: NO live OpenAI calls. Tests assert the OpenAI client is
constructed with the env-tunable max_retries + timeout (root cause of the
~90s retry storm was the SDK default max_retries=2 with no explicit cap), and
that ai_reasoner stays verdict-isolated (its return feeds topic/memory/ai_status
only — never a verdict field).
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_reasoner
import config


_AI_REASONER_ENV = ("AI_REASONER_MAX_RETRIES", "AI_REASONER_TIMEOUT_SECONDS")


class _EnvScope:
    """Set/clear specific env vars for the duration of a with-block."""

    def __init__(self, **values):
        self._values = values
        self._saved = {}

    def __enter__(self):
        for key in (*_AI_REASONER_ENV, "OPENAI_API_KEY"):
            self._saved[key] = os.environ.get(key)
        # Clear the knobs first so each test starts from the default baseline.
        for key in _AI_REASONER_ENV:
            os.environ.pop(key, None)
        for key, val in self._values.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        return self

    def __exit__(self, *exc):
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        return False


def _build_client_capture_kwargs():
    """Call get_openai_client() with OpenAI patched to a MagicMock; return the
    kwargs the OpenAI(...) constructor was called with."""
    fake_ctor = MagicMock(return_value=MagicMock())
    with patch.object(ai_reasoner, "OpenAI", fake_ctor):
        client, reason = ai_reasoner.get_openai_client()
    assert reason is None, f"expected client, got reason={reason!r}"
    assert fake_ctor.call_count == 1
    return fake_ctor.call_args.kwargs


class RetryTimeoutConstructionTests(unittest.TestCase):
    def test_defaults_max_retries_1_timeout_20(self):
        with _EnvScope(OPENAI_API_KEY="dummy-test-key"):
            kwargs = _build_client_capture_kwargs()
        self.assertEqual(kwargs.get("max_retries"), 1)
        self.assertEqual(kwargs.get("timeout"), 20.0)
        # config accessors agree with the constructed values.
        self.assertEqual(config.ai_reasoner_max_retries(), 1)
        self.assertEqual(config.ai_reasoner_timeout_seconds(), 20.0)

    def test_env_overrides_flow_through(self):
        with _EnvScope(
            OPENAI_API_KEY="dummy-test-key",
            AI_REASONER_MAX_RETRIES="0",
            AI_REASONER_TIMEOUT_SECONDS="10",
        ):
            kwargs = _build_client_capture_kwargs()
        self.assertEqual(kwargs.get("max_retries"), 0)  # ~20s hard cap
        self.assertEqual(kwargs.get("timeout"), 10.0)

    def test_bad_input_falls_back_to_defaults(self):
        with _EnvScope(
            OPENAI_API_KEY="dummy-test-key",
            AI_REASONER_MAX_RETRIES="garbage",
            AI_REASONER_TIMEOUT_SECONDS="abc",
        ):
            kwargs = _build_client_capture_kwargs()
        self.assertEqual(kwargs.get("max_retries"), 1)
        self.assertEqual(kwargs.get("timeout"), 20.0)

    def test_negative_retries_clamped_to_zero(self):
        with _EnvScope(
            OPENAI_API_KEY="dummy-test-key",
            AI_REASONER_MAX_RETRIES="-5",
        ):
            kwargs = _build_client_capture_kwargs()
        self.assertEqual(kwargs.get("max_retries"), 0)

    def test_model_unchanged_gpt_4o_mini_default(self):
        # Guard against the AI_MODEL="gpt-5.5" history: the default real id
        # stays gpt-4o-mini and this milestone does not touch model selection.
        self.assertEqual(config.DEFAULT_AI_MODEL, "gpt-4o-mini")


class VerdictIsolationContractTests(unittest.TestCase):
    """The retry/timeout change is confined to client construction. The
    run_ai_reasoning return contract (consumed by topic/memory/ai_status) is
    unchanged and never carries a verdict field."""

    _VERDICT_FIELDS = (
        "verdict_label",
        "policy_alert_level",
        "verification_card",
        "policy_confidence",
        "verdict_confidence",
        "final_decision",
    )

    def test_success_returns_ai_result_shape_without_verdict_fields(self):
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_response.output_text = '{"one_line_summary": "s", "main_policy_issue": "x"}'
        fake_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        fake_client.responses.create.return_value = fake_response
        with _EnvScope(OPENAI_API_KEY="dummy-test-key"), \
                patch("ai_reasoner.get_openai_client", return_value=(fake_client, None)):
            result = ai_reasoner.run_ai_reasoning(
                news_title="t",
                news_summary="s",
                article_body="b",
                policy_claims=[],
                memory_context="",
            )
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["ai_status"], "ok")
        self.assertIn("ai_model", result)
        for field in self._VERDICT_FIELDS:
            self.assertNotIn(
                field, result,
                f"ai_reasoner result must stay verdict-isolated; leaked {field!r}",
            )


if __name__ == "__main__":
    unittest.main()
