def split_korean_sentences(text: str) -> list[str]:
    if not text:
        return []

    text = text.replace("\n", " ")
    sentences = []
    current = ""

    for char in text:
        current += char
        if char in [".", "?", "!", "\ub2e4", "\uc694"]:
            sentence = current.strip()
            if len(sentence) >= 20:
                sentences.append(sentence)
            current = ""

    if current.strip() and len(current.strip()) >= 20:
        sentences.append(current.strip())

    return sentences


def analyze_authority(sentence: str) -> dict:
    top_authority_keywords = [
        "\uc815\ubd80",
        "\uae08\uc735\uc704\uc6d0\ud68c",
        "\uae08\uc735\uc704",
        "\uae08\uc735\ub2f9\uad6d",
        "\uae08\uac10\uc6d0",
        "\uae08\uc735\uac10\ub3c5\uc6d0",
        "\uad6d\ud1a0\uad50\ud1b5\ubd80",
        "\uad6d\ud1a0\ubd80",
        "\uae30\ud68d\uc7ac\uc815\ubd80",
        "\ud55c\uad6d\uc740\ud589",
        "\ub300\ud1b5\ub839",
    ]
    official_document_keywords = [
        "\ubcf4\ub3c4\uc790\ub8cc",
        "\uacf5\uc2dd \ubc1c\ud45c",
        "\ubc95\ub839",
        "\uace0\uc2dc",
        "\uacf5\uace0",
        "\uc2dc\ud589\ub839",
        "\uc2dc\ud589\uaddc\uce59",
    ]
    medium_authority_keywords = [
        "\uad6d\ud68c",
        "\ub354\ubd88\uc5b4\ubbfc\uc8fc\ub2f9",
        "\uad6d\ubbfc\uc758\ud798",
        "\uc758\uc6d0",
        "TF",
        "\uc9c0\uc790\uccb4",
        "\uacf5\uacf5\uae30\uad00",
        "HUG",
        "HF",
        "SGI",
        "\uc2dc\uc8fc",
        "\uc2dc\uc7a5",
        "\uad6c\uccad",
    ]
    low_authority_keywords = [
        "\uad50\uc218",
        "\uc804\ubb38\uac00",
        "\ud611\ud68c",
        "\uc5c5\uacc4",
        "\ud1a0\ub860\ud68c",
        "\uc138\ubbf8\ub098",
        "\ud3ec\ub7fc",
        "\uc5f0\uad6c\uc6d0",
        "\uad00\uacc4\uc790",
    ]

    if any(keyword in sentence for keyword in official_document_keywords):
        return {
            "authority_label": "\uacf5\uc2dd \ubb38\uc11c/\ubc1c\ud45c",
            "authority_score": 5,
            "authority_reason": "\ubcf4\ub3c4\uc790\ub8cc/\ubc95\ub839/\uace0\uc2dc \ub4f1 \uacf5\uc2dd \ubb38\uc11c \ud45c\ud604 \ud3ec\ud568",
        }

    if any(keyword in sentence for keyword in top_authority_keywords):
        return {
            "authority_label": "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d \uc9c1\uc811 \uc8fc\uccb4",
            "authority_score": 5,
            "authority_reason": "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d/\uc911\uc559\ubd80\ucc98 \ud45c\ud604 \ud3ec\ud568",
        }

    if any(keyword in sentence for keyword in medium_authority_keywords):
        return {
            "authority_label": "\uc815\uce58\uad8c/\uacf5\uacf5\uae30\uad00/\uc9c0\uc790\uccb4 \uc8fc\uccb4",
            "authority_score": 3,
            "authority_reason": "\uad6d\ud68c/\uc815\ub2f9/\uc9c0\uc790\uccb4/\uacf5\uacf5\uae30\uad00 \ud45c\ud604 \ud3ec\ud568",
        }

    if any(keyword in sentence for keyword in low_authority_keywords):
        return {
            "authority_label": "\uc804\ubb38\uac00/\uc5c5\uacc4/\ud1a0\ub860\ud68c \ubc1c\uc5b8",
            "authority_score": 1,
            "authority_reason": "\uc804\ubb38\uac00/\ud611\ud68c/\uc5c5\uacc4 \ubc1c\uc5b8 \uc911\uc2ec",
        }

    return {
        "authority_label": "\uc8fc\uccb4 \ubd88\uba85",
        "authority_score": 1,
        "authority_reason": "\uba85\ud655\ud55c \uc815\ucc45 \uc8fc\uccb4 \uc5c6\uc74c",
    }


