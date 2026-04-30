from config import QUERY


def classify_policy_topic(
    news_title: str,
    news_summary: str,
    article_body: str,
    ai_result: dict,
) -> str:
    text = " ".join(
        [
            news_title or "",
            news_summary or "",
            article_body or "",
            ai_result.get("main_policy_issue", "") or "",
            ai_result.get("one_line_summary", "") or "",
        ]
    )

    if any(k in text for k in ["STO", "\ud1a0\ud070", "\uc99d\uad8c\ud615", "\uacf5\uacf5 STO"]):
        return "\ubd80\ub3d9\uc0b0 STO"

    rate_discount_keywords = [
        "\uae08\ub9ac\uac10\uba74",
        "\uae08\ub9ac \uac10\uba74",
        "0.6%p \uac10\uba74",
        "\uc6b0\ub300\uae08\ub9ac",
        "\uae08\ub9ac \ud61c\ud0dd",
        "\uae08\ub9ac \ub0ae\ucd98\ub2e4",
        "\uae08\ub9ac\ub97c \ub0ae",
        "\uae08\ub9ac \uc778\ud558",
    ]
    sme_support_keywords = [
        "\uc911\uc18c\uae30\uc5c5",
        "\uadfc\ub85c\uc790",
        "\uc7ac\uc9c1\uc790",
        "\uae30\uc5c5\uc740\ud589",
        "IBK",
    ]
    support_keywords = [
        "\uc9c0\uc6d0",
        "\uae08\ub9ac\uac10\uba74",
        "\uae08\ub9ac \uac10\uba74",
        "\uc6b0\ub300\uae08\ub9ac",
        "\ud61c\ud0dd",
        "\uc778\ud558",
    ]

    if any(k in text for k in rate_discount_keywords):
        if any(k in text for k in sme_support_keywords):
            return "\uc911\uc18c\uae30\uc5c5 \uae08\uc735\uc9c0\uc6d0"
        return "\ub300\ucd9c \uae08\ub9ac\uac10\uba74"

    if (
        any(k in text for k in support_keywords)
        and any(k in text for k in sme_support_keywords)
        and any(k in text for k in ["\ub300\ucd9c", "\uae08\ub9ac", "\uc8fc\ub2f4\ub300", "\uc804\uc138\ub300\ucd9c"])
    ):
        return "\uc911\uc18c\uae30\uc5c5 \uae08\uc735\uc9c0\uc6d0"

    if any(
        k in text
        for k in [
            "\uc2e0\ud63c",
            "\ucd9c\uc0b0",
            "\uc790\ub140\ucd9c\uc0b0",
            "\uc8fc\uac70\ube44",
            "\uc774\uc790 \uc9c0\uc6d0",
            "\ub300\ucd9c\uc774\uc790 \uc9c0\uc6d0",
            "\uc8fc\ud0dd\ub9c8\ub828",
            "\ubcf5\uad8c\uae30\uae08",
        ]
    ):
        return "\uc8fc\uac70\ube44 \uc9c0\uc6d0"

    if any(
        k in text
        for k in [
            "1\uc8fc\ud0dd",
            "\uc720\uc8fc\ud0dd\uc790",
            "\uaddc\uc81c\uc9c0\uc5ed",
            "\ub9cc\uae30",
            "\uc790\ucd9c \ucc28\ub2e8",
            "\ub300\ucd9c \uc81c\ud55c",
            "\uaddc\uc81c \uac15\ud654",
            "\ud604\ud669 \uc870\uc0ac",
        ]
    ):
        return "\uc804\uc138\ub300\ucd9c \uaddc\uc81c"

    if any(k in text for k in ["DSR", "\ucd1d\ubd80\ucc44\uc6d0\ub9ac\uae08\uc0c1\ud658\ube44\uc728"]):
        return "DSR \uaddc\uc81c"

    if any(k in text for k in ["\uc8fc\ub2f4\ub300", "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c"]):
        return "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c \uaddc\uc81c"

    if "\uc804\uc138" in text and "\ub300\ucd9c" in text:
        return "\uc804\uc138\ub300\ucd9c \uc77c\ubc18"

    return QUERY
