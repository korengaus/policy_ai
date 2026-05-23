"""Phase 2 M7.5: operational automation runner.

Bundles the standard post-change and canary-monitoring checks behind a
single CLI so operators stop typing the same five commands after every
milestone. The runner only orchestrates existing scripts (``validate.py``,
``smoke_async_job.py``, ``smoke_semantic_canary.py``,
``build_historical_claim_batch.py``, ``evaluate_real_claim_batch.py``) —
it never touches Render env, never modifies ``render.yaml``, never makes
production decisions on the operator's behalf.

Strict design contract:
    * Subprocess only. No new imports from verdict / scoring / agent
      modules.
    * No autonomous coding. No multi-agent orchestration.
    * No Redis / Celery / pgvector dependency.
    * Reports go under ``reports/`` (gitignored). Never committed.
    * API key is never printed or persisted.
    * The runner can fail loudly (default) or treat warnings as soft
      via ``--fail-on-warn`` for stricter CI-like use.

Profiles:
    quick           validate only — pre-commit local check
    post-commit     validate + legacy Render smoke
    render-baseline legacy + semantic canary (no semantic-enabled expectation)
    render-canary   semantic canary expecting openai + legacy smoke
    historical      historical batch dry-run + deterministic eval if file exists
    review-local    self-contained reviewer-workflow smoke (offline, no Render, no OpenAI)
    full            validate + render-canary + historical

Exit codes:
    0 — every step passed (or warn-only if ``--fail-on-warn`` not set)
    1 — at least one step failed
    2 — at least one step warned and ``--fail-on-warn`` was set
    130 — operator interrupt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


PROFILES = (
    "quick",
    "post-commit",
    "render-baseline",
    "render-canary",
    "historical",
    "review-local",
    "review-exposure",
    "review-token-gate",
    "source-registry",
    "source-crawler",
    "source-enable",
    "source-extractor",
    "source-linker",
    "verdict-comparison",
    "verdict-label-diagnostic",
    "legacy-review-enroll",
    "korean-constants",
    "postgres-dual-write",
    "postgres-backfill",
    "llm-judge-dry-run",
    "frontend-build",
    "http-cache",
    "official-crawler-cache",
    "cache-measurement-dry",
    "structured-logging",
    "print-migration",
    "json-logging-verification",
    "full",
)

# M9.5 — env var name the review-token-gate profile expects the
# operator to have set locally. Documented here (and in the profile
# step's command) so the runner never has to read the value itself.
REVIEW_TOKEN_GATE_DEFAULT_ENV = "REVIEW_API_SMOKE_TOKEN"

DEFAULT_BASE_URL = "https://policy-ai-q5ax.onrender.com"
DEFAULT_QUERY = "전세사기"
DEFAULT_SECONDARY_QUERY = "청년 월세"

# Number of stdout/stderr lines to keep in the consolidated report.
# Keeps the report small but preserves enough context to diagnose
# unexpected failures.
STDOUT_TAIL_LINES = 30
STDERR_TAIL_LINES = 20


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run common validation / smoke / canary checks from one CLI "
            "and produce a consolidated report. Never modifies Render env. "
            "Never makes production decisions. Never calls OpenAI directly "
            "— though render-canary may indirectly trigger OpenAI server-"
            "side if Render semantic matching is currently enabled."
        ),
    )
    parser.add_argument(
        "--profile", choices=PROFILES, default="quick",
        help="Which check bundle to run (default: %(default)s).",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="Render / local base URL (default: %(default)s).")
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help="Primary query for smoke / canary (default: %(default)s).")
    parser.add_argument("--secondary-query", default=DEFAULT_SECONDARY_QUERY,
                        help="Secondary query when --include-secondary-query is set.")
    parser.add_argument("--max-news", type=int, default=1,
                        help="max_news passed to /jobs/analyze (default: %(default)s).")
    parser.add_argument("--timeout-seconds", type=float, default=300.0,
                        help="Per-job timeout seconds (default: %(default)s).")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Smoke script poll interval (default: %(default)s).")
    parser.add_argument(
        "--skip-validate", action="store_true",
        help="Skip the validate.py step even if the profile includes it.",
    )
    parser.add_argument(
        "--skip-render", action="store_true",
        help="Skip every step that hits the base-url (smoke_async_job, smoke_semantic_canary).",
    )
    parser.add_argument(
        "--skip-semantic-canary", action="store_true",
        help="Skip semantic canary steps but keep legacy smoke.",
    )
    parser.add_argument(
        "--skip-historical", action="store_true",
        help="Skip historical batch dry-run and deterministic eval steps.",
    )
    parser.add_argument(
        "--include-secondary-query", action="store_true",
        help="Run semantic canary smoke twice — once per query.",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None,
        help="Path to consolidated JSON report (default: reports/operational_check_<ts>.json).",
    )
    parser.add_argument(
        "--markdown-out", type=Path, default=None,
        help="Path to consolidated Markdown report (default: reports/operational_check_<ts>.md).",
    )
    parser.add_argument(
        "--no-default-reports", action="store_true",
        help="Do not auto-write a default reports/operational_check_<ts>.{json,md} when paths omitted.",
    )
    parser.add_argument(
        "--fail-on-warn", action="store_true",
        help="Exit code 2 when any step's status is 'warn'.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print commands that would run and write the dry-run report — execute nothing.",
    )
    parser.add_argument(
        "--no-openai-note", action="store_true",
        help="Suppress the 'render-canary may trigger OpenAI server-side' note.",
    )
    return parser


# ---------------------------------------------------------------------------
# Profile → command list
# ---------------------------------------------------------------------------


def _python() -> str:
    return sys.executable or "python"


def _validate_step() -> dict:
    return {
        "name": "validate",
        "command": [_python(), str(ROOT / "scripts" / "validate.py")],
        "parser": _parse_validate_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _smoke_async_step(args: argparse.Namespace, query: str) -> dict:
    return {
        "name": f"smoke_async_job({query})",
        "command": [
            _python(),
            str(ROOT / "scripts" / "smoke_async_job.py"),
            "--base-url", args.base_url,
            "--query", query,
            "--max-news", str(args.max_news),
            "--timeout-seconds", str(int(args.timeout_seconds)),
            "--poll-interval", str(args.poll_interval),
        ],
        "parser": _parse_smoke_async_output,
        "hits_render": True,
        "may_call_openai": False,  # script doesn't; Render may
        "optional": False,
    }


def _smoke_canary_step(args: argparse.Namespace, query: str, *, expect_enabled: bool) -> dict:
    cmd = [
        _python(),
        str(ROOT / "scripts" / "smoke_semantic_canary.py"),
        "--base-url", args.base_url,
        "--query", query,
        "--max-news", str(args.max_news),
        "--timeout-seconds", str(int(args.timeout_seconds)),
        "--poll-interval", str(args.poll_interval),
    ]
    if expect_enabled:
        cmd.extend(["--expect-semantic-enabled", "--expect-provider", "openai",
                    "--fail-on-semantic-unavailable"])
    return {
        "name": f"smoke_semantic_canary({query}{'/expect-enabled' if expect_enabled else '/baseline'})",
        "command": cmd,
        "parser": _parse_smoke_canary_output,
        "hits_render": True,
        "may_call_openai": expect_enabled,  # canary mode implies semantic is on
        "optional": False,
    }


def _review_exposure_step(args: argparse.Namespace) -> dict:
    """Phase 2 M8.8: no-token review API public-exposure smoke.

    Hits ``args.base_url`` with no ``X-Review-Token`` header and
    verifies every ``/review/*`` endpoint returns a safe gate (503
    disabled or 403 token-required). Never calls OpenAI. Never asks
    the operator for ``REVIEW_API_TOKEN``. Never modifies Render env.
    """
    return {
        "name": "smoke_review_api_exposure(expect-disabled)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "smoke_review_api_exposure.py"),
            "--base-url", args.base_url,
            "--expect-disabled",
            "--timeout-seconds", str(int(args.timeout_seconds)),
        ],
        "parser": _parse_review_exposure_output,
        "hits_render": True,
        "may_call_openai": False,
        "optional": False,
    }


def _review_token_gate_step(args: argparse.Namespace) -> dict:
    """Phase 2 M9.5: controlled review API token-gate smoke.

    Hits ``args.base_url`` via the M9.5 read-only token-gate smoke.
    The smoke itself reads the correct token from
    ``REVIEW_API_SMOKE_TOKEN`` in the operator's local env (or
    whatever name the operator passes via the smoke's own
    ``--token-env`` flag). The runner does NOT inspect, copy, or
    echo the token; it only invokes the subprocess and parses the
    subprocess's stdout. The token never appears in the runner's
    own command list either.
    """
    return {
        "name": "smoke_review_api_token_gate",
        "command": [
            _python(),
            str(ROOT / "scripts" / "smoke_review_api_token_gate.py"),
            "--base-url", args.base_url,
            "--token-env", REVIEW_TOKEN_GATE_DEFAULT_ENV,
            "--timeout-seconds", str(int(args.timeout_seconds)),
        ],
        "parser": _parse_review_token_gate_output,
        "hits_render": True,
        "may_call_openai": False,
        "requires_secret_env": REVIEW_TOKEN_GATE_DEFAULT_ENV,
        "optional": False,
    }


def _review_local_step() -> dict:
    """Phase 2 M8.3: offline reviewer-workflow smoke.

    Runs ``scripts/smoke_review_workflow.py --self-contained`` which spins
    up the FastAPI app against a temp SQLite DB and a dummy in-process
    token. Never calls Render, never calls OpenAI, never modifies Render
    env, never prints the dummy token.
    """
    return {
        "name": "smoke_review_workflow(self-contained)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "smoke_review_workflow.py"),
            "--self-contained",
        ],
        "parser": _parse_review_local_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M10.1 — source-registry profile.
#
# Four steps, every one offline (no Render, no OpenAI, no network):
#   1) scripts/validate_source_registry.py            — schema check
#   2) scripts/classify_source_url.py --help          — CLI smoke
#   3) scripts/classify_source_url.py <matched_url>   — MATCHED probe
#   4) scripts/classify_source_url.py <unknown_url>   — NO_MATCH probe
#
# Steps 3 and 4 each carry their own parser so the runner can mark
# the expected NO_MATCH exit-code-1 step as a *pass* without
# special-casing exit codes elsewhere.
# ---------------------------------------------------------------------------


# Sample URLs chosen so the profile exercises both halves of the
# CLI's exit policy. The matched URL hits ``kr_law_open_data_candidate``
# in ``data/source_registry.json``; the no-match URL is documented as
# unreachable from the seed.
SOURCE_REGISTRY_MATCHED_URL_SAMPLE = "https://www.law.go.kr/sample"
SOURCE_REGISTRY_NO_MATCH_URL_SAMPLE = "https://unknown-source-example.invalid/page"


def _source_registry_validate_step() -> dict:
    return {
        "name": "validate_source_registry",
        "command": [
            _python(),
            str(ROOT / "scripts" / "validate_source_registry.py"),
            "--json",
        ],
        "parser": _parse_validate_source_registry_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_registry_help_step() -> dict:
    return {
        "name": "classify_source_url(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "classify_source_url.py"),
            "--help",
        ],
        "parser": _parse_classify_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_registry_matched_step() -> dict:
    return {
        "name": "classify_source_url(matched)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "classify_source_url.py"),
            SOURCE_REGISTRY_MATCHED_URL_SAMPLE,
        ],
        "parser": _parse_classify_matched_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_registry_no_match_step() -> dict:
    return {
        "name": "classify_source_url(no_match)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "classify_source_url.py"),
            SOURCE_REGISTRY_NO_MATCH_URL_SAMPLE,
        ],
        # No_match is the *expected* behavior, so exit_code=1 means the
        # CLI is working correctly. The custom parser turns that into
        # a runner-level PASS.
        "parser": _parse_classify_no_match_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M10.2 — source-crawler profile.
#
# Dry-run only. The crawler never fetches in this profile (no --save).
# Steps:
#   1) scripts/validate_source_registry.py                       (schema)
#   2) scripts/fetch_registry_source.py --help                   (CLI smoke)
#   3) scripts/fetch_registry_source.py --source-id ... --dry-run (safety)
#   4) scripts/classify_source_url.py <same url>                  (consistency)
#
# The dry-run step's expected exit code is 1 (the seed entry is
# default_enabled=false; the safety check refuses). The custom
# parser treats that exit-1 + "safety_refusal" + "DRY RUN" combo
# as a PASS, the same shape the M10.1 no-match parser uses.
# ---------------------------------------------------------------------------


SOURCE_CRAWLER_PROBE_SOURCE_ID = "kr_law_open_data_candidate"
SOURCE_CRAWLER_PROBE_URL = "https://www.law.go.kr/sample"


def _source_crawler_help_step() -> dict:
    return {
        "name": "fetch_registry_source(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "fetch_registry_source.py"),
            "--help",
        ],
        "parser": _parse_fetch_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_crawler_dry_run_step() -> dict:
    return {
        "name": "fetch_registry_source(dry_run)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "fetch_registry_source.py"),
            "--source-id", SOURCE_CRAWLER_PROBE_SOURCE_ID,
            "--url", SOURCE_CRAWLER_PROBE_URL,
            "--dry-run",
        ],
        # The seed entry is default_enabled=false so the safety check
        # refuses. That refusal is the expected dry-run outcome —
        # the parser treats it as PASS.
        "parser": _parse_fetch_dry_run_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_crawler_consistency_step() -> dict:
    return {
        "name": "classify_source_url(crawler_consistency)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "classify_source_url.py"),
            SOURCE_CRAWLER_PROBE_URL,
        ],
        # Same URL the dry-run step probed. The classifier sees
        # MATCHED (kr_law_open_data_candidate) — confirms registry
        # entry + crawler agree on the host.
        "parser": _parse_classify_matched_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M10.3 — source-enable profile.
#
# Dry-run only. The enable workflow never writes during this profile:
# step 3 uses --dry-run against the disabled seed entry; step 4 runs
# the offline tests. Steps:
#   1) scripts/validate_source_registry.py --json       (schema)
#   2) scripts/enable_registry_source.py --list         (status smoke)
#   3) scripts/enable_registry_source.py --source-id <id> --justification ... --dry-run
#   4) tests/test_enable_registry_source.py             (offline tests)
#
# No network. No OpenAI. The seed registry is never modified — the
# dry-run step only reports what would change.
# ---------------------------------------------------------------------------


SOURCE_ENABLE_PROBE_SOURCE_ID = "kr_law_open_data_candidate"
SOURCE_ENABLE_PROBE_JUSTIFICATION = (
    "operator dry run test justification text"
)


def _source_enable_list_step() -> dict:
    return {
        "name": "enable_registry_source(--list)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "enable_registry_source.py"),
            "--list",
        ],
        "parser": _parse_enable_list_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_enable_dry_run_step() -> dict:
    return {
        "name": "enable_registry_source(dry_run)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "enable_registry_source.py"),
            "--source-id", SOURCE_ENABLE_PROBE_SOURCE_ID,
            "--justification", SOURCE_ENABLE_PROBE_JUSTIFICATION,
            "--dry-run",
        ],
        "parser": _parse_enable_dry_run_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_enable_tests_step() -> dict:
    return {
        "name": "test_enable_registry_source",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_enable_registry_source.py"),
        ],
        "parser": _parse_enable_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M10.4 — source-extractor profile.
#
# Fully offline. The extractor never touches the network, never calls
# OpenAI, and never modifies source_fetch_artifacts. Steps:
#   1) scripts/validate_source_registry.py --json   (schema)
#   2) scripts/extract_artifact_text.py --help      (CLI smoke)
#   3) tests/test_artifact_extractor.py             (offline tests)
# ---------------------------------------------------------------------------


def _source_extractor_help_step() -> dict:
    return {
        "name": "extract_artifact_text(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "extract_artifact_text.py"),
            "--help",
        ],
        "parser": _parse_extract_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_extractor_tests_step() -> dict:
    return {
        "name": "test_artifact_extractor",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_artifact_extractor.py"),
        ],
        "parser": _parse_extractor_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M10.5 — source-linker profile.
#
# Fully offline. The linker never touches the network, never calls
# OpenAI, and never modifies source_fetch_artifacts /
# artifact_text_extractions / analysis_results. Steps:
#   1) scripts/validate_source_registry.py --json     (schema)
#   2) scripts/link_artifact_evidence.py --help       (CLI smoke)
#   3) scripts/link_artifact_evidence.py --list-extractions  (read-only smoke)
#   4) tests/test_artifact_evidence_linker.py         (offline tests)
# ---------------------------------------------------------------------------


def _source_linker_help_step() -> dict:
    return {
        "name": "link_artifact_evidence(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "link_artifact_evidence.py"),
            "--help",
        ],
        "parser": _parse_link_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_linker_list_extractions_step() -> dict:
    return {
        "name": "link_artifact_evidence(--list-extractions)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "link_artifact_evidence.py"),
            "--list-extractions",
        ],
        "parser": _parse_link_list_extractions_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _source_linker_tests_step() -> dict:
    return {
        "name": "test_artifact_evidence_linker",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_artifact_evidence_linker.py"),
        ],
        "parser": _parse_linker_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M11.0a — verdict-comparison profile.
#
# Fully offline. Read-only measurement layer for the three verdict
# producers. Never calls the network, never calls OpenAI, never
# modifies the producers themselves, and never runs analyze_pipeline.
# Steps:
#   1) scripts/compare_verdict_producers.py --help        (CLI smoke)
#   2) scripts/compare_verdict_producers.py --summary     (DB read-only)
#   3) tests/test_verdict_producer_comparison.py          (offline tests)
# ---------------------------------------------------------------------------


def _verdict_comparison_help_step() -> dict:
    return {
        "name": "compare_verdict_producers(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "compare_verdict_producers.py"),
            "--help",
        ],
        "parser": _parse_compare_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _verdict_comparison_summary_step() -> dict:
    return {
        "name": "compare_verdict_producers(--summary)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "compare_verdict_producers.py"),
            "--summary",
        ],
        "parser": _parse_compare_summary_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _verdict_comparison_tests_step() -> dict:
    return {
        "name": "test_verdict_producer_comparison",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_verdict_producer_comparison.py"),
        ],
        "parser": _parse_comparator_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M11.0b — verdict-label-diagnostic profile.
#
# Fully offline. Read-only diagnostic for verification_card._verdict_label.
# The producers themselves are never modified, and analyze_pipeline
# is never invoked. Steps:
#   1) scripts/diagnose_verdict_labels.py --help          (CLI smoke)
#   2) scripts/diagnose_verdict_labels.py --branch-table  (no-DB smoke)
#   3) scripts/diagnose_verdict_labels.py --summary       (read-only DB)
#   4) tests/test_verdict_label_diagnostic.py             (offline tests)
# ---------------------------------------------------------------------------


def _verdict_label_diag_help_step() -> dict:
    return {
        "name": "diagnose_verdict_labels(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "diagnose_verdict_labels.py"),
            "--help",
        ],
        "parser": _parse_diag_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _verdict_label_diag_branch_table_step() -> dict:
    return {
        "name": "diagnose_verdict_labels(--branch-table)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "diagnose_verdict_labels.py"),
            "--branch-table",
        ],
        "parser": _parse_diag_branch_table_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _verdict_label_diag_summary_step() -> dict:
    return {
        "name": "diagnose_verdict_labels(--summary)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "diagnose_verdict_labels.py"),
            "--summary",
        ],
        "parser": _parse_diag_summary_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _verdict_label_diag_tests_step() -> dict:
    return {
        "name": "test_verdict_label_diagnostic",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_verdict_label_diagnostic.py"),
        ],
        "parser": _parse_diag_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _verdict_label_b08_fix_tests_step() -> dict:
    """M11.0c B08 conservative-fix pin tests. Bundled into the
    verdict-label-diagnostic profile so any future change that drops
    a gate or unbalances the catalog surfaces in the same profile
    operators already run for verdict-label health."""
    return {
        "name": "test_verdict_label_b08_fix",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_verdict_label_b08_fix.py"),
        ],
        "parser": _parse_b08_fix_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M11.1 — legacy-review-enroll profile.
#
# Fully offline AND read-mostly. The profile NEVER passes --enroll to
# the enrollment CLI; only read-only modes (--help, --check-status,
# --list, --dry-run) and the offline test suite run. An operator
# performing actual enrollment uses ``--enroll --yes`` manually
# outside any profile.
# ---------------------------------------------------------------------------


def _legacy_review_enroll_help_step() -> dict:
    return {
        "name": "enroll_legacy_weak_verified(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "enroll_legacy_weak_verified.py"),
            "--help",
        ],
        "parser": _parse_enroll_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _legacy_review_enroll_check_status_step() -> dict:
    return {
        "name": "enroll_legacy_weak_verified(--check-status)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "enroll_legacy_weak_verified.py"),
            "--check-status",
        ],
        "parser": _parse_enroll_check_status_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _legacy_review_enroll_list_step() -> dict:
    return {
        "name": "enroll_legacy_weak_verified(--list)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "enroll_legacy_weak_verified.py"),
            "--list",
        ],
        "parser": _parse_enroll_list_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _legacy_review_enroll_dry_run_step() -> dict:
    return {
        "name": "enroll_legacy_weak_verified(--dry-run)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "enroll_legacy_weak_verified.py"),
            "--dry-run",
        ],
        "parser": _parse_enroll_dry_run_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _legacy_review_enroll_tests_step() -> dict:
    return {
        "name": "test_legacy_review_enrollment",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_legacy_review_enrollment.py"),
        ],
        "parser": _parse_enroll_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M11.2 — korean-constants profile.
#
# Pure data-centralization checks. No DB, no network, no OpenAI.
# Steps:
#   1) compileall korean_constants.py             (syntax + import smoke)
#   2) tests/test_korean_constants.py             (immutability, pins,
#                                                  import-graph wiring,
#                                                  anti-reintroduction)
# ---------------------------------------------------------------------------


def _korean_constants_compile_step() -> dict:
    return {
        "name": "compileall(korean_constants.py)",
        "command": [
            _python(), "-m", "compileall",
            str(ROOT / "korean_constants.py"),
        ],
        "parser": _parse_korean_constants_compile_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _korean_constants_tests_step() -> dict:
    return {
        "name": "test_korean_constants",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_korean_constants.py"),
        ],
        "parser": _parse_korean_constants_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M12.0a — postgres-dual-write profile.
#
# Fully offline. The profile MUST work with USE_POSTGRES_WRITE unset
# (the default). When USE_POSTGRES_WRITE=true is set during validation
# the health-check step will still exit 0 (it reports "enabled but
# unable to connect") UNLESS a real Postgres is reachable. To keep the
# profile portable across CI and local environments, the runner does
# not require a live Postgres — only the offline tests + the
# read-only CLI smokes. Steps:
#   1) scripts/check_postgres_health.py --help    (CLI smoke)
#   2) scripts/check_postgres_health.py           (default-env report)
#   3) tests/test_postgres_storage.py             (offline tests)
# ---------------------------------------------------------------------------


def _postgres_health_help_step() -> dict:
    return {
        "name": "check_postgres_health(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_postgres_health.py"),
            "--help",
        ],
        "parser": _parse_postgres_health_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _postgres_health_default_step() -> dict:
    return {
        "name": "check_postgres_health(default)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_postgres_health.py"),
        ],
        "parser": _parse_postgres_health_default_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _postgres_storage_tests_step() -> dict:
    return {
        "name": "test_postgres_storage",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_postgres_storage.py"),
        ],
        "parser": _parse_postgres_storage_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M12.0b — postgres-backfill profile.
#
# Fully offline. The profile MUST work with USE_POSTGRES_WRITE unset
# (the default). The --status step gracefully reports the disabled
# state; the offline tests exercise the backfill logic against a
# sqlite:// SQLAlchemy substrate. SQLite source rows are isolated in
# temp files; the real policy_ai.db is never touched. Steps:
#   1) scripts/run_postgres_backfill.py --help    (CLI smoke)
#   2) scripts/run_postgres_backfill.py --status  (default-env report)
#   3) tests/test_postgres_backfill.py            (offline tests)
# ---------------------------------------------------------------------------


def _postgres_backfill_help_step() -> dict:
    return {
        "name": "run_postgres_backfill(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "run_postgres_backfill.py"),
            "--help",
        ],
        "parser": _parse_postgres_backfill_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _postgres_backfill_status_step() -> dict:
    return {
        "name": "run_postgres_backfill(--status)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "run_postgres_backfill.py"),
            "--status",
        ],
        "parser": _parse_postgres_backfill_status_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _postgres_backfill_tests_step() -> dict:
    return {
        "name": "test_postgres_backfill",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_postgres_backfill.py"),
        ],
        "parser": _parse_postgres_backfill_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M13.1a — llm-judge-dry-run profile.
#
# Fully offline. Exercises the Judge CLI's --help / --status smokes
# plus a --simulate-confirm round trip against a temp SQLite source so
# the simulation pipeline is verified end-to-end. The profile uses
# stub providers and built-in fake providers; no real LLM API call is
# ever made and the Judge is NOT connected to analyze_pipeline in
# M13.1a. Steps:
#   1) scripts/dry_run_llm_judge.py --help     (CLI smoke)
#   2) scripts/dry_run_llm_judge.py --status   (provider chain report)
#   3) tests/test_llm_judge.py                 (offline tests, 59 cases)
# ---------------------------------------------------------------------------


def _llm_judge_help_step() -> dict:
    return {
        "name": "dry_run_llm_judge(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "dry_run_llm_judge.py"),
            "--help",
        ],
        "parser": _parse_llm_judge_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _llm_judge_status_step() -> dict:
    return {
        "name": "dry_run_llm_judge(--status)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "dry_run_llm_judge.py"),
            "--status",
        ],
        "parser": _parse_llm_judge_status_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _llm_judge_tests_step() -> dict:
    return {
        "name": "test_llm_judge",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_llm_judge.py"),
        ],
        "parser": _parse_llm_judge_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M13.2a — frontend-build profile.
#
# Fully offline. Verifies the build pipeline's byte-identical
# guarantee between frontend/ source and the committed web/index.html
# artifact. --status is read-only; --check is read-only; both refuse
# to write any file. The repo-level integration test in
# tests/test_frontend_build.py is the canonical "no drift" pin.
# Steps:
#   1) frontend/build_index.py --status   (paths + checksums; no writes)
#   2) frontend/build_index.py --check    (byte-identical guarantee)
#   3) tests/test_frontend_build.py       (synthetic + repo-level tests)
# ---------------------------------------------------------------------------


def _frontend_build_status_step() -> dict:
    return {
        "name": "frontend_build(--status)",
        "command": [
            _python(),
            str(ROOT / "frontend" / "build_index.py"),
            "--status",
        ],
        "parser": _parse_frontend_build_status_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _frontend_build_check_step() -> dict:
    return {
        "name": "frontend_build(--check)",
        "command": [
            _python(),
            str(ROOT / "frontend" / "build_index.py"),
            "--check",
        ],
        "parser": _parse_frontend_build_check_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _frontend_build_tests_step() -> dict:
    return {
        "name": "test_frontend_build",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_frontend_build.py"),
        ],
        "parser": _parse_frontend_build_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M13.3a — http-cache profile.
#
# Fully offline. --help and --status are read-only smokes; the tests
# pin the cache's safety contract (never raises, never integrated with
# the pipeline, no real HTTP traffic). The cache is disabled by default
# and dormant in production until M13.3b wires up specific call sites.
# Steps:
#   1) scripts/check_http_cache.py --help    (CLI smoke)
#   2) scripts/check_http_cache.py --status  (default-env report)
#   3) tests/test_http_cache.py              (offline tests, 70 cases)
# ---------------------------------------------------------------------------


def _http_cache_help_step() -> dict:
    return {
        "name": "check_http_cache(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_http_cache.py"),
            "--help",
        ],
        "parser": _parse_http_cache_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _http_cache_status_step() -> dict:
    return {
        "name": "check_http_cache(--status)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_http_cache.py"),
        ],
        "parser": _parse_http_cache_status_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _http_cache_tests_step() -> dict:
    return {
        "name": "test_http_cache",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_http_cache.py"),
        ],
        "parser": _parse_http_cache_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M13.3b — official-crawler-cache profile.
#
# Fully offline. The integration tests use unittest.mock to patch
# requests.get so no real HTTP traffic occurs. The byte-identicality
# regression pin (CacheOffByteIdentityTests) is the most important
# assertion: with both feature flags unset, _request_url's return
# value is *identical* to the underlying requests.get call. Steps:
#   1) tests/test_official_crawler_cache.py  (integration tests)
#   2) tests/test_http_cache.py              (incl. FetchWithCacheTests)
# ---------------------------------------------------------------------------


def _official_crawler_cache_tests_step() -> dict:
    return {
        "name": "test_official_crawler_cache",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_official_crawler_cache.py"),
        ],
        "parser": _parse_official_crawler_cache_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _official_crawler_cache_underlying_tests_step() -> dict:
    return {
        "name": "test_http_cache(fetch_with_cache)",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_http_cache.py"),
        ],
        "parser": _parse_http_cache_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M13.3c — cache-measurement-dry profile.
#
# Tooling-only profile. The two CLIs (--help smokes) confirm the
# scripts parse args and the test suites exercise the parser,
# verdict thresholds, simulate paths, and failure handling without
# ever hitting Render. The actual measurement against Render is an
# operator step (see docs/CACHE_ACTIVATION_GUIDE.md). Steps:
#   1) scripts/measure_cache_impact.py --help         (CLI smoke)
#   2) scripts/check_cache_activation.py --help       (CLI smoke)
#   3) tests/test_measure_cache_impact.py             (unit tests)
#   4) tests/test_check_cache_activation.py           (unit tests)
# ---------------------------------------------------------------------------


def _measure_cache_impact_help_step() -> dict:
    return {
        "name": "measure_cache_impact(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "measure_cache_impact.py"),
            "--help",
        ],
        "parser": _parse_measure_cache_impact_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _check_cache_activation_help_step() -> dict:
    return {
        "name": "check_cache_activation(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_cache_activation.py"),
            "--help",
        ],
        "parser": _parse_check_cache_activation_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _measure_cache_impact_tests_step() -> dict:
    return {
        "name": "test_measure_cache_impact",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_measure_cache_impact.py"),
        ],
        "parser": _parse_measure_cache_impact_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _check_cache_activation_tests_step() -> dict:
    return {
        "name": "test_check_cache_activation",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_check_cache_activation.py"),
        ],
        "parser": _parse_check_cache_activation_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M14.0a — structured-logging profile.
#
# Fully offline. --help and --status are read-only smokes;
# --emit-sample produces a few representative log records via the
# structured logger so operators can verify the format. The offline
# tests cover env parsing, idempotency, JSON shape, Korean UTF-8
# preservation, module-adoption for the 10 M13.x modules, and the
# legacy-isolation pin for 18 untouched files. No real network, no
# OpenAI, no DB writes.
# Steps:
#   1) scripts/check_logging.py --help          (CLI smoke)
#   2) scripts/check_logging.py --status        (default-env report)
#   3) scripts/check_logging.py --emit-sample   (sample log records)
#   4) tests/test_structured_logging.py         (offline tests)
# ---------------------------------------------------------------------------


def _structured_logging_help_step() -> dict:
    return {
        "name": "check_logging(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_logging.py"),
            "--help",
        ],
        "parser": _parse_structured_logging_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _structured_logging_status_step() -> dict:
    return {
        "name": "check_logging(--status)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_logging.py"),
        ],
        "parser": _parse_structured_logging_status_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _structured_logging_emit_sample_step() -> dict:
    return {
        "name": "check_logging(--emit-sample)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_logging.py"),
            "--emit-sample",
        ],
        "parser": _parse_structured_logging_emit_sample_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _structured_logging_tests_step() -> dict:
    return {
        "name": "test_structured_logging",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_structured_logging.py"),
        ],
        "parser": _parse_structured_logging_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M14.3a — request-id context tests live under the
# structured-logging profile so the same ops run guards both the
# JSON formatter contract and the request-id propagation contract.
# ---------------------------------------------------------------------------


def _request_context_tests_step() -> dict:
    return {
        "name": "test_request_context",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_request_context.py"),
        ],
        "parser": _parse_request_context_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _api_request_id_middleware_tests_step() -> dict:
    return {
        "name": "test_api_request_id_middleware",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_api_request_id_middleware.py"),
        ],
        "parser": _parse_api_request_id_middleware_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M14.0b — print-migration profile.
#
# Tooling-only profile. The print() -> structured logging migration
# touched 5 production files (main.py, official_crawler.py,
# verification_card.py, news_collector.py, article_extractor.py).
# This profile pins:
#   * Zero remaining print() calls in those 5 files (AST + token).
#   * get_logger imported + module-level logger init present.
#   * Log-call count >= pre-migration print count per file.
#   * 8 deferred files retain their pre-M14.0b print counts.
# It also re-runs the structured logging tests so the M14.0a
# foundation is exercised alongside the new migration pin.
# ---------------------------------------------------------------------------


def _print_migration_tests_step() -> dict:
    return {
        "name": "test_print_migration",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_print_migration.py"),
        ],
        "parser": _parse_print_migration_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _print_migration_compileall_step() -> dict:
    """Confirm all 13 migrated source files still compile cleanly
    (5 from M14.0b + 8 from M14.0c)."""
    targets = [
        str(ROOT / name) for name in (
            # M14.0b
            "main.py",
            "official_crawler.py",
            "verification_card.py",
            "news_collector.py",
            "article_extractor.py",
            # M14.0c
            "evidence_comparator.py",
            "policy_decision.py",
            "policy_confidence.py",
            "policy_impact.py",
            "bias_framing_agent.py",
            "evidence_extraction_agent.py",
            "contradiction_agent.py",
            "official_source_body.py",
        )
    ]
    return {
        "name": "compileall(migrated 13 files)",
        "command": [_python(), "-m", "compileall", *targets],
        "parser": _parse_compileall_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _print_migration_m14_0c_tests_step() -> dict:
    return {
        "name": "test_print_migration_m14_0c",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_print_migration_m14_0c.py"),
        ],
        "parser": _parse_print_migration_m14_0c_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


# ---------------------------------------------------------------------------
# Phase 2 M14.2 — json-logging-verification profile.
#
# Tooling-only profile. The --help smoke confirms the CLI parses
# args; the --local mode subprocesses check_logging.py to actually
# verify JSON output works on the current machine; the unit tests
# pin schema, Korean preservation, base-url mode behaviour, and the
# env-var non-mutation contract. NO real Render call happens during
# this profile.
# ---------------------------------------------------------------------------


def _check_json_logging_help_step() -> dict:
    return {
        "name": "check_json_logging(--help)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_json_logging.py"),
            "--help",
        ],
        "parser": _parse_check_json_logging_help_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _check_json_logging_local_step() -> dict:
    return {
        "name": "check_json_logging(--local)",
        "command": [
            _python(),
            str(ROOT / "scripts" / "check_json_logging.py"),
            "--local",
        ],
        "parser": _parse_check_json_logging_local_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _check_json_logging_tests_step() -> dict:
    return {
        "name": "test_check_json_logging",
        "command": [
            _python(),
            str(ROOT / "tests" / "test_check_json_logging.py"),
        ],
        "parser": _parse_check_json_logging_tests_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _historical_dry_run_step() -> dict:
    return {
        "name": "historical_dry_run",
        "command": [
            _python(),
            str(ROOT / "scripts" / "build_historical_claim_batch.py"),
            "--dry-run", "--max-cases", "100",
        ],
        "parser": _parse_historical_dry_run_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": False,
    }


def _historical_eval_step() -> dict:
    fixture = ROOT / "reports" / "semantic_historical_claim_batch.generated.json"
    return {
        "name": "historical_deterministic_eval",
        "command": [
            _python(),
            str(ROOT / "scripts" / "evaluate_real_claim_batch.py"),
            "--case-file", str(fixture),
            "--provider", "deterministic", "--no-network",
            "--show-failures",
        ],
        "parser": _parse_historical_eval_output,
        "hits_render": False,
        "may_call_openai": False,
        "optional": True,  # only run if fixture exists
        "requires_file": str(fixture),
    }


def _resolve_steps(args: argparse.Namespace) -> List[dict]:
    """Translate a profile + skip flags into an ordered list of steps."""
    steps: List[dict] = []
    profile = args.profile

    if profile in ("quick", "post-commit", "full"):
        if not args.skip_validate:
            steps.append(_validate_step())

    if profile == "post-commit":
        if not args.skip_render:
            steps.append(_smoke_async_step(args, args.query))

    if profile == "render-baseline":
        if not args.skip_render:
            steps.append(_smoke_async_step(args, args.query))
            if not args.skip_semantic_canary:
                steps.append(_smoke_canary_step(args, args.query, expect_enabled=False))

    if profile == "render-canary":
        if not args.skip_render and not args.skip_semantic_canary:
            steps.append(_smoke_canary_step(args, args.query, expect_enabled=True))
            if args.include_secondary_query:
                steps.append(_smoke_canary_step(args, args.secondary_query, expect_enabled=True))
        if not args.skip_render:
            steps.append(_smoke_async_step(args, args.query))

    if profile == "historical":
        if not args.skip_historical:
            steps.append(_historical_dry_run_step())
            steps.append(_historical_eval_step())

    if profile == "review-local":
        # Fully offline: no Render, no OpenAI, no token from operator.
        steps.append(_review_local_step())

    if profile == "review-exposure":
        # Hits Render (or any --base-url) with NO token; never modifies
        # Render env; never calls OpenAI. Intentionally separate from
        # render-canary so the operator can run an exposure check
        # without paying the canary's OpenAI / semantic cost.
        if not args.skip_render:
            steps.append(_review_exposure_step(args))

    if profile == "review-token-gate":
        # M9.5 — only safe to run when the operator has intentionally
        # set REVIEW_API_ENABLED=true on the deploy AND has the smoke
        # token (REVIEW_API_SMOKE_TOKEN) in their local env. Never
        # bundled into quick / validate / review-exposure. --skip-render
        # drops the step entirely so the operator can no-op the
        # profile in a smoke-test harness.
        if not args.skip_render:
            steps.append(_review_token_gate_step(args))

    if profile == "source-registry":
        # M10.1 — offline source-registry validator + URL classifier
        # CLI smokes. Fully local (never hits Render, never calls
        # OpenAI). The no-match step uses a custom parser because its
        # *expected* exit code is 1 (NO_MATCH on the unknown URL is
        # the correct CLI behavior).
        steps.append(_source_registry_validate_step())
        steps.append(_source_registry_help_step())
        steps.append(_source_registry_matched_step())
        steps.append(_source_registry_no_match_step())

    if profile == "source-crawler":
        # M10.2 — dry-run only crawler profile. Never fetches: the
        # dry-run step runs against the disabled seed entry, so the
        # safety check refuses. The custom parser treats the
        # expected refusal as PASS. No --save anywhere.
        steps.append(_source_registry_validate_step())
        steps.append(_source_crawler_help_step())
        steps.append(_source_crawler_dry_run_step())
        steps.append(_source_crawler_consistency_step())

    if profile == "source-enable":
        # M10.3 — operator enable workflow. Fully offline. The
        # dry-run probe never writes; the test suite uses temp
        # registries so the real seed is never modified.
        steps.append(_source_registry_validate_step())
        steps.append(_source_enable_list_step())
        steps.append(_source_enable_dry_run_step())
        steps.append(_source_enable_tests_step())

    if profile == "source-extractor":
        # M10.4 — text extraction pipeline. Fully offline. The
        # test suite uses temp SQLite files so the real policy_ai.db
        # is never touched.
        steps.append(_source_registry_validate_step())
        steps.append(_source_extractor_help_step())
        steps.append(_source_extractor_tests_step())

    if profile == "source-linker":
        # M10.5 — evidence candidate linker. Fully offline. The
        # linker reads from artifact_text_extractions + analysis_results
        # and writes only to artifact_evidence_candidates. Tests use
        # temp SQLite files so the real policy_ai.db is never touched.
        steps.append(_source_registry_validate_step())
        steps.append(_source_linker_help_step())
        steps.append(_source_linker_list_extractions_step())
        steps.append(_source_linker_tests_step())

    if profile == "verdict-comparison":
        # M11.0a — read-only verdict-producer comparison tool. Fully
        # offline. Reads analysis_results / reports JSON; writes only
        # to verdict_producer_comparisons (and only when --save is
        # passed). The producers themselves are never modified.
        steps.append(_verdict_comparison_help_step())
        steps.append(_verdict_comparison_summary_step())
        steps.append(_verdict_comparison_tests_step())

    if profile == "verdict-label-diagnostic":
        # M11.0b — read-only diagnostic for verification_card._verdict_label.
        # Fully offline. Branch-table smoke needs no DB; summary mode
        # reads verdict_label_attributions (empty on a clean DB);
        # tests use temp SQLite files only. verification_card.py is
        # never modified; analyze_pipeline is never invoked.
        # M11.0c — adds the B08 conservative-fix pin tests so the
        # gates (score>=60 + strength in {medium,high}) stay in place.
        steps.append(_verdict_label_diag_help_step())
        steps.append(_verdict_label_diag_branch_table_step())
        steps.append(_verdict_label_diag_summary_step())
        steps.append(_verdict_label_diag_tests_step())
        steps.append(_verdict_label_b08_fix_tests_step())

    if profile == "legacy-review-enroll":
        # M11.1 — read-mostly enrollment of legacy weak-verified rows.
        # The profile NEVER calls --enroll. Operators perform the
        # actual write manually with ``--enroll --yes`` outside any
        # profile. Steps cover the four read-only CLI modes plus the
        # offline test suite (temp SQLite files; real policy_ai.db
        # is never touched).
        steps.append(_legacy_review_enroll_help_step())
        steps.append(_legacy_review_enroll_check_status_step())
        steps.append(_legacy_review_enroll_list_step())
        steps.append(_legacy_review_enroll_dry_run_step())
        steps.append(_legacy_review_enroll_tests_step())

    if profile == "korean-constants":
        # M11.2 — centralized Korean keyword constants. Pure data;
        # no DB, no network, no live pipeline. The compileall step
        # catches import errors; the test step covers immutability,
        # regression-safety pins, minimum sizes, hygiene, cross-file
        # equivalence, and the import-graph anti-reintroduction guard.
        steps.append(_korean_constants_compile_step())
        steps.append(_korean_constants_tests_step())

    if profile == "postgres-dual-write":
        # M12.0a — Postgres dual-write foundation. Fully offline. The
        # default-env health check reports "disabled" (USE_POSTGRES_WRITE
        # unset) which is the expected state during validation; the
        # offline tests exercise the feature-flag, schema parity, and
        # SQLite-isolation invariants without needing a real Postgres.
        # SQLite remains the sole source of truth in this milestone.
        steps.append(_postgres_health_help_step())
        steps.append(_postgres_health_default_step())
        steps.append(_postgres_storage_tests_step())

    if profile == "postgres-backfill":
        # M12.0b — Postgres backfill. Fully offline. --status reports
        # the disabled state cleanly when USE_POSTGRES_WRITE is unset
        # (the default); the offline tests exercise the round-trip
        # against a sqlite:// SQLAlchemy substrate. SQLite source rows
        # live in temp files; the real policy_ai.db is never touched
        # and is never modified by backfill under any circumstance.
        steps.append(_postgres_backfill_help_step())
        steps.append(_postgres_backfill_status_step())
        steps.append(_postgres_backfill_tests_step())

    if profile == "llm-judge-dry-run":
        # M13.1a — LLM Judge dry-run. Fully offline. The Judge is NOT
        # connected to analyze_pipeline in M13.1a; --status reports
        # both stub providers as unavailable; the tests exercise the
        # validator, the run_judge orchestration, the CLI simulation
        # flags, and the upgrade-refusal contract end-to-end with
        # built-in fake providers. No real LLM API call is made.
        steps.append(_llm_judge_help_step())
        steps.append(_llm_judge_status_step())
        steps.append(_llm_judge_tests_step())

    if profile == "frontend-build":
        # M13.2a — frontend build pipeline. Fully offline. --status
        # and --check are both read-only; the offline tests exercise
        # idempotency, marker validation, --check pass/fail paths,
        # and the canonical repo-level integration check. The build
        # script depends on stdlib only — no bundler, no npm
        # dependency, no Node tool chain.
        steps.append(_frontend_build_status_step())
        steps.append(_frontend_build_check_step())
        steps.append(_frontend_build_tests_step())

    if profile == "http-cache":
        # M13.3a — shared HTTP cache foundation. Fully offline.
        # --help and --status are read-only smokes; the offline tests
        # cover feature flag, key stability, Cache-Control refusals,
        # LRU eviction, thread safety, singleton lifecycle, the CLI,
        # and the pipeline-isolation pin that guarantees no production
        # module imports http_cache in M13.3a.
        steps.append(_http_cache_help_step())
        steps.append(_http_cache_status_step())
        steps.append(_http_cache_tests_step())

    if profile == "official-crawler-cache":
        # M13.3b — HTTP cache integration into official_crawler.
        # Fully offline (requests.get patched with a fake). The
        # byte-identicality pin in CacheOffByteIdentityTests is the
        # contract that guarantees the cache-off default keeps
        # _request_url unchanged compared to the pre-M13.3b code.
        # The fetch_with_cache helper added to http_cache.py is
        # covered by tests/test_http_cache.py::FetchWithCacheTests.
        steps.append(_official_crawler_cache_tests_step())
        steps.append(_official_crawler_cache_underlying_tests_step())

    if profile == "cache-measurement-dry":
        # M13.3c — cache measurement + activation tooling.
        # Tooling-only profile: --help smokes confirm both CLIs
        # parse args; the unit tests exercise the parser, verdict
        # thresholds, simulate paths, and failure handling. NO real
        # Render call happens during this profile. Real measurement
        # against Render is an operator step driven by
        # docs/CACHE_ACTIVATION_GUIDE.md.
        steps.append(_measure_cache_impact_help_step())
        steps.append(_check_cache_activation_help_step())
        steps.append(_measure_cache_impact_tests_step())
        steps.append(_check_cache_activation_tests_step())

    if profile == "structured-logging":
        # M14.0a — structured logging foundation. Fully offline.
        # --help / --status / --emit-sample are read-only or stderr-
        # only smokes; the offline tests pin the module-adoption
        # contract for the 10 M13.x modules and the legacy-isolation
        # contract for 18 untouched files. No real network. No
        # external logging service. JSON output is opt-in.
        # M14.3a — the request_context + middleware tests run here
        # too so the same profile guards the request-id contract.
        steps.append(_structured_logging_help_step())
        steps.append(_structured_logging_status_step())
        steps.append(_structured_logging_emit_sample_step())
        steps.append(_structured_logging_tests_step())
        steps.append(_request_context_tests_step())
        steps.append(_api_request_id_middleware_tests_step())

    if profile == "print-migration":
        # M14.0b + M14.0c — print() -> structured logging migration on
        # all 13 files (251 prints total). Fully offline. The
        # compileall step confirms every migrated file still parses;
        # the M14.0b test pins the top-5 contract; the M14.0c test
        # pins the remaining-8 contract AND subprocess-invokes the
        # verdict test suites to prove verdict invariance; the
        # structured logging tests re-run as a regression check.
        steps.append(_print_migration_compileall_step())
        steps.append(_print_migration_tests_step())
        steps.append(_print_migration_m14_0c_tests_step())
        steps.append(_structured_logging_tests_step())

    if profile == "json-logging-verification":
        # M14.2 — JSON logging production activation tooling.
        # Fully offline. --help confirms the CLI loads; --local
        # actually subprocesses check_logging.py and verifies the
        # JSON schema on the current machine; the unit tests pin
        # validation logic, base-url mode behaviour, and the
        # env-var non-mutation contract. The script does NOT
        # touch Render and does NOT modify any env var after exit.
        steps.append(_check_json_logging_help_step())
        steps.append(_check_json_logging_local_step())
        steps.append(_check_json_logging_tests_step())

    if profile == "full":
        if not args.skip_render and not args.skip_semantic_canary:
            steps.append(_smoke_canary_step(args, args.query, expect_enabled=True))
            if args.include_secondary_query:
                steps.append(_smoke_canary_step(args, args.secondary_query, expect_enabled=True))
        if not args.skip_render:
            steps.append(_smoke_async_step(args, args.query))
        if not args.skip_historical:
            steps.append(_historical_dry_run_step())
            steps.append(_historical_eval_step())

    return steps


# ---------------------------------------------------------------------------
# Output parsers — best-effort, never raise.
# ---------------------------------------------------------------------------


_HEALTH_PASS = "pass"
_HEALTH_WARN = "warn"
_HEALTH_FAIL = "fail"
_HEALTH_SKIPPED = "skipped"
_HEALTH_UNKNOWN = "unknown"


def _parse_validate_output(stdout: str, stderr: str, exit_code: int) -> dict:
    if "all checks passed" in stdout.lower():
        return {"status": _HEALTH_PASS, "summary": "validate.py all checks passed"}
    if exit_code != 0:
        return {
            "status": _HEALTH_FAIL,
            "summary": f"validate.py exited {exit_code}",
        }
    return {
        "status": _HEALTH_UNKNOWN,
        "summary": "validate.py exited 0 but 'all checks passed' line not detected",
    }


def _parse_smoke_async_output(stdout: str, stderr: str, exit_code: int) -> dict:
    if exit_code != 0:
        return {"status": _HEALTH_FAIL, "summary": f"smoke_async_job exited {exit_code}"}
    final_status_match = re.search(r"final_status\s*=\s*(\w+)", stdout)
    results_match = re.search(r"results_count=(\d+|n/a)", stdout)
    elapsed_match = re.search(r"elapsed\s*=\s*([\d.]+)s", stdout)
    final_status = final_status_match.group(1) if final_status_match else "?"
    results_count = results_match.group(1) if results_match else "?"
    elapsed = elapsed_match.group(1) if elapsed_match else "?"
    passed = "PASSED" in stdout
    return {
        "status": _HEALTH_PASS if passed else _HEALTH_FAIL,
        "summary": (
            f"smoke_async_job: passed={passed} final_status={final_status} "
            f"results_count={results_count} elapsed={elapsed}s"
        ),
        "metrics": {
            "passed": passed,
            "final_status": final_status,
            "results_count": results_count,
            "elapsed_seconds": elapsed,
        },
    }


# The semantic canary scorecard prints a single deterministic line like:
#   result_count=1 semantic_summary_count=1 semantic_enabled=1 ...
#   ... cap_ratio=0.000 runtime_p95_ms=7523 health=warn
_CANARY_SCORE_RE = re.compile(
    r"result_count=(?P<result_count>\d+).*?"
    r"semantic_summary_count=(?P<semantic_summary_count>\d+).*?"
    r"semantic_enabled=(?P<semantic_enabled>\d+).*?"
    r"semantic_available=(?P<semantic_available>\d+).*?"
    r"provider_errors=(?P<provider_errors>\d+).*?"
    r"overstrong_like=(?P<overstrong_like>\d+).*?"
    r"cap_ratio=(?P<cap_ratio>[\d.]+).*?"
    r"runtime_p95_ms=(?P<runtime_p95_ms>\d+).*?"
    r"health=(?P<health>\w+)",
    re.DOTALL,
)


def _canary_safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _canary_safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# M8.4 thresholds — kept in sync with semantic_canary_metrics.WARN_* constants.
# Duplicated here (not imported) because run_operational_checks.py is a pure
# subprocess orchestrator and must not import verdict / scoring / agent modules.
_CANARY_WARN_CAP_RATIO = 0.70
_CANARY_WARN_RUNTIME_MS_P95 = 1500


def _classify_canary_semantic_safety(fields: dict, exit_code: int) -> dict:
    """Phase 2 M8.4: separate semantic safety signals from runtime-only warns.

    Returns a dict with stable keys the runner / report can rely on:

    * ``semantic_safety_status`` — ``pass`` / ``warn`` / ``fail``. Looks only
      at provider errors, overstrong_like detection, and semantic
      availability — never at runtime / cap_ratio.
    * ``semantic_runtime_status`` — ``pass`` / ``warn``. Tripped by
      runtime_p95 above the M7.2 threshold or cap_ratio above 0.70.
    * ``rollback_recommended`` — bool. ``True`` only when at least one
      hard safety signal fires (provider_errors > 0, overstrong_like > 0,
      semantic configured-but-unavailable, smoke exit 1/2).
    * ``rollback_reasons`` — list of human-readable strings, one per hard
      safety signal.
    * ``warn_only_reasons`` — list of human-readable strings for soft
      signals that warrant attention but **not** rollback (runtime, cap).
    * ``semantic_safety_summary`` — short single-line message for reports.

    The classifier is deterministic — same input always produces the
    same output. It never recommends "verified" and never claims a
    semantic match strength is a verdict. The whole point of this layer
    is operational, not verdict.
    """
    provider_errors = _canary_safe_int(fields.get("provider_errors"))
    overstrong_like = _canary_safe_int(fields.get("overstrong_like"))
    semantic_enabled = _canary_safe_int(fields.get("semantic_enabled"))
    semantic_available = _canary_safe_int(fields.get("semantic_available"))
    cap_ratio = _canary_safe_float(fields.get("cap_ratio"))
    runtime_p95 = _canary_safe_int(fields.get("runtime_p95_ms"))

    rollback_reasons: List[str] = []
    warn_only_reasons: List[str] = []

    # Hard safety signals → rollback. Order matches the runbook.
    if provider_errors > 0:
        rollback_reasons.append(
            f"provider_errors={provider_errors} — provider failures must be addressed"
        )
    if overstrong_like > 0:
        rollback_reasons.append(
            f"overstrong_like={overstrong_like} — critical mismatch detected with "
            "strong support (M6.5-style failure mode)"
        )
    if exit_code == 2:
        rollback_reasons.append(
            "semantic was expected enabled but unavailable "
            "(smoke_semantic_canary exit code 2 / --fail-on-semantic-unavailable)"
        )
    elif semantic_enabled > 0 and semantic_available == 0:
        # Even without --fail-on-semantic-unavailable, configured-but-
        # unavailable semantic on a canary run is a rollback trigger:
        # the canary was meant to measure live semantic behavior and the
        # provider isn't answering.
        rollback_reasons.append(
            "semantic_enabled=1 but semantic_available=0 — provider configured "
            "but unavailable"
        )
    if exit_code == 1:
        rollback_reasons.append(
            "smoke_semantic_canary exited 1 (script / server / result-shape failure)"
        )

    # Soft signals — these are warn-only and must NOT promote to rollback.
    if cap_ratio > _CANARY_WARN_CAP_RATIO:
        warn_only_reasons.append(
            f"cap_ratio={cap_ratio:.3f} > {_CANARY_WARN_CAP_RATIO:.2f} — "
            "guardrails carrying high safety load; investigate input drift"
        )
    if runtime_p95 > _CANARY_WARN_RUNTIME_MS_P95:
        warn_only_reasons.append(
            f"runtime_p95_ms={runtime_p95} > {_CANARY_WARN_RUNTIME_MS_P95} — "
            "verify Render request budget (cold-start / warm-cache effects)"
        )

    rollback_recommended = bool(rollback_reasons)

    if rollback_recommended:
        semantic_safety_status = _HEALTH_FAIL
    elif provider_errors == 0 and overstrong_like == 0:
        # Clean safety signals. Note semantic_enabled=0 with available=0
        # is also "pass" — the canary just isn't measuring live semantic.
        semantic_safety_status = _HEALTH_PASS
    else:
        semantic_safety_status = _HEALTH_WARN

    semantic_runtime_status = _HEALTH_WARN if warn_only_reasons else _HEALTH_PASS

    if semantic_safety_status == _HEALTH_PASS:
        safety_summary = (
            "semantic safety clean: "
            f"provider_errors=0 overstrong_like=0 "
            f"semantic_enabled={semantic_enabled} "
            f"semantic_available={semantic_available}"
        )
    elif semantic_safety_status == _HEALTH_FAIL:
        safety_summary = "semantic safety FAIL — " + "; ".join(rollback_reasons)
    else:
        safety_summary = "semantic safety degraded but no rollback trigger"

    return {
        "semantic_safety_status": semantic_safety_status,
        "semantic_runtime_status": semantic_runtime_status,
        "rollback_recommended": rollback_recommended,
        "rollback_reasons": rollback_reasons,
        "warn_only_reasons": warn_only_reasons,
        "semantic_safety_summary": safety_summary,
    }


def _parse_smoke_canary_output(stdout: str, stderr: str, exit_code: int) -> dict:
    """Phase 2 M8.4: parse the canary scorecard and classify safety vs. runtime.

    smoke_semantic_canary exit codes:
        0 — clean
        1 — script / server failure → fail
        2 — semantic unavailable when expected → fail
        3 — health warn/fail when --fail-on-health-warn was set → warn or fail

    Every code path now goes through ``_classify_canary_semantic_safety``
    so the report always carries the new M8.4 fields (semantic_safety_status,
    semantic_runtime_status, rollback_recommended, rollback_reasons,
    warn_only_reasons, semantic_safety_summary), even when the scorecard
    line is missing or the smoke failed early.
    """
    match = _CANARY_SCORE_RE.search(stdout)
    fields: Dict[str, Any] = match.groupdict() if match else {}
    classification = _classify_canary_semantic_safety(fields, exit_code)

    metrics: Dict[str, Any] = {**fields, **classification}
    rb_flag = "true" if classification["rollback_recommended"] else "false"

    # Step status. The runner-level status uses the smoke's own health
    # mapping for exit 0; exit 1/2 are hard fails; exit 3 (warn/fail with
    # --fail-on-health-warn) is treated as warn or fail based on the
    # scorecard health value.
    if exit_code == 1:
        return {
            "status": _HEALTH_FAIL,
            "summary": (
                f"smoke_semantic_canary exited 1 — {classification['semantic_safety_summary']} "
                f"rollback_recommended={rb_flag}"
            ),
            "metrics": metrics,
        }
    if exit_code == 2:
        return {
            "status": _HEALTH_FAIL,
            "summary": (
                "smoke_semantic_canary exited 2 — semantic expected enabled but unavailable "
                f"rollback_recommended={rb_flag}"
            ),
            "metrics": metrics,
        }

    if not match:
        return {
            "status": _HEALTH_UNKNOWN if exit_code == 0 else _HEALTH_FAIL,
            "summary": "smoke_semantic_canary scorecard line not detected",
            "metrics": metrics,
        }

    health = fields.get("health", "unknown").lower()
    status_map = {"pass": _HEALTH_PASS, "warn": _HEALTH_WARN, "fail": _HEALTH_FAIL}
    status = status_map.get(health, _HEALTH_UNKNOWN)

    # When the smoke reports health=warn but the safety classifier flags a
    # rollback signal (e.g. semantic_enabled=1 but semantic_available=0),
    # promote the step status to fail. The classifier is conservative —
    # we trust it over the smoke's softer ``health`` value here.
    if classification["rollback_recommended"] and status != _HEALTH_FAIL:
        status = _HEALTH_FAIL

    summary_text = (
        "smoke_semantic_canary: "
        f"health={health} "
        f"semantic_enabled={fields['semantic_enabled']} "
        f"semantic_available={fields['semantic_available']} "
        f"provider_errors={fields['provider_errors']} "
        f"overstrong_like={fields['overstrong_like']} "
        f"cap_ratio={fields['cap_ratio']} "
        f"runtime_p95_ms={fields['runtime_p95_ms']} "
        f"semantic_safety_status={classification['semantic_safety_status']} "
        f"semantic_runtime_status={classification['semantic_runtime_status']} "
        f"rollback_recommended={rb_flag}"
    )
    return {
        "status": status,
        "summary": summary_text,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# M10.1 — source-registry profile parsers.
# ---------------------------------------------------------------------------


def _parse_validate_source_registry_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """Parse ``scripts/validate_source_registry.py --json`` output.
    The JSON payload carries ``passed`` / ``sources_count`` /
    ``issues`` / ``warnings`` — surface them as runner metrics."""
    summary_obj: Optional[dict] = None
    candidate = stdout if stdout.startswith("{") else ""
    if not candidate:
        nl = stdout.find("\n{")
        candidate = stdout[nl + 1:] if nl != -1 else ""
    if candidate:
        try:
            summary_obj = json.loads(candidate)
        except Exception:
            summary_obj = None
    if summary_obj is None:
        return {
            "status": _HEALTH_PASS if exit_code == 0 else _HEALTH_FAIL,
            "summary": (
                f"validate_source_registry exit_code={exit_code} "
                "(JSON not detected)"
            ),
        }
    passed = bool(summary_obj.get("passed"))
    status = _HEALTH_PASS if passed and exit_code == 0 else _HEALTH_FAIL
    return {
        "status": status,
        "summary": (
            f"validate_source_registry: passed={passed} "
            f"sources_count={summary_obj.get('sources_count')} "
            f"enabled_count={summary_obj.get('enabled_count')} "
            f"issues={len(summary_obj.get('issues') or [])}"
        ),
        "metrics": {
            "passed": passed,
            "sources_count": int(summary_obj.get("sources_count") or 0),
            "enabled_count": int(summary_obj.get("enabled_count") or 0),
            "browser_required_count": int(
                summary_obj.get("browser_required_count") or 0
            ),
            "issues_count": len(summary_obj.get("issues") or []),
            "warnings_count": len(summary_obj.get("warnings") or []),
        },
    }


def _parse_classify_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--help`` must exit 0 and surface the documented header."""
    ok = (
        exit_code == 0
        and "Classify URLs against" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"classify_source_url --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_classify_matched_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """Matched URL probe must exit 0, surface MATCHED + the
    candidate source_id, and carry the documented safety note."""
    has_matched = "Status: MATCHED" in stdout
    has_safety = "official_source_candidate does not imply truth" in stdout
    ok = exit_code == 0 and has_matched and has_safety
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"classify_source_url(matched): exit_code={exit_code} "
            f"matched_detected={has_matched} "
            f"safety_note_detected={has_safety}"
        ),
        "metrics": {
            "matched_detected": has_matched,
            "safety_note_detected": has_safety,
            "exit_code_ok": exit_code == 0,
        },
    }


def _parse_classify_no_match_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """No-match URL probe must exit **1** and surface NO_MATCH + the
    safety note. Exit 0 here would mean the CLI failed to enforce its
    conservative exit policy and must surface as a runner FAIL."""
    has_no_match = "Status: NO_MATCH" in stdout
    has_safety = "official_source_candidate does not imply truth" in stdout
    ok = exit_code == 1 and has_no_match and has_safety
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"classify_source_url(no_match): exit_code={exit_code} "
            f"no_match_detected={has_no_match} "
            f"safety_note_detected={has_safety}"
        ),
        "metrics": {
            "no_match_detected": has_no_match,
            "safety_note_detected": has_safety,
            "exit_code_was_one": exit_code == 1,
        },
    }


# ---------------------------------------------------------------------------
# M10.2 — source-crawler profile parsers.
# ---------------------------------------------------------------------------


def _parse_fetch_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``fetch_registry_source.py --help`` must exit 0 and surface
    the documented header text."""
    ok = (
        exit_code == 0
        and "fetch_registry_source" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"fetch_registry_source --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_fetch_dry_run_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """The dry-run probe runs against a default_enabled=false seed
    entry, so the M10.2 safety check refuses. Expected:

        * exit_code = 1
        * stdout carries "DRY RUN" + "safety_refusal" + the safety notes
        * network_fetch_performed is False (the CLI prints that line)

    Anything else is a regression — including the dangerous shape
    "exit_code=0 with no DRY RUN line", which would mean the safety
    check no longer fires."""
    has_dry_run = "DRY RUN" in stdout
    has_safety_refusal = (
        "safety_refusal" in stdout or "would refuse fetch" in stdout
    )
    has_truth_note = "truth_claim: False" in stdout
    no_network = "network_fetch_performed: False" in stdout
    ok = (
        exit_code == 1
        and has_dry_run
        and has_safety_refusal
        and has_truth_note
        and no_network
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"fetch_registry_source(dry_run): exit_code={exit_code} "
            f"dry_run_detected={has_dry_run} "
            f"safety_refusal_detected={has_safety_refusal} "
            f"truth_note_detected={has_truth_note} "
            f"no_network={no_network}"
        ),
        "metrics": {
            "exit_code_was_one": exit_code == 1,
            "dry_run_detected": has_dry_run,
            "safety_refusal_detected": has_safety_refusal,
            "truth_note_detected": has_truth_note,
            "no_network": no_network,
        },
    }


# ---------------------------------------------------------------------------
# M10.3 — source-enable profile parsers.
# ---------------------------------------------------------------------------


def _parse_enable_list_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``enable_registry_source.py --list`` must exit 0, surface at
    least one source_id row, and carry the documented safety note."""
    has_header = "Registry Source Status" in stdout
    has_total = "Total:" in stdout
    has_safety = (
        "does NOT imply truth" in stdout
        or "does not imply truth" in stdout
    )
    ok = exit_code == 0 and has_header and has_total and has_safety
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"enable_registry_source(--list): exit_code={exit_code} "
            f"header_detected={has_header} total_line_detected={has_total} "
            f"safety_note_detected={has_safety}"
        ),
        "metrics": {
            "header_detected": has_header,
            "total_line_detected": has_total,
            "safety_note_detected": has_safety,
            "exit_code_ok": exit_code == 0,
        },
    }


def _parse_enable_dry_run_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """Dry-run against the disabled seed entry must:
        * exit 0 (the spec keeps dry-run idempotently 0)
        * surface 'DRY RUN' header
        * surface the proposed default_enabled True transition
        * surface the safety note
    """
    has_dry_run = "DRY RUN" in stdout
    has_transition = "default_enabled: False -> True" in stdout
    has_safety = (
        "does NOT imply truth" in stdout
        or "does not imply truth" in stdout
    )
    ok = exit_code == 0 and has_dry_run and has_transition and has_safety
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"enable_registry_source(dry_run): exit_code={exit_code} "
            f"dry_run_detected={has_dry_run} "
            f"transition_detected={has_transition} "
            f"safety_note_detected={has_safety}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "dry_run_detected": has_dry_run,
            "transition_detected": has_transition,
            "safety_note_detected": has_safety,
        },
    }


def _parse_enable_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_enable_registry_source.py`` is a unittest runner —
    exit 0 with an 'OK' line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_enable_registry_source: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M10.4 — source-extractor profile parsers.
# ---------------------------------------------------------------------------


def _parse_extract_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``extract_artifact_text.py --help`` must exit 0 and surface the
    documented header text."""
    ok = (
        exit_code == 0
        and "Extract structured text" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"extract_artifact_text --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_extractor_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_artifact_extractor.py`` is a unittest runner —
    exit 0 with an 'OK' line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_artifact_extractor: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M10.5 — source-linker profile parsers.
# ---------------------------------------------------------------------------


def _parse_link_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``link_artifact_evidence.py --help`` must exit 0 and surface
    the documented header text."""
    ok = (
        exit_code == 0
        and "evidence candidates" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"link_artifact_evidence --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_link_list_extractions_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``link_artifact_evidence.py --list-extractions`` must exit 0
    and surface the safety footer. The number of rows depends on the
    operator's local DB — we only assert read-only success."""
    has_header = "artifact_text_extractions" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    has_safety_review = "operator_review_required=True" in stdout
    has_safety_no_pipeline = (
        "do not feed into the live analysis pipeline" in stdout
    )
    ok = (
        exit_code == 0 and has_header and has_safety_truth
        and has_safety_review and has_safety_no_pipeline
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"link_artifact_evidence(--list-extractions): "
            f"exit_code={exit_code} header_detected={has_header} "
            f"truth_note_detected={has_safety_truth} "
            f"review_note_detected={has_safety_review} "
            f"no_pipeline_note_detected={has_safety_no_pipeline}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "header_detected": has_header,
            "truth_note_detected": has_safety_truth,
            "review_note_detected": has_safety_review,
            "no_pipeline_note_detected": has_safety_no_pipeline,
        },
    }


def _parse_linker_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_artifact_evidence_linker.py`` is a unittest runner
    — exit 0 with an 'OK' line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_artifact_evidence_linker: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M11.0a — verdict-comparison profile parsers.
# ---------------------------------------------------------------------------


def _parse_compare_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``compare_verdict_producers.py --help`` must exit 0 and surface
    the documented header text."""
    ok = (
        exit_code == 0
        and "verdict producers" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"compare_verdict_producers --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_compare_summary_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """The ``--summary`` mode must exit 0 even on an empty DB. The
    output always carries the three safety notes plus the
    ``Disagreement Summary`` header."""
    has_header = "Disagreement Summary" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    has_safety_review = "operator_review_required=True" in stdout
    has_safety_no_logic = "No verdict logic was modified" in stdout
    ok = (
        exit_code == 0 and has_header and has_safety_truth
        and has_safety_review and has_safety_no_logic
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"compare_verdict_producers(--summary): exit_code={exit_code} "
            f"header_detected={has_header} "
            f"truth_note_detected={has_safety_truth} "
            f"review_note_detected={has_safety_review} "
            f"no_logic_note_detected={has_safety_no_logic}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "header_detected": has_header,
            "truth_note_detected": has_safety_truth,
            "review_note_detected": has_safety_review,
            "no_logic_note_detected": has_safety_no_logic,
        },
    }


def _parse_comparator_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_verdict_producer_comparison.py`` is a unittest
    runner — exit 0 with an 'OK' line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_verdict_producer_comparison: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M11.0b — verdict-label-diagnostic profile parsers.
# ---------------------------------------------------------------------------


def _parse_diag_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``diagnose_verdict_labels.py --help`` must exit 0 and surface
    the documented header text."""
    ok = (
        exit_code == 0
        and "_verdict_label" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"diagnose_verdict_labels --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_diag_branch_table_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--branch-table`` must exit 0, list both verified-emitting
    branches (B08 and B13), and surface the safety footer.

    M11.0c moved B08 from ``verified_without_strict_checks`` to
    ``verified_with_strict_checks``. The expected invariant is now:
    BOTH B08 and B13 appear in the table AND both carry the strict
    classification. We do NOT assert the absence of the loose bucket
    here — it remains a valid catalog constant for future regression
    detection — only that the strict classification is present on at
    least one branch in the printed table."""
    has_b08 = "B08_direct_support_only" in stdout
    has_b13 = "B13_strong_confidence_verified" in stdout
    has_strict_risk = "verified_with_strict_checks" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    ok = (
        exit_code == 0 and has_b08 and has_b13 and has_strict_risk
        and has_safety_truth
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"diagnose_verdict_labels(--branch-table): "
            f"exit_code={exit_code} b08_detected={has_b08} "
            f"b13_detected={has_b13} "
            f"strict_risk_detected={has_strict_risk} "
            f"truth_note_detected={has_safety_truth}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "b08_detected": has_b08,
            "b13_detected": has_b13,
            "strict_risk_detected": has_strict_risk,
            "truth_note_detected": has_safety_truth,
        },
    }


def _parse_diag_summary_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--summary`` must exit 0 even on an empty DB and surface the
    three safety notes plus the header."""
    has_header = "Diagnostic Summary" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    has_safety_review = "operator_review_required=True" in stdout
    has_safety_no_logic = "No verdict logic was modified" in stdout
    ok = (
        exit_code == 0 and has_header and has_safety_truth
        and has_safety_review and has_safety_no_logic
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"diagnose_verdict_labels(--summary): exit_code={exit_code} "
            f"header_detected={has_header} "
            f"truth_note_detected={has_safety_truth} "
            f"review_note_detected={has_safety_review} "
            f"no_logic_note_detected={has_safety_no_logic}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "header_detected": has_header,
            "truth_note_detected": has_safety_truth,
            "review_note_detected": has_safety_review,
            "no_logic_note_detected": has_safety_no_logic,
        },
    }


def _parse_diag_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_verdict_label_diagnostic.py`` is a unittest runner —
    exit 0 with an 'OK' line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_verdict_label_diagnostic: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_b08_fix_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_verdict_label_b08_fix.py`` pins the M11.0c B08
    gates. Same unittest runner shape — exit 0 with an 'OK' line."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_verdict_label_b08_fix: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M11.1 — legacy-review-enroll profile parsers.
# ---------------------------------------------------------------------------


def _parse_enroll_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``enroll_legacy_weak_verified.py --help`` must exit 0 and
    surface the documented header text."""
    ok = (
        exit_code == 0
        and "legacy weak-verified" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"enroll_legacy_weak_verified --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_enroll_check_status_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--check-status`` is read-only against the real DB. Must exit
    0 even when there are zero candidates, and must surface the
    safety footer."""
    has_header = "Enrollment Status" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    has_safety_review = "operator_review_required=True" in stdout
    has_safety_no_result = (
        "analysis_results.verdict_label is NOT modified" in stdout
    )
    ok = (
        exit_code == 0 and has_header and has_safety_truth
        and has_safety_review and has_safety_no_result
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"enroll_legacy_weak_verified(--check-status): "
            f"exit_code={exit_code} header_detected={has_header} "
            f"truth_note_detected={has_safety_truth} "
            f"review_note_detected={has_safety_review} "
            f"no_result_note_detected={has_safety_no_result}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "header_detected": has_header,
            "truth_note_detected": has_safety_truth,
            "review_note_detected": has_safety_review,
            "no_result_note_detected": has_safety_no_result,
        },
    }


def _parse_enroll_list_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    has_header = "Legacy Weak-Verified Candidates" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    ok = exit_code == 0 and has_header and has_safety_truth
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"enroll_legacy_weak_verified(--list): "
            f"exit_code={exit_code} header_detected={has_header} "
            f"truth_note_detected={has_safety_truth}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "header_detected": has_header,
            "truth_note_detected": has_safety_truth,
        },
    }


def _parse_enroll_dry_run_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    has_header = "dry-run" in stdout
    has_would_enroll = "Would enroll now:" in stdout
    has_safety_truth = "truth_claim=False" in stdout
    has_safety_no_auto = (
        "No auto-publication" in stdout
        or "No auto-approval" in stdout
    )
    ok = (
        exit_code == 0 and has_header and has_would_enroll
        and has_safety_truth and has_safety_no_auto
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"enroll_legacy_weak_verified(--dry-run): "
            f"exit_code={exit_code} header_detected={has_header} "
            f"would_enroll_line_detected={has_would_enroll} "
            f"truth_note_detected={has_safety_truth} "
            f"no_auto_note_detected={has_safety_no_auto}"
        ),
        "metrics": {
            "exit_code_ok": exit_code == 0,
            "header_detected": has_header,
            "would_enroll_line_detected": has_would_enroll,
            "truth_note_detected": has_safety_truth,
            "no_auto_note_detected": has_safety_no_auto,
        },
    }


def _parse_enroll_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_legacy_review_enrollment.py`` is a unittest runner —
    exit 0 with an 'OK' line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_legacy_review_enrollment: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M11.2 — korean-constants profile parsers.
# ---------------------------------------------------------------------------


def _parse_korean_constants_compile_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    ok = exit_code == 0
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"compileall(korean_constants.py): exit_code={exit_code}"
        ),
        "metrics": {"exit_code_ok": exit_code == 0},
    }


def _parse_korean_constants_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_korean_constants.py`` is a unittest runner — exit
    0 with an 'OK' line means the suite passed (immutability +
    regression pins + import-graph wiring all green)."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_korean_constants: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M12.0a — postgres-dual-write profile parsers.
# ---------------------------------------------------------------------------


def _parse_postgres_health_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``check_postgres_health.py --help`` must exit 0 and surface
    the documented header text."""
    ok = (
        exit_code == 0
        and "check_postgres_health" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_postgres_health --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_postgres_health_default_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """Default-env health report. The expected shape in CI / local
    validation runs is dual-write DISABLED — that's a clean PASS.
    A live Postgres on the operator's machine is fine too (PASS).
    The only failure shape is "enabled but cannot connect" (exit 1)
    or any exit code other than 0/1.
    """
    has_header = "Postgres Dual-Write Health" in stdout
    has_safety_footer = "SQLite remains the source of truth" in stdout
    disabled_line = "dual_write_enabled:    False" in stdout
    enabled_line = "dual_write_enabled:    True" in stdout
    if exit_code == 0 and has_header and has_safety_footer:
        status = _HEALTH_PASS
        if disabled_line:
            summary = (
                "check_postgres_health: disabled (USE_POSTGRES_WRITE "
                "unset) — SQLite remains the sole source of truth"
            )
        elif enabled_line:
            summary = (
                "check_postgres_health: enabled and reachable "
                "(SQLite still sole source of truth)"
            )
        else:
            summary = "check_postgres_health: clean exit, status unclear"
    elif exit_code == 1:
        status = _HEALTH_FAIL
        summary = (
            "check_postgres_health: dual-write enabled but cannot "
            "connect (operator action needed)"
        )
    else:
        status = _HEALTH_FAIL
        summary = f"check_postgres_health: exit_code={exit_code}"
    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "exit_code": exit_code,
            "header_detected": has_header,
            "safety_footer_detected": has_safety_footer,
            "disabled_line_detected": disabled_line,
            "enabled_line_detected": enabled_line,
        },
    }


def _parse_postgres_storage_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_postgres_storage.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_postgres_storage: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M12.0b — postgres-backfill profile parsers.
# ---------------------------------------------------------------------------


def _parse_postgres_backfill_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``run_postgres_backfill.py --help`` must exit 0 and surface
    the documented header text + the Exit codes section."""
    ok = (
        exit_code == 0
        and "run_postgres_backfill" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"run_postgres_backfill --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_postgres_backfill_status_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """Default-env --status. Expected PASS shape in CI / local
    validation: dual-write DISABLED, clear instructions, exit 0.
    A live Postgres on the operator's machine is also fine (PASS)
    as long as the report finishes cleanly. The only failure shape
    is a non-zero exit code or a missing safety footer."""
    has_header = "Postgres Backfill Status" in stdout
    has_safety = "SQLite remains the source of truth" in stdout
    disabled_line = "Postgres dual-write enabled: False" in stdout
    enabled_line = "Postgres dual-write enabled: True" in stdout
    if exit_code == 0 and has_header:
        status = _HEALTH_PASS
        if disabled_line:
            summary = (
                "run_postgres_backfill --status: disabled "
                "(USE_POSTGRES_WRITE unset) — backfill is a no-op"
            )
        elif enabled_line:
            summary = (
                "run_postgres_backfill --status: enabled "
                "— ready to dry-run / execute"
            )
        else:
            summary = "run_postgres_backfill --status: clean exit"
    else:
        status = _HEALTH_FAIL
        summary = (
            f"run_postgres_backfill --status: exit_code={exit_code}"
        )
    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "exit_code": exit_code,
            "header_detected": has_header,
            "safety_footer_detected": has_safety,
            "disabled_line_detected": disabled_line,
            "enabled_line_detected": enabled_line,
        },
    }


def _parse_postgres_backfill_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_postgres_backfill.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_postgres_backfill: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M13.1a — llm-judge-dry-run profile parsers.
# ---------------------------------------------------------------------------


def _parse_llm_judge_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``dry_run_llm_judge.py --help`` must exit 0 and surface
    the documented header text + the Exit codes section."""
    ok = (
        exit_code == 0
        and "dry_run_llm_judge" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"dry_run_llm_judge --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_llm_judge_status_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--status`` must exit 0, list both stub providers, and
    surface the M13.1a safety footer (no pipeline connection)."""
    has_header = "LLM Judge Provider Status" in stdout
    has_anthropic_stub = "anthropic_stub" in stdout
    has_openai_stub = "openai_stub" in stdout
    has_safety = "NOT connected to analyze_pipeline" in stdout
    ok = (
        exit_code == 0
        and has_header
        and has_anthropic_stub
        and has_openai_stub
        and has_safety
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"dry_run_llm_judge --status: exit_code={exit_code} "
            f"header={has_header} anthropic_stub={has_anthropic_stub} "
            f"openai_stub={has_openai_stub} safety_footer={has_safety}"
        ),
        "metrics": {
            "exit_code": exit_code,
            "header_detected": has_header,
            "anthropic_stub_detected": has_anthropic_stub,
            "openai_stub_detected": has_openai_stub,
            "safety_footer_detected": has_safety,
        },
    }


def _parse_llm_judge_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_llm_judge.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_llm_judge: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M13.2a — frontend-build profile parsers.
# ---------------------------------------------------------------------------


def _parse_frontend_build_status_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``frontend/build_index.py --status`` is read-only and must
    exit 0 with the documented label set."""
    has_template = "Template:" in stdout
    has_css = "CSS:" in stdout
    has_served = "Served HTML:" in stdout
    has_checksum_label = "Checksum" in stdout
    ok = (
        exit_code == 0
        and has_template
        and has_css
        and has_served
        and has_checksum_label
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"frontend_build --status: exit_code={exit_code} "
            f"template={has_template} css={has_css} "
            f"served={has_served} checksum={has_checksum_label}"
        ),
        "metrics": {
            "exit_code": exit_code,
            "template_label": has_template,
            "css_label": has_css,
            "served_label": has_served,
            "checksum_label": has_checksum_label,
        },
    }


def _parse_frontend_build_check_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--check`` exit 0 with ``matches build output exactly`` means
    the byte-identical guarantee holds. Anything else is a drift and
    must surface as a runner-level FAIL."""
    matches_line = "matches build output exactly" in stdout
    drift_signal = (
        "does not match" in stderr
        or "First diff at byte" in stderr
        or "does not exist" in stderr
    )
    ok = exit_code == 0 and matches_line and not drift_signal
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"frontend_build --check: exit_code={exit_code} "
            f"matches={matches_line} drift_signal={drift_signal}"
        ),
        "metrics": {
            "exit_code": exit_code,
            "matches_line_detected": matches_line,
            "drift_signal_detected": drift_signal,
        },
    }


def _parse_frontend_build_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_frontend_build.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_frontend_build: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M13.3a — http-cache profile parsers.
# ---------------------------------------------------------------------------


def _parse_http_cache_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``check_http_cache.py --help`` must exit 0 and surface the
    documented header text and Exit codes section."""
    ok = (
        exit_code == 0
        and "check_http_cache" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_http_cache --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_http_cache_status_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--status`` (default mode) must exit 0 and surface the
    safety footer asserting the M13.3a non-integration claim."""
    has_header = "HTTP Cache Status" in stdout
    has_safety_footer = (
        "M13.3a infrastructure only" in stdout
        and "NOT integrated" in stdout
    )
    disabled_line = "Enabled:                False" in stdout
    enabled_line = "Enabled:                True" in stdout
    if exit_code == 0 and has_header and has_safety_footer:
        status = _HEALTH_PASS
        if disabled_line:
            summary = (
                "check_http_cache --status: disabled "
                "(HTTP_CACHE_ENABLED unset) -- dormant in production"
            )
        elif enabled_line:
            summary = (
                "check_http_cache --status: enabled (operator opt-in)"
            )
        else:
            summary = "check_http_cache --status: clean exit"
    else:
        status = _HEALTH_FAIL
        summary = (
            f"check_http_cache --status: exit_code={exit_code} "
            f"header={has_header} safety_footer={has_safety_footer}"
        )
    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "exit_code": exit_code,
            "header_detected": has_header,
            "safety_footer_detected": has_safety_footer,
            "disabled_line_detected": disabled_line,
            "enabled_line_detected": enabled_line,
        },
    }


def _parse_http_cache_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_http_cache.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_http_cache: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M13.3b — official-crawler-cache profile parsers.
# ---------------------------------------------------------------------------


def _parse_official_crawler_cache_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_official_crawler_cache.py`` is a unittest runner."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_official_crawler_cache: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M13.3c — cache-measurement-dry profile parsers.
# ---------------------------------------------------------------------------


def _parse_measure_cache_impact_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    ok = (
        exit_code == 0
        and "measure_cache_impact" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"measure_cache_impact --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_check_cache_activation_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    ok = (
        exit_code == 0
        and "check_cache_activation" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_cache_activation --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_measure_cache_impact_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_measure_cache_impact: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_check_cache_activation_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_check_cache_activation: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M14.0a — structured-logging profile parsers.
# ---------------------------------------------------------------------------


def _parse_structured_logging_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``check_logging.py --help`` must exit 0 and surface the
    documented header text and Exit codes section."""
    ok = (
        exit_code == 0
        and "check_logging" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_logging --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_structured_logging_status_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """Default-env status. Expected PASS shape: LOG_FORMAT line plus
    safety footer asserting the M14.0a opt-in claim."""
    has_header = "Structured Logging Status" in stdout
    has_format_line = "LOG_FORMAT:" in stdout
    has_safety_footer = (
        "M14.0a is opt-in" in stdout
        or "JSON output enabled" in stdout
    )
    text_mode = "LOG_FORMAT:         text" in stdout
    json_mode = "LOG_FORMAT:         json" in stdout
    if exit_code == 0 and has_header and has_format_line and has_safety_footer:
        status = _HEALTH_PASS
        if text_mode:
            summary = (
                "check_logging --status: text mode (LOG_FORMAT unset)"
            )
        elif json_mode:
            summary = (
                "check_logging --status: json mode (operator opt-in)"
            )
        else:
            summary = "check_logging --status: clean exit"
    else:
        status = _HEALTH_FAIL
        summary = (
            f"check_logging --status: exit_code={exit_code} "
            f"header={has_header} format_line={has_format_line} "
            f"safety_footer={has_safety_footer}"
        )
    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "exit_code": exit_code,
            "header_detected": has_header,
            "format_line_detected": has_format_line,
            "safety_footer_detected": has_safety_footer,
            "text_mode_detected": text_mode,
            "json_mode_detected": json_mode,
        },
    }


def _parse_structured_logging_emit_sample_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``--emit-sample`` must exit 0 and surface INFO/WARNING/ERROR
    records on stderr along with the human banner on stdout."""
    has_banner = "Sample log emission" in stdout
    has_info = "INFO" in stderr
    has_warning = "WARNING" in stderr
    has_error = "ERROR" in stderr
    ok = (
        exit_code == 0 and has_banner
        and has_info and has_warning and has_error
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_logging --emit-sample: exit_code={exit_code} "
            f"banner={has_banner} levels="
            f"INFO={has_info} WARNING={has_warning} ERROR={has_error}"
        ),
        "metrics": {
            "exit_code": exit_code,
            "banner_detected": has_banner,
            "info_detected": has_info,
            "warning_detected": has_warning,
            "error_detected": has_error,
        },
    }


# ---------------------------------------------------------------------------
# M14.0b — print-migration profile parsers.
# ---------------------------------------------------------------------------


def _parse_print_migration_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_print_migration.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_print_migration: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_print_migration_m14_0c_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_print_migration_m14_0c.py`` is a unittest runner;
    it subprocess-invokes the verdict test suites so its exit reflects
    both the migration pins AND verdict invariance."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_print_migration_m14_0c: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


# ---------------------------------------------------------------------------
# M14.2 — json-logging-verification profile parsers.
# ---------------------------------------------------------------------------


def _parse_check_json_logging_help_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    ok = (
        exit_code == 0
        and "check_json_logging" in stdout
        and "Exit codes" in stdout
    )
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_json_logging --help: exit_code={exit_code} "
            f"help_text_detected={ok}"
        ),
    }


def _parse_check_json_logging_local_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``check_json_logging.py --local`` must exit 0 and surface the
    'PASS' marker. Anything else indicates the JSON schema isn't
    being produced as M14.0a guaranteed."""
    has_header = "JSON Logging Verification (local)" in stdout
    has_pass = "Result: PASS" in stdout
    ok = exit_code == 0 and has_header and has_pass
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"check_json_logging --local: exit_code={exit_code} "
            f"header={has_header} pass={has_pass}"
        ),
        "metrics": {
            "exit_code": exit_code,
            "header_detected": has_header,
            "pass_marker_detected": has_pass,
        },
    }


def _parse_check_json_logging_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_check_json_logging.py`` is a unittest runner —
    exit 0 + ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_check_json_logging: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_compileall_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``python -m compileall`` exits 0 on success and 1 on any
    syntax error. Output to stdout / stderr is purely informational."""
    return {
        "status": _HEALTH_PASS if exit_code == 0 else _HEALTH_FAIL,
        "summary": (
            f"compileall: exit_code={exit_code}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
        },
    }


# ---------------------------------------------------------------------------
# M14.3a — request-id context profile parsers.
# ---------------------------------------------------------------------------


def _parse_request_context_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_request_context: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_api_request_id_middleware_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_api_request_id_middleware: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_structured_logging_tests_output(
    stdout: str, stderr: str, exit_code: int,
) -> dict:
    """``tests/test_structured_logging.py`` is a unittest runner —
    exit 0 with an ``OK`` line means the suite passed."""
    has_ok = "\nOK" in (stdout + "\n" + stderr)
    ok = exit_code == 0 and has_ok
    return {
        "status": _HEALTH_PASS if ok else _HEALTH_FAIL,
        "summary": (
            f"test_structured_logging: exit_code={exit_code} "
            f"ok_detected={has_ok}"
        ),
        "metrics": {
            "exit_code_was_zero": exit_code == 0,
            "ok_detected": has_ok,
        },
    }


def _parse_review_token_gate_output(stdout: str, stderr: str, exit_code: int) -> dict:
    """Phase 2 M9.5: parse ``smoke_review_api_token_gate.py`` output.

    Same JSON-tail / fallback shape as the exposure parser. Surfaces
    the M9.5 metrics distinctly from the M8.8 exposure parser so the
    runner record can be inspected without confusing the two profiles.
    """
    summary_obj: Optional[dict] = None
    if stdout.startswith("{"):
        candidate = stdout
    else:
        start = stdout.find("\n{")
        candidate = stdout[start + 1:] if start != -1 else ""
    if candidate:
        try:
            summary_obj = json.loads(candidate)
        except Exception:
            summary_obj = None
            idx = candidate.rfind("\n}")
            while idx != -1 and summary_obj is None:
                try:
                    summary_obj = json.loads(candidate[: idx + 2])
                except Exception:
                    idx = candidate.rfind("\n}", 0, idx)

    if summary_obj is None:
        passed = exit_code == 0
        return {
            "status": _HEALTH_PASS if passed else _HEALTH_FAIL,
            "summary": (
                f"smoke_review_api_token_gate exit_code={exit_code} "
                "(JSON summary not detected)"
            ),
        }

    passed = bool(summary_obj.get("passed"))
    public_access_detected = bool(summary_obj.get("public_access_detected"))
    disabled_detected = bool(summary_obj.get("disabled_detected"))
    token_gate_ok = bool(summary_obj.get("token_gate_ok"))
    valid_token_read_ok = bool(summary_obj.get("valid_token_read_ok"))
    auth_passed_not_found_count = int(
        summary_obj.get("auth_passed_not_found_count") or 0
    )
    token_required_count = int(summary_obj.get("token_required_count") or 0)
    disabled_count = int(summary_obj.get("disabled_count") or 0)
    unexpected_count = int(summary_obj.get("unexpected_count") or 0)
    recommendation = str(summary_obj.get("recommendation") or "")

    # public_access is the hard fail signal; otherwise trust the
    # smoke's own ``passed`` decision.
    if public_access_detected:
        status = _HEALTH_FAIL
    elif passed:
        status = _HEALTH_PASS
    else:
        status = _HEALTH_FAIL

    summary_text = (
        f"smoke_review_api_token_gate: passed={passed} "
        f"public_access_detected={public_access_detected} "
        f"disabled_detected={disabled_detected} "
        f"token_gate_ok={token_gate_ok} "
        f"valid_token_read_ok={valid_token_read_ok} "
        f"token_required={token_required_count} "
        f"auth_passed_not_found={auth_passed_not_found_count} "
        f"unexpected={unexpected_count}"
    )
    return {
        "status": status,
        "summary": summary_text,
        "metrics": {
            "public_access_detected": public_access_detected,
            "disabled_detected": disabled_detected,
            "token_gate_ok": token_gate_ok,
            "valid_token_read_ok": valid_token_read_ok,
            "auth_passed_not_found_count": auth_passed_not_found_count,
            "token_required_count": token_required_count,
            "disabled_count": disabled_count,
            "unexpected_count": unexpected_count,
            "recommendation": recommendation,
        },
    }


def _parse_review_exposure_output(stdout: str, stderr: str, exit_code: int) -> dict:
    """Phase 2 M8.8: parse ``smoke_review_api_exposure.py`` output.

    The smoke prints a human summary followed by a JSON dump. The parser
    prefers the JSON tail when present so the runner record carries the
    structured counts (``public_access_detected``, ``disabled_count``,
    ``token_required_count``, ``unexpected_count``,
    ``expectation_mismatch_count``, ``expectation_mode``,
    ``recommendation``). Falls back to exit-code-only on parse failure.
    """
    summary_obj: Optional[dict] = None
    # Find the JSON tail: either at column 0 right at the start of stdout
    # (the smoke prints headerless JSON in --json mode) or after the
    # first newline (default human-summary + JSON tail).
    if stdout.startswith("{"):
        candidate = stdout
    else:
        start = stdout.find("\n{")
        candidate = stdout[start + 1:] if start != -1 else ""
    if candidate:
        try:
            summary_obj = json.loads(candidate)
        except Exception:
            summary_obj = None
            idx = candidate.rfind("\n}")
            while idx != -1 and summary_obj is None:
                try:
                    summary_obj = json.loads(candidate[: idx + 2])
                except Exception:
                    idx = candidate.rfind("\n}", 0, idx)

    if summary_obj is None:
        passed = exit_code == 0
        return {
            "status": _HEALTH_PASS if passed else _HEALTH_FAIL,
            "summary": (
                f"smoke_review_api_exposure exit_code={exit_code} "
                "(JSON summary not detected)"
            ),
        }

    passed = bool(summary_obj.get("passed"))
    public_access_detected = bool(summary_obj.get("public_access_detected"))
    disabled_count = int(summary_obj.get("disabled_count") or 0)
    token_required_count = int(summary_obj.get("token_required_count") or 0)
    unexpected_count = int(summary_obj.get("unexpected_count") or 0)
    mismatch_count = int(summary_obj.get("expectation_mismatch_count") or 0)
    expectation_mode = str(summary_obj.get("expectation_mode") or "")
    recommendation = str(summary_obj.get("recommendation") or "")

    # public_access is the hard fail signal; expectation_mismatch /
    # unexpected also block pass. Otherwise the smoke's own ``passed``
    # is the source of truth.
    if public_access_detected:
        status = _HEALTH_FAIL
    elif passed:
        status = _HEALTH_PASS
    else:
        status = _HEALTH_FAIL

    summary_text = (
        f"smoke_review_api_exposure: passed={passed} "
        f"public_access_detected={public_access_detected} "
        f"disabled={disabled_count} token_required={token_required_count} "
        f"unexpected={unexpected_count} mismatch={mismatch_count} "
        f"expectation_mode={expectation_mode}"
    )
    return {
        "status": status,
        "summary": summary_text,
        "metrics": {
            "public_access_detected": public_access_detected,
            "disabled_count": disabled_count,
            "token_required_count": token_required_count,
            "unexpected_count": unexpected_count,
            "expectation_mismatch_count": mismatch_count,
            "expectation_mode": expectation_mode,
            "recommendation": recommendation,
        },
    }


def _parse_review_local_output(stdout: str, stderr: str, exit_code: int) -> dict:
    """Phase 2 M8.3: parse ``smoke_review_workflow.py --self-contained`` output.

    The smoke prints a deterministic human-readable summary block followed
    by the same data as JSON. The parser prefers the JSON tail when present
    (so future schema additions surface in the runner's report) and falls
    back to the ``passed=...`` line otherwise.
    """
    if exit_code not in (0, 1):
        return {
            "status": _HEALTH_FAIL,
            "summary": f"smoke_review_workflow exited {exit_code}",
        }
    summary_obj: Optional[dict] = None
    # The JSON dump starts at the first '{' at column 0 and ends at the
    # matching closing brace at column 0.
    start = stdout.find("\n{")
    if start != -1:
        candidate = stdout[start + 1:]
        # Try progressively shorter candidates until json.loads succeeds.
        try:
            summary_obj = json.loads(candidate)
        except Exception:
            summary_obj = None
            for marker in ("\n}\n", "\n}"):
                idx = candidate.rfind(marker)
                while idx != -1:
                    try:
                        summary_obj = json.loads(candidate[: idx + 2])
                        break
                    except Exception:
                        idx = candidate.rfind(marker, 0, idx)
                if summary_obj is not None:
                    break
    if summary_obj is None:
        passed = exit_code == 0
        return {
            "status": _HEALTH_PASS if passed else _HEALTH_FAIL,
            "summary": (
                f"smoke_review_workflow exit_code={exit_code} "
                "(JSON summary block not detected)"
            ),
        }
    overall = bool(summary_obj.get("passed"))
    sub_results = {
        key: bool((summary_obj.get(key) or {}).get("passed"))
        for key in (
            "disabled_check", "token_check", "task_creation_check",
            "idempotency_check", "list_detail_check", "decision_check",
            "verdict_isolation_check", "publication_absent_check",
            "audit_trail_check",   # M9.0
            "audit_packet_check",  # M9.1
        )
    }
    fail_keys = [k for k, v in sub_results.items() if not v]
    return {
        "status": _HEALTH_PASS if overall else _HEALTH_FAIL,
        "summary": (
            f"smoke_review_workflow: passed={overall} "
            + ("all 10 checks ok" if overall else f"failed=[{', '.join(fail_keys)}]")
        ),
        "metrics": sub_results,
    }


def _parse_historical_dry_run_output(stdout: str, stderr: str, exit_code: int) -> dict:
    if exit_code != 0:
        return {"status": _HEALTH_FAIL, "summary": f"historical dry-run exited {exit_code}"}
    emitted_match = re.search(r"emitted=(\d+)", stdout)
    skipped_match = re.search(r"skipped=(\d+)", stdout)
    emitted = emitted_match.group(1) if emitted_match else "?"
    skipped = skipped_match.group(1) if skipped_match else "?"
    return {
        "status": _HEALTH_PASS,
        "summary": f"historical dry-run: emitted={emitted} skipped={skipped}",
        "metrics": {"emitted": emitted, "skipped": skipped},
    }


def _parse_historical_eval_output(stdout: str, stderr: str, exit_code: int) -> dict:
    if exit_code != 0:
        return {"status": _HEALTH_FAIL, "summary": f"historical eval exited {exit_code}"}
    score_match = re.search(
        r"cases=(\d+) pass=(\d+) fail=(\d+).*?overstrong=(\d+)",
        stdout,
    )
    if not score_match:
        return {
            "status": _HEALTH_UNKNOWN,
            "summary": "historical eval scorecard not detected",
        }
    cases, passed, failed, overstrong = score_match.groups()
    status = _HEALTH_PASS if int(failed) == 0 and int(overstrong) == 0 else _HEALTH_WARN
    return {
        "status": status,
        "summary": (
            f"historical eval: cases={cases} pass={passed} fail={failed} "
            f"overstrong={overstrong}"
        ),
        "metrics": {
            "cases": cases, "pass": passed, "fail": failed, "overstrong": overstrong,
        },
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _format_command(cmd: List[str]) -> str:
    return " ".join(cmd)


def _tail(text: str, n_lines: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= n_lines:
        return text.rstrip()
    return "\n".join(lines[-n_lines:]).rstrip()


def _run_step(step: dict, *, dry_run: bool, runner=None) -> dict:
    """Execute one step or simulate it under --dry-run. ``runner`` is an
    optional injected callable for tests so we can run the orchestration
    logic without spawning real subprocesses."""
    record: dict = {
        "name": step["name"],
        "command": _format_command(step["command"]),
        "hits_render": bool(step.get("hits_render")),
        "may_call_openai": bool(step.get("may_call_openai")),
    }

    if dry_run:
        record.update({
            "exit_code": None,
            "duration_seconds": 0.0,
            "status": _HEALTH_SKIPPED,
            "summary": "dry-run (not executed)",
            "stdout_tail": "",
            "stderr_tail": "",
        })
        return record

    if step.get("requires_file"):
        path = Path(step["requires_file"])
        if not path.exists():
            record.update({
                "exit_code": None,
                "duration_seconds": 0.0,
                "status": _HEALTH_SKIPPED,
                "summary": f"optional step skipped — required file missing: {path}",
                "stdout_tail": "",
                "stderr_tail": "",
            })
            return record

    started = time.perf_counter()
    print(f"\n[ops] $ {_format_command(step['command'])}")
    if runner is not None:
        # Test-friendly injection point.
        exit_code, stdout, stderr = runner(step["command"])
    else:
        try:
            completed = subprocess.run(
                step["command"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            exit_code = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except Exception as error:
            exit_code = -1
            stdout = ""
            stderr = f"{type(error).__name__}: {error}"

    duration = time.perf_counter() - started

    parser = step.get("parser")
    if parser is None:
        parsed = {"status": _HEALTH_PASS if exit_code == 0 else _HEALTH_FAIL,
                  "summary": f"exit_code={exit_code}"}
    else:
        try:
            parsed = parser(stdout, stderr, exit_code)
        except Exception as error:
            parsed = {
                "status": _HEALTH_UNKNOWN,
                "summary": f"parser raised: {type(error).__name__}: {error}",
            }

    record.update({
        "exit_code": exit_code,
        "duration_seconds": round(duration, 3),
        "status": parsed.get("status") or (_HEALTH_PASS if exit_code == 0 else _HEALTH_FAIL),
        "summary": parsed.get("summary") or "",
        "metrics": parsed.get("metrics") or {},
        "stdout_tail": _tail(stdout, STDOUT_TAIL_LINES),
        "stderr_tail": _tail(stderr, STDERR_TAIL_LINES),
    })
    return record


# ---------------------------------------------------------------------------
# Consolidation + reporting
# ---------------------------------------------------------------------------


def _classify_overall(records: List[dict]) -> str:
    statuses = {r.get("status") for r in records}
    if _HEALTH_FAIL in statuses:
        return _HEALTH_FAIL
    if _HEALTH_WARN in statuses:
        return _HEALTH_WARN
    if _HEALTH_UNKNOWN in statuses:
        return _HEALTH_UNKNOWN
    return _HEALTH_PASS


def _next_actions(overall: str, records: List[dict]) -> List[str]:
    """Operational hints based on the overall status. Conservative.

    M8.4: when a semantic canary step is present, use its
    ``rollback_recommended`` / ``rollback_reasons`` / ``warn_only_reasons``
    classification to split clear rollback guidance from runtime-only
    warnings. Runtime-only warnings explicitly do **not** trigger a
    rollback recommendation; the operator should re-run after caches
    warm up. Hard safety signals (provider_errors, overstrong_like,
    semantic unavailable while expected) always do.
    """
    # M8.8 + M9.5 — public-exposure failure (from either smoke) must
    # always surface a specific rollback hint, ahead of any other
    # recommendation. Inspect the exposure / token-gate step metrics.
    exposure_records = [
        r for r in records
        if str(r.get("name", "")).startswith("smoke_review_api_exposure(")
        or str(r.get("name", "")) == "smoke_review_api_token_gate"
    ]
    public_exposure_records = [
        r for r in exposure_records
        if (r.get("metrics") or {}).get("public_access_detected")
    ]
    if public_exposure_records:
        actions: List[str] = [
            "PUBLIC EXPOSURE detected: at least one /review/* endpoint "
            "returned 2xx WITHOUT a valid token. Set REVIEW_API_ENABLED=false "
            "in the Render dashboard immediately and investigate.",
        ]
        for r in public_exposure_records:
            rec = (r.get("metrics") or {}).get("recommendation") or ""
            if rec:
                actions.append(f"{r['name']}: {rec}")
        return actions

    # M9.5-specific failure paths — surface before generic fail hints
    # so the operator knows whether the deploy is just disabled (not a
    # bug) or the token doesn't match.
    token_gate_records = [
        r for r in records
        if str(r.get("name", "")) == "smoke_review_api_token_gate"
    ]
    token_gate_fail_records = [
        r for r in token_gate_records
        if r.get("status") == _HEALTH_FAIL
        and not (r.get("metrics") or {}).get("public_access_detected")
    ]
    if token_gate_fail_records:
        actions: List[str] = []
        for r in token_gate_fail_records:
            metrics = r.get("metrics") or {}
            if metrics.get("disabled_detected"):
                actions.append(
                    f"{r['name']}: review API is disabled on this deploy "
                    "(503). No public exposure detected. If you intended the "
                    "API to stay disabled, run the review-exposure profile "
                    "instead — that's the right check for current Render policy."
                )
            else:
                rec = metrics.get("recommendation") or ""
                actions.append(f"{r['name']}: {rec or 'token-gate fail'}")
        actions.append(
            "Do not paste the review token into chat or any committed "
            "file. Keep REVIEW_API_SMOKE_TOKEN local-only and clear the "
            "env var after the smoke completes."
        )
        return actions

    canary_records = [
        r for r in records
        if str(r.get("name", "")).startswith("smoke_semantic_canary(")
    ]
    rollback_records = [
        r for r in canary_records
        if (r.get("metrics") or {}).get("rollback_recommended")
    ]
    runtime_only_records = [
        r for r in canary_records
        if (r.get("metrics") or {}).get("semantic_runtime_status") == _HEALTH_WARN
        and not (r.get("metrics") or {}).get("rollback_recommended")
    ]

    if rollback_records:
        actions: List[str] = []
        for r in rollback_records:
            reasons = (r.get("metrics") or {}).get("rollback_reasons") or []
            joined = "; ".join(reasons) if reasons else "rollback_recommended=true"
            actions.append(f"{r['name']}: rollback_recommended=true — {joined}")
        actions.append(
            "Roll back the Render semantic env vars in the dashboard "
            "(SEMANTIC_MATCHING_ENABLED=false, EMBEDDING_PROVIDER=disabled) "
            "and re-run --profile post-commit to confirm the legacy verdict "
            "path is unchanged."
        )
        actions.append(
            "Do not commit code that broke validate.py. Reports under "
            "reports/ are gitignored — keep them locally for the postmortem."
        )
        return actions

    if overall == _HEALTH_FAIL:
        return [
            "At least one step failed. Inspect stderr_tail in the JSON report.",
            "For non-canary failures, fix the failing step locally and re-run "
            "--profile quick before pushing.",
            "Do not commit code that broke validate.py.",
        ]

    if runtime_only_records:
        actions = []
        for r in runtime_only_records:
            warns = (r.get("metrics") or {}).get("warn_only_reasons") or []
            joined = "; ".join(warns) if warns else "runtime warn"
            actions.append(
                f"{r['name']}: runtime-only warn — {joined}. Semantic safety "
                "signals are clean (provider_errors=0, overstrong_like=0, "
                "semantic_available=1); no rollback recommended."
            )
        actions.append(
            "Runtime-only semantic canary warning detected. Re-run after a "
            "few minutes to see if warm caches resolve the warning; no "
            "rollback needed unless the warn pattern persists across runs."
        )
        return actions

    if overall == _HEALTH_WARN:
        return [
            "Warn-level signals detected. Common causes: cold-start runtime, "
            "small-sample cap_ratio math. Re-run after a few minutes to see "
            "if warm caches resolve the warning.",
            "No rollback needed unless the warn pattern persists across runs.",
        ]
    if overall == _HEALTH_UNKNOWN:
        return [
            "Some step output didn't parse. Check stdout_tail in the JSON "
            "report — the run may still be healthy; the runner just couldn't "
            "extract structured metrics.",
        ]
    return [
        "All checks passed. Safe to proceed.",
    ]


def _build_report(args: argparse.Namespace, records: List[dict],
                  started_iso: str, finished_iso: str, duration: float) -> dict:
    overall = _classify_overall(records) if records else _HEALTH_PASS
    warnings: List[str] = []
    for r in records:
        if r.get("status") == _HEALTH_WARN:
            warnings.append(f"{r['name']}: {r.get('summary')}")
    return {
        "profile": args.profile,
        "started_at": started_iso,
        "finished_at": finished_iso,
        "duration_seconds": round(duration, 3),
        "base_url": args.base_url,
        "query": args.query,
        "secondary_query": args.secondary_query if args.include_secondary_query else None,
        "max_news": args.max_news,
        "dry_run": bool(args.dry_run),
        "commands": records,
        "overall_status": overall,
        "warnings": warnings,
        "next_actions": _next_actions(overall, records),
    }


def _format_markdown(report: dict) -> str:
    lines: List[str] = []
    lines.append("# Operational Check Report")
    lines.append("")
    lines.append(f"- profile: `{report['profile']}`")
    lines.append(f"- base_url: `{report['base_url']}`")
    lines.append(f"- started_at: `{report['started_at']}`")
    lines.append(f"- finished_at: `{report['finished_at']}`")
    lines.append(f"- duration_seconds: {report['duration_seconds']}")
    lines.append(f"- dry_run: `{report['dry_run']}`")
    lines.append(f"- overall_status: **`{report['overall_status']}`**")
    lines.append("")
    lines.append("## Commands")
    lines.append("")
    lines.append("| step | status | exit | duration | summary |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in report["commands"]:
        lines.append(
            f"| `{r['name']}` | `{r['status']}` | "
            f"`{r.get('exit_code')}` | `{r.get('duration_seconds')}s` | "
            f"{r.get('summary', '').replace('|', '\\|')} |"
        )
    lines.append("")
    if report.get("warnings"):
        lines.append("## Warnings")
        lines.append("")
        for w in report["warnings"]:
            lines.append(f"- {w}")
        lines.append("")
    if report.get("next_actions"):
        lines.append("## Next actions")
        lines.append("")
        for a in report["next_actions"]:
            lines.append(f"- {a}")
        lines.append("")
    lines.append(
        "> Generated by `scripts/run_operational_checks.py`. This report is "
        "operational monitoring only — it does not change verdict / "
        "confidence / methodology / export wording, and it never modifies "
        "Render env. Semantic match strength is metadata only; rule-based "
        "verification and official body matching remain authoritative."
    )
    lines.append("")
    return "\n".join(lines)


def _default_report_paths(args: argparse.Namespace) -> tuple:
    """Resolve where to write the consolidated JSON / Markdown reports.

    ``--no-default-reports`` suppresses the auto-generated timestamped
    paths under ``reports/`` but still honors any explicit
    ``--json-out`` / ``--markdown-out`` the operator passed — those
    paths are an intentional override and should always be written.
    """
    if args.no_default_reports:
        return (args.json_out, args.markdown_out)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.json_out or (ROOT / "reports" / f"operational_check_{ts}.json")
    md_path = args.markdown_out or (ROOT / "reports" / f"operational_check_{ts}.md")
    return (json_path, md_path)


def _write_outputs(report: dict, json_path: Optional[Path], md_path: Optional[Path]) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[ops] JSON written to {json_path}")
    if md_path is not None:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_format_markdown(report), encoding="utf-8")
        print(f"[ops] Markdown written to {md_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace, *, runner=None) -> dict:
    """Pure function so tests can call directly with an injected runner.

    The optional ``runner`` callable replaces ``subprocess.run`` — tests
    pass a function ``(cmd) -> (exit_code, stdout, stderr)`` so the
    orchestration logic can be exercised without spawning real processes.
    """
    started = time.perf_counter()
    started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"[ops] profile={args.profile} base_url={args.base_url}")
    if args.profile in ("render-canary", "full") and not args.no_openai_note:
        print(
            "[ops] note: render-canary may indirectly trigger OpenAI calls "
            "server-side if Render's SEMANTIC_MATCHING_ENABLED is currently true."
        )

    steps = _resolve_steps(args)
    if not steps:
        print(
            f"[ops] profile={args.profile} resolved to zero steps (skip flags?). "
            "Nothing to do.",
        )

    records: List[dict] = []
    for step in steps:
        rec = _run_step(step, dry_run=args.dry_run, runner=runner)
        records.append(rec)
        status = rec.get("status")
        print(
            f"[ops]   status={status} duration={rec.get('duration_seconds')}s "
            f"summary={rec.get('summary')}",
        )
        # Default stop-on-fail unless dry-run.
        if not args.dry_run and status == _HEALTH_FAIL:
            print(
                f"[ops] step {rec['name']!r} failed — stopping the run "
                "(no further steps will execute).",
            )
            break

    duration = time.perf_counter() - started
    finished_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = _build_report(args, records, started_iso, finished_iso, duration)

    json_path, md_path = _default_report_paths(args)
    _write_outputs(report, json_path, md_path)

    print(f"\n[ops] overall_status={report['overall_status']}")
    for w in report.get("warnings", []):
        print(f"[ops]   warn: {w}")
    for a in report.get("next_actions", []):
        print(f"[ops]   next: {a}")
    return report


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run(args)
    except KeyboardInterrupt:
        print("[ops] aborted by user", file=sys.stderr)
        return 130

    overall = report.get("overall_status")
    if overall == _HEALTH_FAIL:
        return 1
    if overall == _HEALTH_WARN and args.fail_on_warn:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
