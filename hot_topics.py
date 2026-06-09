"""HOTTOPIC Phase 2 — AI web-search hot-topic keyword selector (pin-OUT helper).

Purpose
-------
The daily cron (``scheduler.py``) searches a FIXED list of 7 ``DEFAULT_QUERIES``
and cannot catch breaking / emerging policy issues. This module adds an UPSTREAM
keyword-selection layer: once per cron run it asks Anthropic (with the
``web_search`` server tool enabled) for the day's hot POLICY keywords, filters
them, and returns the survivors so the scheduler can APPEND them to the fixed 7.
The downstream verification (``analyze_pipeline`` -> verdict -> judge -> card ->
FRESHNESS badge) is reused UNCHANGED. The LLM here is a pure upstream selector;
it is fully decoupled from the verdict-path judge (it never calls ``run_judge``,
never builds ``LLMRequest``/``LLMResponse``, never imports the verdict path).

Why a separate module
---------------------
``scheduler.py`` is pin-IN (``tests/test_log_level_reclassification.py``
MIGRATED_FILES) — its ``log.*`` call count is part of the
EXPECTED_TOTAL_LOG_CALLS=331 / EXPECTED_TOTAL_LOG_ERRORS=16 pins. This module is
NOT in MIGRATED_FILES, so ALL of the new logging (surviving keywords, cost,
fail-safe warnings) lives here and does not touch the pins. ``scheduler.py``
only gains an import + a changed loop iterable (no ``log.*`` line). Mirrors the
``scheduler_dedup.py`` precedent.

Safeguards (all four)
---------------------
(a) web_search must actually fire — the response is inspected for
    ``server_tool_use`` / ``web_search_tool_result`` blocks; if none are present
    the result is discarded (return ``[]``).
(b) source-URL required — every keyword must carry an ``http(s)`` source URL or
    it is dropped.
(c) policy-domain filter — a keyword must match >=1 ALLOWLIST term AND 0
    DENYLIST terms (DENYLIST reuses ``news_collector.OBITUARY_MARKERS`` plus a
    small local off-topic list; ``scripts/observe_daily`` is deliberately NOT
    imported — it is a scripts/ tool).
(d) downstream net — unchanged; each surviving keyword flows through the existing
    ``scheduler.py`` -> ``analyze_pipeline`` -> verdict -> FRESHNESS path, so a
    bad keyword with no official match surfaces LOW / freshness-pending, never a
    false strong card. (Nothing to build here.)

Fail-safe
---------
``build_dynamic_queries`` wraps EVERYTHING in try/except (like
``scheduler_dedup.should_skip_topic``). On ANY failure — flag off, missing
``ANTHROPIC_API_KEY``, SDK import error, network/timeout, malformed/unparseable
JSON, no web_search block, empty result — it logs a warning and returns ``[]``,
so the cron NEVER crashes and ALWAYS retains the fixed 7.
"""

from __future__ import annotations

import json
import os
import re
import time

import config
from llm_observability import estimate_cost_usd, record_llm_call
from news_collector import OBITUARY_MARKERS
from structured_logging import get_logger


log = get_logger(__name__)


# Anthropic web_search server-tool spec (probe-confirmed at HEAD 041c8fdf44).
_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
_DEFAULT_MODEL = "claude-sonnet-4-6"
# Small ceiling — the selector only needs a short JSON array back; the search
# reasoning happens server-side inside the tool loop.
_MAX_OUTPUT_TOKENS = 2048


# ---------------------------------------------------------------------------
# Safeguard (c) — policy-domain filter lists. Kept small and LOCAL.
#
# ALLOWLIST: a keyword must contain >=1 of these to be considered a policy
# topic (finance / loan / real-estate / welfare / SMB / tax / regulation).
# DENYLIST: a keyword containing ANY of these is dropped (off-topic:
# election / securities-trading / foreign-market / sports / entertainment),
# UNIONed with news_collector.OBITUARY_MARKERS (funeral-notice nouns).
# Rule: survive iff (>=1 ALLOWLIST hit) AND (0 DENYLIST hits). Conservative —
# when allow and deny both hit (e.g. "미국 금리"), deny wins and it is dropped.
# ---------------------------------------------------------------------------
_ALLOWLIST = (
    # finance / loan / rates
    "금융", "대출", "금리", "가계부채", "DSR", "서민금융", "햇살론",
    "예금", "보험", "신용",
    # real estate
    "전세", "주택", "부동산", "주담대", "분양", "청약", "임대", "임대료",
    "양도세", "종부세", "LTV", "재건축", "재개발",
    # welfare
    "복지", "지원금", "보조금", "수당", "연금", "바우처", "취약계층", "기초생활",
    # SMB / self-employed
    "소상공인", "자영업", "중소기업", "상가",
    # tax
    "세제", "세액공제", "세금", "감면", "공제", "과세",
    # generic policy verbs/nouns
    "규제", "정책", "지원", "공급", "대책", "제도", "개편", "시행", "법안", "개정",
)

