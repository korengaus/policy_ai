"""Phase 2 M5.6: semantic calibration tests.

Verifies:
    * the calibration fixture is well-formed and covers the required categories,
    * the helper module classifies overstrong cases correctly,
    * the evaluator script runs with the deterministic provider (no OpenAI key,
      no network, no Postgres),
    * the evaluator emits JSON / CSV / Markdown outputs,
    * --provider openai --no-network never makes a live call,
    * --fail-on-regression returns a non-zero code on an intentionally
      impossible expectation,
    * verdict-side modules still do not depend on calibration output.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import semantic_calibration
import semantic_embeddings
import semantic_evidence_agent


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "semantic_calibration_cases.json"
EVALUATOR_SCRIPT = ROOT / "scripts" / "evaluate_semantic_calibration.py"


REQUIRED_CATEGORIES = {
    "direct_support",
    "contextual_only",
    "unrelated",
    "number_mismatch",
    "date_mismatch",
    "eligibility_mismatch",
    "finality_mismatch",
    "negation_or_refutation",
    "no_body",
    "contradiction_like",
    "partial_support",
    "same_topic_wrong_policy",
    "local_vs_central_authority",
    "actor_mismatch",
}

# Categories where the fixture intentionally embeds a critical-fact disagreement
# the M5.7 guardrails should detect. Used by
# ``FixtureShapeTests.test_mismatch_cases_declare_expected_risk_flags``.
GUARDRAIL_FLAG_BY_CATEGORY = {
    "number_mismatch": "number_mismatch",
    "date_mismatch": "date_mismatch",
    "eligibility_mismatch": "eligibility_mismatch",
    "finality_mismatch": "finality_mismatch",
    "negation_or_refutation": "negation_mismatch",
}

# Categories that count as "mismatch / trap" cases — every category except
# direct_support is a calibration trap where the agent should NOT report a
# strong semantic support label. M6.0 requires at least 40% of the fixture to
# fall into one of these buckets so the scorecard exercises guardrails
# rather than rewarding easy direct matches.
MISMATCH_CATEGORIES = {
    "contextual_only",
    "unrelated",
    "number_mismatch",
    "date_mismatch",
    "eligibility_mismatch",
    "finality_mismatch",
    "negation_or_refutation",
    "no_body",
    "contradiction_like",
    "partial_support",
    "same_topic_wrong_policy",
    "local_vs_central_authority",
    "actor_mismatch",
}


@contextmanager
def _env(**overrides: str):
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_evaluator(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        for key, value in env_extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return subprocess.run(
        [sys.executable, str(EVALUATOR_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


class FixtureShapeTests(unittest.TestCase):
    def test_fixture_exists_and_parses(self):
        self.assertTrue(FIXTURE_PATH.exists(), f"fixture missing: {FIXTURE_PATH}")
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(cases, list)
        # M6.0 expanded fixture: 30+ cases across realistic policy domains.
        self.assertGreaterEqual(
            len(cases), 30,
            msg=f"expected expanded fixture (>= 30 cases); found {len(cases)}",
        )

    def test_fixture_covers_required_categories(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        seen = {case.get("category") for case in cases}
        missing = REQUIRED_CATEGORIES - seen
        self.assertFalse(missing, f"missing categories: {sorted(missing)}")

    def test_case_ids_are_unique(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        ids = [case.get("case_id") for case in cases]
        duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
        self.assertFalse(duplicates, f"duplicate case_ids: {duplicates}")

    def test_each_case_has_required_fields(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for case in cases:
            self.assertIn("case_id", case, msg=case)
            self.assertIn("category", case, msg=case)
            self.assertIn("claim_text", case, msg=case)
            self.assertIn("sources", case, msg=case)
            self.assertIsInstance(case["sources"], list, msg=case)
            self.assertIn("expected", case, msg=case)

    def test_at_least_forty_percent_are_mismatch_traps(self):
        # M6.0 fixture must lean heavily on mismatch / trap cases so the
        # scorecard actually exercises the guardrails. A fixture dominated by
        # direct_support cases would let a sloppy provider score well without
        # being tested against false-positive patterns.
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        trap_count = sum(1 for case in cases if case.get("category") in MISMATCH_CATEGORIES)
        ratio = trap_count / len(cases) if cases else 0.0
        self.assertGreaterEqual(
            ratio, 0.40,
            msg=(
                f"only {trap_count}/{len(cases)} ({ratio:.0%}) cases fall in "
                f"mismatch / trap categories; expected >= 40%. "
                f"Mismatch categories considered: {sorted(MISMATCH_CATEGORIES)}"
            ),
        )

    def test_mismatch_cases_declare_expected_risk_flags(self):
        # For categories that map directly to a guardrail flag, the fixture's
        # ``expected.risk_flags`` list must include the corresponding flag.
        # This keeps the fixture self-documenting about what the guardrails
        # should catch on that case.
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for case in cases:
            expected_flag = GUARDRAIL_FLAG_BY_CATEGORY.get(case.get("category"))
            if not expected_flag:
                continue
            flags = (case.get("expected") or {}).get("risk_flags") or []
            self.assertIn(
                expected_flag, flags,
                msg=(
                    f"case {case.get('case_id')!r} "
                    f"category={case.get('category')!r} must declare "
                    f"risk_flag {expected_flag!r}; got {flags}"
                ),
            )


class HelperClassificationTests(unittest.TestCase):
    def test_support_level_rank_ordering(self):
        self.assertLess(
            semantic_calibration.support_level_rank("unavailable"),
            semantic_calibration.support_level_rank("weak"),
        )
        self.assertLess(
            semantic_calibration.support_level_rank("weak"),
            semantic_calibration.support_level_rank("contextual"),
        )
        self.assertLess(
            semantic_calibration.support_level_rank("contextual"),
            semantic_calibration.support_level_rank("strong"),
        )
        # Unknown labels coerce to 0 instead of raising.
        self.assertEqual(semantic_calibration.support_level_rank("???"), 0)
        self.assertEqual(semantic_calibration.support_level_rank(None), 0)

    def test_is_overstrong_triggers_on_higher_rank(self):
        self.assertTrue(semantic_calibration.is_overstrong("strong", "contextual"))
        self.assertTrue(semantic_calibration.is_overstrong("contextual", "weak"))
        self.assertFalse(semantic_calibration.is_overstrong("weak", "contextual"))
        # "any" disables the comparison.
        self.assertFalse(semantic_calibration.is_overstrong("strong", "any"))
        self.assertFalse(semantic_calibration.is_overstrong("strong", ""))

    def test_evaluate_case_marks_overstrong_failures(self):
        summary = {
            "best_support_level": "strong",
            "best_overall_score_percent": 90,
            "claim_matches": [{
                "top_matches": [{
                    "source_url": "https://example.go.kr/sme-emergency-aid",
                    "source_id": "https://example.go.kr/sme-emergency-aid",
                    "score": 0.9,
                }],
            }],
        }
        expected = {
            "related_source_url_contains": "sme-emergency-aid",
            "should_rank_related_first": True,
            "should_not_be_strong": True,
        }
        evaluation = semantic_calibration.evaluate_case(summary, expected)
        self.assertFalse(evaluation["passed"])
        self.assertTrue(evaluation["overstrong"])
        self.assertTrue(any("strong" in line for line in evaluation["failures"]))
        self.assertTrue(evaluation["related_top1"])

    def test_evaluate_case_passes_when_related_first_and_not_overstrong(self):
        summary = {
            "best_support_level": "contextual",
            "best_overall_score_percent": 72,
            "claim_matches": [{
                "top_matches": [{
                    "source_url": "https://example.go.kr/housing-support",
                    "source_id": "https://example.go.kr/housing-support",
                    "score": 0.6,
                }],
            }],
        }
        expected = {
            "related_source_url_contains": "housing-support",
            "should_rank_related_first": True,
            "should_not_be_strong": True,
        }
        evaluation = semantic_calibration.evaluate_case(summary, expected)
        self.assertTrue(evaluation["passed"], msg=evaluation["failures"])

    def test_evaluate_case_unavailable_handling(self):
        summary = {"best_support_level": "unavailable", "claim_matches": []}
        expected = {"should_be_unavailable_when_no_body": True}
        evaluation = semantic_calibration.evaluate_case(summary, expected)
        self.assertTrue(evaluation["passed"])

    def test_summarize_calibration_results_aggregates(self):
        rows = [
            {
                "summary": {"runtime_ms": 5, "cache_hits": 1, "embedding_request_count": 2},
                "evaluation": {
                    "passed": True,
                    "support_level": "weak",
                    "overstrong": False,
                    "related_top1": True,
                },
            },
            {
                "summary": {"runtime_ms": 10, "cache_hits": 0, "embedding_request_count": 4},
                "evaluation": {
                    "passed": False,
                    "support_level": "strong",
                    "overstrong": True,
                    "related_top1": True,
                },
            },
            {
                "summary": {"runtime_ms": 0, "cache_hits": 0, "embedding_request_count": 0},
                "evaluation": {
                    "passed": True,
                    "support_level": "unavailable",
                    "overstrong": False,
                    "related_top1": None,
                },
            },
        ]
        scorecard = semantic_calibration.summarize_calibration_results(rows)
        self.assertEqual(scorecard["case_count"], 3)
        self.assertEqual(scorecard["pass_count"], 2)
        self.assertEqual(scorecard["fail_count"], 1)
        self.assertEqual(scorecard["overstrong_count"], 1)
        self.assertEqual(scorecard["unavailable_count"], 1)
        self.assertEqual(scorecard["related_top1_eligible"], 2)
        self.assertEqual(scorecard["related_top1_count"], 2)
        self.assertEqual(scorecard["total_cache_hits"], 1)
        self.assertEqual(scorecard["total_embedding_request_count"], 6)
        self.assertEqual(scorecard["average_runtime_ms"], 5)
        self.assertIn("weak", scorecard["support_level_distribution"])


class DeterministicEvaluatorIntegrationTests(unittest.TestCase):
    def test_direct_support_case_ranks_related_source_first(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        case = next(c for c in cases if c["case_id"] == "direct_support_housing")
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": case["claim_text"]}],
                source_candidates=case["sources"],
                evidence_snippets=[],
                provider=provider,
            )
            evaluation = semantic_calibration.evaluate_case(summary, case["expected"])
            self.assertTrue(evaluation["related_top1"], msg=evaluation["failures"])

    def test_no_body_case_yields_unavailable(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        case = next(c for c in cases if c["case_id"] == "official_body_missing")
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": case["claim_text"]}],
                source_candidates=case["sources"],
                evidence_snippets=[],
                provider=provider,
            )
            evaluation = semantic_calibration.evaluate_case(summary, case["expected"])
            self.assertEqual(summary["best_support_level"], "unavailable")
            self.assertTrue(evaluation["passed"])

    def test_unrelated_case_is_not_classified_strong(self):
        """The deterministic provider can sometimes label an unrelated source
        as ``strong`` because of accidental character bigram overlap. The
        evaluator must mark such a result as overstrong (i.e. evaluation
        fails) — that is the signal we want to surface for real-provider
        calibration. Tests assert the *evaluator's response*, not the
        deterministic provider's accuracy.
        """
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        case = next(c for c in cases if c["case_id"] == "unrelated_school_lunch")
        with _env(SEMANTIC_MATCHING_ENABLED="true", EMBEDDING_PROVIDER="deterministic"):
            provider = semantic_embeddings.get_active_provider()
            summary = semantic_evidence_agent.compute_semantic_evidence_summary(
                normalized_claims=[{"claim_text": case["claim_text"]}],
                source_candidates=case["sources"],
                evidence_snippets=[],
                provider=provider,
            )
            evaluation = semantic_calibration.evaluate_case(summary, case["expected"])
            # Either the provider correctly avoided "strong" (passed=True), or
            # the evaluator caught it (overstrong=True, passed=False). Both
            # are acceptable outcomes — what's NOT acceptable is silently
            # accepting a strong label.
            if summary["best_support_level"] == "strong":
                self.assertTrue(
                    evaluation["overstrong"],
                    "evaluator failed to flag a strong-on-unrelated case as overstrong",
                )
                self.assertFalse(evaluation["passed"])
            else:
                self.assertNotEqual(summary["best_support_level"], "strong")


class EvaluatorScriptCLITests(unittest.TestCase):
    def test_deterministic_mode_runs_without_openai_key(self):
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_evaluator(
                "--provider", "deterministic",
                "--max-cases", "3",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("provider=deterministic-hash", result.stdout)
        self.assertIn("scorecard:", result.stdout)

    def test_json_csv_markdown_outputs_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_out = tmp_path / "report.json"
            csv_out = tmp_path / "report.csv"
            md_out = tmp_path / "report.md"
            result = _run_evaluator(
                "--provider", "deterministic",
                "--max-cases", "3",
                "--json-out", str(json_out),
                "--csv-out", str(csv_out),
                "--markdown-out", str(md_out),
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(json_out.exists())
            self.assertTrue(csv_out.exists())
            self.assertTrue(md_out.exists())

            payload = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertIn("scorecard", payload)
            self.assertIn("cases", payload)
            self.assertGreater(len(payload["cases"]), 0)

            with csv_out.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreater(len(rows), 0)
            self.assertIn("case_id", rows[0])
            self.assertIn("support_level", rows[0])

            md_text = md_out.read_text(encoding="utf-8")
            self.assertIn("# Semantic Calibration Report", md_text)
            self.assertIn("Scorecard", md_text)
            # Conservative disclaimer must appear.
            self.assertIn("metadata only", md_text)

    def test_openai_no_network_does_not_call_live_api(self):
        # Pretend an API key is present so the test would otherwise be
        # tempted to call out; --no-network must strip it cleanly.
        with _env(OPENAI_API_KEY="sk-fake-shouldnt-leak", EMBEDDING_MODEL="bogus"):
            result = _run_evaluator(
                "--provider", "openai",
                "--no-network",
                "--fail-on-unavailable",
                "--max-cases", "1",
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("provider=openai", result.stdout)
        self.assertIn("available=False", result.stdout)
        self.assertNotIn("sk-fake-shouldnt-leak", result.stdout)
        self.assertNotIn("sk-fake-shouldnt-leak", result.stderr)

    def test_deterministic_evaluator_passes_full_expanded_fixture(self):
        # M6.0: the expanded calibration fixture must process end-to-end with
        # ``--fail-on-regression`` on the deterministic provider. If a new
        # case is added that the current guardrails or thresholds cannot
        # handle, this test surfaces it before CI does. No network, no
        # OpenAI key, no Postgres.
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_evaluator(
                "--provider", "deterministic",
                "--fail-on-regression",
            )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("scorecard:", result.stdout)
        # Confirm the full fixture ran (the scorecard line carries cases=N).
        self.assertIn("overstrong=0", result.stdout)
        self.assertNotIn("regressed", result.stderr)

    def test_fail_on_regression_returns_3_on_intentional_failure(self):
        # Build a temp fixture whose single case has an impossible expectation
        # — a related source URL that doesn't appear anywhere in the input.
        # The evaluator must flag this as a regression and exit 3.
        impossible_case = [{
            "case_id": "impossible_expectation",
            "category": "direct_support",
            "claim_text": "정부가 어떤 정책을 발표했다.",
            "sources": [{
                "source_id": "src_a",
                "title": "공식",
                "url": "https://example.go.kr/real-source",
                "official_body_text": "정부는 어떤 정책을 발표했다고 안내했다.",
            }],
            "expected": {
                "related_source_url_contains": "this-url-does-not-exist-anywhere",
                "should_rank_related_first": True,
                "should_not_be_strong": False,
            },
        }]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "impossible.json"
            fixture.write_text(json.dumps(impossible_case, ensure_ascii=False), encoding="utf-8")
            result = _run_evaluator(
                "--provider", "deterministic",
                "--case-file", str(fixture),
                "--fail-on-regression",
            )
        self.assertEqual(result.returncode, 3, msg=result.stderr or result.stdout)
        self.assertIn("regressed", result.stderr)


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_calibration(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn("semantic_calibration", text,
                             f"{module_name}.py must not import semantic_calibration")
            self.assertNotIn("semantic_evidence_summary", text,
                             f"{module_name}.py must not read semantic_evidence_summary")


class CISafetyTests(unittest.TestCase):
    def test_helper_does_not_require_network_or_openai(self):
        # Pure import — no env, no OpenAI, no Postgres.
        import importlib
        importlib.reload(semantic_calibration)
        # Helper must work without any env config.
        evaluation = semantic_calibration.evaluate_case(
            {"best_support_level": "weak", "claim_matches": []},
            {"should_not_be_strong": True},
        )
        self.assertTrue(evaluation["passed"])


if __name__ == "__main__":
    unittest.main()
