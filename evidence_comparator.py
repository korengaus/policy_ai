import re
from collections import Counter

# M11.2: source-of-truth for STOPWORDS / CONCEPT_SYNONYMS lives in
# korean_constants. evidence_comparator's variants differ from the
# ones in official_relevance.py / official_source_body.py — see
# docs/KOREAN_CONSTANTS.md for why they're kept distinct.
from korean_constants import (
    STOPWORDS_COMPARATOR as STOPWORDS,
    CONCEPT_SYNONYMS_COMPARATOR as CONCEPT_SYNONYMS,
)

from official_evidence_resolution import _is_strong_primary_document_match
from structured_logging import get_logger

log = get_logger(__name__)

# M22-1 — Lane-A verification levels that a strong Lane-B (Policy-Briefing)
# match is allowed to upgrade. These are the "no real Lane-A match" levels; a
# genuine Lane-A medium/strong match is left untouched (Lane A wins).
_LANE_B_UPGRADABLE_LEVELS = frozenset({
    "official_access_failed",
    "official_document_not_found",
    "excluded_non_policy_page",
    "weak_official_match",
    "low_confidence_match",
})


CONFLICT_PHRASES = [
    "\ud655\uc815\ub418\uc9c0 \uc54a\uc558\ub2e4",
    "\ud655\uc815\ub41c \ubc14 \uc5c6\ub2e4",
    "\uac80\ud1a0\ud55c \ubc14 \uc5c6\ub2e4",
    "\uac80\ud1a0 \uc911",
    "\uac80\ud1a0\uc911",
    "\uc0ac\uc2e4\uacfc \ub2e4\ub974\ub2e4",
    "\uc0ac\uc2e4\uc774 \uc544\ub2c8\ub2e4",
    "\ud574\uba85",
    "\ubc18\ubc15",
    "\uc544\ub2c8\ub2e4",
]

def _normalize_text(value) -> str:
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def _policy_claims_to_text(policy_claims: list[dict]) -> str:
    lines = []

    for claim in policy_claims or []:
        if isinstance(claim, dict):
            lines.append(_normalize_text(claim.get("sentence")))
        else:
            lines.append(_normalize_text(claim))

    return " ".join(line for line in lines if line)


def _is_comparable_evidence(result: dict) -> bool:
    relevance_score = result.get("document_relevance_score") or 0
    evidence_grade = result.get("evidence_grade")

    if result.get("should_exclude_from_verification"):
        return False
    if evidence_grade not in {"A", "B", "C"}:
        return False
    if result.get("usable") is True and relevance_score >= 40:
        return True
    if result.get("weakly_usable") is True and relevance_score >= 35:
        return True

    return False


def _extract_keywords(*texts: str, max_keywords: int = 18) -> list[str]:
    combined = " ".join(_normalize_text(text) for text in texts if text)
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", combined)
    cleaned = []

    for token in tokens:
        token = token.strip()
        lower_token = token.lower()

        if len(token) < 2:
            continue
        if token in STOPWORDS or lower_token in STOPWORDS:
            continue
        if token.isdigit():
            continue

        cleaned.append(token)

    counter = Counter(cleaned)
    return [keyword for keyword, _ in counter.most_common(max_keywords)]


def _news_text(news_title, news_summary, article_body, policy_claims) -> str:
    return " ".join(
        [
            _normalize_text(news_title),
            _normalize_text(news_summary),
            _policy_claims_to_text(policy_claims),
            _normalize_text(article_body[:1200] if article_body else ""),
        ]
    )


def _build_official_text(official_evidence_results: list[dict]) -> str:
    parts = []

    for result in official_evidence_results or []:
        if not _is_comparable_evidence(result):
            continue
        if not result.get("document_fetched") and not result.get("fetched"):
            continue

        document_title = _normalize_text(result.get("document_title"))
        document_text = _normalize_text(result.get("document_text_snippet"))

        if document_text:
            parts.append(document_title)
            parts.append(document_text)
            continue

        parts.append(_normalize_text(result.get("title")))
        parts.append(_normalize_text(result.get("text_snippet")))

    return " ".join(part for part in parts if part)


