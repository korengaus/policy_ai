import logging
import os
import secrets


_log = logging.getLogger(__name__)


QUERY = "전세대출"
MAX_NEWS_RESULTS = 3
RECENT_DAYS = 30
MAX_ARTICLE_CHARS = 5000
MAX_POLICY_SENTENCES = 6

DEFAULT_AI_MODEL = "gpt-4o-mini"
AI_MODEL = os.getenv("AI_MODEL", DEFAULT_AI_MODEL)
MEMORY_FILE = "policy_memory.json"


def has_openai_api_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def describe_ai_config() -> dict:
    return {
        "ai_model": AI_MODEL,
        "ai_model_default": DEFAULT_AI_MODEL,
        "ai_model_from_env": os.getenv("AI_MODEL") is not None,
        "ai_api_key_present": has_openai_api_key(),
    }


# M20 Phase 1: Naver search provider configuration. Read at runtime (not at
# import time) so tests can mutate the environment and see the effect
# immediately, mirroring the semantic-matching accessors below. The provider
# is DISABLED BY DEFAULT (NAVER_SEARCH_ENABLED default false) so merely adding
# the module is a no-op until a later wiring milestone enables it.


def naver_client_id() -> str:
    return (os.getenv("NAVER_CLIENT_ID") or "").strip()


def naver_client_secret() -> str:
    return (os.getenv("NAVER_CLIENT_SECRET") or "").strip()


def naver_search_enabled() -> bool:
    return _env_bool("NAVER_SEARCH_ENABLED", False)


def naver_search_timeout_seconds() -> float:
    return _env_float("NAVER_SEARCH_TIMEOUT_SECONDS", 10.0)


def describe_naver_config() -> dict:
    """Snapshot of the Naver provider configuration. Safe to log/serialize:
    reports credential PRESENCE only — never the client id or secret values."""
    return {
        "enabled": naver_search_enabled(),
        "client_id_present": bool(naver_client_id()),
        "client_secret_present": bool(naver_client_secret()),
        "timeout_seconds": naver_search_timeout_seconds(),
    }


# M21 Phase 2b: Policy Briefing (data.go.kr 1371000) press-release provider
# configuration. Read at runtime (not at import time) so tests can mutate the
# environment, mirroring the Naver accessors above. DISABLED BY DEFAULT
# (POLICY_BRIEFING_ENABLED default false) so merely adding the provider is a
# no-op until an operator flips the flag on Render.


def datagokr_service_key() -> str:
    return (os.getenv("DATAGOKR_SERVICE_KEY") or "").strip()


def policy_briefing_enabled() -> bool:
    return _env_bool("POLICY_BRIEFING_ENABLED", False)


def policy_briefing_timeout_seconds() -> float:
    # FIN-7 — default lowered 10.0 -> 5.0: real pages respond ~2s, so 5s is a
    # safe margin while letting a hung call (the API is intermittently slow)
    # fail fast instead of paying a 10s read-timeout. Env still overrides.
    return _env_float("POLICY_BRIEFING_TIMEOUT_SECONDS", 5.0)


# FIN-5 — recall widening (flag-gated; defaults preserve current behavior).
# lookback_days default 3 == a single 3-day window (today-2..today). Raising it
# covers more days via looped non-overlapping 3-day windows AND engages
# pagination within each window (see providers/policy_briefing.py). At the
# default value the path is byte-identical to pre-FIN-5: one window, page 1 only.
def policy_briefing_lookback_days() -> int:
    return _env_int("POLICY_BRIEFING_LOOKBACK_DAYS", 3)


# FIN-5 — config-driven top-N selection cap. Default 15 == the current
# MAX_PRESS_RELEASES so default behavior is unchanged; raise it (without a code
# change) so a now-fetched older cited release is not dropped by the recency
# tiebreak in _select_documents once the window is widened.
def policy_briefing_max_releases() -> int:
    return _env_int("POLICY_BRIEFING_MAX_RELEASES", 15)


