"""Phase 2 M7.2: smoke_semantic_canary script tests.

Tests only the pure / parser / argparse parts of the smoke script.
The HTTP request path is NOT exercised here — running a live server in
CI would require uvicorn + network. Instead we verify:

    * the script's ``--help`` exits cleanly without env / network,
    * the script imports cleanly without OPENAI_API_KEY,
    * the smoke script does not import database / verdict modules,
    * verdict modules don't reference the smoke script.

The metrics helper itself is covered exhaustively by
``tests/test_semantic_canary_metrics.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SMOKE_SCRIPT = ROOT / "scripts" / "smoke_semantic_canary.py"
ENV_SCRIPT = ROOT / "scripts" / "check_semantic_canary_env.py"


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


def _run(script: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
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
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


class SmokeScriptCLITests(unittest.TestCase):
    def test_help_runs_without_env(self):
        with _env(
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
            SEMANTIC_MATCHING_ENABLED=None,
            EMBEDDING_PROVIDER=None,
        ):
            result = _run(SMOKE_SCRIPT, "--help")
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("--base-url", result.stdout)
        self.assertIn("--fail-on-health-warn", result.stdout)
        self.assertIn("--fail-on-semantic-unavailable", result.stdout)
        self.assertIn("--expect-semantic-enabled", result.stdout)
        # API keys must never appear in --help output.
        self.assertNotIn("sk-", result.stdout)
        self.assertNotIn("sk-", result.stderr)

    def test_smoke_script_does_not_import_database(self):
        text = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import database", text)
        self.assertNotIn("import api_server", text)
        self.assertNotIn("import policy_decision", text)
        self.assertNotIn("import policy_scoring", text)
        self.assertNotIn("import verification_card", text)


class EnvCheckerCLITests(unittest.TestCase):
    def test_env_checker_runs_without_env(self):
        with _env(
            OPENAI_API_KEY=None,
            EMBEDDING_MODEL=None,
            SEMANTIC_MATCHING_ENABLED=None,
            EMBEDDING_PROVIDER=None,
        ):
            result = _run(ENV_SCRIPT)
        # Returns non-zero because env is intentionally missing.
        self.assertEqual(result.returncode, 1, msg=result.stderr or result.stdout)
        self.assertIn("ready_for_local_canary", result.stdout)
        self.assertIn("missing:", result.stdout)
        # Never print the API key value, never print "sk-".
        self.assertNotIn("sk-", result.stdout)
        self.assertNotIn("sk-", result.stderr)

    def test_env_checker_reports_ready_when_fully_configured(self):
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_MODEL="text-embedding-3-small",
            OPENAI_API_KEY="sk-fake-shouldnt-leak",
        ):
            result = _run(ENV_SCRIPT, "--require-openai")
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("ready_for_local_canary: True", result.stdout)
        # The script must NEVER echo the actual key value.
        self.assertNotIn("sk-fake-shouldnt-leak", result.stdout)
        self.assertNotIn("sk-fake-shouldnt-leak", result.stderr)
        # Length is OK to print (helps operator confirm shell isn't truncating).
        self.assertIn("OPENAI_API_KEY length:", result.stdout)

    def test_env_checker_warns_when_render_change_implied(self):
        with _env(
            SEMANTIC_MATCHING_ENABLED="true",
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_MODEL="text-embedding-3-small",
            OPENAI_API_KEY="sk-fake-shouldnt-leak",
        ):
            result = _run(ENV_SCRIPT)
        self.assertEqual(result.returncode, 0)
        self.assertIn("does not modify Render", result.stdout)

    def test_env_checker_script_does_not_import_database(self):
        text = ENV_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import database", text)
        self.assertNotIn("import api_server", text)
        self.assertNotIn("import policy_decision", text)


class VerdictIsolationTests(unittest.TestCase):
    def test_verdict_modules_do_not_reference_smoke_canary(self):
        for module_name in ("policy_decision", "policy_scoring", "verification_card"):
            module_path = ROOT / f"{module_name}.py"
            self.assertTrue(module_path.exists())
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "smoke_semantic_canary", text,
                f"{module_name}.py must not import smoke_semantic_canary",
            )
            self.assertNotIn(
                "check_semantic_canary_env", text,
                f"{module_name}.py must not import check_semantic_canary_env",
            )


if __name__ == "__main__":
    unittest.main()