def _build_document_text(official_evidence_results: list[dict]) -> str:
    parts = []

    for result in official_evidence_results or []:
        if not _is_comparable_evidence(result):
            continue
        if not result.get("document_fetched"):
            continue

        parts.append(_normalize_text(result.get("document_title")))
        parts.append(_normalize_text(result.get("document_text_snippet")))

    return " ".join(part for part in parts if part)


def _detect_concepts(text: str) -> list[str]:
    detected = []

    for concept, synonyms in CONCEPT_SYNONYMS.items():
        if any(synonym and synonym in text for synonym in synonyms):
            detected.append(concept)

    return detected


def _find_conflict_signals(text: str) -> list[str]:
    return [phrase for phrase in CONFLICT_PHRASES if phrase in text]


def _evidence_access_counts(official_evidence_results: list[dict]) -> dict:
    results = official_evidence_results or []
    comparable_results = [result for result in results if _is_comparable_evidence(result)]
    weakly_usable_results = [
        result
        for result in comparable_results
        if result.get("weakly_usable") is True and (result.get("document_relevance_score") or 0) >= 35
    ]
    strongly_usable_results = [
        result
        for result in comparable_results
        if result.get("usable") is True and (result.get("document_relevance_score") or 0) >= 40
    ]
    excluded_results = [
        result
        for result in results
        if result.get("should_exclude_from_verification") or result.get("evidence_grade") in {"D", "E", "F"}
    ]
    return {
        "official_evidence_count": len(results),
        "search_success_count": sum(1 for result in results if result.get("fetched_search_page") or result.get("fetched")),
        "document_found_count": sum(1 for result in comparable_results if result.get("selected_document_url")),
        "document_success_count": sum(1 for result in comparable_results if result.get("document_fetched")),
        "relevance_qualified_count": len(comparable_results),
        "weakly_usable_count": len(weakly_usable_results),
        "strongly_usable_count": len(strongly_usable_results),
        "excluded_non_policy_count": len(excluded_results),
    }


def _semantic_score(news_concepts: list[str], official_concepts: list[str], document_success_count: int) -> int:
    if not news_concepts:
        return 0

    matched = set(news_concepts) & set(official_concepts)
    base_score = int(round((len(matched) / len(set(news_concepts))) * 85))
    document_bonus = min(15, document_success_count * 5)
    return min(100, base_score + document_bonus)


def _quality_from_score(
    semantic_support_score: int,
    document_success_count: int,
    document_found_count: int,
    search_success_count: int,
) -> str:
    if search_success_count == 0:
        return "failed"
    if document_success_count == 0:
        return "weak"
    if semantic_support_score >= 70:
        return "strong"
    if semantic_support_score >= 40:
        return "medium"
    if document_found_count > 0:
        return "weak"
    return "failed"


def _verification_level(
    semantic_support_score: int,
    document_success_count: int,
    document_found_count: int,
    search_success_count: int,
    weakly_usable_count: int,
    strongly_usable_count: int,
    excluded_non_policy_count: int,
) -> str:
    if search_success_count == 0:
        return "official_access_failed"
    if document_found_count == 0 and excluded_non_policy_count > 0:
        return "excluded_non_policy_page"
    if document_found_count == 0:
        return "official_document_not_found"
    if document_success_count == 0:
        return "official_document_not_found"
    if weakly_usable_count > 0 and strongly_usable_count == 0:
        return "weak_official_match"
    if semantic_support_score >= 70:
        return "strong_official_match"
    if semantic_support_score >= 45:
        return "medium_official_match"
    if semantic_support_score >= 25:
        return "low_confidence_match"
    return "low_confidence_match"


