"""M23 — National Law Information (법제처) provider tests (mock-driven, NO live calls).

Covers: search/body XML parse (CDATA, <law id=...>, 조문단위 shallow-gather incl.
항내용 child), fail-closed (Response error envelope / non-00 / HTML / malformed),
marker fields + full law.go.kr URL, the M22-1b resolve-overwrite regression
(national_law_mst survives the retrieval_method overwrite), Lane-B cap-70
(strong → confidence 70 / medium_official_match / draft_likely_true, never
draft_verified; medium/weak → no raise), disabled-path zero-network byte-identity,
call-budget caps, and PB+law coexistence (no marker collision).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import evidence_comparator as ec  # noqa: E402
import policy_confidence as pc  # noqa: E402
import verification_card as vc  # noqa: E402
from providers import national_law as nl  # noqa: E402
from official_evidence_resolution import (  # noqa: E402
    resolve_official_evidence,
    extract_primary_document_match,
    _is_strong_primary_document_match,
)
from source_reliability_agent import evaluate_source_candidates  # noqa: E402


class _EnvScope:
    KEYS = ("NATIONAL_LAW_ENABLED", "LAW_OC", "NATIONAL_LAW_TIMEOUT_SECONDS")

    def __enter__(self):
        self._snapshot = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _set_env(**values):
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# --- Canned XML (the proven strong-scoring content, wrapped as law XML) -------

SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<LawSearch><resultCode>00</resultCode><totalCnt>1</totalCnt>
<law id="1"><법령일련번호>276291</법령일련번호><법령ID>001248</법령ID>
<법령명한글><![CDATA[금융소비자 보호에 관한 법률]]></법령명한글><시행일자>20230101</시행일자>
<소관부처명><![CDATA[금융위원회]]></소관부처명><법령상세링크>/lsInfoP.do?lsiSeq=276291</법령상세링크></law>
</LawSearch>"""

