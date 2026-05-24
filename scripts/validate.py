"""Run the full Phase 2 validation suite locally.

Mirrors the CI workflow's compile + offline test steps so contributors can
verify changes before pushing. Prints each command, stops on the first
failure, and exits with the failing command's return code.

Works on Windows PowerShell and Linux/macOS shells alike — commands are
launched via ``subprocess`` with ``shell=False`` so quoting is consistent
across platforms.

NOTE (M13.0): This script defines the canonical local validation flow.
``.github/workflows/ci.yml`` runs this script as the primary CI check.
If a check belongs in CI, add it here. Do not duplicate logic in the
workflow YAML — CI must mirror local.
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
    _normalize_log_format_for_determinism()
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
         "postgres_storage.py", "scripts/check_postgres_health.py",
         # M12.0b — Postgres backfill.
         "postgres_backfill.py", "scripts/run_postgres_backfill.py",
         # M13.1a — LLM Judge infrastructure (dry-run only).
         "llm_judge.py", "scripts/dry_run_llm_judge.py",
         # M13.2a — frontend build pipeline.
         "frontend/build_index.py",
         # M13.3a — shared HTTP cache infrastructure (disabled by default).
         "http_cache.py", "scripts/check_http_cache.py",
         # M14.0a — structured logging foundation (opt-in via LOG_FORMAT).
         "structured_logging.py", "scripts/check_logging.py",
         # M14.2 — JSON logging production activation tooling.
         "scripts/check_json_logging.py",
         # M14.3a — request ID propagation infrastructure.
         "request_context.py",
         # M13.3c — cache measurement + activation tooling.
         "scripts/measure_cache_impact.py",
         "scripts/check_cache_activation.py"],
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
        # M11.4b — dead-duplicate removal pin. AST-level uniqueness +
        # signature + behavioral pins for verification_card.
        # _missing_context_specific. The dead L491 copy was deleted;
        # this test catches any future re-introduction.
        [python, "tests/test_verification_card_dedup.py"],
        # M11.5 — audit-identified dead-code removal pins. Items 1-3
        # were deleted (evidence_comparator dup branch,
        # extract_evidence_snippets claim_evidence_map double-build,
        # frontend renderResultsLegacy + buildReportTextLegacy). Item 4
        # (OFFICIAL_DOMAIN_QUERY_HINTS) was deferred; the test pins
        # current behavior so a future cleanup can confirm scope.
        [python, "tests/test_dead_code_removal.py"],
        # M11.3 — read-only audit of the M11.1 candidate list. Compile
        # + --help smoke confirms the script loads; the test suite
        # pins idempotency, atomic-write, Korean round-trip, and the
        # read-only contract (no review_tasks ever created by the
        # audit script).
        [python, "-m", "compileall", "scripts/audit_legacy_enrollment.py"],
        [python, "scripts/audit_legacy_enrollment.py", "--help"],
        [python, "tests/test_audit_legacy_enrollment.py"],
        # M11.2 — centralized Korean keyword constants (read-move refactor).
        [python, "tests/test_korean_constants.py"],
        # M12.0a — Postgres dual-write foundation.
        [python, "scripts/check_postgres_health.py", "--help"],
        [python, "tests/test_postgres_storage.py"],
        # M12.0b — Postgres backfill CLI + tests.
        [python, "scripts/run_postgres_backfill.py", "--help"],
        [python, "scripts/run_postgres_backfill.py", "--status"],
        [python, "tests/test_postgres_backfill.py"],
        # M13.1a — LLM Judge dry-run CLI + tests.
        [python, "scripts/dry_run_llm_judge.py", "--help"],
        [python, "scripts/dry_run_llm_judge.py", "--status"],
        [python, "tests/test_llm_judge.py"],
        # M13.2a / M13.2b — frontend build pipeline. --check enforces
        # the byte-identical guarantee between frontend/ source (now
        # template.html + styles/main.css + scripts/main.js) and the
        # committed web/index.html artifact. Drift here fails CI.
        [python, "frontend/build_index.py", "--check"],
        [python, "tests/test_frontend_build.py"],
        # M13.3a — shared HTTP cache infrastructure. --help and
        # --status are read-only smokes; the tests pin the cache's
        # safety contract (never raises, never integrated with the
        # pipeline, no real HTTP traffic).
        [python, "scripts/check_http_cache.py", "--help"],
        [python, "scripts/check_http_cache.py", "--status"],
        [python, "tests/test_http_cache.py"],
        # M13.3b — HTTP cache integration into official_crawler
        # (feature-flagged, default off). The byte-identicality
        # regression pin lives in CacheOffByteIdentityTests.
        [python, "tests/test_official_crawler_cache.py"],
        # M13.3d — cache expansion to official_source_body +
        # news_collector. Each has its own feature flag and its own
        # CacheOffByteIdentityTests regression pin.
        [python, "-m", "compileall", "official_source_body.py", "news_collector.py"],
        [python, "tests/test_official_source_body_cache.py"],
        [python, "tests/test_news_collector_cache.py"],
        # M13.3c — cache measurement + activation tooling.
        # --help smokes confirm the scripts load and parse args;
        # the unit tests exercise the parser, verdict thresholds,
        # and simulate paths without hitting Render.
        [python, "scripts/measure_cache_impact.py", "--help"],
        [python, "scripts/check_cache_activation.py", "--help"],
        [python, "tests/test_measure_cache_impact.py"],
        [python, "tests/test_check_cache_activation.py"],
        # M14.0b — print() -> structured logging migration on the top
        # 5 files. AST + token pin that prints are gone in the M14.0b
        # set + in the M14.0c set (formerly 'deferred', now migrated).
        [python, "tests/test_print_migration.py"],
        # M14.0c — completes the migration on the remaining 8 files.
        # Subprocess-invokes verdict test suites to prove invariance.
        [python, "tests/test_print_migration_m14_0c.py"],
        # M14.4 — log level reclassification: AST pins that no log.error
        # call is a field-name reporter, that known false-positives are
        # log.info, and that known real errors are still log.error.
        [python, "tests/test_log_level_reclassification.py"],
        # M14.2 — JSON logging production activation tooling.
        [python, "scripts/check_json_logging.py", "--help"],
        [python, "tests/test_check_json_logging.py"],
        # M14.3a — request ID propagation (contextvars + FastAPI
        # middleware). Backward-compatible: no request_id field in
        # JSON output when the ContextVar is unset.
        [python, "tests/test_request_context.py"],
        [python, "tests/test_api_request_id_middleware.py"],
        # M14.3b — worker context propagation. Compile + the two
        # propagation/end-to-end suites. The compileall ensures
        # job_manager still imports cleanly after the helpers were
        # added; the propagation suite pins concurrent isolation;
        # the end-to-end suite pins the middleware → worker JSON
        # log line path.
        [python, "-m", "compileall", "job_manager.py"],
        [python, "tests/test_job_request_id_propagation.py"],
        [python, "tests/test_end_to_end_request_id_through_job.py"],
        # M14.0a — structured logging foundation. --help and --status
        # are read-only smokes; the test suite pins module-adoption
        # for the 10 M13.x modules, legacy-isolation for 18 untouched
        # files, and JSON shape / Korean-text preservation.
        [python, "scripts/check_logging.py", "--help"],
        [python, "scripts/check_logging.py", "--status"],
        [python, "tests/test_structured_logging.py"],
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


def _normalize_log_format_for_determinism() -> None:
    """M14.0a — clear ``LOG_FORMAT`` from the environment before
    running subprocesses so JSON-mode output cannot pollute the
    text-parsing output parsers used by smoke checks. The structured
    logging tests exercise the JSON toggle internally via env-scope
    helpers; CI and validate.py runs always behave as text-mode.
    """
    raw = os.environ.get("LOG_FORMAT")
    if raw and raw.strip(" \t").lower() == "json":
        print(
            "[validate] LOG_FORMAT=json detected -- clearing for "
            "deterministic validation run. The structured-logging tests "
            "exercise the JSON toggle via env-scope helpers internally."
        )
    # Unconditionally clear so every child subprocess sees the same
    # baseline.
    os.environ.pop("LOG_FORMAT", None)


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
