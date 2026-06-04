"""LLM Judge (M13.1a) — infrastructure only, no pipeline connection.

The Judge is a constrained reasoning step that REVIEWS a deterministic
verdict produced by :func:`verification_card._verdict_label` and can:

* ``confirm`` — accept the verdict as-is.
* ``downgrade`` — replace with a strictly more conservative label.
* ``flag_for_review`` — escalate to the human review queue without
  changing the stored label.

The Judge CANNOT:

* Upgrade a label (e.g. ``draft_needs_context`` → ``draft_verified``
  is mechanically refused by :func:`validate_judge_response_json`).
* Emit any label outside the documented ``draft_*`` set.
* Bypass schema validation — any malformed response falls back to
  ``confirm`` with the failure reason recorded.
* Run during M13.1a's CI / validation — tests use mocked providers
  and the production provider chain is stubs that always report
  ``is_available() == False``.

Designed for M13.1b connection to ``analyze_pipeline`` behind a feature
flag. M13.1a only provides infrastructure plus a dry-run CLI so
operators can observe what the Judge WOULD do against stored
verdicts. The module is **not** imported by ``main.py`` /
``api_server.py`` / any pipeline entry point — that contract is
pinned by static tests in ``tests/test_llm_judge.py``.

Safety invariants
-----------------

* ``truth_claim`` is always ``False`` in every serialised output.
* ``operator_review_required`` is always ``True`` in every serialised
  output.
* No network I/O, no OpenAI / Anthropic imports at module level.
* No public function raises — failures return
  :func:`_safe_confirm_fallback`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from structured_logging import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Label set and conservatism ordering.
#
# The rank table mirrors the descriptive M11.0b ordering documented in
# ``docs/VERDICT_LABEL_DIAGNOSTIC.md``. Lower rank = more conservative.
# ``is_downgrade`` and the schema validator both consult this table; the
# Judge can never produce a label whose rank is higher than the input's
# rank.
# ---------------------------------------------------------------------------


LABEL_SEVERITY_RANK = {
    "draft_unverified": 0,
    "draft_needs_context": 1,
    "draft_needs_review": 1,
    "draft_needs_official_confirmation": 1,
    "draft_disputed": 1,
    "draft_high_risk_review": 1,
    "draft_likely_true": 2,
    "draft_verified": 3,
}


ALLOWED_JUDGE_ACTIONS = frozenset({"confirm", "downgrade", "flag_for_review"})


# Used by the dry-run CLI when an operator passes ``--simulate-downgrade``
# without specifying a target label. Kept here (not in the CLI) so the
# Judge module owns the safe default.
DEFAULT_DOWNGRADE_FALLBACK = "draft_needs_review"


# M13.1b — per-1K-token pricing for cost estimation. Hardcoded so the
# cost log can self-report without an external rate sheet.
#
# OpenAI gpt-4o-mini verified against the OpenAI public pricing page
# on 2026-05-26:
#   gpt-4o-mini → $0.15 / 1M input tokens, $0.60 / 1M output tokens
#   ⇒ $0.000150 / 1K input, $0.000600 / 1K output.
# Sources (M13.1b-obs verification):
#   - https://openai.com/api/pricing/
#   - https://developers.openai.com/api/docs/models/gpt-4o-mini
#
# M13.1c — Anthropic Claude Sonnet 4.6 verified 2026-05-27 against:
#   - https://docs.anthropic.com/en/docs/about-claude/pricing
#   - https://www.anthropic.com/news/claude-sonnet-4-6
#   claude-sonnet-4-6 → $3.00 / 1M input, $15.00 / 1M output
#   ⇒ $0.003 / 1K input, $0.015 / 1K output.
# Pricing matches Sonnet 4.5 (Anthropic kept the same rate card).
# Prompt caching (up to 90% savings) and batch processing (50%
# savings) are NOT applied here — flat per-call rate. Deferred to
# follow-up milestone with explicit operator approval.
#
# Any model not listed produces ``estimated_cost_usd = None`` in the
# log payload rather than guessing. Update this dict (not call sites)
# when a provider changes pricing or when a new model is enabled, and
# refresh the verification date above in the same PR.
LLM_COST_PER_1K = {
    "gpt-4o-mini": {"input": 0.000150, "output": 0.000600},
    "claude-sonnet-4-6": {"input": 0.003000, "output": 0.015000},
}


def llm_judge_enabled() -> bool:
    """Returns True iff env var ``LLM_JUDGE_ENABLED`` equals ``"true"``
    (case-insensitive, leading/trailing whitespace stripped).

    Defaults to False so the pipeline behaves byte-identically to
    pre-M13.1b until an operator opts in via the Render dashboard.
    Read lazily on every call so toggling the env var does not require
    a process restart.
    """
    return os.environ.get("LLM_JUDGE_ENABLED", "").strip().lower() == "true"


def llm_judge_prejudge_enabled() -> bool:
    """Returns True iff env var ``LLM_JUDGE_PREJUDGE_ENABLED`` equals
    ``"true"`` (case-insensitive, leading/trailing whitespace stripped).

    M22-2 — gates the SEPARATE, record-only PRE-verdict judge invocation
    in ``main._process_news_item_phase_a``. It is INDEPENDENT of
    :func:`llm_judge_enabled` (which gates the existing post-verdict
    binding block): the pre-verdict judge writes ONLY to
    ``debug_summary["llm_judge_prejudge"]`` and has zero influence on any
    verdict field. Defaults to False so the pipeline behaves
    byte-identically until an operator opts in via the Render dashboard.
    Read lazily on every call so toggling the env var does not require a
    process restart.
    """
    return (
        os.environ.get("LLM_JUDGE_PREJUDGE_ENABLED", "").strip().lower()
        == "true"
    )


def llm_judge_prejudge_binding_enabled() -> bool:
    """Returns True iff env var ``LLM_JUDGE_PREJUDGE_BINDING_ENABLED`` equals
    ``"true"`` (case-insensitive, leading/trailing whitespace stripped).

    M22-3a — gates the GUARDED, downgrade-only BINDING behavior of the
    pre-verdict judge. Layered ON TOP of :func:`llm_judge_prejudge_enabled`:
    the binding path is active only when BOTH flags are true, so an operator
    can run the pre-verdict judge record-only (observe ``debug_summary[
    "llm_judge_prejudge"]``) and enable verdict-binding independently later.
    Defaults to False so the pipeline behaves byte-identically (record-only,
    then ultimately HEAD) until an operator opts in via the Render dashboard.
    Read lazily on every call so toggling the env var does not require a
    process restart.
    """
    return (
        os.environ.get("LLM_JUDGE_PREJUDGE_BINDING_ENABLED", "")
        .strip()
        .lower()
        == "true"
    )


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int,
) -> Optional[float]:
    """Compute an estimated USD cost for a single Judge call.

    Returns ``None`` when the model is not in :data:`LLM_COST_PER_1K` —
    a missing rate is reported honestly rather than silently guessed.
    """
    rates = LLM_COST_PER_1K.get(model)
    if rates is None:
        return None
    input_cost = (max(0, int(input_tokens)) / 1000.0) * rates["input"]
    output_cost = (max(0, int(output_tokens)) / 1000.0) * rates["output"]
    return round(input_cost + output_cost, 6)


def _max_rank() -> int:
    return max(LABEL_SEVERITY_RANK.values())


def is_downgrade(from_label: str, to_label: str) -> bool:
    """Returns True iff ``to_label`` is *strictly* more conservative than
    ``from_label`` per :data:`LABEL_SEVERITY_RANK`.

    Equality is **not** a downgrade — a same-rank lateral move is
    refused by the schema validator. Unknown labels are treated as
    ``_max_rank() + 1`` so any change to a known label appears as a
    downgrade target (forcing explicit handling rather than silently
    accepting unknown labels).
    """
    unknown = _max_rank() + 1
    from_rank = LABEL_SEVERITY_RANK.get(from_label, unknown)
    to_rank = LABEL_SEVERITY_RANK.get(to_label, unknown)
    return to_rank < from_rank


# ---------------------------------------------------------------------------
# Provider abstraction.
#
# Stubs in M13.1a — both return ``is_available() == False`` and the
# fallback path in ``run_judge`` produces a safe-confirm verdict. M13.1b
# will replace these with real Anthropic / OpenAI clients behind a
# feature flag. Importantly, no LLM SDK is imported at module load —
# real implementations will lazy-import inside ``call`` so the M13.1a
# module stays free of any anthropic / openai dependency.
# ---------------------------------------------------------------------------


@dataclass
class LLMRequest:
    """One Judge query."""

    system_prompt: str
    user_prompt: str
    model: str
    max_tokens: int = 800
    temperature: float = 0.0


@dataclass
class LLMResponse:
    """One provider response.

    ``input_tokens`` / ``output_tokens`` are added in M13.1b so the
    cost log can report real token usage from providers that return
    a ``usage`` block (OpenAI v2.x SDK does). Stubs and providers
    without usage data leave the fields at ``0``.
    """

    raw_text: str
    model: str
    provider: str
    success: bool
    error: Optional[str] = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class ReasoningProvider:
    """Abstract base — concrete implementations land in M13.1b."""

    name: str = "abstract"

    def is_available(self) -> bool:
        return False

    def call(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class StubAnthropicProvider(ReasoningProvider):
    """Placeholder for the M13.1b Anthropic implementation."""

    name = "anthropic_stub"

    def is_available(self) -> bool:
        return False

    def call(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            raw_text="",
            model=request.model,
            provider=self.name,
            success=False,
            error=(
                "stub provider — M13.1a infrastructure only, "
                "no real calls"
            ),
        )


class StubOpenAIProvider(ReasoningProvider):
    """M13.1b fallback when ``OPENAI_API_KEY`` is unset.

    Kept in the chain for offline / CI environments so the Judge always
    has a provider to consult. ``is_available`` returns False, which
    drives ``run_judge`` to the safe-confirm fallback — pipeline output
    stays byte-identical to pre-M13.1b when no key is available.
    """

    name = "openai_stub"

    def is_available(self) -> bool:
        return False

    def call(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            raw_text="",
            model=request.model,
            provider=self.name,
            success=False,
            error=(
                "stub provider — no OPENAI_API_KEY set, "
                "no real calls"
            ),
        )


# ---------------------------------------------------------------------------
# M13.1b — Real OpenAI provider.
#
# The class lazy-imports the ``openai`` SDK inside ``call`` so the
# llm_judge module's import surface stays free of any LLM dependency
# (the M13.1a static test pins this). ``is_available`` only reads the
# env var — the SDK is imported on demand.
# ---------------------------------------------------------------------------


_OPENAI_TIMEOUT_SECONDS = 15.0

# M13.1c — Anthropic uses the same 15s budget as OpenAI; Sonnet 4.6
# is generally slower per-call than gpt-4o-mini but well under 15s
# for the short Judge prompt. Observability will surface real p95.
_ANTHROPIC_TIMEOUT_SECONDS = 15.0


# M13.1c — strip ```json ... ``` and ``` ... ``` code-fence wrappers
# that Anthropic Sonnet sometimes emits around its JSON response.
# OpenAI's `response_format={"type":"json_object"}` already returns
# bare JSON so this helper is only meaningful inside AnthropicProvider.
# Falls back to the original text when no fence is found.
_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL,
)


def _strip_json_fences(text: str) -> str:
    if not text:
        return text
    match = _JSON_FENCE_RE.match(text)
    if match:
        return match.group(1)
    return text


def _failed_response_for(
    request: "LLMRequest", provider_name: str, reason: str,
) -> "LLMResponse":
    """Generalised failure-shaped LLMResponse used by both OpenAI and
    Anthropic providers. Empty ``raw_text`` plus ``success=False``
    drives ``run_judge`` past this provider to the next chain entry
    (or the safe-confirm fallback)."""
    return LLMResponse(
        raw_text="",
        model=request.model,
        provider=provider_name,
        success=False,
        error=reason,
    )


def _failed_response(request: "LLMRequest", reason: str) -> "LLMResponse":
    """Compat shim — preserves the M13.1b shape of the OpenAI-only
    failure helper. New providers call :func:`_failed_response_for`
    directly with their own provider name."""
    return _failed_response_for(request, "openai", reason)


class OpenAIProvider(ReasoningProvider):
    """Real OpenAI Chat Completions caller (gpt-4o-mini by default).

    Activation requires BOTH ``OPENAI_API_KEY`` set in the environment
    AND ``LLM_JUDGE_ENABLED=true`` at the pipeline call site (the
    second guard lives in main.py). ``is_available`` only checks the
    key — the pipeline-level flag is checked by ``main._process_news_item_phase_a``
    so the dry-run CLI can exercise the real provider with the key
    set even when the pipeline flag is off.

    NEVER raises. Any SDK error (import, auth, network, rate limit,
    timeout, shape mismatch) returns a failure-shaped LLMResponse so
    ``run_judge`` falls back to safe-confirm cleanly.
    """

    name = "openai"

    def is_available(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())

    def call(self, request: LLMRequest) -> LLMResponse:
        try:
            from openai import OpenAI  # lazy import
        except ImportError:
            return _failed_response(request, "openai_sdk_missing")

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return _failed_response(request, "missing_api_key")

        start = time.time()
        try:
            client = OpenAI(api_key=api_key, timeout=_OPENAI_TIMEOUT_SECONDS)
            response = client.chat.completions.create(
                model=request.model,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 — never propagate
            # Exception type is logged but the full message (which may
            # quote prompt fragments) is intentionally NOT captured —
            # cuts the chance of leaking prompt PII into logs.
            return _failed_response(
                request, f"openai_call_failed: {type(exc).__name__}"
            )
        latency_ms = int((time.time() - start) * 1000)

        try:
            text = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            input_tokens = (
                getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
            )
            output_tokens = (
                getattr(usage, "completion_tokens", 0) or 0 if usage else 0
            )
        except (AttributeError, IndexError, TypeError):
            return _failed_response(
                request, "openai_response_shape_unexpected"
            )

        return LLMResponse(
            raw_text=text,
            model=request.model,
            provider=self.name,
            success=True,
            latency_ms=latency_ms,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
        )


# ---------------------------------------------------------------------------
# M13.1c — Real Anthropic provider (Claude Sonnet 4.6 primary).
#
# Mirrors OpenAIProvider's shape: lazy SDK import, env-key activation,
# NEVER-raises contract, structured failure responses.
#
# Key differences from OpenAI Chat Completions surfaced here:
#   * system prompt is passed at the top level as ``system=`` (not in
#     the messages array).
#   * usage fields are ``input_tokens`` / ``output_tokens`` (not
#     ``prompt_tokens`` / ``completion_tokens``).
#   * text is at ``message.content[0].text`` (not
#     ``choices[0].message.content``).
#   * no native ``response_format={"type":"json_object"}`` — JSON
#     output is requested via the existing prompt + Pydantic-style
#     validator. Sonnet sometimes wraps JSON in ```json fences; we
#     strip them via :func:`_strip_json_fences` BEFORE returning so
#     the validator sees bare JSON.
# ---------------------------------------------------------------------------


def _resolve_anthropic_model(request_model: Optional[str]) -> str:
    """Provider-owned model resolution. If main.py passed a Claude
    model id (begins with ``claude-``), honour it; otherwise fall
    back to ``ANTHROPIC_MODEL`` env (default ``claude-sonnet-4-6``).
    Same pattern keeps OpenAIProvider's model resolution untouched
    when it receives an OpenAI model id like ``gpt-4o-mini``."""
    if request_model and str(request_model).startswith("claude-"):
        return str(request_model)
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-4-6"


class AnthropicProvider(ReasoningProvider):
    """Real Anthropic Messages API caller (claude-sonnet-4-6 by default).

    Activation requires BOTH ``ANTHROPIC_API_KEY`` set in the environment
    AND ``LLM_JUDGE_ENABLED=true`` at the pipeline call site (the second
    guard lives in main.py). ``is_available`` only checks the key.

    NEVER raises. Any SDK error (import, auth, network, rate limit,
    timeout, shape mismatch) returns a failure-shaped LLMResponse so
    ``run_judge`` falls back cleanly — to OpenAIProvider in the
    M13.1c default chain, then to safe-confirm.
    """

    name = "anthropic"

    def __init__(
        self,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        # M26-provider-A: optional reliability knobs so ai_reasoner can reuse
        # this provider with its M26-retry caps. Defaults preserve the judge's
        # original behavior EXACTLY: timeout falls back to
        # _ANTHROPIC_TIMEOUT_SECONDS, and when max_retries is None the
        # max_retries kwarg is NOT passed to the SDK (so the SDK default is
        # used, identical to pre-M26-provider-A). The judge instantiates
        # AnthropicProvider() with no args → byte-identical.
        self._timeout = timeout if timeout is not None else _ANTHROPIC_TIMEOUT_SECONDS
        self._max_retries = max_retries

    def is_available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def call(self, request: LLMRequest) -> LLMResponse:
        try:
            from anthropic import Anthropic  # lazy import
        except ImportError:
            return _failed_response_for(
                request, "anthropic", "anthropic_sdk_missing",
            )

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return _failed_response_for(
                request, "anthropic", "missing_api_key",
            )

        model = _resolve_anthropic_model(request.model)
        start = time.time()
        try:
            client_kwargs = {"api_key": api_key, "timeout": self._timeout}
            if self._max_retries is not None:
                client_kwargs["max_retries"] = self._max_retries
            client = Anthropic(**client_kwargs)
            message = client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                system=request.system_prompt,
                messages=[{"role": "user", "content": request.user_prompt}],
                temperature=request.temperature,
            )
        except Exception as exc:  # noqa: BLE001 — never propagate
            # Exception type is logged but the full message (which may
            # quote prompt fragments) is intentionally NOT captured —
            # cuts the chance of leaking prompt PII into logs. Same
            # contract as OpenAIProvider.call.
            return _failed_response_for(
                request, "anthropic",
                f"anthropic_call_failed: {type(exc).__name__}",
            )
        latency_ms = int((time.time() - start) * 1000)

        try:
            content_blocks = getattr(message, "content", None) or []
            raw_text = ""
            if content_blocks:
                first_block = content_blocks[0]
                raw_text = getattr(first_block, "text", "") or ""
            raw_text = _strip_json_fences(raw_text)
            usage = getattr(message, "usage", None)
            input_tokens = (
                getattr(usage, "input_tokens", 0) or 0 if usage else 0
            )
            output_tokens = (
                getattr(usage, "output_tokens", 0) or 0 if usage else 0
            )
        except (AttributeError, IndexError, TypeError):
            return _failed_response_for(
                request, "anthropic",
                "anthropic_response_shape_unexpected",
            )

        return LLMResponse(
            raw_text=raw_text,
            model=model,
            provider=self.name,
            success=True,
            latency_ms=latency_ms,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
        )


def _resolve_provider_instance(name: str) -> Optional[ReasoningProvider]:
    """Map a provider name (from env vars) to a real provider instance.
    Returns None when the name is ``none`` / ``disabled`` / unknown —
    callers treat that as "no provider in this slot"."""
    label = (name or "").strip().lower()
    if label == "anthropic":
        return AnthropicProvider()
    if label == "openai":
        return OpenAIProvider()
    # ``none`` / ``disabled`` / empty / unknown → drop the slot.
    return None


def get_default_provider_chain() -> list:
    """Returns the provider chain in priority order.

    M13.1c env-driven routing:

    * ``LLM_PROVIDER`` controls the PRIMARY provider:
        - ``anthropic`` (default if unset) → AnthropicProvider primary
        - ``openai``                       → OpenAIProvider primary
        - ``disabled``                     → empty chain (run_judge
          immediately returns the safe-confirm fallback — equivalent
          to having no provider available)
    * ``LLM_FALLBACK_PROVIDER`` controls the SECONDARY slot:
        - ``openai`` (default if unset) → OpenAIProvider in slot 2
        - ``anthropic``                 → AnthropicProvider in slot 2
        - ``none``                      → no slot 2 (primary-only chain)

    Providers whose API key is unset still appear in the chain — their
    ``is_available`` returns False and ``run_judge`` advances past
    them. This means an operator can pre-configure the chain even
    before adding keys.

    Stubs are NOT in the active chain in M13.1c. They remain available
    for the dry-run CLI to use explicitly via its ``--provider`` flag.

    M13.1b backward compat: with both API keys set on Render and
    ``LLM_PROVIDER`` unset, the chain is [Anthropic, OpenAI]. This IS
    a behavioral change from M13.1b (which was OpenAI-only). To restore
    M13.1b behavior, the operator sets ``LLM_PROVIDER=openai`` in the
    Render dashboard.
    """
    primary_name = os.environ.get("LLM_PROVIDER", "anthropic")
    fallback_name = os.environ.get("LLM_FALLBACK_PROVIDER", "openai")

    chain: list = []
    primary = _resolve_provider_instance(primary_name)
    if primary is not None:
        chain.append(primary)

    # Skip the fallback slot when LLM_PROVIDER is disabled — there's
    # no primary to "fall back FROM", so an explicitly-disabled
    # chain means "no LLM at all".
    if (primary_name or "").strip().lower() != "disabled":
        fallback = _resolve_provider_instance(fallback_name)
        # Avoid putting the same provider twice (e.g.
        # LLM_PROVIDER=openai + LLM_FALLBACK_PROVIDER=openai).
        if fallback is not None and (
            primary is None or type(fallback) is not type(primary)
        ):
            chain.append(fallback)

    return chain


# ---------------------------------------------------------------------------
# Judge prompt and schema.
#
# The system prompt is in Korean because the platform operates on
# Korean policy / news claims. The user prompt is a template populated
# from a :class:`JudgeInput` so the operator gets a deterministic
# rendering for the dry-run CLI's diff display.
# ---------------------------------------------------------------------------


JUDGE_SYSTEM_PROMPT_KO = """\
당신은 한국 정책/뉴스 검증 플랫폼의 LLM Judge입니다.

