"""Tests for FSS-PROVIDER — FSS 보도자료 (bodoInfo) PrimaryDocumentProvider.

Run with: python -m pytest tests/test_fss_press_release.py
       or: python tests/test_fss_press_release.py

Mirrors tests/test_m21_policy_briefing.py / tests/test_m23_national_law.py.

Covers:
(1) JSON parse / normalize — the REAL response shape ("reponse" envelope typo,
    resultCode "1", contentKor body typo), subject entity unescape,
    publishOrg->publisher, originUrl->url, contentId->id.
(2) ★ Body cleaning — HTML tags stripped, literal "nn"/"u203B" noise removed,
    HTML entities unescaped, whitespace normalized.
(3) Fail-closed — resultCode!="1" / non-200 / malformed JSON -> empty documents
    + error, never raises.
(4) Disabled path — FSS_ENABLED=false -> DisabledFssProvider, ZERO network
    (requests.get asserted not called), fetch_and_build -> ([], 0).
(5) Candidate contract — source_type/raw_text_available/raw_text body, one
    candidate per (claim x release), official_body_match NEVER set here.
(6) ★ LANE-A ISOLATION — the stable marker fss_bodo_content_id is NOT a member
    of official_evidence_resolution._PRIMARY_DOCUMENT_MARKER_FIELDS, so FSS gets
    NO Lane-B verdict-raise uplift.
(7) Dedup — duplicate contentId collapses to one release.
(8) Invariant — the provider/candidate path never introduces truth_claim or
    operator_review_required (evidence only).

NO real API call is ever made — requests.get is patched everywhere a real
provider could call it.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import config  # noqa: E402
from providers import fss_press_release as fss  # noqa: E402


# ---------------------------------------------------------------------------
# Env scope helper — mirrors test_m21's _EnvScope.
# ---------------------------------------------------------------------------


class _EnvScope:
    KEYS = (
        "FSS_ENABLED",
        "FSS_API_KEY",
        "FSS_TIMEOUT_SECONDS",
        "FSS_LOOKBACK_DAYS",
        "FSS_MAX_RELEASES",
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
            os.environ[key] = str(value)


class _Resp:
    """Minimal fake requests.Response."""

    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": "application/json"}


# A real-shape FSS bodoInfo JSON body: the "reponse" envelope typo, resultCode
# "1", and contentKor (no trailing s) carrying HTML tags, literal "nn"/"u203B"
# noise, and HTML entities — exactly what the live API returns.
_REAL_BODY = (
    "<p>26.5월 全 금융권 가계대출은 +9.3조원 증가하였다.</p>nn"
    "<p>주택담보대출이 증가세를 주도하였으며 &lt;가계부채&gt; 관리 기조를 "
    "&quot;유지&quot;한다. u203B 첨부파일 참고.</p>"
)
_REAL_JSON = json.dumps({
    "reponse": {
        "resultCode": "1",
        "resultMsg": "NORMAL",
        "resultCnt": 2,
        "result": [
            {
                "contentId": "fss-1001",
                "subject": "2026년 5월 가계대출 동향 &middot; 잠정",
                "publishOrg": "금융감독원",
                "originUrl": "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=fss-1001",
                "regDate": "2026-06-05",
                "contentKor": _REAL_BODY,
            },
            {
                "contentId": "fss-1002",
                "subject": "보험회사 건전성 감독 강화",
                "publishOrg": "금융감독원",
                "originUrl": "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=fss-1002",
                "regDate": "2026-06-03",
                "contentKor": "<p>보험회사 지급여력비율 관리를 강화한다.</p>",
            },
        ],
    }
}, ensure_ascii=False)


_CLAIMS = [
    {"claim_text": "전 금융권 가계대출이 9.3조원 증가했다", "object": "가계대출"},
]


class ParseNormalizeTests(unittest.TestCase):
    def test_parse_real_shape(self):
        code, cnt, items = fss.parse_bodo_json(_REAL_JSON)
        self.assertEqual(code, "1")
        self.assertEqual(cnt, 2)
        self.assertEqual(len(items), 2)
        self.assertTrue(fss._is_success(code))

    def test_normalize_maps_fields(self):
        _, _, items = fss.parse_bodo_json(_REAL_JSON)
        doc = fss._normalize_item(items[0])
        self.assertEqual(doc["id"], "fss-1001")
        self.assertEqual(doc["publisher"], "금융감독원")
        self.assertEqual(
            doc["original_url"],
            "https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=fss-1001",
        )
        self.assertEqual(doc["reg_date"], "2026-06-05")
        # subject entity (&middot;) unescaped to ·
        self.assertIn("·", doc["title"])
        self.assertNotIn("&middot;", doc["title"])

    def test_malformed_json_returns_empty(self):
        code, cnt, items = fss.parse_bodo_json("not json {{{")
        self.assertEqual((code, cnt, items), ("", 0, []))

    def test_wrong_envelope_returns_empty(self):
        # No "reponse"/"response" envelope -> empty, never raises.
        code, cnt, items = fss.parse_bodo_json(json.dumps({"foo": {"bar": 1}}))
        self.assertEqual((code, cnt, items), ("", 0, []))


class BodyCleaningTests(unittest.TestCase):
    def test_clean_body_strips_tags_noise_entities(self):
        _, _, items = fss.parse_bodo_json(_REAL_JSON)
        body = fss._normalize_item(items[0])["body"]
        # (1) HTML tags gone
        self.assertNotIn("<p>", body)
        self.assertNotIn("</p>", body)
        # (2) literal noise gone
        self.assertNotIn("u203B", body)
        self.assertNotIn("nn", body)
        # (3) entities unescaped (not left as &lt; &gt; &quot;)
        self.assertNotIn("&lt;", body)
        self.assertNotIn("&gt;", body)
        self.assertNotIn("&quot;", body)
        # real content survived
        self.assertIn("가계대출", body)
        self.assertIn("9.3조원", body)
        # entity decoded to real chars
        self.assertIn("<가계부채>", body)

    def test_clean_body_empty(self):
        self.assertEqual(fss._clean_body(None), "")
        self.assertEqual(fss._clean_body(""), "")


class FailClosedTests(unittest.TestCase):
    def _ready_provider(self):
        _set_env(FSS_ENABLED="true", FSS_API_KEY="x" * 32)
        provider = fss.FssPressReleaseProvider()
        self.assertTrue(provider.available)
        return provider

    def test_non_200_empty_no_raise(self):
        with _EnvScope():
            provider = self._ready_provider()
            with patch("requests.get", return_value=_Resp(503, "boom")) as g:
                result = provider.fetch_press_releases(
                    start_date="2026-06-01", end_date="2026-06-07",
                )
            self.assertTrue(g.called)
            self.assertEqual(result["documents"], [])
            self.assertIsNotNone(result["error"])

    def test_result_code_not_one_empty(self):
        bad = json.dumps({"reponse": {"resultCode": "0", "resultMsg": "ERR", "result": []}})
        with _EnvScope():
            provider = self._ready_provider()
            with patch("requests.get", return_value=_Resp(200, bad)):
                result = provider.fetch_press_releases(
                    start_date="2026-06-01", end_date="2026-06-07",
                )
            self.assertEqual(result["documents"], [])
            self.assertIsNotNone(result["error"])

    def test_transport_error_empty_no_raise(self):
        with _EnvScope():
            provider = self._ready_provider()
            with patch("requests.get", side_effect=RuntimeError("conn reset")):
                result = provider.fetch_press_releases(
                    start_date="2026-06-01", end_date="2026-06-07",
                )
            self.assertEqual(result["documents"], [])
            self.assertIsNotNone(result["error"])

    def test_success_path_returns_documents(self):
        with _EnvScope():
            provider = self._ready_provider()
            with patch("requests.get", return_value=_Resp(200, _REAL_JSON)):
                result = provider.fetch_press_releases(
                    start_date="2026-06-01", end_date="2026-06-07",
                )
            self.assertEqual(len(result["documents"]), 2)
            self.assertIsNone(result["error"])


class DisabledPathTests(unittest.TestCase):
    def test_disabled_gate_zero_network(self):
        with _EnvScope():
            _set_env(FSS_ENABLED="false", FSS_API_KEY="x" * 32)
            provider = fss.get_fss_provider("fss_press_release")
            self.assertIsInstance(provider, fss.DisabledFssProvider)
            self.assertFalse(provider.available)
            with patch("requests.get") as g:
                cands, count = fss.fetch_and_build_fss_candidates(_CLAIMS)
                # disabled provider also returns an empty result with no network
                result = provider.fetch_press_releases(start_date="a", end_date="b")
            self.assertFalse(g.called)
            self.assertEqual((cands, count), ([], 0))
            self.assertEqual(result["documents"], [])

    def test_missing_key_disabled(self):
        with _EnvScope():
            _set_env(FSS_ENABLED="true", FSS_API_KEY=None)
            provider = fss.get_fss_provider("fss_press_release")
            self.assertIsInstance(provider, fss.DisabledFssProvider)
            self.assertFalse(provider.available)


class CandidateContractTests(unittest.TestCase):
    def _candidates(self, claims=None):
        _, _, items = fss.parse_bodo_json(_REAL_JSON)
        docs = [fss._normalize_item(it) for it in items]
        return fss.to_official_source_candidates(docs, claims or _CLAIMS)

    def test_relevance_filter_drops_offtopic_release(self):
        # The 가계대출 claim overlaps the 가계대출 release (fss-1001) but NOT the
        # off-topic insurance release (fss-1002) -> MIN_CLAIM_TOKEN_OVERLAP=1
        # correctly injects only the on-topic release. Supply-and-filter model.
        cands, injected = self._candidates()
        self.assertEqual(injected, 1)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["fss_bodo_content_id"], "fss-1001")

    def test_candidate_shape(self):
        cands, _ = self._candidates()
        c = cands[0]
        self.assertEqual(c["source_type"], "official_government")
        self.assertTrue(c["raw_text_available"])
        self.assertTrue(c["official_body_fetched"])
        self.assertEqual(c["publisher"], "금융감독원")
        self.assertTrue(c["raw_text"])
        self.assertEqual(c["official_body_length"], len(c["raw_text"]))
        self.assertEqual(c["retrieval_method"], "fss_bodo_api")
        self.assertEqual(c["claim_index"], 0)

    def test_one_candidate_per_claim_release(self):
        # TWO claims, both overlapping the 가계대출 release -> 2 claims x 1 release
        # = 2 candidates (claim_index 0 and 1). Demonstrates the cross-product.
        two_claims = [
            {"claim_text": "전 금융권 가계대출이 9.3조원 증가했다", "object": "가계대출"},
            {"claim_text": "가계대출 증가세를 주택담보대출이 주도했다", "object": "주택담보대출"},
        ]
        cands, injected = self._candidates(claims=two_claims)
        self.assertEqual(injected, 1)
        self.assertEqual(len(cands), 2)
        self.assertEqual({c["claim_index"] for c in cands}, {0, 1})

    def test_official_body_match_never_set_here(self):
        cands, _ = self._candidates()
        for c in cands:
            self.assertNotIn("official_body_match", c)

    def test_marker_present_for_provenance(self):
        cands, _ = self._candidates()
        self.assertEqual(cands[0]["fss_bodo_content_id"], "fss-1001")
        self.assertEqual(cands[0]["fss_bodo_publish_org"], "금융감독원")

    def test_invariant_no_truth_claim_or_review_flag(self):
        cands, _ = self._candidates()
        for c in cands:
            self.assertNotIn("truth_claim", c)
            self.assertNotIn("operator_review_required", c)


class LaneAIsolationTests(unittest.TestCase):
    """★ The crux: FSS gets Lane-A injection ONLY — its marker must NOT grant the
    Lane-B verdict-raise uplift."""

    def test_marker_not_in_primary_document_marker_fields(self):
        from official_evidence_resolution import _PRIMARY_DOCUMENT_MARKER_FIELDS

        self.assertNotIn("fss_bodo_content_id", _PRIMARY_DOCUMENT_MARKER_FIELDS)
        # sanity: the established PB/law markers ARE there (we didn't touch them)
        self.assertIn("policy_briefing_news_item_id", _PRIMARY_DOCUMENT_MARKER_FIELDS)
        self.assertIn("national_law_mst", _PRIMARY_DOCUMENT_MARKER_FIELDS)

    def test_extract_primary_document_match_ignores_fss(self):
        # An FSS candidate carrying the resolve-computed strong-match fields would
        # STILL be ignored by extract_primary_document_match because it lacks a
        # recognized marker — proving no Lane-B uplift path exists for FSS.
        from official_evidence_resolution import extract_primary_document_match

        fss_like = {
            "fss_bodo_content_id": "fss-1001",
            "official_body_match": True,
            "official_evidence_classification": "strong_official_direct_support",
            "official_evidence_score": 99,
            "score": 99,
        }
        self.assertIsNone(extract_primary_document_match([fss_like]))


class DedupTests(unittest.TestCase):
    def test_duplicate_content_id_collapses(self):
        dup = json.dumps({
            "reponse": {
                "resultCode": "1",
                "resultCnt": 2,
                "result": [
                    {"contentId": "dup-1", "subject": "가계대출 동향",
                     "publishOrg": "금융감독원", "originUrl": "u1",
                     "regDate": "2026-06-05", "contentKor": "<p>가계대출 증가</p>"},
                    {"contentId": "dup-1", "subject": "가계대출 동향",
                     "publishOrg": "금융감독원", "originUrl": "u1",
                     "regDate": "2026-06-05", "contentKor": "<p>가계대출 증가</p>"},
                ],
            }
        }, ensure_ascii=False)
        with _EnvScope():
            _set_env(FSS_ENABLED="true", FSS_API_KEY="x" * 32)
            with patch("requests.get", return_value=_Resp(200, dup)):
                cands, injected = fss.fetch_and_build_fss_candidates(_CLAIMS)
            # 1 claim x 1 deduped release = 1 candidate
            self.assertEqual(injected, 1)
            self.assertEqual(len(cands), 1)


if __name__ == "__main__":
    unittest.main()
