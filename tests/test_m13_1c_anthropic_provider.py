"""M13.1c (2026-05-27) — AnthropicProvider + multi-provider abstraction.

Pins:

  1. AnthropicProviderHappyPathTests (2 tests):
     - happy-path mocked call returns well-formed LLMResponse with
       Anthropic field-name token capture (input_tokens /
       output_tokens, not OpenAI's prompt_tokens / completion_tokens)
     - JSON code fences (`````json ... `````) are stripped from
       message.content[0].text before raw_text is returned

  2. AnthropicProviderFailureTests (3 tests):
     - SDK ImportError → failed LLMResponse with
       error="anthropic_sdk_missing"
     - ANTHROPIC_API_KEY unset → failed with error="missing_api_key"
     - client.messages.create raises → caught; failed with
       error="anthropic_call_failed: <ExcType>"

  3. CostCalculationTests (1 test):
     - claude-sonnet-4-6 cost formula matches $3/M input + $15/M
       output (per the 2026-05-27 verified pricing in
       LLM_COST_PER_1K)

  4. ProviderRoutingTests (4 tests):
     - anthropic primary succeeds → openai.call is never invoked,
       verdict.provider_used == "anthropic"
     - anthropic returns success=False → openai is called,
       verdict.provider_used == "openai", llm_judge.fallback_engaged
       log fires with primary/fallback names + failure reason,
       verdict.primary_provider_failed is True
     - LLM_PROVIDER=openai → AnthropicProvider never instantiated,
       chain is [OpenAIProvider]
     - LLM_PROVIDER=disabled → chain is empty, run_judge returns
       safe-confirm fallback verdict

NEVER calls the real Anthropic API — every test patches
``anthropic.Anthropic`` via ``unittest.mock``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import llm_judge  # noqa: E402


# ---------------------------------------------------------------------------
# Env scope helper — local copy so this file stands alone.
# ---------------------------------------------------------------------------


class _EnvScope:
    """Snapshot/restore the env vars this milestone touches."""

    KEYS = (
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "OPENAI_API_KEY", "AI_MODEL",
        "LLM_PROVIDER", "LLM_FALLBACK_PROVIDER",
        "LLM_JUDGE_ENABLED",
    )

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _set_env(**kwargs):
    for key, value in kwargs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


# ---------------------------------------------------------------------------
# Log capture helper (same pattern as M11.7a-2 / M13.1b-obs tests)
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.name == self._name_prefix
            or record.name.startswith(self._name_prefix + ".")
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
# Fakes mimicking anthropic SDK shapes
# ---------------------------------------------------------------------------


class _FakeContentBlock:
    """Mimics anthropic SDK's TextBlock."""

    def __init__(self, text: str):
        self.text = text


