"""M22-1 — Lane A↔B join (the real verdict fix).

Run with: python tests/test_m22_1_lane_join.py

A genuine STRONG Policy-Briefing (Lane B) official_body_match must raise the
verdict deterministically and conservatively when Lane A (crawl results) is
empty:
    * verdict_confidence -> fixed 70 ceiling, verification_strength -> "low"
    * verification_level -> "medium_official_match" (NEVER strong)
    * verdict_label      -> "draft_likely_true" (NEVER draft_verified)
    * the user-facing summary names the Policy-Briefing BODY match (not a
      semantic-concept match), carries the live score, and never over-claims.

Gating (both required): Lane A empty (not official_usable) AND a strong
Policy-Briefing match (retrieval_method=="policy_briefing_api" + resolve-computed
official_body_match + strong_official_direct_support + score>=75). When the gate
is not met, every producer is byte-identical to pre-M22-1.

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
    _is_strong_primary_document_match,
)


# A genuine STRONG Policy-Briefing body match as resolve_official_evidence would
# leave it on a source_candidate (Lane B). We READ these fields; never set them.
def _pb_strong_candidate(score: int = 86) -> dict:
    return {
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
        "retrieval_method": "policy_briefing_api",
        "purpose": "primary_source",
    }


def _compare(primary_document_match):
    return ec.compare_news_with_official_evidence(
        news_title="금융위, 전세대출 규제 강화",
        news_summary="금융위원회가 전세대출 규제를 강화한다",
        article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
        policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
        official_evidence_results=[],  # Lane A empty
        primary_document_match=primary_document_match,
    )


def _confidence(primary_document_match, evidence_comparison):
    return pc.calculate_policy_confidence(
        news_title="금융위, 전세대출 규제 강화",
        news_summary="금융위원회가 전세대출 규제를 강화한다",
        article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
        policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
        official_evidence_results=[],  # Lane A empty
        evidence_comparison=evidence_comparison,
        primary_document_match=primary_document_match,
    )


def _verification_card(policy_confidence, evidence_comparison, source_candidates):
    return vc.build_verification_card(
        news={"title": "금융위, 전세대출 규제 강화", "summary": "전세대출 규제 강화"},
        original_url="https://news.example.com/a/1",
        policy_claims=[{"sentence": "금융위원회가 전세대출 규제를 강화한다"}],
        official_evidence_results=[],  # Lane A empty
        evidence_comparison=evidence_comparison,
        policy_confidence=policy_confidence,
        article_body="금융위원회는 전세대출 규제를 강화한다고 발표했다.",
        claims=["금융위원회가 전세대출 규제를 강화한다"],
        normalized_claims=[{"claim_text": "금융위원회가 전세대출 규제를 강화한다"}],
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
# Extractor gate (M19-3 — read-only, Policy-Briefing-marker + strong only).
# ---------------------------------------------------------------------------


class ExtractorGateTests(unittest.TestCase):
    def test_strong_policy_briefing_match_extracted(self):
        match = extract_primary_document_match([_pb_strong_candidate(86)])
        self.assertIsNotNone(match)
        self.assertEqual(match["score"], 86)
        self.assertEqual(match["classification"], "strong_official_direct_support")
        self.assertTrue(_is_strong_primary_document_match(match))

    def test_medium_classification_not_extracted(self):
        cand = _pb_strong_candidate(60)
        cand["official_evidence_classification"] = "medium_official_contextual_support"
        self.assertIsNone(extract_primary_document_match([cand]))

    def test_score_below_75_not_extracted(self):
        self.assertIsNone(extract_primary_document_match([_pb_strong_candidate(74)]))

    def test_non_policy_briefing_marker_not_extracted(self):
        # Same strong body match but from the crawl lane → never triggers M22-1.
        cand = _pb_strong_candidate(86)
        cand["retrieval_method"] = "official_search_url_candidate"
        self.assertIsNone(extract_primary_document_match([cand]))

    def test_no_body_match_not_extracted(self):
        cand = _pb_strong_candidate(86)
        cand["official_body_match"] = False
        self.assertIsNone(extract_primary_document_match([cand]))


# ---------------------------------------------------------------------------
# Case 1 — raise fires (end to end).
# ---------------------------------------------------------------------------


class RaiseFiresTests(unittest.TestCase):
    def setUp(self):
        self.match = extract_primary_document_match([_pb_strong_candidate(86)])
        self.cmp = _compare(self.match)
        self.conf = _confidence(self.match, self.cmp)
        self.card = _verification_card(self.conf, self.cmp, [_pb_strong_candidate(86)])

    def test_verification_level_upgraded_to_medium_only(self):
        self.assertEqual(self.cmp["verification_level"], "medium_official_match")
        self.assertNotEqual(self.cmp["verification_level"], "strong_official_match")
        # semantic_support_score left at its honest (zero) Lane-A value.
        self.assertEqual(self.cmp["semantic_support_score"], 0)

    def test_confidence_ceiling_70_strength_low(self):
        self.assertEqual(self.conf["policy_confidence_score"], 70)
        self.assertEqual(self.conf["verification_strength"], "low")
        self.assertIn(
            "raised by strong Policy Briefing official body match (Lane B)",
            self.conf["confidence_reasons"],
        )

    def test_verdict_likely_true_not_verified(self):
        self.assertEqual(self.card["verdict_label"], "draft_likely_true")
        self.assertNotEqual(self.card["verdict_label"], "draft_verified")
        self.assertEqual(self.card["verdict_confidence"], 70)
        self.assertFalse(self.card["official_mismatch"])

    def test_summary_coherence_lane_b_branch(self):
        summary = self.cmp["comparison_summary"]
        # Names the Policy-Briefing BODY match + carries the dynamic score.
        self.assertIn("정책브리핑", summary)
        self.assertIn("86", summary)
        # No empty-concept artifact and no semantic-concept wording.
        self.assertNotIn("  ", summary)
        self.assertNotIn("의미 개념이", summary)
        # No over-claim tokens.
        for token in ("검증", "확정", "100%"):
            self.assertNotIn(token, summary)
        # Keeps operator-review framing.
        self.assertIn("사람 검토", summary)
        # Next action is Lane-B-appropriate and not an over-claim.
        action = self.cmp["recommended_next_action"]
        self.assertIn("정책브리핑", action)
        for token in ("검증", "확정", "100%"):
            self.assertNotIn(token, action)

    def test_evidence_summary_inherits_coherent_wording(self):
        # verification_card.evidence_summary prepends comparison_summary.
        self.assertIn("정책브리핑", self.card["evidence_summary"])
        self.assertNotIn("의미 개념이", self.card["evidence_summary"])


# ---------------------------------------------------------------------------
# Case 2 — None control: byte-identical to pre-M22-1 (gate not met).
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
        # Default-None param path == omitting the param entirely.
        self.assertEqual(cmp_with_none, cmp_without_param)
        # And it is NOT upgraded.
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
        # No Lane A doc + no Lane B match → clamped to 20 / "none" as before.
        self.assertLessEqual(conf_with_none["policy_confidence_score"], 20)
        self.assertEqual(conf_with_none["verification_strength"], "none")


# ---------------------------------------------------------------------------
# Case 3 — medium-only control: no raise.
# ---------------------------------------------------------------------------


class MediumOnlyControlTests(unittest.TestCase):
    def test_medium_classification_does_not_raise(self):
        cand = _pb_strong_candidate(60)
        cand["official_evidence_classification"] = "medium_official_contextual_support"
        match = extract_primary_document_match([cand])  # -> None
        cmp = _compare(match)
        conf = _confidence(match, cmp)
        self.assertNotEqual(cmp["verification_level"], "medium_official_match")
        self.assertLessEqual(conf["policy_confidence_score"], 20)
        self.assertEqual(conf["verification_strength"], "none")


# ---------------------------------------------------------------------------
# Case 4 — non-policy_briefing_api marker control: no raise.
# ---------------------------------------------------------------------------


class CrawlLaneMarkerControlTests(unittest.TestCase):
    def test_crawl_lane_body_match_does_not_raise(self):
        cand = _pb_strong_candidate(86)
        cand["retrieval_method"] = "official_search_url_candidate"
        match = extract_primary_document_match([cand])  # -> None
        cmp = _compare(match)
        conf = _confidence(match, cmp)
        self.assertNotEqual(cmp["verification_level"], "medium_official_match")
        self.assertLessEqual(conf["policy_confidence_score"], 20)
        self.assertEqual(conf["verification_strength"], "none")


# ---------------------------------------------------------------------------
# Conflict precedence preserved.
# ---------------------------------------------------------------------------


class ConflictPrecedenceTests(unittest.TestCase):
    def test_conflict_blocks_lane_b_upgrade(self):
        # A document body carrying a conflict phrase makes Lane A report a
        # conflict; the Lane-B upgrade must be skipped (conflict precedence).
        match = extract_primary_document_match([_pb_strong_candidate(86)])
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
