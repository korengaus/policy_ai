"""HOTTOPIC Phase 2b — hot-topic keyword selector (pin-OUT helper).

Purpose
-------
The daily cron (``scheduler.py``) searches a FIXED list of 7 ``DEFAULT_QUERIES``
and cannot catch breaking / emerging policy issues. This module adds an UPSTREAM
keyword-selection layer: once per cron run it (1) fetches fresh external news
TITLES via the existing ``news_collector`` across a few broad policy seeds, then
(2) feeds ONLY those titles to a TOOL-FREE Anthropic text call that picks the
day's top-K hot policy keywords. The survivors are APPENDED to the fixed 7. The
downstream verification (``analyze_pipeline`` -> verdict -> judge -> card ->
FRESHNESS badge) is reused UNCHANGED. The LLM here is a pure upstream selector,
fully decoupled from the verdict-path judge (never calls ``run_judge``, never
builds ``LLMRequest``/``LLMResponse``, never imports the verdict path).

Why this engine (Phase 2b redesign)
-----------------------------------
The Phase-2 engine used the Anthropic ``web_search`` server tool, which injects
the FULL content of each fetched page into the model context: prod measured
input=96,670-109,097 tokens/call, ~3.6x the 30,000-input-tokens/min org rate
limit for claude-sonnet-4-6 (429s; ~$0.34/call). SDK inspection (anthropic
0.104.1) confirmed NO parameter caps result-body size (``max_uses`` = search
count only). The Phase 2a probe measured that pooling ~40 TITLES (no bodies)
across broad seeds is ~4k tokens — ~25x smaller, comfortably under the limit,
~$0.01/call. So the engine fetches titles-only and uses a tool-free pick.

Why a separate module
---------------------
``scheduler.py`` is pin-IN (``tests/test_log_level_reclassification.py``
MIGRATED_FILES) — its ``log.*`` call count is part of the
EXPECTED_TOTAL_LOG_CALLS=331 / EXPECTED_TOTAL_LOG_ERRORS=16 pins. This module is
NOT in MIGRATED_FILES, so ALL the logging lives here. ``scheduler.py`` only calls
``build_query_list`` (no ``log.*`` line). ``news_collector`` is pin-IN, but
importing/calling it adds no ``log.*`` site there. Mirrors ``scheduler_dedup.py``.

Safeguards (all four, preserved across the engine swap)
-------------------------------------------------------
(a) search-actually-happened -> "titles were actually fetched": if
    ``_fetch_candidate_titles`` returns nothing, return ``[]`` BEFORE any LLM
    call.
(b) provenance (was source-URL): each picked keyword must carry a
    ``title_index`` that maps to a REAL fetched title, or it is dropped. The
    source title + its link are logged for audit. Provenance is grounded in the
    fetched candidate set, not a model free-text URL.
(c) policy-domain filter — a keyword must match >=1 ALLOWLIST term AND 0
    DENYLIST terms (DENYLIST reuses ``news_collector.OBITUARY_MARKERS`` plus a
    small local off-topic list; ``scripts/observe_daily`` is deliberately NOT
    imported — it is a scripts/ tool).
(d) downstream net — unchanged; each surviving keyword flows through the existing
    ``scheduler.py`` -> ``analyze_pipeline`` -> verdict -> FRESHNESS path.

Fail-safe
---------
``build_dynamic_queries`` wraps EVERYTHING in try/except (like
``scheduler_dedup.should_skip_topic``). On ANY failure — flag off, missing
``ANTHROPIC_API_KEY``, news-fetch failure, no titles, SDK import error,
network/timeout, malformed/unparseable JSON — it logs a warning and returns
``[]``, so the cron NEVER crashes and ALWAYS retains the fixed 7.
"""

from __future__ import annotations

import json
import os
import re
import time

import config
from llm_observability import estimate_cost_usd, record_llm_call
from news_collector import OBITUARY_MARKERS, search_google_news_rss_with_meta
from structured_logging import get_logger


log = get_logger(__name__)


_DEFAULT_MODEL = "claude-sonnet-4-6"
# Tool-free pick returns only a short JSON array of keywords — keep it small.
_MAX_OUTPUT_TOKENS = 512
# news_collector marks its synthetic "search result page" emergency item with
# this source / collection_source when all real parsing failed. Excluded from
# the candidate pool so a seed STRING is never mistaken for a hot title.
_EMERGENCY_FALLBACK_SOURCE = "forced_search_fallback"


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


