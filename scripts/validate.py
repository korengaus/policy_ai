"""Run the full Phase 2 validation suite locally.

Mirrors the CI workflow's compile + offline test steps so contributors can
verify changes before pushing. Prints each command, stops on the first
failure, and exits with the failing command's return code.

Works on Windows PowerShell and Linux/macOS shells alike — commands are
launched via ``subprocess`` with ``shell=False`` so quoting is consistent
across platforms.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parent.parent


def _npm_executable() -> str:
    # On Windows, ``npm`` is shipped as ``npm.cmd``; resolving via shutil.which
    # avoids the "[WinError 193] %1 is not a valid Win32 application" error
    # that subprocess raises when shell=False finds the bare ``npm`` shim.
    found = shutil.which("npm.cmd") if os.name == "nt" else None
    if found:
        return found
    found = shutil.which("npm")
    if found:
        return found
    return "npm.cmd" if os.name == "nt" else "npm"


def _commands() -> List[List[str]]:
    python = sys.executable or "python"
    npm = _npm_executable()
    return [
        [python, "-m", "compileall", "api_server.py", "database.py", "job_manager.py"],
        [python, "tests/test_jobs.py"],
        [python, "tests/test_postgres_dual_write.py"],
        [python, "tests/test_ai_reasoner_status.py"],
        [python, "tests/test_semantic_matching.py"],
        [python, "tests/test_semantic_activation.py"],
        [python, "tests/test_semantic_calibration.py"],
        [python, "tests/test_semantic_fact_guardrails.py"],
        [python, "tests/test_semantic_provider_comparison.py"],
        [python, "tests/test_semantic_real_claim_batch.py"],
        [python, "tests/test_historical_claim_batch_builder.py"],
        [python, "tests/test_semantic_canary_metrics.py"],
        [python, "tests/test_smoke_semantic_canary.py"],
        [python, "tests/test_operational_checks_runner.py"],
        [python, "tests/test_review_workflow.py"],
        [python, "tests/test_review_api.py"],
        [python, "tests/test_review_workflow_smoke.py"],
        [python, "tests/test_review_audit_trail.py"],
        [python, "tests/test_operator_preflight.py"],
        [python, "tests/test_review_bundle.py"],
        [python, "tests/test_review_api_exposure_smoke.py"],
        [npm, "test"],
    ]


def _format_command(cmd: List[str]) -> str:
    return " ".join(cmd)


def main() -> int:
    print(f"[validate] working directory: {ROOT}")
    for cmd in _commands():
        print(f"\n[validate] $ {_format_command(cmd)}")
        try:
            completed = subprocess.run(cmd, cwd=ROOT, check=False)
        except FileNotFoundError as error:
            print(f"[validate] FAILED — command not found: {error}", file=sys.stderr)
            return 127
        if completed.returncode != 0:
            print(
                f"[validate] FAILED — command exited with {completed.returncode}: "
                f"{_format_command(cmd)}",
                file=sys.stderr,
            )
            return completed.returncode
    print("\n[validate] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
