"""M22-1 / M22-1b — Lane A↔B join (the real verdict fix), production-shape tests.

Run with: python tests/test_m22_1_lane_join.py

A genuine STRONG Policy-Briefing (Lane B) official_body_match must raise the
verdict deterministically and conservatively when Lane A (crawl results) has no
usable document:
    * verdict_confidence -> fixed 70 ceiling, verification_strength -> "low"
    * verification_level -> "medium_official_match" (NEVER strong)
    * verdict_label      -> "draft_likely_true" (NEVER draft_verified)
    * the user-facing summary names the Policy-Briefing BODY match, carries the
      live score, and never over-claims.

M22-1b: the extractor now gates on the STABLE marker policy_briefing_news_item_id
(NOT retrieval_method). resolve_official_evidence OVERWRITES retrieval_method to
"official_evidence_resolved" on a strong/medium match — the original M22-1 gate
keyed on "policy_briefing_api" and therefore missed every resolve-processed PB
candidate in production. These tests drive the REAL production shape (and a real
resolve -> evaluate -> extract pass) so they would have caught that bug.

The LLM judge is NOT involved here. official_body_match is read-only (M19-3).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import evidence_comparator as ec  # noqa: E402
import policy_confidence as pc  # noqa: E402
import verification_card as vc  # noqa: E402
from official_evidence_resolution import (  # noqa: E402
    extract_primary_document_match,
    resolve_official_evidence,
    _is_strong_primary_document_match,
)
from source_reliability_agent import evaluate_source_candidates  # noqa: E402


# A Policy-Briefing candidate AS IT LOOKS AFTER resolve_official_evidence in
# production: retrieval_method has been OVERWRITTEN to "official_evidence_resolved"
# (this is exactly what broke M22-1), official_body_match/classification/score set
# by resolve, and the STABLE marker policy_briefing_news_item_id still present.
def _pb_resolved_candidate(score: int = 82, *, marker: bool = True) -> dict:
    cand = {
        "source_id": "pb-1",
        "claim_index": 0,
        "title": "전세대출 규제 강화 보도자료",
        "url": "https://www.korea.kr/news/policyNewsView.do?newsId=148900001",
        "official_detail_url": "https://www.korea.kr/news/policyNewsView.do?newsId=148900001",
        "publisher": "금융위원회",
        "source_type": "official_government",
        "raw_text": "금융위원회는 전세대출 규제를 강화한다고 발표했다. " * 30,
        "raw_text_available": True,
        "official_body_fetched": True,
        "official_body_match": True,
        "official_evidence_classification": "strong_official_direct_support",
        "official_evidence_score": score,
        # The bug: resolve renames this away from "policy_briefing_api".
        "retrieval_method": "official_evidence_resolved",
        "purpose": "primary_source",
    }
    if marker:
        cand["policy_briefing_news_item_id"] = "148900001"
    return cand


# A RAW Policy-Briefing candidate exactly as to_official_source_candidates injects
# it (pre-resolve): the marker is present, official_body_match is NOT set (M19-3 —
# resolve computes it), retrieval_method is still "policy_briefing_api".
def _pb_raw_candidate() -> dict:
    body = (
        "금융위원회는 전세대출 규제를 강화한다고 발표했다. 전세대출 한도와 DSR 규제를 "
        "함께 조정한다. 이번 대책은 가계부채 관리와 주택시장 안정을 목표로 한다. "
        "전세대출 규제는 수도권 규제지역에 우선 적용된다. 금융위원회는 실수요자 보호를 "
        "위한 예외 규정도 마련한다. 대출 심사 기준과 DSR 산정 방식도 정비된다. "
        "전세대출 규제 시행 시기는 2025년 7월로 발표됐다. 금융당국은 전세대출 규제 "
        "효과를 점검한다."
    ) * 2
    return {
        "source_id": "pb-raw-1",
        "claim_index": 0,
        "title": "전세대출 규제 강화 보도자료",
        "url": "https://www.korea.kr/news/policyNewsView.do?newsId=148900001",
        "official_detail_url": "https://www.korea.kr/news/policyNewsView.do?newsId=148900001",
        "publisher": "금융위원회",
        "source_type": "official_government",
        "raw_text": body,
        "raw_text_available": True,
        "official_body_fetched": True,
        "retrieval_method": "policy_briefing_api",  # resolve will overwrite this
        "purpose": "primary_source",
        "policy_briefing_news_item_id": "148900001",
    }


_CLAIM = {
    "claim_text": "금융위원회가 전세대출 규제를 강화하고 DSR 한도를 2025년 7월에 조정한다",
    "actor": "금융위원회",
    "action": "전세대출 규제 강화",
    "target": "전세대출",
    "object": "DSR 한도",
    "date_or_time": "2025년 7월",
}

# Lane A doc that is fetched but excluded (the live 82-score scenario):
# should_exclude_from_verification -> not comparable -> document_found_count==0,
# excluded_non_policy_count>0 -> verification_level "excluded_non_policy_page",
# and _best_official_evidence filters it -> official_usable=False.
_EXCLUDED_LANE_A_DOC = {
    "fetched": True,
    "should_exclude_from_verification": True,
    "document_relevance_score": 0,
    "selected_document_url": "https://www.korea.kr/list/index.do",
}


def _compare(primary_document_match, official_evidence_results=None):
    return ec.compare_news_with_official_evidence(
        news_title="금융위, 전세대출 규제 강화",
        news_summary="금융위원회가 전세대출 규제를 강화한다",
        article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
        policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
        official_evidence_results=official_evidence_results or [],
        primary_document_match=primary_document_match,
    )


def _confidence(primary_document_match, evidence_comparison, official_evidence_results=None):
    return pc.calculate_policy_confidence(
        news_title="금융위, 전세대출 규제 강화",
        news_summary="금융위원회가 전세대출 규제를 강화한다",
        article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
        policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
        official_evidence_results=official_evidence_results or [],
        evidence_comparison=evidence_comparison,
        primary_document_match=primary_document_match,
    )


def _verification_card(policy_confidence, evidence_comparison, source_candidates,
                       official_evidence_results=None):
    return vc.build_verification_card(
        news={"title": "금융위, 전세대출 규제 강화", "summary": "전세대출 규제 강화"},
        original_url="https://news.example.com/a/1",
        policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
        official_evidence_results=official_evidence_results or [],
        evidence_comparison=evidence_comparison,
        policy_confidence=policy_confidence,
        article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
        claims=["금융위원회가 전세대출 규제를 강화한다"],
        normalized_claims=[_CLAIM],
        source_queries=[],
        source_candidates=source_candidates,
        evidence_snippets=[],
        claim_evidence_map={},
        contradiction_checks=[],
        contradiction_summary={},
        bias_framing_analysis=[],
        bias_framing_summary={},
    )


# ---------------------------------------------------------------------------
# Extractor gate — keys on the STABLE marker, survives the retrieval_method
# overwrite (the M22-1b regression).
# ---------------------------------------------------------------------------


class ExtractorGateTests(unittest.TestCase):
    def test_resolve_overwritten_retrieval_method_still_extracted(self):
        # THE M22-1b REGRESSION: retrieval_method is "official_evidence_resolved"
        # (not "policy_briefing_api"), yet the marker lets the extractor find it.
        cand = _pb_resolved_candidate(82)
        self.assertEqual(cand["retrieval_method"], "official_evidence_resolved")
        match = extract_primary_document_match([cand])
        self.assertIsNotNone(match)
        self.assertEqual(match["score"], 82)
        self.assertEqual(match["classification"], "strong_official_direct_support")
        self.assertTrue(_is_strong_primary_document_match(match))

    def test_missing_marker_not_extracted(self):
        # A strong official match WITHOUT the Policy-Briefing marker (e.g. a
        # crawl-lane candidate) must never trigger the raise.
        self.assertIsNone(extract_primary_document_match([_pb_resolved_candidate(86, marker=False)]))

    def test_medium_classification_not_extracted(self):
        cand = _pb_resolved_candidate(60)
        cand["official_evidence_classification"] = "medium_official_contextual_support"
        self.assertIsNone(extract_primary_document_match([cand]))

    def test_score_below_75_not_extracted(self):
        self.assertIsNone(extract_primary_document_match([_pb_resolved_candidate(74)]))

    def test_no_body_match_not_extracted(self):
        cand = _pb_resolved_candidate(86)
        cand["official_body_match"] = False
        self.assertIsNone(extract_primary_document_match([cand]))


# ---------------------------------------------------------------------------
# Real pipeline pass — resolve -> evaluate -> extract (proves the marker
# survives the overwrite and the raise fires through the actual chain).
# ---------------------------------------------------------------------------


class RealPipelineTests(unittest.TestCase):
    def setUp(self):
        resolved, _ = resolve_official_evidence([_pb_raw_candidate()], [_CLAIM])
        self.evaluated = evaluate_source_candidates(resolved)
        self.cand = self.evaluated[0]

    def test_retrieval_method_overwritten_marker_survives(self):
        # The bug premise: resolve renames retrieval_method...
        self.assertEqual(self.cand["retrieval_method"], "official_evidence_resolved")
        # ...but the stable marker survives resolve AND evaluate.
        self.assertIn("policy_briefing_news_item_id", self.cand)
        self.assertTrue(self.cand["official_body_match"])
        self.assertEqual(
            self.cand["official_evidence_classification"], "strong_official_direct_support"
        )

    def test_extractor_finds_match_after_real_pipeline(self):
        match = extract_primary_document_match(self.evaluated)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(match["score"], 75)
        self.assertEqual(match["classification"], "strong_official_direct_support")

    def test_raise_fires_end_to_end(self):
        match = extract_primary_document_match(self.evaluated)
        cmp = _compare(match)
        conf = _confidence(match, cmp)
        card = _verification_card(conf, cmp, self.evaluated)
        self.assertEqual(cmp["verification_level"], "medium_official_match")
        self.assertEqual(conf["policy_confidence_score"], 70)
        self.assertEqual(conf["verification_strength"], "low")
        self.assertEqual(card["verdict_label"], "draft_likely_true")
        self.assertNotEqual(card["verdict_label"], "draft_verified")
        self.assertEqual(card["verdict_confidence"], 70)


# ---------------------------------------------------------------------------
# Live 82-score scenario — Lane A PRESENT but all excluded (official_usable=False).
# ---------------------------------------------------------------------------


class LaneAExcludedTests(unittest.TestCase):
    def setUp(self):
        self.match = extract_primary_document_match([_pb_resolved_candidate(82)])
        self.cmp = _compare(self.match, official_evidence_results=[_EXCLUDED_LANE_A_DOC])
        self.conf = _confidence(
            self.match, self.cmp, official_evidence_results=[_EXCLUDED_LANE_A_DOC]
        )
        self.card = _verification_card(
            self.conf, self.cmp, [_pb_resolved_candidate(82)],
            official_evidence_results=[_EXCLUDED_LANE_A_DOC],
        )

    def test_lane_a_excluded_does_not_block_raise(self):
        # Reproduces the live bug scenario: Lane A had excluded docs (so the old
        # report showed verification_level=excluded_non_policy_page, confidence=10).
        # With the marker fix the strong PB match now raises.
        self.assertEqual(self.cmp["verification_level"], "medium_official_match")
        self.assertEqual(self.conf["policy_confidence_score"], 70)
        self.assertEqual(self.conf["verification_strength"], "low")
        self.assertEqual(self.card["verdict_label"], "draft_likely_true")
        self.assertNotEqual(self.card["verdict_label"], "draft_verified")
        self.assertFalse(self.card["official_mismatch"])

    def test_summary_coherent_lane_b_wording(self):
        summary = self.cmp["comparison_summary"]
        self.assertIn("정책브리핑", summary)
        self.assertIn("82", summary)
        self.assertNotIn("  ", summary)
        self.assertNotIn("의미 개념이", summary)
        for token in ("검증", "확정", "100%"):
            self.assertNotIn(token, summary)


# ---------------------------------------------------------------------------
# Controls — gate not met → byte-identical / no raise.
# ---------------------------------------------------------------------------


class NoneControlTests(unittest.TestCase):
    def test_byte_identical_without_lane_b_match(self):
        cmp_with_none = _compare(None)
        cmp_without_param = ec.compare_news_with_official_evidence(
            news_title="금융위, 전세대출 규제 강화",
            news_summary="금융위원회가 전세대출 규제를 강화한다",
            article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
            policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
            official_evidence_results=[],
        )
        self.assertEqual(cmp_with_none, cmp_without_param)
        self.assertNotEqual(cmp_with_none["verification_level"], "medium_official_match")

        conf_with_none = _confidence(None, cmp_with_none)
        conf_without_param = pc.calculate_policy_confidence(
            news_title="금융위, 전세대출 규제 강화",
            news_summary="금융위원회가 전세대출 규제를 강화한다",
            article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
            policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
            official_evidence_results=[],
            evidence_comparison=cmp_with_none,
        )
        self.assertEqual(conf_with_none, conf_without_param)
        self.assertLessEqual(conf_with_none["policy_confidence_score"], 20)
        self.assertEqual(conf_with_none["verification_strength"], "none")


class MediumOnlyControlTests(unittest.TestCase):
    def test_medium_classification_does_not_raise(self):
        cand = _pb_resolved_candidate(60)
        cand["official_evidence_classification"] = "medium_official_contextual_support"
        match = extract_primary_document_match([cand])  # -> None
        cmp = _compare(match)
        conf = _confidence(match, cmp)
        self.assertNotEqual(cmp["verification_level"], "medium_official_match")
        self.assertLessEqual(conf["policy_confidence_score"], 20)
        self.assertEqual(conf["verification_strength"], "none")


class MissingMarkerControlTests(unittest.TestCase):
    def test_candidate_without_marker_does_not_raise(self):
        # Crawl-lane strong match (resolved, no PB marker) → no raise.
        match = extract_primary_document_match([_pb_resolved_candidate(86, marker=False)])
        self.assertIsNone(match)
        cmp = _compare(match)
        conf = _confidence(match, cmp)
        self.assertNotEqual(cmp["verification_level"], "medium_official_match")
        self.assertLessEqual(conf["policy_confidence_score"], 20)


# ---------------------------------------------------------------------------
# Conflict precedence preserved.
# ---------------------------------------------------------------------------


class ConflictPrecedenceTests(unittest.TestCase):
    def test_conflict_blocks_lane_b_upgrade(self):
        match = extract_primary_document_match([_pb_resolved_candidate(86)])
        cmp = ec.compare_news_with_official_evidence(
            news_title="금융위 전세대출 규제",
            news_summary="전세대출 규제 강화",
            article_body="전세대출 규제 강화",
            policy_claims=[{"sentence": "전세대출 규제 강화"}],
            official_evidence_results=[
                {
                    "usable": True,
                    "evidence_grade": "C",
                    "document_relevance_score": 50,
                    "document_fetched": True,
                    "fetched": True,
                    "document_text_snippet": "해당 내용은 사실이 아니며 확정되지 않았다.",
                    "is_detail_page": True,
                    "selected_document_url": "https://x.go.kr/d/1",
                }
            ],
            primary_document_match=match,
        )
        if cmp["conflict_signals"]:
            self.assertNotEqual(cmp["verification_level"], "medium_official_match")


if __name__ == "__main__":
    unittest.main(verbosity=2)
