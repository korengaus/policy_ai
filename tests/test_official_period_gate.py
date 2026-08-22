"""PERIOD-GATE (100-APPLY) — the official matcher tests period compatibility.

A periodic government release (고용동향, 가계대출 동향, 물가지수 …) carries
enough boilerplate to term-match almost any claim in its subject area. Before
this gate the matcher had no period test, so every new monthly edition could
become "primary evidence" for a claim about a different month or year; three
earlier fixes wrapped display and detection around the matcher and the defect
kept recurring (audit C7 grew 3 -> 5 rows).

The gate sits in official_evidence_resolution._resolve_source between the
term-overlap classification and the official_body_match promotion. It blocks
PROMOTION, not existence: the candidate stays in source_candidates, carries
the recorded test, and is never strong/medium, never official_body_match,
never verification_role=primary_evidence.

Specimens mirror real stored rows (ids 16105, 13977, 7871, 15617); no Korean
wording is invented beyond what those rows and documents already say.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_evidence_resolution import (  # noqa: E402
    PERIOD_GATE_STATUS,
    extract_primary_document_match,
    parse_korean_period_tokens,
    periodic_edition_period_gate,
    resolve_official_evidence,
)
from source_reliability_agent import evaluate_source_candidates  # noqa: E402


# A body long enough (>=300 chars) that repeats the claim's own terms and
# numbers — exactly the boilerplate overlap that made wrong-period editions
# score "strong" before the gate.
_EMPLOYMENT_SENTENCE = (
    "15~64세 고용률(OECD 비교기준)은 70.2%로 전년동월대비 0.3%p 하락 "
    "실업률은 2.9%로 전년동월대비 0.1%p 상승 실업자는 878천명으로 전년동월대비 "
    "2만 5천명(3.0%) 증가 청년층 실업률은 7.2%로 전년동월대비 상승."
)
_EMPLOYMENT_BODY = " ".join([_EMPLOYMENT_SENTENCE] * 4)


def _claim(text: str, date_or_time: str) -> dict:
    return {"claim_text": text, "date_or_time": date_or_time}


def _candidate(title: str, body: str = _EMPLOYMENT_BODY, claim_index: int = 0) -> dict:
    return {
        "title": title,
        "url": "https://www.moel.go.kr/news/enews/report/enewsView.do?news_seq=1",
        "official_detail_url": "https://www.moel.go.kr/news/enews/report/enewsView.do?news_seq=1",
        "source_type": "official_government",
        "publisher": "고용노동부",
        "purpose": "support",
        "raw_text_available": True,
        "official_body_fetched": True,
        "official_body_text": body,
        "claim_index": claim_index,
        # stable primary-document marker so extract_primary_document_match
        # is exercised too (M22-1b)
        "policy_briefing_news_item_id": "pb-1",
    }


def _resolve_one(candidate: dict, claims: list[dict]) -> dict:
    resolved, _summary = resolve_official_evidence([candidate], claims)
    return resolved[0]


class ParseTokensTests(unittest.TestCase):
    def test_two_digit_edition_and_full_forms(self):
        self.assertEqual(parse_korean_period_tokens("26년 7월 고용동향"), [(2026, 7)])
        self.assertEqual(parse_korean_period_tokens("2026년 6월 가계대출 동향(잠정)"), [(2026, 6)])
        self.assertEqual(parse_korean_period_tokens("2021년, 11월"), [(2021, 11)])
        self.assertEqual(parse_korean_period_tokens("2029년"), [(2029, None)])

    def test_relative_dates_parse_nothing(self):
        # The 7871 shape — "내년 2월 26일" has no year, so no period.
        self.assertEqual(parse_korean_period_tokens("내년, 2월, 26일, 6월"), [])


class GatePredicateTests(unittest.TestCase):
    def test_mismatch_on_periodic_family_with_different_month(self):
        gate = periodic_edition_period_gate(
            "26년 7월 고용동향", [_claim("[2026년 5월 고용동향] …", "2026년, 5월")])
        self.assertTrue(gate["mismatch"])
        self.assertEqual(gate["document_periods"], [(2026, 7)])
        self.assertEqual(gate["claim_periods"], [(2026, 5)])

    def test_agreeing_month_never_trips(self):
        gate = periodic_edition_period_gate(
            "26년 7월 고용동향", [_claim("[2026년 7월 고용동향] …", "2026년, 7월")])
        self.assertFalse(gate["mismatch"])

    def test_year_only_claim_agrees_with_any_month_of_that_year(self):
        gate = periodic_edition_period_gate(
            "2026년 6월 가계대출 동향(잠정)", [_claim("…", "2026년")])
        self.assertFalse(gate["mismatch"])

    def test_non_periodic_document_never_trips(self):
        # 15617 shape: a one-off press release + a claim dated 2029년.
        gate = periodic_edition_period_gate(
            "수도권 23만호+α 추가 공급, 주택 공급 촉진 및 청년 등 실수요자 금융지원 강화",
            [_claim("과천·태릉 2029년 착공", "2029년")])
        self.assertFalse(gate["mismatch"])

    def test_claim_without_year_never_trips(self):
        gate = periodic_edition_period_gate(
            "2026년 6월 가계대출 동향(잠정)",
            [_claim("내년 2월 26일부터 시행", "내년, 2월, 26일")])
        self.assertFalse(gate["mismatch"])

    def test_claim_text_fallback_when_no_date_field(self):
        gate = periodic_edition_period_gate(
            "2026년 6월 고용동향", [_claim("2017년 3월 고용률은 60.2%", "")])
        self.assertTrue(gate["mismatch"])

    def test_never_raises_on_garbage(self):
        self.assertFalse(periodic_edition_period_gate(None, None)["mismatch"])
        self.assertFalse(periodic_edition_period_gate(123, [None, 5])["mismatch"])


class MatcherGateTests(unittest.TestCase):
    """The gate inside _resolve_source, end to end through evaluate_source_candidates."""

    def test_wrong_period_edition_is_not_promoted(self):
        # id 16105 shape: May claim, July edition, boilerplate overlap scores strong.
        claims = [_claim("[2026년 5월 고용동향] " + _EMPLOYMENT_SENTENCE, "2026년, 5월")]
        item = _resolve_one(_candidate("26년 7월 고용동향"), claims)
        self.assertEqual(item["official_evidence_classification"], "weak_official_candidate_only")
        self.assertEqual(item["official_direct_match_classification"], "weak_official_candidate_only")
        self.assertFalse(item.get("official_body_match"))
        self.assertEqual(item["official_period_gate"]["status"], PERIOD_GATE_STATUS)
        self.assertEqual(item["official_period_gate"]["ungated_classification"],
                         "strong_official_direct_support")
        self.assertEqual(item["official_period_gate"]["document_periods"], [[2026, 7]])
        self.assertEqual(item["official_period_gate"]["claim_periods"], [[2026, 5]])
        # Scoring itself is untouched — the score that WOULD have promoted is still recorded.
        self.assertGreaterEqual(item["official_evidence_score"], 75)
        # Existence preserved, promotion blocked: still a candidate, not primary evidence.
        evaluated = evaluate_source_candidates([item])
        self.assertEqual(len(evaluated), 1)
        self.assertNotEqual(evaluated[0]["verification_role"], "primary_evidence")
        self.assertIn("official_body_mismatch", evaluated[0]["source_risk_flags"])
        self.assertIsNone(extract_primary_document_match(evaluated))

    def test_same_period_edition_still_promotes(self):
        # FALSE-POSITIVE guard: a claim about the month the document covers must match.
        claims = [_claim("[2026년 7월 고용동향] " + _EMPLOYMENT_SENTENCE, "2026년, 7월")]
        item = _resolve_one(_candidate("26년 7월 고용동향"), claims)
        self.assertEqual(item["official_evidence_classification"], "strong_official_direct_support")
        self.assertTrue(item["official_body_match"])
        self.assertNotIn("official_period_gate", item)
        evaluated = evaluate_source_candidates([item])
        self.assertEqual(evaluated[0]["verification_role"], "primary_evidence")
        self.assertIsNotNone(extract_primary_document_match(evaluated))

    def test_claim_without_year_is_left_to_the_matcher(self):
        # id 7871 shape: "내년 2월 26일" parses no period — the gate stays out.
        claims = [_claim("내년 2월 26일부터 " + _EMPLOYMENT_SENTENCE, "내년, 2월, 26일")]
        item = _resolve_one(_candidate("2026년 6월 고용동향"), claims)
        self.assertEqual(item["official_evidence_classification"], "strong_official_direct_support")
        self.assertNotIn("official_period_gate", item)

    def test_non_periodic_document_with_different_year_still_promotes(self):
        # id 15617 shape: a one-off press release supporting a claim that names 2029년.
        claims = [_claim("과천·태릉 2029년 착공 " + _EMPLOYMENT_SENTENCE, "2029년")]
        item = _resolve_one(
            _candidate("수도권 23만호+α 추가 공급 주택 공급 촉진 2026년"), claims)
        self.assertEqual(item["official_evidence_classification"], "strong_official_direct_support")
        self.assertNotIn("official_period_gate", item)

    def test_gate_applies_per_claim_index(self):
        # The candidate is matched against ITS claim (claim_index), not every claim.
        claims = [
            _claim("[2026년 7월 고용동향] " + _EMPLOYMENT_SENTENCE, "2026년, 7월"),
            _claim("[2026년 5월 고용동향] " + _EMPLOYMENT_SENTENCE, "2026년, 5월"),
        ]
        ok = _resolve_one(_candidate("26년 7월 고용동향", claim_index=0), claims)
        gated = _resolve_one(_candidate("26년 7월 고용동향", claim_index=1), claims)
        self.assertEqual(ok["official_evidence_classification"], "strong_official_direct_support")
        self.assertEqual(gated["official_evidence_classification"], "weak_official_candidate_only")

    def test_gate_does_not_touch_a_candidate_that_never_qualified(self):
        # No body → no_usable_official_detail regardless of periods; no gate record.
        claims = [_claim("[2026년 5월 고용동향] " + _EMPLOYMENT_SENTENCE, "2026년, 5월")]
        item = _resolve_one(_candidate("26년 7월 고용동향", body=""), claims)
        self.assertEqual(item["official_evidence_classification"], "no_usable_official_detail")
        self.assertNotIn("official_period_gate", item)


class ApiServerReusesTheMatcherPredicateTests(unittest.TestCase):
    """The display predicate in api_server.py must be the SAME objects — no third copy."""

    def test_same_objects(self):
        import official_evidence_resolution as oer
        import api_server  # noqa: F401  (offline import; builds the app, no server)

        self.assertIs(api_server._parse_korean_period_tokens, oer.parse_korean_period_tokens)
        self.assertIs(api_server._official_periods_agree, oer.official_periods_agree)
        self.assertIs(api_server._OFFICIAL_PERIODIC_FAMILY_RE, oer.OFFICIAL_PERIODIC_FAMILY_RE)


if __name__ == "__main__":
    unittest.main()
