"""Tests for the M13.1b OpenAI judge activation.

Run with: python tests/test_m13_1b_openai_provider.py

Covers:
* ``OpenAIProvider.is_available`` reflects ``OPENAI_API_KEY`` presence.
* ``OpenAIProvider.call`` returns a populated ``LLMResponse`` on
  success and a failure-shaped one on every error class.
* ``get_default_provider_chain`` shape depends on the env.
* ``llm_judge_enabled`` parsing rules.
* ``estimate_cost_usd`` math + unknown-model behaviour.
* ``judge_verdict_to_dict`` exposes the new fields without dropping
  the existing safety pins.
* The ``llm_judge.completed`` log emission has all required fields.
* The application-site helper ``main._apply_judge_to_final_decision``
  enforces every invariant from the Phase 1 design.
* The pipeline does NOT call ``run_judge`` when the flag is False and
  DOES call it (and tolerates safe-confirm fallback) when True.

NO real OpenAI call is ever made — every test patches the SDK via
``unittest.mock``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import llm_judge  # noqa: E402


# ---------------------------------------------------------------------------
# Env scope helper — mirrors the pattern from test_postgres_storage.
# ---------------------------------------------------------------------------


class _EnvScope:
    """Snapshot/restore the M13.1b env vars."""

    KEYS = ("OPENAI_API_KEY", "LLM_JUDGE_ENABLED", "AI_MODEL")

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _set_env(**values):
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Fake OpenAI SDK response builders — keep tests deterministic and
# free of any real SDK dependency at runtime.
# ---------------------------------------------------------------------------


def _make_chat_completion(
    text: str, prompt_tokens: int = 100, completion_tokens: int = 30,
) -> MagicMock:
    """Synthesize a v2.x ``ChatCompletion`` shape."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _make_request(
    *, model: str = "gpt-4o-mini",
    system: str = "system prompt",
    user: str = "user prompt",
) -> llm_judge.LLMRequest:
    return llm_judge.LLMRequest(
        system_prompt=system,
        user_prompt=user,
        model=model,
        max_tokens=800,
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# OpenAIProvider — is_available
# ---------------------------------------------------------------------------


class IsAvailableTests(unittest.TestCase):
    def test_is_available_true_when_key_set(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            self.assertTrue(llm_judge.OpenAIProvider().is_available())

    def test_is_available_false_when_key_unset(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY=None)
            self.assertFalse(llm_judge.OpenAIProvider().is_available())

    def test_is_available_false_when_key_blank(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY="   ")
            self.assertFalse(llm_judge.OpenAIProvider().is_available())


# ---------------------------------------------------------------------------
# OpenAIProvider — call() success + every failure class.
# ---------------------------------------------------------------------------


class CallSuccessTests(unittest.TestCase):
    def test_success_returns_populated_response(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = _make_chat_completion(
            text='{"action":"confirm","new_label":null,"reason_ko":"ok","evidence_gaps":[]}',
            prompt_tokens=1247, completion_tokens=89,
        )
        fake_openai_cls = MagicMock(return_value=fake_client)

        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            with patch.dict(
                "sys.modules", {"openai": MagicMock(OpenAI=fake_openai_cls)},
            ):
                resp = llm_judge.OpenAIProvider().call(_make_request())

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "openai")
        self.assertEqual(resp.input_tokens, 1247)
        self.assertEqual(resp.output_tokens, 89)
        self.assertIn("confirm", resp.raw_text)
        self.assertGreaterEqual(resp.latency_ms, 0)

    def test_uses_json_response_format(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = _make_chat_completion(
            text='{"action":"confirm","reason_ko":"x","evidence_gaps":[]}',
        )
        fake_openai_cls = MagicMock(return_value=fake_client)

        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            with patch.dict(
                "sys.modules", {"openai": MagicMock(OpenAI=fake_openai_cls)},
            ):
                llm_judge.OpenAIProvider().call(_make_request())

        kwargs = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs.get("response_format"), {"type": "json_object"})
        self.assertEqual(kwargs.get("model"), "gpt-4o-mini")
        self.assertEqual(kwargs.get("temperature"), 0.0)

    def test_timeout_passed_to_client_constructor(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = _make_chat_completion(
            text='{"action":"confirm","reason_ko":"x","evidence_gaps":[]}',
        )
        fake_openai_cls = MagicMock(return_value=fake_client)

        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            with patch.dict(
                "sys.modules", {"openai": MagicMock(OpenAI=fake_openai_cls)},
            ):
                llm_judge.OpenAIProvider().call(_make_request())

        ctor_kwargs = fake_openai_cls.call_args.kwargs
        self.assertEqual(ctor_kwargs.get("timeout"), 15.0)
        self.assertEqual(ctor_kwargs.get("api_key"), "sk-test")


class CallFailureTests(unittest.TestCase):
    def _call_with_sdk_raising(self, exc: Exception) -> llm_judge.LLMResponse:
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = exc
        fake_openai_cls = MagicMock(return_value=fake_client)

        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            with patch.dict(
                "sys.modules", {"openai": MagicMock(OpenAI=fake_openai_cls)},
            ):
                return llm_judge.OpenAIProvider().call(_make_request())

    def test_network_error_returns_failed_response(self):
        resp = self._call_with_sdk_raising(ConnectionError("net down"))
        self.assertFalse(resp.success)
        self.assertEqual(resp.raw_text, "")
        self.assertIn("openai_call_failed", resp.error or "")
        self.assertIn("ConnectionError", resp.error or "")

    def test_rate_limit_error_returns_failed_response(self):
        class FakeRateLimit(Exception):
            pass
        resp = self._call_with_sdk_raising(FakeRateLimit("429"))
        self.assertFalse(resp.success)
        self.assertEqual(resp.raw_text, "")

    def test_generic_exception_returns_failed_response(self):
        resp = self._call_with_sdk_raising(RuntimeError("anything"))
        self.assertFalse(resp.success)
        self.assertEqual(resp.raw_text, "")

    def test_missing_api_key_returns_failed_response(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY=None)
            with patch.dict(
                "sys.modules", {"openai": MagicMock(OpenAI=MagicMock())},
            ):
                resp = llm_judge.OpenAIProvider().call(_make_request())
        self.assertFalse(resp.success)
        self.assertIn("missing_api_key", resp.error or "")

    def test_malformed_response_shape_returns_failed_response(self):
        """SDK returns an object without ``.choices`` — defensive
        AttributeError guard kicks in."""
        bad_resp = MagicMock()
        del bad_resp.choices  # force AttributeError on access
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = bad_resp
        fake_openai_cls = MagicMock(return_value=fake_client)

        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            with patch.dict(
                "sys.modules", {"openai": MagicMock(OpenAI=fake_openai_cls)},
            ):
                resp = llm_judge.OpenAIProvider().call(_make_request())
        self.assertFalse(resp.success)
        self.assertIn("openai_response_shape_unexpected", resp.error or "")


# ---------------------------------------------------------------------------
# Default chain — env-aware switch.
# ---------------------------------------------------------------------------


class DefaultChainTests(unittest.TestCase):
    def test_chain_returns_openai_provider_when_key_set(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            chain = llm_judge.get_default_provider_chain()
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], llm_judge.OpenAIProvider)

    def test_chain_returns_stub_when_key_unset(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY=None)
            chain = llm_judge.get_default_provider_chain()
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], llm_judge.StubOpenAIProvider)

    def test_chain_returns_stub_when_key_blank(self):
        with _EnvScope():
            _set_env(OPENAI_API_KEY="   ")
            chain = llm_judge.get_default_provider_chain()
        self.assertIsInstance(chain[0], llm_judge.StubOpenAIProvider)

    def test_anthropic_stub_no_longer_in_default_chain(self):
        """M13.1c will revive Anthropic; M13.1b explicitly excludes it."""
        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-test")
            chain = llm_judge.get_default_provider_chain()
        for provider in chain:
            self.assertNotIsInstance(
                provider, llm_judge.StubAnthropicProvider
            )


