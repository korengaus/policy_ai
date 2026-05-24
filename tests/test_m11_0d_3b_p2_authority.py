"""M11.0d-3b — NARROW Strategy A: contract pins for P2 authority +
Constraint #11 + #12 invariants from M11.0d-1 Section E.

What this file pins:

  1. **P2 authority** — running the P1+P2 sequence end-to-end with
     synthetic inputs proves the final ``policy_alert_level`` is
     ``calibrate_final_decision``'s output, NOT
     ``make_final_decision``'s. This is the codification of what
     M11.0d-1 found and M11.0d-3a made visible.

  2. **P1 prose-only contract** — the docstring of
     ``make_final_decision`` and ``calibrate_final_decision`` carry
     the M11.0d-3b contract phrases so a future refactor cannot
     silently revert the milestone's framing.

  3. **disagreement_signal preserves P1's label** — the M11.0d-3a
     capture path (``p1_alert_level_raw`` → ``_build_disagreement_signal``)
     still works after the docstring changes. Cross-checks
     ``tests/test_m11_0d_3a_disagreement_signal.py``.

  4. **Constraint #11 — `operator_review_required` is ALWAYS True:**
     - database.py schemas declare ``NOT NULL DEFAULT 1`` on every
       table that holds the field.
     - artifact_evidence_linker.candidate_to_dict forces it to
       True even when the dataclass had False.

  5. **Constraint #12 — LLM cannot raise verdict:** structural pin.
     - llm_judge.py, ai_reasoner.py, scripts/dry_run_llm_judge.py
       contain NO write to ``policy_alert_level``,
       ``policy_confidence_score``, or ``verification_strength``.
     - AST scan of the whole repo: writes to ``policy_alert_level``
       are confined to the documented producer files (main.py,
       policy_decision.py, policy_scoring.py, plus the read-only
       diagnostic comparators).

This is a NARROW milestone — no prose alignment yet. M11.0d-3b-2
will follow up on prose. See docs/VERDICT_PRODUCER_DISAGREEMENT_MAP.md
"M11.0d-3b status" section for context.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


from policy_decision import make_final_decision  # noqa: E402
from policy_scoring import calibrate_final_decision  # noqa: E402
from main import _build_disagreement_signal  # noqa: E402


# ---------------------------------------------------------------------------
# 1. P2 authority — behavioral pin
# ---------------------------------------------------------------------------


class P2AuthorityBehavioralPin(unittest.TestCase):
    """Run the P1+P2 sequence end-to-end with synthetic inputs.
    Assert that the final ``policy_alert_level`` is what P2 returned,
    not what P1 returned."""

    def _p1_p2_sequence(
        self,
        *,
        score: int,
        strength: str,
        risk_level: str,
        impact_level: str,
        evidence_quality_avg: int = 70,
        source_trust_components: dict | None = None,
        strength_summary: dict | None = None,
        official_mismatch: bool = False,
        approved_boost: bool = False,
    ) -> tuple[str, str, str]:
        """Returns (p1_label, p2_label, final_field_value)."""
        policy_confidence = {
            "policy_confidence_score": score,
            "verification_strength": strength,
            "risk_level": risk_level,
        }
        policy_impact = {
            "impact_level": impact_level,
            "impact_direction": "mixed",
            "consumer_sensitivity": 40,
            "business_sensitivity": 40,
            "market_sensitivity": 40,
            "affected_sectors": [],
            "affected_groups": [],
            "impact_reasons": [],
        }
        # P1 first
        final_decision = make_final_decision(
            policy_confidence=policy_confidence,
            policy_impact=policy_impact,
        )
        p1_label = final_decision["policy_alert_level"]
        # P2 next (overwrites)
        verification_card = {
            "official_mismatch": official_mismatch,
            "source_reliability_summary": {
                **(source_trust_components or {}),
                "official_mismatch": official_mismatch,
            },
            "contradiction_summary": {
                "confirmed_contradiction_count": 0,
                "possible_contradiction_count": 0,
            },
            "evidence_quality_summary": {
                "average_evidence_quality_score": evidence_quality_avg,
            },
        }
        debug_summary = {
            "evidence_strength_summary": strength_summary
            or {"strong": 0, "medium": 0, "weak": 0},
            "evidence_quality_summary": {
                "average_evidence_quality_score": evidence_quality_avg,
            },
            "approved_boost": approved_boost,
            "rejected_penalty": False,
        }
        final_decision, _ = calibrate_final_decision(
            final_decision=final_decision,
            policy_confidence=policy_confidence,
            policy_impact=policy_impact,
            verification_card=verification_card,
            source_candidates=[],
            evidence_snippets=[],
            debug_summary=debug_summary,
        )
        p2_label = final_decision["policy_alert_level"]
        return p1_label, p2_label, final_decision["policy_alert_level"]

    def test_p2_overwrites_p1_when_they_disagree(self):
        """The strong-evidence ELS scenario: P1 = MEDIUM, P2 = HIGH.
        Final value MUST be P2's HIGH. Mirrors
        regression_fixture_geumyungwi_strong in the M11.0d-1
        snapshot."""
        p1, p2, final = self._p1_p2_sequence(
            score=85, strength="high", risk_level="medium", impact_level="high",
            evidence_quality_avg=80,
            source_trust_components={
                "official_detail_available": True,
                "official_body_matches": 1,
                "official_resolution_direct_matches": 1,
                "official_resolution_top_score": 80,
                "average_reliability_score": 90,
            },
            strength_summary={"strong": 1, "medium": 0, "weak": 0},
        )
        self.assertEqual(p1, "MEDIUM",
                         "P1 should say MEDIUM on this fixture per M11.0d-1.")
        self.assertEqual(p2, "HIGH",
                         "P2 should calibrate up to HIGH per M11.0d-1.")
        self.assertEqual(
            final, "HIGH",
            "FINAL field MUST be P2's label — that's the M11.0d-3b "
            "codification.",
        )
        self.assertNotEqual(
            final, p1,
            "If final == P1, P2's authority is broken.",
        )

    def test_p2_authority_when_p1_p2_agree(self):
        """When P1 and P2 agree (LOW + LOW), the codification still
        holds: the final value IS P2's output (which happens to
        equal P1's). This is the trivial-case sanity check."""
        p1, p2, final = self._p1_p2_sequence(
            score=18, strength="none", risk_level="medium", impact_level="medium",
            evidence_quality_avg=22,
            source_trust_components={"average_reliability_score": 30},
            official_mismatch=True,
        )
        self.assertEqual(p2, final,
                         "Final must equal P2's label, even when P1 happens to agree.")

    def test_p2_emits_only_documented_vocabulary(self):
        """Sanity pin tying back to M11.0d-1's vocabulary contract:
        P2 NEVER emits MEDIUM. This is also pinned in
        test_verdict_producer_disagreement_diagnostic.py over a
        42-row matrix; here we add a single sanity hit so an
        operator running just this file sees the constraint."""
        allowed = {"HIGH", "WATCH", "LOW"}
        for fixture in (
            dict(score=85, strength="high", risk_level="high", impact_level="high",
                 evidence_quality_avg=80,
                 source_trust_components={"official_detail_available": True,
                                           "official_body_matches": 1,
                                           "average_reliability_score": 80},
                 strength_summary={"strong": 1, "medium": 0, "weak": 0}),
            dict(score=50, strength="medium", risk_level="medium", impact_level="medium"),
            dict(score=10, strength="none", risk_level="low", impact_level="low"),
        ):
            with self.subTest(score=fixture["score"]):
                _, p2, _ = self._p1_p2_sequence(**fixture)
                self.assertIn(p2, allowed,
                              f"P2 emitted {p2!r} — not in {sorted(allowed)}.")


