"""NOISE1-A: content-nature classification (tool-free Sonnet, metadata-only).

Mirrors ``domain_classifier.py`` verbatim in shape. Assigns each analysis row a
SINGLE content-nature label from a fixed 3-label taxonomy that separates
government/public-policy substance from private-market/commercial (listing)
content. The label is METADATA: it is persisted beside ``domain`` and consumed
by the feed-composition layer later (Part B) — it NEVER feeds any
verdict/scoring field, and Part A stores it in OBSERVE mode only (no
feed/display change).

Auth/call convention mirrors domain_classifier.py / hot_topics.py: lazy
``from anthropic import Anthropic``, ``ANTHROPIC_API_KEY``, TOOL-FREE (no
``tools=`` / no web_search — the token-blowup lesson). Cost/observability via
``llm_observability`` (caller="content_nature_classifier"). ALL classification
logging lives in THIS module (pin-OUT); main.py (pin-IN) adds no log site.

Hard contract: ``classify_content_nature`` NEVER raises. On a missing key, empty
input, SDK error, network error, or unparseable response it returns
``mixed_or_unclear`` (the fail-to-safe policy-side fallback) so analysis/
persistence is never blocked and a wrong label never removes a real policy
article by construction.
"""

from __future__ import annotations

import os
import time

from structured_logging import get_logger
from llm_observability import estimate_cost_usd, record_llm_call

log = get_logger(__name__)


# Fixed content-nature taxonomy. mixed_or_unclear is the explicit fail-to-safe
# fallback for genuinely ambiguous / none-fit rows (defaults to the policy side).
LABELS = [
    "government_policy", "market_commercial", "mixed_or_unclear",
]

# The fallback label returned on ANY failure / ambiguity (never raises). Chosen
# so a classification miss can never trigger feed treatment in Part B.
FALLBACK_LABEL = "mixed_or_unclear"

_DEFAULT_MODEL = "claude-sonnet-4-6"
# Tiny output budget — we want ONE label back, nothing else.
_MAX_OUTPUT_TOKENS = 24
# Truncation widths for the prompt (keep tokens minimal, per the probe).
_CLAIM_SNIPPET = 240


def _build_content_nature_prompt(title: str, claim_text: str | None) -> str:
    """Tight single-label classification prompt. TOOL-FREE: plain text in, one
    label out. No web_search, no tools."""
    claim_snip = (claim_text or "").strip().replace("\n", " ")[:_CLAIM_SNIPPET]
    labels = " / ".join(LABELS)
    return (
        "You are a strict single-label classifier for Korean news. Decide whether "
        "the article's CORE SUBJECT is a government/public-institution policy "
        "action, or private-market/commercial (listing) content. Assign EXACTLY "
        "ONE label.\n\n"
        f"Allowed labels: {labels}\n\n"
        "Label meanings:\n"
        "- government_policy: 핵심 주제가 정부/공공기관의 행위 — 법령·대책·규제·지원·"
        "공급계획·발표·시행. 정책의 효과를 보여주려 시세를 인용해도 정책이 본질이면 이 라벨.\n"
        "- market_commercial: 핵심 주제가 민간 시장/상업 — 시세·매물·분양 마케팅·임대료 "
        "동향·청약 경쟁률·건설사 실적. 정책을 스치듯 언급해도 본질이 매물/상품이면 이 라벨.\n"
        "- mixed_or_unclear: 둘 다이거나 판단 불가 — 애매하면 반드시 이 라벨(fail-safe).\n\n"
        "Boundary rule: a listing/marketing article that only MENTIONS a policy "
        "incidentally = market_commercial; a policy article that QUOTES prices as "
        "evidence of the policy's effect = government_policy. When genuinely torn, "
        "choose mixed_or_unclear.\n\n"
        "Reply with ONLY the single label token, nothing else.\n\n"
        f"Title: {title or ''}\n"
        f"Claim: {claim_snip}\n"
        "Label:"
    )


def _call_anthropic_content_nature(prompt: str, model: str, api_key: str):
    """TOOL-FREE Anthropic Messages call (no ``tools=``). Mirrors
    domain_classifier._call_anthropic_tool_free (lazy import, ANTHROPIC_API_KEY).
    Returns the raw SDK message. May raise — the public wrapper is fail-safe."""
    from anthropic import Anthropic  # lazy import (matches domain_classifier.py)

    client = Anthropic(api_key=api_key)
    return client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )


def _join_content_nature_text_blocks(content_blocks) -> str:
    """Concatenate the text of all ``text`` blocks (mirrors domain_classifier)."""
    parts = []
    for block in content_blocks or []:
        if str(getattr(block, "type", "") or "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(parts)


def _parse_content_nature_label(raw: str) -> str:
    """Extract a single LABEL from the model's reply (handles stray text /
    'Label: market_commercial' / quotes). Returns the matched label, or
    FALLBACK_LABEL if none of the allowed labels appears."""
    s = (raw or "").strip().strip("`'\" .").lower()
    for label in LABELS:
        if label == FALLBACK_LABEL:
            continue
        if label.lower() in s:
            return label
    return FALLBACK_LABEL


def classify_content_nature(title: str, claim_text: str | None = None) -> str:
    """Return ONE content-nature label for a news/analysis row (metadata-only).

    Tool-free claude-sonnet-4-6 single-label classification. NEVER raises: on a
    missing API key, empty title, SDK/network error, or unparseable response it
    returns ``mixed_or_unclear`` (fail-to-safe policy side) so the caller can
    persist a value and continue.
    """
    try:
        if not (title or "").strip():
            return FALLBACK_LABEL

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            log.warning(
                "[ContentNatureClassifier] ANTHROPIC_API_KEY missing; returning fallback.",
            )
            return FALLBACK_LABEL

        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or _DEFAULT_MODEL
        prompt = _build_content_nature_prompt(title, claim_text)

        start = time.time()
        message = _call_anthropic_content_nature(prompt, model, api_key)
        latency_ms = int((time.time() - start) * 1000)

        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        # Cost/observability — mirrors domain_classifier's record_llm_call usage.
        try:
            record_llm_call(
                caller="content_nature_classifier",
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

        label = _parse_content_nature_label(_join_content_nature_text_blocks(getattr(message, "content", None) or []))
        log.info(
            "[ContentNatureClassifier] tool-free content_nature label: "
            f"label={label} input={input_tokens} output={output_tokens} "
            f"estimated_cost_usd={cost}",
            extra={
                "content_nature_label": label,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": cost,
                "model": model,
            },
        )
        return label
    except Exception as error:  # FAIL-SOFT — classification never blocks analysis
        log.warning(
            f"[ContentNatureClassifier] classification failed; returning fallback: {error}",
            extra={"exception_type": type(error).__name__},
        )
        return FALLBACK_LABEL
