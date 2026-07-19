"""HONESTY-GUARD B3 Phase 2a — tests for the pure validator honesty_guard.py.

Offline: no DB, no network, no app wiring. Mirrors the existing honesty-test
pattern (test_trending_endpoint.py: forbidden-vocab + inspect.getsource
guards) and the generate_weekly_report.honesty_guard_ok byte-exact-framing
precedent. The closed sets in honesty_guard are duplicated constants; the
SyncWithAuthoritativeSourcesTests below pin them against their sources so
divergence fails CI instead of drifting silently.
"""

import copy
import inspect
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import honesty_guard  # noqa: E402
from honesty_guard import validate_payload  # noqa: E402


def _rules(violations):
    return [v["rule"] for v in violations]


# ---------------------------------------------------------------------------
# A realistic known-good /analyze-shaped payload. Deliberately includes every
# false-positive trap from the Phase 1 map: 검증 in our honest fixed copy and
# in a journalist title, draft_* labels, the legal confidence keys, a nested
# artifact row with truth_claim=False / operator_review_required=True, and
# both byte-exact framing strings.
# ---------------------------------------------------------------------------
def good_payload():
    return {
        "status": "ok",
        "results": [{
            "title": "정부 검증 절차 강화… 사실 확인 착수",  # passthrough — I5-exempt
            "claim_text": "정부가 청년 지원을 확대한다",
            "verdict_label": "draft_needs_context",
            "verdict_confidence": 40,
            "policy_confidence": {
                "policy_confidence_score": 55,
                "verification_strength": "low",
                "risk_level": "medium",
            },
            "final_decision": {"policy_alert_level": "LOW"},
            "verification_card": {
                "verdict_label": "draft_needs_context",
                "verdict_confidence": 40,
                "review_status": "ai_draft_pending_human_review",
                "missing_context": [
                    "검증에 사용할 수 있는 공식 상세문서가 부족합니다.",
                    "최종 공개 전 사람 검토와 원문 재확인이 필요합니다.",
                ],
                "evidence_summary": "공식문서와 뉴스 주장 사이의 검증이 필요합니다.",
                "has_genuine_official_support": False,
            },
            "artifact_row": {
                "truth_claim": False,
                "operator_review_required": True,
            },
        }],
        "weekly": {
            "framing": "확산 규모 기준 · 사실 검증 아님",
            "kind": "spread",
            "top": [{"size_label": "9개 매체 보도 중", "outlet_count": 9}],
        },
        "faded": {
            "framing": (
                "이 목록은 후속 보도가 끊긴 사실만 보여줍니다. 주장의 진위나 정책의 "
                "추진·성패에 대한 판단이 아니며, 후속 보도가 저희 수집망 밖에 "
                "있었을 수도 있습니다."
            ),
        },
    }


class GoodCardTests(unittest.TestCase):
    def test_known_good_payload_passes(self):
        ok, violations = validate_payload(good_payload())
        self.assertTrue(ok, "unexpected violations: %r" % violations)
        self.assertEqual(violations, [])


class I1TruthClaimTests(unittest.TestCase):
    def test_truth_claim_true_fails(self):
        payload = good_payload()
        payload["results"][0]["artifact_row"]["truth_claim"] = True
        ok, violations = validate_payload(payload)
        self.assertFalse(ok)
        self.assertEqual(_rules(violations), ["I1_TRUTH_CLAIM_NOT_FALSE"])
        self.assertIn("artifact_row.truth_claim", violations[0]["path"])

    def test_truth_claim_truthy_nonbool_fails(self):
        payload = good_payload()
        payload["results"][0]["artifact_row"]["truth_claim"] = 1
        ok, violations = validate_payload(payload)
        self.assertEqual(_rules(violations), ["I1_TRUTH_CLAIM_NOT_FALSE"])


class I2OperatorReviewTests(unittest.TestCase):
    def test_operator_review_false_fails(self):
        payload = good_payload()
        payload["results"][0]["artifact_row"]["operator_review_required"] = False
        ok, violations = validate_payload(payload)
        self.assertFalse(ok)
        self.assertEqual(_rules(violations), ["I2_REVIEW_NOT_REQUIRED"])