def _build_prompt(display_titles: list[str], top_k: int) -> str:
    """Tool-free Korean prompt: pick top-K hot POLICY keywords from the numbered
    title list, returning STRICT JSON [{"keyword","title_index"}]. title_index is
    0-based into ``display_titles`` (provenance, safeguard b)."""
    numbered = "\n".join(f"{i}. {title}" for i, title in enumerate(display_titles))
    return (
        "당신은 한국의 경제·금융·부동산·복지·세제 정책 뉴스를 모니터링하는 분석가입니다.\n"
        "아래는 오늘 수집된 뉴스 제목 목록입니다 (번호. 제목):\n"
        f"{numbered}\n\n"
        f"이 제목들 중에서 오늘 새롭게 부상하는 한국 '정책' 이슈를 가장 잘 대표하는 "
        f"핫 키워드를 최대 {top_k}개 골라 주세요.\n"
        "요구사항:\n"
        "1. 키워드는 금융·대출·부동산·복지·소상공인·세제 등 정책 영역과 직접 관련될 것.\n"
        "2. 연예·스포츠·정치인물·선거·증시/시세·인사/하마평·외신은 제외할 것.\n"
        "3. 각 키워드는 2~4 단어의 한국어 검색어 형태로, 검증 파이프라인에 바로 넣을 수 있게 작성할 것.\n"
        "4. 각 키워드마다 근거가 된 제목의 번호를 title_index(정수)로 함께 제시할 것.\n"
        "5. 제목 전체나 기사 본문을 그대로 옮기지 말고, 핵심 검색 키워드만 추출할 것.\n"
        "출력 형식: 다른 설명 없이 JSON 배열만 출력. "
        '예: [{"keyword":"부동산 세제 개편","title_index":3}]'
    )


