"""Phase 2 M11.0b: tests for ``verdict_label_diagnostic`` +
``diagnose_verdict_labels``.

Every test that writes to a database uses a temp SQLite file so the
real ``policy_ai.db`` is untouched. No test path calls
``analyze_pipeline``, makes a network call, imports OpenAI, or
invokes browser automation. No test mutates ``verification_card.py``
or ``_verdict_label``.

Covers the M11.0b spec items:
    A. ID 105-shaped row (no official, score=10, strength=none,
       direct_support_count>=1, claim_count=1) → attribution maps to
       B08_direct_support_only, confidence="high",
       is_weak_evidence_verified=True
    B. Strong-official inputs (score>=85, strong_official_match,
       direct_support<claim_count, official_sources present) →
       attribution maps to B13_strong_confidence_verified,
       is_weak_evidence_verified=False
    C. has_conflict=True → attribution maps to
       B01_conflict_or_official_conflict, label=draft_disputed
    D. Totally empty inputs → fallback_unverified or unknown branch
    E-H. compute_weak_evidence_signals signal coverage
    I-J. truth_claim / operator_review_required invariants
    K-L. DB layer forces truth_claim=0 and operator_review_required=1
    M. INSERT OR REPLACE on analysis_id
    N. get_verdict_label_attributions filters by analysis_id
    O. only_weak_evidence_verified filter
    P. compute_branch_summary with zero rows
    Q. compute_branch_summary per-branch/label/risk counts
    R. VERDICT_LABEL_BRANCHES has one entry per `return "draft_*"` line
    S. No network calls in any test path
    T. No OpenAI imports in verdict_label_diagnostic.py
    U. verdict_label_diagnostic not imported by main / api / scheduler
    V. verification_card.py unchanged (signature + line-414 pin)
    W. Malformed JSON in stored row → still returns attribution, no crash
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database  # noqa: E402
import postgres_storage  # noqa: E402
import verdict_label_diagnostic as diagnostic  # noqa: E402
import verification_card  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "diagnose_verdict_labels.py"
DIAGNOSTIC_MODULE_PATH = ROOT / "verdict_label_diagnostic.py"
VERIFICATION_CARD_PATH = ROOT / "verification_card.py"

CLI_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row_id_105_pattern() -> dict:
    """Mirror the production ID-105 row that originally surfaced the
    suspected bug: no official sources, score=10, strength=none, but
    the evidence_snippets list contains a single direct_support entry
    so the B08 branch fires."""
    return {
        "id": 105,
        "claim_text": "정부가 전세 보증금을 지원한다",
        "claims": json.dumps(
            ["정부가 전세 보증금을 지원한다"], ensure_ascii=False,
        ),
        "verdict_label": "draft_verified",
        "verdict_confidence": 10,
        "policy_alert_level": "LOW",
        "policy_confidence_score": 10,
        "verification_strength": "none",
        "evidence_summary": (
            "공식 검색 페이지 접근이 실패해 상세 공식문서를 비교할 수 없습니다."
        ),
        "evidence_sources": json.dumps(
            [{"title": "비공식 뉴스", "source_type": "news"}],
            ensure_ascii=False,
        ),
        "evidence_snippets": json.dumps(
            [{"evidence_type": "direct_support",
              "claim_text": "정부가 전세 보증금을 지원한다"}],
            ensure_ascii=False,
        ),
        "contradiction_summary": "{}",
        "bias_framing_summary": "{}",
        "official_sources": "[]",
        "debug_summary": json.dumps(
            {"evidence_comparison": {
                "comparison_status": "official_evidence_missing",
                "verification_level": None,
            }},
            ensure_ascii=False,
        ),
    }


def _row_strong_official_pattern() -> dict:
    """Strong-evidence pattern that the B13 branch is designed for:
    score>=85, verification_level=strong_official_match, but the
    snippets do not satisfy B08 (direct_support_count < claim_count)
    so B13 wins."""
    return {
        "id": 207,
        "claim_text": "정부가 전세 보증금 한도를 상향한다",
        "claims": json.dumps(
            [
                "정부가 전세 보증금 한도를 상향한다",
                "보증금 한도는 8천만 원이다",
            ],
            ensure_ascii=False,
        ),
        "verdict_label": "draft_verified",
        "verdict_confidence": 88,
        "policy_alert_level": "HIGH",
        "policy_confidence_score": 88,
        "verification_strength": "high",
        "evidence_summary": "공식 부처 발표를 확인했습니다.",
        "evidence_snippets": "[]",  # no direct_support → B08 cannot fire
        "contradiction_summary": "{}",
        "bias_framing_summary": "{}",
        "official_sources": json.dumps(
            [{"title": "기획재정부 보도자료",
              "url": "https://moef.go.kr/x"}],
            ensure_ascii=False,
        ),
        "debug_summary": json.dumps(
            {"evidence_comparison": {
                "comparison_status": "official_evidence_confirmed",
                "verification_level": "strong_official_match",
            }},
            ensure_ascii=False,
        ),
    }


def _row_conflict_pattern() -> dict:
    return {
        "id": 301,
        "claim_text": "정부가 정책을 폐지한다",
        "verdict_label": "draft_disputed",
        "policy_alert_level": "WATCH",
        "policy_confidence_score": 40,
        "verification_strength": "low",
        "evidence_summary": "공식 발표와 뉴스 보도가 상반됩니다.",
        "evidence_snippets": "[]",
        "contradiction_summary": "{}",
        "bias_framing_summary": "{}",
        "official_sources": json.dumps(
            [{"title": "정부 발표", "url": "https://example.go.kr"}],
            ensure_ascii=False,
        ),
        "debug_summary": json.dumps(
            {"evidence_comparison": {
                "comparison_status": "official_conflict_possible",
                "verification_level": "medium_official_match",
                "conflict_signals": ["polarity_mismatch"],
            }},
            ensure_ascii=False,
        ),
    }


def _row_empty_pattern() -> dict:
    return {
        "id": 999,
        "verdict_label": None,
    }


def _row_with_broken_json() -> dict:
    """Row whose JSON columns contain syntactically broken JSON.
    The diagnostic must still return an attribution without raising."""
    row = _row_id_105_pattern()
    row["evidence_snippets"] = "[{not valid json"
    row["debug_summary"] = "{not valid json"
    row["contradiction_summary"] = "{not valid json"
    return row


# ---------------------------------------------------------------------------
# A-D. attribute_branch_for_row
# ---------------------------------------------------------------------------


class AttributeBranchTests(unittest.TestCase):
    def test_id_105_pattern_attributes_to_b08(self):
        attr = diagnostic.attribute_branch_for_row(_row_id_105_pattern())
        # The diagnostic catalog still flags B08 as the label-emitting
        # branch for draft_verified, but after M11.0c B08's trigger
        # gates (score>=60 AND strength in {medium,high}) do not match
        # the ID-105 row (score=10, strength=none). The diagnostic
        # falls back to the first label-matching branch in source
        # order with confidence="low" — exactly what's intended when
        # the stored label could not have been produced by the
        # current source.
        self.assertEqual(attr.attributed_branch_id, "B08_direct_support_only")
        self.assertEqual(attr.attribution_confidence, "low")
        self.assertEqual(attr.stored_verdict_label, "draft_verified")
        self.assertTrue(attr.is_weak_evidence_verified)
        # All four weak-evidence signals fire for this row.
        for signal in (
            "no_official_sources", "score_leq_30",
            "strength_none", "evidence_summary_says_failure",
        ):
            self.assertIn(signal, attr.weak_evidence_signals)
        # Reconstructed snippet counts match the fixture.
        self.assertEqual(attr.reconstructed_direct_support_count, 1)
        self.assertEqual(attr.reconstructed_claim_count, 1)
        # The fixture's official_sources column is an empty list, so the
        # reconstructed count is 0 (the evidence_sources fallback only
        # fires when official_sources is *missing*, not when it's [])
        # — which is what makes the no_official_sources signal fire.
        self.assertEqual(attr.reconstructed_official_sources_count, 0)
        self.assertIs(attr.truth_claim, False)
        self.assertIs(attr.operator_review_required, True)

    def test_strong_official_pattern_attributes_to_b13(self):
        attr = diagnostic.attribute_branch_for_row(
            _row_strong_official_pattern(),
        )
        self.assertEqual(
            attr.attributed_branch_id, "B13_strong_confidence_verified",
        )
        self.assertEqual(attr.attribution_confidence, "high")
        self.assertEqual(attr.stored_verdict_label, "draft_verified")
        self.assertFalse(attr.is_weak_evidence_verified)
        # No weak signals on this strong-evidence row.
        self.assertNotIn("no_official_sources", attr.weak_evidence_signals)
        self.assertNotIn("score_leq_30", attr.weak_evidence_signals)
        self.assertNotIn("strength_none", attr.weak_evidence_signals)

    def test_conflict_pattern_attributes_to_b01(self):
        attr = diagnostic.attribute_branch_for_row(_row_conflict_pattern())
        self.assertEqual(
            attr.attributed_branch_id, "B01_conflict_or_official_conflict",
        )
        self.assertEqual(attr.stored_verdict_label, "draft_disputed")
        self.assertEqual(attr.attribution_confidence, "high")

    def test_empty_row_attribution_is_unknown(self):
        attr = diagnostic.attribute_branch_for_row(_row_empty_pattern())
        # No stored_verdict_label → attribution must be unknown.
        self.assertIsNone(attr.attributed_branch_id)
        self.assertEqual(attr.attribution_confidence, "unknown")
        self.assertFalse(attr.is_weak_evidence_verified)

    def test_non_dict_input_is_handled_safely(self):
        attr = diagnostic.attribute_branch_for_row(None)  # type: ignore[arg-type]
        self.assertEqual(attr.attribution_confidence, "unknown")
        self.assertIs(attr.truth_claim, False)
        self.assertIs(attr.operator_review_required, True)


# ---------------------------------------------------------------------------
# W. Malformed JSON → no crash
# ---------------------------------------------------------------------------


class MalformedJsonTests(unittest.TestCase):
    def test_broken_json_does_not_crash(self):
        attr = diagnostic.attribute_branch_for_row(_row_with_broken_json())
        # Should still return an attribution; the broken JSON columns
        # reduce to empty lists/dicts so reconstructed counts go to 0.
        self.assertEqual(attr.reconstructed_direct_support_count, 0)
        # Without direct_support, B08 cannot fire — the stored
        # draft_verified label cannot be triggered by any branch's
        # reconstructed inputs. attribution_confidence must surface
        # the issue (low or unknown) without raising.
        self.assertIn(
            attr.attribution_confidence, {"low", "medium", "unknown"},
        )
        self.assertIs(attr.truth_claim, False)
        self.assertIs(attr.operator_review_required, True)


# ---------------------------------------------------------------------------
# E-H. compute_weak_evidence_signals
# ---------------------------------------------------------------------------


class WeakEvidenceSignalsTests(unittest.TestCase):
    def test_no_official_sources_signal(self):
        row = {"official_sources": "[]", "evidence_sources": "[]"}
        self.assertIn(
            "no_official_sources",
            diagnostic.compute_weak_evidence_signals(row),
        )

    def test_score_leq_30_signal_at_boundary(self):
        for score in (0, 10, 30):
            row = {"policy_confidence_score": score}
            self.assertIn(
                "score_leq_30",
                diagnostic.compute_weak_evidence_signals(row),
                f"score {score} should fire score_leq_30",
            )

    def test_score_above_threshold_does_not_fire(self):
        row = {"policy_confidence_score": 31}
        self.assertNotIn(
            "score_leq_30",
            diagnostic.compute_weak_evidence_signals(row),
        )

    def test_strength_none_signal(self):
        row = {"verification_strength": "none"}
        self.assertIn(
            "strength_none",
            diagnostic.compute_weak_evidence_signals(row),
        )

    def test_strength_strong_does_not_fire(self):
        row = {"verification_strength": "strong"}
        self.assertNotIn(
            "strength_none",
            diagnostic.compute_weak_evidence_signals(row),
        )

    def test_evidence_summary_failure_phrase_signal(self):
        for phrase in ("비교할 수 없습니다", "접근이 실패했습니다"):
            row = {"evidence_summary": f"공식문서를 {phrase}."}
            self.assertIn(
                "evidence_summary_says_failure",
                diagnostic.compute_weak_evidence_signals(row),
                f"phrase {phrase!r} should trigger the signal",
            )

    def test_clean_row_emits_no_signals(self):
        row = {
            "official_sources": json.dumps(
                [{"title": "ok"}], ensure_ascii=False,
            ),
            "policy_confidence_score": 90,
            "verification_strength": "strong",
            "evidence_summary": "공식 발표 확인 완료.",
        }
        self.assertEqual(diagnostic.compute_weak_evidence_signals(row), [])


# ---------------------------------------------------------------------------
# I/J. ProducerLabelAttribution invariants
# ---------------------------------------------------------------------------


class AttributionInvariantsTests(unittest.TestCase):
    def test_truth_claim_always_false_after_caller_lies(self):
        attr = diagnostic.attribute_branch_for_row(_row_id_105_pattern())
        # Mutate the dataclass fields to simulate a misbehaving caller.
        attr.truth_claim = True             # type: ignore[assignment]
        attr.operator_review_required = False  # type: ignore[assignment]
        d = diagnostic.attribution_to_dict(attr)
        self.assertIs(d["truth_claim"], False)
        self.assertIs(d["operator_review_required"], True)

    def test_attribution_to_dict_keys(self):
        attr = diagnostic.attribute_branch_for_row(_row_id_105_pattern())
        d = diagnostic.attribution_to_dict(attr)
        for key in (
            "analysis_id", "stored_verdict_label",
            "stored_verdict_confidence",
            "stored_policy_alert_level",
            "stored_policy_confidence_score",
            "stored_verification_strength", "stored_claim_text",
            "stored_evidence_summary", "reconstructed_inputs",
            "attributed_branch_id", "attribution_confidence",
            "attribution_reason", "is_weak_evidence_verified",
            "weak_evidence_signals", "diagnostic_timestamp",
            "notes", "truth_claim", "operator_review_required",
        ):
            self.assertIn(key, d, f"missing key: {key}")
        # reconstructed_inputs is a JSON-encoded string.
        self.assertIsInstance(d["reconstructed_inputs"], str)
        # weak_evidence_signals is a JSON-encoded string for the TEXT column.
        self.assertIsInstance(d["weak_evidence_signals"], str)
        signals = json.loads(d["weak_evidence_signals"])
        self.assertIn("no_official_sources", signals)


# ---------------------------------------------------------------------------
# K-O. Database round-trip with temp DB
# ---------------------------------------------------------------------------


class DatabaseRoundTripTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: round-trip via the PG-primary path. Point a private
        # sqlite:// substitute at a per-test temp file (USE_POSTGRES_WRITE
        # ON) and reset the cached engine so each test binds to its own
        # fresh DB. Env vars are snapshot/restored so the rest of the
        # process (and validate.py's dual-write-disabled determinism) is
        # untouched. Pattern copied from the #1/#2/#3-migrated tests.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._pg_db = str(Path(self._tmp_dir.name) / "pg.db")
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db}"
        postgres_storage.reset_engine_for_tests()

    def tearDown(self):
        import gc as _gc
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        postgres_storage.reset_engine_for_tests()
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def _save(self, *, row, lie=False) -> int:
        attr = diagnostic.attribute_branch_for_row(row)
        d = diagnostic.attribution_to_dict(attr)
        if lie:
            d["truth_claim"] = True
            d["operator_review_required"] = False
        return database.save_verdict_label_attribution(d)

    def test_basic_round_trip(self):
        row_id = self._save(row=_row_id_105_pattern())
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)
        rows = database.get_verdict_label_attributions()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["analysis_id"], "105")
        self.assertEqual(r["stored_verdict_label"], "draft_verified")
        self.assertEqual(
            r["attributed_branch_id"], "B08_direct_support_only",
        )
        self.assertIs(r["truth_claim"], False)
        self.assertIs(r["operator_review_required"], True)
        self.assertIs(r["is_weak_evidence_verified"], True)

    def test_save_forces_truth_claim_zero_even_when_caller_lies(self):
        self._save(row=_row_id_105_pattern(), lie=True)
        rows = database.get_verdict_label_attributions()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["truth_claim"], False)

    def test_save_forces_operator_review_required_one_even_when_lied(self):
        self._save(row=_row_id_105_pattern(), lie=True)
        rows = database.get_verdict_label_attributions()
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["operator_review_required"], True)

    def test_insert_or_replace_on_analysis_id(self):
        self._save(row=_row_id_105_pattern())
        # Same analysis_id again → upsert (PG: ON CONFLICT DO UPDATE).
        self._save(row=_row_id_105_pattern())
        rows = database.get_verdict_label_attributions()
        self.assertEqual(
            len(rows), 1, "duplicate analysis_id must overwrite",
        )
        # A different analysis_id produces a new row, not a replacement.
        self._save(row=_row_strong_official_pattern())
        rows = database.get_verdict_label_attributions()
        self.assertEqual(len(rows), 2)

    def test_get_filters_by_analysis_id(self):
        self._save(row=_row_id_105_pattern())
        self._save(row=_row_strong_official_pattern())
        only_105 = database.get_verdict_label_attributions(
            analysis_id="105",
        )
        self.assertEqual(len(only_105), 1)
        self.assertEqual(only_105[0]["analysis_id"], "105")

    def test_only_weak_evidence_verified_filter(self):
        self._save(row=_row_id_105_pattern())            # weak verified
        self._save(row=_row_strong_official_pattern())   # not weak
        self._save(row=_row_conflict_pattern())          # not verified
        weak = database.get_verdict_label_attributions(
            only_weak_evidence_verified=True,
        )
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["analysis_id"], "105")

    def test_save_rejects_missing_required_fields(self):
        for bad in (
            None,
            {},
            {"analysis_id": "x"},  # no diagnostic_timestamp
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    database.save_verdict_label_attribution(bad)


# ---------------------------------------------------------------------------
# M12.0e-6b-1: InitDbSqliteSchemaTests removed. It pinned init_db()'s SQLite
# schema-creation (verdict_label_attributions, verified via sqlite_master) —
# that machinery is intentionally retired in 0e-6b-3, so the coverage is
# dropped here rather than left coupled to soon-to-be-removed symbols. PG
# schema is owned by postgres_storage.ensure_schema.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P-Q. compute_branch_summary
# ---------------------------------------------------------------------------


class BranchSummaryTests(unittest.TestCase):
    def test_zero_rows(self):
        summary = diagnostic.compute_branch_summary([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["unknown_attribution_count"], 0)
        self.assertEqual(summary["per_branch_counts"], {})
        self.assertEqual(summary["weak_evidence_verified_count"], 0)

    def test_counts_per_branch_label_and_risk(self):
        attributions = [
            diagnostic.attribute_branch_for_row(_row_id_105_pattern()),
            diagnostic.attribute_branch_for_row(
                _row_strong_official_pattern(),
            ),
            diagnostic.attribute_branch_for_row(_row_conflict_pattern()),
        ]
        summary = diagnostic.compute_branch_summary(attributions)
        self.assertEqual(summary["total"], 3)
        # Per-branch.
        self.assertEqual(
            summary["per_branch_counts"]["B08_direct_support_only"], 1,
        )
        self.assertEqual(
            summary["per_branch_counts"]["B13_strong_confidence_verified"], 1,
        )
        self.assertEqual(
            summary["per_branch_counts"]["B01_conflict_or_official_conflict"],
            1,
        )
        # Per-label.
        self.assertEqual(
            summary["per_output_label_counts"]["draft_verified"], 2,
        )
        self.assertEqual(
            summary["per_output_label_counts"]["draft_disputed"], 1,
        )
        # Per-risk. M11.0c moved B08 from verified_without_strict_checks
        # to verified_with_strict_checks, so both B08 and B13
        # attributions (the two draft_verified branches) now land in
        # the strict bucket.
        risk = summary["per_risk_classification_counts"]
        self.assertEqual(risk["verified_with_strict_checks"], 2)
        self.assertEqual(risk["conservative_safe"], 1)
        self.assertNotIn(
            "verified_without_strict_checks", risk,
            "no branch should land in verified_without_strict_checks "
            "after M11.0c",
        )
        # Weak-evidence counts.
        self.assertEqual(summary["weak_evidence_verified_count"], 1)
        self.assertIn(
            "no_official_sources",
            summary["weak_evidence_signal_histogram"],
        )


# ---------------------------------------------------------------------------
# R. VERDICT_LABEL_BRANCHES branch-count parity with the source
# ---------------------------------------------------------------------------


class BranchCatalogueParityTests(unittest.TestCase):
    def test_branches_match_return_statements_in_source(self):
        source = VERIFICATION_CARD_PATH.read_text(encoding="utf-8")
        # Find the _verdict_label function body and count `return "draft_*"`
        # statements inside it.
        function_start = source.find("def _verdict_label(")
        self.assertGreater(
            function_start, -1, "expected _verdict_label in source",
        )
        # The function ends at the next top-level `def ` line.
        next_def = source.find("\ndef ", function_start + 1)
        body = source[function_start:next_def if next_def != -1 else None]
        returns = re.findall(r'return "draft_[a-z_]+"', body)
        self.assertEqual(
            len(returns), len(diagnostic.VERDICT_LABEL_BRANCHES),
            f"branch count mismatch: source has {len(returns)} "
            f"return statements but VERDICT_LABEL_BRANCHES has "
            f"{len(diagnostic.VERDICT_LABEL_BRANCHES)} entries",
        )

    def test_branch_id_uniqueness(self):
        ids = [b["branch_id"] for b in diagnostic.VERDICT_LABEL_BRANCHES]
        self.assertEqual(len(ids), len(set(ids)), "branch_id duplicates")

    def test_every_branch_has_required_fields(self):
        for b in diagnostic.VERDICT_LABEL_BRANCHES:
            for key in (
                "branch_id", "line_range", "output_label",
                "trigger_summary", "risk_classification",
            ):
                self.assertIn(key, b)
            self.assertIn(
                b["risk_classification"], diagnostic.RISK_CLASSIFICATIONS,
            )


# ---------------------------------------------------------------------------
# V. verification_card.py unchanged
# ---------------------------------------------------------------------------


class VerificationCardUnchangedTests(unittest.TestCase):
    def test_verdict_label_signature_pinned(self):
        sig = inspect.signature(verification_card._verdict_label)
        params = list(sig.parameters)
        expected_first_three = [
            "policy_confidence", "evidence_comparison", "official_sources",
        ]
        self.assertEqual(params[:3], expected_first_three)
        for name in (
            "evidence_snippets", "contradiction_summary",
            "bias_framing_summary", "claim_count",
        ):
            self.assertIn(name, params)

    def test_verdict_label_definition_still_present(self):
        # M11.0c shifted the definition (a new module-level constant
        # was inserted at the top of verification_card.py to gate B08).
        # We no longer pin a specific line number — that's brittle.
        # Instead, confirm the definition exists exactly once, on its
        # own line, with the documented opening signature.
        source = VERIFICATION_CARD_PATH.read_text(encoding="utf-8")
        matches = [
            line for line in source.splitlines()
            if line.startswith("def _verdict_label(")
        ]
        self.assertEqual(
            len(matches), 1,
            f"_verdict_label definition count changed: {matches!r}",
        )

    def test_b08_branch_still_present_in_source(self):
        source = VERIFICATION_CARD_PATH.read_text(encoding="utf-8")
        # B08 is now a multi-line gated if (M11.0c). Pin the two
        # most stable substrings: the count predicate and the
        # score gate. Any future refactor that drops either of
        # these forces an update to this milestone's docs.
        self.assertIn(
            "direct_support_count >= claim_count",
            source,
            "B08 count predicate removed from verification_card.py",
        )
        self.assertIn(
            "confidence_score >= 60", source,
            "B08 score gate (M11.0c) removed from verification_card.py",
        )
        self.assertIn(
            "_STRONG_VERIFICATION_STRENGTHS", source,
            "B08 strength gate (M11.0c) constant removed",
        )
        self.assertIn(
            "confidence_score >= 85", source,
            "B13 strict-confidence check removed",
        )


# ---------------------------------------------------------------------------
# CLI smokes
# ---------------------------------------------------------------------------


def _run_cli_subprocess(*args, timeout=CLI_TIMEOUT_SECONDS, env=None):
    completed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
        cwd=str(ROOT),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return completed.returncode, completed.stdout, completed.stderr


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        # M12.0e-3a: the CLI is PG-only. Point a private sqlite://
        # substitute at a per-test temp file (USE_POSTGRES_WRITE ON) and
        # reset the cached engine. The CLI subprocess inherits both env
        # vars via _run_cli_subprocess's env={**os.environ, ...} merge.
        # These smokes seed nothing — they exercise --help / --branch-table
        # (DB-free) / empty-DB reads / usage errors only.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._pg_db = str(Path(self._tmp_dir.name) / "cli_smoke_pg.db")
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in ("USE_POSTGRES_WRITE", "DATABASE_URL")
        }
        os.environ["USE_POSTGRES_WRITE"] = "true"
        os.environ["DATABASE_URL"] = f"sqlite:///{self._pg_db}"
        postgres_storage.reset_engine_for_tests()

    def tearDown(self):
        import gc as _gc
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        postgres_storage.reset_engine_for_tests()
        _gc.collect()
        try:
            self._tmp_dir.cleanup()
        except Exception:
            pass

    def test_help_exits_0(self):
        rc, stdout, _ = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("_verdict_label", stdout)
        self.assertIn("Exit codes", stdout)

    def test_branch_table_exits_0_no_db_needed(self):
        rc, stdout, _ = _run_cli_subprocess("--branch-table")
        self.assertEqual(rc, 0)
        self.assertIn("B08_direct_support_only", stdout)
        # M11.0c: B08 was moved from verified_without_strict_checks
        # to verified_with_strict_checks (gates added). The branch
        # table now lists B08 in the strict bucket alongside B13.
        self.assertIn("verified_with_strict_checks", stdout)
        self.assertIn("truth_claim=False", stdout)

    def test_branch_table_json(self):
        rc, stdout, _ = _run_cli_subprocess("--branch-table", "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["mode"], "branch_table")
        self.assertEqual(
            len(payload["branches"]),
            len(diagnostic.VERDICT_LABEL_BRANCHES),
        )

    def test_summary_empty_db(self):
        rc, stdout, _ = _run_cli_subprocess("--summary")
        self.assertEqual(rc, 0)
        self.assertIn("Diagnostic Summary", stdout)
        self.assertIn("Total rows attributed:       0", stdout)
        self.assertIn("truth_claim=False", stdout)

    def test_list_weak_verified_empty(self):
        rc, stdout, _ = _run_cli_subprocess("--list-weak-verified")
        self.assertEqual(rc, 0)
        self.assertIn("Total: 0", stdout)

    def test_no_mode_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess()
        self.assertEqual(rc, 2)
        self.assertIn("required", stderr)

    def test_two_modes_simultaneously_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--summary", "--branch-table",
        )
        self.assertEqual(rc, 2)
        self.assertIn("only one", stderr)

    def test_save_and_dry_run_mutually_exclusive(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--from-sqlite", "--save", "--dry-run",
        )
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", stderr)


# ---------------------------------------------------------------------------
# S/T/U. Static safety
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def _import_lines(self, path):
        text = path.read_text(encoding="utf-8")
        return [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]

    def test_diagnostic_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(DIAGNOSTIC_MODULE_PATH))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"verdict_label_diagnostic.py must not import {forbidden!r}",
            )

    def test_cli_does_not_import_network_or_openai(self):
        joined = "\n".join(self._import_lines(CLI_SCRIPT))
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"diagnose_verdict_labels.py must not import {forbidden!r}",
            )

    def test_diagnostic_not_imported_by_pipeline_entry_points(self):
        for module_name in ("main.py", "api_server.py", "scheduler.py"):
            module_path = ROOT / module_name
            if not module_path.exists():
                continue
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "verdict_label_diagnostic", text,
                f"{module_name} must not import verdict_label_diagnostic",
            )
            self.assertNotIn(
                "diagnose_verdict_labels", text,
                f"{module_name} must not import diagnose_verdict_labels",
            )


if __name__ == "__main__":
    unittest.main()
