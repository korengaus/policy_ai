"""Centralized Korean keyword constants for policy_ai (Phase 2 M11.2).

Single source of truth for the Korean keyword sets that were
previously declared as module-level literals across multiple files.
Each constant in this module is **pure data** — no logic, no I/O, no
side effects on import.

What this module DOES
---------------------

It declares one named ``frozenset`` (or ``tuple`` when order matters)
per previously-duplicated keyword group. Source files import the
named constant they need; they no longer carry their own copy.

What this module DOES NOT do
----------------------------

It does NOT silently union near-duplicates. The audit found that
several "duplicated" lists across files have OVERLAPPING but
NOT IDENTICAL contents — for example, ``MOJIBAKE_MARKERS`` in
``text_utils.py`` and ``article_extractor.py`` share six markers but
each has six unique to itself. Unioning them would broaden mojibake
detection at both call sites, which is a semantic change.

The conservative rule: **when contents differ, declare two separate
named constants**. Each call site keeps its original behaviour.
``docs/KOREAN_CONSTANTS.md`` records which constants were kept
separate and why.

Hard contract
-------------

    * Every constant is a ``frozenset`` or a ``tuple`` (immutable).
    * No constant is empty.
    * No keyword has leading or trailing whitespace.
    * Every keyword is valid Korean / ASCII / decodable UTF-8.
    * No constant has ``truth_claim`` semantics; this module is data.
    * For every main constant ``X``, a ``TEST_X_MIN`` subset constant
      pins the keywords that must remain. ``tests/test_korean_constants.py``
      asserts ``TEST_X_MIN <= X`` so accidental removals fail
      immediately.

Maintenance rules
-----------------

    1. Add keywords only after operator review.
    2. Never remove a keyword without confirming no call site relied
       on its presence.
    3. If two constants ought to be unified, do that in a separate
       milestone with operator approval — not in M11.2.

Public surface (stable, pinned by tests)
----------------------------------------

    # CONCEPT SYNONYMS (kept separate — different values)
    CONCEPT_SYNONYMS_RELEVANCE
    CONCEPT_SYNONYMS_COMPARATOR
    CONCEPT_GROUPS_OFFICIAL_BODY

    # MOJIBAKE MARKERS (kept separate — different values)
    MOJIBAKE_MARKERS_TEXT_UTILS                       (tuple, ordered)
    MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR                (tuple, ordered)

    # STOPWORDS (kept separate — different values)
    STOPWORDS_OFFICIAL_BODY
    STOPWORDS_COMPARATOR

    # HOUSING TOPIC TERMS (single source, but lifted here for
    # discoverability under the centralization audit)
    HOUSING_QUERY_TERMS
    HOUSING_DOCUMENT_TERMS

    # POLICY ACTION KEYWORDS (single source, lifted for the same reason)
    POLICY_ACTION_KEYWORDS                            (tuple, ordered)

    # Regression-safety pins
    TEST_*_MIN — subset of each main constant
"""

from __future__ import annotations

from typing import FrozenSet, Mapping, Tuple


# ============================================================
# CONCEPT SYNONYMS
# Used by: official_relevance.py, evidence_comparator.py,
#          official_source_body.py
# Purpose: map Korean policy concepts to alternative phrasings.
#
# CONCEPT_SYNONYMS_RELEVANCE and CONCEPT_SYNONYMS_COMPARATOR were
# declared independently in the two files. Their key sets and value
# lists overlap but are not identical (the relevance variant adds an
# ``official_statement`` key and richer synonym lists). They are kept
# separate; unioning would broaden concept matching at both sites.
# CONCEPT_GROUPS_OFFICIAL_BODY uses a different key vocabulary
# (housing_finance, real_estate, rate_policy, …) and is a third,
# distinct mapping.
# ============================================================


