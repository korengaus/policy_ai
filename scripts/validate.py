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
    _assert_dual_write_disabled_for_determinism()
    return [
        [python, "-m", "compileall", "api_server.py", "database.py", "job_manager.py",
         "source_crawler.py", "scripts/fetch_registry_source.py",
         "scripts/enable_registry_source.py",
         "artifact_extractor.py", "scripts/extract_artifact_text.py",
         "artifact_evidence_linker.py", "scripts/link_artifact_evidence.py",
         "verdict_producer_comparison.py",
         "scripts/compare_verdict_producers.py",
         "verdict_label_diagnostic.py",
         "scripts/diagnose_verdict_labels.py",
         "legacy_review_enrollment.py",
         "scripts/enroll_legacy_weak_verified.py",
         "korean_constants.py",
         # M12.0a — Postgres dual-write foundation.
         "postgres_storage.py", "scripts/check_postgres_health.py"],
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
        [python, "tests/test_review_api_token_gate_smoke.py"],
        [python, "tests/test_review_ui_local_demo.py"],
        [python, "tests/test_source_registry.py"],
        # M10.1 — URL classifier CLI smoke + assertions.
        [python, "scripts/classify_source_url.py", "--help"],
        [python, "tests/test_source_url_classifier.py"],
        # M10.2 — static crawler + operator CLI (dry-run only).
        [python, "scripts/fetch_registry_source.py", "--help"],
        [python, "tests/test_source_crawler.py"],
        # M10.3 — operator enable workflow CLI (offline, list smoke + tests).
        [python, "scripts/enable_registry_source.py", "--help"],
        [python, "scripts/enable_registry_source.py", "--list"],
        [python, "tests/test_enable_registry_source.py"],
        # M10.4 — artifact text extractor + operator CLI (offline tests).
        [python, "scripts/extract_artifact_text.py", "--help"],
        [python, "tests/test_artifact_extractor.py"],
        # M10.5 — evidence candidate linker + operator CLI (offline tests).
        [python, "scripts/link_artifact_evidence.py", "--help"],
        [python, "tests/test_artifact_evidence_linker.py"],
        # M11.0a — verdict producer comparison tool (offline tests).
        [python, "scripts/compare_verdict_producers.py", "--help"],
        [python, "tests/test_verdict_producer_comparison.py"],
        # M11.0b — verdict label branch diagnostic (offline tests).
        [python, "scripts/diagnose_verdict_labels.py", "--help"],
        [python, "scripts/diagnose_verdict_labels.py", "--branch-table"],
        [python, "tests/test_verdict_label_diagnostic.py"],
        # M11.0c — B08 conservative fix pin tests (offline, fast).
        [python, "tests/test_verdict_label_b08_fix.py"],
        # M11.1 — legacy weak-verified review-queue enrollment.
        [python, "scripts/enroll_legacy_weak_verified.py", "--help"],
        [python, "tests/test_legacy_review_enrollment.py"],
        # M11.2 — centralized Korean keyword constants (read-move refactor).
        [python, "tests/test_korean_constants.py"],
        # M12.0a — Postgres dual-write foundation.
        [python, "scripts/check_postgres_health.py", "--help"],
        [python, "tests/test_postgres_storage.py"],
        [npm, "test"],
    ]


def _assert_dual_write_disabled_for_determinism() -> None:
    """M12.0a — validate.py must run with dual-write disabled so the
    test suite is byte-deterministic regardless of operator env. If
    ``USE_POSTGRES_WRITE`` is set to anything other than an explicit
    "false"/empty value, refuse to start; the operator has likely left
    a local DATABASE_URL pointed at a real Postgres and a deterministic
    validation run is no longer guaranteed.
    """
    raw = os.environ.get("USE_POSTGRES_WRITE", "").strip().lower()
    if raw not in ("", "false", "0", "no", "off"):
        raise SystemExit(
            "[validate] refusing to run with USE_POSTGRES_WRITE="
            f"{raw!r}. Set USE_POSTGRES_WRITE=false (or unset it) to "
            "keep validation runs deterministic; the dual-write tests "
            "exercise the toggle internally."
        )


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
