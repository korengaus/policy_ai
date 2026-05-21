"""Phase 2 M7.5: operational automation runner tests.

Verifies:
    * the runner's --help exits cleanly without env / network,
    * each profile resolves to the expected commands,
    * output parsers handle realistic stdout samples (validate.py,
      smoke_async_job, smoke_semantic_canary, historical),
    * dry-run never executes commands and still writes the report,
    * stop-on-fail / fail-on-warn classification works,
    * the runner never imports verdict / database modules and never
      requires OPENAI_API_KEY / network / Postgres.

CI safety: all tests use either the runner's own pure functions or an
injected fake subprocess runner — no real subprocess spawn, no live
server, no live OpenAI.
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
from typing import List


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNNER_SCRIPT = ROOT / "scripts" / "run_operational_checks.py"

import scripts.run_operational_checks as runner_module  # noqa: E402


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


def _argv_to_args(*argv: str):
    """Parse a list of CLI tokens with the runner's argparser."""
    return runner_module._build_parser().parse_args(list(argv))


# ---------------------------------------------------------------------------
# CLI / help
# ---------------------------------------------------------------------------


class CLITests(unittest.TestCase):
    def test_help_exits_cleanly(self):
        with _env(OPENAI_API_KEY=None):
            result = subprocess.run(
                [sys.executable, str(RUNNER_SCRIPT), "--help"],
                capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        for token in ("--profile", "--dry-run", "--fail-on-warn",
                      "--include-secondary-query", "--base-url"):
            self.assertIn(token, result.stdout)
        # API keys must never appear in --help output.
        self.assertNotIn("sk-", result.stdout)
        self.assertNotIn("sk-", result.stderr)


# ---------------------------------------------------------------------------
# Profile → step resolution
# ---------------------------------------------------------------------------


def _step_names(steps: List[dict]) -> List[str]:
    return [s["name"] for s in steps]


class ProfileResolutionTests(unittest.TestCase):
    def test_quick_profile_includes_validate_only(self):
        steps = runner_module._resolve_steps(_argv_to_args("--profile", "quick"))
        names = _step_names(steps)
        self.assertEqual(names, ["validate"])

    def test_post_commit_profile_includes_validate_and_smoke(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "post-commit",
            "--base-url", "http://example.invalid",
        ))
        names = _step_names(steps)
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], "validate")
        self.assertTrue(names[1].startswith("smoke_async_job("))

    def test_render_baseline_profile_includes_smoke_and_canary(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "render-baseline",
            "--base-url", "http://example.invalid",
        ))
        names = _step_names(steps)
        # legacy smoke + baseline canary (no expect-enabled)
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith("smoke_async_job("))
        self.assertIn("baseline", names[1])

    def test_render_canary_profile_includes_expect_enabled_and_legacy(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "render-canary",
            "--base-url", "http://example.invalid",
        ))
        names = _step_names(steps)
        # canary expecting semantic enabled + legacy smoke
        self.assertEqual(len(names), 2)
        self.assertIn("expect-enabled", names[0])
        self.assertTrue(names[1].startswith("smoke_async_job("))
        # canary command must carry --expect-semantic-enabled and --expect-provider openai
        canary_cmd = " ".join(steps[0]["command"])
        self.assertIn("--expect-semantic-enabled", canary_cmd)
        self.assertIn("--expect-provider openai", canary_cmd)
        self.assertIn("--fail-on-semantic-unavailable", canary_cmd)

    def test_render_canary_with_secondary_query_runs_twice(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "render-canary",
            "--include-secondary-query",
        ))
        names = _step_names(steps)
        self.assertEqual(len(names), 3)
        self.assertIn("expect-enabled", names[0])
        self.assertIn("expect-enabled", names[1])
        self.assertTrue(names[2].startswith("smoke_async_job("))

    def test_review_local_profile_includes_only_smoke_review_workflow(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "review-local",
            "--base-url", "http://example.invalid",
        ))
        names = _step_names(steps)
        self.assertEqual(names, ["smoke_review_workflow(self-contained)"])
        # The step must not hit Render and must not be flagged as may-call-openai.
        self.assertFalse(steps[0]["hits_render"])
        self.assertFalse(steps[0]["may_call_openai"])
        # The command must reference the smoke script with --self-contained.
        cmd = " ".join(steps[0]["command"])
        self.assertIn("smoke_review_workflow.py", cmd)
        self.assertIn("--self-contained", cmd)
        # The runner must NOT pass any --base-url or token-related arg to the
        # smoke script — it is fully offline.
        self.assertNotIn("--base-url", cmd)
        self.assertNotIn("REVIEW_API_TOKEN", cmd)

    def test_review_local_profile_unaffected_by_skip_render_flag(self):
        # The review-local profile is offline; --skip-render must not drop it.
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "review-local", "--skip-render",
        ))
        names = _step_names(steps)
        self.assertEqual(names, ["smoke_review_workflow(self-contained)"])

    # M8.8 — review-exposure profile.
    def test_review_exposure_profile_resolves_to_exposure_smoke(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "review-exposure",
            "--base-url", "http://example.invalid",
        ))
        names = _step_names(steps)
        self.assertEqual(names, ["smoke_review_api_exposure(expect-disabled)"])
        # The single step hits Render (the supplied base URL) but does
        # NOT call OpenAI and does NOT require a token from the operator.
        self.assertTrue(steps[0]["hits_render"])
        self.assertFalse(steps[0]["may_call_openai"])
        cmd = " ".join(steps[0]["command"])
        self.assertIn("smoke_review_api_exposure.py", cmd)
        self.assertIn("--expect-disabled", cmd)
        self.assertIn("--base-url http://example.invalid", cmd)
        # The runner must NOT inject any token / Authorization arg —
        # the exposure smoke intentionally probes without a token.
        self.assertNotIn("REVIEW_API_TOKEN", cmd)
        self.assertNotIn("X-Review-Token", cmd)
        self.assertNotIn("Bearer", cmd)

    def test_review_exposure_profile_is_not_part_of_quick(self):
        # quick must never include the exposure smoke — quick is offline
        # and must not hit Render.
        steps = runner_module._resolve_steps(_argv_to_args("--profile", "quick"))
        names = _step_names(steps)
        self.assertNotIn("smoke_review_api_exposure(expect-disabled)", names)
        for step in steps:
            self.assertFalse(
                step["hits_render"],
                f"quick profile must not include any Render-hitting step "
                f"(got {step['name']!r})",
            )

    def test_review_exposure_profile_dropped_when_skip_render(self):
        # --skip-render takes the exposure step out (it would have hit
        # the base URL) — the runner then has zero steps for this profile.
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "review-exposure",
            "--base-url", "http://example.invalid",
            "--skip-render",
        ))
        self.assertEqual(steps, [])

    def test_review_exposure_is_a_known_profile(self):
        self.assertIn("review-exposure", runner_module.PROFILES)

    def test_historical_profile_includes_dry_run_and_optional_eval(self):
        steps = runner_module._resolve_steps(_argv_to_args("--profile", "historical"))
        names = _step_names(steps)
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], "historical_dry_run")
        self.assertEqual(names[1], "historical_deterministic_eval")

    def test_full_profile_includes_validate_canary_legacy_historical(self):
        steps = runner_module._resolve_steps(_argv_to_args("--profile", "full"))
        names = _step_names(steps)
        # validate + canary + legacy + historical dry-run + historical eval
        self.assertEqual(names[0], "validate")
        self.assertTrue(any("expect-enabled" in n for n in names))
        self.assertTrue(any(n.startswith("smoke_async_job(") for n in names))
        self.assertIn("historical_dry_run", names)
        self.assertIn("historical_deterministic_eval", names)

    def test_skip_flags_remove_steps(self):
        steps = runner_module._resolve_steps(_argv_to_args(
            "--profile", "full", "--skip-render", "--skip-historical",
        ))
        names = _step_names(steps)
        self.assertEqual(names, ["validate"])


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------


