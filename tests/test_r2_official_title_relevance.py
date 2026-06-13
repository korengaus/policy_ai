"""REL-1 R2-redefined — official-snippet selection title-relevance re-order.

Covers the evidence_extraction_agent.py change: among official body candidates of EQUAL
match-status, the one with higher claim<->title topic relevance is preferred for the
evidence_snippets[:2] surface; a matched (>=55) doc keeps precedence and is never demoted
below a sub-55 more-title-relevant doc; empty tokenization falls through with no reorder.

The re-order is removal-free and verdict-math-free (validated by scripts/r2_typeflip.py,
N_label_flip=0). These tests assert the SELECTION effect via the [:2] cap with 3 candidates.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evidence_extraction_agent as ee


# claim whose SPECIFIC topic tokens (after BROAD-exclude of 규제/강화) are {전세대출, 한도, 시행}
_CLAIM = {"claim_text": "전세대출 한도 규제 강화 시행"}

# identical on-topic body on EVERY candidate so each produces snippets and the only variable
# driving selection is the TITLE relevance (and score / match-status), isolating the change.
_BODY = (
    "정부는 전세대출 한도 규제를 강화하고 시행한다고 밝혔다. "
    "전세대출 한도 규제 강화 시행 세부 방안이 구체적으로 마련되었다고 발표했다."
)

# off-topic titles -> title relevance 0 (no overlap with {전세대출, 한도, 시행})
_TITLE_OFF_A = "소상공인 금융 성과 점검 회의"
_TITLE_OFF_B = "부동산 시장 동향 분석 보고"
# on-topic titles -> title relevance 3
_TITLE_ON_1 = "전세대출 한도 시행 세부 안내"
_TITLE_ON_2 = "전세대출 한도 시행 추가 공지"


def _official(source_id, title, score, *, match=False):
    src = {
        "source_id": source_id,
        "claim_index": 0,
        "title": title,
        "url": f"https://www.korea.kr/{source_id}",
        "publisher": "금융위원회",
        "source_type": "official_government",
        "raw_text": _BODY,
        "raw_text_available": True,
        "official_body_length": len(_BODY),
        "official_evidence_score": score,
    }
    if match:
        src["official_body_match"] = True
    return src


def _official_snippet_source_ids(source_candidates):
    result = ee.extract_evidence_snippets(
        normalized_claims=[_CLAIM],
        source_candidates=source_candidates,
        article_body="",
    )
    return {
        snip["source_id"]
        for snip in result["evidence_snippets"]
        if snip.get("extraction_method") == "official_body_sentence_overlap"
    }


class TitleRelevanceSelectionTests(unittest.TestCase):
    def test_helper_excludes_broad_and_strips_josa(self):
        # relevance notion matches r2_pbcheck PART C: BROAD words excluded, trailing josa stripped
        self.assertEqual(ee._r2_josa_strip("가능성을"), "가능성")
        self.assertEqual(ee._r2_josa_strip("정부는"), "정부")
        self.assertNotIn("금융", ee._r2_title_topic_tokens("금융 전세대출"))   # 금융 is BROAD
        self.assertIn("전세대출", ee._r2_title_topic_tokens("금융 전세대출"))

    def test_more_title_relevant_doc_promoted_among_equal_status(self):
        # 3 unmatched official docs, [:2] cap. By score alone the on-topic low-score doc (C) is
        # excluded; the title-relevance term promotes it INTO the surfaced set and drops B.
        a = _official("A", _TITLE_OFF_A, 50)
        b = _official("B", _TITLE_OFF_B, 48)
        c = _official("C", _TITLE_ON_1, 30)
        ids = _official_snippet_source_ids([a, b, c])
        self.assertIn("C", ids)       # promoted by title relevance despite lowest score
        self.assertNotIn("B", ids)    # dropped from the [:2] surface

    def test_matched_doc_keeps_precedence_over_sub55_relevant_docs(self):
        # matched (>=55) but OFF-topic-title doc M vs two unmatched ON-topic docs. match-status is
        # the FIRST sort key, so M must stay in the [:2] surface (never demoted below sub-55 docs).
        m = _official("M", _TITLE_OFF_A, 56, match=True)
        r1 = _official("R1", _TITLE_ON_1, 50)
        r2 = _official("R2", _TITLE_ON_2, 48)
        ids = _official_snippet_source_ids([m, r1, r2])
        self.assertIn("M", ids)       # matched doc surfaces despite off-topic title
        self.assertNotIn("R2", ids)   # the lower-score on-topic doc is the one dropped, not M

    def test_empty_tokenization_falls_through_with_no_error(self):
        # empty claim topic / empty title -> relevance 0 -> sort falls through to score; no throw.
        self.assertEqual(ee._r2_claim_topic_tokens([]), set())
        self.assertEqual(ee._official_title_relevance(set(), _official("X", _TITLE_ON_1, 30)), 0)
        self.assertEqual(
            ee._official_title_relevance({"전세대출"}, _official("Y", "", 30)), 0
        )
        # end-to-end with an empty-text claim must not raise and returns a snippet list
        result = ee.extract_evidence_snippets(
            normalized_claims=[{"claim_text": ""}],
            source_candidates=[_official("Z", _TITLE_ON_1, 30)],
            article_body="",
        )
        self.assertIsInstance(result["evidence_snippets"], list)


if __name__ == "__main__":
    unittest.main()