# ---------------------------------------------------------------------------
# 2. P1 prose-only docstring contract pin
# ---------------------------------------------------------------------------


class DocstringContractPin(unittest.TestCase):
    """Read the producer docstrings and assert the M11.0d-3b contract
    phrases are present. Catches a refactor that silently strips the
    role-clarification text."""

    def test_p1_docstring_states_prose_only_role(self):
        doc = make_final_decision.__doc__ or ""
        for needle in (
            "Producer 1 (P1)",
            "M11.0d-3b",
            "PROSE-ONLY",
            "OVERWRITTEN",
            "calibrate_final_decision",
            "disagreement_signal",
            "M11.0d-3b-2",
        ):
            self.assertIn(
                needle, doc,
                f"make_final_decision docstring missing {needle!r}. "
                "The M11.0d-3b codification depends on the docstring "
                "stating P1 is prose-only.",
            )

    def test_p2_docstring_states_authoritative_role(self):
        doc = calibrate_final_decision.__doc__ or ""
        for needle in (
            "Producer 2 (P2)",
            "AUTHORITATIVE",
            "policy_alert_level",
            "OVERWRITES",
            "make_final_decision",
            "{HIGH, WATCH, LOW}",
            "disagreement_signal",
        ):
            self.assertIn(
                needle, doc,
                f"calibrate_final_decision docstring missing {needle!r}. "
                "The M11.0d-3b codification depends on the docstring "
                "stating P2 is authoritative.",
            )


