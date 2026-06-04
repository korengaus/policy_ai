"""Tests for M21 Phase 2b — Policy Briefing press-release PrimaryDocumentProvider.

Run with: python tests/test_m21_policy_briefing.py

Covers:
(1) XML parse / normalize — Title entity unescape, DataContents tag-strip,
    MinisterCode->ministry, OriginalUrl->url, FileUrl capture.
(2) Fail-closed — THREE_DAYS_OVER_ERROR / non-200 / malformed XML -> empty
    documents + error, never raises.
(3) Disabled path — POLICY_BRIEFING_ENABLED=false -> DisabledPolicyBriefingProvider,
    zero network (requests.get asserted not called), fetch_and_build -> ([], 0).
(4) Candidate contract — source_type/raw_text_available/raw_text body, one
    candidate per (claim x release), official_body_match NEVER set here.
(5) Option-A uplift only on genuine body-match — through the real
    resolve_official_evidence + evaluate_source_candidates: matching body ->
    official_body_match True + reliability uplift; non-matching -> falsy + capped.
(6) Invariant — provider/candidate path never introduces truth_claim or
    operator_review_required (evidence only).

NO real API call is ever made — requests.get is patched everywhere.
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


import config  # noqa: E402
from providers import policy_briefing as pb  # noqa: E402
from official_evidence_resolution import resolve_official_evidence  # noqa: E402
from source_reliability_agent import evaluate_source_candidates  # noqa: E402


# ---------------------------------------------------------------------------
# Env scope helper — mirrors test_m20_2's _EnvScope.
# ---------------------------------------------------------------------------


class _EnvScope:
    KEYS = (
        "POLICY_BRIEFING_ENABLED",
        "DATAGOKR_SERVICE_KEY",
        "POLICY_BRIEFING_TIMEOUT_SECONDS",
    )

    def __enter__(self):
        self._snapshot = {key: os.environ.get(key) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _set_env(**values):
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


# A long matching body (>= 300 chars after strip) so resolve's has_body holds.
_MATCH_BODY = (
    "<p>금융위원회는 전세대출 규제를 강화한다고 발표했다. "
    "전세대출 한도와 DSR 규제를 함께 조정한다. 이번 대책은 "
    "가계부채 관리와 주택시장 안정을 목표로 한다. 전세대출 "
    "규제는 수도권 규제지역에 우선 적용된다. 금융위원회는 "
    "실수요자 보호를 위한 예외 규정도 마련한다고 밝혔다. "
    "대출 심사 기준과 DSR 산정 방식도 정비된다. 금융당국은 "
    "전세대출 규제 시행 시기를 추가로 안내할 예정이다. "
    "금융위원회는 전세대출 규제와 DSR 규제를 단계적으로 "
    "확대 적용한다고 설명했다. 가계부채 증가세를 억제하기 "
    "위해 전세대출 심사를 강화하고 주택담보대출 규제도 함께 "
    "정비한다. 금융당국은 전세대출 규제 효과를 점검하며 "
    "실수요자 보호 방안을 지속적으로 보완할 계획이라고 "
    "밝혔다. 전세대출 규제와 DSR 한도 조정은 주택시장 "
    "안정과 가계부채 관리를 위한 핵심 정책으로 추진된다.</p>"
)

_NORMAL_XML = f"""<response>
  <header><resultCode>0</resultCode><resultMsg>NORMAL_SERVICE</resultMsg></header>
  <body>
    <NewsItem>
      <NewsItemId>148900001</NewsItemId>
      <Title><![CDATA[전세대출 규제 강화 &middot; 실수요자 보호]]></Title>
      <SubTitle1>금융위원회 보도자료</SubTitle1>
      <DataContents><![CDATA[{_MATCH_BODY}]]></DataContents>
      <MinisterCode>금융위원회</MinisterCode>
      <OriginalUrl>https://www.korea.kr/news/policyNewsView.do?newsId=148900001</OriginalUrl>
      <ApproveDate>06/02/2026 09:30:00</ApproveDate>
      <EmbargoDate></EmbargoDate>
      <FileName>전세대출규제.hwp</FileName>
      <FileUrl>https://www.korea.kr/file/0001.hwp</FileUrl>
    </NewsItem>
  </body>