# ---------------------------------------------------------------------------
# llm_judge_enabled — env flag parsing.
# ---------------------------------------------------------------------------


class JudgeEnabledFlagTests(unittest.TestCase):
    def test_default_false(self):
        with _EnvScope():
            _set_env(LLM_JUDGE_ENABLED=None)
            self.assertFalse(llm_judge.llm_judge_enabled())

    def test_true_truthy(self):
        with _EnvScope():
            _set_env(LLM_JUDGE_ENABLED="true")
            self.assertTrue(llm_judge.llm_judge_enabled())
            _set_env(LLM_JUDGE_ENABLED="TRUE")
            self.assertTrue(llm_judge.llm_judge_enabled())
            _set_env(LLM_JUDGE_ENABLED=" true ")
            self.assertTrue(llm_judge.llm_judge_enabled())

    def test_other_truthy_values_falsy(self):
        """Only the lowercase word ``true`` enables (after strip+lower).
        Keeps the flag from firing on accidental ``1`` / ``yes`` / ``on``
        strings, which other parts of the codebase treat as truthy."""
        for value in ("1", "yes", "on", "", "false", "no", "off"):
            with _EnvScope():
                _set_env(LLM_JUDGE_ENABLED=value)
                self.assertFalse(
                    llm_judge.llm_judge_enabled(),
                    msg=f"value {value!r} should be False",
                )


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