def _comparison_status(
    semantic_support_score: int,
    conflict_signals: list[str],
    semantic_matched_concepts: list[str],
    verification_level: str,
) -> str:
    if verification_level == "official_access_failed":
        return "official_access_failed"
    if verification_level in {"official_document_not_found", "excluded_non_policy_page"}:
        return "official_evidence_missing"
    if conflict_signals and semantic_matched_concepts:
        return "official_conflict_possible"
    if verification_level in {"strong_official_match", "medium_official_match"}:
        return "official_support_found"
    if verification_level in {"weak_official_match", "low_confidence_match"}:
        return "unclear"
    if semantic_support_score < 35:
        return "official_evidence_missing"
    return "unclear"


def _make_summary(
    status: str,
    support_score: int,
    semantic_support_score: int,
    matched_keywords: list[str],
    semantic_matched_concepts: list[str],
    verification_level: str,
    official_evidence_results: list[dict],
    *,
    lane_b_upgraded: bool = False,
    primary_document_match: dict | None = None,
) -> str:
    # M22-1 — gated Lane-B branch. Fires FIRST and ONLY when a strong
    # Policy-Briefing match drove the medium upgrade (lane_b_upgraded=True;
    # default False → every existing branch below is reached byte-identically).
    # Describes a BODY match (never a semantic-concept match), inserts the live
    # official_direct_match_score, and keeps operator-review framing — no
    # "검증"/"확정"/"100%" overclaim, no semantic_matched_concepts reference.
    if lane_b_upgraded and primary_document_match:
        score = int(primary_document_match.get("score") or 0)
        return (
            f"정책브리핑 공식 보도자료 본문이 기사 핵심 주장과 직접 일치하는 것으로 "
            f"확인됩니다 (직접 매칭 점수 {score}점). 다만 최종 공개 전 사람 검토와 "
            f"원문 재확인이 필요합니다."
        )

    if status == "official_access_failed":
        return "\uacf5\uc2dd \uac80\uc0c9 \ud398\uc774\uc9c0 \uc811\uadfc\uc774 \uc2e4\ud328\ud574 \uc0c1\uc138 \uacf5\uc2dd\ubb38\uc11c\ub97c \ube44\uad50\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4."

    if status == "official_conflict_possible":
        return "\uc0c1\uc138 \uacf5\uc2dd\ubb38\uc11c\uc5d0 \ud574\uba85/\ubc18\ubc15/\ubbf8\ud655\uc815 \uc2e0\ud638\uac00 \uc788\uace0 \uad00\ub828 \uc815\ucc45 \uac1c\ub150\ub3c4 \ub9e4\uce6d\ub418\uc5b4 \ucda9\ub3cc \uac00\ub2a5\uc131\uc744 \ud655\uc778\ud574\uc57c \ud569\ub2c8\ub2e4."

    if status == "official_support_found":
        return (
            f"\uc0c1\uc138 \uacf5\uc2dd\ubb38\uc11c\uc640 \ub274\uc2a4 \uc8fc\uc7a5\uc758 \uc758\ubbf8 \uac1c\ub150\uc774 "
            f"{', '.join(semantic_matched_concepts)} \uc218\uc900\uc5d0\uc11c \ub9e4\uce6d\ub418\uc5b4 \uacf5\uc2dd \uadfc\uac70\uac00 \ube44\uad50\uc801 \uac15\ud569\ub2c8\ub2e4."
        )

    if verification_level == "excluded_non_policy_page":
        excluded = [
            result
            for result in official_evidence_results or []
            if result.get("should_exclude_from_verification") or result.get("evidence_grade") in {"D", "E", "F"}
        ]
        reasons = []
        has_detail_url = False
        for result in excluded[:2]:
            if result.get("selected_document_url") and result.get("is_detail_page"):
                has_detail_url = True
            label = result.get("document_type") or "non_policy_page"
            detail = "; ".join(result.get("classification_reasons") or [])
            reasons.append(f"{label}: {detail}" if detail else label)
        if has_detail_url:
            return (
                "상세 공식문서는 찾았지만 뉴스 핵심 주제와 불일치하여 검증 근거에서 제외했습니다. "
                + " / ".join(reasons)
            )
        return "수집된 공식 페이지가 검증 대상에서 제외됐습니다. " + " / ".join(reasons)

    if verification_level == "official_document_not_found":
        return "\uacf5\uc2dd \uac80\uc0c9 \ud398\uc774\uc9c0\ub294 \uc811\uadfc\ud588\uc9c0\ub9cc \ube44\uad50\ud560 \uc0c1\uc138 \uacf5\uc2dd\ubb38\uc11c\ub97c \ucc3e\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4."

    if verification_level == "low_confidence_match":
        return (
            f"공식 상세문서는 확보했지만 정책 키워드 또는 정책 대상 일치가 약합니다. "
            f"semantic score {semantic_support_score}점, keyword score {support_score}점이며 "
            f"매칭 개념은 {', '.join(semantic_matched_concepts) or '없음'}입니다."
        )

    if verification_level == "weak_official_match":
        weak_docs = [
            result
            for result in official_evidence_results or []
            if result.get("weakly_usable") and result.get("evidence_grade") in {"A", "B", "C"}
        ]
        detail = ""
        if weak_docs:
            first = weak_docs[0]
            detail = (
                f" 등급 {first.get('evidence_grade')}, 유형 {first.get('document_type')}, "
                f"개념점수 {first.get('concept_overlap_score')}, 주제점수 {first.get('topic_match_score')}입니다."
            )
        return (
            f"제한적인 공식 근거만 확인됐습니다.{detail} "
            f"정책명/대상/시행 내용이 뉴스 주장과 완전히 맞는지는 추가 확인이 필요합니다."
        )

    return (
        f"\uc0c1\uc138 \uacf5\uc2dd\ubb38\uc11c\uc640 \uc77c\ubd80 \uac1c\ub150\uc740 \ub9e4\uce6d\ub418\uc9c0\ub9cc "
        f"semantic score {semantic_support_score}\uc810\uc73c\ub85c \uc644\uc804\ud55c \uacf5\uc2dd \ud655\uc778\uc73c\ub85c\ub294 \ubd80\uc871\ud569\ub2c8\ub2e4."
    )


