import json
import os
import time

from config import AI_MODEL
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


def get_openai_client():
    """Return (client, unavailable_reason). client is None when unusable."""
    if OpenAI is None:
        return None, "openai_package_missing"

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return None, "missing_api_key"

    return OpenAI(api_key=api_key), None


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
                "fell_back": False,
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
