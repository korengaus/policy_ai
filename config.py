import os


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


STAGE_ORDER = {
    "소문": 1,
    "발언": 2,
    "논의": 3,
    "검토": 4,
    "추진": 5,
    "확정": 6,
    "시행": 7,
}
