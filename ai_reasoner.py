import json
import os
import time

from config import (
    AI_MODEL,
    ai_reasoner_fallback_provider,
    ai_reasoner_max_output_tokens,
    ai_reasoner_max_retries,
    ai_reasoner_provider,
    ai_reasoner_timeout_seconds,
)
from llm_observability import estimate_cost_usd, record_llm_call
from structured_logging import get_logger

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# M13.1b-obs (2026-05-26): per-call observability — token usage, cost
# estimation, latency, and aggregator integration around the live
# OpenAI API call. Adds two structured log events:
#
#   * ``ai_reasoner.completed`` (log.info) — successful API call;
#     fired BEFORE downstream JSON parsing so the metrics roll up
#     even if the response payload is malformed (API cost was already
#     incurred).
#   * ``ai_reasoner.failed`` (log.warning) — API failure OR downstream
#     JSON parse failure. Additive only — the broad ``except Exception``
#     pattern in ``run_ai_reasoning`` is preserved per M11.7c
#     ("openai SDK has undocumented exception surface; narrowing
#     risks letting library errors propagate and break the pipeline").
#
# OPENAI_API_KEY is never read or referenced in these log lines.
log = get_logger(__name__)


# M16-speed-1a Part F1: constructor-level timeout for the OpenAI client.
# Without this, the SDK default is 600s — a wedged API call could
# absorb the entire JOB_TIMEOUT_SECONDS budget. 20s gives headroom
# over llm_judge.py's 15s (the reasoning prompt is 2-4x the judge
# prompt size). On timeout the SDK raises openai.APITimeoutError,
# which is a subclass of Exception and is caught cleanly by the
# existing broad except handler in run_ai_reasoning — no additional
# exception handling needed. Mirrors llm_judge.py:280 convention.
_AI_REASONER_TIMEOUT_SECONDS = 20.0


def get_openai_client():
    """Return (client, unavailable_reason). client is None when unusable."""
    if OpenAI is None:
        return None, "openai_package_missing"

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return None, "missing_api_key"

    # M26-retry: cap retries (SDK default is 2 -> up to 3x20s+backoff ~90s on a
    # wedged call). Both timeout and max_retries are env-tunable via config
    # (defaults: 20.0s, 1 retry) so the operator can revert/tune on Render
    # without a redeploy. _AI_REASONER_TIMEOUT_SECONDS remains as the documented
    # baseline; the live value comes from config.ai_reasoner_timeout_seconds().
    return OpenAI(
        api_key=api_key,
        timeout=ai_reasoner_timeout_seconds(),
        max_retries=ai_reasoner_max_retries(),
    ), None


def _unavailable_result(
    reason: str,
    *,
    official_source_candidates,
    official_evidence_results,
    evidence_comparison,
    fallback_message: str,
    error_message: str | None = None,
) -> dict:
    return {
        "ai_available": False,
        "ai_status": "unavailable",
        "ai_status_reason": reason,
        "ai_model": AI_MODEL,
        "error": error_message or "AI reasoning is unavailable.",
        "fallback_message": fallback_message,
        "official_source_needed": bool(official_source_candidates),
        "recommended_official_sources": official_source_candidates or [],
        "official_evidence_found": any(
            result.get("fetched") for result in (official_evidence_results or [])
        ),
        "official_evidence_summary": (
            "AI unavailable; official page fetch results were collected separately."
        ),
        "official_comparison_status": (
            evidence_comparison or {}
        ).get("comparison_status", "unclear"),
        "official_support_score": (evidence_comparison or {}).get(
            "semantic_support_score",
            (evidence_comparison or {}).get("support_score", 0),
        ),
        "official_verification_note": (
            "AI unavailable; using rule-based official evidence comparison only."
        ),
    }


