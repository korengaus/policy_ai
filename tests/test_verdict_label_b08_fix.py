"""Phase 2 M11.0c: pinned tests for the B08 conservative fix in
``verification_card._verdict_label``.

Background
----------

M11.0b diagnostic data attributed 28 production rows to the B08 branch
(``claim_count and direct_support_count >= claim_count``). Of those:

    * 21 rows had ``score_leq_30`` AND ``strength_none`` → wrongly
      labelled ``draft_verified`` against the conservative invariant.
    * 7 rows had ``policy_confidence_score >= 61`` AND
      ``verification_strength in {medium, high}`` → correctly
      labelled ``draft_verified``.

The fix adds two gates to B08:

    * ``policy_confidence_score >= 60``
    * ``verification_strength in _STRONG_VERIFICATION_STRENGTHS``
      (frozenset({"medium", "high"}) — using the exact strings
      ``policy_confidence._verification_strength`` emits)

Pinning checks
--------------

This file pins every behavioural property the fix is supposed to
preserve so a future refactor that removes a gate fails immediately:

    * 21-bad-pattern lockdown (parameterized, ≥5 representative rows)
    * 7-good-pattern preservation (parameterized, ≥5 representative
      rows)
    * Boundary cases (score=59, 60, 100; strength=weak, none)
    * Exact ID-105 regression (the row that originally exposed the bug)
    * Other branches still behave as documented (B01 conflict,
      B02 high_framing+confirmed, B13 strict-confidence verified)
    * Constant integrity (_STRONG_VERIFICATION_STRENGTHS shape)
    * Diagnostic-catalog parity (B08 catalog entry post-M11.0c)
    * Static safety (no network imports in verification_card.py)
"""

from __future__ import annotations

import importlib
import inspect
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import verification_card  # noqa: E402
from verification_card import (  # noqa: E402
    _STRONG_VERIFICATION_STRENGTHS,
    _verdict_label,
)
import verdict_label_diagnostic as diagnostic  # noqa: E402


VERIFICATION_CARD_PATH = ROOT / "verification_card.py"


# ---------------------------------------------------------------------------
# Helpers — single-claim direct_support snippets used across many tests
# ---------------------------------------------------------------------------


def _direct_support_snippets(n: int = 1) -> list:
    return [{"evidence_type": "direct_support"} for _ in range(n)]


def _call(
    *, score: int, strength: str, claim_count: int = 1,
    direct_support_count: int = 1, official_sources=None,
    evidence_comparison=None, contradiction=None, bias=None,
    extra_snippets=None,
) -> str:
    """Invoke ``_verdict_label`` with the shape main.py passes in
    production. Defaults yield a row equivalent to the M11.0b
    diagnostic shape (one claim, one direct_support snippet, no other
    evidence_type entries)."""
    policy_confidence = {
        "policy_confidence_score": score,
        "verification_strength": strength,
    }
    snippets = _direct_support_snippets(direct_support_count)
    if extra_snippets:
        snippets = snippets + list(extra_snippets)
    return _verdict_label(
        policy_confidence,
        evidence_comparison or {},
        official_sources or [],
        evidence_snippets=snippets,
        contradiction_summary=contradiction or {},
        bias_framing_summary=bias or {},
        claim_count=claim_count,
    )


# ---------------------------------------------------------------------------
# 21-bad-pattern lockdown
# ---------------------------------------------------------------------------


# Each fixture mirrors the (score, strength, claim, direct_support)
# tuple the M11.0b diagnostic flagged. We include >5 representative
# combinations to cover the diagnostic's reported variants.
BAD_PATTERNS = [
    # The canonical ID-105 shape: zero score, no strength, single
    # direct_support snippet.
    {"score": 10, "strength": "none"},
    # Variants the M11.0b investigation listed (IDs 58, 65, 82, 83, 87,
    # 95, 104). All have low score + none strength.
    {"score": 0, "strength": "none"},
    {"score": 5, "strength": "none"},
    {"score": 15, "strength": "none"},
    {"score": 20, "strength": "none"},
    {"score": 30, "strength": "none"},
    # Even with a non-zero "low" strength the row should still fail.
    {"score": 10, "strength": "low"},
    {"score": 0, "strength": "low"},
]