def _next_action(
    status: str, verification_level: str, *, lane_b_upgraded: bool = False,
) -> str:
    # M22-1 — gated Lane-B next-action. Fires only on the Policy-Briefing
    # upgrade path (default False → existing branches reached byte-identically).
    if lane_b_upgraded:
        return "정책브리핑 보도자료 원문의 발표일·시행일·지원/규제 대상을 확인하세요."
    if status == "official_access_failed":
        return "\uacf5\uc2dd\uae30\uad00 \uc811\uadfc \uc2e4\ud328 \uc0ac\uc720\ub97c \ud655\uc778\ud558\uace0 \ubcf4\ub3c4\uc790\ub8cc/\uacf5\uc9c0 \uac8c\uc2dc\ud310\uc744 \uc218\ub3d9 \uac80\uc0c9\ud558\uc138\uc694."
    if status == "official_conflict_possible":
        return "\ud574\uba85/\ubc18\ubc15/\ubbf8\ud655\uc815 \ubb38\uad6c\uac00 \ub274\uc2a4 \uc8fc\uc7a5\uc744 \uc815\uc815\ud558\ub294\uc9c0 \uc6d0\ubb38\uc744 \uc9c1\uc811 \ud655\uc778\ud558\uc138\uc694."
    if status == "official_support_found":
        return "\ub9e4\uce6d\ub41c \uacf5\uc2dd\ubb38\uc11c\uc758 \ubc1c\ud45c\uc77c, \uc2dc\ud589\uc77c, \uc9c0\uc6d0/\uaddc\uc81c \ub300\uc0c1\uc744 \ud655\uc778\ud558\uc138\uc694."
    if verification_level == "low_confidence_match":
        return "\uac80\uc0c9 \ud0a4\uc6cc\ub4dc\ub97c \uc0ac\uc5c5\uba85/\uc81c\ub3c4\uba85 \uc911\uc2ec\uc73c\ub85c \ub2e4\uc2dc \ub9cc\ub4e4\uc5b4 \uc0c1\uc138 \ubb38\uc11c\ub97c \uc7ac\uc218\uc9d1\ud558\uc138\uc694."
    if verification_level == "excluded_non_policy_page":
        return "검색 결과가 목록/안내/일반 정책정보 페이지로 치우쳐 있어 보도자료나 상세 정책문서 URL을 다시 수집하세요."
    if verification_level == "weak_official_match":
        return "\uc57d\ud55c \uad00\ub828\uc131\uc73c\ub85c \ube44\uad50\ub41c \uacf5\uc2dd\ubb38\uc11c\uc774\ubbc0\ub85c \uc81c\ub3c4\uba85/\ubc1c\ud45c\uc77c/\ub300\uc0c1\uc744 \uc6d0\ubb38\uc5d0\uc11c \uc218\ub3d9 \ud655\uc778\ud558\uc138\uc694."
    return "\ucd94\uac00 \uacf5\uc2dd \ucd9c\ucc98 \uc218\uc9d1 \ud6c4 \ub274\uc2a4 \uc8fc\uc7a5\uacfc \uc0c1\uc138 \ubb38\uc11c\uc758 \uac1c\ub150 \uc77c\uce58\ub97c \uc7ac\ud310\ub2e8\ud558\uc138\uc694."