class ParserTests(unittest.TestCase):
    def test_validate_pass_detected(self):
        parsed = runner_module._parse_validate_output(
            "[validate] $ python tests/test_x.py\n[validate] all checks passed\n",
            "", 0,
        )
        self.assertEqual(parsed["status"], "pass")

    def test_validate_fail_detected(self):
        parsed = runner_module._parse_validate_output("", "exited 1", 1)
        self.assertEqual(parsed["status"], "fail")

    def test_validate_unknown_when_passed_line_missing(self):
        # Exit 0 but the "all checks passed" line wasn't found in stdout.
        parsed = runner_module._parse_validate_output("some output", "", 0)
        self.assertEqual(parsed["status"], "unknown")

    def test_smoke_async_pass_detected(self):
        sample = (
            "[smoke] PASSED\n"
            "        final_status    = completed\n"
            "        result_summary  = status=ok job_status=completed result_source=cache "
            "results_count=1 has_stored_result=True\n"
            "        elapsed         = 5.6s\n"
        )
        parsed = runner_module._parse_smoke_async_output(sample, "", 0)
        self.assertEqual(parsed["status"], "pass")
        self.assertIn("passed=True", parsed["summary"])
        self.assertIn("final_status=completed", parsed["summary"])

    def test_smoke_async_fail_on_nonzero_exit(self):
        parsed = runner_module._parse_smoke_async_output("", "boom", 1)
        self.assertEqual(parsed["status"], "fail")

    def test_smoke_canary_pass_detected(self):
        sample = (
            "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
            "semantic_available=1 provider_errors=0 overstrong_like=0 "
            "cap_ratio=0.000 runtime_p95_ms=200 health=pass"
        )
        parsed = runner_module._parse_smoke_canary_output(sample, "", 0)
        self.assertEqual(parsed["status"], "pass")
        self.assertIn("health=pass", parsed["summary"])

    def test_smoke_canary_warn_detected(self):
        sample = (
            "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
            "semantic_available=1 provider_errors=0 overstrong_like=0 "
            "cap_ratio=0.0 runtime_p95_ms=7523 health=warn"
        )
        parsed = runner_module._parse_smoke_canary_output(sample, "", 0)
        self.assertEqual(parsed["status"], "warn")

    def test_smoke_canary_exit_2_means_unavailable_fail(self):
        # smoke_semantic_canary exits 2 when semantic is configured but
        # unavailable. The runner must surface this as fail.
        parsed = runner_module._parse_smoke_canary_output("", "FAIL: semantic", 2)
        self.assertEqual(parsed["status"], "fail")

    def test_smoke_canary_unknown_when_scorecard_missing(self):
        parsed = runner_module._parse_smoke_canary_output(
            "some unrelated output", "", 0,
        )
        self.assertEqual(parsed["status"], "unknown")
        # M8.4 — even with no scorecard the safety classification fields
        # must be present (default to clean / no rollback).
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "pass")
        self.assertEqual(metrics["semantic_runtime_status"], "pass")
        self.assertFalse(metrics["rollback_recommended"])
        self.assertEqual(metrics["rollback_reasons"], [])

    # -----------------------------------------------------------------------
    # M8.4 — semantic canary safety classification
    # -----------------------------------------------------------------------

    _RUNTIME_ONLY_WARN_FIXTURE = (
        "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
        "semantic_available=1 provider_errors=0 overstrong_like=0 "
        "cap_ratio=0.000 runtime_p95_ms=7523 health=warn"
    )

    _CLEAN_PASS_FIXTURE = (
        "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
        "semantic_available=1 provider_errors=0 overstrong_like=0 "
        "cap_ratio=0.000 runtime_p95_ms=400 health=pass"
    )

    def test_canary_runtime_only_warn_is_not_rollback(self):
        parsed = runner_module._parse_smoke_canary_output(
            self._RUNTIME_ONLY_WARN_FIXTURE, "", 0,
        )
        # Step status preserves the smoke's own warn classification — this
        # is intentionally not promoted to fail.
        self.assertEqual(parsed["status"], "warn")
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "pass")
        self.assertEqual(metrics["semantic_runtime_status"], "warn")
        self.assertFalse(metrics["rollback_recommended"])
        self.assertEqual(metrics["rollback_reasons"], [])
        # warn_only_reasons should mention the runtime threshold trip.
        joined_warns = " ".join(metrics["warn_only_reasons"])
        self.assertIn("runtime_p95_ms", joined_warns)
        # Summary surfaces the classification flags explicitly.
        self.assertIn("semantic_safety_status=pass", parsed["summary"])
        self.assertIn("rollback_recommended=false", parsed["summary"])

    def test_canary_runtime_only_warn_next_actions_say_no_rollback(self):
        parsed = runner_module._parse_smoke_canary_output(
            self._RUNTIME_ONLY_WARN_FIXTURE, "", 0,
        )
        record = {
            "name": "smoke_semantic_canary(전세사기/expect-enabled)",
            "status": parsed["status"],
            "metrics": parsed["metrics"],
        }
        actions = runner_module._next_actions("warn", [record])
        joined = "\n".join(actions)
        self.assertIn("runtime-only warn", joined)
        self.assertIn("no rollback recommended", joined)
        # Hard rollback message must NOT appear.
        self.assertNotIn("Roll back the Render semantic env vars", joined)

    def test_canary_provider_errors_force_rollback(self):
        sample = (
            "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
            "semantic_available=1 provider_errors=1 overstrong_like=0 "
            "cap_ratio=0.000 runtime_p95_ms=400 health=fail"
        )
        parsed = runner_module._parse_smoke_canary_output(sample, "", 0)
        self.assertEqual(parsed["status"], "fail")
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "fail")
        self.assertTrue(metrics["rollback_recommended"])
        joined_reasons = " ".join(metrics["rollback_reasons"])
        self.assertIn("provider_errors=1", joined_reasons)
        actions = runner_module._next_actions("fail", [
            {"name": "smoke_semantic_canary(전세사기/expect-enabled)",
             "status": "fail", "metrics": metrics},
        ])
        joined = "\n".join(actions)
        self.assertIn("rollback_recommended=true", joined)
        self.assertIn("provider_errors=1", joined)
        self.assertIn("Roll back the Render semantic env vars", joined)

    def test_canary_overstrong_like_forces_rollback(self):
        sample = (
            "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
            "semantic_available=1 provider_errors=0 overstrong_like=1 "
            "cap_ratio=0.000 runtime_p95_ms=400 health=fail"
        )
        parsed = runner_module._parse_smoke_canary_output(sample, "", 0)
        self.assertEqual(parsed["status"], "fail")
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "fail")
        self.assertTrue(metrics["rollback_recommended"])
        joined_reasons = " ".join(metrics["rollback_reasons"])
        self.assertIn("overstrong_like=1", joined_reasons)

    def test_canary_semantic_unavailable_while_expected_forces_rollback(self):
        # Health=warn but the safety classifier should promote to fail
        # because semantic_enabled=1 with semantic_available=0 means the
        # canary was meant to measure semantic and the provider isn't
        # answering.
        sample = (
            "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
            "semantic_available=0 provider_errors=0 overstrong_like=0 "
            "cap_ratio=0.000 runtime_p95_ms=400 health=warn"
        )
        parsed = runner_module._parse_smoke_canary_output(sample, "", 0)
        # Promoted to fail by the classifier.
        self.assertEqual(parsed["status"], "fail")
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "fail")
        self.assertTrue(metrics["rollback_recommended"])
        joined_reasons = " ".join(metrics["rollback_reasons"])
        self.assertIn("semantic_available=0", joined_reasons)

    def test_canary_exit_2_records_rollback_reason(self):
        # Even with an empty scorecard line, exit 2 means semantic was
        # expected enabled but unavailable.
        parsed = runner_module._parse_smoke_canary_output("", "FAIL: semantic", 2)
        self.assertEqual(parsed["status"], "fail")
        metrics = parsed["metrics"]
        self.assertTrue(metrics["rollback_recommended"])
        joined_reasons = " ".join(metrics["rollback_reasons"])
        self.assertIn("expected enabled but unavailable", joined_reasons)

    def test_canary_clean_pass_has_no_rollback_and_runtime_pass(self):
        parsed = runner_module._parse_smoke_canary_output(
            self._CLEAN_PASS_FIXTURE, "", 0,
        )
        self.assertEqual(parsed["status"], "pass")
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "pass")
        self.assertEqual(metrics["semantic_runtime_status"], "pass")
        self.assertFalse(metrics["rollback_recommended"])
        self.assertEqual(metrics["rollback_reasons"], [])
        self.assertEqual(metrics["warn_only_reasons"], [])

    def test_canary_cap_ratio_warn_is_warn_only(self):
        sample = (
            "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
            "semantic_available=1 provider_errors=0 overstrong_like=0 "
            "cap_ratio=0.850 runtime_p95_ms=400 health=warn"
        )
        parsed = runner_module._parse_smoke_canary_output(sample, "", 0)
        self.assertEqual(parsed["status"], "warn")
        metrics = parsed["metrics"]
        self.assertEqual(metrics["semantic_safety_status"], "pass")
        self.assertEqual(metrics["semantic_runtime_status"], "warn")
        self.assertFalse(metrics["rollback_recommended"])
        joined_warns = " ".join(metrics["warn_only_reasons"])
        self.assertIn("cap_ratio", joined_warns)

    def test_review_local_pass_detected(self):
        # Realistic smoke output: human summary + JSON tail.
        sample = (
            "[smoke-review] self-contained run\n"
            "  disabled_check         : True\n"
            "  token_check            : True\n"
            "  passed=True\n"
            "{\n"
            "  \"mode\": \"self-contained\",\n"
            "  \"passed\": true,\n"
            "  \"disabled_check\": {\"passed\": true},\n"
            "  \"token_check\": {\"passed\": true},\n"
            "  \"task_creation_check\": {\"passed\": true},\n"
            "  \"idempotency_check\": {\"passed\": true},\n"
            "  \"list_detail_check\": {\"passed\": true},\n"
            "  \"decision_check\": {\"passed\": true},\n"
            "  \"verdict_isolation_check\": {\"passed\": true},\n"
            "  \"publication_absent_check\": {\"passed\": true}\n"
            "}\n"
        )
        parsed = runner_module._parse_review_local_output(sample, "", 0)
        self.assertEqual(parsed["status"], "pass")
        self.assertIn("passed=True", parsed["summary"])
        # Metrics record each sub-check.
        self.assertTrue(parsed["metrics"]["verdict_isolation_check"])
        self.assertTrue(parsed["metrics"]["publication_absent_check"])

    def test_review_local_fail_detected_when_sub_check_fails(self):
        # Smoke ran end-to-end (exit_code=1) but a sub-check failed.
        sample = (
            "[smoke-review] self-contained run\n"
            "{\n"
            "  \"passed\": false,\n"
            "  \"disabled_check\": {\"passed\": true},\n"
            "  \"token_check\": {\"passed\": true},\n"
            "  \"task_creation_check\": {\"passed\": true},\n"
            "  \"idempotency_check\": {\"passed\": true},\n"
            "  \"list_detail_check\": {\"passed\": true},\n"
            "  \"decision_check\": {\"passed\": true},\n"
            "  \"verdict_isolation_check\": {\"passed\": false},\n"
            "  \"publication_absent_check\": {\"passed\": true}\n"
            "}\n"
        )
        parsed = runner_module._parse_review_local_output(sample, "", 1)
        self.assertEqual(parsed["status"], "fail")
        self.assertIn("verdict_isolation_check", parsed["summary"])

    def test_review_local_fail_on_unexpected_exit_code(self):
        # Anything other than 0/1 (e.g. 2 = bad CLI usage) is a hard fail.
        parsed = runner_module._parse_review_local_output("", "argparse complained", 2)
        self.assertEqual(parsed["status"], "fail")

    # M8.8 — review-exposure parser.
    def test_review_exposure_pass_detected(self):
        # Realistic exposure smoke output: human summary + JSON tail.
        sample = (
            "[exposure] base_url=https://policy-ai-q5ax.onrender.com\n"
            "[exposure] passed=True\n"
            "{\n"
            "  \"passed\": true,\n"
            "  \"base_url\": \"https://policy-ai-q5ax.onrender.com\",\n"
            "  \"expectation_mode\": \"expect-disabled\",\n"
            "  \"endpoints_checked\": 5,\n"
            "  \"public_access_detected\": false,\n"
            "  \"disabled_count\": 5,\n"
            "  \"token_required_count\": 0,\n"
            "  \"unexpected_count\": 0,\n"
            "  \"expectation_mismatch_count\": 0,\n"
            "  \"results\": [],\n"
            "  \"warnings\": [],\n"
            "  \"errors\": [],\n"
            "  \"recommendation\": \"PASS: every endpoint disabled\"\n"
            "}\n"
        )
        parsed = runner_module._parse_review_exposure_output(sample, "", 0)
        self.assertEqual(parsed["status"], "pass")
        metrics = parsed["metrics"]
        self.assertFalse(metrics["public_access_detected"])
        self.assertEqual(metrics["disabled_count"], 5)
        self.assertEqual(metrics["token_required_count"], 0)
        self.assertEqual(metrics["unexpected_count"], 0)
        self.assertEqual(metrics["expectation_mismatch_count"], 0)
        self.assertEqual(metrics["expectation_mode"], "expect-disabled")
        self.assertIn("PASS", metrics["recommendation"])

    def test_review_exposure_public_access_marks_status_fail(self):
        sample = (
            "{\n"
            "  \"passed\": false,\n"
            "  \"base_url\": \"http://example.invalid\",\n"
            "  \"expectation_mode\": \"expect-disabled\",\n"
            "  \"endpoints_checked\": 5,\n"
            "  \"public_access_detected\": true,\n"
            "  \"disabled_count\": 0,\n"
            "  \"token_required_count\": 0,\n"
            "  \"unexpected_count\": 0,\n"
            "  \"expectation_mismatch_count\": 0,\n"
            "  \"results\": [],\n"
            "  \"warnings\": [],\n"
            "  \"errors\": [],\n"
            "  \"recommendation\": \"FAIL: public-exposure incident\"\n"
            "}\n"
        )
        parsed = runner_module._parse_review_exposure_output(sample, "", 1)
        self.assertEqual(parsed["status"], "fail")
        self.assertTrue(parsed["metrics"]["public_access_detected"])
        self.assertIn("public_access_detected=True", parsed["summary"])

    def test_review_exposure_mismatch_in_expect_disabled_is_fail(self):
        # 5x 403 with expect-disabled → still safe (no public access)
        # but the operator's expectation didn't match. The runner
        # marks this as fail so the discrepancy doesn't get hidden.
        sample = (
            "{\n"
            "  \"passed\": false,\n"
            "  \"expectation_mode\": \"expect-disabled\",\n"
            "  \"endpoints_checked\": 5,\n"
            "  \"public_access_detected\": false,\n"
            "  \"disabled_count\": 0,\n"
            "  \"token_required_count\": 5,\n"
            "  \"unexpected_count\": 0,\n"
            "  \"expectation_mismatch_count\": 5,\n"
            "  \"results\": [], \"warnings\": [], \"errors\": [],\n"
            "  \"recommendation\": \"MISMATCH: review API enabled\"\n"
            "}\n"
        )
        parsed = runner_module._parse_review_exposure_output(sample, "", 1)
        self.assertEqual(parsed["status"], "fail")
        self.assertFalse(parsed["metrics"]["public_access_detected"])
        self.assertEqual(parsed["metrics"]["expectation_mismatch_count"], 5)

    def test_review_exposure_falls_back_when_json_missing(self):
        parsed = runner_module._parse_review_exposure_output("no json here", "", 1)
        self.assertEqual(parsed["status"], "fail")
        self.assertIn("JSON summary not detected", parsed["summary"])

    def test_historical_dry_run_pass_detected(self):
        sample = (
            "[build-historical] reports_scanned=471 sqlite_rows=105 "
            "candidates=739 emitted=100 skipped=0 elapsed=1.18s anonymized=True"
        )
        parsed = runner_module._parse_historical_dry_run_output(sample, "", 0)
        self.assertEqual(parsed["status"], "pass")
        self.assertIn("emitted=100", parsed["summary"])

    def test_historical_eval_pass_detected(self):
        sample = (
            "[evaluate] scorecard: cases=100 pass=100 fail=0 related_top1=72/72 "
            "overstrong=0 unavailable=28 avg_runtime_ms=3"
        )
        parsed = runner_module._parse_historical_eval_output(sample, "", 0)
        self.assertEqual(parsed["status"], "pass")

    def test_historical_eval_warn_when_overstrong_positive(self):
        sample = (
            "[evaluate] scorecard: cases=100 pass=95 fail=5 related_top1=70/72 "
            "overstrong=2 unavailable=28 avg_runtime_ms=3"
        )
        parsed = runner_module._parse_historical_eval_output(sample, "", 0)
        # fail > 0 OR overstrong > 0 → warn
        self.assertEqual(parsed["status"], "warn")