_STRONG_BODY = (
    "금융위원회는 전세대출 규제를 강화한다고 발표했다. 전세대출 한도와 DSR 규제를 함께 "
    "조정한다. 이번 대책은 가계부채 관리와 주택시장 안정을 목표로 한다. 전세대출 규제는 "
    "수도권 규제지역에 우선 적용된다. 금융위원회는 실수요자 보호를 위한 예외 규정도 "
    "마련한다. 대출 심사 기준과 DSR 산정 방식도 정비된다. 전세대출 규제 시행 시기는 "
    "2025년 7월로 발표됐다. 금융당국은 전세대출 규제 효과를 점검한다."
) * 2
BODY_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<법령><기본정보><법령ID>001248</법령ID></기본정보>
<조문><조문단위><조문번호>10</조문번호><조문제목><![CDATA[전세대출 규제]]></조문제목>
<조문내용><![CDATA[{_STRONG_BODY}]]></조문내용>
<항><항내용><![CDATA[전세대출 한도와 DSR 규제 적용에 관한 세부 사항]]></항내용></항>
</조문단위></조문></법령>"""

_CLAIM = {
    "claim_text": "금융위원회가 전세대출 규제를 강화하고 DSR 한도를 2025년 7월에 조정한다",
    "actor": "금융위원회", "action": "전세대출 규제 강화",
    "target": "전세대출", "object": "DSR 한도", "date_or_time": "2025년 7월",
}

_RESPONSE_ERROR_XML = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
                       "<result>실패</result><msg>필수 입력값이 존재하지 않습니다.</msg></Response>")


def _mock_provider(search_xml=SEARCH_XML, body_map=None):
    return nl.MockNationalLawProvider(
        search_xml=search_xml, body_xml_by_mst=body_map or {"276291": BODY_XML})


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class SearchParseTests(unittest.TestCase):
    def test_parse_search_ok(self):
        code, ok, laws = nl.parse_law_search_xml(SEARCH_XML)
        self.assertEqual(code, "00")
        self.assertTrue(ok)
        self.assertEqual(len(laws), 1)
        law = laws[0]
        self.assertEqual(law["mst"], "276291")
        self.assertEqual(law["law_id"], "001248")
        self.assertEqual(law["name"], "금융소비자 보호에 관한 법률")
        self.assertEqual(law["ministry"], "금융위원회")

    def test_response_error_envelope_not_ok(self):
        code, ok, laws = nl.parse_law_search_xml(_RESPONSE_ERROR_XML)
        self.assertFalse(ok)
        self.assertEqual(laws, [])

    def test_non_00_result_code_not_ok(self):
        xml = "<LawSearch><resultCode>99</resultCode></LawSearch>"
        code, ok, laws = nl.parse_law_search_xml(xml)
        self.assertEqual(code, "99")
        self.assertFalse(ok)

    def test_html_and_malformed_not_ok(self):
        self.assertFalse(nl.parse_law_search_xml("<html><body>blocked</body></html>")[1])
        self.assertFalse(nl.parse_law_search_xml("not xml <<<")[1])
        self.assertFalse(nl.parse_law_search_xml("")[1])


class BodyParseTests(unittest.TestCase):
    def test_parse_body_shallow_gather(self):
        ok, articles = nl.parse_law_body_xml(BODY_XML)
        self.assertTrue(ok)
        self.assertEqual(len(articles), 1)
        art = articles[0]
        self.assertEqual(art["article_no"], "10")
        self.assertEqual(art["title"], "전세대출 규제")
        # gather pulled BOTH 조문내용 (inline) AND the 항내용 child.
        self.assertIn("전세대출", art["text"])
        self.assertIn("세부 사항", art["text"])

    def test_body_wrong_root_not_ok(self):
        self.assertFalse(nl.parse_law_body_xml(_RESPONSE_ERROR_XML)[0])
        self.assertFalse(nl.parse_law_body_xml("not xml <<<")[0])

    def test_assemble_body_caps_length(self):
        big = [{"title": "t", "text": "가" * 9000}]
        self.assertLessEqual(len(nl._assemble_body_text(big)), nl.MAX_ARTICLE_CHARS)


# ---------------------------------------------------------------------------
# Candidate construction + markers + full URL
# ---------------------------------------------------------------------------


class CandidateTests(unittest.TestCase):
    def test_markers_and_full_url(self):
        cands, n = nl.fetch_and_build_national_law_candidates([_CLAIM], provider=_mock_provider())
        self.assertEqual(n, 1)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["national_law_mst"], "276291")
        self.assertEqual(c["national_law_id"], "001248")
        self.assertEqual(c["source_type"], "official_government")
        self.assertTrue(c["raw_text_available"])
        self.assertTrue(c["raw_text"])
        self.assertEqual(c["retrieval_method"], "national_law_api")
        # relative 법령상세링크 was promoted to a FULL law.go.kr URL.
        self.assertTrue(c["url"].startswith("https://www.law.go.kr/"))
        # official_body_match must NOT be set here (M19-3: resolve computes it).
        self.assertNotIn("official_body_match", c)

    def test_empty_when_no_claims(self):
        self.assertEqual(
            nl.fetch_and_build_national_law_candidates([], provider=_mock_provider()), ([], 0))


# ---------------------------------------------------------------------------
# Resolve-overwrite regression (M22-1b shape) + Lane-B cap-70
# ---------------------------------------------------------------------------


class RealPipelineTests(unittest.TestCase):
    def setUp(self):
        cands, _ = nl.fetch_and_build_national_law_candidates([_CLAIM], provider=_mock_provider())
        resolved, _ = resolve_official_evidence(cands, [_CLAIM])
        self.evaluated = evaluate_source_candidates(resolved)
        self.cand = self.evaluated[0]

    def test_marker_survives_retrieval_method_overwrite(self):
        # resolve renames retrieval_method on a strong match...
        self.assertEqual(self.cand["retrieval_method"], "official_evidence_resolved")
        # ...but the stable marker survives resolve + evaluate.
        self.assertIn("national_law_mst", self.cand)
        self.assertTrue(self.cand["official_body_match"])
        self.assertEqual(
            self.cand["official_evidence_classification"], "strong_official_direct_support")

    def test_extractor_finds_law_match(self):
        m = extract_primary_document_match(self.evaluated)
        self.assertIsNotNone(m)
        self.assertTrue(_is_strong_primary_document_match(m))
        self.assertGreaterEqual(m["score"], 75)

    def test_lane_b_cap_70_never_verified(self):
        m = extract_primary_document_match(self.evaluated)
        cmp = ec.compare_news_with_official_evidence(
            news_title="금융위 전세대출 규제", news_summary="전세대출 규제 강화",
            article_body="전세대출 규제 강화", policy_claims=[{"sentence": "x"}],
            official_evidence_results=[], primary_document_match=m)
        conf = pc.calculate_policy_confidence(
            news_title="t", news_summary="s", article_body="a", policy_claims=[{"sentence": "x"}],
            official_evidence_results=[], evidence_comparison=cmp, primary_document_match=m)
        card = vc.build_verification_card(
            news={"title": "금융위 전세대출 규제", "summary": "전세대출 규제 강화"},
            original_url="https://news.example.com/a/1",
            policy_claims=[{"sentence": "x"}], official_evidence_results=[],
            evidence_comparison=cmp, policy_confidence=conf, article_body="전세대출 규제 강화",
            claims=["금융위원회가 전세대출 규제를 강화한다"], normalized_claims=[_CLAIM],
            source_queries=[], source_candidates=self.evaluated, evidence_snippets=[],
            claim_evidence_map={}, contradiction_checks=[], contradiction_summary={},
            bias_framing_analysis=[], bias_framing_summary={})
        self.assertEqual(cmp["verification_level"], "medium_official_match")
        self.assertEqual(conf["policy_confidence_score"], 70)
        self.assertEqual(conf["verification_strength"], "low")
        self.assertEqual(card["verdict_label"], "draft_likely_true")
        self.assertNotEqual(card["verdict_label"], "draft_verified")
        self.assertEqual(card["verdict_confidence"], 70)


# ---------------------------------------------------------------------------
# Disabled-path: zero network, byte-identical
# ---------------------------------------------------------------------------


class DisabledPathTests(unittest.TestCase):
    def test_disabled_returns_disabled_provider_zero_network(self):
        with _EnvScope():
            _set_env(NATIONAL_LAW_ENABLED="false", LAW_OC="dummy")
            with patch("requests.get") as mock_get:
                provider = nl.get_law_provider("national_law")
                self.assertIsInstance(provider, nl.DisabledNationalLawProvider)
                self.assertFalse(provider.available)
                cands, n = nl.fetch_and_build_national_law_candidates([_CLAIM])
            mock_get.assert_not_called()
            self.assertEqual((cands, n), ([], 0))

    def test_missing_oc_disabled_even_when_enabled(self):
        with _EnvScope():
            _set_env(NATIONAL_LAW_ENABLED="true", LAW_OC=None)
            provider = nl.get_law_provider("national_law")
            self.assertIsInstance(provider, nl.DisabledNationalLawProvider)
            self.assertIn("LAW_OC missing", provider.reason)


# ---------------------------------------------------------------------------
# Call-budget caps
# ---------------------------------------------------------------------------


class BudgetTests(unittest.TestCase):
    def test_derive_queries_capped(self):
        claims = [{"actor": f"기관{i}", "object": f"대상{i}"} for i in range(10)]
        self.assertLessEqual(len(nl._derive_queries(claims)), nl.MAX_SEARCHES)

    def test_body_fetches_capped_to_K(self):
        # Search returns 6 laws; only K (=3) bodies should be fetched.
        laws = "".join(
            f"<law id='{i}'><법령일련번호>{100+i}</법령일련번호><법령ID>{i}</법령ID>"
            f"<법령명한글><![CDATA[전세대출 규제 법률 {i}]]></법령명한글>"
            f"<법령상세링크>/lsInfoP.do?lsiSeq={100+i}</법령상세링크></law>"
            for i in range(6))
        search_xml = f"<LawSearch><resultCode>00</resultCode>{laws}</LawSearch>"
        body_map = {str(100 + i): BODY_XML for i in range(6)}

        class _Counting(nl.MockNationalLawProvider):
            calls = 0
            def fetch_law_body(self, mst):
                _Counting.calls += 1
                return super().fetch_law_body(mst)

        prov = _Counting(search_xml=search_xml, body_xml_by_mst=body_map)
        nl.fetch_and_build_national_law_candidates([_CLAIM], provider=prov)
        self.assertLessEqual(_Counting.calls, nl.MAX_KEPT_LAWS)
        self.assertLessEqual(_Counting.calls, nl.MAX_BODY_FETCHES)


# ---------------------------------------------------------------------------
# Coexistence with Policy Briefing — no marker collision
# ---------------------------------------------------------------------------


class CoexistenceTests(unittest.TestCase):
    def test_pb_and_law_both_recognized_crawl_ignored(self):
        # Hand-built post-resolve candidates (resolve overwrites retrieval_method).
        pb = {"retrieval_method": "official_evidence_resolved", "official_body_match": True,
              "official_evidence_classification": "strong_official_direct_support",
              "official_evidence_score": 80, "policy_briefing_news_item_id": "pb-1"}
        law = {"retrieval_method": "official_evidence_resolved", "official_body_match": True,
               "official_evidence_classification": "strong_official_direct_support",
               "official_evidence_score": 82, "national_law_mst": "276291"}
        crawl = {"retrieval_method": "official_evidence_resolved", "official_body_match": True,
                 "official_evidence_classification": "strong_official_direct_support",
                 "official_evidence_score": 90}  # NO primary-doc marker -> ignored
        # Each recognized on its own; crawl (no marker) is not.
        self.assertIsNotNone(extract_primary_document_match([pb]))
        self.assertIsNotNone(extract_primary_document_match([law]))
        self.assertIsNone(extract_primary_document_match([crawl]))
        # With all three, the best-scoring MARKED candidate wins (law, 82 > pb 80;
        # the unmarked crawl 90 is excluded).
        best = extract_primary_document_match([pb, law, crawl])
        self.assertEqual(best["score"], 82)


# ---------------------------------------------------------------------------
# REL-1 R1 — body-overlap selection filter (off-topic statutes dropped)
# ---------------------------------------------------------------------------


# An off-topic statute: 법령명 + body are about 세월호 (no finance terms), so a
# finance claim (_CLAIM) shares ZERO body tokens with it.
_OFFTOPIC_SEARCH_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<LawSearch><resultCode>00</resultCode><totalCnt>1</totalCnt>"
    "<law id=\"1\"><법령일련번호>900001</법령일련번호><법령ID>009001</법령ID>"
    "<법령명한글><![CDATA[4·16세월호참사 피해구제 및 지원 등을 위한 특별법 시행령]]></법령명한글>"
    "<시행일자>20200101</시행일자><소관부처명><![CDATA[해양수산부]]></소관부처명>"
    "<법령상세링크>/lsInfoP.do?lsiSeq=900001</법령상세링크></law></LawSearch>"
)
_OFFTOPIC_BODY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><법령><기본정보><법령ID>009001</법령ID></기본정보>'
    "<조문><조문단위><조문번호>3</조문번호><조문제목><![CDATA[피해자 범위]]></조문제목>"
    "<조문내용><![CDATA[세월호 참사 피해자에 대한 배상금과 추모 사업 및 진상규명 절차를 규정한다]]></조문내용>"
    "</조문단위></조문></법령>"
)


class RelOneBodyOverlapTests(unittest.TestCase):
    def test_offtopic_statute_dropped(self):
        # 세월호 statute returned for a finance claim -> body shares 0 material
        # tokens -> dropped before injection (no wrong official candidate).
        prov = nl.MockNationalLawProvider(
            search_xml=_OFFTOPIC_SEARCH_XML, body_xml_by_mst={"900001": _OFFTOPIC_BODY_XML})
        cands, n = nl.fetch_and_build_national_law_candidates([_CLAIM], provider=prov)
        self.assertEqual((cands, n), ([], 0))

    def test_name_disjoint_body_relevant_kept(self):
        # The canonical M23 law (법령명 "금융소비자 보호에 관한 법률") is name-disjoint
        # from _CLAIM but its BODY overlaps (전세대출/DSR/규제) -> MUST survive the
        # REL-1 filter (regression guard against over-filtering).
        cands, n = nl.fetch_and_build_national_law_candidates([_CLAIM], provider=_mock_provider())
        self.assertEqual(n, 1)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["national_law_mst"], "276291")

    def test_material_token_helpers(self):
        # structural / generic-admin words are stripped; domain terms are kept.
        self.assertFalse(nl._is_material_token("시행령"))
        self.assertFalse(nl._is_material_token("관한"))
        self.assertTrue(nl._is_material_token("전세대출"))
        body_toks = nl._law_body_material_tokens({"raw_text": "전세대출 한도와 DSR 규제 시행령"})
        self.assertIn("전세대출", body_toks)
        self.assertNotIn("시행령", body_toks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