def detect_execution_likelihood(sentence: str, authority_label: str) -> dict:
    high_execution_keywords = [
        "\uc2dc\ud589",
        "\ud655\uc815",
        "\uacb0\uc815",
        "\uc758\uacb0",
        "\uacf5\ud3ec",
        "\uace0\uc2dc",
        "\uc2dc\ud589\ub839",
        "\uc2dc\ud589\uaddc\uce59",
        "\ubcf4\ub3c4\uc790\ub8cc",
        "\uacf5\uc2dd \ubc1c\ud45c",
        "\ubaa8\uc9d1",
        "\uc2e0\uccad \uae30\uac04",
        "\uc9c0\uc6d0\ud55c\ub2e4",
    ]
    active_government_keywords = [
        "\uac80\ud1a0",
        "\ub17c\uc758",
        "\uc870\uc0ac",
        "\ucc29\uc218",
        "\ub300\ucc45",
        "\ud604\ud669",
        "\ud30c\uc545",
    ]
    medium_execution_keywords = [
        "\ucd94\uc9c4",
        "\uc900\ube44",
        "\uacc4\ud68d",
        "\ubc29\uc548",
        "\uc9c0\uc790\uccb4",
        "\ucc29\uc218",
    ]
    low_execution_keywords = [
        "\ubaa8\uc0c9",
        "\uac00\ub2a5\uc131",
        "\uc81c\uc5b8",
        "\ud544\uc694",
        "\ud1a0\ub860\ud68c",
        "\ucd95\uc0ac",
        "\uc124\uba85\ud588\ub2e4",
        "\uac15\uc870\ud588\ub2e4",
    ]

    is_top = authority_label in [
        "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d \uc9c1\uc811 \uc8fc\uccb4",
        "\uacf5\uc2dd \ubb38\uc11c/\ubc1c\ud45c",
    ]
    is_medium = authority_label == "\uc815\uce58\uad8c/\uacf5\uacf5\uae30\uad00/\uc9c0\uc790\uccb4 \uc8fc\uccb4"
    is_low = authority_label == "\uc804\ubb38\uac00/\uc5c5\uacc4/\ud1a0\ub860\ud68c \ubc1c\uc5b8"

    if is_top and any(keyword in sentence for keyword in high_execution_keywords):
        return {
            "execution_label": "\uc2e4\ud589 \uac00\ub2a5\uc131 \ub9e4\uc6b0 \ub192\uc74c",
            "execution_score": 5,
            "execution_reason": "\uc815\ubd80/\uacf5\uc2dd \uc8fc\uccb4 + \uc2dc\ud589/\ud655\uc815/\uacf5\uc2dd \ubc1c\ud45c/\uc2e0\uccad \uc77c\uc815 \ud45c\ud604",
        }

    if is_top and any(keyword in sentence for keyword in active_government_keywords):
        return {
            "execution_label": "\uc2e4\ud589 \uac00\ub2a5\uc131 \ub192\uc74c",
            "execution_score": 4,
            "execution_reason": "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d \uc9c1\uc811 \uc8fc\uccb4 + \uc870\uc0ac/\uac80\ud1a0/\ub300\ucc45 \ud45c\ud604",
        }

    if is_top and any(keyword in sentence for keyword in medium_execution_keywords):
        return {
            "execution_label": "\uc2e4\ud589 \uac00\ub2a5\uc131 \uc911\uac04",
            "execution_score": 3,
            "execution_reason": "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d \uc9c1\uc811 \uc8fc\uccb4 + \uacc4\ud68d/\ubc29\uc548/\ucd94\uc9c4 \ud45c\ud604",
        }

    if is_medium and any(keyword in sentence for keyword in high_execution_keywords):
        return {
            "execution_label": "\uc81c\ub3c4/\uc9d1\ud589 \uad00\ub828 \uc2e0\ud638",
            "execution_score": 3,
            "execution_reason": "\uc815\uce58\uad8c/\uacf5\uacf5\uae30\uad00/\uc9c0\uc790\uccb4 \uc8fc\uccb4 + \uc2dc\ud589/\uc2e0\uccad \ud45c\ud604",
        }

    if is_medium and any(keyword in sentence for keyword in medium_execution_keywords):
        return {
            "execution_label": "\ub17c\uc758/\uc9d1\ud589 \uac00\ub2a5\uc131 \uc911\uac04",
            "execution_score": 2,
            "execution_reason": "\uc815\uce58\uad8c/\uacf5\uacf5\uae30\uad00/\uc9c0\uc790\uccb4 \uc8fc\uccb4 + \ucd94\uc9c4/\ubc29\uc548 \ud45c\ud604",
        }

    if is_low and any(keyword in sentence for keyword in low_execution_keywords):
        return {
            "execution_label": "\uc2e4\ud589 \uac00\ub2a5\uc131 \ub0ae\uc74c",
            "execution_score": 1,
            "execution_reason": "\uc804\ubb38\uac00/\uc5c5\uacc4/\ud1a0\ub860\ud68c \ubc1c\uc5b8 \ub610\ub294 \uc81c\uc5b8 \uc218\uc900",
        }

    if any(keyword in sentence for keyword in low_execution_keywords):
        return {
            "execution_label": "\uc2e4\ud589 \uac00\ub2a5\uc131 \ub0ae\uc74c",
            "execution_score": 1,
            "execution_reason": "\ubaa8\uc0c9/\uac00\ub2a5\uc131/\uc81c\uc5b8/\ud1a0\ub860\ud68c \uc911\uc2ec \ud45c\ud604",
        }

    return {
        "execution_label": "\uc2e4\ud589 \uac00\ub2a5\uc131 \ud310\ub2e8 \uc5b4\ub824\uc6c0",
        "execution_score": 1,
        "execution_reason": "\uc2e4\ud589 \uac00\ub2a5\uc131\uc744 \uc2dd\ubcc4\ud560 \uc8fc\uccb4/\uc808\ucc28 \ud45c\ud604 \ubd80\uc871",
    }