def _normalize(text: str) -> str:
    """Case/space-normalized key for dedup."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _extract_json_array(text: str):
    """Robustly extract the top-level JSON array from the model's final text.

    Handles ALL of: (a) a ```json ... ``` fenced block, (b) a bare ``` ... ```
    fence, (c) raw JSON with no fence, (d) a JSON array embedded in surrounding
    prose. Strategy: strip a leading/trailing ``` fence if present and try a
    direct parse; if that fails, regex-search for the first top-level ``[ ... ]``
    array (DOTALL) anywhere in the original text and parse that substring.
    Returns the parsed ``list`` or ``None`` when nothing parseable is found
    (caller is fail-safe)."""
    if not text:
        return None
    candidate = str(text).strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z0-9]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, TypeError):
        pass
    # Fallback: first top-level JSON array anywhere in the raw text (covers
    # prose around the array, or a fence we could not cleanly strip).
    match = re.search(r"\[.*\]", str(text), re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except (ValueError, TypeError):
            pass
    return None


def _passes_domain_filter(keyword: str) -> bool:
    """Safeguard (c): keyword survives iff it matches >=1 allowlist term and 0
    denylist terms."""
    if not keyword:
        return False
    if any(marker in keyword for marker in _DENYLIST):
        return False
    return any(term in keyword for term in _ALLOWLIST)


def _join_text_blocks(content_blocks) -> str:
    """Concatenate the text of all ``text`` blocks (the model's answer). A
    tool-free response is text-only, but this stays robust to multiple blocks."""
    parts = []
    for block in content_blocks or []:
        if str(getattr(block, "type", "") or "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(parts)


def _fetch_candidate_titles() -> list[dict]:
    """Pool fresh external news titles across the broad policy seeds.

    Calls ``news_collector.search_google_news_rss_with_meta`` per seed (titles +
    short snippets, NEVER bodies), pools + dedups by normalized title, and SKIPS
    the synthetic emergency-fallback item (Phase 3 risk #4). Fail-soft per seed:
    a seed that errors is skipped, never fatal. Returns a list of
    ``{title, summary, link, source, published}`` dicts (order preserved == the
    title_index the LLM will reference)."""
    seeds = config.hot_topic_seed_queries()
    per_seed = max(1, config.hot_topic_titles_per_seed())
    pooled: list[dict] = []
    seen: set[str] = set()
    for seed in seeds:
        try:
            out = search_google_news_rss_with_meta(seed, max_results=per_seed)
        except Exception as error:  # fail-soft per seed
            log.warning(
                f"[HotTopics] Title fetch failed for seed '{seed}': {error}",
                extra={"seed": seed, "exception_type": type(error).__name__},
            )
            continue
        debug = (out or {}).get("debug") or {}
        # Skip the whole seed if it resolved only to the emergency search page.
        if debug.get("collection_source") == _EMERGENCY_FALLBACK_SOURCE:
            continue
        for item in (out or {}).get("results") or []:
            # Defensive per-item exclusion of the synthetic fallback item.
            if (item.get("source") or "") == _EMERGENCY_FALLBACK_SOURCE:
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            norm = _normalize(title)
            if norm in seen:
                continue
            seen.add(norm)
            pooled.append(
                {
                    "title": title,
                    "summary": str(item.get("summary") or "").strip(),
                    "link": item.get("google_link")
                    or item.get("original_url")
                    or item.get("link")
                    or "",
                    "source": item.get("source") or "",
                    "published": item.get("published") or item.get("published_at") or "",
                }
            )
    return pooled


def _call_anthropic_pick(prompt: str, model: str):
    """Self-contained TOOL-FREE Anthropic Messages call (no ``tools=``).

    Reuses the key/model CONVENTIONS of ai_reasoner / llm_judge (lazy
    ``from anthropic import Anthropic``, ``ANTHROPIC_API_KEY``) but is fully
    decoupled from the verdict path — no ``tools``, no web_search, no
    LLMRequest/LLMResponse, no run_judge. Returns the raw SDK message object.
    May raise; the caller is fail-safe. Isolated so tests can monkeypatch it."""
    from anthropic import Anthropic  # lazy import (matches llm_judge.py:516)

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )


def build_dynamic_queries() -> list[str]:
    """Return up to ``HOT_TOPIC_TOP_K`` filtered hot-policy keyword strings, or
    ``[]``.

    Fail-safe: returns ``[]`` on the disabled flag and on ANY error (missing
    key, news-fetch failure, no titles, SDK import failure, network/timeout,
    malformed JSON). NEVER raises.
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
        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or _DEFAULT_MODEL

        # Safeguard (a) -> "titles were actually fetched". No titles -> no LLM
        # call, no spend.
        candidates = _fetch_candidate_titles()
        if not candidates:
            log.warning(
                "[HotTopics] No candidate titles fetched; returning [] (safeguard a).",
            )
            return []

        # Titles-only by default (~4k tokens); snippets are an opt-in toggle
        # (~8k) for when the title alone is too sparse.
        include_snippets = config.hot_topic_include_snippets()
        display_titles = []
        for item in candidates:
            if include_snippets and item.get("summary"):
                display_titles.append(f"{item['title']} | {item['summary'][:160]}")
            else:
                display_titles.append(item["title"])
        prompt = _build_prompt(display_titles, top_k)

        start = time.time()
        message = _call_anthropic_pick(prompt, model)
        latency_ms = int((time.time() - start) * 1000)

        # Cost logging (reuses record_llm_call / estimate_cost_usd). Now accurate
        # — a tool-free titles pick has NO web_search surcharge.
        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
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
            "[HotTopics] LLM cost (tool-free titles pick): "
            f"titles={len(candidates)} input={input_tokens} output={output_tokens} "
            f"estimated_cost_usd={cost}",
            extra={
                "candidate_titles": len(candidates),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": cost,
                "model": model,
            },
        )

        # Robustly extract the JSON array (fenced / bare / raw / prose).
        parsed = _extract_json_array(_join_text_blocks(getattr(message, "content", None) or []))
        if parsed is None:
            log.warning(
                "[HotTopics] Could not extract a JSON array from response; returning [].",
            )
            return []

        survivors: list[str] = []
        seen: set[str] = set()
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            keyword = str(entry.get("keyword") or "").strip()
            if not keyword:
                continue
            # Safeguard (b)/provenance: title_index must map to a real fetched
            # title, else drop (no provenance).
            try:
                idx = int(entry.get("title_index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(candidates):
                continue
            source_item = candidates[idx]
            # Safeguard (c): policy-domain allow/deny filter on the keyword.
            if not _passes_domain_filter(keyword):
                continue
            norm = _normalize(keyword)
            if norm in seen:
                continue
            seen.add(norm)
            survivors.append(keyword)
            log.info(
                f"[HotTopics] Kept keyword: {keyword}",
                extra={
                    "keyword": keyword,
                    "source_title": source_item.get("title", "")[:300],
                    "source_link": (source_item.get("link") or "")[:500],
                },
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