# ---------------------------------------------------------------------------
# Orchestration with injected fake runner
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records every command requested and returns scripted responses."""

    def __init__(self, scripted_outputs: List[tuple]):
        # scripted_outputs is a list of (exit_code, stdout, stderr) tuples
        # — one per call. Will raise IndexError if more calls happen
        # than scripts provided, so tests catch unexpected commands.
        self._scripted = list(scripted_outputs)
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str]) -> tuple:
        self.calls.append(list(cmd))
        if not self._scripted:
            raise AssertionError(
                f"unexpected extra command (no scripted output left): {cmd}"
            )
        return self._scripted.pop(0)


class OrchestrationTests(unittest.TestCase):
    def test_dry_run_does_not_execute_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "render-canary",
                "--include-secondary-query",
                "--dry-run",
                "--json-out", str(Path(tmp) / "out.json"),
                "--markdown-out", str(Path(tmp) / "out.md"),
                "--no-default-reports",
            )
            fake = FakeRunner([])  # no scripted outputs — must not be called
            report = runner_module.run(args, runner=fake)
            self.assertEqual(report["overall_status"], "pass")
            self.assertEqual(len(fake.calls), 0)
            for r in report["commands"]:
                self.assertEqual(r["status"], "skipped")
            self.assertTrue((Path(tmp) / "out.json").exists())
            self.assertTrue((Path(tmp) / "out.md").exists())

    def test_quick_profile_runs_validate_and_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "quick",
                "--json-out", str(Path(tmp) / "out.json"),
                "--markdown-out", str(Path(tmp) / "out.md"),
                "--no-default-reports",
            )
            fake = FakeRunner([(0, "[validate] all checks passed\n", "")])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(report["overall_status"], "pass")
            self.assertEqual(report["commands"][0]["status"], "pass")

    def test_fail_command_marks_overall_fail_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "post-commit",
                "--base-url", "http://example.invalid",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
            )
            # validate fails → second step (smoke) must not run.
            fake = FakeRunner([(1, "", "boom")])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(report["overall_status"], "fail")
            self.assertEqual(report["commands"][0]["status"], "fail")
            self.assertEqual(len(report["commands"]), 1)

    def test_warn_status_does_not_stop_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "render-canary",
                "--base-url", "http://example.invalid",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
            )
            canary_warn = (
                "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
                "semantic_available=1 provider_errors=0 overstrong_like=0 "
                "cap_ratio=0.0 runtime_p95_ms=7523 health=warn"
            )
            legacy_pass = (
                "[smoke] PASSED\n"
                "        final_status    = completed\n"
                "        result_summary  = status=ok results_count=1\n"
                "        elapsed         = 5.6s\n"
            )
            fake = FakeRunner([
                (0, canary_warn, ""),  # canary warn
                (0, legacy_pass, ""),  # legacy pass
            ])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(report["overall_status"], "warn")
            self.assertEqual(report["commands"][0]["status"], "warn")
            self.assertEqual(report["commands"][1]["status"], "pass")

    def test_review_local_orchestrates_with_injected_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "review-local",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
            )
            smoke_pass = (
                "[smoke-review] self-contained run\n"
                "{\n"
                "  \"passed\": true,\n"
                "  \"disabled_check\": {\"passed\": true},\n"
                "  \"token_check\": {\"passed\": true},\n"
                "  \"task_creation_check\": {\"passed\": true},\n"
                "  \"idempotency_check\": {\"passed\": true},\n"
                "  \"list_detail_check\": {\"passed\": true},\n"
                "  \"decision_check\": {\"passed\": true},\n"
                "  \"verdict_isolation_check\": {\"passed\": true},\n"
                "  \"publication_absent_check\": {\"passed\": true}\n"
                "}\n"
            )
            fake = FakeRunner([(0, smoke_pass, "")])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(report["overall_status"], "pass")
            self.assertEqual(report["commands"][0]["status"], "pass")
            # The injected command must NOT carry any Render base URL or
            # token argument — review-local is fully offline.
            executed = " ".join(fake.calls[0])
            self.assertIn("smoke_review_workflow.py", executed)
            self.assertIn("--self-contained", executed)
            self.assertNotIn("--base-url", executed)
            self.assertNotIn("REVIEW_API_TOKEN", executed)

    def test_review_exposure_orchestrates_with_injected_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "review-exposure",
                "--base-url", "http://example.invalid",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
            )
            exposure_pass = (
                "[exposure] passed=True\n"
                "{\n"
                "  \"passed\": true,\n"
                "  \"expectation_mode\": \"expect-disabled\",\n"
                "  \"endpoints_checked\": 5,\n"
                "  \"public_access_detected\": false,\n"
                "  \"disabled_count\": 5,\n"
                "  \"token_required_count\": 0,\n"
                "  \"unexpected_count\": 0,\n"
                "  \"expectation_mismatch_count\": 0,\n"
                "  \"results\": [], \"warnings\": [], \"errors\": [],\n"
                "  \"recommendation\": \"PASS: every endpoint disabled\"\n"
                "}\n"
            )
            fake = FakeRunner([(0, exposure_pass, "")])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(report["overall_status"], "pass")
            self.assertEqual(report["commands"][0]["status"], "pass")
            # The injected command must NOT carry any token argument.
            executed = " ".join(fake.calls[0])
            self.assertIn("smoke_review_api_exposure.py", executed)
            self.assertIn("--expect-disabled", executed)
            self.assertNotIn("REVIEW_API_TOKEN", executed)
            self.assertNotIn("X-Review-Token", executed)

    def test_review_exposure_public_access_surfaces_rollback_hint(self):
        # When the exposure smoke flags public_access_detected the
        # runner's next_actions must lead with a specific rollback hint.
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "review-exposure",
                "--base-url", "http://example.invalid",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
            )
            exposure_fail = (
                "{\n"
                "  \"passed\": false,\n"
                "  \"expectation_mode\": \"expect-disabled\",\n"
                "  \"endpoints_checked\": 5,\n"
                "  \"public_access_detected\": true,\n"
                "  \"disabled_count\": 4,\n"
                "  \"token_required_count\": 0,\n"
                "  \"unexpected_count\": 0,\n"
                "  \"expectation_mismatch_count\": 0,\n"
                "  \"results\": [], \"warnings\": [], \"errors\": [],\n"
                "  \"recommendation\": \"FAIL: public-exposure incident\"\n"
                "}\n"
            )
            fake = FakeRunner([(1, exposure_fail, "")])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(report["overall_status"], "fail")
            joined = " ".join(report["next_actions"])
            self.assertIn("PUBLIC EXPOSURE", joined)
            self.assertIn("REVIEW_API_ENABLED=false", joined)

    def test_main_exit_code_2_when_warn_and_fail_on_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "render-canary",
                "--base-url", "http://example.invalid",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
                "--fail-on-warn",
            )
            canary_warn = (
                "result_count=1 semantic_summary_count=1 semantic_enabled=1 "
                "semantic_available=1 provider_errors=0 overstrong_like=0 "
                "cap_ratio=0.0 runtime_p95_ms=7523 health=warn"
            )
            legacy_pass = (
                "[smoke] PASSED\n"
                "        final_status    = completed\n"
                "        result_summary  = results_count=1\n"
                "        elapsed         = 5.6s\n"
            )
            fake = FakeRunner([(0, canary_warn, ""), (0, legacy_pass, "")])
            report = runner_module.run(args, runner=fake)
            self.assertEqual(report["overall_status"], "warn")
            # main() turns warn + fail-on-warn into exit code 2.


# ---------------------------------------------------------------------------
# Report shape + markdown
# ---------------------------------------------------------------------------


class ReportShapeTests(unittest.TestCase):
    def test_json_report_has_expected_top_level_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "quick",
                "--json-out", str(Path(tmp) / "out.json"),
                "--no-default-reports",
            )
            fake = FakeRunner([(0, "[validate] all checks passed\n", "")])
            runner_module.run(args, runner=fake)
            payload = json.loads((Path(tmp) / "out.json").read_text(encoding="utf-8"))
            for key in ("profile", "started_at", "finished_at", "duration_seconds",
                        "base_url", "commands", "overall_status", "warnings",
                        "next_actions"):
                self.assertIn(key, payload)
            self.assertEqual(payload["overall_status"], "pass")

    def test_markdown_report_contains_command_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "quick",
                "--markdown-out", str(Path(tmp) / "out.md"),
                "--no-default-reports",
            )
            fake = FakeRunner([(0, "[validate] all checks passed\n", "")])
            runner_module.run(args, runner=fake)
            text = (Path(tmp) / "out.md").read_text(encoding="utf-8")
            self.assertIn("# Operational Check Report", text)
            self.assertIn("## Commands", text)
            self.assertIn("| step | status | exit | duration | summary |", text)
            self.assertIn("validate", text)
            # Conservative disclaimer must appear.
            self.assertIn("metadata only", text)
            # Must never claim verification.
            self.assertNotIn("verified", text.lower())


# ---------------------------------------------------------------------------
# CI safety + isolation
# ---------------------------------------------------------------------------


class CISafetyTests(unittest.TestCase):
    def test_runner_does_not_require_openai_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = _argv_to_args(
                "--profile", "quick",
                "--no-default-reports",
                "--json-out", str(Path(tmp) / "out.json"),
            )
            with _env(OPENAI_API_KEY=None, EMBEDDING_MODEL=None,
                      SEMANTIC_MATCHING_ENABLED=None, EMBEDDING_PROVIDER=None):
                fake = FakeRunner([(0, "[validate] all checks passed\n", "")])
                report = runner_module.run(args, runner=fake)
            self.assertEqual(report["overall_status"], "pass")

    def test_runner_does_not_import_database_or_verdict_modules(self):
        text = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import database", text)
        self.assertNotIn("import api_server", text)
        self.assertNotIn("import policy_decision", text)
        self.assertNotIn("import policy_scoring", text)
        self.assertNotIn("import verification_card", text)

    def test_runner_does_not_modify_render_env(self):
        # Static check: the runner must never call os.environ pop/set on
        # the Render-side env vars. (Inline subprocess env injection is
        # OK if needed — but the runner does not need it because it
        # delegates to existing scripts that already handle env.)
        text = RUNNER_SCRIPT.read_text(encoding="utf-8")
        for needle in (
            'os.environ["SEMANTIC_MATCHING_ENABLED"]',
            'os.environ["EMBEDDING_PROVIDER"]',
            'os.environ["OPENAI_API_KEY"]',
        ):
            self.assertNotIn(needle, text,
                             f"runner must not mutate {needle}")


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_runner(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "run_operational_checks", text,
                f"{module_name}.py must not import run_operational_checks",
            )


if __name__ == "__main__":
    unittest.main()
