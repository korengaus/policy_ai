import trafilatura


def clean_extracted_text(text: str) -> str:
    if not text:
        return ""

    bad_keywords = [
        "\ubb34\ub2e8\uc804\uc7ac",
        "\uc0ac\uc5c5\uc790\ubc88\ud638",
        "\ub4f1\ub85d\ubc88\ud638",
        "\uccad\uc18c\ub144\ubcf4\ud638\ucc45\uc784\uc790",
        "\ubb34\ub2e8\ubcf5\uc81c",
        "\uc7ac\ubc30\ud3ec \uae08\uc9c0",
        "Copyright",
        "copyright",
        "\ub85c\uadf8\uc778",
        "\ud68c\uc6d0\uac00\uc785",
        "\uae30\uc0ac\uc81c\ubcf4",
        "\uace0\uac1d\uc13c\ud130",
        "\uac1c\uc778\uc815\ubcf4",
        "\uc774\uc6a9\uc57d\uad00",
        "\ub9ce\uc774 \ubcf8 \ub274\uc2a4",
        "\uc8fc\uc694\ub274\uc2a4",
        "\uc624\ub298\uc758 \ud3ec\ud1a0",
        "\ucd94\ucc9c\uae30\uc0ac",
        "\uad00\ub828\uae30\uc0ac",
        "\ub7ad\ud0b9\ub274\uc2a4",
    ]

    cleaned_lines = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()

        if len(line) < 20:
            continue

        if any(keyword in line for keyword in bad_keywords):
            continue

        if line in seen:
            continue

        seen.add(line)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def fetch_article_body(url: str, max_chars: int = 5000) -> str:
    try:
        downloaded = trafilatura.fetch_url(url)

        if not downloaded:
            return "\ubcf8\ubb38 \uc218\uc9d1 \uc2e4\ud328: \ud398\uc774\uc9c0 \ub2e4\uc6b4\ub85c\ub4dc \uc2e4\ud328"

        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )

        if not extracted:
            return "\ubcf8\ubb38 \ucd94\ucd9c \uc2e4\ud328: trafilatura\uac00 \ubcf8\ubb38\uc744 \ucc3e\uc9c0 \ubabb\ud568"

        cleaned = clean_extracted_text(extracted)

        if not cleaned:
            return "\ubcf8\ubb38 \ucd94\ucd9c \uc2e4\ud328: \uc815\ub9ac \ud6c4 \ub0a8\uc740 \ubcf8\ubb38 \uc5c6\uc74c"

        return cleaned[:max_chars]

    except Exception as e:
        return f"\ubcf8\ubb38 \uc218\uc9d1 \uc911 \uc624\ub958 \ubc1c\uc0dd: {e}"