class I3VerdictLabelTests(unittest.TestCase):
    def _with_label(self, label):
        payload = good_payload()
        payload["results"][0]["verdict_label"] = label
        payload["results"][0]["verification_card"]["verdict_label"] = label
        return payload

    def test_raised_verdict_fails(self):
        for leaked in ("verified", "likely_true", "true", "false",
                       "confirmed", "draft_bogus"):
            ok, violations = validate_payload(self._with_label(leaked))
            self.assertFalse(ok, "leaked label %r passed" % leaked)
            self.assertEqual(set(_rules(violations)),
                             {"I3_ILLEGAL_VERDICT_LABEL"})

    def test_non_string_label_fails(self):
        ok, violations = validate_payload(self._with_label(None))
        self.assertEqual(set(_rules(violations)), {"I3_ILLEGAL_VERDICT_LABEL"})

    def test_every_legal_draft_label_passes(self):
        for label in sorted(honesty_guard.LEGAL_VERDICT_LABELS):
            ok, violations = validate_payload(self._with_label(label))
            self.assertTrue(ok, "legal label %r flagged: %r" % (label, violations))

    def test_illegal_alert_level_fails(self):
        payload = good_payload()
        payload["results"][0]["final_decision"]["policy_alert_level"] = "RED"
        ok, violations = validate_payload(payload)
        self.assertEqual(_rules(violations), ["I3_ILLEGAL_ALERT_LEVEL"])

    def test_legal_and_unset_alert_levels_pass(self):
        for level in ("HIGH", "MEDIUM", "WATCH", "LOW", "", None):
            payload = good_payload()
            payload["results"][0]["final_decision"]["policy_alert_level"] = level
            ok, violations = validate_payload(payload)
            self.assertTrue(ok, "alert level %r flagged: %r" % (level, violations))


class I4TruthProbabilityKeyTests(unittest.TestCase):
    def test_nested_truth_probability_key_fails(self):
        payload = good_payload()
        payload["results"][0]["verification_card"]["extra"] = {
            "truth_probability": 0.93}
        ok, violations = validate_payload(payload)
        self.assertFalse(ok)
        self.assertEqual(_rules(violations), ["I4_TRUTH_PROBABILITY_KEY"])
        self.assertIn("extra.truth_probability", violations[0]["path"])

    def test_denylist_shapes_fail(self):
        for key in ("p_true", "P(true)", "veracity_score", "factuality",
                    "is_true", "likely_true", "accuracy_score",
                    "truth-likelihood"):
            payload = good_payload()
            payload["results"][0][key] = 0.5
            ok, violations = validate_payload(payload)
            self.assertEqual(_rules(violations), ["I4_TRUTH_PROBABILITY_KEY"],
                             "key %r not caught" % key)

    def test_whitelisted_confidence_keys_pass(self):
        # verdict_confidence / policy_confidence_score are already in the
        # good payload; assert explicitly they never trip I4.
        ok, violations = validate_payload(good_payload())
        self.assertTrue(ok)
        self.assertFalse(
            honesty_guard._is_truth_probability_key("verdict_confidence"))
        self.assertFalse(
            honesty_guard._is_truth_probability_key("policy_confidence_score"))