# FIN-7 — per-window page cap. DEFAULT 1: the data.go.kr pressReleaseList API
# IGNORES pageNo (proven 2026-06: page 1 == page 2 == ... byte-identical items),
# so a single fetch already returns the whole window; pages 2+ were pure
# duplicates that dedup discarded while occasionally paying a 10s read-timeout.
# Default 1 removes those no-op duplicate calls. Env can raise it if the API ever
# starts honoring pageNo. Multi-window recall (windows = ceil(lookback/3)) is
# unaffected — that is the real recall lever.
def policy_briefing_max_pages() -> int:
    return _env_int("POLICY_BRIEFING_MAX_PAGES", 1)


def describe_policy_briefing_config() -> dict:
    """Snapshot of the Policy Briefing provider configuration. Safe to
    log/serialize: reports key PRESENCE only — never the serviceKey value."""
    return {
        "enabled": policy_briefing_enabled(),
        "service_key_present": bool(datagokr_service_key()),
        "timeout_seconds": policy_briefing_timeout_seconds(),
    }


# M23: National Law Information (법제처 law.go.kr DRF) provider configuration.
# Auth is OC (env LAW_OC), NOT the data.go.kr serviceKey. Read at runtime so
# tests can mutate the environment. DISABLED BY DEFAULT (NATIONAL_LAW_ENABLED
# default false) so merely adding the provider is a no-op until an operator
# flips the flag on Render.


def law_oc() -> str:
    return (os.getenv("LAW_OC") or "").strip()


def national_law_enabled() -> bool:
    return _env_bool("NATIONAL_LAW_ENABLED", False)


def national_law_timeout_seconds() -> float:
    return _env_float("NATIONAL_LAW_TIMEOUT_SECONDS", 10.0)


def describe_national_law_config() -> dict:
    """Snapshot of the National Law provider configuration. Safe to
    log/serialize: reports OC PRESENCE only — never the OC value."""
    return {
        "enabled": national_law_enabled(),
        "law_oc_present": bool(law_oc()),
        "timeout_seconds": national_law_timeout_seconds(),
    }


# FSS-PROVIDER: FSS 보도자료 (bodoInfo) press-release provider configuration. Auth
# is FSS_API_KEY (a 32-char authKey), distinct from DATAGOKR_SERVICE_KEY (M21) and
# LAW_OC (M23). Read at runtime so tests can mutate the environment. DISABLED BY
# DEFAULT (FSS_ENABLED default false) so merely adding the provider is a no-op
# until an operator flips the flag on Render. Conservative defaults mirror the
# FIN-5/7 knobs: one recent 7-day window per run (1 GET; well under the FSS
# 30-calls/day, 1-month-range limit).


def fss_api_key() -> str:
    return (os.getenv("FSS_API_KEY") or "").strip()


def fss_enabled() -> bool:
    return _env_bool("FSS_ENABLED", False)


def fss_timeout_seconds() -> float:
    return _env_float("FSS_TIMEOUT_SECONDS", 5.0)


def fss_lookback_days() -> int:
    return _env_int("FSS_LOOKBACK_DAYS", 7)


def fss_max_releases() -> int:
    return _env_int("FSS_MAX_RELEASES", 15)


def describe_fss_config() -> dict:
    """Snapshot of the FSS provider configuration. Safe to log/serialize:
    reports key PRESENCE only — never the authKey value."""
    return {
        "enabled": fss_enabled(),
        "api_key_present": bool(fss_api_key()),
        "timeout_seconds": fss_timeout_seconds(),
        "lookback_days": fss_lookback_days(),
        "max_releases": fss_max_releases(),
    }


# M25a: pgvector storage infrastructure. DISABLED BY DEFAULT
# (PGVECTOR_ENABLED default false). When false, the embedding cache uses ONLY
# the existing JSON embedding_cache table (byte-identical to pre-M25a) and the
# pgvector extension / embedding_vectors table are never created. M25a is
# storage-only: it changes no scoring and touches no verdict path.


