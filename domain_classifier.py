"""CLASSIFY-2a: forward domain classification (tool-free Sonnet, metadata-only).

Promotes the CLASSIFY-PROBE logic (scripts/classify_probe.py) into a production
module. Assigns each analysis row a SINGLE domain label from a fixed 10-label
taxonomy. The label is METADATA: it is persisted beside ``topic`` and consumed
by category UI later — it NEVER feeds any verdict/scoring field.

Auth/call convention mirrors hot_topics.py / llm_judge.py: lazy
``from anthropic import Anthropic``, ``ANTHROPIC_API_KEY``, TOOL-FREE (no
``tools=`` / no web_search — the token-blowup lesson). Cost/observability via
``llm_observability`` (caller="domain_classifier"). ALL classification logging
lives in THIS module (pin-OUT); main.py (pin-IN) adds no log site.

Hard contract: ``classify_domain`` NEVER raises. On a missing key, empty input,
SDK error, network error, or unparseable response it returns ``기타-미분류`` so
analysis/persistence is never blocked by a classification failure.
"""

from __future__ import annotations

import os
import time

from structured_logging import get_logger
from llm_observability import estimate_cost_usd, record_llm_call

log = get_logger(__name__)


# Fixed domain taxonomy (the CLASSIFY-PROBE / CLASSIFY-1 set). 기타-미분류 is the
# explicit fallback for genuinely ambiguous / none-fit rows.
LABELS = [
    "finance", "welfare", "agriculture", "labor", "health",
    "environment", "SMB", "realestate", "statistics", "기타-미분류",
]

# The fallback label returned on ANY failure / ambiguity (never raises).
FALLBACK_LABEL = "기타-미분류"

_DEFAULT_MODEL = "claude-sonnet-4-6"
# Tiny output budget — we want ONE label back, nothing else.
_MAX_OUTPUT_TOKENS = 24
# Truncation widths for the prompt (keep tokens minimal, per the probe).
_CLAIM_SNIPPET = 240


def _build_prompt(title: str, claim_text: str | None) -> str:
    """Tight single-label classification prompt. TOOL-FREE: plain text in, one
    label out. No web_search, no tools."""
    claim_snip = (claim_text or "").strip().replace("\n", " ")[:_CLAIM_SNIPPET]
    labels = " / ".join(LABELS)
    return (
        "You are a strict single-label classifier for Korean government / "
        "policy news. Read the article and assign EXACTLY ONE domain label.\n\n"
        f"Allowed labels: {labels}\n\n"
        "Label meanings:\n"
        "- finance: 금융/대출/금리/가계부채/세제/은행 (money policy, not property)\n"
        "- realestate: 부동산/주택/전세/임대/분양 (housing as property)\n"
        "- welfare: 복지/지원금/돌봄/연금/수당/취약계층\n"
        "- labor: 고용/일자리/실업/임금/근로\n"
        "- agriculture: 농업/축산/농가/농림/식품/농산물\n"
        "- health: 의료/질병/백신/병원/건강/감염병\n"
        "- environment: 환경/탄소/에너지/기후/온실가스\n"
        "- SMB: 소상공인/자영업/중소기업\n"
        "- statistics: 통계청 지표/물가지수/고용률/실업률 (statistics as the subject)\n"
        "- 기타-미분류: use ONLY if none of the above clearly fits\n\n"
        "Reply with ONLY the single label token, nothing else.\n\n"
        f"Title: {title or ''}\n"
        f"Claim: {claim_snip}\n"
        "Label:"
    )


def _call_anthropic_tool_free(prompt: str, model: str, api_key: str):
    """TOOL-FREE Anthropic Messages call (no ``tools=``). Mirrors
    hot_topics._call_anthropic_pick (lazy import, ANTHROPIC_API_KEY). Returns the
    raw SDK message. May raise — the public wrapper is fail-safe."""
    from anthropic import Anthropic  # lazy import (matches hot_topics.py / llm_judge.py)

    client = Anthropic(api_key=api_key)
    return client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )


def _join_text_blocks(content_blocks) -> str:
    """Concatenate the text of all ``text`` blocks (mirrors hot_topics)."""
    parts = []
    for block in content_blocks or []:
        if str(getattr(block, "type", "") or "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(parts)


def _parse_label(raw: str) -> str:
    """Extract a single LABEL from the model's reply (handles stray text /
    'Label: finance' / quotes). Returns the matched label, or FALLBACK_LABEL if
    none of the allowed labels appears (validated by CLASSIFY-PROBE)."""
    s = (raw or "").strip().strip("`'\" .").lower()
    if "기타" in s or "미분류" in s:
        return "기타-미분류"
    for label in LABELS:
        if label == "기타-미분류":
            continue
        if label.lower() in s:
            return label
    return FALLBACK_LABEL


def classify_domain(title: str, claim_text: str | None = None) -> str:
    """Return ONE domain label for a news/analysis row (metadata-only).

    Tool-free claude-sonnet-4-6 single-label classification. NEVER raises: on a
    missing API key, empty title, SDK/network error, or unparseable response it
    returns ``기타-미분류`` so the caller can persist a value and continue.
    """
    try:
        if not (title or "").strip():
            return FALLBACK_LABEL

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            log.warning(
                "[DomainClassifier] ANTHROPIC_API_KEY missing; returning fallback.",
            )
            return FALLBACK_LABEL

        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or _DEFAULT_MODEL
        prompt = _build_prompt(title, claim_text)

        start = time.time()
        message = _call_anthropic_tool_free(prompt, model, api_key)
        latency_ms = int((time.time() - start) * 1000)

        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        # Cost/observability — mirrors hot_topics' record_llm_call usage.
        try:
            record_llm_call(
                caller="domain_classifier",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                latency_ms=latency_ms,
                success=True,
                provider="anthropic",
            )
        except Exception:  # observability must never break classification
            pass

        label = _parse_label(_join_text_blocks(getattr(message, "content", None) or []))
        log.info(
            "[DomainClassifier] tool-free domain label: "
            f"label={label} input={input_tokens} output={output_tokens} "
            f"estimated_cost_usd={cost}",
            extra={
                "domain_label": label,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": cost,
                "model": model,
            },
        )
        return label
    except Exception as error:  # FAIL-SOFT — classification never blocks analysis
        log.warning(
            f"[DomainClassifier] classification failed; returning fallback: {error}",
            extra={"exception_type": type(error).__name__},
        )
        return FALLBACK_LABEL