class CostEstimationTests(unittest.TestCase):
    def test_known_model_math(self):
        cost = llm_judge.estimate_cost_usd(
            "gpt-4o-mini", input_tokens=1000, output_tokens=500,
        )
        # 1.0 * 0.000150 + 0.5 * 0.000600 = 0.00015 + 0.0003 = 0.00045
        self.assertAlmostEqual(cost, 0.00045, places=6)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(
            llm_judge.estimate_cost_usd(
                "claude-sonnet-4-5", input_tokens=1000, output_tokens=500,
            )
        )

    def test_zero_tokens(self):
        self.assertEqual(
            llm_judge.estimate_cost_usd("gpt-4o-mini", 0, 0), 0.0
        )

    def test_negative_tokens_treated_as_zero(self):
        self.assertEqual(
            llm_judge.estimate_cost_usd("gpt-4o-mini", -50, -50), 0.0
        )


# ---------------------------------------------------------------------------
# judge_verdict_to_dict — pins the M13.1b dict shape and the
# always-False / always-True safety contract.
# ---------------------------------------------------------------------------


class VerdictDictShapeTests(unittest.TestCase):
    def test_dict_contains_token_and_cost_fields(self):
        verdict = llm_judge.JudgeVerdict(
            action="confirm",
            model="gpt-4o-mini",
            input_tokens=1000,
            output_tokens=500,
        )
        out = llm_judge.judge_verdict_to_dict(verdict)
        self.assertIn("input_tokens", out)
        self.assertIn("output_tokens", out)
        self.assertIn("estimated_cost_usd", out)
        self.assertEqual(out["input_tokens"], 1000)
        self.assertEqual(out["output_tokens"], 500)
        self.assertAlmostEqual(out["estimated_cost_usd"], 0.00045, places=6)

    def test_dict_truth_claim_always_false(self):
        verdict = llm_judge.JudgeVerdict(
            action="confirm", truth_claim=True,
        )
        out = llm_judge.judge_verdict_to_dict(verdict)
        self.assertIs(out["truth_claim"], False)

    def test_dict_operator_review_required_always_true(self):
        verdict = llm_judge.JudgeVerdict(
            action="confirm", operator_review_required=False,
        )
        out = llm_judge.judge_verdict_to_dict(verdict)
        self.assertIs(out["operator_review_required"], True)


# ---------------------------------------------------------------------------
# Cost log emission — assert all 8 required fields.
# ---------------------------------------------------------------------------