_LOCAL_DENYLIST = (
    # election / political personalities
    "선거", "당선", "득표", "지방선거", "여당", "야당", "대선", "총선", "공천", "탄핵",
    # securities trading / market quotes
    "증권", "채권운용", "투자증권", "코스피", "코스닥", "주가", "상장", "공모주",
    # foreign markets
    "연준", "일본은행", "미국 금리", "엔저", "블룸버그", "로이터", "중동", "이란",
    "월스트리트", "나스닥",
    # sports / entertainment
    "연예", "아이돌", "드라마", "스포츠", "야구", "축구", "예능",
)

# Full denylist = local off-topic markers UNION imported obituary markers.
_DENYLIST = tuple(_LOCAL_DENYLIST) + tuple(OBITUARY_MARKERS)


def _build_prompt(pool_size: int) -> str:
    """Korean policy-keyword prompt. Asks the model to actually web_search and
    return ONLY a JSON array of {keyword, source_url}."""
    return (
        "당신은 한국의 경제·금융·부동산·복지·세제 정책 뉴스를 모니터링하는 분석가입니다. "
        "웹 검색을 사용해 오늘 한국에서 가장 화제가 되는 '정책' 관련 이슈를 찾아, "
        "검증 파이프라인에 넣을 검색 키워드를 추려 주세요.\n"
        "요구사항:\n"
        "1. 반드시 웹 검색을 수행할 것.\n"
        "2. 연예·스포츠·정치인물·선거·증권시세·해외시장 등 정책과 무관한 주제는 제외할 것.\n"
        "3. 각 키워드는 한국 정부·금융당국·지자체의 정책·제도·지원·규제와 직접 관련될 것.\n"
        f"4. 최대 {pool_size}개의 키워드를 고를 것(2~4 단어의 한국어 검색어 형태).\n"
        "5. 각 키워드마다 근거가 된 실제 기사 URL(http 또는 https)을 함께 제시할 것.\n"
        "출력 형식: 다른 설명 없이 JSON 배열만 출력. "
        '예: [{"keyword":"햇살론 개편 서민금융","source_url":"https://example.com/article"}]'
    )