def compare_news_with_official_evidence(
    news_title,
    news_summary,
    article_body,
    policy_claims,
    official_evidence_results,
    *,
    primary_document_match: dict | None = None,
) -> dict:
    counts = _evidence_access_counts(official_evidence_results)
    official_text = _build_official_text(official_evidence_results)
    document_text = _build_document_text(official_evidence_results)
    news_text = _news_text(news_title, news_summary, article_body, policy_claims)

    keywords = _extract_keywords(news_text, max_keywords=18)
    matched_keywords = [keyword for keyword in keywords if keyword in official_text]
    missing_keywords = [keyword for keyword in keywords if keyword not in official_text]

    if keywords:
        # audit §1.5 #5 (2026-05-26): support_score formula —
        # 70% weight on keyword-overlap ratio, 30% cap on corpus
        # access (doc fetches × 10 + searches × 5). See
        # docs/MAGIC_THRESHOLDS.md §11. The 70/30 split intentionally
        # privileges semantic overlap over raw access count.
        support_score = min(
            100,
            int(round((len(matched_keywords) / len(keywords)) * 70))
            + min(30, counts["document_success_count"] * 10 + counts["search_success_count"] * 5),
        )
    else:
        support_score = 0

    news_concepts = _detect_concepts(news_text)
    official_concepts = _detect_concepts(document_text or official_text)
    semantic_matched_concepts = sorted(set(news_concepts) & set(official_concepts))
    semantic_missing_concepts = sorted(set(news_concepts) - set(official_concepts))
    semantic_support_score = _semantic_score(
        news_concepts=news_concepts,
        official_concepts=official_concepts,
        document_success_count=counts["document_success_count"],
    )

    semantic_conflict_signals = _find_conflict_signals(document_text)
    conflict_signals = semantic_conflict_signals
    verification_level = _verification_level(
        semantic_support_score=semantic_support_score,
        document_success_count=counts["document_success_count"],
        document_found_count=counts["document_found_count"],
        search_success_count=counts["search_success_count"],
        weakly_usable_count=counts["weakly_usable_count"],
        strongly_usable_count=counts["strongly_usable_count"],
        excluded_non_policy_count=counts["excluded_non_policy_count"],
    )
    # M22-1 — Lane A↔B join. When Lane A found no real match but Lane B carries a
    # GENUINE strong Policy-Briefing official body match (and there is no
    # conflict to override), upgrade the categorical level to
    # "medium_official_match" (NEVER "strong_official_match", so the
    # draft_verified gate at verification_card.py:472 stays closed). Computed
    # BEFORE status so _comparison_status maps it coherently to
    # "official_support_found". semantic_support_score is left at its honest
    # Lane-A value (not faked). Gated so existing/no-Lane-B fixtures are
    # byte-identical; conflict precedence is preserved (skipped on conflict, and
    # _verdict_label evaluates conflicts first regardless).
    lane_b_upgraded = False
    if (
        not conflict_signals
        and verification_level in _LANE_B_UPGRADABLE_LEVELS
        and _is_strong_primary_document_match(primary_document_match)
    ):
        verification_level = "medium_official_match"
        lane_b_upgraded = True
    evidence_quality = _quality_from_score(
        semantic_support_score=semantic_support_score,
        document_success_count=counts["document_success_count"],
        document_found_count=counts["document_found_count"],
        search_success_count=counts["search_success_count"],
    )
    status = _comparison_status(
        semantic_support_score=semantic_support_score,
        conflict_signals=semantic_conflict_signals,
        semantic_matched_concepts=semantic_matched_concepts,
        verification_level=verification_level,
    )

    return {
        "comparison_status": status,
        "support_score": support_score,
        "semantic_support_score": semantic_support_score,
        "official_evidence_count": counts["official_evidence_count"],
        "fetched_success_count": counts["search_success_count"] + counts["document_success_count"],
        "search_success_count": counts["search_success_count"],
        "document_found_count": counts["document_found_count"],
        "document_success_count": counts["document_success_count"],
        "relevance_qualified_count": counts["relevance_qualified_count"],
        "weakly_usable_count": counts["weakly_usable_count"],
        "strongly_usable_count": counts["strongly_usable_count"],
        "excluded_non_policy_count": counts["excluded_non_policy_count"],
        "matched_keywords": matched_keywords,
        "missing_keywords": missing_keywords,
        "conflict_signals": conflict_signals,
        "semantic_matched_concepts": semantic_matched_concepts,
        "semantic_missing_concepts": semantic_missing_concepts,
        "semantic_conflict_signals": semantic_conflict_signals,
        "evidence_quality": evidence_quality,
        "verification_level": verification_level,
        "comparison_summary": _make_summary(
            status=status,
            support_score=support_score,
            semantic_support_score=semantic_support_score,
            matched_keywords=matched_keywords,
            semantic_matched_concepts=semantic_matched_concepts,
            verification_level=verification_level,
            official_evidence_results=official_evidence_results,
            lane_b_upgraded=lane_b_upgraded,
            primary_document_match=primary_document_match,
        ),
        "relevance_filter_summary": (
            f"{counts['relevance_qualified_count']} official documents passed usable=true >= 40 "
            f"or weakly_usable=true >= 35 "
            f"(strong={counts['strongly_usable_count']}, weak={counts['weakly_usable_count']})."
        ),
        "recommended_next_action": _next_action(
            status, verification_level, lane_b_upgraded=lane_b_upgraded,
        ),
    }