def detect_policy_strength(sentence: str, authority_label: str) -> tuple[str, int, str]:
    announcement_keywords = ["\ubc1c\ud45c", "\uacf5\uc2dd \ubc1c\ud45c", "\ubcf4\ub3c4\uc790\ub8cc"]
    confirmed_keywords = [
        "\ud655\uc815",
        "\uacb0\uc815",
        "\uc2dc\ud589",
        "\ub3c4\uc785",
        "\uc801\uc6a9",
        "\uacf5\ud3ec",
        "\uc758\uacb0",
        "\ud1b5\uacfc",
        "\uc2e0\uccad \uae30\uac04",
        "\uc9c4\ud589",
    ]
    strong_keywords = ["\uc2dc\ud589 \uc608\uc815", "\ub3c4\uc785 \uc608\uc815", "\uc801\uc6a9 \uc608\uc815", "\ucd94\uc9c4", "\ucc29\uc218", "\ub9c8\ub828", "\ub300\ucc45"]
    medium_keywords = ["\uac80\ud1a0", "\ub17c\uc758", "\uc124\uacc4", "\uc900\ube44", "\uacc4\ud68d", "\ubc29\uc548", "\uc608\uc815", "\uc870\uc0ac", "\ud30c\uc545"]
    weak_keywords = ["\ubaa8\uc0c9", "\uac00\ub2a5\uc131", "\uac70\ub860", "\uc804\ub9dd", "\uc81c\uc5b8", "\uc758\uacac", "\ud544\uc694"]

    is_top_authority = authority_label in [
        "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d \uc9c1\uc811 \uc8fc\uccb4",
        "\uacf5\uc2dd \ubb38\uc11c/\ubc1c\ud45c",
    ]
    is_medium_authority = authority_label == "\uc815\uce58\uad8c/\uacf5\uacf5\uae30\uad00/\uc9c0\uc790\uccb4 \uc8fc\uccb4"

    if any(keyword in sentence for keyword in announcement_keywords):
        if is_top_authority or is_medium_authority:
            return "\uacf5\uc2dd \ubc1c\ud45c\uae09", 5, "\uacf5\uc2dd/\uacf5\uacf5 \uc8fc\uccb4\uc758 \ubc1c\ud45c \ud45c\ud604"
        return "\ubc1c\ud45c/\ud589\uc0ac \ubc1c\uc5b8 \uc218\uc900", 1, "\uc815\ubd80 \uacf5\uc2dd \ubc1c\ud45c\uac00 \uc544\ub2cc \ubc1c\ud45c \ud45c\ud604"

    if any(keyword in sentence for keyword in confirmed_keywords):
        if is_top_authority or is_medium_authority:
            return "\ud655\uc815/\uc2dc\ud589\uae09", 5, "\uacf5\uc2dd/\uacf5\uacf5 \uc8fc\uccb4\uc758 \ud655\uc815/\uc2dc\ud589/\uc2e0\uccad \uc77c\uc815 \ud45c\ud604"
        return "\ube44\uc815\ubd80 \ud655\uc815/\uc2dc\ud589 \uc5b8\uae09", 1, "\uc815\ubd80 \uacf5\uc2dd \uc8fc\uccb4\uac00 \uc544\ub2cc \ud655\uc815/\uc2dc\ud589 \ud45c\ud604"

    for keyword in strong_keywords:
        if keyword in sentence:
            if is_top_authority or is_medium_authority:
                return "\uac15\ud55c \uc815\ucc45 \uc2e0\ud638", 4, f"\uacf5\uc2dd/\uacf5\uacf5 \uc8fc\uccb4\uc758 '{keyword}' \ud45c\ud604"
            return "\ube44\uc815\ubd80 \uc815\ucc45 \uc81c\uc548/\ud589\uc0ac \uc218\uc900", 1, f"\uc815\ubd80 \uacf5\uc2dd \uc8fc\uccb4\uac00 \uc544\ub2cc '{keyword}' \ud45c\ud604"

    for keyword in medium_keywords:
        if keyword in sentence:
            return "\uac80\ud1a0/\ub17c\uc758 \ub2e8\uacc4", 3, f"'{keyword}' \ud45c\ud604 \ud3ec\ud568"

    for keyword in weak_keywords:
        if keyword in sentence:
            return "\uac00\ub2a5\uc131/\uc81c\uc5b8 \ub2e8\uacc4", 2, f"'{keyword}' \ud45c\ud604 \ud3ec\ud568"

    return "\uc815\ucc45 \uac15\ub3c4 \ub0ae\uc74c", 1, "\uc815\ucc45 \uac15\ub3c4 \ud45c\ud604 \uc5c6\uc74c"