def _normalize(text: str) -> str:
    """Case/space-normalized key for dedup."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _strip_code_fence(text: str) -> str:
    """Strip a leading ```json (or bare ```) fence and trailing ``` from the
    model's final text block. Probe-confirmed: the JSON array comes fenced."""
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _passes_domain_filter(keyword: str) -> bool:
    """Safeguard (c): keyword survives iff it matches >=1 allowlist term and 0
    denylist terms."""
    if not keyword:
        return False
    if any(marker in keyword for marker in _DENYLIST):
        return False
    return any(term in keyword for term in _ALLOWLIST)


def _has_web_search_block(content_blocks) -> bool:
    """Safeguard (a): True iff the response contains at least one
    server_tool_use or web_search_tool_result block (proof a search fired)."""
    for block in content_blocks or []:
        block_type = str(getattr(block, "type", "") or "")
        if block_type in {"server_tool_use", "web_search_tool_result"}:
            return True
    return False


def _join_text_blocks(content_blocks) -> str:
    """Concatenate the text of all ``text`` blocks (the final answer)."""
    parts = []
    for block in content_blocks or []:
        if str(getattr(block, "type", "") or "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(parts)


def _web_search_request_count(usage) -> int:
    """Extract usage.server_tool_use.web_search_requests for cost logging."""
    try:
        server_tool_use = getattr(usage, "server_tool_use", None)
        return int(getattr(server_tool_use, "web_search_requests", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _call_anthropic_web_search(prompt: str, model: str, max_searches: int):
    """Self-contained Anthropic Messages call with the web_search server tool.

    Reuses the key/model CONVENTIONS of ai_reasoner / llm_judge (lazy
    ``from anthropic import Anthropic``, ``ANTHROPIC_API_KEY``) but is fully
    decoupled from the verdict path — no LLMRequest/LLMResponse, no run_judge.
    Returns the raw SDK message object. May raise; the caller is fail-safe.

    Isolated into its own function so tests can monkeypatch it cleanly.
    """
    from anthropic import Anthropic  # lazy import (matches llm_judge.py:516)

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        tools=[
            {
                "type": _WEB_SEARCH_TOOL_TYPE,
                "name": "web_search",
                "max_uses": max(1, int(max_searches)),
            }
        ],
    )


def build_dynamic_queries() -> list[str]:
    """Return up to ``HOT_TOPIC_TOP_K`` filtered hot-policy keyword strings, or
    ``[]``.

    Fail-safe: returns ``[]`` on the disabled flag and on ANY error (missing
    key, SDK import failure, network/timeout, malformed JSON, no web_search
    block, empty result). NEVER raises.
    """
    if not config.hot_topic_enabled():
        return []

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            log.warning(
                "[HotTopics] ANTHROPIC_API_KEY missing; skipping dynamic keywords.",
            )
            return []

        top_k = max(1, config.hot_topic_top_k())
        max_searches = config.hot_topic_max_searches()
        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or _DEFAULT_MODEL
        # Ask for a slightly larger pool so post-filter truncation can still
        # reach top_k; truncate to top_k after filtering.
        prompt = _build_prompt(top_k + 2)

        start = time.time()
        message = _call_anthropic_web_search(prompt, model, max_searches)
        latency_ms = int((time.time() - start) * 1000)

        content_blocks = getattr(message, "content", None) or []

        # Safeguard (a): a real web_search must have fired.
        if not _has_web_search_block(content_blocks):
            log.warning(
                "[HotTopics] No web_search block in response; discarding (safeguard a).",
            )
            return []

        # Cost logging (reuses the record_llm_call / estimate_cost_usd
        # convention). NOTE: estimate_cost_usd counts TOKENS ONLY — the
        # Anthropic web_search per-search surcharge is NOT included; the actual
        # search count is logged separately for operator audit.
        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        web_search_requests = _web_search_request_count(usage)
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        record_llm_call(
            caller="hot_topics",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            latency_ms=latency_ms,
            success=True,
            provider="anthropic",
        )
        log.info(
            "[HotTopics] LLM cost (token-only; web_search surcharge NOT included): "
            f"input={input_tokens} output={output_tokens} "
            f"estimated_cost_usd={cost} web_search_requests={web_search_requests}",
            extra={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": cost,
                "web_search_requests": web_search_requests,
                "model": model,
            },
        )

        # Parse the fenced JSON array from the final text block(s).
        raw_text = _strip_code_fence(_join_text_blocks(content_blocks))
        if not raw_text:
            log.warning("[HotTopics] Empty text block; no keywords parsed.")
            return []
        parsed = json.loads(raw_text)
        if not isinstance(parsed, list):
            log.warning("[HotTopics] Parsed JSON is not a list; discarding.")
            return []

        survivors: list[str] = []
        seen: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            keyword = str(item.get("keyword") or "").strip()
            source_url = str(item.get("source_url") or "").strip()
            if not keyword:
                continue
            # Safeguard (b): http(s) source URL required.
            if not source_url.lower().startswith(("http://", "https://")):
                continue
            # Safeguard (c): policy-domain allow/deny filter.
            if not _passes_domain_filter(keyword):
                continue
            norm = _normalize(keyword)
            if norm in seen:
                continue
            seen.add(norm)
            survivors.append(keyword)
            log.info(
                f"[HotTopics] Kept keyword: {keyword}",
                extra={"keyword": keyword, "source_url": source_url[:500]},
            )
            if len(survivors) >= top_k:
                break

        if not survivors:
            log.warning("[HotTopics] No keywords survived filtering; returning [].")
        return survivors
    except Exception as error:  # noqa: BLE001 — fail-safe, never crash the cron
        log.warning(
            f"[HotTopics] Dynamic keyword selection failed: {error}",
            extra={
                "exception_type": type(error).__name__,
                "exception_message": str(error)[:500],
            },
        )
        return []


def build_query_list(default_queries) -> list[str]:
    """Return the query list the scheduler should iterate.

    Flag OFF -> exactly ``list(default_queries)`` (byte-identical cron). Flag ON
    -> fixed queries first, then the dynamic keywords that are NOT duplicates of
    a fixed query (case/space-normalized). ``default_queries`` is passed IN (not
    imported) so this module never imports ``scheduler`` — no circular import.
    """
    fixed = list(default_queries or [])
    if not config.hot_topic_enabled():
        return fixed

    merged = list(fixed)
    seen = {_normalize(query) for query in fixed}
    for keyword in build_dynamic_queries():
        norm = _normalize(keyword)
        if norm in seen:
            continue
        seen.add(norm)
        merged.append(keyword)
    return merged
