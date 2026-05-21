"""Phase 2 M8.8: review API public-exposure smoke.

No-token, no-secret smoke that verifies the ``/review/*`` endpoints
are not publicly accessible on a deployed instance. The script never
asks for, accepts, or sends a real ``REVIEW_API_TOKEN`` — every
request goes out **without** an ``X-Review-Token`` header, and the
output is asserted to carry no token-shaped literal.

Hard contract:
    * No OpenAI calls.
    * No Render env modification.
    * No secret print / log / persist.
    * No real review token required from the operator.
    * Stdlib only (``urllib.request`` — no ``requests`` / ``httpx``).
    * Reads the public M8.0+ review surface only.

Classification rules (every endpoint independently):

    public_access   any 2xx without token  → ALWAYS FAIL (every mode)
    disabled        HTTP 503 + "disabled"  → safe gate (review API off)
    token_required  HTTP 403               → safe gate (review API on, token gate works)
    unexpected      everything else        → fail unless explicitly justified

Per-mode expectations:

    --expect-disabled                 every endpoint must be ``disabled``.
                                      ``token_required`` is reported as an
                                      *expectation mismatch* (the operator
                                      expected the API to be off), still safe
                                      against public exposure, but the run
                                      fails to surface the mismatch.
    --expect-token-required           every endpoint must be ``token_required``.
                                      ``disabled`` is reported as an
                                      *expectation mismatch* (the operator
                                      expected the API to be on + gated).
    --allow-disabled-or-token-required  either ``disabled`` or ``token_required``
                                        is safe; mixed responses are also safe.

In every mode, ``public_access`` is a hard fail; ``unexpected`` statuses
are reported and fail the run unless the operator inspects them.

Exit codes:
    0 — every endpoint matched the expected safe classification
    1 — public access detected OR expectation mismatch OR unexpected status
    2 — bad CLI usage

Usage:

    python scripts/smoke_review_api_exposure.py \\
        --base-url https://policy-ai-q5ax.onrender.com --expect-disabled

    python scripts/smoke_review_api_exposure.py \\
        --base-url http://127.0.0.1:8000 --expect-token-required

    python scripts/smoke_review_api_exposure.py \\
        --base-url https://policy-ai-q5ax.onrender.com \\
        --allow-disabled-or-token-required
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


DEFAULT_TIMEOUT_SECONDS = 30.0

EXPECT_DISABLED = "expect-disabled"
EXPECT_TOKEN_REQUIRED = "expect-token-required"
ALLOW_EITHER = "allow-disabled-or-token-required"

# Classification tags.
CLASS_PUBLIC = "public_access"
CLASS_DISABLED = "disabled"
CLASS_TOKEN_REQUIRED = "token_required"
CLASS_UNEXPECTED = "unexpected"


# Synthetic harmless POST bodies. No secrets, no real user data, no
# semantic labels, no token. The server should refuse these long before
# inspecting their payload — but even if a future regression starts
# accepting them without a token, the body itself records that the
# request was a smoke probe.
SYNTHETIC_FROM_RESULT_BODY: Dict[str, object] = {
    "result_id": "smoke-public-exposure",
    "job_id": None,
    "item_index": 0,
    "query": "policy_ai public exposure smoke",
    "result_payload": {
        "status": "ok",
        "query": "policy_ai public exposure smoke",
        "result": {"results": []},
    },
}

SYNTHETIC_DECISION_BODY: Dict[str, object] = {
    "decision": "comment",
    "comment": "public exposure smoke - no token",
}


# Endpoint catalogue. Each tuple is (method, path, optional body dict).
ENDPOINTS: Tuple[Tuple[str, str, Optional[Dict[str, object]]], ...] = (
    ("GET", "/review/tasks", None),
    ("GET", "/review/tasks/nonexistent-smoke-task-id", None),
    ("GET", "/review/tasks/nonexistent-smoke-task-id/decisions", None),
    ("POST", "/review/tasks/from-result", SYNTHETIC_FROM_RESULT_BODY),
    ("POST", "/review/tasks/nonexistent-smoke-task-id/decision", SYNTHETIC_DECISION_BODY),
)


# Heuristic: "disabled" body marker. The M8.0 review_auth.py disabled
# detail message starts with "Review API is disabled.". We accept any
# case-insensitive substring containing the word "disabled" — the goal
# is to distinguish 503-from-the-gate from 503-from-something-else.
_DISABLED_BODY_RE = re.compile(r"disabled", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class EndpointResult:
    method: str
    path: str
    status_code: int
    classification: str
    # Trimmed body so the JSON summary doesn't grow unbounded, and so
    # nothing token-shaped can sneak through if the server ever echoed
    # a header back.
    body_snippet: str = ""
    error: Optional[str] = None


@dataclass
class SmokeResult:
    passed: bool
    base_url: str
    expectation_mode: str
    endpoints_checked: int
    public_access_detected: bool
    disabled_count: int
    token_required_count: int
    unexpected_count: int
    expectation_mismatch_count: int
    results: List[EndpointResult] = field(default_factory=list)
    recommendation: str = ""
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _normalize_base_url(raw: str) -> str:
    """Strip trailing slashes; do not auto-add a scheme — the operator
    must be explicit about http/https for safety."""
    if raw is None:
        return ""
    base = str(raw).strip()
    while base.endswith("/"):
        base = base[:-1]
    return base


def _make_request(
    method: str, url: str, body: Optional[Dict[str, object]],
    *, timeout_seconds: float,
) -> Tuple[int, str, Optional[str]]:
    """Return (status_code, body_text, error_string).

    Never raises. Connection failures / timeouts become ``(0, "", err)``
    so the classifier can report them as ``unexpected`` rather than
    crashing the smoke. **No** ``X-Review-Token`` header is ever sent.
    """
    headers = {
        # Identify the smoke source so server logs can tell where the
        # probes came from. No secret material.
        "User-Agent": "policy_ai-public-exposure-smoke/M8.8",
        "Accept": "application/json",
    }
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            payload = resp.read() or b""
            return status, payload.decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as http_err:
        # 4xx/5xx land here — read the body if we can so we can detect
        # "disabled" in the error detail.
        status = int(getattr(http_err, "code", 0) or 0)
        try:
            payload = http_err.read() or b""
        except Exception:
            payload = b""
        return status, payload.decode("utf-8", errors="replace"), None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as net_err:
        return 0, "", f"{type(net_err).__name__}: {net_err}"
    except Exception as err:  # pragma: no cover - defensive
        return 0, "", f"{type(err).__name__}: {err}"


# Type alias for the injectable fetcher tests use.
FetchFn = Callable[[str, str, Optional[Dict[str, object]]], Tuple[int, str, Optional[str]]]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_response(status_code: int, body_text: str) -> str:
    """Map an HTTP status + body to one of the four classifications."""
    if 200 <= status_code < 300:
        return CLASS_PUBLIC
    if status_code == 503 and _DISABLED_BODY_RE.search(body_text or ""):
        return CLASS_DISABLED
    if status_code == 403:
        return CLASS_TOKEN_REQUIRED
    return CLASS_UNEXPECTED


def _trim_body_for_report(body_text: str, *, max_len: int = 240) -> str:
    """Shrink the body to ``max_len`` chars, replace anything that looks
    like a hex/base64 token literal with ``<redacted>``. Defensive."""
    if not body_text:
        return ""
    text = body_text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    # Redact long hex / base64 runs as a belt-and-braces protection.
    text = re.sub(r"[0-9a-fA-F]{32,}", "<redacted>", text)
    text = re.sub(r"[A-Za-z0-9+/=]{40,}", "<redacted>", text)
    return text


def _classify_against_expectation(
    classification: str, expectation_mode: str,
) -> Tuple[str, Optional[str]]:
    """Return (per_endpoint_status, mismatch_reason).

    ``per_endpoint_status`` is one of ``pass`` / ``fail`` / ``mismatch``;
    ``fail`` covers public access + unexpected, ``mismatch`` covers safe
    gates that don't match the operator's expectation mode (still safe
    from public exposure but the operator should know).
    """
    if classification == CLASS_PUBLIC:
        return ("fail", "2xx without token — endpoint is publicly accessible")
    if classification == CLASS_UNEXPECTED:
        return ("fail", "unexpected status code (not 2xx/403/503-disabled)")

    if expectation_mode == ALLOW_EITHER:
        return ("pass", None)

    if expectation_mode == EXPECT_DISABLED:
        if classification == CLASS_DISABLED:
            return ("pass", None)
        # 403 / token_required → expectation mismatch, but still safe.
        return (
            "mismatch",
            "expected disabled (503) but the endpoint is enabled + token-gated (403); "
            "current Render policy is disabled-by-default",
        )

    if expectation_mode == EXPECT_TOKEN_REQUIRED:
        if classification == CLASS_TOKEN_REQUIRED:
            return ("pass", None)
        return (
            "mismatch",
            "expected token-gated (403) but the endpoint is disabled (503); the "
            "review API was not enabled on this deployment",
        )

    return ("fail", f"unknown expectation_mode: {expectation_mode}")


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def _build_recommendation(result: SmokeResult) -> str:
    if result.public_access_detected:
        return (
            "FAIL: at least one /review/* endpoint returned 2xx WITHOUT a token. "
            "This is a public-exposure incident. Set REVIEW_API_ENABLED=false in the "
            "Render dashboard immediately (or remove the deployment from public DNS) "
            "and investigate before anything else."
        )
    if result.unexpected_count:
        return (
            "FAIL: at least one /review/* endpoint returned an unexpected status "
            "(not 2xx, not 403, not 503-disabled). Inspect the per-endpoint "
            "results — this may indicate a misconfigured proxy, an unrelated "
            "outage, or a regression in the review_auth gate."
        )
    if result.expectation_mismatch_count and result.expectation_mode != ALLOW_EITHER:
        if result.expectation_mode == EXPECT_DISABLED:
            return (
                "MISMATCH: every endpoint is token-gated (403) but the operator "
                "expected the review API to be disabled (503). Current Render "
                "policy is disabled-by-default — confirm whether the review API "
                "was intentionally enabled on this deployment. No public exposure."
            )
        return (
            "MISMATCH: at least one endpoint is disabled (503) but the operator "
            "expected the review API to be token-gated (403). The deployment is "
            "still safe from public exposure, but the review surface is not "
            "actually reachable for review work."
        )
    if result.expectation_mode == EXPECT_DISABLED:
        return (
            "PASS: every /review/* endpoint returns 503 disabled — matches "
            "current Render policy (review API off by default)."
        )
    if result.expectation_mode == EXPECT_TOKEN_REQUIRED:
        return (
            "PASS: every /review/* endpoint returns 403 without a token — the "
            "review-auth gate is enforcing the X-Review-Token header."
        )
    return (
        "PASS: every /review/* endpoint refused the no-token probe with a safe "
        "gate (disabled or token-required); no public exposure detected."
    )


def run_exposure_smoke(
    base_url: str,
    expectation_mode: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    fetch_fn: Optional[FetchFn] = None,
    endpoints: Sequence[Tuple[str, str, Optional[Dict[str, object]]]] = ENDPOINTS,
) -> SmokeResult:
    """Pure function — every external dependency is injectable via
    ``fetch_fn``. Tests pass a stub that returns canned ``(status, body,
    error)`` triples per (method, path, body)."""
    normalized = _normalize_base_url(base_url)
    fetch = fetch_fn or (
        lambda method, url, body: _make_request(
            method, url, body, timeout_seconds=timeout_seconds,
        )
    )

    results: List[EndpointResult] = []
    counts = {
        CLASS_PUBLIC: 0, CLASS_DISABLED: 0,
        CLASS_TOKEN_REQUIRED: 0, CLASS_UNEXPECTED: 0,
    }
    mismatch_count = 0
    overall_pass = True
    errors: List[str] = []
    warnings: List[str] = []

    for method, path, body in endpoints:
        url = normalized + path
        status, body_text, err = fetch(method, url, body)
        classification = classify_response(status, body_text)
        counts[classification] = counts.get(classification, 0) + 1

        per_status, mismatch_reason = _classify_against_expectation(
            classification, expectation_mode,
        )
        if per_status == "fail":
            overall_pass = False
            errors.append(f"{method} {path}: {mismatch_reason or 'fail'}")
        elif per_status == "mismatch":
            mismatch_count += 1
            warnings.append(f"{method} {path}: {mismatch_reason or 'mismatch'}")
            # In strict modes (expect-disabled / expect-token-required) a
            # mismatch fails the run; in allow-either it would never reach
            # this branch.
            overall_pass = False

        # Network-level error → unexpected + recorded under errors.
        if err and classification == CLASS_UNEXPECTED:
            errors.append(f"{method} {path}: {err}")

        results.append(EndpointResult(
            method=method,
            path=path,
            status_code=status,
            classification=classification,
            body_snippet=_trim_body_for_report(body_text),
            error=err,
        ))

    smoke = SmokeResult(
        passed=overall_pass,
        base_url=normalized,
        expectation_mode=expectation_mode,
        endpoints_checked=len(results),
        public_access_detected=counts[CLASS_PUBLIC] > 0,
        disabled_count=counts[CLASS_DISABLED],
        token_required_count=counts[CLASS_TOKEN_REQUIRED],
        unexpected_count=counts[CLASS_UNEXPECTED],
        expectation_mismatch_count=mismatch_count,
        results=results,
        warnings=warnings,
        errors=errors,
    )
    smoke.recommendation = _build_recommendation(smoke)
    return smoke


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def smoke_to_dict(smoke: SmokeResult) -> Dict[str, object]:
    return {
        "passed": smoke.passed,
        "base_url": smoke.base_url,
        "expectation_mode": smoke.expectation_mode,
        "endpoints_checked": smoke.endpoints_checked,
        "public_access_detected": smoke.public_access_detected,
        "disabled_count": smoke.disabled_count,
        "token_required_count": smoke.token_required_count,
        "unexpected_count": smoke.unexpected_count,
        "expectation_mismatch_count": smoke.expectation_mismatch_count,
        "results": [
            {
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "classification": r.classification,
                "body_snippet": r.body_snippet,
                "error": r.error,
            }
            for r in smoke.results
        ],
        "warnings": list(smoke.warnings),
        "errors": list(smoke.errors),
        "recommendation": smoke.recommendation,
    }


def _print_human_summary(smoke: SmokeResult) -> None:
    print(f"[exposure] base_url={smoke.base_url}")
    print(f"[exposure] expectation_mode={smoke.expectation_mode}")
    print(f"[exposure] endpoints_checked={smoke.endpoints_checked}")
    print(
        "[exposure] public_access_detected="
        f"{smoke.public_access_detected} "
        f"disabled={smoke.disabled_count} "
        f"token_required={smoke.token_required_count} "
        f"unexpected={smoke.unexpected_count} "
        f"mismatch={smoke.expectation_mismatch_count}",
    )
    for r in smoke.results:
        print(
            f"[exposure]   {r.method} {r.path} -> {r.status_code} "
            f"[{r.classification}]"
        )
    for w in smoke.warnings:
        print(f"[exposure] warn: {w}")
    for e in smoke.errors:
        print(f"[exposure] error: {e}")
    print(f"[exposure] recommendation: {smoke.recommendation}")
    print(f"[exposure] passed={smoke.passed}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "No-token smoke that verifies /review/* endpoints are not "
            "publicly accessible on a deployed instance. Never accepts "
            "or sends a real REVIEW_API_TOKEN; never calls OpenAI; "
            "never modifies Render env."
        ),
    )
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL to probe (e.g. https://policy-ai-q5ax.onrender.com).",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--expect-disabled", dest="expectation_mode",
        action="store_const", const=EXPECT_DISABLED,
        help="Require every endpoint to return 503 disabled.",
    )
    mode_group.add_argument(
        "--expect-token-required", dest="expectation_mode",
        action="store_const", const=EXPECT_TOKEN_REQUIRED,
        help="Require every endpoint to return 403 (token gate enforced).",
    )
    mode_group.add_argument(
        "--allow-disabled-or-token-required", dest="expectation_mode",
        action="store_const", const=ALLOW_EITHER,
        help="Accept either 503 disabled or 403 token-required.",
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout (default {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Suppress the human summary; only print the JSON payload.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.timeout_seconds <= 0:
        print(
            "[exposure] --timeout-seconds must be > 0.", file=sys.stderr,
        )
        return 2

    smoke = run_exposure_smoke(
        args.base_url,
        args.expectation_mode,
        timeout_seconds=args.timeout_seconds,
    )

    payload = smoke_to_dict(smoke)
    if not args.json:
        _print_human_summary(smoke)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0 if smoke.passed else 1


if __name__ == "__main__":
    sys.exit(main())
