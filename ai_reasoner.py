import json
import os

from config import AI_MODEL

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def get_openai_client():
    if OpenAI is None:
        return None

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return None

    return OpenAI(api_key=api_key)


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
    client = get_openai_client()

    if client is None:
        return {
            "ai_available": False,
            "error": "OPENAI_API_KEY is missing or the openai package is not installed.",
            "fallback_message": "Only rule-based analysis was performed.",
            "official_source_needed": bool(official_source_candidates),
            "recommended_official_sources": official_source_candidates or [],
            "official_evidence_found": any(
                result.get("fetched") for result in (official_evidence_results or [])
            ),
            "official_evidence_summary": "AI unavailable; official page fetch results were collected separately.",
            "official_comparison_status": (
                evidence_comparison or {}
            ).get("comparison_status", "unclear"),
            "official_support_score": (evidence_comparison or {}).get(
                "semantic_support_score",
                (evidence_comparison or {}).get("support_score", 0),
            ),
            "official_verification_note": "AI unavailable; using rule-based official evidence comparison only.",
        }

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

        raw_text = response.output_text
        parsed = json.loads(raw_text)
        parsed["ai_available"] = True
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

    except Exception as e:
        return {
            "ai_available": False,
            "error": f"AI reasoning failed: {e}",
            "fallback_message": "AI reasoning failed. Use the rule-based analysis only.",
            "official_source_needed": bool(official_source_candidates),
            "recommended_official_sources": official_source_candidates or [],
            "official_evidence_found": any(
                result.get("fetched") for result in (official_evidence_results or [])
            ),
            "official_evidence_summary": "AI reasoning failed; official page fetch results were collected separately.",
            "official_comparison_status": (
                evidence_comparison or {}
            ).get("comparison_status", "unclear"),
            "official_support_score": (evidence_comparison or {}).get(
                "semantic_support_score",
                (evidence_comparison or {}).get("support_score", 0),
            ),
            "official_verification_note": "AI reasoning failed; using rule-based official evidence comparison only.",
        }