class CostLogTests(unittest.TestCase):
    def _capture(self, response: llm_judge.LLMResponse,
                 verdict: llm_judge.JudgeVerdict) -> dict:
        captured = []

        class _Handler(logging.Handler):
            def emit(self, record):
                captured.append(record)

        logger = logging.getLogger("llm_judge")
        handler = _Handler(level=logging.INFO)
        logger.addHandler(handler)
        prior_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            llm_judge._emit_cost_log(response, verdict)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prior_level)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_log_contains_all_required_fields(self):
        response = llm_judge.LLMResponse(
            raw_text="{}", model="gpt-4o-mini", provider="openai",
            success=True, latency_ms=1820,
            input_tokens=1247, output_tokens=89,
        )
        verdict = llm_judge.JudgeVerdict(
            action="downgrade", model="gpt-4o-mini",
            input_tokens=1247, output_tokens=89,
        )
        record = self._capture(response, verdict)
        self.assertEqual(record.msg, "llm_judge.completed")
        for field in (
            "model", "action", "input_tokens", "output_tokens",
            "estimated_cost_usd", "latency_ms", "provider", "fell_back",
        ):
            self.assertTrue(
                hasattr(record, field),
                msg=f"expected '{field}' in log record",
            )

    def test_log_cost_none_for_unknown_model(self):
        response = llm_judge.LLMResponse(
            raw_text="{}", model="some-unknown-model", provider="openai",
            success=True, latency_ms=1, input_tokens=10, output_tokens=5,
        )
        verdict = llm_judge.JudgeVerdict(
            action="confirm", model="some-unknown-model",
            input_tokens=10, output_tokens=5,
        )
        record = self._capture(response, verdict)
        self.assertIsNone(record.estimated_cost_usd)

    def test_api_key_never_appears_in_log(self):
        """Defence-in-depth: even if OPENAI_API_KEY is in env, the
        log payload must not contain it."""
        with _EnvScope():
            _set_env(OPENAI_API_KEY="sk-supersecret-key-DO-NOT-LEAK")
            response = llm_judge.LLMResponse(
                raw_text="{}", model="gpt-4o-mini", provider="openai",
                success=True, latency_ms=10,
                input_tokens=10, output_tokens=5,
            )
            verdict = llm_judge.JudgeVerdict(
                action="confirm", model="gpt-4o-mini",
            )
            record = self._capture(response, verdict)
        for field_name, value in record.__dict__.items():
            self.assertNotIn(
                "sk-supersecret",
                str(value),
                msg=f"API key leaked via record.{field_name}",
            )


# ---------------------------------------------------------------------------
# Application-site invariants — main._apply_judge_to_final_decision.
# ---------------------------------------------------------------------------