당신의 역할:
- 결정론적 검증 시스템이 이미 산출한 draft_* 라벨을 검토합니다.
- 라벨을 더 보수적으로 변경(downgrade)하거나, 사람 검토(flag_for_review)를 요청하거나, 그대로 confirm 할 수 있습니다.
- 라벨을 더 강한 방향으로 변경(upgrade)할 수 없습니다.
- 어떤 새로운 사실도 만들지 마십시오. 제공된 evidence만 사용하십시오.

라벨 보수성 순서 (낮을수록 보수적):
- 0: draft_unverified
- 1: draft_needs_context / draft_needs_review / draft_needs_official_confirmation / draft_disputed / draft_high_risk_review
- 2: draft_likely_true
- 3: draft_verified

출력은 반드시 JSON 형식이어야 하며, 다음 스키마를 따라야 합니다:
{
  "action": "confirm" | "downgrade" | "flag_for_review",
  "new_label": "<draft_* label if action is downgrade, else null>",
  "reason_ko": "<한국어로 짧은 사유 (최대 200자)>",
  "evidence_gaps": ["<누락된 증거 항목 리스트, 0~5개>"]
}

규칙:
- action='downgrade'일 때만 new_label을 채우십시오. new_label은 현재 라벨보다 *반드시* 보수적이어야 합니다 (downgrade-only).
- evidence가 약하거나 모순되거나 공식 출처가 없으면 downgrade하십시오.
- 판단이 불확실하면 flag_for_review를 선택하십시오. 라벨을 임의로 바꾸지 마십시오.
- 어떠한 경우에도 truth_claim을 True로 가정하지 마십시오.
"""


JUDGE_USER_PROMPT_TEMPLATE_KO = """\
다음은 검토 대상 분석입니다.

