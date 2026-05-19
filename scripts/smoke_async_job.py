"""End-to-end smoke test for the async-job verification flow.

Exercises a live deployment (local uvicorn or Render) by:
  1. GET /health
  2. POST /jobs/analyze with a query
  3. Polling GET /jobs/{job_id} until the job reaches a terminal state
  4. GET /jobs/{job_id}/result on completion

This script is intentionally NOT wired into the default CI run because it
triggers the real verification pipeline on the target server (news fetch,
external sites). Invoke it manually, or via the workflow_dispatch input
``smoke_base_url`` on the CI workflow.

Uses only the Python stdlib (urllib + json) so it adds no new dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional


TERMINAL_STATUSES = {"completed", "failed", "timeout"}


class SmokeError(RuntimeError):
    """Raised when the smoke test cannot continue (HTTP error, bad payload)."""


def _http_request(method: str, url: str, payload: Optional[dict] = None, timeout: float = 30.0) -> dict:
    body_bytes: Optional[bytes] = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url=url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as error:
                raise SmokeError(f"{method} {url}: response was not JSON: {error}") from error
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise SmokeError(
            f"{method} {url}: HTTP {error.code} {error.reason}"
            + (f" — {detail[:400]}" if detail else "")
        ) from error
    except urllib.error.URLError as error:
        raise SmokeError(f"{method} {url}: network error: {error.reason}") from error


def check_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/health"
    print(f"[smoke] GET {url}")
    data = _http_request("GET", url)
    status = data.get("status")
    if status != "healthy":
        raise SmokeError(f"/health did not report healthy: {data}")
    return data


def create_job(base_url: str, query: str, max_news: int) -> dict:
    url = f"{base_url.rstrip('/')}/jobs/analyze"
    payload = {"query": query, "max_news": max_news}
    print(f"[smoke] POST {url} payload={payload}")
    data = _http_request("POST", url, payload=payload)
    if not data.get("job_id"):
        raise SmokeError(f"/jobs/analyze did not return a job_id: {data}")
    return data


def poll_job(
    base_url: str,
    job_id: str,
    *,
    poll_interval: float,
    timeout_seconds: float,
) -> tuple[dict, list[str]]:
    url = f"{base_url.rstrip('/')}/jobs/{job_id}"
    stages_seen: list[str] = []
    started = time.monotonic()
    last_stage: Optional[str] = None
    last_percent: Optional[int] = None
    while True:
        data = _http_request("GET", url)
        status = (data.get("job_status") or "").lower()
        stage = data.get("current_stage")
        percent = data.get("progress_percent")
        if stage and stage != last_stage:
            stages_seen.append(stage)
            last_stage = stage
        if (stage, percent) != (last_stage, last_percent):
            last_percent = percent
            print(f"[smoke] poll job_status={status} stage={stage} percent={percent}")
        if status in TERMINAL_STATUSES:
            return data, stages_seen
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise SmokeError(
                f"polling timed out after {elapsed:.1f}s "
                f"(last status={status}, stage={stage})"
            )
        time.sleep(poll_interval)


def fetch_result(base_url: str, job_id: str) -> dict:
    url = f"{base_url.rstrip('/')}/jobs/{job_id}/result"
    print(f"[smoke] GET {url}")
    return _http_request("GET", url)


def _summarize_result(payload: dict) -> str:
    """Concise human-readable summary of the /jobs/{id}/result response."""
    status = payload.get("status")
    job_status = payload.get("job_status")
    source = payload.get("result_source")
    inner = payload.get("result") or {}
    results = inner.get("results") if isinstance(inner, dict) else None
    count = len(results) if isinstance(results, list) else "n/a"
    stored = payload.get("stored_result")
    has_stored = bool(stored)
    return (
        f"status={status} job_status={job_status} result_source={source} "
        f"results_count={count} has_stored_result={has_stored}"
    )


def _assert_result_usable(payload: dict) -> None:
    """Confirm the result endpoint returned something the UI could render.

    Either the in-memory cache hands us a structured ``result.results`` array,
    or the server clearly flags ``result_source`` (e.g. ``stored_result``) so a
    client knows to fall back. A bare ``status=ok`` with neither is treated as
    a failure.
    """
    status = payload.get("status")
    if status == "result_unavailable":
        raise SmokeError(
            f"job completed but result is unavailable: {payload.get('error_message')}"
        )
    if status != "ok":
        raise SmokeError(f"unexpected result status: {payload}")
    inner = payload.get("result")
    source = payload.get("result_source")
    stored = payload.get("stored_result")
    if isinstance(inner, dict) and isinstance(inner.get("results"), list):
        return
    if source and (stored or inner):
        return
    raise SmokeError(
        "result endpoint returned status=ok but no usable payload "
        f"(result_source={source!r}, has_inner={bool(inner)}, has_stored={bool(stored)})"
    )


def run_smoke(
    *,
    base_url: str,
    query: str,
    max_news: int,
    poll_interval: float,
    timeout_seconds: float,
) -> int:
    started = time.monotonic()
    base_url = base_url.rstrip("/")
    print(f"[smoke] base_url={base_url}")

    try:
        check_health(base_url)
        job = create_job(base_url, query=query, max_news=max_news)
        job_id = job["job_id"]
        print(f"[smoke] job_id={job_id} status={job.get('job_status')}")

        final, stages = poll_job(
            base_url,
            job_id,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
        )
        final_status = (final.get("job_status") or "").lower()
        if final_status != "completed":
            elapsed = time.monotonic() - started
            print(
                f"[smoke] FAILED — job ended in non-completed state\n"
                f"        base_url        = {base_url}\n"
                f"        job_id          = {job_id}\n"
                f"        stages_observed = {stages}\n"
                f"        final_status    = {final_status}\n"
                f"        error_message   = {final.get('error_message')}\n"
                f"        elapsed         = {elapsed:.1f}s"
            )
            return 2

        result_payload = fetch_result(base_url, job_id)
        _assert_result_usable(result_payload)

        elapsed = time.monotonic() - started
        print(
            "[smoke] PASSED\n"
            f"        base_url        = {base_url}\n"
            f"        job_id          = {job_id}\n"
            f"        stages_observed = {stages}\n"
            f"        final_status    = {final_status}\n"
            f"        result_summary  = {_summarize_result(result_payload)}\n"
            f"        elapsed         = {elapsed:.1f}s"
        )
        return 0
    except SmokeError as error:
        elapsed = time.monotonic() - started
        print(f"[smoke] FAILED after {elapsed:.1f}s: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[smoke] aborted by user", file=sys.stderr)
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke-test the async-job verification flow against a live deployment.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="Base URL of the deployment (default: %(default)s)")
    parser.add_argument("--query", default="전세사기",
                        help="Verification query to submit (default: %(default)s)")
    parser.add_argument("--max-news", type=int, default=1,
                        help="max_news parameter passed to /jobs/analyze (default: %(default)s)")
    parser.add_argument("--timeout-seconds", type=float, default=300.0,
                        help="Maximum seconds to wait for the job to reach a terminal state.")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Seconds between job-status polls.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_smoke(
        base_url=args.base_url,
        query=args.query,
        max_news=args.max_news,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