def pgvector_enabled() -> bool:
    return _env_bool("PGVECTOR_ENABLED", False)


def describe_pgvector_config() -> dict:
    """Snapshot of the pgvector configuration. Safe to log/serialize."""
    return {
        "enabled": pgvector_enabled(),
    }


# M26.2: persistent warm Chromium reuse. DISABLED BY DEFAULT
# (WARM_BROWSER_ENABLED default false), read lazily per call so a dashboard
# flip needs no redeploy (matches the HTTP-cache flag convention). When false,
# official_browser_crawler.fetch_rendered_page runs the verbatim cold
# launch/teardown path — production behavior is byte-identical to pre-M26.2.
# When true, one persistent Chromium is reused across renders via a single
# dedicated render thread (LESSON 1: still exactly one browser, sequential).


def warm_browser_enabled() -> bool:
    return _env_bool("WARM_BROWSER_ENABLED", False)


def describe_warm_browser_config() -> dict:
    """Snapshot of the warm-browser configuration. Safe to log/serialize."""
    return {
        "enabled": warm_browser_enabled(),
    }


# M26-retry: ai_reasoner OpenAI client reliability knobs. The client was built
# with timeout=20s but NO max_retries, so the SDK default (max_retries=2)
# applied -> up to 3x20s+backoff (~90s) on a wedged call (the largest latency
# contributor observed in M26.2). Cap retries at 1 by default (fail fast, but
# tolerate a single transient blip) and keep the 20s per-attempt timeout, both
# now env-tunable so the operator can revert/tune via Render without a redeploy
# (e.g. AI_REASONER_MAX_RETRIES=0 for a ~20s hard cap). Read lazily per call.
# Provider/model are unchanged (OpenAI gpt-4o-mini); this only bounds latency.


def ai_reasoner_max_retries() -> int:
    # Clamp to >= 0: the OpenAI SDK rejects a negative max_retries, and a
    # bad/negative env value should degrade to "no retries" rather than crash.
    return max(0, _env_int("AI_REASONER_MAX_RETRIES", 1))


def ai_reasoner_timeout_seconds() -> float:
    return _env_float("AI_REASONER_TIMEOUT_SECONDS", 20.0)


def describe_ai_reasoner_reliability_config() -> dict:
    """Snapshot of the ai_reasoner reliability knobs. Safe to log/serialize."""
    return {
        "max_retries": ai_reasoner_max_retries(),
        "timeout_seconds": ai_reasoner_timeout_seconds(),
    }


# M26-provider-A: ai_reasoner provider selection ("socket + switch"). DEFAULT
# "openai" — merging changes NOTHING in production until the operator
# deliberately flips AI_REASONER_PROVIDER. A DEDICATED flag (not LLM_PROVIDER,
# which is already "anthropic" for the judge) so ai_reasoner never auto-switches
# to Claude just because the judge is on Claude. Read lazily per call. The
# OpenAI path stays the existing Responses-API code (M26-retry caps intact); the
# "anthropic" path reuses llm_judge.AnthropicProvider with the same caps.
# Fallback defaults to "none" (single provider = today's behavior); opt-in only.


def ai_reasoner_provider() -> str:
    return (os.getenv("AI_REASONER_PROVIDER") or "openai").strip().lower()


def ai_reasoner_fallback_provider() -> str:
    return (os.getenv("AI_REASONER_FALLBACK_PROVIDER") or "none").strip().lower()


def ai_reasoner_max_output_tokens() -> int:
    # Anthropic Messages requires an explicit max_tokens; ai_reasoner's JSON
    # schema is larger than the judge's 800 default, so use 1500 to avoid
    # truncation. Applies to the anthropic path only (the OpenAI Responses path
    # does not set max_tokens and is unchanged).
    return _env_int("AI_REASONER_MAX_OUTPUT_TOKENS", 1500)