[현재 라벨]
{current_label}

[현재 신뢰도 점수]
{policy_confidence_score}

[검증 강도]
{verification_strength}

[주장 텍스트]
{claim_text}

[공식 출처 개수]
{official_sources_count}

[근거 요약]
{evidence_summary}

[모순 신호 요약]
{contradiction_summary}

[편향/프레이밍 신호 요약]
{bias_framing_summary}

위 정보를 바탕으로 JSON 응답을 출력하십시오.
다른 텍스트는 출력하지 마십시오.
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class JudgeInput:
    """Inputs to one Judge invocation. All fields default to safe
    placeholders so a partially-populated SQLite row never crashes the
    prompt builder."""

    current_label: str
    policy_confidence_score: Optional[int] = None
    verification_strength: Optional[str] = None
    claim_text: Optional[str] = None
    official_sources_count: int = 0
    evidence_summary: Optional[str] = None
    contradiction_summary: Optional[str] = None
    bias_framing_summary: Optional[str] = None


@dataclass
class JudgeVerdict:
    """The Judge's decision on one input.

    ``truth_claim`` and ``operator_review_required`` are kept on the
    dataclass for completeness but the canonical safety values are
    re-asserted at serialisation time in :func:`judge_verdict_to_dict`.

    M13.1b adds ``input_tokens`` / ``output_tokens`` so the pipeline's
    ``debug_summary["llm_judge"]`` payload can surface real usage. Both
    default to 0 (stubs and fallbacks report no tokens).
    """

    action: str
    new_label: Optional[str] = None
    reason_ko: str = ""
    evidence_gaps: list = field(default_factory=list)
    raw_response: Optional[str] = None
    provider_used: Optional[str] = None
    model: Optional[str] = None
    latency_ms: int = 0
    fell_back: bool = False
    fallback_reason: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    # M13.1c — true iff the primary provider in the chain failed and
    # the chain advanced to a secondary provider (or to safe-confirm).
    # Distinct from ``fell_back`` which is True only when the verdict
    # itself IS the safe-confirm fallback (LLM unreachable / output
    # rejected). The two can both be True (primary failed + fallback
    # also failed → safe-confirm). M13.1b tests check ``fell_back``
    # alone and remain unaffected.
    primary_provider_failed: bool = False
    # Safety pins — always False / True regardless of LLM output.
    truth_claim: bool = False
    operator_review_required: bool = True


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _safe_confirm_fallback(reason: str) -> JudgeVerdict:
    """The single shared fallback path. Returned whenever the LLM is
    unavailable, the response is malformed, the schema is invalid, or
    the model tried to upgrade."""
    return JudgeVerdict(
        action="confirm",
        new_label=None,
        reason_ko=f"LLM 폴백: {reason}"[:200],
        evidence_gaps=[],
        fell_back=True,
        fallback_reason=reason,
    )


