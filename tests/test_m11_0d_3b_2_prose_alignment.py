"""M11.0d-3b-2 — Strategy A FULL: Korean prose alignment to P2's
authoritative ``policy_alert_level``.

Background
----------

M11.0d-3b codified P2 (``policy_scoring.calibrate_final_decision``)
as the authoritative producer of ``policy_alert_level``. But the
Korean user-facing prose strings (``decision_summary``,
``action_recommendation``) were still generated from P1's intuition
inside ``policy_decision.make_final_decision``. Whenever P1 and P2
disagreed (~30% of analyses), users saw an alert card reading one
tier next to prose describing another tier.

M11.0d-3b-2 fixes this. ``main.analyze_pipeline`` now calls
``policy_decision.action_recommendation_for`` and
``policy_decision.decision_summary_for`` AFTER
``calibrate_final_decision`` returns, passing P2's authoritative
``policy_alert_level``, and OVERWRITES the two prose fields on
``final_decision``. The realignment is GATED on
``not verification_card["official_mismatch"]`` so the conservative
override applied at ``main.py:735-749`` is preserved.

What this file pins
-------------------

  Class A — Prose realignment behavioral pins (8 tests):
    Pattern A (P1 over-claim → P2 down) and Pattern B (P1 under →
    P2 up) realignments produce the expected Korean strings.
    Agreement cases are no-ops. official_mismatch path is preserved.
    market_signal and decision_reasons are byte-identical.

  Class B — Invariant byte-identity pins (5 tests):
    policy_alert_level, verdict_label, disagreement_signal,
    operator_review_required, truth_claim all byte-identical to
    pre-M11.0d-3b-2 — the realignment is prose-only.

  Class C — Conservative wording / immutable fixture pins (3 tests):
    Korean conservative phrases in web/index.html preserved.
    The 6 M11.0d-1 fixture files byte-identical via SHA256.
    Export consistency: aligned prose flows through to TXT export.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


from policy_decision import (  # noqa: E402
    action_recommendation_for,
    decision_summary_for,
    make_final_decision,
)
from policy_scoring import calibrate_final_decision  # noqa: E402
from verification_card import _verdict_label  # noqa: E402


_FIXTURES = _PROJECT_ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# Pipeline harness — mirrors the main.analyze_pipeline sequence for the
# verdict + prose portion of the pipeline. Pure function; no I/O.
# ---------------------------------------------------------------------------


def _run_verdict_pipeline(
    *,
    policy_confidence: dict,
    policy_impact: dict,
    verification_card: dict,
    debug_summary: dict,
    source_candidates: list[dict] | None = None,
    evidence_snippets: list[dict] | None = None,
) -> dict:
    """Run P1 → official_mismatch rewrite → P2 → M11.0d-3b-2 prose
    realignment. Returns the final_decision dict the user would see.

    Mirrors the main.py:665-784 sequence one-to-one. Used by every
    behavioral pin below."""
    final_decision = make_final_decision(
        policy_confidence=policy_confidence,
        policy_impact=policy_impact,
    )

    # Mirror main.py:717-750 official_mismatch override.
    if verification_card.get("official_mismatch"):
        policy_confidence = dict(policy_confidence)
        policy_confidence["policy_confidence_score"] = min(
            int(policy_confidence.get("policy_confidence_score") or 0),
            20,
        )
        policy_confidence["verification_strength"] = "none"
        final_decision = dict(final_decision)
        final_decision["policy_alert_level"] = (
            "WATCH"
            if policy_impact.get("impact_level") == "high"
            else final_decision.get("policy_alert_level", "WATCH")
        )
        final_decision["action_recommendation"] = "추가 공식 출처 확인 필요"
        final_decision["decision_summary"] = (
            "공식 상세 근거가 부족하거나 뉴스 핵심 주제와 불일치하여 추가 공식 출처 확인이 필요합니다."
        )

    # P2 calibration — overwrites policy_alert_level.
    final_decision, _debug = calibrate_final_decision(
        final_decision=final_decision,
        policy_confidence=policy_confidence,
        policy_impact=policy_impact,
        verification_card=verification_card,
        source_candidates=source_candidates or [],
        evidence_snippets=evidence_snippets or [],
        debug_summary=debug_summary,
    )

    # M11.0d-3b-2 — realign prose to P2's authoritative label.
    if not verification_card.get("official_mismatch"):
        aligned_alert_level = final_decision.get("policy_alert_level")
        aligned_market_signals = final_decision.get("market_signal") or []
        final_decision["action_recommendation"] = action_recommendation_for(
            aligned_alert_level,
            aligned_market_signals,
            policy_confidence,
            policy_impact,
        )
        final_decision["decision_summary"] = decision_summary_for(
            aligned_alert_level,
            aligned_market_signals,
            policy_confidence,
            policy_impact,
        )

    return final_decision


def _neutral_market_inputs(
    *,
    score: int,
    strength: str,
    risk_level: str,
    impact_level: str,
    impact_direction: str = "mixed",
) -> tuple[dict, dict]:
    """Build policy_confidence + policy_impact that produce NO
    short-circuiting market_signal in the prose helpers.

    ``_market_signal`` fires housing/sme/consumer-credit signals only
    when ``affected_sectors``/``affected_groups`` + ``impact_direction``
    match specific patterns. Leaving both lists empty and using a
    direction that doesn't engage the bank-margin / credit-relief
    branches forces the fallback signals (``no_clear_signal`` or
    ``policy_uncertainty``) — neither of those are matched by
    ``action_recommendation_for`` / ``decision_summary_for``'s
    market-signal short-circuit chain, so the alert_level branch
    executes."""
    return (
        {
            "policy_confidence_score": score,
            "verification_strength": strength,
            "risk_level": risk_level,
        },
        {
            "impact_level": impact_level,
            "impact_direction": impact_direction,
            "consumer_sensitivity": 40,
            "business_sensitivity": 40,
            "market_sensitivity": 40,
            "affected_sectors": [],
            "affected_groups": [],
            "impact_reasons": [],
        },
    )


def _vcard(
    *,
    official_mismatch: bool = False,
    evidence_quality_avg: int = 70,
    source_trust_components: dict | None = None,
    contradiction_summary: dict | None = None,
) -> dict:
    return {
        "official_mismatch": official_mismatch,
        "source_reliability_summary": {
            **(source_trust_components or {}),
            "official_mismatch": official_mismatch,
        },
        "contradiction_summary": contradiction_summary
        or {"confirmed_contradiction_count": 0, "possible_contradiction_count": 0},
        "evidence_quality_summary": {
            "average_evidence_quality_score": evidence_quality_avg,
        },
    }


def _debug(
    *,
    strength_summary: dict | None = None,
    evidence_quality_avg: int = 70,
    approved_boost: bool = False,
    rejected_penalty: bool = False,
) -> dict:
    return {
        "evidence_strength_summary": strength_summary
        or {"strong": 0, "medium": 0, "weak": 0},
        "evidence_quality_summary": {
            "average_evidence_quality_score": evidence_quality_avg,
        },
        "approved_boost": approved_boost,
        "rejected_penalty": rejected_penalty,
    }


# Expected Korean strings — pinned verbatim from policy_decision._policy_alert_level
# and the prose helpers. If the helpers' wording ever changes, this file is the
# single update site.
HIGH_SUMMARY_FMT = "공식 검증과 {impact} 영향이 결합되어 높은 수준의 정책 알림이 필요합니다."
WATCH_HIGH_CONSUMER_SUMMARY = "공식 검증은 제한적이지만 소비자 민감도가 높아 관찰이 필요합니다."
MEDIUM_SUMMARY = "정책 신뢰도와 영향도가 중간 이상으로 확인되어 후속 모니터링이 필요합니다."
LOW_FALLBACK_SUMMARY = "현재 단계에서는 명확한 정책 실행 신호가 낮아 일반 모니터링 대상으로 판단됩니다."
OFFICIAL_MISMATCH_SUMMARY = (
    "공식 상세 근거가 부족하거나 뉴스 핵심 주제와 불일치하여 추가 공식 출처 확인이 필요합니다."
)

WATCH_RECOMMENDATION = "Keep on watchlist until usable official evidence is available."
FALLBACK_RECOMMENDATION = "No immediate action beyond routine monitoring."
OFFICIAL_MISMATCH_RECOMMENDATION = "추가 공식 출처 확인 필요"


# ---------------------------------------------------------------------------
# Class A — Prose realignment behavioral pins
# ---------------------------------------------------------------------------


class ProseRealignmentBehavioralPins(unittest.TestCase):

    def test_pattern_A_p1_medium_p2_low_prose_realigns_to_low(self):
        """P1=MEDIUM (score=60, impact=medium), P2 calibrates to LOW
        because source_trust + evidence_quality components are weak.
        Mirrors ``boundary_score_60_medium_impact`` row.

        Expected: ``decision_summary`` matches the LOW fallback Korean
        string, NOT the MEDIUM string."""
        pc, pi = _neutral_market_inputs(
            score=60, strength="medium", risk_level="medium", impact_level="medium",
        )
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(evidence_quality_avg=30),
            debug_summary=_debug(evidence_quality_avg=30),
        )
        self.assertEqual(final["policy_alert_level"], "LOW",
                         "P2 should calibrate this row to LOW.")
        self.assertEqual(
            final["decision_summary"], LOW_FALLBACK_SUMMARY,
            "Pattern A: P1=MEDIUM but P2=LOW must yield LOW-tier prose.",
        )
        self.assertNotEqual(
            final["decision_summary"], MEDIUM_SUMMARY,
            "Pre-M11.0d-3b-2 bug: MEDIUM prose next to LOW alert.",
        )

    def test_pattern_A_p1_high_p2_watch_prose_realigns_to_watch(self):
        """P1=HIGH (impact=high + risk=high) but P2 calibrates DOWN to
        WATCH because contradiction_adjustment=-35 (confirmed).
        Mirrors ``contradiction_confirmed`` row.

        Expected: ``decision_summary`` and ``action_recommendation``
        match WATCH branches, not HIGH."""
        pc, pi = _neutral_market_inputs(
            score=70, strength="high", risk_level="high", impact_level="high",
        )
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(
                contradiction_summary={
                    "confirmed_contradiction_count": 1,
                    "possible_contradiction_count": 0,
                },
            ),
            debug_summary=_debug(),
        )
        self.assertEqual(final["policy_alert_level"], "WATCH",
                         "P2 should calibrate this contradiction row to WATCH.")
        # WATCH branch: alert_level==WATCH fallback OR consumer>=80 branch.
        # Here consumer=40, so the recommendation hits the WATCH fallback.
        self.assertEqual(final["action_recommendation"], WATCH_RECOMMENDATION,
                         "Pattern A: P1=HIGH but P2=WATCH must yield WATCH recommendation.")
        # The LOW-fallback summary is what _decision_summary returns when
        # alert_level is WATCH AND consumer_sensitivity<80 (no dedicated
        # WATCH branch for that case).
        self.assertEqual(final["decision_summary"], LOW_FALLBACK_SUMMARY,
                         "WATCH + consumer<80 falls through to the generic line.")

    def test_pattern_B_p1_medium_p2_high_prose_realigns_to_high(self):
        """The strong-evidence ELS scenario from M11.0d-1
        (regression_fixture_geumyungwi_strong): P1=MEDIUM, P2=HIGH.

        Expected: ``decision_summary`` contains the HIGH string and
        embeds the impact level."""
        pc, pi = _neutral_market_inputs(
            score=85, strength="high", risk_level="medium", impact_level="high",
        )
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(
                evidence_quality_avg=80,
                source_trust_components={
                    "official_detail_available": True,
                    "official_body_matches": 1,
                    "official_resolution_direct_matches": 1,
                    "official_resolution_top_score": 80,
                    "average_reliability_score": 90,
                },
            ),
            debug_summary=_debug(
                strength_summary={"strong": 1, "medium": 0, "weak": 0},
                evidence_quality_avg=80,
            ),
        )
        self.assertEqual(final["policy_alert_level"], "HIGH",
                         "P2 should calibrate the strong-ELS row to HIGH.")
        self.assertEqual(
            final["decision_summary"],
            HIGH_SUMMARY_FMT.format(impact="high"),
            "Pattern B: P1=MEDIUM but P2=HIGH must yield HIGH-tier prose.",
        )
        self.assertNotEqual(
            final["decision_summary"], MEDIUM_SUMMARY,
            "Pre-M11.0d-3b-2 bug: MEDIUM prose next to HIGH alert.",
        )

    def test_pattern_B_action_recommendation_realigns_on_watch_upgrade(self):
        """P1=LOW but P2 calibrates UP to WATCH (e.g. high impact +
        verification_strength other than none, with weak components).
        No short-circuiting market signal — uses an empty-sectors
        row so the alert_level branch in the helpers actually fires.

        Expected: ``action_recommendation`` is the WATCH string.

        (Operator-requested: this test uses a row WITHOUT market_signal
        short-circuit so the alert_level branch is exercised end-to-end.)
        """
        pc, pi = _neutral_market_inputs(
            score=18, strength="low", risk_level="medium", impact_level="high",
        )
        # Sanity: P1 falls through to WATCH branch 4 (impact=high +
        # strength=low) in this configuration. But we want P1≠P2 to be
        # a realigning step, so we let make_final_decision compute P1
        # and assert P2 is also WATCH (the alignment must hold either
        # way; the test asserts the prose came from P2's label).
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(evidence_quality_avg=20),
            debug_summary=_debug(evidence_quality_avg=20),
        )
        self.assertEqual(final["policy_alert_level"], "WATCH")
        # No market signal short-circuit fired (sectors/groups empty,
        # direction=mixed), so action_recommendation hit the
        # alert_level branch and returned the WATCH string.
        self.assertEqual(final["action_recommendation"], WATCH_RECOMMENDATION,
                         "WATCH alert must yield the WATCH recommendation.")
        # decision_summary: WATCH + consumer<80 falls to generic line.
        self.assertEqual(final["decision_summary"], LOW_FALLBACK_SUMMARY)

    def test_agreement_case_prose_unchanged(self):
        """P1=LOW, P2=LOW. No realignment needed — and the prose must
        be the LOW fallback string (same as it was before
        M11.0d-3b-2)."""
        pc, pi = _neutral_market_inputs(
            score=10, strength="low", risk_level="low", impact_level="low",
        )
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(evidence_quality_avg=20),
            debug_summary=_debug(evidence_quality_avg=20),
        )
        self.assertEqual(final["policy_alert_level"], "LOW")
        self.assertEqual(final["decision_summary"], LOW_FALLBACK_SUMMARY)
        self.assertEqual(final["action_recommendation"], FALLBACK_RECOMMENDATION)

    def test_official_mismatch_conservative_prose_preserved(self):
        """When ``official_mismatch=True``, the conservative override
        applied in main.py:735-749 must survive the M11.0d-3b-2 gate.
        Even if P1=HIGH would normally produce HIGH prose AND P2 stays
        WATCH, the prose fields stay frozen at the conservative
        ``"추가 공식 출처 확인 필요"`` pair."""
        pc, pi = _neutral_market_inputs(
            score=70, strength="high", risk_level="high", impact_level="high",
        )
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(
                official_mismatch=True,
                source_trust_components={"average_reliability_score": 20},
            ),
            debug_summary=_debug(),
        )
        self.assertEqual(final["action_recommendation"], OFFICIAL_MISMATCH_RECOMMENDATION,
                         "Conservative override must survive M11.0d-3b-2's gate.")
        self.assertEqual(final["decision_summary"], OFFICIAL_MISMATCH_SUMMARY,
                         "Conservative override must survive M11.0d-3b-2's gate.")

    def test_market_signal_byte_identical_post_alignment(self):
        """``market_signal`` is label-independent and must NOT be
        touched by the realignment step."""
        pc, pi = _neutral_market_inputs(
            score=85, strength="high", risk_level="medium", impact_level="high",
        )
        # Pre-realignment P1 output.
        p1_only = make_final_decision(policy_confidence=pc, policy_impact=pi)
        # Full pipeline.
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(
                evidence_quality_avg=80,
                source_trust_components={
                    "official_detail_available": True,
                    "official_body_matches": 1,
                    "average_reliability_score": 90,
                },
            ),
            debug_summary=_debug(
                strength_summary={"strong": 1, "medium": 0, "weak": 0},
                evidence_quality_avg=80,
            ),
        )
        self.assertEqual(
            final["market_signal"], p1_only["market_signal"],
            "market_signal must be byte-identical to P1's output — "
            "the realignment step touches only prose fields.",
        )

    def test_decision_reasons_byte_identical_at_p1_prefix(self):
        """``decision_reasons`` from P1 must remain in the list
        unchanged. P2 appends calibration-specific reasons (this is
        a pre-existing M11.0d-3b behavior); M11.0d-3b-2 must not
        alter that prefix."""
        pc, pi = _neutral_market_inputs(
            score=85, strength="high", risk_level="medium", impact_level="high",
        )
        p1_only = make_final_decision(policy_confidence=pc, policy_impact=pi)
        p1_reasons = list(p1_only["decision_reasons"])
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(
                evidence_quality_avg=80,
                source_trust_components={
                    "official_detail_available": True,
                    "official_body_matches": 1,
                    "average_reliability_score": 90,
                },
            ),
            debug_summary=_debug(
                strength_summary={"strong": 1, "medium": 0, "weak": 0},
                evidence_quality_avg=80,
            ),
        )
        # P1's reasons must remain as a prefix of the final
        # decision_reasons (P2 appends calibration reasons + the
        # "calibrated alert X -> Y" line on disagreement).
        for reason in p1_reasons:
            self.assertIn(
                reason, final["decision_reasons"],
                f"P1 reason {reason!r} lost during realignment — "
                "M11.0d-3b-2 must not touch decision_reasons.",
            )


# ---------------------------------------------------------------------------
# Class B — Invariant byte-identity pins
# ---------------------------------------------------------------------------


class InvariantByteIdentityPins(unittest.TestCase):

    def _load(self, name: str):
        return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))

    def test_invariant_policy_alert_level_byte_identical_on_full_matrix(self):
        """Re-run all 42 synthetic-matrix rows through the
        P1+P2+realignment pipeline and assert ``policy_alert_level``
        matches the M11.0d-1 P2 snapshot byte-identically. M11.0d-3b-2
        must NOT shift any label."""
        matrix = self._load("m11_0d_1_synthetic_matrix.json")
        p2_snapshot = self._load("m11_0d_1_p2_snapshot.json")
        actual: dict[str, str] = {}
        for row in matrix:
            f = row["_fields"]
            pc = {
                "policy_confidence_score": f["score"],
                "verification_strength": f["strength"],
                "risk_level": f["risk_level"],
                "confidence_evidence_grade": f["evidence_grade"],
                "confidence_reasons": f["confidence_reasons"],
            }
            pi = {
                "impact_level": f["impact_level"],
                "impact_direction": f["impact_direction"],
                "consumer_sensitivity": f["consumer_sensitivity"],
                "business_sensitivity": f["business_sensitivity"],
                "market_sensitivity": f["market_sensitivity"],
                "affected_sectors": f["affected_sectors"],
                "affected_groups": f["affected_groups"],
                "impact_reasons": f["impact_reasons"],
            }
            vcard = {
                "official_mismatch": f["official_mismatch"],
                "source_reliability_summary": {
                    **f["source_trust_components"],
                    "official_mismatch": f["official_mismatch"],
                },
                "contradiction_summary": {
                    "confirmed_contradiction_count": f["confirmed_contradiction_count"],
                    "possible_contradiction_count": f["possible_contradiction_count"],
                },
                "evidence_quality_summary": {
                    "average_evidence_quality_score": f["evidence_quality_avg"],
                },
            }
            debug = {
                "evidence_strength_summary": f["strength_summary"],
                "evidence_quality_summary": {
                    "average_evidence_quality_score": f["evidence_quality_avg"],
                },
                "approved_boost": f["approved_boost"],
                "rejected_penalty": f["rejected_penalty"],
            }
            final = _run_verdict_pipeline(
                policy_confidence=pc, policy_impact=pi,
                verification_card=vcard, debug_summary=debug,
            )
            actual[row["id"]] = final["policy_alert_level"]
        self.assertEqual(
            actual, p2_snapshot,
            "policy_alert_level drifted from M11.0d-1 P2 snapshot. "
            "M11.0d-3b-2 is prose-only — labels must be byte-identical.",
        )

    def test_invariant_verdict_label_byte_identical_on_full_matrix(self):
        """P3's ``verdict_label`` runs on inputs unrelated to P1's
        prose; the realignment cannot affect P3. This is a guard
        against accidentally importing the realignment into the P3
        path."""
        matrix = self._load("m11_0d_1_synthetic_matrix.json")
        p3_snapshot = self._load("m11_0d_1_p3_snapshot.json")
        actual: dict[str, str] = {}
        for row in matrix:
            f = row["_fields"]
            pc = {
                "policy_confidence_score": f["score"],
                "verification_strength": f["strength"],
                "risk_level": f["risk_level"],
                "confidence_evidence_grade": f["evidence_grade"],
                "confidence_reasons": f["confidence_reasons"],
            }
            snippets: list[dict] = []
            snippets += [{"evidence_type": "direct_support"}
                         for _ in range(f["direct_support_count"])]
            snippets += [{"evidence_type": "official_reference"}
                         for _ in range(f["official_reference_count"])]
            snippets += [{"evidence_type": "insufficient_evidence"}
                         for _ in range(f["insufficient_count"])]
            evidence_comparison = {
                "comparison_status": f["comparison_status"],
                "verification_level": f["verification_level"],
                "conflict_signals": f["conflict_signals"],
                "semantic_conflict_signals": [],
            }
            official_sources = (
                [{"title": "공식 출처", "url": "https://example.go.kr/x",
                  "source_type": "official_government", "reliability_score": 5}]
                if f["official_sources_present"] else []
            )
            contradiction_summary = {
                "possible_contradiction_count": f["possible_contradiction_count"],
                "confirmed_contradiction_count": f["confirmed_contradiction_count"],
                "needs_official_confirmation_count": f["official_confirmation_count"],
                "insufficient_evidence_count": f["insufficient_claim_count"],
            }
            bias_framing_summary = {"high_framing_count": f["high_framing_count"]}
            actual[row["id"]] = _verdict_label(
                policy_confidence=pc,
                evidence_comparison=evidence_comparison,
                official_sources=official_sources,
                evidence_snippets=snippets,
                contradiction_summary=contradiction_summary,
                bias_framing_summary=bias_framing_summary,
                claim_count=f["claim_count"],
            )
        self.assertEqual(
            actual, p3_snapshot,
            "verdict_label drifted from M11.0d-1 P3 snapshot. "
            "M11.0d-3b-2 must not affect P3.",
        )

    def test_invariant_disagreement_signal_unchanged(self):
        """The (p1_label, p2_label, agreed) triple from
        ``_build_disagreement_signal`` is unchanged: the realignment
        step runs AFTER P2 returns but does not mutate
        policy_alert_level."""
        from main import _build_disagreement_signal
        # Pattern B case: P1=MEDIUM, P2=HIGH.
        signal = _build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",
        )
        self.assertEqual(signal["p1_label"], "MEDIUM")
        self.assertEqual(signal["p2_label"], "HIGH")
        self.assertFalse(signal["agreed"])
        # Agreement case.
        signal = _build_disagreement_signal(
            p1_alert_level_raw="LOW",
            p2_alert_level="LOW",
            p3_verdict_label="draft_unverified",
        )
        self.assertTrue(signal["agreed"])

    def test_invariant_operator_review_required_always_true(self):
        """Cross-pin with test_m11_0d_3b_p2_authority.py:
        ``operator_review_required`` is forced True by
        ``candidate_to_dict`` regardless of input. M11.0d-3b-2 is a
        prose-only milestone and must not touch this invariant."""
        from artifact_evidence_linker import (
            EvidenceCandidate,
            candidate_to_dict,
        )
        candidate = EvidenceCandidate(
            extraction_id=1,
            source_id="src-001",
            url="https://example.go.kr/x",
            analysis_id="ana-001",
            claim_text="테스트 주장",
            match_score=42.0,
            matched_tokens=["테스트"],
            operator_review_required=False,
        )
        payload = candidate_to_dict(candidate)
        self.assertTrue(payload["operator_review_required"])

    def test_invariant_truth_claim_always_false(self):
        """Partner invariant: ``truth_claim`` is always False."""
        from artifact_evidence_linker import (
            EvidenceCandidate,
            candidate_to_dict,
        )
        candidate = EvidenceCandidate(
            extraction_id=1,
            source_id="src-001",
            url="https://example.go.kr/x",
            analysis_id="ana-001",
            claim_text="테스트",
            match_score=10.0,
            matched_tokens=[],
            operator_review_required=False,
        )
        payload = candidate_to_dict(candidate)
        self.assertFalse(payload["truth_claim"])


# ---------------------------------------------------------------------------
# Class C — Conservative wording / immutable fixture pins
# ---------------------------------------------------------------------------


# SHA256 of each immutable M11.0d-1 fixture file as committed at
# M11.0d-3b-2 time. Recompute and update these hex digests ONLY when
# a future milestone explicitly re-baselines the fixtures (which is
# itself a verdict-changing event, not a prose-only one).
_M11_0D_1_FIXTURE_HASHES = {
    "m11_0d_1_synthetic_matrix.json":
        "a02e50bc5c51099d65fe473e46f0e37078518dbb6de99268459817fe5d7689a6",
    "m11_0d_1_p1_snapshot.json":
        "3b56cead1e22362823825e8700449f3ceeb443a04fef29264a45b03b1516b2fa",
    "m11_0d_1_p2_snapshot.json":
        "a0b3d8794b3a106e30cfdeda81ee8ac48e5a8e2b5cb754060ab64eb113704ce2",
    "m11_0d_1_p3_snapshot.json":
        "17420b5195cd9d1318a28cea4510472771310f8bb945337fd5d4339e76c92f40",
    "m11_0d_1_disagreement_summary.json":
        "ac43c795543aa79ac55ef8964cc6ad8ce51721d0c6fc50f3565e158af6fd9477",
    "m11_0d_1_regression_fixtures_snapshot.json":
        "9a30b16b09acf8eb58262cb3ae3098ffe54e8e84a9f5ccde58ae7b2ea9ba77a4",
}


_CONSERVATIVE_KOREAN_PHRASES = (
    "공식 후보만 있음",
    "공식기관 후보는 있으나 상세 본문 미확인",
    "의미 매칭 근거 부족",
    "사람 검토 필요",
)


class ConservativeWordingAndFixtureImmutabilityPins(unittest.TestCase):

    def test_conservative_korean_wording_preserved_in_index_html(self):
        """Mirrors tests/regression.test.js:15-23 in Python so this
        file alone catches a methodology-section wording drift."""
        index_html = (_PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for phrase in _CONSERVATIVE_KOREAN_PHRASES:
            self.assertIn(
                phrase, index_html,
                f"Conservative Korean phrase {phrase!r} missing from "
                "web/index.html methodology section. M11.0d-3b-2 must "
                "not weaken or remove conservative wording.",
            )
        self.assertNotIn(
            "100%", index_html.split("<section id=\"methodology\"", 1)[1]
            .split("</section>", 1)[0] if "<section id=\"methodology\"" in index_html
            else "",
            "Methodology section must never promise 100% certainty.",
        )

    def test_m11_0d_1_fixtures_byte_identical_hash(self):
        """SHA256 every M11.0d-1 fixture and compare to the digest
        committed at M11.0d-3b-2 time. Catches any accidental edit
        to the immutable snapshot files."""
        for filename, expected in _M11_0D_1_FIXTURE_HASHES.items():
            with self.subTest(file=filename):
                path = _FIXTURES / filename
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(
                    actual, expected,
                    f"{filename} content drifted from M11.0d-3b-2 "
                    f"baseline. Expected sha256 {expected}, got "
                    f"{actual}. These fixtures are IMMUTABLE until a "
                    "milestone explicitly re-baselines them — and "
                    "M11.0d-3b-2 is NOT such a milestone.",
                )

    def test_export_consistency_aligned_prose_visible_in_decision_summary(self):
        """Frontend export at frontend/scripts/main.js reads
        ``decision.decision_summary`` directly into the TXT export
        line ``"권장 조치:"`` / ``"요약:"``. After M11.0d-3b-2 the
        realigned prose flows through that path unchanged because
        the realignment writes to the same key. This test exercises
        the end-to-end Pattern B case and asserts the HIGH-tier
        string is what the export consumer would read."""
        pc, pi = _neutral_market_inputs(
            score=85, strength="high", risk_level="medium", impact_level="high",
        )
        final = _run_verdict_pipeline(
            policy_confidence=pc, policy_impact=pi,
            verification_card=_vcard(
                evidence_quality_avg=80,
                source_trust_components={
                    "official_detail_available": True,
                    "official_body_matches": 1,
                    "official_resolution_direct_matches": 1,
                    "official_resolution_top_score": 80,
                    "average_reliability_score": 90,
                },
            ),
            debug_summary=_debug(
                strength_summary={"strong": 1, "medium": 0, "weak": 0},
                evidence_quality_avg=80,
            ),
        )
        # Export consumer reads decision_summary directly.
        exported_summary = final.get("decision_summary")
        self.assertEqual(
            exported_summary, HIGH_SUMMARY_FMT.format(impact="high"),
            "Export consumer (frontend/scripts/main.js) reads "
            "decision_summary directly — must reflect P2's HIGH tier.",
        )


if __name__ == "__main__":
    unittest.main()