def describe_ai_reasoner_provider_config() -> dict:
    """Snapshot of ai_reasoner provider routing. Safe to log/serialize:
    names/ints only — never secrets."""
    return {
        "provider": ai_reasoner_provider(),
        "fallback_provider": ai_reasoner_fallback_provider(),
        "max_output_tokens": ai_reasoner_max_output_tokens(),
    }


# M26.3: concurrent Phase-B ai_reasoner fan-out. DISABLED BY DEFAULT
# (AI_REASONER_CONCURRENCY_ENABLED default false) -> production byte-identical;
# the Phase-B loop calls run_ai_reasoning inline exactly as pre-M26.3. When on,
# the N per-item ai_reasoner network calls (pure functions of phase_a) run on a
# bounded ThreadPoolExecutor; the order-dependent fold-back (dedup, memory,
# topic, counters) stays serial in original order. Network-bound concurrency,
# NOT Chromium/CPU (LESSON 1 unaffected); pool bounded by max_concurrency.
# Read lazily per call so the operator can flip/revert on Render without a
# redeploy.


def ai_reasoner_concurrency_enabled() -> bool:
    return _env_bool("AI_REASONER_CONCURRENCY_ENABLED", False)


def ai_reasoner_max_concurrency() -> int:
    # Bounds the fan-out pool. Default 3 mirrors MAX_PARALLEL_NEWS_ITEMS; items
    # are <= MAX_NEWS_RESULTS (3) after dedup, so effective concurrency is small.
    return max(1, _env_int("AI_REASONER_MAX_CONCURRENCY", 3))


def describe_ai_reasoner_concurrency_config() -> dict:
    """Snapshot of ai_reasoner concurrency knobs. Safe to log/serialize."""
    return {
        "enabled": ai_reasoner_concurrency_enabled(),
        "max_concurrency": ai_reasoner_max_concurrency(),
    }


# Phase 2 M5: semantic evidence matching — optional, off by default.
# The flags below are read at runtime (not at import time) so changing
# the environment in tests immediately takes effect. Embedding calls
# NEVER happen unless SEMANTIC_MATCHING_ENABLED is true AND the provider
# is configured AND its credentials are present.

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def semantic_matching_enabled() -> bool:
    return _env_bool("SEMANTIC_MATCHING_ENABLED", False)


def classify_enabled() -> bool:
    # CLASSIFY-2a — forward domain classification at analysis time. Default
    # False (ships dark): no classify call, rows persist with domain=NULL until
    # the operator sets CLASSIFY_ENABLED=true per service. Read at runtime.
    return _env_bool("CLASSIFY_ENABLED", False)


def content_nature_enabled() -> bool:
    # NOISE1-A — content-nature classification at analysis time (metadata only;
    # government_policy / market_commercial / mixed_or_unclear). Default False
    # (ships dark, OBSERVE mode): no classify call, rows persist with
    # content_nature=NULL until the operator sets CONTENT_NATURE_ENABLED=true per
    # service. Read at runtime. Verdict-isolated; never feeds any scoring field.
    return _env_bool("CONTENT_NATURE_ENABLED", False)


def embedding_provider() -> str:
    return (os.getenv("EMBEDDING_PROVIDER") or "disabled").strip().lower()


def embedding_model() -> str:
    return (os.getenv("EMBEDDING_MODEL") or "").strip()


def embedding_cache_enabled() -> bool:
    return _env_bool("EMBEDDING_CACHE_ENABLED", True)


def embedding_timeout_seconds() -> float:
    return _env_float("EMBEDDING_TIMEOUT_SECONDS", 10.0)


def embedding_max_text_chars() -> int:
    return _env_int("EMBEDDING_MAX_TEXT_CHARS", 4000)


def semantic_max_chunks_per_source() -> int:
    return _env_int("SEMANTIC_MAX_CHUNKS_PER_SOURCE", 20)