def validate_judge_response_json(
    text: str, current_label: str,
) -> JudgeVerdict:
    """Parses and validates the LLM's JSON response.

    Returns a :class:`JudgeVerdict`. On ANY failure — empty text, parse
    error, wrong type, invalid action, missing or invalid new_label,
    or a refused upgrade attempt — returns :func:`_safe_confirm_fallback`
    with the failure reason recorded. NEVER raises.

    This function is the security boundary: even if the LLM returns the
    string ``"upgrade"`` or wraps an upgrade attempt inside a
    ``downgrade`` action, the validator refuses and the operator sees
    ``confirm`` instead.

    Extra keys in the response are intentionally TOLERATED (M13.1b
    decision): LLMs occasionally append explanatory keys, and rejecting
    them would force unnecessary safe-confirm fallbacks. The action +
    new_label invariants are still enforced strictly.
    """
    if not text or not str(text).strip():
        return _safe_confirm_fallback("empty response")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _safe_confirm_fallback(f"json parse error: {exc.msg}")
    except Exception as exc:  # noqa: BLE001 — never propagate
        return _safe_confirm_fallback(f"unexpected parse error: {exc}")

    if not isinstance(data, dict):
        return _safe_confirm_fallback("response not a JSON object")

    action = data.get("action")
    if action not in ALLOWED_JUDGE_ACTIONS:
        return _safe_confirm_fallback(f"invalid action: {action!r}")

    reason_raw = data.get("reason_ko") or data.get("reason") or ""
    if not isinstance(reason_raw, str):
        reason_raw = str(reason_raw)
    reason = reason_raw[:200]

    evidence_gaps_raw = data.get("evidence_gaps") or []
    if not isinstance(evidence_gaps_raw, list):
        evidence_gaps_raw = []
    evidence_gaps = [str(g)[:100] for g in evidence_gaps_raw[:5]]

    new_label = data.get("new_label")
    if action == "downgrade":
        if not new_label or new_label not in LABEL_SEVERITY_RANK:
            return _safe_confirm_fallback(
                f"downgrade with invalid new_label: {new_label!r}"
            )
        if not is_downgrade(current_label, new_label):
            # CRITICAL: model tried to upgrade or move laterally.
            return _safe_confirm_fallback(
                f"refused upgrade attempt: {current_label} -> {new_label}"
            )
    else:
        new_label = None

    return JudgeVerdict(
        action=action,
        new_label=new_label,
        reason_ko=reason,
        evidence_gaps=evidence_gaps,
        raw_response=text,
    )