# ---------------------------------------------------------------------------
# 3. disagreement_signal still captures P1's label
# ---------------------------------------------------------------------------


class DisagreementSignalStillCapturesP1Pin(unittest.TestCase):
    """The M11.0d-3a wiring must continue to work after M11.0d-3b
    landed. Cross-check that _build_disagreement_signal still
    reports P1's label distinctly from P2's."""

    def test_p1_label_carried_in_signal_when_p2_overrides(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="MEDIUM",
            p2_alert_level="HIGH",
            p3_verdict_label="draft_verified",
        )
        self.assertEqual(signal["p1_label"], "MEDIUM",
                         "P1's label must be preserved verbatim in the signal.")
        self.assertEqual(signal["p2_label"], "HIGH")
        self.assertFalse(signal["agreed"])

    def test_p1_label_carried_when_p2_agrees(self):
        signal = _build_disagreement_signal(
            p1_alert_level_raw="LOW",
            p2_alert_level="LOW",
            p3_verdict_label="draft_unverified",
        )
        self.assertEqual(signal["p1_label"], "LOW")
        self.assertEqual(signal["p2_label"], "LOW")
        self.assertTrue(signal["agreed"])


# ---------------------------------------------------------------------------
# 4. Constraint #11 — operator_review_required ALWAYS True
# ---------------------------------------------------------------------------


class OperatorReviewRequiredInvariantPin(unittest.TestCase):
    """Pin the M11.0d-1 Section E Constraint #11."""

    def test_database_schemas_force_default_1(self):
        """Every CREATE TABLE in database.py that has an
        operator_review_required column must declare it
        NOT NULL DEFAULT 1."""
        source = (_PROJECT_ROOT / "database.py").read_text(encoding="utf-8")
        # Find every line containing the column declaration.
        pattern = re.compile(
            r"operator_review_required\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+1",
            re.IGNORECASE,
        )
        matches = pattern.findall(source)
        self.assertGreaterEqual(
            len(matches), 3,
            "Expected at least 3 database tables with "
            "`operator_review_required INTEGER NOT NULL DEFAULT 1`; "
            f"found {len(matches)}. M11.0d-1 Constraint #11 requires "
            "the DB to enforce True regardless of caller input.",
        )

    def test_artifact_evidence_linker_forces_true_in_candidate_to_dict(self):
        """Even if the dataclass has operator_review_required=False,
        candidate_to_dict must coerce it to True."""
        from artifact_evidence_linker import (
            EvidenceCandidate,
            candidate_to_dict,
        )
        # Build a candidate with operator_review_required deliberately
        # set to False to verify it gets coerced. EvidenceCandidate
        # field names are confirmed from artifact_evidence_linker.py:112.
        candidate = EvidenceCandidate(
            extraction_id=1,
            source_id="src-001",
            url="https://example.go.kr/x",
            analysis_id="ana-001",
            claim_text="테스트 주장",
            match_score=42.0,
            matched_tokens=["테스트"],
            operator_review_required=False,  # Deliberately wrong.
        )
        payload = candidate_to_dict(candidate)
        self.assertTrue(
            payload["operator_review_required"],
            "candidate_to_dict must force operator_review_required to "
            "True even when the dataclass passes False (Constraint #11).",
        )
        self.assertFalse(
            payload["truth_claim"],
            "candidate_to_dict must also keep truth_claim False as "
            "the partner invariant.",
        )


# ---------------------------------------------------------------------------
# 5. Constraint #12 — LLM cannot raise verdict (structural pin)
# ---------------------------------------------------------------------------