def semantic_min_score_for_support() -> float:
    return _env_float("SEMANTIC_MIN_SCORE_FOR_SUPPORT", 0.72)


def semantic_min_score_for_context() -> float:
    return _env_float("SEMANTIC_MIN_SCORE_FOR_CONTEXT", 0.55)


def describe_semantic_config() -> dict:
    """Snapshot of the semantic-matching configuration. Safe to log/serialize."""
    return {
        "enabled": semantic_matching_enabled(),
        "provider": embedding_provider(),
        "model": embedding_model(),
        "cache_enabled": embedding_cache_enabled(),
        "timeout_seconds": embedding_timeout_seconds(),
        "max_text_chars": embedding_max_text_chars(),
        "max_chunks_per_source": semantic_max_chunks_per_source(),
        "min_score_for_support": semantic_min_score_for_support(),
        "min_score_for_context": semantic_min_score_for_context(),
    }


# HOTTOPIC hot-topic keyword selector configuration. Read at runtime (not import
# time) so tests can mutate the environment, mirroring the Naver / Policy-Briefing
# accessors above. DISABLED BY DEFAULT (HOT_TOPIC_ENABLED default false) so the
# upstream keyword layer is a no-op — scheduler.py iterates exactly
# DEFAULT_QUERIES — until an operator flips the flag on Render. The selector lives
# entirely in the pin-OUT hot_topics module and touches no verdict field.
#
# Phase 2b engine: fresh news TITLES via news_collector across broad policy seeds
# -> a tool-free Anthropic pick. (Replaced the web_search engine, which injected
# full result bodies -> ~100k input tokens, 3.6x the 30k/min rate limit. Titles
# only measure ~4k tokens — see HOTTOPIC Phase 2a probe.) The obsolete
# web_search-only knobs (HOT_TOPIC_MAX_SEARCHES / HOT_TOPIC_INPUT_TOKEN_WARN)
# were removed with that engine.
_DEFAULT_HOT_TOPIC_SEEDS = (
    "금융 정책",
    "부동산 정책",
    "복지 정책",
    "세제 개편",
    "소상공인 지원",
)


def hot_topic_enabled() -> bool:
    return _env_bool("HOT_TOPIC_ENABLED", False)


def hot_topic_top_k() -> int:
    return _env_int("HOT_TOPIC_TOP_K", 3)


def hot_topic_seed_queries() -> list[str]:
    # Broad DOMAIN seeds (deliberately NOT the fixed-7 specific queries, and NOT
    # the noisy bare "정책" seed — Phase 2a probe). Comma-separated env override
    # lets seeds be tuned without a code change; blank entries are dropped.
    raw = os.getenv("HOT_TOPIC_SEED_QUERIES")
    if raw is None or not raw.strip():
        return list(_DEFAULT_HOT_TOPIC_SEEDS)
    seeds = [part.strip() for part in raw.split(",") if part.strip()]
    return seeds or list(_DEFAULT_HOT_TOPIC_SEEDS)


# HOTTOPIC-UNBOUND — SEEDLESS Google News section/topic feeds merged into the
# hot-topic candidate pool so topics we never seeded can surface (the seed
# searches above are bounded by our own query terms). Ships DORMANT
# (HOT_TOPIC_SECTION_ENABLED default false, mirroring HOT_TOPIC_ENABLED): the
# operator enables it on Render after watching a few runs. Topic-selection
# metadata only; never a verdict field.
_DEFAULT_HOT_TOPIC_SECTION_TOPICS = ("BUSINESS", "NATION")


def hot_topic_section_enabled() -> bool:
    return _env_bool("HOT_TOPIC_SECTION_ENABLED", False)


def hot_topic_section_topics() -> list[str]:
    raw = os.getenv("HOT_TOPIC_SECTION_TOPICS")
    if raw is None or not raw.strip():
        return list(_DEFAULT_HOT_TOPIC_SECTION_TOPICS)
    topics = [part.strip().upper() for part in raw.split(",") if part.strip()]
    return topics or list(_DEFAULT_HOT_TOPIC_SECTION_TOPICS)