class I5VocabAndFramingTests(unittest.TestCase):
    def test_forbidden_vocab_in_generated_label_fails(self):
        payload = good_payload()
        payload["weekly"]["top"][0]["size_label"] = "검증된 주장 9건"
        ok, violations = validate_payload(payload)
        self.assertFalse(ok)
        self.assertEqual(_rules(violations), ["I5_FORBIDDEN_VOCAB"])

    def test_forbidden_vocab_in_kind_fails(self):
        payload = good_payload()
        payload["weekly"]["kind"] = "Verified"  # case-insensitive latin match
        ok, violations = validate_payload(payload)
        self.assertEqual(_rules(violations), ["I5_FORBIDDEN_VOCAB"])

    def test_vocab_in_passthrough_and_fixed_copy_passes(self):
        # Journalist title + missing_context + evidence_summary all carry 검증
        # in the good payload and must NOT be scanned (field scope, not content).
        ok, violations = validate_payload(good_payload())
        self.assertTrue(ok, violations)

    def test_framing_drift_fails(self):
        payload = good_payload()
        payload["weekly"]["framing"] = "확산 규모 기준 - 사실 검증 아님"  # one char off
        ok, violations = validate_payload(payload)
        self.assertFalse(ok)
        self.assertEqual(_rules(violations), ["I5_FRAMING_DRIFT"])

    def test_both_real_framing_strings_pass(self):
        ok, violations = validate_payload(good_payload())
        self.assertTrue(ok, violations)


class PurityTests(unittest.TestCase):
    def test_never_mutates_payload(self):
        payload = good_payload()
        payload["results"][0]["artifact_row"]["truth_claim"] = True  # violating
        frozen = copy.deepcopy(payload)
        validate_payload(payload)
        self.assertEqual(payload, frozen)

    def test_deterministic(self):
        payload = good_payload()
        payload["results"][0]["verdict_label"] = "verified"
        first = validate_payload(payload)
        second = validate_payload(payload)
        self.assertEqual(first, second)

    def test_odd_shapes_never_raise(self):
        for odd in (None, 3, "text", [], {}, {"a": {1: 2}}, [{"b": (1, 2)}],
                    {"truth_claim": False}, {"verdict_label": ""}):
            validate_payload(odd)  # must not raise

    def test_cyclic_structure_never_hangs(self):
        payload = {"a": {}}
        payload["a"]["self"] = payload
        ok, violations = validate_payload(payload)
        self.assertTrue(ok)


class SyncWithAuthoritativeSourcesTests(unittest.TestCase):
    """The duplicated constants must match their authoritative sources."""

    def test_verdict_labels_match_verification_card(self):
        import verification_card
        source = inspect.getsource(verification_card._verdict_label)
        from_source = set(re.findall(r'return "(draft_[a-z_]+)"', source))
        self.assertEqual(from_source | {""},
                         set(honesty_guard.LEGAL_VERDICT_LABELS))

    def test_alert_levels_match_policy_decision(self):
        import policy_decision
        source = inspect.getsource(policy_decision._policy_alert_level)
        from_source = set(re.findall(r'return "([A-Z]+)", reasons', source))
        self.assertEqual(from_source, set(honesty_guard.LEGAL_ALERT_LEVELS))

    def test_vocab_superset_of_brainmap_constant(self):
        import build_brainmap_graph
        self.assertTrue(
            set(build_brainmap_graph.FORBIDDEN_LABEL_VOCAB)
            <= set(honesty_guard.FORBIDDEN_LABEL_VOCAB))

    def test_framing_whitelist_matches_sources_byte_exact(self):
        import api_server
        import build_brainmap_graph
        import generate_weekly_report
        self.assertEqual(
            honesty_guard.FRAMING_WHITELIST,
            frozenset({generate_weekly_report.FRAMING_TEXT,
                       api_server._FADED_FRAMING,
                       build_brainmap_graph.SYNDICATION_FRAMING}))

    def test_syndication_framing_whitelisted_and_vocab_clean(self):
        # B5d 2b: the syndication framing is exposed via /api/spread — it must
        # be byte-exact whitelisted AND carry no forbidden truth vocab (it is
        # descriptive spread structure, never 복붙/베낌/truth-implying).
        import build_brainmap_graph
        framing = build_brainmap_graph.SYNDICATION_FRAMING
        self.assertIn(framing, honesty_guard.FRAMING_WHITELIST)
        for word in honesty_guard.FORBIDDEN_LABEL_VOCAB:
            self.assertNotIn(word, framing)
        for word in ("복붙", "베낌", "베꼈", "표절"):
            self.assertNotIn(word, framing)
        # A payload carrying it under the "framing" key passes I5.
        ok, violations = validate_payload({"framing": framing})
        self.assertTrue(ok, violations)