def is_low_value_sentence(sentence: str, authority_label: str) -> bool:
    low_value_keywords = [
        "\ud1a0\ub860\ud68c",
        "\uac1c\ud68c",
        "\ucd95\uc0ac",
        "\ud589\uc0ac",
        "\ucc38\uc11d",
        "\ub9d0\ud588\ub2e4",
        "\uac15\uc870\ud588\ub2e4",
        "\uc124\uba85\ud588\ub2e4",
        "\uc804\ubb38\uac00",
    ]
    must_keep_keywords = [
        "\uc804\uc138\ub300\ucd9c",
        "\ub300\ucd9c",
        "DSR",
        "\ubcf4\uc99d",
        "\uc81c\ud55c",
        "\uac15\ud654",
        "\ucd95\uc18c",
        "\uc815\ubd80",
        "\uc2dc\ud589",
        "\ud655\uc815",
        "\uc9c0\uc6d0",
        "\uc2e0\uccad",
    ]

    if authority_label in [
        "\uc815\ubd80/\uae08\uc735\ub2f9\uad6d \uc9c1\uc811 \uc8fc\uccb4",
        "\uacf5\uc2dd \ubb38\uc11c/\ubc1c\ud45c",
        "\uc815\uce58\uad8c/\uacf5\uacf5\uae30\uad00/\uc9c0\uc790\uccb4 \uc8fc\uccb4",
    ]:
        return False

    if any(keyword in sentence for keyword in must_keep_keywords):
        return False

    return any(keyword in sentence for keyword in low_value_keywords)