_DEFAULT_CORS_ALLOWED_ORIGINS = (
    "https://policy-ai-q5ax.onrender.com",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)


# AUTH-2b — session cookie signing key. Read from SESSION_SECRET_KEY (preferred)
# or SECRET_KEY. NEVER hardcoded. When neither env var is set, a per-process
# random key is generated ONCE and a WARNING is logged: sessions become
# ephemeral (invalidated on every restart) rather than relying on a known
# constant — fails safe instead of fails open.
_SESSION_SECRET_FALLBACK: "str | None" = None


def session_secret_key() -> str:
    """Return the signing key for the session cookie.

    Prefers ``SESSION_SECRET_KEY``, then ``SECRET_KEY``. When neither is set,
    returns a per-process random key generated once (cached for the lifetime
    of the process so a single boot keeps consistent signatures) and logs a
    WARNING. NEVER returns a hardcoded constant secret; the key, fallback or
    not, is never logged or printed.
    """
    raw = (os.getenv("SESSION_SECRET_KEY") or os.getenv("SECRET_KEY") or "").strip()
    if raw:
        return raw
    global _SESSION_SECRET_FALLBACK
    if _SESSION_SECRET_FALLBACK is None:
        _SESSION_SECRET_FALLBACK = secrets.token_urlsafe(64)
        _log.warning(
            "SESSION_SECRET_KEY is not set; using a per-process RANDOM key. "
            "Sessions are insecure/ephemeral and will be invalidated on every "
            "restart. Set SESSION_SECRET_KEY to a long random value in "
            "production."
        )
    return _SESSION_SECRET_FALLBACK


def cors_allowed_origins() -> list[str]:
    # Browser cross-origin allow-list for CORSMiddleware. Comma-separated env
    # override lets origins be tuned without a code change; blank entries are
    # dropped. Falls back to the default list (prod origin + localhost) so the
    # allow-list is never empty — an empty list would block the live site's own
    # browser calls.
    raw = os.getenv("CORS_ALLOWED_ORIGINS")
    if raw is None or not raw.strip():
        return list(_DEFAULT_CORS_ALLOWED_ORIGINS)
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    return origins or list(_DEFAULT_CORS_ALLOWED_ORIGINS)


def hot_topic_titles_per_seed() -> int:
    return _env_int("HOT_TOPIC_TITLES_PER_SEED", 8)


def hot_topic_include_snippets() -> bool:
    # Default OFF — titles-only measured ~4k tokens; snippets ~8k. Opt in only if
    # titles alone prove too sparse for the pick.
    return _env_bool("HOT_TOPIC_INCLUDE_SNIPPETS", False)


def describe_hot_topic_config() -> dict:
    """Snapshot of the hot-topic selector configuration. Safe to log/serialize."""
    return {
        "enabled": hot_topic_enabled(),
        "top_k": hot_topic_top_k(),
        "seed_queries": hot_topic_seed_queries(),
        "titles_per_seed": hot_topic_titles_per_seed(),
        "include_snippets": hot_topic_include_snippets(),
    }


# audit §1.5 #3 re-audit (2026-05-26): STAGE_ORDER shares Korean
# tokens (발언, 검토, 추진, 논의) with multiple keyword lists in
# policy_confidence.py, policy_impact.py, and bias_framing_agent.py.
# Despite the token overlap, the SHAPE is fundamentally different:
# STAGE_ORDER maps stage-name → integer rank (policy-stage ordering),
# while the other constants are flat lists scored against text. They
# cannot be unified without changing the data model. Keep separate.
STAGE_ORDER = {
    "소문": 1,
    "발언": 2,
    "논의": 3,
    "검토": 4,
    "추진": 5,
    "확정": 6,
    "시행": 7,
}