def print_evidence_comparison(evidence_comparison: dict):
    log.info("\n----- News vs official evidence comparison -----")
    log.info(f"comparison_status: {evidence_comparison.get('comparison_status')}")
    log.info(f"support_score: {evidence_comparison.get('support_score')}")
    log.info(f"semantic_support_score: {evidence_comparison.get('semantic_support_score')}")
    log.info(f"matched_keywords: {', '.join(evidence_comparison.get('matched_keywords', []))}")
    log.info(f"missing_keywords: {', '.join(evidence_comparison.get('missing_keywords', []))}")
    log.info(f"conflict_signals: {', '.join(evidence_comparison.get('conflict_signals', []))}")
    log.info(f"semantic_matched_concepts: {', '.join(evidence_comparison.get('semantic_matched_concepts', []))}")
    log.info(f"semantic_missing_concepts: {', '.join(evidence_comparison.get('semantic_missing_concepts', []))}")
    log.info(f"evidence_quality: {evidence_comparison.get('evidence_quality')}")
    log.info(f"verification_level: {evidence_comparison.get('verification_level')}")
    log.info(f"relevance_filter_summary: {evidence_comparison.get('relevance_filter_summary')}")
    log.info(f"comparison_summary: {evidence_comparison.get('comparison_summary')}")
    log.info(f"recommended_next_action: {evidence_comparison.get('recommended_next_action')}")
