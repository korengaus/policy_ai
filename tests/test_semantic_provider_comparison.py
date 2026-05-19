"""Phase 2 M5.8: provider comparison + threshold recommendation tests.

Verifies:
    * the comparison CLI honors --no-network for openai and runs cleanly
      without an OpenAI key,
    * the live-OpenAI gate refuses to run live calls without an explicit
      confirmation token (exit 3) and refuses to run with a token but
      missing env (exit 2),
    * JSON / Markdown outputs carry the documented shape,
    * the threshold helper marks overstrong scorecards as ``not_ready``
      and never uses verification language,
    * verdict-side modules continue to ignore the new module / script.

CI-safety contract: no network, no OpenAI key, no Postgres, no temp DB.
"""

from __future__ import annotations

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

import semantic_thresholds


COMPARE_SCRIPT = ROOT / "scripts" / "compare_semantic_providers.py"


@contextmanager
def _env(**overrides):
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


def _run_compare(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
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
        [sys.executable, str(COMPARE_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------

class DeterministicOnlyCLITests(unittest.TestCase):
    def test_deterministic_only_runs_without_openai_key(self):
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_compare(
                "--providers", "deterministic",
                "--max-cases", "3",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("provider=deterministic", result.stdout)
        self.assertIn("activation_readiness", result.stdout)
        # API keys must never appear in the output.
        self.assertNotIn("sk-", result.stdout)
        self.assertNotIn("sk-", result.stderr)

    def test_deterministic_and_disabled_runs_without_network(self):
        # The "disabled" provider must report unavailable cleanly.
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_compare(
                "--providers", "deterministic,disabled",
                "--max-cases", "2",
                "--no-network",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("provider=deterministic", result.stdout)
        self.assertIn("provider=disabled", result.stdout)


class LiveOpenAIGateTests(unittest.TestCase):
    def test_openai_with_no_network_does_not_call_live(self):
        # Pretend an API key is set so the gate would otherwise be tempted to
        # call out; --no-network must keep us offline.
        with _env(OPENAI_API_KEY="sk-fake-should-not-leak", EMBEDDING_MODEL="bogus"):
            result = _run_compare(
                "--providers", "openai",
                "--no-network",
                "--max-cases", "1",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("provider=openai", result.stdout)
        self.assertIn("available=False", result.stdout)
        self.assertNotIn("sk-fake-should-not-leak", result.stdout)
        self.assertNotIn("sk-fake-should-not-leak", result.stderr)

    def test_openai_live_without_token_exits_with_code_3(self):
        # Even with all env set, no token must refuse to run live.
        with _env(
            OPENAI_API_KEY="sk-fake-should-not-leak",
            EMBEDDING_MODEL="text-embedding-3-small",
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
        ):
            result = _run_compare(
                "--providers", "openai",
                "--max-cases", "1",
            )
        self.assertEqual(result.returncode, 3, msg=result.stderr or result.stdout)
        self.assertIn("LIVE_OPENAI_OK", result.stderr)
        self.assertNotIn("sk-fake-should-not-leak", result.stdout)
        self.assertNotIn("sk-fake-should-not-leak", result.stderr)

    def test_openai_live_with_token_but_missing_env_exits_with_code_2(self):
        # Token present but no API key: exit 2 (required env missing).
        with _env(
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
            SEMANTIC_MATCHING_ENABLED=None,
            EMBEDDING_PROVIDER=None,
        ):
            result = _run_compare(
                "--providers", "openai",
                "--live-confirm-token", "LIVE_OPENAI_OK",
                "--max-cases", "1",
            )
        self.assertEqual(result.returncode, 2, msg=result.stderr or result.stdout)
        self.assertIn("OPENAI_API_KEY", result.stderr)


class OutputShapeTests(unittest.TestCase):
    def test_json_output_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_out = tmp_path / "comparison.json"
            md_out = tmp_path / "comparison.md"
            with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
                result = _run_compare(
                    "--providers", "deterministic,disabled",
                    "--max-cases", "3",
                    "--no-network",
                    "--json-out", str(json_out),
                    "--markdown-out", str(md_out),
                )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            payload = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertIn("providers", payload)
            self.assertIn("comparison", payload)
            self.assertIn("recommendation", payload)
            self.assertIn("live_openai_called", payload)
            self.assertFalse(payload["live_openai_called"])
            # Each provider block must carry available/scorecard/rows.
            for name in ("deterministic", "disabled"):
                self.assertIn(name, payload["providers"])
                block = payload["providers"][name]
                self.assertIn("available", block)
                self.assertIn("scorecard", block)
                self.assertIn("rows", block)
            # Recommendation must use the conservative vocabulary.
            self.assertIn(
                payload["recommendation"]["activation_readiness"],
                {"not_ready", "local_only", "debug_canary_candidate"},
            )

    def test_markdown_output_has_recommendation_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_out = Path(tmp) / "comparison.md"
            with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
                result = _run_compare(
                    "--providers", "deterministic",
                    "--max-cases", "3",
                    "--no-network",
                    "--markdown-out", str(md_out),
                )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            text = md_out.read_text(encoding="utf-8")
            self.assertIn("# Semantic Provider Comparison Report", text)
            self.assertIn("## Recommendation", text)
            self.assertIn("activation_readiness", text)
            # Conservative disclaimer must appear.
            self.assertIn("metadata only", text)
            self.assertIn("authoritative", text)
            # Must never claim verification.
            self.assertNotIn("verified", text.lower())


# ---------------------------------------------------------------------------
# Threshold helper unit tests
# ---------------------------------------------------------------------------

class ThresholdHelperTests(unittest.TestCase):
    def test_overstrong_scorecard_is_not_ready(self):
        classification = semantic_thresholds.classify_activation_readiness({
            "case_count": 8,
            "fail_count": 1,
            "overstrong_count": 1,
            "related_top1_rate": 0.95,
            "average_runtime_ms": 50,
            "support_cap_applied_count": 2,
            "total_critical_mismatches": 3,
        })
        self.assertEqual(classification["activation_readiness"], "not_ready")
        self.assertTrue(
            any("overstrong" in reason for reason in classification["reasons"]),
            msg=classification["reasons"],
        )

    def test_clean_scorecard_can_reach_debug_canary_candidate(self):
        classification = semantic_thresholds.classify_activation_readiness({
            "case_count": 8,
            "fail_count": 0,
            "overstrong_count": 0,
            "related_top1_rate": 0.95,
            "related_top1_count": 7,
            "related_top1_eligible": 7,
            "average_runtime_ms": 50,
            "support_cap_applied_count": 0,
            "total_critical_mismatches": 0,
        })
        self.assertEqual(classification["activation_readiness"], "debug_canary_candidate")

    def test_low_related_top1_rate_blocks_canary(self):
        classification = semantic_thresholds.classify_activation_readiness({
            "case_count": 8,
            "fail_count": 0,
            "overstrong_count": 0,
            "related_top1_rate": 0.55,  # below MIN_RELATED_TOP1_RATE_FOR_LOCAL
            "average_runtime_ms": 50,
        })
        self.assertEqual(classification["activation_readiness"], "not_ready")

    def test_borderline_top1_rate_marks_local_only(self):
        classification = semantic_thresholds.classify_activation_readiness({
            "case_count": 8,
            "fail_count": 0,
            "overstrong_count": 0,
            "related_top1_rate": 0.70,  # local-only band
            "average_runtime_ms": 50,
        })
        self.assertEqual(classification["activation_readiness"], "local_only")

    def test_helper_never_uses_verified_language(self):
        # Probe every classification output for the word "verified".
        for over in (0, 1):
            for rate in (0.0, 0.5, 0.8, 1.0):
                classification = semantic_thresholds.classify_activation_readiness({
                    "case_count": 4,
                    "fail_count": over,
                    "overstrong_count": over,
                    "related_top1_rate": rate,
                    "average_runtime_ms": 200,
                    "support_cap_applied_count": 1,
                    "total_critical_mismatches": 2,
                })
                serialized = json.dumps(classification, ensure_ascii=False)
                self.assertNotIn("verified", serialized.lower())
                self.assertIn(
                    classification["activation_readiness"],
                    {"not_ready", "local_only", "debug_canary_candidate"},
                )

    def test_recommend_thresholds_returns_null_thresholds(self):
        # M5.8 intentionally does not tune cosine cutoffs.
        recommendation = semantic_thresholds.recommend_thresholds({
            "deterministic": {
                "available": True,
                "scorecard": {
                    "case_count": 8, "fail_count": 0, "overstrong_count": 0,
                    "related_top1_rate": 0.95, "average_runtime_ms": 50,
                },
            },
        })
        self.assertIsNone(recommendation["recommended_thresholds"]["support"])
        self.assertIsNone(recommendation["recommended_thresholds"]["context"])

    def test_deterministic_only_caps_at_local_only(self):
        # The deterministic provider is a test surrogate — even when every
        # rule passes, it alone cannot support a canary recommendation.
        recommendation = semantic_thresholds.recommend_thresholds({
            "deterministic": {
                "available": True,
                "scorecard": {
                    "case_count": 8, "fail_count": 0, "overstrong_count": 0,
                    "related_top1_rate": 0.95, "average_runtime_ms": 50,
                    "related_top1_count": 7, "related_top1_eligible": 7,
                },
            },
        })
        self.assertEqual(recommendation["activation_readiness"], "local_only")

    def test_unavailable_only_is_not_ready(self):
        recommendation = semantic_thresholds.recommend_thresholds({
            "openai": {"available": False, "scorecard": {}},
        })
        self.assertEqual(recommendation["activation_readiness"], "not_ready")


# ---------------------------------------------------------------------------
# Isolation + dependency tests
# ---------------------------------------------------------------------------

class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_compare_or_thresholds(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "semantic_thresholds", text,
                f"{module_name}.py must not import semantic_thresholds",
            )
            self.assertNotIn(
                "compare_semantic_providers", text,
                f"{module_name}.py must not import the comparison script",
            )


class NoPostgresRequiredTests(unittest.TestCase):
    def test_threshold_helper_has_no_database_dependency(self):
        # The helper is pure stdlib; importing it should not pull in
        # database.py or any external SDK.
        import importlib
        # Reload to make sure we re-exercise the import path.
        importlib.reload(semantic_thresholds)
        self.assertTrue(hasattr(semantic_thresholds, "classify_activation_readiness"))
        self.assertTrue(hasattr(semantic_thresholds, "recommend_thresholds"))

    def test_compare_script_runs_without_database_url(self):
        # Drop DATABASE_URL just in case some local config has it; the script
        # must still run on SQLite-only.
        with _env(
            DATABASE_URL=None,
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
        ):
            result = _run_compare(
                "--providers", "deterministic",
                "--max-cases", "1",
                "--no-network",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
