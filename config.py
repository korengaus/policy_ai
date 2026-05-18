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


STAGE_ORDER = {
    "소문": 1,
    "발언": 2,
    "논의": 3,
    "검토": 4,
    "추진": 5,
    "확정": 6,
    "시행": 7,
}