class _FakeUsage:
    """Mimics anthropic SDK's Usage block — uses input_tokens /
    output_tokens (NOT prompt_tokens / completion_tokens like OpenAI)."""

    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeAnthropicMessage:
    """Mimics anthropic SDK's Message response."""

    def __init__(self, text: str, input_tokens: int, output_tokens: int):
        self.content = [_FakeContentBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


def _patch_anthropic_client(fake_message=None, raise_on_call=None):
    """Returns a mock.patch context that swaps `anthropic.Anthropic`
    for a constructor returning a client whose .messages.create either
    returns fake_message OR raises raise_on_call."""
    fake_client = mock.MagicMock()
    if raise_on_call is not None:
        fake_client.messages.create.side_effect = raise_on_call
    else:
        fake_client.messages.create.return_value = fake_message
    fake_constructor = mock.MagicMock(return_value=fake_client)
    # We patch the symbol the lazy `from anthropic import Anthropic`
    # inside AnthropicProvider.call() will resolve.
    return mock.patch("anthropic.Anthropic", fake_constructor)


# ---------------------------------------------------------------------------
# 1-2: Happy path
# ---------------------------------------------------------------------------


class AnthropicProviderHappyPathTests(unittest.TestCase):

    def test_call_returns_well_formed_response_with_tokens(self):
        fake_message = _FakeAnthropicMessage(
            text='{"action":"confirm","new_label":null,'
                 '"reason_ko":"ok","evidence_gaps":[]}',
            input_tokens=750,
            output_tokens=120,
        )
        with _EnvScope():
            _set_env(ANTHROPIC_API_KEY="ak-test")
            with _patch_anthropic_client(fake_message):
                provider = llm_judge.AnthropicProvider()
                request = llm_judge.LLMRequest(
                    system_prompt="sys",
                    user_prompt="user",
                    model="claude-sonnet-4-6",
                )
                response = provider.call(request)

        self.assertTrue(response.success)
        self.assertEqual(response.provider, "anthropic")
        self.assertEqual(response.model, "claude-sonnet-4-6")
        self.assertEqual(response.input_tokens, 750)
        self.assertEqual(response.output_tokens, 120)
        # raw_text is the bare JSON (no fence to strip in this case).
        self.assertIn('"action":"confirm"', response.raw_text)
        self.assertGreaterEqual(response.latency_ms, 0)
        self.assertIsNone(response.error)

    def test_strips_json_code_fences_from_response(self):
        """Sonnet sometimes wraps JSON in ```json ... ``` fences;
        AnthropicProvider.call MUST strip them so the existing
        validator sees bare JSON."""
        fenced_payload = (
            "```json\n"
            '{"action":"downgrade","new_label":"draft_needs_context",'
            '"reason_ko":"테스트","evidence_gaps":[]}\n'
            "```"
        )
        fake_message = _FakeAnthropicMessage(
            text=fenced_payload, input_tokens=100, output_tokens=40,
        )
        with _EnvScope():
            _set_env(ANTHROPIC_API_KEY="ak-test")
            with _patch_anthropic_client(fake_message):
                provider = llm_judge.AnthropicProvider()
                response = provider.call(llm_judge.LLMRequest(
                    system_prompt="sys", user_prompt="user",
                    model="claude-sonnet-4-6",
                ))

        self.assertTrue(response.success)
        self.assertFalse(
            response.raw_text.startswith("```"),
            f"raw_text still wrapped in fences: {response.raw_text!r}",
        )
        self.assertFalse(
            response.raw_text.endswith("```"),
            f"raw_text still has trailing fence: {response.raw_text!r}",
        )
        # The bare JSON parses cleanly.
        parsed = json.loads(response.raw_text)
        self.assertEqual(parsed["action"], "downgrade")
        self.assertEqual(parsed["new_label"], "draft_needs_context")


# ---------------------------------------------------------------------------
# 3-5: Failure paths
# ---------------------------------------------------------------------------


class AnthropicProviderFailureTests(unittest.TestCase):

    def test_sdk_import_missing_returns_failed_response(self):
        """When the anthropic SDK is unavailable (ImportError on lazy
        import), AnthropicProvider.call returns a failure-shaped
        LLMResponse — never raises. The error string identifies the
        cause for operator diagnostics."""
        with _EnvScope():
            _set_env(ANTHROPIC_API_KEY="ak-test")
            # Force ImportError on the inner `from anthropic import Anthropic`.
            with mock.patch.dict(sys.modules, {"anthropic": None}):
                provider = llm_judge.AnthropicProvider()
                response = provider.call(llm_judge.LLMRequest(
                    system_prompt="sys", user_prompt="user",
                    model="claude-sonnet-4-6",
                ))

        self.assertFalse(response.success)
        self.assertEqual(response.provider, "anthropic")
        self.assertEqual(response.error, "anthropic_sdk_missing")
        self.assertEqual(response.raw_text, "")

    def test_missing_api_key_returns_failed_response(self):
        with _EnvScope():
            _set_env(ANTHROPIC_API_KEY=None)
            provider = llm_judge.AnthropicProvider()
            response = provider.call(llm_judge.LLMRequest(
                system_prompt="sys", user_prompt="user",
                model="claude-sonnet-4-6",
            ))

        self.assertFalse(response.success)
        self.assertEqual(response.provider, "anthropic")
        self.assertEqual(response.error, "missing_api_key")
        self.assertEqual(response.raw_text, "")

    def test_sdk_call_raises_returns_failed_response(self):
        """When client.messages.create raises, the broad except
        catches and returns a failure-shaped response. The error
        string carries the exception type name but NOT the message
        (which could contain prompt fragments — same PII-protection
        contract as OpenAIProvider)."""
        with _EnvScope():
            _set_env(ANTHROPIC_API_KEY="ak-test")
            with _patch_anthropic_client(
                raise_on_call=RuntimeError("simulated anthropic explosion"),
            ):
                provider = llm_judge.AnthropicProvider()
                response = provider.call(llm_judge.LLMRequest(
                    system_prompt="sys", user_prompt="user",
                    model="claude-sonnet-4-6",
                ))

        self.assertFalse(response.success)
        self.assertEqual(response.provider, "anthropic")
        self.assertTrue(
            response.error.startswith("anthropic_call_failed: "),
            f"unexpected error string: {response.error!r}",
        )
        self.assertIn("RuntimeError", response.error)
        # CRITICAL: the exception MESSAGE must NOT leak into the
        # logged error string (only the type name is captured).
        self.assertNotIn("simulated anthropic explosion", response.error)


# ---------------------------------------------------------------------------
# 6: Cost calculation
# ---------------------------------------------------------------------------


class CostCalculationTests(unittest.TestCase):
    """Sonnet 4.6: $3 / 1M input + $15 / 1M output (verified 2026-05-27
    against https://docs.anthropic.com/en/docs/about-claude/pricing).
    Per-1K convention: 0.003 input + 0.015 output."""

    def test_sonnet_4_6_cost_formula(self):
        # 1000 input + 1000 output → 0.003 + 0.015 = 0.018
        self.assertAlmostEqual(
            llm_judge.estimate_cost_usd("claude-sonnet-4-6", 1000, 1000),
            0.018, places=6,
        )
        # 5000 input + 1000 output → 0.015 + 0.015 = 0.030
        self.assertAlmostEqual(
            llm_judge.estimate_cost_usd("claude-sonnet-4-6", 5000, 1000),
            0.030, places=6,
        )
        # 0 tokens → 0 cost
        self.assertEqual(
            llm_judge.estimate_cost_usd("claude-sonnet-4-6", 0, 0),
            0.0,
        )


# ---------------------------------------------------------------------------
# 7-10: Provider routing
# ---------------------------------------------------------------------------


# Convenience JSON strings for fake provider responses.
_VALID_CONFIRM_JSON = (
    '{"action":"confirm","new_label":null,'
    '"reason_ko":"ok","evidence_gaps":[]}'
)


class _FakeProvider(llm_judge.ReasoningProvider):
    """Test-only provider with deterministic behaviour."""

    def __init__(
        self, name: str, *,
        is_available_value: bool = True,
        success: bool = True,
        raw_text: str = _VALID_CONFIRM_JSON,
        error: str | None = None,
        invocation_log: list[str] | None = None,
    ):
        self.name = name
        self._is_available = is_available_value
        self._success = success
        self._raw_text = raw_text
        self._error = error
        self.invocation_log = invocation_log
        self.calls = 0

    def is_available(self) -> bool:
        return self._is_available

    def call(self, request):
        self.calls += 1
        if self.invocation_log is not None:
            self.invocation_log.append(self.name)
        return llm_judge.LLMResponse(
            raw_text=self._raw_text if self._success else "",
            model=request.model,
            provider=self.name,
            success=self._success,
            error=self._error,
            input_tokens=100 if self._success else 0,
            output_tokens=40 if self._success else 0,
        )


class ProviderRoutingTests(unittest.TestCase):

    def setUp(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()
        self.judge_log = _attach("llm_judge")

    def tearDown(self):
        import llm_observability
        llm_observability.reset_metrics_for_tests()
        _detach("llm_judge", self.judge_log)

    def test_anthropic_primary_succeeds_openai_not_called(self):
        invocation_log: list[str] = []
        anthropic = _FakeProvider(
            "anthropic", success=True, raw_text=_VALID_CONFIRM_JSON,
            invocation_log=invocation_log,
        )
        openai = _FakeProvider(
            "openai", success=True, raw_text=_VALID_CONFIRM_JSON,
            invocation_log=invocation_log,
        )
        verdict = llm_judge.run_judge(
            llm_judge.JudgeInput(current_label="draft_verified"),
            providers=[anthropic, openai],
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertEqual(verdict.provider_used, "anthropic")
        self.assertFalse(verdict.primary_provider_failed)
        # OpenAI MUST not have been invoked at all.
        self.assertEqual(invocation_log, ["anthropic"])
        self.assertEqual(openai.calls, 0)
        # No fallback log when primary succeeded.
        self.assertEqual(
            len(_records_with_event(
                self.judge_log.records, "llm_judge.fallback_engaged",
            )),
            0,
        )

    def test_anthropic_fails_openai_called_fallback_logged(self):
        invocation_log: list[str] = []
        anthropic = _FakeProvider(
            "anthropic", success=False,
            error="anthropic_call_failed: TimeoutError",
            invocation_log=invocation_log,
        )
        openai = _FakeProvider(
            "openai", success=True, raw_text=_VALID_CONFIRM_JSON,
            invocation_log=invocation_log,
        )
        verdict = llm_judge.run_judge(
            llm_judge.JudgeInput(current_label="draft_verified"),
            providers=[anthropic, openai],
        )
        self.assertEqual(verdict.action, "confirm")
        self.assertEqual(verdict.provider_used, "openai")
        self.assertTrue(
            verdict.primary_provider_failed,
            "primary_provider_failed must be True when chain advanced "
            "past slot 0.",
        )
        self.assertEqual(invocation_log, ["anthropic", "openai"])
        # llm_judge.fallback_engaged log must fire with the right fields.
        fallback_records = _records_with_event(
            self.judge_log.records, "llm_judge.fallback_engaged",
        )
        self.assertEqual(len(fallback_records), 1)
        record = fallback_records[0]
        self.assertEqual(getattr(record, "primary_provider"), "anthropic")
        self.assertEqual(getattr(record, "fallback_provider"), "openai")
        self.assertIn(
            "anthropic_call_failed",
            getattr(record, "primary_failure_reason"),
        )

    def test_llm_provider_openai_only_anthropic_never_called(self):
        """LLM_PROVIDER=openai → chain is [OpenAIProvider] only.
        AnthropicProvider never appears."""
        with _EnvScope():
            _set_env(
                LLM_PROVIDER="openai", LLM_FALLBACK_PROVIDER="none",
                ANTHROPIC_API_KEY="ak-test",
                OPENAI_API_KEY="sk-test",
            )
            chain = llm_judge.get_default_provider_chain()
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], llm_judge.OpenAIProvider)
        for provider in chain:
            self.assertNotIsInstance(provider, llm_judge.AnthropicProvider)

    def test_llm_provider_disabled_returns_safe_confirm_fallback(self):
        """LLM_PROVIDER=disabled → empty chain. run_judge returns the
        safe-confirm fallback verdict (action='confirm', fell_back=True)."""
        with _EnvScope():
            _set_env(
                LLM_PROVIDER="disabled", LLM_FALLBACK_PROVIDER=None,
                ANTHROPIC_API_KEY="ak-test",
                OPENAI_API_KEY="sk-test",
            )
            chain = llm_judge.get_default_provider_chain()
            self.assertEqual(chain, [])
            # run_judge with the empty default chain → safe-confirm.
            verdict = llm_judge.run_judge(
                llm_judge.JudgeInput(current_label="draft_verified"),
            )
        self.assertEqual(verdict.action, "confirm")
        self.assertTrue(verdict.fell_back)
        # No primary slot existed, so primary_provider_failed stays False.
        self.assertFalse(verdict.primary_provider_failed)


if __name__ == "__main__":
    unittest.main()