class BadPatternLockdownTests(unittest.TestCase):
    """Every bad-pattern row must NOT emit ``draft_verified``."""

    def test_bad_patterns_never_return_draft_verified(self):
        for pattern in BAD_PATTERNS:
            with self.subTest(**pattern):
                result = _call(**pattern)
                self.assertNotEqual(
                    result, "draft_verified",
                    f"bad pattern {pattern} produced draft_verified "
                    "— B08 gate failed",
                )

    def test_bad_patterns_land_on_conservative_label(self):
        # The fall-through after B08 reaches B09/B10/B11/B12. For the
        # canonical bad shape (no official_sources + strength='none'),
        # B12 fires → draft_unverified. Pinning that path makes a
        # future regression that flips the cascade order visible.
        for pattern in BAD_PATTERNS:
            with self.subTest(**pattern):
                result = _call(**pattern)
                # Acceptable conservative outcomes when B08 is gated:
                # - draft_unverified (B12 — no official_sources OR
                #   verification_strength=='none')
                # - draft_needs_official_confirmation (B09 — if the
                #   row had official_reference snippets, which our
                #   bad fixtures do not)
                # - draft_needs_context (B10/B11/B15)
                # Anything OTHER than these is a regression.
                self.assertIn(
                    result,
                    {
                        "draft_unverified",
                        "draft_needs_official_confirmation",
                        "draft_needs_context",
                    },
                    f"bad pattern {pattern} landed on unexpected "
                    f"label {result!r}",
                )


# ---------------------------------------------------------------------------
# 7-good-pattern preservation
# ---------------------------------------------------------------------------


# Score-strength tuples drawn from the M11.0b "7 good rows" cluster:
# all had score >= 61 and strength in {medium, high}.
GOOD_PATTERNS = [
    {"score": 61, "strength": "medium"},
    {"score": 65, "strength": "medium"},
    {"score": 76, "strength": "high"},
    {"score": 77, "strength": "high"},
    {"score": 79, "strength": "high"},
    {"score": 87, "strength": "high"},
    {"score": 100, "strength": "high"},
]


class GoodPatternPreservationTests(unittest.TestCase):
    """Every good-pattern row must continue to emit ``draft_verified``."""

    def test_good_patterns_still_return_draft_verified(self):
        for pattern in GOOD_PATTERNS:
            with self.subTest(**pattern):
                result = _call(**pattern)
                self.assertEqual(
                    result, "draft_verified",
                    f"good pattern {pattern} no longer returns "
                    f"draft_verified — got {result!r}; B08 gate "
                    "may be too aggressive",
                )


# ---------------------------------------------------------------------------
# Boundary tests
# ---------------------------------------------------------------------------


class BoundaryTests(unittest.TestCase):
    def test_score_59_with_medium_blocked(self):
        # 59 < 60 → score gate fails.
        result = _call(score=59, strength="medium")
        self.assertNotEqual(result, "draft_verified")

    def test_score_60_with_medium_passes(self):
        # 60 is the inclusive boundary.
        result = _call(score=60, strength="medium")
        self.assertEqual(result, "draft_verified")

    def test_score_100_with_weak_blocked(self):
        # weak is not in _STRONG_VERIFICATION_STRENGTHS, even at full score.
        result = _call(score=100, strength="weak")
        self.assertNotEqual(result, "draft_verified")

    def test_score_100_with_none_blocked(self):
        result = _call(score=100, strength="none")
        self.assertNotEqual(result, "draft_verified")

    def test_claim_count_2_with_direct_support_1_blocked(self):
        # B08's existing count gate (direct_support_count >= claim_count)
        # must still apply. claim_count=2 + direct_support=1 should not
        # reach B08 regardless of score/strength.
        result = _call(
            score=88, strength="high", claim_count=2,
            direct_support_count=1,
        )
        self.assertNotEqual(result, "draft_verified")

    def test_claim_count_2_with_direct_support_2_passes(self):
        # claim_count=2 + direct_support=2 + score/strength gates ok.
        # Note: official_sources empty would let B12 short-circuit
        # before B08 in the original code, but B12 sits AFTER B08 in
        # the function body. B08 is reached first — gates fire — and
        # it returns draft_verified.
        result = _call(
            score=88, strength="high", claim_count=2,
            direct_support_count=2,
        )
        self.assertEqual(result, "draft_verified")


