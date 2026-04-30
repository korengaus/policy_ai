from config import STAGE_ORDER


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
    print("\n========== Topic Timeline Summary ==========")

    topics = memory.get("topics", {})

    if not topics:
        print("No policy memory has been saved yet.")
        return

    for topic, data in topics.items():
        events = data.get("events", [])
        timeline = data.get("timeline", {})

        print(f"\n[Topic] {topic}")
        print("saved events:", len(events))
        print("timeline trend:", timeline.get("trend"))
        print("stage change:", timeline.get("stage_change"))
        print("latest stage:", data.get("latest_stage"))
        print("latest execution probability:", str(data.get("latest_probability")) + "%")
        print("latest market impact:", data.get("latest_market_impact"))
        print("latest summary:", data.get("latest_summary"))

        if len(events) >= 2:
            print(
                "execution probability change:",
                f"{timeline.get('previous_probability')}% -> {timeline.get('latest_probability')}%",
                f"({timeline.get('probability_change'):+}p)",
            )

        print("\nRecent events:")
        for event in events[-3:]:
            print(
                f"- {event.get('published')} | "
                f"{event.get('execution_stage')} | "
                f"{event.get('execution_probability')}% | "
                f"{event.get('one_line_summary')}"
            )
