import hashlib
import json
import os
from datetime import datetime, timezone

from config import MEMORY_FILE
from timeline import rebuild_topic_summaries
from topic_classifier import classify_policy_topic


def load_policy_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated_at": None,
            "topics": {},
            "articles": [],
        }

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as file:
            memory = json.load(file)

        memory.setdefault("topics", {})
        memory.setdefault("articles", [])

        return memory

    except Exception:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated_at": None,
            "topics": {},
            "articles": [],
        }


def save_policy_memory(memory: dict):
    memory["last_updated_at"] = datetime.now(timezone.utc).isoformat()

    with open(MEMORY_FILE, "w", encoding="utf-8") as file:
        json.dump(memory, file, ensure_ascii=False, indent=2)


def make_article_id(title: str, url: str) -> str:
    raw = f"{title}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def summarize_all_memory(memory: dict, max_topics: int = 5, max_events_per_topic: int = 3) -> str:
    topics = memory.get("topics", {})

    if not topics:
        return "\uae30\uc874 \uc815\ucc45 \uae30\uc5b5\uc774 \uc5c6\uc2b5\ub2c8\ub2e4."

    lines = []

    for topic, data in list(topics.items())[-max_topics:]:
        lines.append(f"\n[\uc8fc\uc81c: {topic}]")
        lines.append(f"- \ucd5c\uc2e0 \ub2e8\uacc4: {data.get('latest_stage')}")
        lines.append(f"- \ucd5c\uc2e0 \uc2e4\ud589 \ud655\ub960: {data.get('latest_probability')}%")
        lines.append(f"- \ucd5c\uc2e0 \uc2dc\uc7a5 \uc601\ud5a5: {data.get('latest_market_impact')}")

        events = data.get("events", [])[-max_events_per_topic:]

        for event in events:
            lines.append(
                f"  - {event.get('published')} | {event.get('execution_stage')} | "
                f"{event.get('execution_probability')}% | {event.get('one_line_summary')}"
            )

    return "\n".join(lines)


def move_existing_articles_to_better_topics(memory: dict):
    articles = memory.get("articles", [])

    memory["topics"] = {}

    for article in articles:
        fake_ai_result = {
            "main_policy_issue": article.get("main_policy_issue", ""),
            "one_line_summary": article.get("one_line_summary", ""),
        }

        topic = classify_policy_topic(
            news_title=article.get("title", ""),
            news_summary=article.get("one_line_summary", ""),
            article_body=article.get("final_judgment", ""),
            ai_result=fake_ai_result,
        )

        article["topic"] = topic

        if topic not in memory["topics"]:
            memory["topics"][topic] = {
                "topic": topic,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_updated_at": None,
                "events": [],
                "latest_stage": None,
                "latest_probability": None,
                "latest_market_impact": None,
                "latest_signal_change": None,
                "timeline": {},
            }

        event_record = {
            "article_id": article.get("article_id"),
            "title": article.get("title"),
            "published": article.get("published"),
            "url": article.get("url"),
            "one_line_summary": article.get("one_line_summary"),
            "main_policy_issue": article.get("main_policy_issue"),
            "execution_probability": article.get("execution_probability"),
            "execution_stage": article.get("execution_stage"),
            "market_impact_level": article.get("market_impact_level"),
            "signal_change": article.get("signal_change"),
            "final_judgment": article.get("final_judgment"),
        }

        memory["topics"][topic]["events"].append(event_record)
        memory["topics"][topic]["last_updated_at"] = datetime.now(timezone.utc).isoformat()

    rebuild_topic_summaries(memory)


def update_memory_with_result(
    memory: dict,
    topic: str,
    article_id: str,
    news: dict,
    original_url: str,
    ai_result: dict,
    policy_claims: list[dict],
):
    existing_ids = {article.get("article_id") for article in memory.get("articles", [])}

    if article_id in existing_ids:
        print("\n----- Policy memory update -----")
        print("This article is already saved. Skipping duplicate save.")
        return memory

    article_record = {
        "article_id": article_id,
        "topic": topic,
        "title": news.get("title"),
        "published": news.get("published"),
        "url": original_url,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "one_line_summary": ai_result.get("one_line_summary"),
        "policy_signal_detected": ai_result.get("policy_signal_detected"),
        "main_policy_issue": ai_result.get("main_policy_issue"),
        "execution_probability": ai_result.get("execution_probability"),
        "execution_stage": ai_result.get("execution_stage"),
        "market_impact_level": ai_result.get("market_impact_level"),
        "signal_change": ai_result.get("signal_change"),
        "official_source_needed": ai_result.get("official_source_needed"),
        "recommended_official_sources": ai_result.get("recommended_official_sources"),
        "official_evidence_found": ai_result.get("official_evidence_found"),
        "official_evidence_summary": ai_result.get("official_evidence_summary"),
        "official_comparison_status": ai_result.get("official_comparison_status"),
        "official_support_score": ai_result.get("official_support_score"),
        "official_verification_note": ai_result.get("official_verification_note"),
        "memory_comparison": ai_result.get("memory_comparison"),
        "final_judgment": ai_result.get("final_judgment"),
        "policy_claims": policy_claims,
    }

    memory.setdefault("articles", []).append(article_record)
    memory.setdefault("topics", {})

    if topic not in memory["topics"]:
        memory["topics"][topic] = {
            "topic": topic,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated_at": None,
            "events": [],
            "latest_stage": None,
            "latest_probability": None,
            "latest_market_impact": None,
            "latest_signal_change": None,
            "timeline": {},
        }

    event_record = {
        "article_id": article_id,
        "title": news.get("title"),
        "published": news.get("published"),
        "url": original_url,
        "one_line_summary": ai_result.get("one_line_summary"),
        "main_policy_issue": ai_result.get("main_policy_issue"),
        "execution_probability": ai_result.get("execution_probability"),
        "execution_stage": ai_result.get("execution_stage"),
        "market_impact_level": ai_result.get("market_impact_level"),
        "signal_change": ai_result.get("signal_change"),
        "official_source_needed": ai_result.get("official_source_needed"),
        "recommended_official_sources": ai_result.get("recommended_official_sources"),
        "official_evidence_found": ai_result.get("official_evidence_found"),
        "official_evidence_summary": ai_result.get("official_evidence_summary"),
        "official_comparison_status": ai_result.get("official_comparison_status"),
        "official_support_score": ai_result.get("official_support_score"),
        "official_verification_note": ai_result.get("official_verification_note"),
        "final_judgment": ai_result.get("final_judgment"),
    }

    memory["topics"][topic]["events"].append(event_record)
    memory["topics"][topic]["last_updated_at"] = datetime.now(timezone.utc).isoformat()

    rebuild_topic_summaries(memory)

    print("\n----- Policy memory update -----")
    print("Saved new policy event.")
    print("topic:", topic)
    print("file:", MEMORY_FILE)

    return memory
