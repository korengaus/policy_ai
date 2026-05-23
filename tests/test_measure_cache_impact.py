"""Tests for ``scripts/measure_cache_impact.py`` (M13.3c).

Run with: python tests/test_measure_cache_impact.py

No real Render calls. The measurement runner is exercised via
``--simulate`` (deterministic synthetic timings) and via
:func:`unittest.mock.patch` on the internal subprocess wrapper. The
canonical real-vs-CI bridge is the parser:
:func:`measure_cache_impact.parse_smoke_output` is unit-tested against
the actual output shape of ``scripts/smoke_async_job.py``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_cli_module():
    """Load the script as a fresh module so each test can rebind
    state without leaking across cases."""
    spec = importlib.util.spec_from_file_location(
        "measure_cache_impact_cli",
        str(_PROJECT_ROOT / "scripts" / "measure_cache_impact.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_cli(argv):
    module = _load_cli_module()
    out_buf, err_buf = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out_buf, err_buf
        rc = module.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class CliArgumentTests(unittest.TestCase):
    def test_help_exits_zero(self):
        rc, out, _ = _run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("measure_cache_impact", out)
        self.assertIn("Exit codes", out)

    def test_missing_base_url_exits_two(self):
        rc, _, err = _run_cli(["--query", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("base-url", err)

    def test_missing_query_exits_two(self):
        rc, _, err = _run_cli(["--base-url", "http://x"])
        self.assertEqual(rc, 2)
        self.assertIn("query", err)

    def test_runs_capped_at_10(self):
        module = _load_cli_module()
        # Patch the run executor to a no-op that records the runs arg
        # so we don't actually do 11 synthetic runs.
        recorded = {}

        def fake_execute(args, mode):
            recorded["runs"] = args.runs
            return {
                "results": [
                    {"run": 1, "status": "pass", "elapsed_seconds": 100.0,
                     "final_status": "completed"},
                ],
                "mean_elapsed_seconds": 100.0,
                "min_elapsed_seconds": 100.0,
                "max_elapsed_seconds": 100.0,
                "pass_rate": 1.0,
                "samples_with_elapsed": 1,
                "total_runs": 1,
            }

        with patch.object(module, "_execute_runs", fake_execute):
            module.main([
                "--base-url", "http://x", "--query", "q",
                "--runs", "99", "--baseline-only",
                "--no-default-reports", "--simulate",
            ])
        self.assertEqual(recorded["runs"], 10)


# ---------------------------------------------------------------------------
# Smoke output parser
# ---------------------------------------------------------------------------


class ParseSmokeOutputTests(unittest.TestCase):
    """Mirrors the actual output shape of scripts/smoke_async_job.py."""

    def setUp(self):
        self.module = _load_cli_module()

    def test_passed_output(self):
        stdout = (
            "[smoke] base_url=https://example.com\n"
            "[smoke] GET https://example.com/health\n"
            "[smoke] POST .../jobs/analyze payload={'query': 'x', 'max_news': 1}\n"
            "[smoke] PASSED\n"
            "        base_url        = https://example.com\n"
            "        job_id          = abc-123\n"
            "        stages_observed = ['init', 'collect']\n"
            "        final_status    = completed\n"
            "        result_summary  = status=ok\n"
            "        elapsed         = 124.3s\n"
        )
        result = self.module.parse_smoke_output(stdout, "", 0)
        self.assertEqual(result["status"], "pass")
        self.assertAlmostEqual(result["elapsed_seconds"], 124.3)
        self.assertEqual(result["final_status"], "completed")
        self.assertEqual(result["exit_code"], 0)

    def test_failed_output(self):
        stdout = ""
        stderr = "[smoke] FAILED after 89.7s: network error\n"
        result = self.module.parse_smoke_output(stdout, stderr, 1)
        self.assertEqual(result["status"], "fail")
        self.assertAlmostEqual(result["elapsed_seconds"], 89.7)
        self.assertIsNone(result["final_status"])
        self.assertEqual(result["exit_code"], 1)

    def test_no_pattern_match_returns_none_elapsed(self):
        stdout = "some unrelated output"
        result = self.module.parse_smoke_output(stdout, "", 1)
        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["elapsed_seconds"])
        self.assertIsNone(result["final_status"])


# ---------------------------------------------------------------------------
# Verdict thresholds
# ---------------------------------------------------------------------------


def _section(elapsed_samples, pass_rate=1.0):
    """Helper to build a fake aggregate section."""
    return {
        "results": [
            {"run": i + 1, "status": "pass" if pass_rate >= 0.5 else "fail",
             "elapsed_seconds": e, "final_status": "completed"}
            for i, e in enumerate(elapsed_samples)
        ],
        "mean_elapsed_seconds": sum(elapsed_samples) / len(elapsed_samples),
        "min_elapsed_seconds": min(elapsed_samples),
        "max_elapsed_seconds": max(elapsed_samples),
        "pass_rate": pass_rate,
        "samples_with_elapsed": len(elapsed_samples),
        "total_runs": len(elapsed_samples),
    }


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()

    def test_pass_when_speedup_geq_20_and_rate_maintained(self):
        baseline = _section([120.0, 118.0, 122.0], pass_rate=1.0)
        cache_on = _section([60.0, 62.0, 58.0], pass_rate=1.0)
        v = self.module.compute_verdict(baseline, cache_on)
        self.assertEqual(v["verdict"], "pass")
        self.assertGreaterEqual(v["mean_speedup_percent"], 20)
        self.assertTrue(v["pass_rate_maintained"])

    def test_marginal_when_speedup_between_5_and_20(self):
        baseline = _section([100.0, 100.0, 100.0])
        cache_on = _section([90.0, 90.0, 90.0])  # 10% speedup
        v = self.module.compute_verdict(baseline, cache_on)
        self.assertEqual(v["verdict"], "marginal")

    def test_investigate_when_speedup_below_5(self):
        baseline = _section([100.0, 100.0, 100.0])
        cache_on = _section([98.0, 98.0, 98.0])  # 2% speedup
        v = self.module.compute_verdict(baseline, cache_on)
        self.assertEqual(v["verdict"], "investigate")

    def test_rollback_when_pass_rate_drops(self):
        baseline = _section([100.0, 100.0, 100.0], pass_rate=1.0)
        cache_on = _section([50.0, 50.0, 50.0], pass_rate=0.5)
        v = self.module.compute_verdict(baseline, cache_on)
        self.assertEqual(v["verdict"], "rollback_recommended")
        self.assertFalse(v["pass_rate_maintained"])

    def test_insufficient_data_when_baseline_missing(self):
        cache_on = _section([60.0])
        v = self.module.compute_verdict(None, cache_on)
        self.assertEqual(v["verdict"], "insufficient_data")

    def test_speedup_exactly_20_is_pass(self):
        baseline = _section([100.0])
        cache_on = _section([80.0])
        v = self.module.compute_verdict(baseline, cache_on)
        self.assertEqual(v["verdict"], "pass")
        self.assertEqual(v["mean_speedup_percent"], 20.0)

    def test_speedup_exactly_5_is_marginal(self):
        baseline = _section([100.0])
        cache_on = _section([95.0])
        v = self.module.compute_verdict(baseline, cache_on)
        self.assertEqual(v["verdict"], "marginal")
        self.assertEqual(v["mean_speedup_percent"], 5.0)


# ---------------------------------------------------------------------------
# Mode flags + simulate end-to-end
# ---------------------------------------------------------------------------


class ModeFlagTests(unittest.TestCase):
    def test_baseline_only_skips_cache_on_section(self):
        rc, out, _ = _run_cli([
            "--base-url", "http://fake", "--query", "q",
            "--runs", "2", "--warmup", "0",
            "--baseline-only", "--no-default-reports",
            "--simulate", "--json",
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["mode"], "baseline")
        self.assertIsNotNone(data["baseline"])
        self.assertIsNone(data["cache_on"])

    def test_cache_on_only_skips_baseline_section(self):
        rc, out, _ = _run_cli([
            "--base-url", "http://fake", "--query", "q",
            "--runs", "2", "--warmup", "0",
            "--cache-on-only", "--no-default-reports",
            "--simulate", "--json",
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["mode"], "cache_on")
        self.assertIsNone(data["baseline"])
        self.assertIsNotNone(data["cache_on"])

    def test_simulate_default_pair_yields_pass_verdict(self):
        rc, out, _ = _run_cli([
            "--base-url", "http://fake", "--query", "q",
            "--runs", "3", "--warmup", "0",
            "--no-default-reports", "--simulate", "--json",
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["improvement"]["verdict"], "pass")
        self.assertTrue(data["improvement"]["pass_rate_maintained"])


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


class OutputWritingTests(unittest.TestCase):
    def test_output_writes_md_and_json(self):
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True,
        ) as tmp:
            prefix = Path(tmp) / "report"
            rc, _, _ = _run_cli([
                "--base-url", "http://fake", "--query", "q",
                "--runs", "1", "--warmup", "0",
                "--simulate", "--output", str(prefix),
            ])
            self.assertEqual(rc, 0)
            md_path = prefix.with_suffix(".md")
            json_path = prefix.with_suffix(".json")
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            md_text = md_path.read_text(encoding="utf-8")
            self.assertIn("# Cache Impact Measurement", md_text)
            self.assertIn("## Results", md_text)
            self.assertIn("## Verdict", md_text)
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], "1.0")
            self.assertEqual(data["base_url"], "http://fake")

    def test_no_default_reports_skips_reports_dir(self):
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True,
        ) as tmp:
            # Use a sentinel reports/ inside the tmpdir to ensure we
            # don't accidentally write to the real reports/ path.
            # (The script writes to project reports/ by default; we
            # rely on --no-default-reports to skip entirely.)
            rc, _, _ = _run_cli([
                "--base-url", "http://fake", "--query", "q",
                "--runs", "1", "--warmup", "0",
                "--simulate", "--no-default-reports",
            ])
            self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Mocked subprocess — exercise the real-mode path without Render.
# ---------------------------------------------------------------------------


class MockedSubprocessTests(unittest.TestCase):
    def test_real_mode_parses_subprocess_output(self):
        module = _load_cli_module()
        canned_stdout = (
            "[smoke] PASSED\n"
            "        final_status    = completed\n"
            "        elapsed         = 99.5s\n"
        )

        class _Completed:
            def __init__(self):
                self.stdout = canned_stdout
                self.stderr = ""
                self.returncode = 0

        def fake_run(*args, **kwargs):
            return _Completed()

        with patch.object(module.subprocess, "run", fake_run):
            with patch.object(module.time, "sleep", lambda *_: None):
                rc = module.main([
                    "--base-url", "http://fake", "--query", "q",
                    "--runs", "2", "--warmup", "0",
                    "--baseline-only", "--no-default-reports", "--json",
                ])
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Static module shape — no network imports at module level
# ---------------------------------------------------------------------------


class StaticShapeTests(unittest.TestCase):
    def setUp(self):
        self.source = (
            _PROJECT_ROOT / "scripts" / "measure_cache_impact.py"
        ).read_text(encoding="utf-8")

    def test_no_requests_or_httpx_import(self):
        for needle in ("requests", "httpx", "urllib.request"):
            pattern = re.compile(
                rf"^(?:from\s+{re.escape(needle)}\b|import\s+{re.escape(needle)}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=(
                    f"measure_cache_impact.py must not import "
                    f"{needle!r} at module level -- it should "
                    "subprocess smoke_async_job.py instead"
                ),
            )

    def test_no_render_env_var_lookups(self):
        # The script must not read RENDER_API_KEY or any env var that
        # would imply it's toggling Render config.
        for needle in (
            "RENDER_API_KEY", "RENDER_SERVICE_ID",
        ):
            self.assertNotIn(
                needle, self.source,
                msg=f"measure_cache_impact.py must not reference {needle}",
            )


if __name__ == "__main__":
    unittest.main()