class VerdictIsolationTests(unittest.TestCase):
    """honesty_guard CHECKS only: no verdict-module import, no verdict raise."""

    def test_no_verdict_module_import(self):
        source = inspect.getsource(honesty_guard)
        for module in ("verification_card", "policy_decision", "llm_judge",
                       "api_server", "main"):
            self.assertNotRegex(
                source, r"(?m)^\s*(import %s\b|from %s\b)" % (module, module))

    def test_import_side_effect_free(self):
        # Module-level surface is constants + functions only (no I/O at import).
        for name, obj in vars(honesty_guard).items():
            if (name.startswith("__") or inspect.ismodule(obj)
                    or name == "annotations"):  # the __future__ feature object
                continue
            self.assertTrue(
                callable(obj) or isinstance(obj, (frozenset, tuple, str)),
                "unexpected module-level object %s=%r" % (name, type(obj)))


class ViolationPersistBestEffortTests(unittest.TestCase):
    """HONESTY-GUARD-DB-LOG: the observability insert is BEST-EFFORT.

    The lock: when the DB write fails for ANY reason (no engine, DB down,
    table missing), a report-mode response must still pass through
    BYTE-IDENTICAL and with its original status — the observability add must
    never alter, delay, or break a response. This is the regression that
    matters, because the insert runs on the already-failing violation path.
    """

    def _violating_payload(self):
        # I1: truth_claim must be exactly False. This is a REAL violation, so
        # the middleware takes the persist path.
        return {"truth_claim": True, "operator_review_required": True}

    def test_persist_swallows_missing_engine(self):
        import api_server

        import postgres_storage

        original = postgres_storage.get_engine
        postgres_storage.get_engine = lambda: None
        try:
            # Must return normally (None), never raise.
            self.assertIsNone(api_server._honesty_persist_violation(
                "report", "/api/test", [{"rule": "I1", "path": "$"}]))
        finally:
            postgres_storage.get_engine = original

    def test_persist_swallows_engine_error(self):
        import api_server

        def boom():
            raise RuntimeError("DB down")

        import postgres_storage

        original = postgres_storage.get_engine
        postgres_storage.get_engine = boom
        try:
            self.assertIsNone(api_server._honesty_persist_violation(
                "report", "/api/test", [{"rule": "I1", "path": "$"}]))
        finally:
            postgres_storage.get_engine = original

    def test_report_mode_response_bytes_unchanged_when_persist_fails(self):
        import json as _json
        import os

        from fastapi import Response
        from fastapi.testclient import TestClient

        import api_server
        import postgres_storage

        payload = self._violating_payload()
        expected = _json.dumps(payload).encode("utf-8")
        # Guard against a trivially-passing test: this payload MUST actually
        # violate, or the middleware never reaches the persist path at all.
        ok, _violations = validate_payload(payload)
        self.assertFalse(ok, "test payload must violate to exercise persist")

        # A throwaway route on the real app so the real middleware runs.
        @api_server.app.get("/__honesty_persist_test__")
        def _probe():  # pragma: no cover - exercised via TestClient
            return Response(content=expected,
                            media_type="application/json")

        def boom():
            raise RuntimeError("DB down")

        original_env = os.environ.get("HONESTY_GUARD_MODE")
        original_engine = postgres_storage.get_engine
        os.environ["HONESTY_GUARD_MODE"] = "report"
        postgres_storage.get_engine = boom
        try:
            with TestClient(api_server.app) as client:
                resp = client.get("/__honesty_persist_test__")
            # Report mode: passes through untouched despite the failed insert.
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.content, expected)
        finally:
            postgres_storage.get_engine = original_engine
            if original_env is None:
                os.environ.pop("HONESTY_GUARD_MODE", None)
            else:
                os.environ["HONESTY_GUARD_MODE"] = original_env
            api_server.app.router.routes = [
                r for r in api_server.app.router.routes
                if getattr(r, "path", None) != "/__honesty_persist_test__"]


if __name__ == "__main__":
    unittest.main()
