"""Phase 2 M5.7: critical-fact guardrails for semantic evidence matching.

When a claim says "100만원" but the matched official text says "50만원", their
cosine similarity is still very high — most tokens overlap. The same is
true for "2026년 시행" vs "2025년 시범", or "누구나 신청 가능" vs
"소득 요건 충족자만". This module extracts the *critical* factual elements
of both texts, compares them, and reports risk flags + a support cap so the
semantic agent never overstates support when these elements disagree.

Strict design contract:
    * Pure standard-library Korean regex extraction. No network, no
      external library, no embedding call.
    * Deterministic. Same inputs always produce the same outputs.
    * Never raises on bad input — empty/None/wrong-type returns the safe
      empty shape.
    * No verdict effect. The output is consumed only by
      ``semantic_evidence_agent`` as metadata.
    * Be conservative — a false positive (cap to ``weak``) is safer than a
      false confidence.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")
_KOREAN_PUNCT_MAP = str.maketrans({
    "·": " ",
    "・": " ",
    "‧": " ",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
})


def normalize_fact_text(text: object) -> str:
    """Trim, collapse whitespace, normalize Korean punctuation. Never raises."""
    if text is None:
        return ""
    try:
        raw = str(text)
    except Exception:
        return ""
    # NFKC folds full-width digits/punct to ASCII so the regex extractors hit.
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.translate(_KOREAN_PUNCT_MAP)
    # Lowercase Latin runs only; Korean letters are case-less so this is safe.
    raw = "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in raw)
    raw = _WHITESPACE.sub(" ", raw)
    return raw.strip()


# ---------------------------------------------------------------------------
# Number extraction
# ---------------------------------------------------------------------------

# A money/count/percent token: optional digit-group (e.g. 1,000) with optional
# decimal, optionally followed by a Korean unit. Years (YYYY년) are handled
# separately and excluded from this match.
_NUMBER_RE = re.compile(
    r"""
    (?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)   # the digits
    \s*
    (?P<unit>만원|억원|조원|원|퍼센트|%|명|건|개|가구|세대|회|일|주|개월|년치)?
    """,
    re.VERBOSE,
)

# Year pattern — we exclude these from ordinary number extraction so a year
# like "2026" is not mistaken for an amount.
_YEAR_RE = re.compile(r"(?P<year>\d{4})\s*년")


def _strip_commas(value: str) -> float:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def extract_numbers(text: object) -> List[dict]:
    """Return number tokens found in ``text``.

    Date components (``2026년``, ``5월``) are intentionally excluded so they
    don't compete with monetary amounts during number-mismatch comparison.
    The exclusion is by character span — any candidate that overlaps a year
    or month span is dropped.
    """
    normalized = normalize_fact_text(text)
    if not normalized:
        return []
    # Collect year and "[M]월" spans so we can skip them when scanning for numbers.
    excluded_spans: List[tuple[int, int]] = []
    for match in _YEAR_RE.finditer(normalized):
        excluded_spans.append(match.span())
    for match in _DATE_MONTH_ONLY.finditer(normalized):
        excluded_spans.append(match.span())

    out: List[dict] = []
    for match in _NUMBER_RE.finditer(normalized):
        span = match.span()
        # Skip year matches: a 4-digit number immediately followed by "년".
        next_token = normalized[span[1]:span[1] + 1]
        if (
            len(match.group("value").replace(",", "").split(".")[0]) == 4
            and not match.group("unit")
            and next_token == "년"
        ):
            continue
        # Drop if the candidate overlaps a year or month span.
        if any(es <= span[0] < ee for (es, ee) in excluded_spans):
            continue
        unit = match.group("unit") or ""
        raw = match.group(0).strip()
        value = _strip_commas(match.group("value"))
        out.append({
            "raw": raw,
            "value": value,
            "unit": unit,
            "normalized": f"{int(value) if value.is_integer() else value}{unit}",
        })
    return out


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

# Three accepted forms: "YYYY년 [M]월", "YYYY[.-/]M[M]", "[M]월" (relative).
_DATE_YYYYMM_KO = re.compile(r"(?P<year>\d{4})\s*년(?:\s*(?P<month>\d{1,2})\s*월)?")
_DATE_YYYYMM_SEP = re.compile(r"(?<!\d)(?P<year>\d{4})\s*[-./]\s*(?P<month>\d{1,2})(?!\d)")
_DATE_MONTH_ONLY = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*월(?!말|차)")


def extract_dates(text: object) -> List[dict]:
    """Detect year+month / year-only / month-only references."""
    normalized = normalize_fact_text(text)
    if not normalized:
        return []
    seen_spans: List[tuple[int, int]] = []
    out: List[dict] = []

    def _add(year: Optional[int], month: Optional[int], raw: str, span: tuple[int, int]) -> None:
        for s, e in seen_spans:
            if span[0] < e and s < span[1]:
                return  # overlaps a span we already produced
        seen_spans.append(span)
        normalized_str = ""
        if year and month:
            normalized_str = f"{year:04d}-{month:02d}"
        elif year:
            normalized_str = f"{year:04d}"
        elif month:
            normalized_str = f"--{month:02d}"
        out.append({
            "raw": raw.strip(),
            "year": year,
            "month": month,
            "normalized": normalized_str,
        })

    for match in _DATE_YYYYMM_KO.finditer(normalized):
        year = int(match.group("year"))
        month_str = match.group("month")
        month = int(month_str) if month_str else None
        _add(year, month, match.group(0), match.span())
    for match in _DATE_YYYYMM_SEP.finditer(normalized):
        year = int(match.group("year"))
        month = int(match.group("month"))
        if 1 <= month <= 12:
            _add(year, month, match.group(0), match.span())
    for match in _DATE_MONTH_ONLY.finditer(normalized):
        month = int(match.group("month"))
        if 1 <= month <= 12:
            _add(None, month, match.group(0), match.span())
    return out


# ---------------------------------------------------------------------------
# Eligibility extraction
# ---------------------------------------------------------------------------

_UNIVERSAL_PATTERNS = [
    "누구나", "누구든", "모두", "전원", "제한 없이", "제한없이",
    "모든 국민", "모든 청년", "모든 가구", "신청하면 받을 수",
    "전 국민", "전체 대상",
]

_RESTRICTION_PATTERNS = [
    "소득 조건", "소득기준", "소득 기준", "거주 요건", "거주요건",
    "자격 요건", "자격요건", "대상자", "충족한 사람", "충족한 가구",
    "일부", "한정", "선별", "조건을 만족", "조건 충족",
    "eligible", "eligibility", "한해", "한하여", "한해서",
    "기준을 충족", "특정", "선정", "심사",
]


def _find_terms(text: str, terms: List[str]) -> List[str]:
    found: List[str] = []
    for term in terms:
        if term in text:
            found.append(term)
    return found


def extract_eligibility_terms(text: object) -> dict:
    normalized = normalize_fact_text(text)
    universal = _find_terms(normalized, _UNIVERSAL_PATTERNS)
    restriction = _find_terms(normalized, _RESTRICTION_PATTERNS)
    return {
        "universal_terms": universal,
        "restriction_terms": restriction,
        "has_universal_claim": bool(universal),
        "has_restriction": bool(restriction),
    }


# ---------------------------------------------------------------------------
# Finality extraction
# ---------------------------------------------------------------------------

# Finality terms must be checked alongside negative companions like
# "확정되지 않" so we don't count "확정되지 않았다" as a finality marker.
_FINAL_TERMS = ["확정", "최종 확정", "최종확정", "시행", "발표", "승인", "결정", "공포"]
_TENTATIVE_PATTERNS = [
    "검토 중", "검토중", "협의 중", "협의중", "논의 중", "논의중",
    "추진 예정", "추진예정", "시행 예정", "시행예정",
    "예정", "미정", "시범 운영", "시범운영", "시범",
    "확정되지 않", "확정되지않", "아직 확정", "확정 전",
    "추후 공지", "추후공지", "검토 단계",
]

# Token that, if present, neutralizes the parallel finality marker.
_FINALITY_NEUTRALIZERS = [
    "확정되지", "확정 안", "결정되지 않", "발표되지 않",
    "시행되지 않", "승인되지 않",
]


def extract_finality_terms(text: object) -> dict:
    normalized = normalize_fact_text(text)
    final_hits = _find_terms(normalized, _FINAL_TERMS)
    tentative_hits = _find_terms(normalized, _TENTATIVE_PATTERNS)
    neutralizers = _find_terms(normalized, _FINALITY_NEUTRALIZERS)
    # If a negated-finality token is present, drop the parallel positive
    # finality term so "확정되지 않았다" doesn't register as 확정.
    if neutralizers:
        final_hits = [term for term in final_hits if term not in {"확정", "결정", "발표", "시행", "승인"}]
    return {
        "final_terms": final_hits,
        "tentative_terms": tentative_hits,
        "has_finality": bool(final_hits),
        "has_tentative": bool(tentative_hits),
    }


# ---------------------------------------------------------------------------
# Negation extraction
# ---------------------------------------------------------------------------

_NEGATION_PATTERNS = [
    "아니다", "아닙니다", "사실이 아", "사실 아",
    "하지 않", "않습니다", "중단", "취소",
    "반박", "부인", "허위", "보류",
    "철회", "거부", "오보", "정정",
]


def extract_negation_terms(text: object) -> dict:
    normalized = normalize_fact_text(text)
    hits = _find_terms(normalized, _NEGATION_PATTERNS)
    return {
        "negation_terms": hits,
        "has_negation": bool(hits),
    }


# ---------------------------------------------------------------------------
# Aggregated extraction
# ---------------------------------------------------------------------------

_TEXT_PREVIEW_LIMIT = 200


def extract_critical_facts(text: object) -> dict:
    normalized = normalize_fact_text(text)
    return {
        "numbers": extract_numbers(normalized),
        "dates": extract_dates(normalized),
        "eligibility": extract_eligibility_terms(normalized),
        "finality": extract_finality_terms(normalized),
        "negation": extract_negation_terms(normalized),
        "text_preview": normalized[:_TEXT_PREVIEW_LIMIT],
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

# Support level ordering used to compute the cap. ``unavailable`` is below
# ``weak``; we never raise the cap above ``strong`` (i.e. no cap means
# "strong allowed").
_SUPPORT_RANK = {"unavailable": 0, "weak": 1, "contextual": 2, "strong": 3}


def _cap_level(current: Optional[str], cap: str) -> str:
    """Return the lower of ``current`` and ``cap``. ``current=None`` means no prior cap."""
    if current is None:
        return cap
    if _SUPPORT_RANK.get(cap, 3) < _SUPPORT_RANK.get(current, 3):
        return cap
    return current


def _same_number_unit(claim_num: dict, source_num: dict) -> bool:
    """Compare two number dicts by unit. Treat blank unit as compatible only with blank unit."""
    if not claim_num.get("unit") and not source_num.get("unit"):
        return True
    return claim_num.get("unit") == source_num.get("unit")


def _has_matching_number(claim_num: dict, source_numbers: List[dict]) -> bool:
    for src in source_numbers:
        if _same_number_unit(claim_num, src) and claim_num.get("value") == src.get("value"):
            return True
    return False


def _conflicting_number(claim_num: dict, source_numbers: List[dict]) -> Optional[dict]:
    """A source number that shares the unit but disagrees on value."""
    for src in source_numbers:
        if _same_number_unit(claim_num, src) and claim_num.get("value") != src.get("value"):
            return src
    return None


def _has_matching_date(claim_date: dict, source_dates: List[dict]) -> bool:
    claim_year = claim_date.get("year")
    claim_month = claim_date.get("month")
    for src in source_dates:
        if claim_year is not None and src.get("year") != claim_year:
            continue
        if claim_month is not None and src.get("month") not in (None, claim_month):
            continue
        return True
    return False


def _conflicting_date(claim_date: dict, source_dates: List[dict]) -> Optional[dict]:
    claim_year = claim_date.get("year")
    claim_month = claim_date.get("month")
    for src in source_dates:
        if claim_year is not None and src.get("year") is not None and src.get("year") != claim_year:
            return src
        if (
            claim_year is not None
            and claim_year == src.get("year")
            and claim_month is not None
            and src.get("month") is not None
            and claim_month != src.get("month")
        ):
            return src
    return None


def compare_critical_facts(claim_text: object, source_text: object) -> dict:
    """Compare critical facts of claim vs. source. Return risk flags + cap.

    Output is a dict with:
        * ``risk_flags``: short labels for the kinds of mismatch / missing fact
          observed.
        * ``mismatches``: detailed dicts for each detected mismatch.
        * ``missing_claim_facts``: claim facts that don't appear in the source.
        * ``matched_claim_facts``: claim facts that the source confirms.
        * ``has_critical_mismatch`` / ``has_missing_critical_fact``: booleans.
        * ``support_cap``: ``strong`` (no cap), ``contextual``, or ``weak``.
        * ``support_cap_reason``: short explanation for diagnostics.
    """
    claim_facts = extract_critical_facts(claim_text)
    source_facts = extract_critical_facts(source_text)

    risk_flags: List[str] = []
    mismatches: List[dict] = []
    missing_claim_facts: List[dict] = []
    matched_claim_facts: List[dict] = []
    support_cap: Optional[str] = None
    cap_reasons: List[str] = []

    def _set_cap(new_cap: str, reason: str) -> None:
        nonlocal support_cap
        old = support_cap
        support_cap = _cap_level(support_cap, new_cap)
        if support_cap != old:
            cap_reasons.insert(0, reason)
        elif reason not in cap_reasons:
            cap_reasons.append(reason)

    # --- Numbers ---
    for claim_num in claim_facts["numbers"]:
        conflict = _conflicting_number(claim_num, source_facts["numbers"])
        if conflict is not None:
            if "number_mismatch" not in risk_flags:
                risk_flags.append("number_mismatch")
            mismatches.append({
                "type": "number_mismatch",
                "claim_value": claim_num.get("raw"),
                "source_value": conflict.get("raw"),
                "reason": (
                    f"claim says {claim_num.get('raw')} but source says "
                    f"{conflict.get('raw')} (same unit, different value)"
                ),
            })
            _set_cap("weak", "number_mismatch")
            continue
        if _has_matching_number(claim_num, source_facts["numbers"]):
            matched_claim_facts.append({"type": "number", "value": claim_num.get("raw")})
        else:
            missing_claim_facts.append({"type": "number", "value": claim_num.get("raw")})
            if "missing_critical_fact" not in risk_flags:
                risk_flags.append("missing_critical_fact")
            mismatches.append({
                "type": "missing_critical_fact",
                "claim_value": claim_num.get("raw"),
                "source_value": None,
                "reason": f"claim mentions {claim_num.get('raw')} but source has no matching amount",
            })
            _set_cap("contextual", "missing_critical_amount")

    # --- Dates ---
    for claim_date in claim_facts["dates"]:
        conflict = _conflicting_date(claim_date, source_facts["dates"])
        if conflict is not None:
            if "date_mismatch" not in risk_flags:
                risk_flags.append("date_mismatch")
            mismatches.append({
                "type": "date_mismatch",
                "claim_value": claim_date.get("raw"),
                "source_value": conflict.get("raw"),
                "reason": (
                    f"claim says {claim_date.get('raw')} but source says "
                    f"{conflict.get('raw')}"
                ),
            })
            _set_cap("weak", "date_mismatch")
            continue
        if _has_matching_date(claim_date, source_facts["dates"]):
            matched_claim_facts.append({"type": "date", "value": claim_date.get("raw")})
        else:
            # Source lacks the specific date but doesn't contradict it.
            missing_claim_facts.append({"type": "date", "value": claim_date.get("raw")})
            if "missing_critical_fact" not in risk_flags:
                risk_flags.append("missing_critical_fact")
            mismatches.append({
                "type": "missing_critical_fact",
                "claim_value": claim_date.get("raw"),
                "source_value": None,
                "reason": f"claim mentions date {claim_date.get('raw')} but source has no matching date",
            })
            _set_cap("contextual", "missing_critical_date")

    # --- Eligibility ---
    claim_elig = claim_facts["eligibility"]
    source_elig = source_facts["eligibility"]
    if claim_elig["has_universal_claim"] and source_elig["has_restriction"]:
        if "eligibility_mismatch" not in risk_flags:
            risk_flags.append("eligibility_mismatch")
        mismatches.append({
            "type": "eligibility_mismatch",
            "claim_value": ", ".join(claim_elig["universal_terms"]),
            "source_value": ", ".join(source_elig["restriction_terms"]),
            "reason": "claim asserts universal eligibility but source describes restrictions",
        })
        _set_cap("weak", "eligibility_mismatch")

    # --- Finality ---
    claim_final = claim_facts["finality"]
    source_final = source_facts["finality"]
    if claim_final["has_finality"] and source_final["has_tentative"]:
        if "finality_mismatch" not in risk_flags:
            risk_flags.append("finality_mismatch")
        mismatches.append({
            "type": "finality_mismatch",
            "claim_value": ", ".join(claim_final["final_terms"]),
            "source_value": ", ".join(source_final["tentative_terms"]),
            "reason": "claim treats the policy as final but source describes it as tentative",
        })
        _set_cap("weak", "finality_mismatch")

    # --- Negation ---
    source_neg = source_facts["negation"]
    if source_neg["has_negation"]:
        if "negation_mismatch" not in risk_flags:
            risk_flags.append("negation_mismatch")
        mismatches.append({
            "type": "negation_mismatch",
            "claim_value": None,
            "source_value": ", ".join(source_neg["negation_terms"]),
            "reason": "source contains negation/refutation language",
        })
        _set_cap("weak", "source_negation_present")

    # Final shape.
    final_cap = support_cap or "strong"
    reason_str = "; ".join(cap_reasons) if cap_reasons else "no critical mismatch detected"
    return {
        "risk_flags": risk_flags,
        "mismatches": mismatches,
        "missing_claim_facts": missing_claim_facts,
        "matched_claim_facts": matched_claim_facts,
        "has_critical_mismatch": any(
            flag in risk_flags
            for flag in (
                "number_mismatch",
                "date_mismatch",
                "eligibility_mismatch",
                "finality_mismatch",
                "negation_mismatch",
            )
        ),
        "has_missing_critical_fact": "missing_critical_fact" in risk_flags,
        "support_cap": final_cap,
        "support_cap_reason": reason_str,
    }


def cap_support_level(raw_level: str, cap: str) -> str:
    """Return the lower of ``raw_level`` and ``cap`` using the support ranking."""
    return _cap_level(raw_level, cap)