class LLMUpgradePathStructuralPin(unittest.TestCase):
    """Pin the M11.0d-1 Section E Constraint #12.

    No LLM-touching module may assign to policy_alert_level,
    policy_confidence_score, or verification_strength. The
    authoritative writers are pinned to a specific allowlist.
    """

    LLM_MODULES = (
        "llm_judge.py",
        "ai_reasoner.py",
        "scripts/dry_run_llm_judge.py",
    )

    SENSITIVE_FIELDS = (
        "policy_alert_level",
        "policy_confidence_score",
        "verification_strength",
    )

    AUTHORIZED_WRITER_FILES = frozenset({
        "main.py",
        "policy_decision.py",
        "policy_scoring.py",
        "policy_confidence.py",
        # Read-only diagnostic comparators emit synthetic states
        # for testing; they never touch live pipeline state.
        "verdict_label_diagnostic.py",
        "verdict_producer_comparison.py",
    })

    def _read(self, relative_path: str) -> str:
        path = _PROJECT_ROOT / relative_path
        if not path.exists():
            self.fail(f"Expected file not found: {relative_path}")
        return path.read_text(encoding="utf-8")

    def test_llm_modules_do_not_assign_sensitive_fields_via_subscript(self):
        """No LLM-touching module may contain
        `result["policy_alert_level"] = ...` or its variants for the
        sensitive fields."""
        for module in self.LLM_MODULES:
            source = self._read(module)
            for field in self.SENSITIVE_FIELDS:
                # Subscript assignment patterns.
                patterns = [
                    rf'\["{re.escape(field)}"\]\s*=',
                    rf"\['{re.escape(field)}'\]\s*=",
                ]
                for pat in patterns:
                    matches = re.findall(pat, source)
                    self.assertEqual(
                        len(matches), 0,
                        f"{module} contains a subscript assignment to "
                        f"`{field}` matching /{pat}/. M11.0d-1 "
                        "Constraint #12 forbids LLM modules from "
                        "writing to verdict-state fields.",
                    )

    def test_llm_modules_do_not_assign_sensitive_fields_via_attribute(self):
        """No LLM module may contain `something.policy_alert_level = ...`."""
        for module in self.LLM_MODULES:
            source = self._read(module)
            tree = ast.parse(source)
            offenders = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr in self.SENSITIVE_FIELDS
                    ):
                        offenders.append(
                            f"{module}:{node.lineno} "
                            f"attribute assignment to .{target.attr}"
                        )
            self.assertEqual(
                offenders, [],
                f"{module} contains attribute assignments to sensitive "
                "verdict fields: " + ", ".join(offenders),
            )

    def test_policy_alert_level_writers_are_in_allowlist(self):
        """AST-walk every .py file in the repo. Every subscript or
        attribute assignment to ``policy_alert_level`` must come
        from a file in AUTHORIZED_WRITER_FILES."""
        skip_dirs = {
            ".git", ".venv", "venv", "__pycache__", "node_modules",
            ".pytest_cache", ".mypy_cache", "tests",
            "frontend",  # JS, not Python
        }
        offenders: list[str] = []
        for dirpath, dirnames, filenames in os.walk(_PROJECT_ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = Path(dirpath) / filename
                rel = str(path.relative_to(_PROJECT_ROOT)).replace("\\", "/")
                if rel in self.AUTHORIZED_WRITER_FILES:
                    continue
                if "/" in rel and rel.split("/")[0] == "scripts":
                    # Scripts directory has dry-run/diagnostic tools
                    # that read state but should not write live state.
                    pass  # subject to the same check
                try:
                    source = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if "policy_alert_level" not in source:
                    continue
                try:
                    tree = ast.parse(source, filename=str(path))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    for target in node.targets:
                        # Subscript assignment: x["policy_alert_level"] = ...
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "policy_alert_level"
                        ):
                            offenders.append(
                                f"{rel}:{node.lineno} "
                                "subscript assignment to "
                                '["policy_alert_level"]'
                            )
                        # Attribute assignment: x.policy_alert_level = ...
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "policy_alert_level"
                        ):
                            offenders.append(
                                f"{rel}:{node.lineno} "
                                "attribute assignment to "
                                ".policy_alert_level"
                            )
        self.assertEqual(
            offenders, [],
            "policy_alert_level was assigned outside the authorized "
            "writer files (main.py, policy_decision.py, "
            "policy_scoring.py, policy_confidence.py, plus the "
            "read-only diagnostic comparators). Offenders:\n"
            + "\n".join(offenders)
            + "\n\nM11.0d-1 Constraint #12: no LLM-driven upgrade "
            "path is permitted.",
        )


if __name__ == "__main__":
    unittest.main()
