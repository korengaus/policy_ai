"""HTTP cache impact measurement (M13.3c).

Runs ``scripts/smoke_async_job.py`` multiple times against a target
deployment and produces a structured before/after report. Designed for
the operator to compare Render performance with the M13.3b cache flags
OFF vs ON.

This script DOES NOT toggle env vars on Render. The operator must
configure ``HTTP_CACHE_ENABLED`` and ``OFFICIAL_CRAWLER_CACHE_ENABLED``
themselves through the Render dashboard. See
``docs/CACHE_ACTIVATION_GUIDE.md`` for the full procedure.

Usage::

    python scripts/measure_cache_impact.py --help
    python scripts/measure_cache_impact.py \\
        --base-url https://policy-ai-q5ax.onrender.com \\
        --query 전세사기 --runs 3

    # Operator workflow:
    #  1. Run with --baseline-only (current Render config)
    #  2. Enable cache flags via Render dashboard
    #  3. Run with --cache-on-only and compare the two reports

Exit codes::

    0 — measurement completed (any verdict; operator decides next step)
    1 — smoke runs failed (infrastructure issue, not a cache issue)
    2 — CLI usage error

Safety:
    * No Render env vars are read or written by this script.
    * The smoke subprocess hits the live deployment — by design.
    * ``--simulate`` mode replaces the subprocess with deterministic
      synthetic measurements so CI can exercise this script without
      touching Render.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Smoke output parsing
# ---------------------------------------------------------------------------


_PASSED_RE = re.compile(r"\[smoke\]\s*PASSED")
_FAILED_RE = re.compile(r"\[smoke\]\s*FAILED")
_ELAPSED_PASS_RE = re.compile(r"elapsed\s*=\s*([\d.]+)s")
_ELAPSED_FAIL_RE = re.compile(r"after\s+([\d.]+)s")
_FINAL_STATUS_RE = re.compile(r"final_status\s*=\s*(\w+)")


def parse_smoke_output(stdout: str, stderr: str, exit_code: int) -> dict:
    """Extract ``status``, ``elapsed_seconds``, ``final_status`` from
    a smoke_async_job run. Returns a dict suitable for the JSON report.

    Tolerates either PASSED or FAILED output shapes; falls back to
    ``elapsed_seconds=None`` when neither pattern matches.
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    passed = bool(_PASSED_RE.search(combined))
    failed = bool(_FAILED_RE.search(combined))

    elapsed = None
    match = _ELAPSED_PASS_RE.search(combined)
    if match:
        try:
            elapsed = float(match.group(1))
        except ValueError:
            elapsed = None
    if elapsed is None:
        match = _ELAPSED_FAIL_RE.search(combined)
        if match:
            try:
                elapsed = float(match.group(1))
            except ValueError:
                elapsed = None

    final_status = None
    match = _FINAL_STATUS_RE.search(combined)
    if match:
        final_status = match.group(1)

    status = "pass" if (exit_code == 0 and passed and not failed) else "fail"
    return {
        "status": status,
        "elapsed_seconds": elapsed,
        "final_status": final_status,
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# Smoke invocation
# ---------------------------------------------------------------------------


def _smoke_command(args, query: str) -> list:
    return [
        sys.executable,
        str(_PROJECT_ROOT / "scripts" / "smoke_async_job.py"),
        "--base-url", args.base_url,
        "--query", query,
        "--max-news", str(args.max_news),
        "--timeout-seconds", str(int(args.timeout_seconds)),
        "--poll-interval", str(args.poll_interval),
    ]


def _run_smoke_once_real(args, query: str) -> dict:
    """Invoke ``scripts/smoke_async_job.py`` as a subprocess and parse
    the output. Returns a result dict. NEVER raises — subprocess
    errors are captured into the result."""
    cmd = _smoke_command(args, query)
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=args.timeout_seconds + 60,
        )
        return parse_smoke_output(
            completed.stdout, completed.stderr, completed.returncode,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "elapsed_seconds": None,
            "final_status": None,
            "exit_code": -1,
            "error": "subprocess timeout",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "elapsed_seconds": None,
            "final_status": None,
            "exit_code": -1,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Simulation mode — deterministic synthetic measurements for CI tests.
#
# Tests typically monkeypatch ``_run_smoke_once_real`` instead, but
# --simulate is useful when an operator wants to dry-run the report
# format without hitting Render.
# ---------------------------------------------------------------------------


# Deterministic synthetic elapsed times. Chosen so the resulting
# verdict is "pass" (mean 121.5 -> mean 61.8 = 49% speedup).
_SIM_BASELINE_ELAPSED = (124.3, 118.7, 121.4, 122.1, 119.8, 123.2, 120.5, 121.0, 122.8, 119.0)
_SIM_CACHE_ON_ELAPSED = (65.2, 58.9, 61.4, 60.0, 63.1, 59.7, 62.4, 60.8, 61.7, 60.5)


def _run_smoke_once_simulated(args, query: str, mode: str, index: int) -> dict:
    """Synthetic measurement. ``mode`` is ``"baseline"`` or
    ``"cache_on"``; ``index`` selects from the synthetic sequence."""
    pool = _SIM_BASELINE_ELAPSED if mode == "baseline" else _SIM_CACHE_ON_ELAPSED
    elapsed = pool[index % len(pool)]
    return {
        "status": "pass",
        "elapsed_seconds": elapsed,
        "final_status": "completed",
        "exit_code": 0,
        "simulated": True,
    }


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------


def _execute_runs(args, mode: str) -> dict:
    """Run warmup + measured runs for one mode. Returns a dict with
    per-run results and aggregate stats. Sleeps 2s between runs to
    avoid hammering Render."""
    results = []
    warmup = max(0, int(args.warmup or 0))
    measured = max(1, min(int(args.runs or 3), 10))

    # 2-second sleep between runs avoids hammering Render. Skipped in
    # simulate mode since the synthetic measurements never touch
    # Render -- keeps test runtime short.
    inter_run_sleep = 0.0 if args.simulate else 2.0

    # Progress prints go to STDERR so --json mode keeps stdout clean
    # for JSON parsers (tests, jq pipelines). Operators still see
    # the running tally during a long Render run because most
    # terminals merge stdout/stderr for the user.
    def _progress(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    # Warmup runs (not counted).
    for index in range(warmup):
        _progress(f"[measure] {mode} warmup {index + 1}/{warmup} ...")
        if args.simulate:
            _run_smoke_once_simulated(args, args.query, mode, index)
        else:
            _run_smoke_once_real(args, args.query)
        if (index < warmup - 1 or measured > 0) and inter_run_sleep > 0:
            time.sleep(inter_run_sleep)

    # Measured runs.
    for index in range(measured):
        _progress(f"[measure] {mode} run {index + 1}/{measured} ...")
        if args.simulate:
            result = _run_smoke_once_simulated(args, args.query, mode, index)
        else:
            result = _run_smoke_once_real(args, args.query)
        result["run"] = index + 1
        results.append(result)
        # Show inline summary so a long Render run isn't a black box.
        _progress(
            f"[measure] {mode} run {index + 1}: "
            f"status={result['status']} "
            f"elapsed={result.get('elapsed_seconds')}s "
            f"final_status={result.get('final_status')}"
        )
        if index < measured - 1 and inter_run_sleep > 0:
            time.sleep(inter_run_sleep)

    elapsed_samples = [
        r["elapsed_seconds"] for r in results
        if r.get("elapsed_seconds") is not None
    ]
    pass_count = sum(1 for r in results if r.get("status") == "pass")
    aggregate = {
        "results": results,
        "mean_elapsed_seconds": (
            round(statistics.mean(elapsed_samples), 2)
            if elapsed_samples else None
        ),
        "min_elapsed_seconds": (
            round(min(elapsed_samples), 2) if elapsed_samples else None
        ),
        "max_elapsed_seconds": (
            round(max(elapsed_samples), 2) if elapsed_samples else None
        ),
        "pass_rate": round(pass_count / len(results), 4) if results else 0.0,
        "samples_with_elapsed": len(elapsed_samples),
        "total_runs": len(results),
    }
    return aggregate


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def compute_verdict(baseline: dict, cache_on: dict) -> dict:
    """Threshold-based verdict per the M13.3c brief.

    Returns ``{"verdict": <label>, "verdict_reason": <text>,
    "mean_speedup_percent": <float or None>,
    "pass_rate_maintained": <bool>}``.
    """
    base_mean = baseline.get("mean_elapsed_seconds") if baseline else None
    on_mean = cache_on.get("mean_elapsed_seconds") if cache_on else None
    base_rate = baseline.get("pass_rate", 0.0) if baseline else 0.0
    on_rate = cache_on.get("pass_rate", 0.0) if cache_on else 0.0

    if base_mean is None or on_mean is None or base_mean <= 0:
        return {
            "verdict": "insufficient_data",
            "verdict_reason": (
                "Need both baseline and cache-on measurements with "
                "valid elapsed times."
            ),
            "mean_speedup_percent": None,
            "pass_rate_maintained": (
                None if baseline is None or cache_on is None
                else on_rate >= base_rate
            ),
        }

    speedup_pct = round(
        (base_mean - on_mean) / base_mean * 100.0, 2,
    )
    pass_rate_maintained = on_rate >= base_rate

    if not pass_rate_maintained:
        verdict = "rollback_recommended"
        reason = (
            f"Pass rate dropped: baseline={base_rate} -> cache_on={on_rate}. "
            "Disable cache and investigate."
        )
    elif speedup_pct >= 20:
        verdict = "pass"
        reason = (
            f"Speedup {speedup_pct}% >= 20% AND pass rate maintained. "
            "Cache is delivering measurable improvement."
        )
    elif speedup_pct >= 5:
        verdict = "marginal"
        reason = (
            f"Speedup {speedup_pct}% between 5% and 20%. "
            "Verify cache hits in Render logs."
        )
    else:
        verdict = "investigate"
        reason = (
            f"Speedup {speedup_pct}% < 5%. Cache may not be active "
            "or domain allow-list may not match actual traffic."
        )

    return {
        "verdict": verdict,
        "verdict_reason": reason,
        "mean_speedup_percent": speedup_pct,
        "pass_rate_maintained": pass_rate_maintained,
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt_seconds(value):
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _render_results_table(results: list) -> str:
    lines = ["| Run | Status | Elapsed (s) | Final Status |"]
    lines.append("|-----|--------|-------------|--------------|")
    for r in results:
        lines.append(
            f"| {r.get('run')} | {r.get('status')} | "
            f"{_fmt_seconds(r.get('elapsed_seconds'))} | "
            f"{r.get('final_status') or 'n/a'} |"
        )
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    lines = ["# Cache Impact Measurement", ""]
    lines.append(f"**Measured at:** {report['measured_at']}")
    lines.append(f"**Base URL:** {report['base_url']}")
    lines.append(f"**Query:** {report['query']}")
    lines.append(
        f"**Runs:** {report['runs']} (warmup: {report['warmup']})"
    )
    lines.append(f"**Mode:** {report['mode']}")
    if report.get("simulated"):
        lines.append("**Simulated:** YES (no real Render calls)")
    lines.append("")
    lines.append("## Results")
    lines.append("")

    if report.get("baseline") is not None:
        b = report["baseline"]
        lines.append("### Baseline (cache OFF / current Render config)")
        lines.append("")
        lines.append(_render_results_table(b["results"]))
        lines.append("")
        lines.append(
            f"- **Mean elapsed:** {_fmt_seconds(b.get('mean_elapsed_seconds'))}s"
        )
        lines.append(
            f"- **Min elapsed:** {_fmt_seconds(b.get('min_elapsed_seconds'))}s"
        )
        lines.append(
            f"- **Max elapsed:** {_fmt_seconds(b.get('max_elapsed_seconds'))}s"
        )
        lines.append(
            f"- **Pass rate:** {b.get('pass_rate')} "
            f"({sum(1 for r in b['results'] if r.get('status') == 'pass')}/"
            f"{len(b['results'])})"
        )
        lines.append("")

    if report.get("cache_on") is not None:
        c = report["cache_on"]
        lines.append("### Cache-on (after operator enables flags)")
        lines.append("")
        lines.append(_render_results_table(c["results"]))
        lines.append("")
        lines.append(
            f"- **Mean elapsed:** {_fmt_seconds(c.get('mean_elapsed_seconds'))}s"
        )
        lines.append(
            f"- **Min elapsed:** {_fmt_seconds(c.get('min_elapsed_seconds'))}s"
        )
        lines.append(
            f"- **Max elapsed:** {_fmt_seconds(c.get('max_elapsed_seconds'))}s"
        )
        lines.append(
            f"- **Pass rate:** {c.get('pass_rate')} "
            f"({sum(1 for r in c['results'] if r.get('status') == 'pass')}/"
            f"{len(c['results'])})"
        )
        lines.append("")

    improvement = report.get("improvement", {})
    lines.append("## Improvement")
    lines.append("")
    if improvement.get("mean_speedup_percent") is not None:
        lines.append(
            f"- **Mean speedup:** {improvement['mean_speedup_percent']}% "
            f"({_fmt_seconds(report.get('baseline', {}).get('mean_elapsed_seconds'))}s "
            f"-> {_fmt_seconds(report.get('cache_on', {}).get('mean_elapsed_seconds'))}s)"
        )
    if improvement.get("pass_rate_maintained") is not None:
        lines.append(
            f"- **Pass rate maintained:** "
            f"{'YES' if improvement['pass_rate_maintained'] else 'NO'}"
        )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{improvement.get('verdict', 'n/a').upper()}** — "
                 f"{improvement.get('verdict_reason', 'n/a')}")
    lines.append("")
    lines.append(
        "[Safety] This script does NOT toggle Render env vars. "
        "See docs/CACHE_ACTIVATION_GUIDE.md for the activation procedure."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="measure_cache_impact",
        description=(
            "Run smoke_async_job multiple times and produce a "
            "before/after comparison report. Used to measure the "
            "M13.3b HTTP cache's actual impact on Render."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- measurement completed (any verdict)\n"
            "  1 -- smoke runs failed (infra issue)\n"
            "  2 -- CLI usage error\n\n"
            "Safety: this script does NOT toggle Render env vars. "
            "See docs/CACHE_ACTIVATION_GUIDE.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", required=True,
                        help="Target deployment URL")
    parser.add_argument("--query", required=True,
                        help="Verification query to send to /jobs/analyze")
    parser.add_argument("--runs", type=int, default=3,
                        help="Measured runs per configuration "
                             "(default 3, max 10)")
    parser.add_argument("--warmup", type=int, default=1,
                        help="Warmup runs not counted (default 1)")
    parser.add_argument("--max-news", type=int, default=1,
                        help="max_news passed to smoke_async_job "
                             "(default %(default)s)")
    parser.add_argument("--timeout-seconds", type=float, default=300.0,
                        help="Per-job timeout seconds (default %(default)s)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Smoke poll interval (default %(default)s)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--baseline-only", action="store_true",
        help="Only measure baseline (current Render config). "
             "Use before enabling cache flags.",
    )
    mode.add_argument(
        "--cache-on-only", action="store_true",
        help="Only measure cache-on (after operator enables flags). "
             "Use after enabling.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output path prefix (writes .json and .md). "
             "Default: reports/cache_measurement_<timestamp>",
    )
    parser.add_argument(
        "--no-default-reports", action="store_true",
        help="Skip default reports/ output (useful for CI smokes).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON to stdout in addition to (or instead of) files.",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Use deterministic synthetic smoke results instead of "
             "invoking scripts/smoke_async_job.py. For CI / dry-runs.",
    )
    return parser


def _resolve_mode(args) -> str:
    if args.baseline_only:
        return "baseline"
    if args.cache_on_only:
        return "cache_on"
    return "both"


def _write_outputs(report: dict, args) -> dict:
    """Write Markdown + JSON outputs per --output / --no-default-reports.
    Returns a dict of {markdown_path, json_path} (None when not written)."""
    md_text = render_markdown(report)
    json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)

    paths = {"markdown_path": None, "json_path": None}
    if args.no_default_reports and not args.output:
        return paths

    if args.output:
        prefix = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        reports_dir = _PROJECT_ROOT / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        prefix = reports_dir / f"cache_measurement_{ts}"

    md_path = prefix.with_suffix(".md")
    json_path = prefix.with_suffix(".json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_text, encoding="utf-8")
    json_path.write_text(json_text, encoding="utf-8")
    paths["markdown_path"] = str(md_path)
    paths["json_path"] = str(json_path)
    print(f"[measure] wrote {md_path}", file=sys.stderr, flush=True)
    print(f"[measure] wrote {json_path}", file=sys.stderr, flush=True)
    return paths


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # Cap runs at 10 per CLI contract.
    if args.runs is not None and args.runs > 10:
        print("[measure] --runs capped at 10", file=sys.stderr, flush=True)
        args.runs = 10

    mode = _resolve_mode(args)
    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    baseline = None
    cache_on = None

    if mode in ("baseline", "both"):
        baseline = _execute_runs(args, "baseline")
    if mode in ("cache_on", "both"):
        cache_on = _execute_runs(args, "cache_on")

    improvement = compute_verdict(baseline, cache_on)

    report = {
        "schema_version": "1.0",
        "measured_at": measured_at,
        "base_url": args.base_url,
        "query": args.query,
        "runs": int(args.runs or 3),
        "warmup": int(args.warmup or 0),
        "mode": mode,
        "simulated": bool(args.simulate),
        "baseline": baseline,
        "cache_on": cache_on,
        "improvement": improvement,
    }

    paths = _write_outputs(report, args)
    report["output_paths"] = paths

    if args.json:
        # Pure JSON mode: stdout is exclusively the report payload so
        # callers (tests, jq pipelines) can parse it directly.
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        # Human mode: short summary so the operator sees the verdict
        # without opening the report file.
        print("")
        print("=== Verdict ===")
        print(
            f"{improvement.get('verdict', 'n/a').upper()} -- "
            f"{improvement.get('verdict_reason', 'n/a')}"
        )
        if improvement.get("mean_speedup_percent") is not None:
            print(
                f"Mean speedup: {improvement['mean_speedup_percent']}%"
            )

    # Determine exit code. Per the brief: 0 on completion regardless
    # of verdict, 1 only when the smoke infrastructure itself failed.
    any_infra_failure = False
    for section in (baseline, cache_on):
        if section is None:
            continue
        for r in section.get("results", []):
            if r.get("status") == "fail":
                any_infra_failure = True
                break
        if any_infra_failure:
            break
    return 1 if any_infra_failure else 0


if __name__ == "__main__":
    sys.exit(main())