# ---------------------------------------------------------------------------
# Judge entry point
# ---------------------------------------------------------------------------


DEFAULT_JUDGE_MODEL = "claude-sonnet-4-5"


def _coerce_summary_text(value, limit: int) -> str:
    """Render a Judge summary field to a truncated display string.

    The live pipeline passes dict summaries (from
    ``summarize_contradiction_checks`` / ``summarize_bias_framing``)
    while the dry-run CLI passes JSON strings read from the DB. Both
    must render without raising — slicing a dict (``some_dict[:400]``)
    raises ``KeyError: slice(None, 400, None)`` because ``dict`` treats
    the slice object as a key.

    Falsy (``None`` / ``{}`` / ``""``) → the safe placeholder, exactly
    preserving the prior ``(x or "정보 없음")`` semantics. For a truthy
    ``str`` the result is byte-identical to the previous
    ``value[:limit]``.
    """
    if not value:
        return "정보 없음"
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    return value[:limit]


def build_judge_request(
    judge_input: JudgeInput, model: str = DEFAULT_JUDGE_MODEL,
) -> LLMRequest:
    """Construct the :class:`LLMRequest` for one Judge query.

    Long fields are truncated so a runaway article body cannot blow up
    the token budget when M13.1b wires in real providers. The Judge is
    intentionally short-context — its job is to react to the summary
    fields produced by the deterministic pipeline, not to re-read the
    article.

    Summary fields are coerced via :func:`_coerce_summary_text` so a
    dict-shaped summary from the live pipeline renders as JSON instead
    of raising ``KeyError`` on the truncation slice.
    """
    claim_text = _coerce_summary_text(judge_input.claim_text, 1000)
    evidence_summary = _coerce_summary_text(judge_input.evidence_summary, 800)
    contradiction_summary = _coerce_summary_text(
        judge_input.contradiction_summary, 400,
    )
    bias_framing_summary = _coerce_summary_text(
        judge_input.bias_framing_summary, 400,
    )
    score_value = (
        judge_input.policy_confidence_score
        if judge_input.policy_confidence_score is not None
        else "정보 없음"
    )
    user_prompt = JUDGE_USER_PROMPT_TEMPLATE_KO.format(
        current_label=judge_input.current_label,
        policy_confidence_score=score_value,
        verification_strength=judge_input.verification_strength or "정보 없음",
        claim_text=claim_text,
        official_sources_count=judge_input.official_sources_count,
        evidence_summary=evidence_summary,
        contradiction_summary=contradiction_summary,
        bias_framing_summary=bias_framing_summary,
    )
    return LLMRequest(
        system_prompt=JUDGE_SYSTEM_PROMPT_KO,
        user_prompt=user_prompt,
        model=model,
        max_tokens=800,
        temperature=0.0,
    )