# Source: official_relevance.py:4-14 (M11.2 audit)
CONCEPT_SYNONYMS_RELEVANCE: Mapping[str, Tuple[str, ...]] = {
    "rental_loan": (
        "전세대출", "전세자금", "버팀목", "임차보증금", "전세자금대출",
    ),
    "mortgage_loan": (
        "주택담보대출", "주담대", "담보대출",
    ),
    "interest_rate": (
        "금리", "이자", "우대금리", "감면",
    ),
    "regulation": (
        "규제", "제한", "차단", "관리강화", "가계부채 관리", "가계부채",
    ),
    "subsidy_support": (
        "지원", "보조", "보조금", "이차보전", "주거비", "혜택",
    ),
    "target_group": (
        "청년", "신혼부부", "자녀출산", "중소기업 근로자",
        "중소기업", "근로자", "1주택자", "유주택자",
    ),
    "implementation": (
        "시행", "운영", "신청", "모집", "공고", "접수", "적용",
    ),
    "review_stage": (
        "검토", "추진", "조사", "착수", "논의", "현황", "파악",
    ),
    "official_statement": (
        "발표", "보도자료", "설명자료", "브리핑", "공지",
    ),
}


# Source: evidence_comparator.py:39-110 (M11.2 audit)
CONCEPT_SYNONYMS_COMPARATOR: Mapping[str, Tuple[str, ...]] = {
    "rental_loan": (
        "전세대출", "버팀목", "전세자금", "전세자금대출",
        "임차보증금", "전세금", "전세",
    ),
    "mortgage_loan": (
        "주택담보대출", "주담대", "담보대출", "주택 담보",
    ),
    "interest_rate": (
        "금리", "이자", "우대금리", "감면", "이자지원", "이차보전",
    ),
    "regulation": (
        "규제", "제한", "차단", "관리강화", "가계부채 관리",
        "가계부채", "대출규제",
    ),
    "subsidy_support": (
        "지원", "보조", "보조금", "이차보전", "주거비", "혜택", "우대",
    ),
    "target_group": (
        "청년", "신혼부부", "자녀출산", "중소기업 근로자",
        "중소기업", "근로자", "1주택자", "유주택자",
    ),
    "implementation": (
        "시행", "운영", "신청", "모집", "공고", "적용", "접수", "시작",
    ),
    "review_stage": (
        "검토", "추진", "조사", "착수", "논의", "파악", "현황",
    ),
}


# Source: official_source_body.py:95-103 (M11.2 audit)
# Different key vocabulary entirely — this is NOT a copy of the
# above; it maps to concept buckets specific to the official-body
# comparator.
CONCEPT_GROUPS_OFFICIAL_BODY: Mapping[str, Tuple[str, ...]] = {
    "housing_finance": (
        "전세대출", "전세자금", "버팀목",
        "주택담보대출", "주담대", "주택금융",
    ),
    "real_estate": (
        "부동산", "주택", "실거주자", "양도세",
        "다주택", "청약", "임대차",
    ),
    "rate_policy": (
        "금리", "기준금리", "인하", "인상", "통화정책", "물가",
    ),
    "financial_regulation": (
        "DSR", "규제", "제한", "가계부채", "연체율", "감독",
    ),
    "social_finance": (
        "사회연대경제조직", "사회연대금융",
        "사회연대금융협의회", "금융지원",
    ),
    "jeonse_fraud": (
        "전세사기", "전세보증", "보증금", "임대인",
    ),
    "tax_investigation": (
        "국세청", "양도세", "양도소득세",
        "세무조사", "탈루", "가상자산",
    ),
}


# ============================================================
# MOJIBAKE MARKERS
# Used by: text_utils.py, article_extractor.py
# Purpose: detect encoding-corrupted text.
#
# Two distinct tuples. The intersection is six markers
# (ë, ì, ê, í, Ã, Â); the difference contains markers each file
# uses for the specific corruption patterns it encounters. KEPT
# SEPARATE — unioning would change the score thresholds at the
# call sites.
# ============================================================


# Source: text_utils.py:10-23 (M11.2 audit)
# Originally a tuple; preserved as a tuple to keep call-site behaviour
# identical (`any(marker in text for marker in MARKERS)` iterates
# in insertion order).
MOJIBAKE_MARKERS_TEXT_UTILS: Tuple[str, ...] = (
    "ë", "ì", "ê", "í", "Ã", "Â", "ð",
    "챙", "챠", "챘", "횂", "占",
)