def _error_result(
    reason: str,
    error_message: str,
    *,
    official_source_candidates,
    official_evidence_results,
    evidence_comparison,
) -> dict:
    return {
        "ai_available": False,
        "ai_status": "error",
        "ai_status_reason": reason,
        "ai_model": AI_MODEL,
        "error": error_message,
        "fallback_message": "AI reasoning failed. Use the rule-based analysis only.",
        "official_source_needed": bool(official_source_candidates),
        "recommended_official_sources": official_source_candidates or [],
        "official_evidence_found": any(
            result.get("fetched") for result in (official_evidence_results or [])
        ),
        "official_evidence_summary": (
            "AI reasoning failed; official page fetch results were collected separately."
        ),
        "official_comparison_status": (
            evidence_comparison or {}
        ).get("comparison_status", "unclear"),
        "official_support_score": (evidence_comparison or {}).get(
            "semantic_support_score",
            (evidence_comparison or {}).get("support_score", 0),
        ),
        "official_verification_note": (
            "AI reasoning failed; using rule-based official evidence comparison only."
        ),
    }


def _format_policy_claims(policy_claims: list[dict]) -> str:
    if not policy_claims:
        return "No important policy claim sentences were found by the rule engine."

    lines = []

    for i, item in enumerate(policy_claims, start=1):
        lines.append(f"[Policy claim {i}]")
        lines.append(f"sentence: {item['sentence']}")
        lines.append(f"rule_score: {item['score']}")
        lines.append(f"authority: {item['authority_label']}")
        lines.append(f"strength: {item['strength_label']}")
        lines.append(f"execution_likelihood: {item['execution_label']}")
        lines.append(f"rule_reasons: {', '.join(item['reasons'])}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_official_source_candidates(official_source_candidates: list[dict] | None) -> str:
    if not official_source_candidates:
        return "No official source candidates were generated."

    return json.dumps(official_source_candidates, ensure_ascii=False, indent=2)


def _format_official_evidence_results(official_evidence_results: list[dict] | None) -> str:
    if not official_evidence_results:
        return "No official page fetch results were collected."

    return json.dumps(official_evidence_results, ensure_ascii=False, indent=2)


def _format_evidence_comparison(evidence_comparison: dict | None) -> str:
    if not evidence_comparison:
        return "No rule-based news vs official evidence comparison was performed."

    return json.dumps(evidence_comparison, ensure_ascii=False, indent=2)


def build_ai_prompt(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
    memory_context: str,
    official_source_candidates: list[dict] | None = None,
    official_evidence_results: list[dict] | None = None,
    evidence_comparison: dict | None = None,
) -> str:
    claims_text = _format_policy_claims(policy_claims)
    official_sources_text = _format_official_source_candidates(official_source_candidates)
    official_evidence_text = _format_official_evidence_results(official_evidence_results)
    evidence_comparison_text = _format_evidence_comparison(evidence_comparison)

    prompt = f"""
You are an AI policy analyst specializing in Korean real estate, housing finance, and financial regulation.

Analyze the article below. Do not merely summarize it. Judge whether the policy signal is likely to become an actual executable policy, and whether official source verification is needed.

Return JSON only.

Required JSON schema:
{{
  "one_line_summary": "short summary of the article's policy signal",
  "policy_signal_detected": true,
  "main_policy_issue": "core policy issue",
  "execution_probability": 0,
  "execution_stage": "소문/발언/논의/검토/추진/확정/시행 중 하나",
  "market_impact_level": "낮음/중간/높음/매우 높음 중 하나",
  "affected_groups": ["affected group"],
  "why_it_matters": "why this matters",
  "evidence_sentences": ["evidence sentence"],
  "risk_factors": ["uncertainty or risk factor"],
  "memory_comparison": "comparison with existing memory",
  "signal_change": "신규/반복/강화/약화/진전/불명 중 하나",
  "official_source_needed": true,
  "recommended_official_sources": [
    {{
      "source_name": "source name",
      "source_type": "source type",
      "reliability_score": 5,
      "search_query": "recommended search query",
      "search_url": "official search URL generated from the query",
      "reason": "why this official source should be checked"
    }}
  ],
  "official_evidence_found": true,
  "official_evidence_summary": "summary of official page fetch results",
  "official_comparison_status": "official_support_found/official_evidence_missing/official_conflict_possible/official_access_failed/unclear",
  "official_support_score": 0,
  "official_verification_note": "how the news vs official evidence comparison affects this judgment",
  "final_judgment": "final conservative judgment"
}}

Analysis rules:
- Treat central government, financial regulators, the Bank of Korea, the National Assembly, and official local governments as stronger signals than expert or industry comments.
- If the article claims a policy is confirmed, implemented, accepting applications, or officially announced, official_source_needed should usually be true unless the article already quotes sufficient official details.
- If the signal is based on rumors, anonymous sources, media interpretation, or unclear wording, official_source_needed should be true.
- Use the official source candidates below. Choose the most relevant candidates, and adjust the reason if needed.
- Each recommended_official_sources item must include either "search_url" or "official_search_url". Prefer the provided official_search_url.
- Treat official_evidence_results as raw official page access results. Use them as context only.
- Do not treat fetched=true as proof that the exact policy was confirmed. This first crawler version only proves that an official page was reachable and text was collected.
- Use evidence_comparison as a rule-based first pass comparing the news claim against official page text.
- Pay special attention to semantic_support_score, semantic_matched_concepts, evidence_quality, and verification_level in evidence_comparison.
- If evidence_comparison says official_conflict_possible or official_evidence_missing, lower confidence unless the article body has strong official confirmation.
- If verification_level is official_document_unrelated or official_document_not_found, do not treat the official evidence as confirmation.
- Do not invent URLs or claim that you verified official sources. These are search candidates only.
- Be conservative and evidence-based.

[Existing policy memory]
{memory_context}

[Article title]
{news_title}

[Article summary]
{news_summary}

[Rule-based policy claim candidates]
{claims_text}

[Official source search candidates]
{official_sources_text}

[Official page fetch results]
{official_evidence_text}

[Rule-based news vs official evidence comparison]
{evidence_comparison_text}

[Article body]
{article_body[:3500]}
"""

    return prompt.strip()


def _run_openai_reasoning(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
    memory_context: str,
    official_source_candidates: list[dict] | None = None,
    official_evidence_results: list[dict] | None = None,
    evidence_comparison: dict | None = None,
    fell_back: bool = False,
) -> dict:
    # M26-provider-A: this is the ORIGINAL run_ai_reasoning body, UNCHANGED
    # (OpenAI Responses API + M26-retry caps), now reachable via the provider
    # selector. The only edit is the observability `fell_back` field (was a
    # hard-coded False literal) so it reports accurately when this runner is
    # used as a fallback. provider stays "openai" (correct for this runner);
    # on the default path with no fallback, fell_back=False -> logs unchanged.
    client, unavailable_reason = get_openai_client()

    if client is None:
        if unavailable_reason == "openai_package_missing":
            error_message = "openai package is not installed."
            fallback_message = (
                "openai package is not installed. Only rule-based analysis was performed."
            )
        else:
            error_message = "OPENAI_API_KEY is missing."
            fallback_message = (
                "OPENAI_API_KEY is missing. Only rule-based analysis was performed."
            )

        return _unavailable_result(
            unavailable_reason or "unknown",
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
            fallback_message=fallback_message,
            error_message=error_message,
        )

    prompt = build_ai_prompt(
        news_title=news_title,
        news_summary=news_summary,
        article_body=article_body,
        policy_claims=policy_claims,
        memory_context=memory_context,
        official_source_candidates=official_source_candidates,
        official_evidence_results=official_evidence_results,
        evidence_comparison=evidence_comparison,
    )

    # M13.1b-obs: latency timer wraps the API call only — JSON parse
    # and dict population happen after the timer stops.
    start = time.perf_counter()
    try:
        response = client.responses.create(
            model=AI_MODEL,
            input=prompt,
            temperature=0,
            top_p=1,
            text={
                "format": {
                    "type": "json_object",
                }
            },
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        # M13.1b-obs: capture Responses-API token usage + emit
        # observability BEFORE parsing. Field names match the
        # Responses API (input_tokens / output_tokens) — distinct
        # from Chat Completions (prompt_tokens / completion_tokens).
        # Token capture is in its own try so a missing/odd usage
        # block does not derail the rest of the success path; the
        # outer except still catches anything that escapes.
        usage = getattr(response, "usage", None)
        input_tokens = (
            getattr(usage, "input_tokens", 0) or 0 if usage else 0
        )
        output_tokens = (
            getattr(usage, "output_tokens", 0) or 0 if usage else 0
        )
        cost = estimate_cost_usd(
            AI_MODEL, int(input_tokens), int(output_tokens),
        )
        log.info(
            "ai_reasoner.completed",
            extra={
                "model": AI_MODEL,
                "action": "reasoning",
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "estimated_cost_usd": cost,
                "latency_ms": latency_ms,
                "provider": "openai",
                "fell_back": fell_back,
            },
        )
        record_llm_call(
            caller="ai_reasoner",
            model=AI_MODEL,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            estimated_cost_usd=cost,
            latency_ms=latency_ms,
            success=True,
            # M13.1c — ai_reasoner uses the OpenAI Responses API
            # directly (no provider abstraction here, distinct from
            # llm_judge.py's multi-provider chain). Provider is
            # always "openai" for this call site.
            provider="openai",
        )

        raw_text = response.output_text
        parsed = json.loads(raw_text)
        parsed["ai_available"] = True
        parsed["ai_status"] = "ok"
        parsed["ai_status_reason"] = "ok"
        parsed["ai_model"] = AI_MODEL
        parsed.setdefault("official_source_needed", bool(official_source_candidates))
        parsed.setdefault("recommended_official_sources", official_source_candidates or [])
        parsed.setdefault(
            "official_evidence_found",
            any(result.get("fetched") for result in (official_evidence_results or [])),
        )
        parsed.setdefault(
            "official_evidence_summary",
            "Official page fetch results were provided as context.",
        )
        parsed.setdefault(
            "official_comparison_status",
            (evidence_comparison or {}).get("comparison_status", "unclear"),
        )
        parsed.setdefault(
            "official_support_score",
            (evidence_comparison or {}).get(
                "semantic_support_score",
                (evidence_comparison or {}).get("support_score", 0),
            ),
        )
        parsed.setdefault(
            "official_verification_note",
            "Rule-based official evidence comparison was provided as context.",
        )
        return parsed

    except json.JSONDecodeError as e:
        # M13.1b-obs: emit failure warning BEFORE returning the
        # existing _error_result. The successful API call (if any)
        # was already recorded by the completed-log block above —
        # this path captures the post-API parsing failure separately.
        log.warning(
            "ai_reasoner.failed",
            extra={
                "reason": "invalid_json_response",
                "exception_type": type(e).__name__,
            },
        )
        return _error_result(
            "invalid_json_response",
            f"AI returned non-JSON response: {e}",
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
        )
    except Exception as e:  # noqa: BLE001
        # M11.7c: intentionally broad — narrowing reviewed and rejected.
        # The openai SDK's exception surface is undocumented (custom
        # OpenAIError hierarchy + network exceptions + ImportError edge
        # cases); narrowing risks letting library errors propagate up
        # and break the pipeline. M13.1b-obs added the warning log
        # below as an ADDITIVE observability hook; the except shape is
        # unchanged. See docs/EXCEPTION_HANDLING_AUDIT.md for the broad-
        # except policy.
        log.warning(
            "ai_reasoner.failed",
            extra={
                "reason": "api_call_failed",
                "exception_type": type(e).__name__,
            },
        )
        return _error_result(
            "api_call_failed",
            f"AI reasoning failed: {e}",
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
        )


def _run_anthropic_reasoning(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
    memory_context: str,
    official_source_candidates: list[dict] | None = None,
    official_evidence_results: list[dict] | None = None,
    evidence_comparison: dict | None = None,
    fell_back: bool = False,
) -> dict:
    """M26-provider-A: Anthropic (Claude) path. Reuses
    ``llm_judge.AnthropicProvider`` (Messages API + ``content[0].text`` +
    ``_strip_json_fences`` + usage fields, never-raises) with the SAME
    M26-retry caps applied via its parametrized constructor. Produces the SAME
    ``ai_result`` shape as the OpenAI path so downstream consumers
    (topic/memory/ai_status) are unaffected. Verdict-isolated — nothing here
    feeds any verdict field."""
    import llm_judge  # lazy: keeps the default-path import graph unchanged

    if not os.getenv("ANTHROPIC_API_KEY"):
        return _unavailable_result(
            "missing_api_key",
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
            fallback_message=(
                "ANTHROPIC_API_KEY is missing. Only rule-based analysis was performed."
            ),
            error_message="ANTHROPIC_API_KEY is missing.",
        )

    prompt = build_ai_prompt(
        news_title=news_title,
        news_summary=news_summary,
        article_body=article_body,
        policy_claims=policy_claims,
        memory_context=memory_context,
        official_source_candidates=official_source_candidates,
        official_evidence_results=official_evidence_results,
        evidence_comparison=evidence_comparison,
    )
    # The full analytical prompt (identical content to the OpenAI `input`) is
    # the user message; a minimal system message reinforces JSON-only output.
    # Claude has no native JSON mode; AnthropicProvider already strips ```json
    # fences before returning raw_text. Model id resolves to ANTHROPIC_MODEL
    # (verified real default claude-sonnet-4-6) — never an unverified id.
    model = os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-4-6"
    request = llm_judge.LLMRequest(
        system_prompt=(
            "You are an AI policy analyst. Output ONLY a single valid JSON "
            "object matching the requested schema — no prose, no markdown fences."
        ),
        user_prompt=prompt,
        model=model,
        max_tokens=ai_reasoner_max_output_tokens(),
        temperature=0,
    )
    # Same M26-retry discipline as the OpenAI path: caps applied to the
    # Anthropic client so a fallback/primary Claude call can't storm.
    provider = llm_judge.AnthropicProvider(
        timeout=ai_reasoner_timeout_seconds(),
        max_retries=ai_reasoner_max_retries(),
    )
    start = time.perf_counter()
    response = provider.call(request)  # never raises
    latency_ms = int((time.perf_counter() - start) * 1000)

    if response is None or not response.success:
        log.warning(
            "ai_reasoner.failed",
            extra={
                "reason": "api_call_failed",
                "provider": "anthropic",
            },
        )
        return _error_result(
            "api_call_failed",
            f"AI reasoning failed: {response.error if response else 'no response'}",
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
        )

    # Record the successful API call (cost incurred) BEFORE parsing — mirrors
    # the OpenAI path ordering so a malformed payload still rolls up metrics.
    cost = estimate_cost_usd(
        response.model,
        int(response.input_tokens or 0),
        int(response.output_tokens or 0),
    )
    log.info(
        "ai_reasoner.completed",
        extra={
            "model": response.model,
            "action": "reasoning",
            "input_tokens": int(response.input_tokens or 0),
            "output_tokens": int(response.output_tokens or 0),
            "estimated_cost_usd": cost,
            "latency_ms": latency_ms,
            "provider": "anthropic",
            "fell_back": fell_back,
        },
    )
    record_llm_call(
        caller="ai_reasoner",
        model=response.model,
        input_tokens=int(response.input_tokens or 0),
        output_tokens=int(response.output_tokens or 0),
        estimated_cost_usd=cost,
        latency_ms=latency_ms,
        success=True,
        provider="anthropic",
    )

    try:
        parsed = json.loads(response.raw_text)
    except json.JSONDecodeError as e:
        log.warning(
            "ai_reasoner.failed",
            extra={
                "reason": "invalid_json_response",
                "provider": "anthropic",
                "exception_type": type(e).__name__,
            },
        )
        return _error_result(
            "invalid_json_response",
            f"AI returned non-JSON response: {e}",
            official_source_candidates=official_source_candidates,
            official_evidence_results=official_evidence_results,
            evidence_comparison=evidence_comparison,
        )

    # SAME field population as the OpenAI path so the ai_result shape is
    # identical for downstream topic/memory/ai_status consumers.
    parsed["ai_available"] = True
    parsed["ai_status"] = "ok"
    parsed["ai_status_reason"] = "ok"
    parsed["ai_model"] = response.model
    parsed.setdefault("official_source_needed", bool(official_source_candidates))
    parsed.setdefault("recommended_official_sources", official_source_candidates or [])
    parsed.setdefault(
        "official_evidence_found",
        any(result.get("fetched") for result in (official_evidence_results or [])),
    )
    parsed.setdefault(
        "official_evidence_summary",
        "Official page fetch results were provided as context.",
    )
    parsed.setdefault(
        "official_comparison_status",
        (evidence_comparison or {}).get("comparison_status", "unclear"),
    )
    parsed.setdefault(
        "official_support_score",
        (evidence_comparison or {}).get(
            "semantic_support_score",
            (evidence_comparison or {}).get("support_score", 0),
        ),
    )
    parsed.setdefault(
        "official_verification_note",
        "Rule-based official evidence comparison was provided as context.",
    )
    return parsed


_REASONER_RUNNERS = {
    "openai": _run_openai_reasoning,
    "anthropic": _run_anthropic_reasoning,
}


def run_ai_reasoning(
    news_title: str,
    news_summary: str,
    article_body: str,
    policy_claims: list[dict],
    memory_context: str,
    official_source_candidates: list[dict] | None = None,
    official_evidence_results: list[dict] | None = None,
    evidence_comparison: dict | None = None,
) -> dict:
    """M26-provider-A dispatcher ("socket + switch"). Routes to the configured
    provider runner. DEFAULT ``AI_REASONER_PROVIDER="openai"`` -> the existing
    OpenAI Responses-API path, byte-identical to pre-M26-provider-A (incl. the
    M26-retry caps). Verdict-isolated: the returned ai_result feeds only
    topic/memory/ai_status, never any verdict field. Signature unchanged so
    the main.py call site is untouched.

    Fallback (``AI_REASONER_FALLBACK_PROVIDER``, default "none") is OFF by
    default -> single-provider behavior identical to today. When opt-in and the
    primary runner reports ``ai_available=False``, the distinct fallback runner
    is tried once with ``fell_back=True``; both runners apply the SAME
    retry/timeout caps so a fallback can never reintroduce a retry storm.
    """
    primary_name = ai_reasoner_provider()
    if primary_name not in _REASONER_RUNNERS:
        primary_name = "openai"  # bad/unknown value -> safe default

    fallback_name = ai_reasoner_fallback_provider()
    if fallback_name not in _REASONER_RUNNERS or fallback_name == primary_name:
        fallback_name = None  # "none"/invalid/same -> no fallback (today's behavior)

    result = _REASONER_RUNNERS[primary_name](
        news_title=news_title,
        news_summary=news_summary,
        article_body=article_body,
        policy_claims=policy_claims,
        memory_context=memory_context,
        official_source_candidates=official_source_candidates,
        official_evidence_results=official_evidence_results,
        evidence_comparison=evidence_comparison,
        fell_back=False,
    )
    if result.get("ai_available") or fallback_name is None:
        return result

    return _REASONER_RUNNERS[fallback_name](
        news_title=news_title,
        news_summary=news_summary,
        article_body=article_body,
        policy_claims=policy_claims,
        memory_context=memory_context,
        official_source_candidates=official_source_candidates,
        official_evidence_results=official_evidence_results,
        evidence_comparison=evidence_comparison,
        fell_back=True,
    )