def run_judge(
    judge_input: JudgeInput,
    providers: Optional[list] = None,
    model: str = DEFAULT_JUDGE_MODEL,
) -> JudgeVerdict:
    """Invoke the Judge against the provider chain.

    NEVER raises. On any failure — provider unavailable, provider
    crash, provider returns ``success=False``, malformed JSON,
    schema-invalid output, or refused upgrade — returns the safe
    confirm fallback with a descriptive ``fallback_reason``.

    A provider that crashes is treated as a transport failure and the
    chain advances to the next provider. A provider that *succeeds*
    but returns content the validator rejects is treated as a *content
    failure*: the chain does NOT advance (the model spoke, it just
    spoke badly), so the operator sees the validator's verdict.
    """
    if providers is None:
        providers = get_default_provider_chain()

    request = build_judge_request(judge_input, model=model)

    last_error = None
    # M13.1c — track which provider attempts failed so we can:
    #   (a) set verdict.primary_provider_failed when the chain
    #       advances past the first slot, and
    #   (b) emit one `llm_judge.fallback_engaged` log per fallback
    #       attempt so operators can see why we engaged secondary.
    primary_name: Optional[str] = (
        providers[0].name if providers else None
    )
    primary_failure_reason: Optional[str] = None

    for slot_index, provider in enumerate(providers):
        is_primary = (slot_index == 0)

        if not provider.is_available():
            last_error = f"{provider.name} unavailable"
            if is_primary:
                primary_failure_reason = last_error
            else:
                _emit_fallback_engaged_log(
                    primary_name, primary_failure_reason,
                    provider.name, "skipped: " + last_error,
                )
            continue
        try:
            start = time.time()
            response = provider.call(request)
            latency_ms = int((time.time() - start) * 1000)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{provider.name} crashed: {exc}"
            if is_primary:
                primary_failure_reason = last_error
            continue

        if response is None or not response.success:
            err = (response.error if response is not None else "no response")
            last_error = f"{provider.name} returned error: {err}"
            if is_primary:
                primary_failure_reason = last_error
            continue

        # Provider returned success — if we've advanced past the primary
        # slot, that means primary failed and we're engaging the
        # fallback. Emit the structured log BEFORE returning so the
        # operator-visible record sits next to the corresponding
        # `llm_judge.completed` line.
        if not is_primary:
            _emit_fallback_engaged_log(
                primary_name, primary_failure_reason,
                provider.name, None,
            )

        verdict = validate_judge_response_json(
            response.raw_text, judge_input.current_label,
        )
        verdict.provider_used = response.provider
        verdict.model = response.model
        verdict.latency_ms = latency_ms
        verdict.input_tokens = int(response.input_tokens or 0)
        verdict.output_tokens = int(response.output_tokens or 0)
        verdict.primary_provider_failed = not is_primary
        _emit_cost_log(response, verdict)
        return verdict

    verdict = _safe_confirm_fallback(last_error or "no available provider")
    # If the primary slot existed and failed, that path is recorded too.
    verdict.primary_provider_failed = bool(primary_failure_reason)
    return verdict


