"""M26-provider-A — ai_reasoner provider-swappable (default OpenAI).

Mock-driven: NO live OpenAI/Anthropic calls.
  * OpenAI path: ai_reasoner.OpenAI is patched to a fake client (Responses API).
  * Anthropic path: a fake ``anthropic`` module is injected so the REAL
    llm_judge.AnthropicProvider runs end-to-end (exercising _strip_json_fences
    and the parametrized client construction) without a live Claude call.

Asserts: default routes to OpenAI with M26-retry caps; anthropic routing applies
the same caps + fence-strips + parses; identical ai_result shape; fallback off by
default / opt-in works; verdict-isolation; judge AnthropicProvider defaults
unchanged; bad provider value falls back to openai.
"""

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_reasoner
import config
import llm_judge


_AI_ENV = (
    "AI_REASONER_PROVIDER",
    "AI_REASONER_FALLBACK_PROVIDER",
    "AI_REASONER_MAX_RETRIES",
    "AI_REASONER_TIMEOUT_SECONDS",
    "AI_REASONER_MAX_OUTPUT_TOKENS",
    "ANTHROPIC_MODEL",
)


class _Env:
    """Clear the M26 knobs, then apply the given overrides for the block."""

    def __init__(self, **values):
        self._values = values
        self._saved = {}
        self._keys = (*_AI_ENV, "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

    def __enter__(self):
        for key in self._keys:
            self._saved[key] = os.environ.get(key)
        for key in _AI_ENV:
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


# ---- fakes ----------------------------------------------------------------


def _fake_openai_client(json_text='{"one_line_summary": "s"}'):
    client = MagicMock()
    resp = MagicMock()
    resp.output_text = json_text
    resp.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)
    client.responses.create.return_value = resp
    return client


def _inject_fake_anthropic(rec, raw_text):
    """Inject a fake `anthropic` module; the REAL AnthropicProvider.call uses
    it, so _strip_json_fences + client-kwarg construction run for real."""
    mod = types.ModuleType("anthropic")

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            rec["client_kwargs"] = kwargs
            self.messages = self

        def create(self, **kw):
            rec["create_kwargs"] = kw
            block = types.SimpleNamespace(text=raw_text)
            usage = types.SimpleNamespace(input_tokens=100, output_tokens=20)
            return types.SimpleNamespace(content=[block], usage=usage)

    mod.Anthropic = _FakeAnthropic
    return mod


def _call(**overrides):
    kwargs = dict(
        news_title="t",
        news_summary="s",
        article_body="b",
        policy_claims=[],
        memory_context="",
    )
    kwargs.update(overrides)
    return ai_reasoner.run_ai_reasoning(**kwargs)


_VERDICT_FIELDS = (
    "verdict_label",
    "policy_alert_level",
    "verification_card",
    "policy_confidence",
    "verdict_confidence",
    "final_decision",
)


class DefaultOpenAITests(unittest.TestCase):
    def test_default_routes_to_openai_with_m26_retry_caps(self):
        fake_ctor = MagicMock(return_value=_fake_openai_client())
        with _Env(OPENAI_API_KEY="dummy"), patch.object(ai_reasoner, "OpenAI", fake_ctor):
            result = _call()
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["ai_status"], "ok")
        self.assertEqual(result["ai_model"], config.AI_MODEL)  # gpt-4o-mini
        # M26-retry caps preserved on the default path.
        ctor_kwargs = fake_ctor.call_args.kwargs
        self.assertEqual(ctor_kwargs.get("max_retries"), 1)
        self.assertEqual(ctor_kwargs.get("timeout"), 20.0)

    def test_default_is_verdict_isolated(self):
        fake_ctor = MagicMock(return_value=_fake_openai_client())
        with _Env(OPENAI_API_KEY="dummy"), patch.object(ai_reasoner, "OpenAI", fake_ctor):
            result = _call()
        for field in _VERDICT_FIELDS:
            self.assertNotIn(field, result)

    def test_bad_provider_value_falls_back_to_openai(self):
        fake_ctor = MagicMock(return_value=_fake_openai_client())
        with _Env(OPENAI_API_KEY="dummy", AI_REASONER_PROVIDER="foo"), \
                patch.object(ai_reasoner, "OpenAI", fake_ctor):
            result = _call()
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["ai_model"], config.AI_MODEL)
        self.assertEqual(fake_ctor.call_count, 1)


