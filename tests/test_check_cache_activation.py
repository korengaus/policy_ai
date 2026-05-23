"""Tests for ``scripts/check_cache_activation.py`` (M13.3c).

Run with: python tests/test_check_cache_activation.py

No real Render calls. The 2-run activation check is exercised via
``--simulate`` variants and via :func:`unittest.mock.patch` on the
internal smoke runner.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "check_cache_activation_cli",
        str(_PROJECT_ROOT / "scripts" / "check_cache_activation.py"),
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
# CLI parsing
# ---------------------------------------------------------------------------


class CliArgumentTests(unittest.TestCase):
    def test_help_exits_zero(self):
        rc, out, _ = _run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("check_cache_activation", out)
        self.assertIn("Exit codes", out)

    def test_missing_base_url_exits_two(self):
        rc, _, err = _run_cli(["--query", "q"])
        self.assertEqual(rc, 2)
        self.assertIn("base-url", err)

    def test_missing_query_exits_two(self):
        rc, _, err = _run_cli(["--base-url", "http://x"])
        self.assertEqual(rc, 2)
        self.assertIn("query", err)


# ---------------------------------------------------------------------------
# Classifier thresholds
# ---------------------------------------------------------------------------


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()

    def test_ratio_well_below_effective_threshold_returns_ok(self):
        # 60/120 = 0.5 -> below 0.75 -> OK
        v = self.module._classify(120.0, 60.0)
        self.assertEqual(v["verdict"], "ok")
        self.assertEqual(v["verdict_label"], "OK")
        self.assertGreater(v["speedup_percent"], 25.0)

    def test_ratio_well_above_ineffective_threshold_returns_warn(self):
        # 110/120 = 0.916... -> above 0.85 -> WARN
        v = self.module._classify(120.0, 110.0)
        self.assertEqual(v["verdict"], "warn")
        self.assertEqual(v["verdict_label"], "WARN")
        self.assertLess(v["speedup_percent"], 15.0)

    def test_ratio_in_middle_returns_ambiguous(self):
        # 96/120 = 0.8 -> in [0.75, 0.85] -> AMBIGUOUS
        v = self.module._classify(120.0, 96.0)
        self.assertEqual(v["verdict"], "ambiguous")
        self.assertEqual(v["verdict_label"], "AMBIGUOUS")

    def test_boundary_just_below_075_is_ok(self):
        v = self.module._classify(100.0, 74.9)
        self.assertEqual(v["verdict"], "ok")

    def test_boundary_just_above_085_is_warn(self):
        v = self.module._classify(100.0, 85.1)
        self.assertEqual(v["verdict"], "warn")

    def test_zero_cold_returns_insufficient_data(self):
        v = self.module._classify(0.0, 60.0)
        self.assertEqual(v["verdict"], "insufficient_data")

    def test_none_elapsed_returns_insufficient_data(self):
        v = self.module._classify(None, 60.0)
        self.assertEqual(v["verdict"], "insufficient_data")
        v = self.module._classify(120.0, None)
        self.assertEqual(v["verdict"], "insufficient_data")


# ---------------------------------------------------------------------------
# Simulate variants — end-to-end via the CLI
# ---------------------------------------------------------------------------


class SimulateVariantTests(unittest.TestCase):
    def test_simulate_default_produces_ok_verdict(self):
        rc, out, _ = _run_cli([
            "--base-url", "http://fake", "--query", "q",
            "--simulate", "--json",
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["verdict"]["verdict_label"], "OK")
        self.assertGreater(data["verdict"]["speedup_percent"], 25.0)

    def test_simulate_ineffective_produces_warn(self):
        rc, out, _ = _run_cli([
            "--base-url", "http://fake", "--query", "q",
            "--simulate-ineffective", "--json",
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["verdict"]["verdict_label"], "WARN")

    def test_simulate_ambiguous_produces_ambiguous(self):
        rc, out, _ = _run_cli([
            "--base-url", "http://fake", "--query", "q",
            "--simulate-ambiguous", "--json",
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["verdict"]["verdict_label"], "AMBIGUOUS")


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def _invoke_main_with_capture(module, argv):
    """Run ``module.main(argv)`` directly on a pre-loaded module
    instance (so patches survive) and capture stdout/stderr."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out_buf, err_buf
        rc = module.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out_buf.getvalue(), err_buf.getvalue()


class FailureHandlingTests(unittest.TestCase):
    def test_cold_run_failure_exits_one(self):
        module = _load_cli_module()

        def fake_cold(args, run_index=None):
            return {
                "status": "fail",
                "elapsed_seconds": None,
                "final_status": None,
                "exit_code": 1,
                "error": "synthetic cold failure",
            }

        with patch.object(
            module, "_run_smoke_once_simulated", fake_cold,
        ):
            rc, _, err = _invoke_main_with_capture(module, [
                "--base-url", "http://fake", "--query", "q",
                "--simulate",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("Cold smoke", err)

    def test_warm_run_failure_exits_one_with_partial_result(self):
        module = _load_cli_module()
        cold_result = {
            "status": "pass",
            "elapsed_seconds": 120.0,
            "final_status": "completed",
            "exit_code": 0,
        }
        warm_result = {
            "status": "fail",
            "elapsed_seconds": None,
            "final_status": None,
            "exit_code": 1,
            "error": "synthetic warm failure",
        }

        results = iter([cold_result, warm_result])

        def fake_run(args, run_index=None):
            return next(results)

        with patch.object(
            module, "_run_smoke_once_simulated", fake_run,
        ):
            rc, _, err = _invoke_main_with_capture(module, [
                "--base-url", "http://fake", "--query", "q",
                "--simulate",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("Warm smoke", err)


# ---------------------------------------------------------------------------
# Static module shape
# ---------------------------------------------------------------------------


class StaticShapeTests(unittest.TestCase):
    def setUp(self):
        self.source = (
            _PROJECT_ROOT / "scripts" / "check_cache_activation.py"
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
                    f"check_cache_activation.py must not import "
                    f"{needle!r} at module level"
                ),
            )

    def test_no_render_env_var_lookups(self):
        for needle in (
            "RENDER_API_KEY", "RENDER_SERVICE_ID",
        ):
            self.assertNotIn(
                needle, self.source,
                msg=(
                    f"check_cache_activation.py must not reference "
                    f"{needle}"
                ),
            )


# ---------------------------------------------------------------------------
# Thresholds are exported constants — protects against tightening drift
# ---------------------------------------------------------------------------


class ThresholdConstantsTests(unittest.TestCase):
    def test_threshold_values_match_brief(self):
        module = _load_cli_module()
        self.assertAlmostEqual(
            module.RATIO_EFFECTIVE_THRESHOLD, 0.75,
        )
        self.assertAlmostEqual(
            module.RATIO_INEFFECTIVE_THRESHOLD, 0.85,
        )


if __name__ == "__main__":
    unittest.main()