</response>"""

_THREE_DAYS_OVER_XML = (
    "<response><header><resultCode>99</resultCode>"
    "<resultMsg>THREE_DAYS_OVER_ERROR</resultMsg></header><body></body></response>"
)


# ---------------------------------------------------------------------------
# (1) XML parse / normalize.
# ---------------------------------------------------------------------------


class XmlParseTests(unittest.TestCase):
    def test_parse_and_normalize(self):
        code, msg, raw_items = pb.parse_press_release_xml(_NORMAL_XML)
        self.assertEqual(code, "0")
        self.assertEqual(msg, "NORMAL_SERVICE")
        self.assertEqual(len(raw_items), 1)

        doc = pb._normalize_item(raw_items[0])
        self.assertEqual(doc["id"], "148900001")
        # Title entity unescaped (&middot; -> ·), no raw entity remains.
        self.assertIn("·", doc["title"])
        self.assertNotIn("&middot;", doc["title"])
        # DataContents tags stripped to plain text.
        self.assertNotIn("<p>", doc["body"])
        self.assertNotIn("</p>", doc["body"])
        self.assertIn("전세대출", doc["body"])
        self.assertEqual(doc["ministry"], "금융위원회")
        self.assertEqual(
            doc["original_url"],
            "https://www.korea.kr/news/policyNewsView.do?newsId=148900001",
        )
        self.assertEqual(doc["file_urls"], ["https://www.korea.kr/file/0001.hwp"])

    def test_mock_provider_uses_same_normalizer(self):
        result = pb.MockPolicyBriefingProvider().fetch_press_releases(
            start_date="20260601", end_date="20260603"
        )
        self.assertTrue(result["available"])
        self.assertGreaterEqual(len(result["documents"]), 1)
        first = result["documents"][0]
        self.assertNotIn("&middot;", first["title"])
        self.assertNotIn("<b>", first["body"])


# ---------------------------------------------------------------------------
# (2) Fail-closed paths.
# ---------------------------------------------------------------------------


class FailClosedTests(unittest.TestCase):
    def _enabled_provider(self):
        _set_env(POLICY_BRIEFING_ENABLED="true", DATAGOKR_SERVICE_KEY="dummy-key")
        return pb.PolicyBriefingProvider()

    def test_three_days_over_error_is_empty(self):
        with _EnvScope():
            provider = self._enabled_provider()
            with patch("requests.get", return_value=_FakeResponse(_THREE_DAYS_OVER_XML)):
                result = provider.fetch_press_releases(start_date="20260101", end_date="20260131")
            self.assertEqual(result["documents"], [])
            self.assertEqual(result["error"], "THREE_DAYS_OVER_ERROR")

    def test_non_200_is_empty(self):
        with _EnvScope():
            provider = self._enabled_provider()
            with patch("requests.get", return_value=_FakeResponse("", status_code=500)):
                result = provider.fetch_press_releases(start_date="20260601", end_date="20260603")
            self.assertEqual(result["documents"], [])
            self.assertIn("500", result["error"])

    def test_malformed_xml_is_empty(self):
        with _EnvScope():
            provider = self._enabled_provider()
            with patch("requests.get", return_value=_FakeResponse("not xml <<<")):
                result = provider.fetch_press_releases(start_date="20260601", end_date="20260603")
            self.assertEqual(result["documents"], [])
            self.assertEqual(result["error"], "XML_PARSE_ERROR")

    def test_transport_error_never_raises(self):
        with _EnvScope():
            provider = self._enabled_provider()
            with patch("requests.get", side_effect=RuntimeError("boom")):
                result = provider.fetch_press_releases(start_date="20260601", end_date="20260603")
            self.assertEqual(result["documents"], [])
            self.assertIn("request failed", result["error"])


# ---------------------------------------------------------------------------
# (3) Disabled path — zero network.
# ---------------------------------------------------------------------------


class DisabledPathTests(unittest.TestCase):
    def test_disabled_returns_disabled_provider_zero_network(self):
        with _EnvScope():
            _set_env(POLICY_BRIEFING_ENABLED="false", DATAGOKR_SERVICE_KEY="dummy-key")
            with patch("requests.get") as mock_get:
                provider = pb.get_document_provider("policy_briefing")
                self.assertIsInstance(provider, pb.DisabledPolicyBriefingProvider)
                self.assertFalse(provider.available)
                result = provider.fetch_press_releases(start_date="20260601", end_date="20260603")
            mock_get.assert_not_called()
            self.assertEqual(result["documents"], [])

    def test_missing_key_disabled_even_when_enabled(self):
        with _EnvScope():
            _set_env(POLICY_BRIEFING_ENABLED="true", DATAGOKR_SERVICE_KEY=None)
            provider = pb.get_document_provider("policy_briefing")
            self.assertIsInstance(provider, pb.DisabledPolicyBriefingProvider)
            self.assertIn("DATAGOKR_SERVICE_KEY missing", provider.reason)

    def test_fetch_and_build_disabled_returns_empty(self):
        with _EnvScope():
            _set_env(POLICY_BRIEFING_ENABLED="false")
            claims = [{"claim_text": "전세대출 규제 강화"}]
            with patch("requests.get") as mock_get:
                candidates, count = pb.fetch_and_build_policy_briefing_candidates(claims)
            mock_get.assert_not_called()
            self.assertEqual(candidates, [])
            self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# (4) Candidate contract.
# ---------------------------------------------------------------------------


def _norm_doc(doc_id: str, title: str, body: str, *, url: str, ministry: str = "금융위원회",
              approve_date: str = "06/02/2026 09:30:00") -> dict:
    return {
        "id": doc_id,
        "title": title,
        "subtitle": "",
        "body": body,
        "ministry": ministry,
        "original_url": url,
        "approve_date": approve_date,
        "embargo_date": "",
        "file_urls": [],
        "raw": {},
    }


class CandidateContractTests(unittest.TestCase):
    def test_one_candidate_per_claim_per_release_and_fields(self):
        docs = [
            _norm_doc("a", "전세대출 규제", "전세대출 규제 본문",
                      url="https://www.korea.kr/news/policyNewsView.do?newsId=1"),
            _norm_doc("b", "DSR 한도 조정", "DSR 한도 조정 본문",
                      url="https://www.korea.kr/news/policyNewsView.do?newsId=2"),
        ]
        claims = [{"claim_text": "전세대출 규제"}, {"claim_text": "DSR 한도"}]
        candidates, count = pb.to_official_source_candidates(docs, claims)
        # 2 claims x 2 releases.
        self.assertEqual(len(candidates), 4)
        self.assertEqual(count, 2)
        for cand in candidates:
            self.assertEqual(cand["source_type"], "official_government")
            self.assertTrue(cand["raw_text_available"])
            self.assertTrue(cand["raw_text"])
            self.assertEqual(cand["retrieval_method"], "policy_briefing_api")
            self.assertEqual(cand["purpose"], "primary_source")
            self.assertEqual(cand["url"], cand["official_detail_url"])
            # MANDATORY: official_body_match is NEVER set here.
            self.assertNotIn("official_body_match", cand)
        # Both claim indices represented.
        self.assertEqual({c["claim_index"] for c in candidates}, {0, 1})

    def test_empty_when_no_claims_or_no_docs(self):
        self.assertEqual(pb.to_official_source_candidates([], [{"claim_text": "x"}]), ([], 0))
        self.assertEqual(pb.to_official_source_candidates([_norm_doc("a", "t", "b", url="u")], []), ([], 0))

    def test_zero_overlap_release_excluded(self):
        # M34 — reverses the prior M21 rank-to-fill/never-exclude behavior:
        # a release sharing ZERO claim-token overlap is now EXCLUDED (the
        # body-matcher still judges STRENGTH for survivors, but off-topic
        # noise no longer gets injected).
        docs = [_norm_doc("z", "축제 일정 안내", "지역 축제 행사 일정 본문",
                          url="https://www.korea.kr/news/policyNewsView.do?newsId=9")]
        claims = [{"claim_text": "전세대출 규제 강화"}]
        candidates, count = pb.to_official_source_candidates(docs, claims)
        self.assertEqual(count, 0)
        self.assertEqual(candidates, [])

    def test_overlap_at_least_one_release_kept(self):
        # M34 recall-safety: a release sharing >= 1 claim token survives the
        # precision filter and is injected.
        docs = [_norm_doc("k", "전세대출 규제 설명", "전세대출 규제 본문",
                          url="https://www.korea.kr/news/policyNewsView.do?newsId=10")]
        claims = [{"claim_text": "전세대출 규제 강화"}]
        candidates, count = pb.to_official_source_candidates(docs, claims)
        self.assertEqual(count, 1)
        self.assertEqual(len(candidates), 1)


# ---------------------------------------------------------------------------
# (5) Option-A uplift only on genuine body-match (M19-3 guard).
# ---------------------------------------------------------------------------


class OptionAUpliftTests(unittest.TestCase):
    def _resolve_and_evaluate(self, doc, claim):
        candidates, _ = pb.to_official_source_candidates([doc], [claim])
        resolved, _ = resolve_official_evidence(candidates, [claim])
        return evaluate_source_candidates(resolved)

    def test_matching_body_earns_uplift(self):
        doc = _norm_doc(
            "match",
            "전세대출 규제 강화 실수요자 보호",
            pb._strip_tags(_MATCH_BODY),
            url="https://www.korea.kr/news/policyNewsView.do?newsId=100",
        )
        claim = {
            "claim_text": "금융위원회가 전세대출 규제를 강화하고 DSR 한도를 조정한다고 발표했다",
            "actor": "금융위원회",
            "action": "규제 강화",
            "target": "전세대출",
            "object": "DSR 한도",
        }
        evaluated = self._resolve_and_evaluate(doc, claim)
        self.assertEqual(len(evaluated), 1)
        cand = evaluated[0]
        self.assertTrue(cand.get("official_body_match"))
        # korea.kr base 95; genuine match keeps it in the uplift band.
        self.assertGreaterEqual(cand["reliability_score"], 84)
        self.assertEqual(cand["verification_role"], "primary_evidence")

    def test_non_matching_body_no_uplift(self):
        # M34 — the doc shares ONE incidental claim token ("전세대출") so it
        # survives the precision filter (overlap >= 1), but its body does NOT
        # support the claim, so the matcher grants no body-match / no uplift.
        # (A truly zero-overlap doc is now excluded upstream — see
        # test_zero_overlap_release_excluded.)
        doc = _norm_doc(
            "nomatch",
            "지역 축제 행사 안내",
            (
                "올해 지역 축제가 다양한 프로그램으로 열린다. 가족 단위 "
                "방문객을 위한 체험 행사와 공연이 준비됐다. 주말 동안 "
                "여러 무대에서 음악 공연이 이어진다. 먹거리 장터와 "
                "전시 부스도 함께 운영된다. 주최 측은 안전 관리에 "
                "만전을 기하겠다고 밝혔다. 자세한 일정은 누리집에서 "
                "확인할 수 있다고 안내했다. 이 행사는 전세대출 정책과는 "
                "무관하다."
            ),
            url="https://www.korea.kr/news/policyNewsView.do?newsId=200",
        )
        claim = {
            "claim_text": "금융위원회가 전세대출 규제를 강화하고 DSR 한도를 조정한다고 발표했다",
            "actor": "금융위원회",
            "target": "전세대출",
        }
        evaluated = self._resolve_and_evaluate(doc, claim)
        cand = evaluated[0]
        self.assertFalse(cand.get("official_body_match"))
        # Non-match penalty caps the official candidate at 70.
        self.assertLessEqual(cand["reliability_score"], 70)


# ---------------------------------------------------------------------------
# (6) Invariant — evidence only, never truth/verdict.
# ---------------------------------------------------------------------------


class InvariantTests(unittest.TestCase):
    def test_candidates_and_resolution_never_set_truth_or_review(self):
        doc = _norm_doc(
            "inv",
            "전세대출 규제 강화",
            pb._strip_tags(_MATCH_BODY),
            url="https://www.korea.kr/news/policyNewsView.do?newsId=300",
        )
        claim = {"claim_text": "전세대출 규제 강화 DSR 한도 조정", "target": "전세대출"}
        candidates, _ = pb.to_official_source_candidates([doc], [claim])
        resolved, _ = resolve_official_evidence(candidates, [claim])
        evaluated = evaluate_source_candidates(resolved)
        for cand in candidates + resolved + evaluated:
            self.assertNotIn("truth_claim", cand)
            self.assertNotIn("operator_review_required", cand)

    def test_describe_config_reports_presence_only(self):
        with _EnvScope():
            _set_env(POLICY_BRIEFING_ENABLED="true", DATAGOKR_SERVICE_KEY="super-secret-value")
            described = config.describe_policy_briefing_config()
            self.assertTrue(described["enabled"])
            self.assertTrue(described["service_key_present"])
            # The secret value must NEVER appear in the snapshot.
            self.assertNotIn("super-secret-value", str(described))


if __name__ == "__main__":
    unittest.main(verbosity=2)