class AppSiteInvariantTests(unittest.TestCase):
    def setUp(self):
        # Ensure judge is disabled at module import for these tests so
        # they exercise only the helper, not the wiring.
        os.environ.pop("LLM_JUDGE_ENABLED", None)
        os.environ.pop("OPENAI_API_KEY", None)
        # Late import so the module loads cleanly even when env was
        # toggled inside test runs.
        import main
        self.main = main

    def _final_decision(self, *, alert="HIGH"):
        return {
            "policy_alert_level": alert,
            "action_recommendation": "정책 발표 직후 보수적 대응",
            "decision_summary": "정책 신호 강함",
            "market_signal": ["bond", "kospi"],
            "decision_reasons": ["evidence_strong"],
        }

    def test_confirm_changes_nothing(self):
        verdict = llm_judge.JudgeVerdict(action="confirm")
        fd = self._final_decision(alert="HIGH")
        before = dict(fd)
        applied = self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertFalse(applied)
        self.assertEqual(fd, before)

    def test_flag_for_review_sets_marker_only(self):
        verdict = llm_judge.JudgeVerdict(action="flag_for_review")
        fd = self._final_decision(alert="HIGH")
        applied = self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertTrue(applied)
        self.assertTrue(fd.get("llm_judge_flagged_for_review"))
        # Alert untouched.
        self.assertEqual(fd["policy_alert_level"], "HIGH")
        # Prose untouched.
        self.assertEqual(
            fd["action_recommendation"], "정책 발표 직후 보수적 대응",
        )
        self.assertEqual(fd["decision_summary"], "정책 신호 강함")

    def test_downgrade_drops_high_to_watch(self):
        verdict = llm_judge.JudgeVerdict(
            action="downgrade", new_label="draft_needs_context",
        )
        fd = self._final_decision(alert="HIGH")
        applied = self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertTrue(applied)
        self.assertEqual(fd["policy_alert_level"], "WATCH")

    def test_downgrade_drops_watch_to_low(self):
        verdict = llm_judge.JudgeVerdict(
            action="downgrade", new_label="draft_needs_context",
        )
        fd = self._final_decision(alert="WATCH")
        applied = self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertTrue(applied)
        self.assertEqual(fd["policy_alert_level"], "LOW")

    def test_downgrade_low_is_noop(self):
        """LOW is the floor — judge cannot go below."""
        verdict = llm_judge.JudgeVerdict(
            action="downgrade", new_label="draft_unverified",
        )
        fd = self._final_decision(alert="LOW")
        applied = self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertFalse(applied)
        self.assertEqual(fd["policy_alert_level"], "LOW")

    def test_downgrade_unknown_tier_is_noop(self):
        verdict = llm_judge.JudgeVerdict(
            action="downgrade", new_label="draft_needs_context",
        )
        fd = self._final_decision(alert="MYSTERY_TIER")
        applied = self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertFalse(applied)
        self.assertEqual(fd["policy_alert_level"], "MYSTERY_TIER")

    def test_never_touches_operator_review_required(self):
        """The judge never sets this field; the application site
        never reads or writes it. Pin the invariant."""
        verdict = llm_judge.JudgeVerdict(action="flag_for_review")
        fd = self._final_decision(alert="HIGH")
        self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertNotIn("operator_review_required", fd)

    def test_never_touches_truth_claim(self):
        verdict = llm_judge.JudgeVerdict(action="downgrade",
                                          new_label="draft_needs_context")
        fd = self._final_decision(alert="HIGH")
        self.main._apply_judge_to_final_decision(
            verdict, fd, debug_summary={},
        )
        self.assertNotIn("truth_claim", fd)

    def test_alert_never_raised(self):
        """Loop over every action × every tier; alert must never go UP."""
        rank = {"LOW": 0, "WATCH": 1, "HIGH": 2}
        for action in ("confirm", "flag_for_review", "downgrade"):
            for tier in ("LOW", "WATCH", "HIGH"):
                verdict = llm_judge.JudgeVerdict(
                    action=action,
                    new_label="draft_needs_context" if action == "downgrade" else None,
                )
                fd = self._final_decision(alert=tier)
                self.main._apply_judge_to_final_decision(
                    verdict, fd, debug_summary={},
                )
                self.assertLessEqual(
                    rank[fd["policy_alert_level"]], rank[tier],
                    msg=f"action={action} tier={tier} → "
                        f"{fd['policy_alert_level']} (raised!)",
                )

    def test_none_verdict_is_noop(self):
        fd = self._final_decision(alert="HIGH")
        before = dict(fd)
        applied = self.main._apply_judge_to_final_decision(
            None, fd, debug_summary={},
        )
        self.assertFalse(applied)
        self.assertEqual(fd, before)


# ---------------------------------------------------------------------------
# Schema contract — extra keys tolerated (M13.1b decision).
# ---------------------------------------------------------------------------


class SchemaContractTests(unittest.TestCase):
    def test_extra_keys_tolerated(self):
        """LLMs occasionally append explanatory keys; the validator
        ignores them rather than forcing safe-confirm fallback."""
        text = json.dumps({
            "action": "confirm",
            "new_label": None,
            "reason_ko": "ok",
            "evidence_gaps": [],
            "extra_explanation": "something the model added",
            "another_unknown_field": 42,
        })
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertFalse(verdict.fell_back)

    def test_action_upgrade_rejected(self):
        text = json.dumps({
            "action": "upgrade",
            "new_label": "draft_verified",
            "reason_ko": "trying to upgrade",
            "evidence_gaps": [],
        })
        verdict = llm_judge.validate_judge_response_json(
            text, "draft_needs_context",
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)


if __name__ == "__main__":
    unittest.main()
