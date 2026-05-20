"""Phase 2 M7.2: semantic debug-canary smoke script.

Exercises the live ``/jobs/analyze`` + ``/jobs/{id}/result`` flow
(same pattern as ``scripts/smoke_async_job.py``) and then runs
``semantic_canary_metrics.summarize_semantic_canary`` over the result
payload to surface a canary-monitoring scorecard.

This script:
    * Talks to a live HTTP endpoint (local uvicorn or Render).
    * Does **not** call OpenAI itself. If the target app has semantic
      matching enabled, the app may call OpenAI on the server side —
      that is the canary the operator is measuring.
    * Never prints the API key. Reads no env vars besides what
      ``urllib`` defaults provide.
    * Writes reports under ``reports/`` (gitignored).
    * Does not modify Render env. Does not change verdict logic.

Exit codes:
    0 — success
    1 — script / server / result failure
    2 — semantic unavailable when ``--fail-on-semantic-unavailable`` set
    3 — health is warn or fail when ``--fail-on-health-warn`` set
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import semantic_canary_metrics  # noqa: E402


DEFAULT_TIMEOUT = 30
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_QUERY = "전세사기"


class CanarySmokeError(RuntimeError):
    """Raised on HTTP / payload-shape failures."""


# ---------------------------------------------------------------------------
# HTTP helpers — small, stdlib-only, mirrors smoke_async_job.py's style.
# ---------------------------------------------------------------------------


def _http_request(method: str, url: str, *, payload: Optional[dict] = None,
                  timeout: int = DEFAULT_TIMEOUT) -> dict:
    headers = {"Accept": "application/json"}
    body: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as error:
        raise CanarySmokeError(
            f"HTTP {error.code} {method} {url}: {error.reason}"
        ) from error
    except urllib_error.URLError as error:
        raise CanarySmokeError(f"URL error {method} {url}: {error.reason}") from error
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as error:
        raise CanarySmokeError(f"non-JSON response from {url}: {error}") from error


def _check_health(base_url: str) -> None:
    url = f"{base_url.rstrip('/')}/health"
    print(f"[smoke-canary] GET {url}")
    data = _http_request("GET", url)
    if (data.get("status") or "").lower() not in {"ok", "healthy"}:
        raise CanarySmokeError(f"/health returned unhealthy: {data}")


def _create_job(base_url: str, *, query: str, max_news: int) -> dict:
    url = f"{base_url.rstrip('/')}/jobs/analyze"
    print(f"[smoke-canary] POST {url} query={query!r} max_news={max_news}")
    return _http_request("POST", url, payload={"query": query, "max_news": max_news})


def _poll_job(base_url: str, job_id: str, *, poll_interval: float,
              timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    url = f"{base_url.rstrip('/')}/jobs/{job_id}"
    while True:
        data = _http_request("GET", url)
        status = (data.get("job_status") or "").lower()
        print(f"[smoke-canary]   poll job_status={status}")
        if status in {"completed", "failed", "timeout"}:
            return data
        if time.monotonic() > deadline:
            raise CanarySmokeError(
                f"job {job_id} did not reach terminal state within {timeout_seconds}s "
                f"(last status={status})"
            )
        time.sleep(poll_interval)


def _fetch_result(base_url: str, job_id: str) -> dict:
    url = f"{base_url.rstrip('/')}/jobs/{job_id}/result"
    print(f"[smoke-canary] GET {url}")
    return _http_request("GET", url)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the async-job flow against a live app and extract "
            "semantic debug-canary metrics. Never calls OpenAI directly; "
            "if the target app has semantic matching enabled, the app "
            "may call OpenAI on the server side."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--max-news", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument(
        "--expect-semantic-enabled", action="store_true",
        help="Fail (with --fail-on-semantic-unavailable) if no summary reports enabled.",
    )
    parser.add_argument(
        "--expect-provider", default="",
        help="Optional provider name; warns if observed providers do not include it.",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None,
        help="Write the canary summary JSON to this path (gitignored under reports/).",
    )
    parser.add_argument(
        "--markdown-out", type=Path, default=None,
        help="Write a Markdown canary report (gitignored under reports/).",
    )
    parser.add_argument(
        "--fail-on-health-warn", action="store_true",
        help="Exit code 3 when canary health is warn or fail.",
    )
    parser.add_argument(
        "--fail-on-semantic-unavailable", action="store_true",
        help="Exit code 2 when semantic is configured but unavailable.",
    )
    parser.add_argument(
        "--no-live-note", action="store_true",
        help="Suppress the 'this may trigger live OpenAI calls server-side' note.",
    )
    return parser


# ---------------------------------------------------------------------------
# Reporting + classification
# ---------------------------------------------------------------------------


def _print_summary(*, base_url: str, job_id: str, final_status: str,
                   summary: dict, expect_provider: str) -> None:
    print()
    print("[smoke-canary] === canary scorecard ===")
    print(f"  base_url: {base_url}")
    print(f"  job_id: {job_id}")
    print(f"  final_status: {final_status}")
    print(f"  {semantic_canary_metrics.format_summary_line(summary)}")
    pc = summary.get("provider_counts") or {}
    mc = summary.get("model_counts") or {}
    print(f"  provider_counts: {pc}")
    print(f"  model_counts: {mc}")
    print(f"  best_support_distribution: {summary.get('best_support_distribution') or {}}")
    print(f"  raw_support_distribution: {summary.get('raw_support_distribution') or {}}")
    print(f"  risk_flag_counts: {summary.get('risk_flag_counts') or {}}")
    if expect_provider:
        observed = set(pc.keys()) - {"(none)"}
        if expect_provider not in observed:
            print(
                f"  [warn] expected provider {expect_provider!r} not observed in "
                f"{sorted(observed) or 'no providers'}",
            )
    health = summary.get("health") or "pass"
    print(f"  health: {health}")
    classification = semantic_canary_metrics.classify_canary_health(summary)
    for reason in classification.get("reasons") or []:
        print(f"    reason: {reason}")
    print(
        "[smoke-canary] reminder: semantic match strength is metadata only; "
        "rule-based verification and official body matching remain authoritative."
    )


def _write_outputs(*, summary: dict, base_url: str, json_out: Optional[Path],
                   markdown_out: Optional[Path]) -> None:
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps({"base_url": base_url, "summary": summary},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[smoke-canary] JSON written to {json_out}")
    if markdown_out:
        markdown_out.parent.mkdir(parents=True, exist_ok=True)
        markdown_out.write_text(
            semantic_canary_metrics.format_markdown_report(summary, base_url=base_url),
            encoding="utf-8",
        )
        print(f"[smoke-canary] Markdown written to {markdown_out}")


def run_smoke(args: argparse.Namespace) -> int:
    started = time.monotonic()
    base_url = args.base_url.rstrip("/")
    print(f"[smoke-canary] base_url={base_url}")
    if not args.no_live_note:
        print(
            "[smoke-canary] note: this script does not call OpenAI itself, but if "
            "the target app has SEMANTIC_MATCHING_ENABLED=true and "
            "EMBEDDING_PROVIDER=openai, the server may issue live OpenAI requests."
        )

    try:
        _check_health(base_url)
        job = _create_job(base_url, query=args.query, max_news=args.max_news)
        job_id = job.get("job_id")
        if not job_id:
            raise CanarySmokeError(f"/jobs/analyze did not return a job_id: {job}")
        print(f"[smoke-canary] job_id={job_id} status={job.get('job_status')}")

        final = _poll_job(
            base_url, job_id,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
        )
        final_status = (final.get("job_status") or "").lower()
        if final_status != "completed":
            raise CanarySmokeError(
                f"job {job_id} did not complete (final status={final_status})"
            )
        result_payload = _fetch_result(base_url, job_id)
    except CanarySmokeError as error:
        print(f"[smoke-canary] FAILED: {error}", file=sys.stderr)
        return 1

    summary = semantic_canary_metrics.summarize_semantic_canary(result_payload)
    _print_summary(
        base_url=base_url,
        job_id=job_id,
        final_status=final_status,
        summary=summary,
        expect_provider=args.expect_provider,
    )

    _write_outputs(
        summary=summary,
        base_url=base_url,
        json_out=args.json_out,
        markdown_out=args.markdown_out,
    )

    elapsed = time.monotonic() - started
    print(f"[smoke-canary] total elapsed {elapsed:.2f}s")

    # Exit-code policy.
    if args.fail_on_semantic_unavailable and args.expect_semantic_enabled:
        if summary.get("semantic_enabled_count", 0) == 0 or summary.get("semantic_available_count", 0) == 0:
            print(
                f"[smoke-canary] FAIL: semantic expected enabled but enabled_count="
                f"{summary.get('semantic_enabled_count')} available_count="
                f"{summary.get('semantic_available_count')}",
                file=sys.stderr,
            )
            return 2

    if args.fail_on_health_warn:
        health = summary.get("health") or "pass"
        if health in {"warn", "fail"}:
            print(
                f"[smoke-canary] FAIL: canary health={health}",
                file=sys.stderr,
            )
            return 3
    return 0


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_smoke(args)
    except KeyboardInterrupt:
        print("[smoke-canary] aborted by user", file=sys.stderr)
        return 130
    except Exception as error:  # defensive
        print(f"[smoke-canary] FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