def _emit_fallback_engaged_log(
    primary_name: Optional[str],
    primary_failure_reason: Optional[str],
    fallback_name: str,
    fallback_skip_reason: Optional[str],
) -> None:
    """M13.1c — single structured INFO emission when the provider chain
    advances past the primary. Lives in llm_judge.py (NOT in
    MIGRATED_FILES) so it does NOT bump the M14.4 pin.

    NEVER raises. ANTHROPIC_API_KEY / OPENAI_API_KEY never logged
    here — only provider names and failure-reason strings (which
    contain exception type names, never prompt content)."""
    try:
        log.info(
            "llm_judge.fallback_engaged",
            extra={
                "primary_provider": primary_name,
                "primary_failure_reason": (
                    primary_failure_reason or "unknown"
                )[:300],
                "fallback_provider": fallback_name,
                "fallback_skip_reason": (
                    fallback_skip_reason[:300]
                    if fallback_skip_reason else None
                ),
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _emit_cost_log(response: LLMResponse, verdict: JudgeVerdict) -> None:
    """Single structured INFO emission after a provider returns. Lives
    in llm_judge.py (NOT in MIGRATED_FILES) so it does NOT bump the
    M14.4 EXPECTED_TOTAL_LOG_CALLS pin.

    OPENAI_API_KEY is never read or referenced here — provider name,
    model, and token counts only.

    M13.1b-obs: ALSO pushes the call metrics into the in-process
    aggregator (``llm_observability.record_llm_call``) so the live
    metrics roll up across both Judge and Reasoner. The aggregator
    push lives inside the same try/except as the log emission — a
    broken aggregator silently degrades to no-op metrics.
    """
    try:
        cost = estimate_cost_usd(
            response.model, response.input_tokens, response.output_tokens,
        )
        log.info(
            "llm_judge.completed",
            extra={
                "model": response.model,
                "action": verdict.action,
                "input_tokens": int(response.input_tokens or 0),
                "output_tokens": int(response.output_tokens or 0),
                "estimated_cost_usd": cost,
                "latency_ms": int(response.latency_ms or 0),
                "provider": response.provider,
                "fell_back": bool(verdict.fell_back),
            },
        )
        # M13.1b-obs aggregator push. Lazy import avoids any
        # circularity at module load.
        from llm_observability import record_llm_call

        record_llm_call(
            caller="llm_judge",
            model=response.model,
            input_tokens=int(response.input_tokens or 0),
            output_tokens=int(response.output_tokens or 0),
            estimated_cost_usd=cost,
            latency_ms=int(response.latency_ms or 0),
            success=bool(response.provider and not verdict.fell_back),
            # M13.1c — populate the per-provider sub-dict so operators
            # can compare anthropic vs openai cost/latency. Falls back
            # to "unknown" inside record_llm_call when response.provider
            # is empty/None (should not happen in practice).
            provider=str(response.provider or "unknown"),
        )
    except Exception:  # noqa: BLE001 — logging must never break the pipeline
        pass


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def judge_verdict_to_dict(verdict: JudgeVerdict) -> dict:
    """Serialise a :class:`JudgeVerdict` for storage / display.

    ``truth_claim`` is always False; ``operator_review_required`` is
    always True. These values are re-asserted here even if the
    in-memory dataclass somehow carried different values, so any
    downstream consumer sees the canonical safety state.
    """
    return {
        "action": verdict.action,
        "new_label": verdict.new_label,
        "reason_ko": verdict.reason_ko,
        "evidence_gaps": list(verdict.evidence_gaps or []),
        "raw_response": verdict.raw_response,
        "provider_used": verdict.provider_used,
        "model": verdict.model,
        "latency_ms": verdict.latency_ms,
        "fell_back": verdict.fell_back,
        "fallback_reason": verdict.fallback_reason,
        "input_tokens": int(verdict.input_tokens or 0),
        "output_tokens": int(verdict.output_tokens or 0),
        "estimated_cost_usd": estimate_cost_usd(
            verdict.model or "",
            verdict.input_tokens or 0,
            verdict.output_tokens or 0,
        ),
        # M13.1c — exposes whether the provider chain advanced past
        # the primary slot (Anthropic in the default config). Always
        # False in M13.1b-only deployments where the chain has a
        # single provider.
        "primary_provider_failed": bool(verdict.primary_provider_failed),
        "truth_claim": False,
        "operator_review_required": True,
    }
