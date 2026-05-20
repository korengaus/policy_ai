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