def score_policy_importance(sentence: str) -> dict:
    authority = analyze_authority(sentence)
    authority_label = authority["authority_label"]
    authority_score = authority["authority_score"]

    strength_label, strength_score, strength_reason = detect_policy_strength(sentence, authority_label)
    execution = detect_execution_likelihood(sentence, authority_label)
    execution_score = execution["execution_score"]

    score = 0
    reasons = []

    policy_object_keywords = [
        "\uc804\uc138\ub300\ucd9c",
        "\ub300\ucd9c",
        "\ub300\ucd9c\uc870\uac74",
        "DSR",
        "\ubcf4\uc99d",
        "\uc8fc\ud0dd\ub2f4\ubcf4\ub300\ucd9c",
        "\uc8fc\ub2f4\ub300",
        "\uc804\uc138\ubcf4\uc99d",
        "\uc8fc\ud0dd \uad6c\uc785\uc790\uae08",
    ]
    policy_action_keywords = [
        "\uc81c\ud55c",
        "\uac15\ud654",
        "\uc644\ud654",
        "\ucd95\uc18c",
        "\ubcc0\uacbd",
        "\ub3c4\uc785",
        "\uc801\uc6a9",
        "\uc911\ub2e8",
        "\ubd88\ud5c8",
        "\ud5c8\uc6a9",
        "\uaddc\uc81c",
        "\uc9c0\uc6d0",
        "\ubaa8\uc9d1",
        "\uc2e0\uccad",
    ]
    uncertainty_keywords = ["\uac80\ud1a0", "\ub17c\uc758", "\ubaa8\uc0c9", "\uac00\ub2a5\uc131", "\uac70\ub860", "\uc804\ub9dd", "\uc608\uc815"]

    score += authority_score * 2
    reasons.append(f"\uc815\ucc45 \uad8c\uc704: {authority_label} ({authority_score})")
    reasons.append(authority["authority_reason"])

    score += strength_score
    reasons.append(strength_reason)

    score += execution_score * 3
    reasons.append(f"\uc2e4\ud589 \uac00\ub2a5\uc131: {execution['execution_label']} ({execution_score})")
    reasons.append(execution["execution_reason"])

    for keyword in policy_object_keywords:
        if keyword in sentence:
            score += 3
            reasons.append(f"\uc815\ucc45 \ub300\uc0c1({keyword})")

    for keyword in policy_action_keywords:
        if keyword in sentence:
            score += 3
            reasons.append(f"\uc815\ucc45 \uc870\uce58({keyword})")

    for keyword in uncertainty_keywords:
        if keyword in sentence:
            score += 2
            reasons.append(f"\ubd88\ud655\uc2e4 \ud45c\ud604({keyword})")

    if authority_label == "\uc804\ubb38\uac00/\uc5c5\uacc4/\ud1a0\ub860\ud68c \ubc1c\uc5b8":
        score -= 8
        reasons.append("\uc804\ubb38\uac00/\uc5c5\uacc4/\ud1a0\ub860\ud68c \ubc1c\uc5b8: \uc2e4\ud589 \uac00\ub2a5\uc131 \ub0ae\uc544 \uac10\uc810")

    if any(k in sentence for k in ["\ud1a0\ub860\ud68c", "\ud589\uc0ac", "\ucd95\uc0ac", "\uac1c\ud68c"]):
        score -= 6
        reasons.append("\ud589\uc0ac \uc18c\uac1c \ubb38\uc7a5 \uac10\uc810")

    return {
        "sentence": sentence,
        "score": score,
        "authority_label": authority_label,
        "authority_score": authority_score,
        "strength_label": strength_label,
        "strength_score": strength_score,
        "execution_label": execution["execution_label"],
        "execution_score": execution_score,
        "reasons": reasons,
    }


def extract_policy_claim_sentences(article_body: str, max_sentences: int = 6) -> list[dict]:
    sentences = split_korean_sentences(article_body)
    scored = []

    for sentence in sentences:
        result = score_policy_importance(sentence)

        if is_low_value_sentence(sentence, result["authority_label"]):
            continue

        if result["score"] >= 10:
            scored.append(result)

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:max_sentences]