class AnthropicRoutingTests(unittest.TestCase):
    def setUp(self):
        self._saved_anthropic = sys.modules.get("anthropic")

    def tearDown(self):
        if self._saved_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._saved_anthropic

    def test_anthropic_routing_applies_caps_and_fence_strips(self):
        rec = {}
        # Fenced JSON exercises the real provider's _strip_json_fences.
        sys.modules["anthropic"] = _inject_fake_anthropic(
            rec, "```json\n{\"one_line_summary\": \"s\"}\n```"
        )
        with _Env(ANTHROPIC_API_KEY="dummy", AI_REASONER_PROVIDER="anthropic"):
            result = _call()
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["ai_status"], "ok")
        self.assertEqual(result["ai_model"], "claude-sonnet-4-6")
        # Same M26-retry caps applied to the Anthropic client.
        self.assertEqual(rec["client_kwargs"].get("timeout"), 20.0)
        self.assertEqual(rec["client_kwargs"].get("max_retries"), 1)
        # Anthropic-path max_tokens comes from the config knob (avoids truncation).
        self.assertEqual(rec["create_kwargs"].get("max_tokens"), 1500)

    def test_anthropic_result_shape_matches_openai(self):
        rec = {}
        sys.modules["anthropic"] = _inject_fake_anthropic(
            rec, '{"one_line_summary": "s"}'
        )
        with _Env(ANTHROPIC_API_KEY="dummy", AI_REASONER_PROVIDER="anthropic"):
            anthropic_result = _call()
        fake_ctor = MagicMock(return_value=_fake_openai_client())
        with _Env(OPENAI_API_KEY="dummy"), patch.object(ai_reasoner, "OpenAI", fake_ctor):
            openai_result = _call()
        # Identical key set -> downstream topic/memory/ai_status consumers see
        # the same shape regardless of provider.
        self.assertEqual(sorted(anthropic_result.keys()), sorted(openai_result.keys()))

    def test_anthropic_is_verdict_isolated(self):
        rec = {}
        sys.modules["anthropic"] = _inject_fake_anthropic(
            rec, '{"one_line_summary": "s"}'
        )
        with _Env(ANTHROPIC_API_KEY="dummy", AI_REASONER_PROVIDER="anthropic"):
            result = _call()
        for field in _VERDICT_FIELDS:
            self.assertNotIn(field, result)


class FallbackTests(unittest.TestCase):
    def test_no_fallback_by_default_on_primary_failure(self):
        # anthropic primary with no key -> unavailable; fallback default "none"
        # -> openai must NOT be attempted (today's single-provider behavior).
        spy = MagicMock(return_value=(None, "missing_api_key"))
        with _Env(AI_REASONER_PROVIDER="anthropic", ANTHROPIC_API_KEY=None), \
                patch.object(ai_reasoner, "get_openai_client", spy):
            result = _call()
        self.assertFalse(result["ai_available"])
        spy.assert_not_called()  # openai never touched

    def test_opt_in_fallback_engages_on_primary_failure(self):
        # anthropic primary fails (no key) -> fallback openai runs and succeeds.
        fake_ctor = MagicMock(return_value=_fake_openai_client())
        with _Env(
            AI_REASONER_PROVIDER="anthropic",
            AI_REASONER_FALLBACK_PROVIDER="openai",
            ANTHROPIC_API_KEY=None,
            OPENAI_API_KEY="dummy",
        ), patch.object(ai_reasoner, "OpenAI", fake_ctor):
            result = _call()
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["ai_model"], config.AI_MODEL)  # came from openai fallback
        # Fallback still applied the M26-retry caps (no storm).
        self.assertEqual(fake_ctor.call_args.kwargs.get("max_retries"), 1)


class JudgeUnchangedTests(unittest.TestCase):
    def setUp(self):
        self._saved_anthropic = sys.modules.get("anthropic")

    def tearDown(self):
        if self._saved_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._saved_anthropic

    def test_anthropic_provider_default_construction_unchanged(self):
        # No-arg construction (how the judge instantiates it) preserves the
        # original timeout and leaves max_retries unset.
        provider = llm_judge.AnthropicProvider()
        self.assertEqual(provider._timeout, llm_judge._ANTHROPIC_TIMEOUT_SECONDS)
        self.assertIsNone(provider._max_retries)

    def test_judge_path_client_omits_max_retries(self):
        rec = {}
        sys.modules["anthropic"] = _inject_fake_anthropic(rec, '{"action": "confirm"}')
        with _Env(ANTHROPIC_API_KEY="dummy"):
            req = llm_judge.LLMRequest(
                system_prompt="s", user_prompt="u", model="claude-sonnet-4-6",
            )
            llm_judge.AnthropicProvider().call(req)
        # Judge byte-identical: timeout=15.0, no max_retries kwarg passed.
        self.assertEqual(rec["client_kwargs"].get("timeout"), 15.0)
        self.assertNotIn("max_retries", rec["client_kwargs"])


if __name__ == "__main__":
    unittest.main()