# Source: article_extractor.py:44 (M11.2 audit)
# Originally a list; preserved as a tuple (immutable) — no call site
# mutates it.
MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR: Tuple[str, ...] = (
    "ì", "í", "ë", "ê", "Â", "Ã", "�",
    "媛", "쒓", "뺤", "댁", "齊",
)


# ============================================================
# STOPWORDS
# Used by: official_source_body.py, evidence_comparator.py
# Purpose: drop high-frequency Korean tokens during tokenization.
#
# Two distinct sets. The intersection is the bulk of common Korean
# noise tokens; each variant adds a few site-specific extras (e.g.,
# ``밝혔다`` / ``전했다`` in evidence_comparator). KEPT SEPARATE —
# unioning would drop slightly more tokens at both sites and change
# the matched-term counts.
# ============================================================


# Source: official_source_body.py:74-92 (M11.2 audit)
STOPWORDS_OFFICIAL_BODY: FrozenSet[str] = frozenset({
    "그리고", "그러나", "있는", "없는", "대한", "관련",
    "이번", "기사", "뉴스", "정부", "정책", "것으로",
    "한다고", "했다", "한다", "있다", "없다",
})


# Source: evidence_comparator.py:18-37 (M11.2 audit)
STOPWORDS_COMPARATOR: FrozenSet[str] = frozenset({
    "관련", "기사", "정책", "정부", "오늘", "이번", "해당",
    "대해", "등을", "등이", "밝혔다", "전했다", "한다", "있다",
    "없다", "위해", "이후", "및",
})


# ============================================================
# HOUSING TOPIC TERMS
# Used by: verification_card.py
# Purpose: detect housing-policy-related queries / documents.
# Lifted here for discoverability — single source today, but the
# audit explicitly listed verification_card.py as part of the
# Korean-keyword surface to centralize.
# ============================================================


# Source: verification_card.py:47-58 (M11.2 audit)
HOUSING_QUERY_TERMS: FrozenSet[str] = frozenset({
    "부동산", "주거", "주택", "전세", "월세", "임대",
    "양도세", "공급", "재건축", "재개발",
})


# Source: verification_card.py:60-73 (M11.2 audit)
HOUSING_DOCUMENT_TERMS: FrozenSet[str] = frozenset({
    "부동산", "주거", "주택", "전세", "월세", "임대",
    "양도세", "공급", "재건축", "재개발", "보증금", "세입자",
})


# ============================================================
# POLICY ACTION KEYWORDS
# Used by: verification_card.py
# Purpose: boost relevance scores for sentences that look like
# they describe a policy action.
# Lifted here for discoverability. Originally a list; preserved as
# a tuple — the call site iterates with `any(... in sentence for ...)`
# which does not require mutability.
# ============================================================


# Source: verification_card.py:93-113 (M11.2 audit)
POLICY_ACTION_KEYWORDS: Tuple[str, ...] = (
    "검토", "추진", "발표", "조사", "착수",
    "시행", "확대", "축소", "제한", "차단",
    "금지", "지원", "감면", "인하", "인상",
    "대출", "금리", "규제", "정책",
)


# ============================================================
# LOW-LEVEL RISK / IMPACT KEYWORDS
# Used by: policy_confidence.py (_risk_level), policy_impact.py (_impact_level)
# Purpose: detect low-severity policy signals when no HIGH or MEDIUM
# keyword matched.
#
# audit §1.5 #3 re-audit (2026-05-26): the two tuples below are
# SET-EQUAL — both wrap {행사, 발언, 제언, 설명, 전망}. M11.2 did not
# catch this because the M11.2 audit treated them as belonging to
# separate single-source files. Each consumer uses
# `for kw in TUPLE: if kw in text: return ..., kw` — first match wins
# and feeds into the human-readable `confidence_reasons` /
# `impact_reasons` strings. The two tuples differ ONLY in the order
# of the last two items (설명 ↔ 전망), so each consumer's first-match
# behavior is preserved exactly when imported with its original
# ordering. KEPT AS TWO SEPARATELY NAMED TUPLES to preserve
# byte-identical reason-string output per consumer.
# ============================================================


