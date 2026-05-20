"""Phase 2 M6.2: anonymized real-claim semantic evaluation batch tests.

Verifies:
    * the real-claim batch fixture is well-formed and has the expected
      shape, size, uniqueness, and mismatch coverage,
    * mismatch categories declare the guardrail flag the M5.7 layer
      should emit (number / date / eligibility / finality / negation),
    * the new evaluator script runs offline on the deterministic provider
      (no OpenAI key, no network, no Postgres),
    * ``--provider openai --no-network`` never makes a live call,
    * the explicit live-confirmation gate refuses live OpenAI runs
      without ``--live-confirm-token LIVE_OPENAI_OK`` (exit code 4),
    * JSON and Markdown outputs can be written to a temp dir,
    * verdict-side modules continue to ignore the new evaluator.

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


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "semantic_real_claim_batch_sample.json"
EVALUATOR_SCRIPT = ROOT / "scripts" / "evaluate_real_claim_batch.py"


# Categories where the fixture intentionally embeds a critical-fact
# disagreement the M5.7 guardrails should detect. Cases in these
# categories must declare the guardrail flag name in
# ``expected.risk_flags`` so the fixture is self-documenting about what
# the guardrails should catch.
GUARDRAIL_FLAG_BY_CATEGORY = {
    "number_mismatch": "number_mismatch",
    "date_mismatch": "date_mismatch",
    "eligibility_mismatch": "eligibility_mismatch",
    "finality_mismatch": "finality_mismatch",
    "negation_or_refutation": "negation_mismatch",
}

# Every category except ``direct_support`` is a calibration trap where
# the agent must NOT report a strong semantic support label. M6.2
# requires at least 50% of the real-claim batch to fall into one of
# these buckets so the scorecard exercises guardrails on realistic
# claim shapes, not just easy direct matches.
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

    def test_fixture_has_at_least_ten_cases(self):
        # M6.2 target: 10-20 cases. Below 10 the activation-readiness
        # signal is too noisy to act on.
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(
            len(cases), 10,
            msg=f"expected >= 10 real-claim cases; found {len(cases)}",
        )

    def test_case_ids_are_unique(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        ids = [case.get("case_id") for case in cases]
        duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
        self.assertFalse(duplicates, f"duplicate case_ids: {duplicates}")

    def test_each_case_has_required_fields(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for case in cases:
            cid = case.get("case_id", "(unnamed)")
            self.assertIn("case_id", case, msg=cid)
            self.assertIn("category", case, msg=cid)
            self.assertIn("claim_text", case, msg=cid)
            self.assertIn("sources", case, msg=cid)
            self.assertIsInstance(case["sources"], list, msg=cid)
            self.assertIn("expected", case, msg=cid)
            for source in case["sources"]:
                self.assertIn("source_id", source, msg=cid)
                self.assertIn("url", source, msg=cid)
                # ``official_body_text`` may be empty for no_body cases,
                # but the field itself must exist.
                self.assertIn("official_body_text", source, msg=cid)

    def test_at_least_fifty_percent_are_mismatch_traps(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        trap_count = sum(
            1 for case in cases if case.get("category") in MISMATCH_CATEGORIES
        )
        ratio = trap_count / len(cases) if cases else 0.0
        self.assertGreaterEqual(
            ratio, 0.50,
            msg=(
                f"only {trap_count}/{len(cases)} ({ratio:.0%}) cases fall in "
                f"mismatch / trap categories; expected >= 50%. "
                f"Trap categories considered: {sorted(MISMATCH_CATEGORIES)}"
            ),
        )

    def test_mismatch_cases_declare_expected_risk_flags(self):
        # For categories that map to a guardrail flag, the fixture must
        # declare the corresponding flag name. Categories without a
        # direct guardrail (``actor_mismatch``, ``local_vs_central_authority``,
        # ``partial_support``, ``same_topic_wrong_policy``) are tolerated —
        # their fixtures may carry documentation-only labels.
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

    def test_uses_synthetic_example_urls(self):
        # Real production URLs and real personal data must not leak in.
        # Every source URL must use an ``example.`` host so the fixture
        # is unambiguously synthetic.
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for case in cases:
            for source in case.get("sources") or []:
                url = source.get("url") or ""
                self.assertTrue(
                    "example." in url,
                    msg=(
                        f"case {case.get('case_id')!r}: source url {url!r} "
                        "must use an 'example.' synthetic host"
                    ),
                )


class EvaluatorScriptCLITests(unittest.TestCase):
    def test_deterministic_mode_runs_without_openai_key(self):
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_evaluator(
                "--provider", "deterministic",
                "--no-network",
                "--show-failures",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("provider=deterministic-hash", result.stdout)
        self.assertIn("scorecard:", result.stdout)
        # API key must never appear in stdout/stderr.
        self.assertNotIn("sk-", result.stdout)
        self.assertNotIn("sk-", result.stderr)

    def test_deterministic_mode_passes_full_fixture_with_fail_on_regression(self):
        # The real-claim batch must process end-to-end without any case
        # failing its expectations on the deterministic provider. If a
        # new case is added that the current guardrails or thresholds
        # cannot handle, this test surfaces it before CI does.
        with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
            result = _run_evaluator(
                "--provider", "deterministic",
                "--no-network",
                "--fail-on-regression",
            )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("overstrong=0", result.stdout)
        self.assertNotIn("regressed", result.stderr)

    def test_json_and_markdown_outputs_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_out = tmp_path / "report.json"
            md_out = tmp_path / "report.md"
            with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None):
                result = _run_evaluator(
                    "--provider", "deterministic",
                    "--no-network",
                    "--max-cases", "3",
                    "--json-out", str(json_out),
                    "--markdown-out", str(md_out),
                )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(json_out.exists())
            self.assertTrue(md_out.exists())

            payload = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertIn("scorecard", payload)
            self.assertIn("cases", payload)
            self.assertGreater(len(payload["cases"]), 0)
            for row in payload["cases"]:
                self.assertIn("case_id", row)
                self.assertIn("evaluation", row)

            md_text = md_out.read_text(encoding="utf-8")
            self.assertIn("Scorecard", md_text)
            # Conservative disclaimer must appear.
            self.assertIn("metadata only", md_text)

    def test_openai_no_network_does_not_call_live(self):
        # Even with a key present in the environment, ``--no-network``
        # must keep us offline and produce a clean unavailable result.
        with _env(OPENAI_API_KEY="sk-fake-shouldnt-leak", EMBEDDING_MODEL="bogus"):
            result = _run_evaluator(
                "--provider", "openai",
                "--no-network",
                "--fail-on-unavailable",
                "--max-cases", "1",
            )
        # ``--fail-on-unavailable`` returns 2 when the provider is offline.
        self.assertEqual(result.returncode, 2, msg=result.stderr or result.stdout)
        self.assertIn("provider=openai", result.stdout)
        self.assertIn("available=False", result.stdout)
        self.assertNotIn("sk-fake-shouldnt-leak", result.stdout)
        self.assertNotIn("sk-fake-shouldnt-leak", result.stderr)

    def test_live_openai_without_confirm_token_exits_with_code_4(self):
        # Even when every env var is set, the gate must refuse to run
        # live OpenAI without the explicit confirmation token.
        with _env(
            OPENAI_API_KEY="sk-fake-shouldnt-leak",
            EMBEDDING_MODEL="text-embedding-3-small",
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
        ):
            result = _run_evaluator(
                "--provider", "openai",
                "--max-cases", "1",
            )
        self.assertEqual(result.returncode, 4, msg=result.stderr or result.stdout)
        self.assertIn("LIVE_OPENAI_OK", result.stderr)
        # Key must never leak even on the refusal path.
        self.assertNotIn("sk-fake-shouldnt-leak", result.stdout)
        self.assertNotIn("sk-fake-shouldnt-leak", result.stderr)


class CISafetyTests(unittest.TestCase):
    def test_helper_module_is_importable_without_openai_or_db(self):
        # Importing the script must not require any env config or a
        # database connection. ``SEMANTIC_MATCHING_ENABLED`` and friends
        # are inspected lazily inside ``run_evaluation``.
        with _env(
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
            SEMANTIC_MATCHING_ENABLED=None,
            EMBEDDING_PROVIDER=None,
            DATABASE_URL=None,
        ):
            import importlib
            import scripts.evaluate_real_claim_batch as script
            importlib.reload(script)
            self.assertTrue(hasattr(script, "main"))
            self.assertEqual(script.LIVE_CONFIRM_TOKEN, "LIVE_OPENAI_OK")
            # Default fixture must point at the real-claim batch.
            self.assertEqual(
                script.DEFAULT_FIXTURE,
                ROOT / "tests" / "fixtures" / "semantic_real_claim_batch_sample.json",
            )

    def test_no_openai_key_required_for_default_invocation(self):
        # The deterministic + --no-network path is the CI path: it must
        # complete successfully without ``OPENAI_API_KEY`` anywhere.
        with _env(
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
            SEMANTIC_MATCHING_ENABLED=None,
            EMBEDDING_PROVIDER=None,
        ):
            result = _run_evaluator(
                "--provider", "deterministic",
                "--no-network",
                "--max-cases", "2",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_real_claim_evaluator(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "evaluate_real_claim_batch", text,
                f"{module_name}.py must not import evaluate_real_claim_batch",
            )
            self.assertNotIn(
                "semantic_real_claim_batch_sample", text,
                f"{module_name}.py must not read the real-claim batch fixture",
            )


if __name__ == "__main__":
    unittest.main()
