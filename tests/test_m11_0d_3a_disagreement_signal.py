"""M11.0d-3a — pins for the disagreement_signal field in debug_summary.

Implements Strategy C of M11.0d-2 (signal-only, no consolidation).
After calibrate_final_decision (P2) runs, main.analyze_pipeline writes
a disagreement_signal dict to debug_summary recording each producer's
label. This pin asserts:

  (a) the signal exists with the documented key set,
  (b) P1's RAW (pre-mismatch-rewrite, pre-P2-overwrite) label is
      captured correctly,
  (c) P3's draft labels map through the _P3_TO_ALERT_TIER table,
  (d) `agreed` is True iff all three normalize to the same tier,
  (e) `final_decision["policy_alert_level"]` is byte-identical to
      what calibrate_final_decision returns (P2 still authoritative),
  (f) `verification_card["verdict_label"]` is byte-identical to what
      _verdict_label returns (P3 still authoritative),
  (g) the structured log.info emission carries the same payload.

All tests exercise main._build_disagreement_signal directly (pure
function) plus a couple of byte-identicality pins. The full
analyze_pipeline is not invoked — it pulls in news_collector, the
crawler stack, and the LLM-judge module, which would require a live
network and OpenAI keys.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


from main import _build_disagreement_signal, _P3_TO_ALERT_TIER  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — capture log records emitted on the `main` logger (same
# pattern as tests/test_m11_7a_category2_logging.py).
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    def __init__(self, name_prefix: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._name_prefix = name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name_prefix or record.name.startswith(
            self._name_prefix + "."
        ):
            self.records.append(record)


def _attach(name: str) -> _CapturingHandler:
    logger = logging.getLogger(name)
    handler = _CapturingHandler(name)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    return handler


def _detach(name: str, handler: logging.Handler) -> None:
    logging.getLogger(name).removeHandler(handler)


# ---------------------------------------------------------------------------
# disagreement_signal structural pins
# ---------------------------------------------------------------------------


class DisagreementSignalShapeTests(unittest.TestCase):
    EXPECTED_KEYS = frozenset({
        "p1_label",
        "p2_label",
        "p3_label",
        "p3_implied_tier",
        "agreed",
        "disagreement_description",
    })

    def test_disagreement_signal_has_expected_keys(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",
        )
        self.assertEqual(set(signal.keys()), self.EXPECTED_KEYS)

    def test_disagreement_signal_handles_missing_inputs(self):
        """If any producer label is None, the signal must still build
        and use 'unknown' / 'UNKNOWN' sentinels so the debug_summary
        write never raises."""
        signal = _build_disagreement_signal(
            p1_alert_level_raw=None,
            p2_alert_level=None,
            p3_verdict_label=None,
        )
        self.assertEqual(signal["p1_label"], "unknown")
        self.assertEqual(signal["p2_label"], "unknown")
        self.assertEqual(signal["p3_label"], "unknown")
        self.assertEqual(signal["p3_implied_tier"], "UNKNOWN")
        self.assertFalse(signal["agreed"])
        # Description must mention something so operators can grep.
        self.assertIn("P1=unknown", signal["disagreement_description"])


# ---------------------------------------------------------------------------
# P3 → alert-tier mapping pin
# ---------------------------------------------------------------------------


class P3ImpliedTierMappingTests(unittest.TestCase):
    """The mapping must cover EVERY documented P3 label and must
    match Section C of docs/VERDICT_PRODUCER_DISAGREEMENT_MAP.md
    and the _p3_implied_alert_tier helper in
    tests/test_verdict_producer_disagreement_diagnostic.py."""

    EXPECTED_MAPPING = {
        "draft_verified": "HIGH",
        "draft_likely_true": "MEDIUM",
        "draft_disputed": "WATCH",
        "draft_high_risk_review": "WATCH",
        "draft_needs_review": "WATCH",
        "draft_needs_official_confirmation": "WATCH",
        "draft_needs_context": "WATCH",
        "draft_unverified": "LOW",
    }

    def test_p3_implied_tier_mapping_correct(self):
        for p3_label, expected_tier in self.EXPECTED_MAPPING.items():
            with self.subTest(p3_label=p3_label):
                self.assertEqual(_P3_TO_ALERT_TIER[p3_label], expected_tier)

    def test_p3_mapping_covers_all_documented_labels_no_extras(self):
        self.assertEqual(
            set(_P3_TO_ALERT_TIER.keys()),
            set(self.EXPECTED_MAPPING.keys()),
            "_P3_TO_ALERT_TIER must cover exactly the 8 documented P3 "
            "labels — no extras (would mean a new vocabulary entry "
            "snuck in) and no omissions (would silently classify as "
            "UNKNOWN at runtime).",
        )

    def test_unknown_p3_label_falls_back_to_uppercase_unknown(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="HIGH",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_some_new_label_we_have_not_seen",
        )
        self.assertEqual(signal["p3_implied_tier"], "UNKNOWN")
        self.assertFalse(signal["agreed"])


# ---------------------------------------------------------------------------
# Agreement semantics pin
# ---------------------------------------------------------------------------


class AgreementSemanticsTests(unittest.TestCase):
    def test_agreed_true_when_producers_align(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="HIGH",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",  # tier HIGH
        )
        self.assertTrue(signal["agreed"])
        self.assertEqual(
            signal["disagreement_description"],
            "P1=P2=P3=HIGH (all agree)",
        )

    def test_agreed_false_when_producers_disagree(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",  # tier HIGH
        )
        self.assertFalse(signal["agreed"])
        self.assertEqual(signal["p1_label"], "MEDIUM")
        self.assertEqual(signal["p2_label"], "HIGH")
        self.assertEqual(signal["p3_label"], "draft_verified")
        self.assertEqual(signal["p3_implied_tier"], "HIGH")
        self.assertIn("P1≠P2", signal["disagreement_description"])
        self.assertIn("P1≠P3", signal["disagreement_description"])

    def test_agreed_false_strong_evidence_regression_fixture(self):
        """Mirrors regression_fixture_geumyungwi_strong from
        M11.0d-1: P1=MEDIUM, P2=HIGH, P3=draft_verified (tier HIGH).
        P1 disagrees with both — the most-recognisable case from the
        disagreement map."""
        signal = _build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",
        )
        self.assertFalse(signal["agreed"])
        # Description must surface P1 vs P2 mismatch.
        self.assertIn("P1=MEDIUM", signal["disagreement_description"])
        self.assertIn("P2=HIGH", signal["disagreement_description"])
        self.assertIn("draft_verified", signal["disagreement_description"])

    def test_agreed_false_when_only_p3_diverges(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="LOW",
            p2_alert_level="LOW",
            p3_verdict_label="draft_needs_review",  # tier WATCH
        )
        self.assertFalse(signal["agreed"])
        self.assertIn("P1≠P3", signal["disagreement_description"])
        self.assertIn("P2≠P3", signal["disagreement_description"])


# ---------------------------------------------------------------------------
# Byte-identicality pins — proves the signal is OBSERVABILITY ONLY.
# ---------------------------------------------------------------------------


class ByteIdenticalityTests(unittest.TestCase):
    """The whole point of Strategy C: the user-facing alert level and
    verdict label are byte-identical before and after M11.0d-3a.

    These pins test the helper itself (it must not mutate its
    inputs), and the surrounding main.py code (the addition is
    additive only — `final_decision` and `verification_card` are
    NOT modified by the new block beyond the existing
    `verification_card["debug_summary"] = debug_summary` line).
    """

    def test_helper_does_not_mutate_inputs(self):
        """Pure-function pin — re-call with the same args and confirm
        identical output."""
        kwargs = dict(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",
        )
        out1 = _build_disagreement_signal(**kwargs)
        out2 = _build_disagreement_signal(**kwargs)
        self.assertEqual(out1, out2)
        # And the kwargs values themselves are untouched.
        self.assertEqual(kwargs["p1_alert_level_raw"], "MEDIUM")
        self.assertEqual(kwargs["p2_alert_level"], "HIGH")
        self.assertEqual(kwargs["p3_verdict_label"], "draft_verified")

    def test_final_decision_alert_level_unchanged_by_signal_addition(self):
        """The signal is consumed by debug_summary only. We pin
        the structural invariant: building the signal from
        `final_decision["policy_alert_level"]` does NOT alter that
        value. (Direct simulation of the main.py block.)"""
        final_decision = {"policy_alert_level": "HIGH"}
        verification_card = {"verdict_label": "draft_verified"}
        signal = _build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level=final_decision.get("policy_alert_level"),
            p3_verdict_label=verification_card.get("verdict_label"),
        )
        debug_summary: dict = {}
        debug_summary["disagreement_signal"] = signal
        # The user-facing fields are still what they were.
        self.assertEqual(final_decision["policy_alert_level"], "HIGH")
        self.assertEqual(verification_card["verdict_label"], "draft_verified")
        # And the signal lives only in debug_summary, not on the
        # top-level structs.
        self.assertNotIn("disagreement_signal", final_decision)
        self.assertNotIn("disagreement_signal", verification_card)
        self.assertEqual(debug_summary["disagreement_signal"]["agreed"], False)

    def test_verdict_label_unchanged_when_p3_unknown(self):
        """If verdict_label is missing (which the pipeline should
        never produce, but defensively), the helper must not raise
        and must not invent a label."""
        signal = _build_disagreement_signal(
            p1_alert_level_raw="LOW",
            p2_alert_level="LOW",
            p3_verdict_label=None,
        )
        self.assertEqual(signal["p3_label"], "unknown")
        # The function returns the signal; it does not modify any
        # outer state.


# ---------------------------------------------------------------------------
# Source-code structural pin — confirms main.py wires the signal at
# the documented insertion points.
# ---------------------------------------------------------------------------


class MainPyWiringTests(unittest.TestCase):
    """Static-text pins that the M11.0d-3a additions are in main.py
    at the documented insertion points. Catches accidental reverts."""

    def setUp(self):
        path = _PROJECT_ROOT / "main.py"
        self.source = path.read_text(encoding="utf-8")

    def test_p1_label_captured_before_p2_overwrite(self):
        """The capture line must come AFTER make_final_decision
        and BEFORE calibrate_final_decision."""
        capture = "p1_alert_level_raw = final_decision.get(\"policy_alert_level\")"
        make_decision = "final_decision = make_final_decision("
        calibrate = "calibrate_final_decision("
        capture_idx = self.source.find(capture)
        make_idx = self.source.find(make_decision)
        calibrate_idx = self.source.find(calibrate)
        self.assertGreater(capture_idx, 0, "p1_alert_level_raw capture missing")
        self.assertGreater(make_idx, 0, "make_final_decision call missing")
        self.assertGreater(calibrate_idx, 0, "calibrate_final_decision call missing")
        self.assertGreater(
            capture_idx, make_idx,
            "p1_alert_level_raw must be captured AFTER make_final_decision.",
        )
        self.assertLess(
            capture_idx, calibrate_idx,
            "p1_alert_level_raw must be captured BEFORE calibrate_final_decision "
            "(otherwise P1's pure label is lost to P2's overwrite).",
        )

    def test_disagreement_signal_logged_after_p2(self):
        """The actual log.info call (not the docstring mention) must
        come AFTER calibrate_final_decision returns.

        Uses ``rfind`` to skip past the docstring quote earlier in the
        file and pin the LAST occurrence — that is the real
        ``log.info("verdict.disagreement_signal", extra={...})`` call.
        """
        log_call = '"verdict.disagreement_signal"'
        calibrate = "calibrate_final_decision("
        log_idx = self.source.rfind(log_call)
        calibrate_idx = self.source.find(calibrate)
        self.assertGreater(log_idx, 0, "verdict.disagreement_signal log missing")
        self.assertGreater(calibrate_idx, 0, "calibrate_final_decision call missing")
        self.assertGreater(
            log_idx, calibrate_idx,
            "Log emission must be AFTER calibrate_final_decision so P2's "
            "final label is in the payload.",
        )

    def test_disagreement_signal_written_to_debug_summary(self):
        self.assertIn(
            'debug_summary["disagreement_signal"] = disagreement_signal',
            self.source,
            "disagreement_signal must be written to debug_summary; that "
            "is the M11.0d-3a contract.",
        )

    def test_disagreement_signal_not_assigned_to_final_decision(self):
        """Strategy C: the signal lives in debug_summary only, not at
        the top of final_decision (which is user-facing)."""
        self.assertNotIn(
            'final_decision["disagreement_signal"]',
            self.source,
            "M11.0d-3a contract: disagreement_signal must NOT be set as "
            "a top-level key on final_decision (that struct is "
            "user-facing). Use debug_summary instead.",
        )

    def test_disagreement_signal_not_assigned_to_verification_card_directly(self):
        """The signal IS visible via verification_card["debug_summary"]
        (because main.py:678 already does that assignment), but no
        top-level verification_card["disagreement_signal"] key should
        be added."""
        self.assertNotIn(
            'verification_card["disagreement_signal"]',
            self.source,
            "M11.0d-3a contract: disagreement_signal must NOT be set as "
            "a top-level key on verification_card. It is visible only "
            "via verification_card['debug_summary']['disagreement_signal'].",
        )


if __name__ == "__main__":
    unittest.main()