# Source: policy_confidence.py:31-37 (audit §1.5 #3 re-audit). The
# trailing 설명 → 전망 order is preserved verbatim so `_risk_level`'s
# first-match keyword for "low risk keyword detected: ..." prose
# remains byte-identical when both 설명 and 전망 appear in text.
LOW_RISK_KEYWORDS_POLICY_CONFIDENCE: Tuple[str, ...] = (
    "행사",
    "발언",
    "제언",
    "설명",
    "전망",
)


# Source: policy_impact.py:48 (audit §1.5 #3 re-audit). The trailing
# 전망 → 설명 order is preserved verbatim so `_impact_level`'s
# first-match keyword for "low impact keyword detected: ..." prose
# remains byte-identical when both 전망 and 설명 appear in text.
# Set-equal to LOW_RISK_KEYWORDS_POLICY_CONFIDENCE — pinned by
# tests/test_keyword_consolidation.py::LowKeywordSetEquivalencePin.
LOW_IMPACT_KEYWORDS_POLICY_IMPACT: Tuple[str, ...] = (
    "행사",
    "발언",
    "제언",
    "전망",
    "설명",
)


# ============================================================
# REGRESSION-SAFETY PINS — DO NOT MODIFY WITHOUT OPERATOR REVIEW
# Each TEST_*_MIN constant is a strict subset of its corresponding
# main constant. Used by tests/test_korean_constants.py to catch
# accidental keyword removals. A failure here means a future edit
# removed a keyword the project pinned as required.
# ============================================================


TEST_CONCEPT_SYNONYMS_RELEVANCE_MIN: Mapping[str, Tuple[str, ...]] = {
    "rental_loan": ("전세대출", "전세자금"),
    "mortgage_loan": ("주택담보대출", "주담대"),
    "interest_rate": ("금리",),
    "regulation": ("규제", "제한", "차단"),
    "target_group": ("청년", "1주택자"),
}


TEST_CONCEPT_SYNONYMS_COMPARATOR_MIN: Mapping[str, Tuple[str, ...]] = {
    "rental_loan": ("전세대출", "버팀목", "전세"),
    "mortgage_loan": ("주택담보대출", "주담대"),
    "interest_rate": ("금리",),
    "regulation": ("규제", "차단"),
}


TEST_CONCEPT_GROUPS_OFFICIAL_BODY_MIN: Mapping[str, Tuple[str, ...]] = {
    "housing_finance": ("전세대출", "주택담보대출"),
    "real_estate": ("부동산", "주택"),
    "rate_policy": ("금리",),
    "jeonse_fraud": ("전세사기", "보증금"),
}


TEST_MOJIBAKE_MARKERS_TEXT_UTILS_MIN: Tuple[str, ...] = (
    "ë", "ì", "ê", "í",
)


TEST_MOJIBAKE_MARKERS_ARTICLE_EXTRACTOR_MIN: Tuple[str, ...] = (
    "ì", "í", "ë", "ê", "�",
)


TEST_STOPWORDS_OFFICIAL_BODY_MIN: FrozenSet[str] = frozenset({
    "그리고", "그러나", "관련", "정부", "정책",
})


TEST_STOPWORDS_COMPARATOR_MIN: FrozenSet[str] = frozenset({
    "관련", "기사", "정부", "이번", "위해",
})


TEST_HOUSING_QUERY_TERMS_MIN: FrozenSet[str] = frozenset({
    "전세", "월세", "주택", "임대", "주거",
})


TEST_HOUSING_DOCUMENT_TERMS_MIN: FrozenSet[str] = frozenset({
    "전세", "월세", "주택", "임대", "주거", "보증금",
})


TEST_POLICY_ACTION_KEYWORDS_MIN: Tuple[str, ...] = (
    "검토", "발표", "시행", "제한", "지원", "규제", "정책",
)


# audit §1.5 #3 re-audit (2026-05-26). Both LOW_* tuples are
# set-equal, so the same minimum-subset pin applies to each. Two
# distinct constants keep the per-consumer naming explicit.
TEST_LOW_RISK_KEYWORDS_POLICY_CONFIDENCE_MIN: Tuple[str, ...] = (
    "행사", "발언", "전망",
)


TEST_LOW_IMPACT_KEYWORDS_POLICY_IMPACT_MIN: Tuple[str, ...] = (
    "행사", "발언", "전망",
)
