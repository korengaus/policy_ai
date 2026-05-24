"""M11.0d-1 — Verdict producer disagreement diagnostic (snapshot pin).

Re-runs the M11.0d-1 synthetic input matrix through all three verdict
producers on every CI run and asserts each producer's output matches
the committed snapshot. Also re-runs the three named regression
fixtures (금융위 strong, 금융위 weak, 전세사기) and asserts each
producer's label matches.

The point of this test is **not** to validate that the producers
agree — they don't, by construction (see
docs/VERDICT_PRODUCER_DISAGREEMENT_MAP.md). The point is to PIN the
current disagreement state so any unintended convergence or drift
during future cleanups (M11.0d-3 in particular) is caught immediately.

Snapshot files (immutable until M11.0d-3 re-baselines them):

    tests/fixtures/m11_0d_1_synthetic_matrix.json        — input matrix
    tests/fixtures/m11_0d_1_p1_snapshot.json             — P1 labels per row
    tests/fixtures/m11_0d_1_p2_snapshot.json             — P2 labels per row
    tests/fixtures/m11_0d_1_p3_snapshot.json             — P3 labels per row
    tests/fixtures/m11_0d_1_disagreement_summary.json    — agreement counts
    tests/fixtures/m11_0d_1_regression_fixtures_snapshot.json — named fixtures

Updating these files is M11.0d-3's job, not M11.0d-1's. If a test
in this file fails, the operator must verify the change is intentional
(i.e. they are actually consolidating producers) before regenerating
the snapshots.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


from policy_decision import make_final_decision  # noqa: E402
from policy_scoring import calibrate_final_decision  # noqa: E402
from verification_card import _verdict_label  # noqa: E402


_FIXTURES = _PROJECT_ROOT / "tests" / "fixtures"
_MATRIX_PATH = _FIXTURES / "m11_0d_1_synthetic_matrix.json"
_P1_SNAPSHOT_PATH = _FIXTURES / "m11_0d_1_p1_snapshot.json"
_P2_SNAPSHOT_PATH = _FIXTURES / "m11_0d_1_p2_snapshot.json"
_P3_SNAPSHOT_PATH = _FIXTURES / "m11_0d_1_p3_snapshot.json"
_SUMMARY_PATH = _FIXTURES / "m11_0d_1_disagreement_summary.json"
_REGRESSION_PATH = _FIXTURES / "m11_0d_1_regression_fixtures_snapshot.json"


# ---------------------------------------------------------------------------
# Producer wiring — identical to _m11_0d_1_generate_snapshots.py so the
# snapshot regeneration is reproducible.
# ---------------------------------------------------------------------------


def _p1_inputs(row: dict) -> tuple[dict, dict]:
    f = row["_fields"]
    policy_confidence = {
        "policy_confidence_score": f["score"],
        "verification_strength": f["strength"],
        "risk_level": f["risk_level"],
        "confidence_evidence_grade": f["evidence_grade"],
        "confidence_reasons": f["confidence_reasons"],
    }
    policy_impact = {
        "impact_level": f["impact_level"],
        "impact_direction": f["impact_direction"],
        "consumer_sensitivity": f["consumer_sensitivity"],
        "business_sensitivity": f["business_sensitivity"],
        "market_sensitivity": f["market_sensitivity"],
        "affected_sectors": f["affected_sectors"],
        "affected_groups": f["affected_groups"],
        "impact_reasons": f["impact_reasons"],
    }
    return policy_confidence, policy_impact


def _p3_inputs(row: dict) -> dict:
    f = row["_fields"]
    pc, _ = _p1_inputs(row)
    snippets: list[dict] = []
    snippets += [{"evidence_type": "direct_support"} for _ in range(f["direct_support_count"])]
    snippets += [{"evidence_type": "official_reference"} for _ in range(f["official_reference_count"])]
    snippets += [{"evidence_type": "insufficient_evidence"} for _ in range(f["insufficient_count"])]
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
    return {
        "policy_confidence": pc,
        "evidence_comparison": evidence_comparison,
        "official_sources": official_sources,
        "evidence_snippets": snippets,
        "contradiction_summary": contradiction_summary,
        "bias_framing_summary": bias_framing_summary,
        "claim_count": f["claim_count"],
    }


def _p2_inputs(row: dict) -> dict:
    f = row["_fields"]
    pc, pi = _p1_inputs(row)
    final_decision = make_final_decision(pc, pi)
    verification_card = {
        "official_mismatch": f["official_mismatch"],
        "source_reliability_summary": {**f["source_trust_components"],
                                        "official_mismatch": f["official_mismatch"]},
        "contradiction_summary": {
            "confirmed_contradiction_count": f["confirmed_contradiction_count"],
            "possible_contradiction_count": f["possible_contradiction_count"],
        },
        "evidence_quality_summary": {
            "average_evidence_quality_score": f["evidence_quality_avg"],
        },
    }
    debug_summary = {
        "evidence_strength_summary": f["strength_summary"],
        "evidence_quality_summary": {
            "average_evidence_quality_score": f["evidence_quality_avg"],
        },
        "approved_boost": f["approved_boost"],
        "rejected_penalty": f["rejected_penalty"],
    }
    return {
        "final_decision": final_decision,
        "policy_confidence": pc,
        "policy_impact": pi,
        "verification_card": verification_card,
        "source_candidates": [],
        "evidence_snippets": _p3_inputs(row)["evidence_snippets"],
        "debug_summary": debug_summary,
    }


def _run_p1(row: dict) -> str:
    pc, pi = _p1_inputs(row)
    return make_final_decision(pc, pi)["policy_alert_level"]


def _run_p2(row: dict) -> str:
    args = _p2_inputs(row)
    calibrated, _ = calibrate_final_decision(**args)
    return calibrated["policy_alert_level"]


def _run_p3(row: dict) -> str:
    return _verdict_label(**_p3_inputs(row))


def _p3_implied_alert_tier(p3_label: str) -> str:
    if p3_label == "draft_verified":
        return "HIGH"
    if p3_label == "draft_likely_true":
        return "MEDIUM"
    if p3_label in ("draft_disputed", "draft_high_risk_review",
                    "draft_needs_review", "draft_needs_official_confirmation",
                    "draft_needs_context"):
        return "WATCH"
    if p3_label == "draft_unverified":
        return "LOW"
    return "UNKNOWN"


def _load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class SyntheticMatrixIntegrityTests(unittest.TestCase):
    """Sanity-check the snapshot files exist and have the expected
    cardinality before downstream pins consume them."""

    def test_synthetic_matrix_exists_and_has_expected_cardinality(self):
        self.assertTrue(_MATRIX_PATH.exists(),
                        f"matrix snapshot missing: {_MATRIX_PATH}")
        matrix = _load_json(_MATRIX_PATH)
        self.assertIsInstance(matrix, list)
        self.assertEqual(
            len(matrix), 42,
            "M11.0d-1 fixed the synthetic matrix at 42 rows. Drift here "
            "means the matrix was regenerated; if intentional, regenerate "
            "all snapshots together and bump M11.0d-* milestone.",
        )
        # Every row has the structural fields the producer wiring expects.
        for row in matrix:
            self.assertIn("id", row)
            self.assertIn("family", row)
            self.assertIn("_fields", row)
            for required_field in (
                "score", "strength", "risk_level", "impact_level",
                "comparison_status", "verification_level",
                "official_mismatch", "claim_count",
            ):
                self.assertIn(
                    required_field, row["_fields"],
                    f"row {row['id']!r} missing required field "
                    f"{required_field!r} in _fields.",
                )


class Producer1SnapshotTests(unittest.TestCase):
    """Producer 1 = ``policy_decision.make_final_decision``.
    Output vocabulary: {HIGH, MEDIUM, WATCH, LOW}."""

    def test_producer_1_output_snapshot_unchanged(self):
        matrix = _load_json(_MATRIX_PATH)
        snapshot = _load_json(_P1_SNAPSHOT_PATH)
        actual = {row["id"]: _run_p1(row) for row in matrix}
        self.assertEqual(
            actual, snapshot,
            "P1 (make_final_decision) output drifted from the M11.0d-1 "
            "snapshot. If intentional (M11.0d-3 consolidation), "
            "regenerate all snapshots together. If unintentional, "
            "revert the producer change.",
        )

    def test_producer_1_only_emits_documented_vocabulary(self):
        snapshot = _load_json(_P1_SNAPSHOT_PATH)
        allowed = {"HIGH", "MEDIUM", "WATCH", "LOW"}
        for rid, label in snapshot.items():
            self.assertIn(
                label, allowed,
                f"P1 emitted {label!r} on row {rid!r} — not in the "
                f"documented vocabulary {sorted(allowed)}.",
            )


class Producer2SnapshotTests(unittest.TestCase):
    """Producer 2 = ``policy_scoring.calibrate_final_decision``.
    Output vocabulary: {HIGH, WATCH, LOW} — NOTE: never MEDIUM."""

    def test_producer_2_output_snapshot_unchanged(self):
        matrix = _load_json(_MATRIX_PATH)
        snapshot = _load_json(_P2_SNAPSHOT_PATH)
        actual = {row["id"]: _run_p2(row) for row in matrix}
        self.assertEqual(
            actual, snapshot,
            "P2 (calibrate_final_decision) output drifted from the "
            "M11.0d-1 snapshot. If intentional (M11.0d-3 consolidation), "
            "regenerate all snapshots together. If unintentional, "
            "revert the producer change.",
        )

    def test_producer_2_only_emits_documented_vocabulary(self):
        snapshot = _load_json(_P2_SNAPSHOT_PATH)
        allowed = {"HIGH", "WATCH", "LOW"}
        for rid, label in snapshot.items():
            self.assertIn(
                label, allowed,
                f"P2 emitted {label!r} on row {rid!r} — not in the "
                f"documented vocabulary {sorted(allowed)}. The audit "
                "specifically pins that P2 never emits MEDIUM; if this "
                "trips, a producer change shifted P2's label set.",
            )


class Producer3SnapshotTests(unittest.TestCase):
    """Producer 3 = ``verification_card._verdict_label``.
    Output vocabulary: draft_* labels, disjoint from P1/P2."""

    def test_producer_3_output_snapshot_unchanged(self):
        matrix = _load_json(_MATRIX_PATH)
        snapshot = _load_json(_P3_SNAPSHOT_PATH)
        actual = {row["id"]: _run_p3(row) for row in matrix}
        self.assertEqual(
            actual, snapshot,
            "P3 (_verdict_label) output drifted from the M11.0d-1 "
            "snapshot. If intentional (M11.0d-3 consolidation), "
            "regenerate all snapshots together. If unintentional, "
            "revert the producer change.",
        )

    def test_producer_3_only_emits_documented_vocabulary(self):
        snapshot = _load_json(_P3_SNAPSHOT_PATH)
        allowed = {
            "draft_disputed", "draft_high_risk_review", "draft_needs_review",
            "draft_needs_official_confirmation", "draft_needs_context",
            "draft_verified", "draft_likely_true", "draft_unverified",
        }
        for rid, label in snapshot.items():
            self.assertIn(
                label, allowed,
                f"P3 emitted {label!r} on row {rid!r} — not in the "
                f"documented vocabulary. A new label would mean a "
                "producer change that needs the audit doc updated.",
            )


class DisagreementCountTests(unittest.TestCase):
    """The whole point of M11.0d-1: pin the CURRENT disagreement count
    so a future producer change is visible as a count drift, not just
    individual snapshot drift."""

    def test_disagreement_count_unchanged(self):
        matrix = _load_json(_MATRIX_PATH)
        summary = _load_json(_SUMMARY_PATH)
        # Recompute counts from scratch.
        all_agree_strict = 0
        all_agree_normalized = 0
        p1_p2 = p1_p3 = p2_p3 = 0
        all_disagree = 0
        for row in matrix:
            p1 = _run_p1(row)
            p2 = _run_p2(row)
            p3 = _run_p3(row)
            p3_tier = _p3_implied_alert_tier(p3)
            a12 = p1 == p2
            if a12 and p1 == p3 and p2 == p3:
                all_agree_strict += 1
            if a12 and p1 == p3_tier and p2 == p3_tier:
                all_agree_normalized += 1
            if a12:
                p1_p2 += 1
            if p1 == p3_tier:
                p1_p3 += 1
            if p2 == p3_tier:
                p2_p3 += 1
            if not a12 and p1 != p3_tier and p2 != p3_tier:
                all_disagree += 1
        for key, observed in (
            ("all_agree_strict_count", all_agree_strict),
            ("all_agree_normalized_count", all_agree_normalized),
            ("p1_p2_agree_count", p1_p2),
            ("p1_p3_agree_normalized_count", p1_p3),
            ("p2_p3_agree_normalized_count", p2_p3),
            ("all_three_disagree_normalized_count", all_disagree),
        ):
            self.assertEqual(
                observed, summary[key],
                f"Disagreement count {key!r} drifted: snapshot says "
                f"{summary[key]}, recomputed {observed}. M11.0d-1 PINS "
                "the disagreement; any drift means a producer changed.",
            )


class RegressionFixtureProducerSnapshotTests(unittest.TestCase):
    """The named regression fixtures (금융위 strong, 금융위 weak, 전세사기)
    have per-producer label snapshots. These are the rows the operator
    will recognize from regression.test.js / Render production runs."""

    def test_regression_fixture_outputs_unchanged(self):
        snapshot = _load_json(_REGRESSION_PATH)
        # We rebuild the same three rows here so the test is fully
        # self-contained; the generator script wrote them once.
        def _build(id_, score, strength, risk_level, impact_level,
                    evidence_grade, comparison_status, verification_level,
                    official_sources_present=False, official_mismatch=False,
                    direct_support_count=0, claim_count=1,
                    evidence_quality_avg=45, source_trust_components=None,
                    strength_summary=None):
            return {
                "id": id_,
                "_fields": {
                    "score": score, "strength": strength,
                    "risk_level": risk_level, "evidence_grade": evidence_grade,
                    "confidence_reasons": [],
                    "impact_level": impact_level,
                    "impact_direction": "uncertain",
                    "consumer_sensitivity": 40, "business_sensitivity": 40,
                    "market_sensitivity": 40,
                    "affected_sectors": [], "affected_groups": [],
                    "impact_reasons": [],
                    "comparison_status": comparison_status,
                    "verification_level": verification_level,
                    "conflict_signals": [],
                    "direct_support_count": direct_support_count,
                    "official_reference_count": 0,
                    "insufficient_count": 0,
                    "claim_count": claim_count,
                    "official_sources_present": official_sources_present,
                    "possible_contradiction_count": 0,
                    "confirmed_contradiction_count": 0,
                    "high_framing_count": 0,
                    "official_confirmation_count": 0,
                    "insufficient_claim_count": 0,
                    "evidence_quality_avg": evidence_quality_avg,
                    "source_trust_components": source_trust_components or {},
                    "strength_summary": strength_summary or {"strong": 0, "medium": 0, "weak": 0},
                    "official_mismatch": official_mismatch,
                    "approved_boost": False,
                    "rejected_penalty": False,
                },
            }

        rows = [
            _build(
                "regression_fixture_geumyungwi_strong",
                score=85, strength="high", risk_level="medium", impact_level="high",
                evidence_grade="A", direct_support_count=1, claim_count=1,
                comparison_status="official_support_found",
                verification_level="strong_official_match",
                official_sources_present=True,
                evidence_quality_avg=80,
                source_trust_components={
                    "official_detail_available": True,
                    "official_body_matches": 1,
                    "official_resolution_direct_matches": 1,
                    "official_resolution_top_score": 80,
                    "average_reliability_score": 90,
                },
                strength_summary={"strong": 1, "medium": 0, "weak": 0},
            ),
            _build(
                "regression_fixture_geumyungwi_weak",
                score=18, strength="none", risk_level="medium", impact_level="medium",
                evidence_grade=None,
                comparison_status="official_evidence_missing",
                verification_level="excluded_non_policy_page",
                official_mismatch=True,
                source_trust_components={
                    "official_mismatch": True,
                    "average_reliability_score": 20,
                },
            ),
            _build(
                "regression_fixture_jeonse_fraud",
                score=12, strength="none", risk_level="high", impact_level="high",
                evidence_grade=None,
                comparison_status="official_evidence_missing",
                verification_level="official_document_not_found",
                official_mismatch=True,
                source_trust_components={
                    "official_mismatch": True,
                    "average_reliability_score": 15,
                },
            ),
        ]
        actual = []
        for row in rows:
            p1 = _run_p1(row)
            p2 = _run_p2(row)
            p3 = _run_p3(row)
            actual.append({
                "id": row["id"],
                "p1": p1, "p2": p2, "p3": p3,
                "p3_implied_tier": _p3_implied_alert_tier(p3),
            })

        # Snapshot has extra "description" field; compare on the
        # producer-output keys only.
        snapshot_subset = [
            {k: row[k] for k in ("id", "p1", "p2", "p3", "p3_implied_tier")}
            for row in snapshot
        ]
        self.assertEqual(
            actual, snapshot_subset,
            "Named regression fixture producer outputs drifted. These "
            "are the rows the operator recognizes from regression.test.js "
            "and from Render production runs — if these shift, the "
            "Render verdict-label behavior is changing.",
        )


if __name__ == "__main__":
    unittest.main()
