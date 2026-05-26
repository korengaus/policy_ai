from config import STAGE_ORDER
from structured_logging import get_logger


# M14.0-print-b (2026-05-26): module logger for the
# print_timeline_summary diagnostic helper. The function is invoked
# from main.analyze_pipeline (main.py:1260) on every Render request,
# so its prior print() output bypassed LOG_FORMAT=json aggregation.
log = get_logger(__name__)


def calculate_topic_timeline(events: list[dict]) -> dict:
    if not events:
        return {
            "event_count": 0,
            "trend": "\ub370\uc774\ud130 \uc5c6\uc74c",
            "probability_change": 0,
            "stage_change": "\ub370\uc774\ud130 \uc5c6\uc74c",
            "latest_stage": None,
            "latest_probability": None,
        }

    if len(events) == 1:
        latest = events[-1]
        return {
            "event_count": 1,
            "trend": "\uc2e0\uaddc \uc0ac\uac74",
            "probability_change": 0,
            "stage_change": "\ube44\uad50 \ub300\uc0c1 \uc5c6\uc74c",
            "latest_stage": latest.get("execution_stage"),
            "latest_probability": latest.get("execution_probability"),
        }

    previous = events[-2]
    latest = events[-1]

    prev_prob = previous.get("execution_probability") or 0
    latest_prob = latest.get("execution_probability") or 0
    probability_change = latest_prob - prev_prob

    prev_stage = previous.get("execution_stage") or "\uc18c\ubb38"
    latest_stage = latest.get("execution_stage") or "\uc18c\ubb38"

    prev_stage_num = STAGE_ORDER.get(prev_stage, 0)
    latest_stage_num = STAGE_ORDER.get(latest_stage, 0)

    if latest_stage_num > prev_stage_num:
        stage_change = "\ub2e8\uacc4 \uc0c1\uc2b9"
    elif latest_stage_num < prev_stage_num:
        stage_change = "\ub2e8\uacc4 \ud558\ub77d"
    else:
        stage_change = "\ub2e8\uacc4 \uc720\uc9c0"

    if probability_change >= 15 or latest_stage_num > prev_stage_num:
        trend = "\uac15\ud654/\uc9c4\uc804"
    elif probability_change <= -15 or latest_stage_num < prev_stage_num:
        trend = "\uc57d\ud654/\ud6c4\ud1f4"
    else:
        trend = "\uc720\uc9c0/\ubc18\ubcf5"

    return {
        "event_count": len(events),
        "trend": trend,
        "probability_change": probability_change,
        "stage_change": stage_change,
        "previous_stage": prev_stage,
        "latest_stage": latest_stage,
        "previous_probability": prev_prob,
        "latest_probability": latest_prob,
    }


def rebuild_topic_summaries(memory: dict):
    for topic, data in memory.get("topics", {}).items():
        events = data.get("events", [])
        timeline = calculate_topic_timeline(events)

        data["timeline"] = timeline

        if events:
            latest = events[-1]
            data["latest_stage"] = latest.get("execution_stage")
            data["latest_probability"] = latest.get("execution_probability")
            data["latest_market_impact"] = latest.get("market_impact_level")
            data["latest_signal_change"] = latest.get("signal_change")
            data["latest_summary"] = latest.get("one_line_summary")


def print_timeline_summary(memory: dict):
    # M14.0-print-b (2026-05-26): print → log.info conversion. Function
    # name kept for backward compatibility (main.py:1260 calls it as
    # `print_timeline_summary`, and tests/test_parallel_news_processing.py
    # patches `main.print_timeline_summary`). Each interpolated value is
    # surfaced via `extra=` so JSON log aggregators (Render with
    # LOG_FORMAT=json) can query individual fields.
    log.info("\n========== Topic Timeline Summary ==========")

    topics = memory.get("topics", {})

    if not topics:
        log.info("No policy memory has been saved yet.")
        return

    for topic, data in topics.items():
        events = data.get("events", [])
        timeline = data.get("timeline", {})

        log.info(f"\n[Topic] {topic}", extra={"topic": topic})
        log.info(
            f"saved events: {len(events)}",
            extra={"topic": topic, "saved_events_count": len(events)},
        )
        timeline_trend = timeline.get("trend")
        log.info(
            f"timeline trend: {timeline_trend}",
            extra={"topic": topic, "timeline_trend": timeline_trend},
        )
        stage_change = timeline.get("stage_change")
        log.info(
            f"stage change: {stage_change}",
            extra={"topic": topic, "stage_change": stage_change},
        )
        latest_stage = data.get("latest_stage")
        log.info(
            f"latest stage: {latest_stage}",
            extra={"topic": topic, "latest_stage": latest_stage},
        )
        latest_probability = data.get("latest_probability")
        log.info(
            f"latest execution probability: {latest_probability}%",
            extra={"topic": topic, "latest_probability": latest_probability},
        )
        latest_market_impact = data.get("latest_market_impact")
        log.info(
            f"latest market impact: {latest_market_impact}",
            extra={"topic": topic, "latest_market_impact": latest_market_impact},
        )
        latest_summary = data.get("latest_summary")
        log.info(
            f"latest summary: {latest_summary}",
            extra={
                "topic": topic,
                "latest_summary": (latest_summary or "")[:200],
            },
        )

        if len(events) >= 2:
            previous_probability = timeline.get("previous_probability")
            probability_change = timeline.get("probability_change")
            log.info(
                f"execution probability change: "
                f"{previous_probability}% -> {latest_probability}% "
                f"({probability_change:+}p)",
                extra={
                    "topic": topic,
                    "previous_probability": previous_probability,
                    "latest_probability": latest_probability,
                    "probability_change": probability_change,
                },
            )

        log.info("\nRecent events:")
        for event in events[-3:]:
            event_published = event.get("published")
            event_stage = event.get("execution_stage")
            event_probability = event.get("execution_probability")
            event_summary = event.get("one_line_summary")
            log.info(
                f"- {event_published} | "
                f"{event_stage} | "
                f"{event_probability}% | "
                f"{event_summary}",
                extra={
                    "topic": topic,
                    "event_published": event_published,
                    "event_stage": event_stage,
                    "event_probability": event_probability,
                    "event_summary": (event_summary or "")[:200],
                },
            )