# ---------------------------------------------------------------------------
# Exact ID-105 regression
# ---------------------------------------------------------------------------


class Id105RegressionTests(unittest.TestCase):
    """Reproduce the exact production ID-105 input that originally
    exposed the bug. Pin the post-fix label."""

    def test_id_105_exact_pattern_is_no_longer_draft_verified(self):
        policy_confidence = {
            "policy_confidence_score": 10,
            "verification_strength": "none",
        }
        evidence_comparison = {
            "comparison_status": "official_evidence_missing",
            "verification_level": None,
        }
        evidence_snippets = [{
            "evidence_type": "direct_support",
            "claim_text": "정부가 전세 보증금을 지원한다",
        }]
        result = _verdict_label(
            policy_confidence,
            evidence_comparison,
            [],  # official_sources empty
            evidence_snippets=evidence_snippets,
            contradiction_summary={},
            bias_framing_summary={},
            claim_count=1,
        )
        # Post-fix: B08 is gated out (score=10 < 60), B12 fires →
        # draft_unverified.
        self.assertEqual(
            result, "draft_unverified",
            "ID-105 pattern still produces draft_verified — "
            "B08 fix did not take effect",
        )


# ---------------------------------------------------------------------------
# Other branches unaffected
# ---------------------------------------------------------------------------


class OtherBranchesUnaffectedTests(unittest.TestCase):
    """Confirm the fix did not regress the rest of _verdict_label."""

    def test_b01_conflict_still_returns_draft_disputed(self):
        result = _verdict_label(
            {"policy_confidence_score": 80, "verification_strength": "high"},
            {"conflict_signals": ["polarity_mismatch"]},
            [{"title": "official", "url": "https://example.go.kr"}],
            evidence_snippets=[],
            contradiction_summary={},
            bias_framing_summary={},
            claim_count=1,
        )
        self.assertEqual(result, "draft_disputed")

    def test_b02_high_framing_with_confirmed_still_returns_high_risk(self):
        result = _verdict_label(
            {"policy_confidence_score": 50, "verification_strength": "medium"},
            {},
            [{"title": "official", "url": "https://example.go.kr"}],
            evidence_snippets=[],
            contradiction_summary={"confirmed_contradiction_count": 2},
            bias_framing_summary={"high_framing_count": 1},
            claim_count=1,
        )
        self.assertEqual(result, "draft_high_risk_review")

    def test_b13_strict_confidence_still_returns_draft_verified(self):
        # B13 requires score >= 85 AND verification_level ==
        # strong_official_match AND the snippet counts not to satisfy
        # B08 (so direct_support_count must be < claim_count).
        result = _verdict_label(
            {"policy_confidence_score": 90, "verification_strength": "high"},
            {"verification_level": "strong_official_match"},
            [{"title": "official", "url": "https://moef.go.kr/x"}],
            evidence_snippets=[],  # zero direct_support → B08 cannot fire
            contradiction_summary={},
            bias_framing_summary={},
            claim_count=2,
        )
        self.assertEqual(result, "draft_verified")

    def test_b14_medium_confidence_still_returns_draft_likely_true(self):
        result = _verdict_label(
            {"policy_confidence_score": 70, "verification_strength": "medium"},
            {"verification_level": "medium_official_match"},
            [{"title": "official", "url": "https://moef.go.kr/x"}],
            evidence_snippets=[],
            contradiction_summary={},
            bias_framing_summary={},
            claim_count=2,
        )
        self.assertEqual(result, "draft_likely_true")

    def test_b04_confirmed_contradiction_still_returns_disputed(self):
        result = _verdict_label(
            {"policy_confidence_score": 90, "verification_strength": "high"},
            {},
            [{"title": "official", "url": "https://moef.go.kr/x"}],
            evidence_snippets=[],
            contradiction_summary={"confirmed_contradiction_count": 1},
            bias_framing_summary={},
            claim_count=1,
        )
        self.assertEqual(result, "draft_disputed")


