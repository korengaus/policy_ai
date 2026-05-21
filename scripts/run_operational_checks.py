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
    "full",
)

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
        )
    }
    fail_keys = [k for k, v in sub_results.items() if not v]
    return {
        "status": _HEALTH_PASS if overall else _HEALTH_FAIL,
        "summary": (
            f"smoke_review_workflow: passed={overall} "
            + ("all 8 checks ok" if overall else f"failed=[{', '.join(fail_keys)}]")
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
    # M8.8 — public-exposure failure must always surface a specific
    # rollback hint, ahead of any other recommendation. Inspect the
    # exposure step's metrics block.
    exposure_records = [
        r for r in records
        if str(r.get("name", "")).startswith("smoke_review_api_exposure(")
    ]
    public_exposure_records = [
        r for r in exposure_records
        if (r.get("metrics") or {}).get("public_access_detected")
    ]
    if public_exposure_records:
        actions: List[str] = [
            "PUBLIC EXPOSURE detected: at least one /review/* endpoint "
            "returned 2xx WITHOUT a token. Set REVIEW_API_ENABLED=false in "
            "the Render dashboard immediately and investigate.",
        ]
        for r in public_exposure_records:
            rec = (r.get("metrics") or {}).get("recommendation") or ""
            if rec:
                actions.append(f"{r['name']}: {rec}")
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