# ---------------------------------------------------------------------------
# Constant integrity
# ---------------------------------------------------------------------------


class StrongVerificationStrengthsTests(unittest.TestCase):
    def test_constant_is_frozenset(self):
        self.assertIsInstance(_STRONG_VERIFICATION_STRENGTHS, frozenset)

    def test_constant_contains_medium_and_high(self):
        self.assertIn("medium", _STRONG_VERIFICATION_STRENGTHS)
        self.assertIn("high", _STRONG_VERIFICATION_STRENGTHS)

    def test_constant_excludes_weak_and_none(self):
        self.assertNotIn("weak", _STRONG_VERIFICATION_STRENGTHS)
        self.assertNotIn("none", _STRONG_VERIFICATION_STRENGTHS)
        self.assertNotIn("low", _STRONG_VERIFICATION_STRENGTHS)

    def test_constant_is_immutable(self):
        # frozenset has no .add(); confirm.
        with self.assertRaises(AttributeError):
            _STRONG_VERIFICATION_STRENGTHS.add("bogus")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Diagnostic catalog parity
# ---------------------------------------------------------------------------


class B08CatalogParityTests(unittest.TestCase):
    """Confirm verdict_label_diagnostic's catalog reflects the M11.0c
    fix. The fix is meaningless if the catalog still labels B08 as
    the bug surface."""

    def _b08(self) -> dict:
        for entry in diagnostic.VERDICT_LABEL_BRANCHES:
            if entry["branch_id"] == "B08_direct_support_only":
                return entry
        self.fail("B08 entry missing from VERDICT_LABEL_BRANCHES")

    def test_b08_risk_classification_is_strict(self):
        self.assertEqual(
            self._b08()["risk_classification"],
            diagnostic.RISK_VERIFIED_STRICT,
        )

    def test_b08_trigger_summary_mentions_gates(self):
        summary = self._b08()["trigger_summary"]
        self.assertIn("confidence_score", summary)
        self.assertIn("verification_strength", summary)
        # Mention of the threshold + the strong-strength names —
        # exact spelling is up to the catalog text, just confirm
        # the numbers and strings landed somewhere.
        self.assertIn("60", summary)
        self.assertIn("medium", summary)
        self.assertIn("high", summary)

    def test_no_branch_is_verified_without_strict_checks(self):
        # The whole point of M11.0c: zero loose-verified branches.
        loose = [
            b for b in diagnostic.VERDICT_LABEL_BRANCHES
            if b["risk_classification"]
            == diagnostic.RISK_VERIFIED_LOOSE
        ]
        self.assertEqual(
            loose, [],
            "found a verified_without_strict_checks branch after "
            f"M11.0c: {loose!r}",
        )


# ---------------------------------------------------------------------------
# Static safety
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def test_verification_card_does_not_import_network_or_openai(self):
        source = VERIFICATION_CARD_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in source.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"verification_card.py must not import {forbidden!r}",
            )

    def test_verification_card_is_importable_without_side_effects(self):
        # Re-import the module and confirm the public surface is still
        # present and the constant is intact. A fresh import_module
        # call must not raise.
        module = importlib.import_module("verification_card")
        self.assertTrue(hasattr(module, "_verdict_label"))
        self.assertTrue(hasattr(module, "_STRONG_VERIFICATION_STRENGTHS"))
        # Function signature must NOT have changed (M11.0c only edits
        # the body of one branch).
        sig = inspect.signature(module._verdict_label)
        params = list(sig.parameters)
        self.assertEqual(
            params[:3],
            ["policy_confidence", "evidence_comparison", "official_sources"],
        )
        for name in (
            "evidence_snippets", "contradiction_summary",
            "bias_framing_summary", "claim_count",
        ):
            self.assertIn(name, params)


if __name__ == "__main__":
    unittest.main()
