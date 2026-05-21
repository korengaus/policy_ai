"""Phase 2 M9.5: controlled review API token-gate smoke.

Read-only smoke that verifies an *intentionally enabled* review API
is properly token-gated and reachable by an operator who holds the
correct token. This is **not** the M8.8 public-exposure smoke — that
one verifies the API is *disabled* (the current Render policy). This
M9.5 smoke is for the future state where the operator manually sets
``REVIEW_API_ENABLED=true`` + ``REVIEW_API_TOKEN`` in the Render
dashboard and wants a low-blast-radius verification that the gate
behaves correctly.

Hard contract:
    * Token value is read from an environment variable; **never** from
      a CLI flag. The variable name is configurable via
      ``--token-env`` but defaults to ``REVIEW_API_SMOKE_TOKEN`` so the
      operator can keep the live Render review token in a separate
      env from this smoke variable.
    * Token value is never printed to stdout / stderr / JSON / any
      report. The JSON payload carries a deliberate
      ``token_value_printed: false`` flag so consumers can pin it.
    * Stdlib only (``urllib.request``). No ``requests`` / ``httpx``.
    * GET-only. No POST, no PUT, no DELETE — the smoke never creates,
      mutates, or deletes any review task / decision.
    * No OpenAI / Anthropic / Render env modification.

Pass criteria:
    * ``GET /review/tasks`` with no token → 403
    * ``GET /review/tasks`` with wrong token → 403
    * ``GET /review/tasks`` with correct token → 200
    * ``GET /review/tasks/<nonexistent>`` with correct token → 404
    * ``GET /review/tasks/<nonexistent>/decisions`` with correct token → 404
    * ``GET /review/tasks/<nonexistent>/audit-packet`` with correct token → 404
    * no 2xx without a valid token (public_access_detected stays False)
    * no 503 from the review-auth gate (would mean the API isn't
      actually enabled on this deploy)

Usage:

    # 1. Operator sets the smoke token locally in PowerShell (matching
    #    the value Render's REVIEW_API_TOKEN is configured to expect):
    $env:REVIEW_API_SMOKE_TOKEN = "<paste-locally-only>"

    # 2. Run the smoke:
    python scripts/smoke_review_api_token_gate.py \\
        --base-url https://policy-ai-q5ax.onrender.com

    # 3. Clear the env var afterward:
    Remove-Item Env:\\REVIEW_API_SMOKE_TOKEN

Exit codes:
    0 — every endpoint matched the expected token-gate classification
    1 — public-access incident OR token rejected with valid token OR
        disabled when this smoke expected enabled OR unexpected status
    2 — bad CLI usage (missing/empty token env, bad timeout, …)
"""

from __future__ import annotations

import argparse
import json
import os
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
DEFAULT_TOKEN_ENV = "REVIEW_API_SMOKE_TOKEN"

# Classification tags. Distinct from the M8.8 exposure smoke so the
# runner parser doesn't conflate the two profiles.
CLASS_TOKEN_REQUIRED = "token_required"          # 403, expected
CLASS_DISABLED = "disabled"                      # 503, fail in this profile
CLASS_VALID_TOKEN_OK = "valid_token_ok"          # 200 with correct token
CLASS_AUTH_PASSED_NOT_FOUND = "auth_passed_not_found"  # 404 with correct token
CLASS_PUBLIC = "public_access"                   # 2xx without/with wrong token
CLASS_TOKEN_REJECTED = "token_rejected_valid_request"
CLASS_DISABLED_WHEN_ENABLED_EXPECTED = "disabled_when_enabled_expected"
CLASS_UNEXPECTED = "unexpected"

TOKEN_MODE_NONE = "no_token"
TOKEN_MODE_WRONG = "wrong_token"
TOKEN_MODE_CORRECT = "correct_token"

# A fixed wrong-token literal that's clearly bogus. Never used for
# anything else; the constant is hard-coded so the test fixture can
# assert that the smoke does not ever pull from any env var for this
# value.
WRONG_TOKEN_LITERAL = "policy_ai-m95-smoke-wrong-token-do-not-use"

# Nonexistent task id used for the four post-auth 404 probes.
NONEXISTENT_TASK_ID = "nonexistent-token-gate-smoke-id"


@dataclass
class Probe:
    method: str
    path: str
    token_mode: str


# The probe catalogue is intentionally GET-only and exactly six entries.
PROBES: Tuple[Probe, ...] = (
    Probe("GET", "/review/tasks", TOKEN_MODE_NONE),
    Probe("GET", "/review/tasks", TOKEN_MODE_WRONG),
    Probe("GET", "/review/tasks", TOKEN_MODE_CORRECT),
    Probe("GET", f"/review/tasks/{NONEXISTENT_TASK_ID}", TOKEN_MODE_CORRECT),
    Probe("GET", f"/review/tasks/{NONEXISTENT_TASK_ID}/decisions",
          TOKEN_MODE_CORRECT),
    Probe("GET", f"/review/tasks/{NONEXISTENT_TASK_ID}/audit-packet",
          TOKEN_MODE_CORRECT),
)


_DISABLED_BODY_RE = re.compile(r"disabled", re.IGNORECASE)


@dataclass
class ProbeResult:
    method: str
    path: str
    token_mode: str
    status_code: int
    classification: str
    body_snippet: str = ""
    error: Optional[str] = None


@dataclass
class SmokeResult:
    passed: bool
    base_url: str
    token_env_var: str
    token_present: bool
    token_value_printed: bool = False
    public_access_detected: bool = False
    disabled_detected: bool = False
    token_gate_ok: bool = False
    valid_token_read_ok: bool = False
    auth_passed_not_found_count: int = 0
    token_required_count: int = 0
    disabled_count: int = 0
    unexpected_count: int = 0
    results: List[ProbeResult] = field(default_factory=list)
    recommendation: str = ""
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _normalize_base_url(raw: str) -> str:
    if raw is None:
        return ""
    base = str(raw).strip()
    while base.endswith("/"):
        base = base[:-1]
    return base


def _make_request(
    method: str, url: str, *,
    token_header: Optional[str], timeout_seconds: float,
) -> Tuple[int, str, Optional[str]]:
    """Single read-only HTTP request. Never logs the token. Returns
    ``(status_code, body_text, error_string)``. Network failures map
    to ``(0, "", err)`` so the classifier can treat them as
    ``unexpected``.
    """
    headers = {
        "User-Agent": "policy_ai-token-gate-smoke/M9.5",
        "Accept": "application/json",
    }
    if token_header:
        # The header carries the token value. We do NOT echo, log, or
        # otherwise persist this header after the request finishes.
        headers["X-Review-Token"] = token_header
    req = urllib.request.Request(url=url, data=None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            payload = resp.read() or b""
            return status, payload.decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as http_err:
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


FetchFn = Callable[[str, str, Optional[str]], Tuple[int, str, Optional[str]]]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_response(
    status_code: int, body_text: str, token_mode: str,
) -> str:
    """Map (status, body, token_mode) → stable classification label."""
    # 2xx without a valid token → public exposure.
    if 200 <= status_code < 300 and token_mode != TOKEN_MODE_CORRECT:
        return CLASS_PUBLIC
    if 200 <= status_code < 300 and token_mode == TOKEN_MODE_CORRECT:
        return CLASS_VALID_TOKEN_OK
    if status_code == 404 and token_mode == TOKEN_MODE_CORRECT:
        return CLASS_AUTH_PASSED_NOT_FOUND
    if status_code == 403:
        # 403 with no token or wrong token is the expected gate; 403
        # with the correct token would be a token-rejection failure
        # (e.g. Render's REVIEW_API_TOKEN doesn't match the smoke env).
        if token_mode == TOKEN_MODE_CORRECT:
            return CLASS_TOKEN_REJECTED
        return CLASS_TOKEN_REQUIRED
    if status_code == 503 and _DISABLED_BODY_RE.search(body_text or ""):
        return CLASS_DISABLED
    return CLASS_UNEXPECTED


def _trim_body_for_report(body_text: str, *, max_len: int = 240) -> str:
    if not body_text:
        return ""
    text = body_text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    text = re.sub(r"[0-9a-fA-F]{32,}", "<redacted>", text)
    text = re.sub(r"[A-Za-z0-9+/=]{40,}", "<redacted>", text)
    return text


def _resolve_token_header(token_mode: str, correct_token: str) -> Optional[str]:
    if token_mode == TOKEN_MODE_NONE:
        return None
    if token_mode == TOKEN_MODE_WRONG:
        return WRONG_TOKEN_LITERAL
    if token_mode == TOKEN_MODE_CORRECT:
        return correct_token
    return None


def _build_recommendation(result: SmokeResult) -> str:
    if result.public_access_detected:
        return (
            "FAIL: at least one /review/* endpoint returned 2xx WITHOUT a "
            "valid token. This is a public-exposure incident. Set "
            "REVIEW_API_ENABLED=false in the Render dashboard immediately "
            "and investigate before anything else."
        )
    if result.disabled_detected and not result.token_gate_ok:
        return (
            "FAIL (token-gate mode): the review API is currently disabled "
            "(503). No public exposure was detected — the gate is simply "
            "off. Confirm whether REVIEW_API_ENABLED should be true on "
            "this deploy; if not, the M8.8 review-exposure profile is the "
            "right check, not this one."
        )
    # Distinguish a token rejection from a generic unexpected fail so
    # the operator can act on the most likely cause first.
    token_rejected = any(
        r.classification == CLASS_TOKEN_REJECTED for r in result.results
    )
    if token_rejected:
        return (
            "FAIL: the correct-token GET /review/tasks returned 403. "
            "The local REVIEW_API_SMOKE_TOKEN value does not match the "
            "REVIEW_API_TOKEN configured on the deploy. Re-check the "
            "Render dashboard env var; do not paste either value into "
            "chat or any log."
        )
    if result.unexpected_count:
        return (
            "FAIL: at least one probe returned an unexpected status "
            "(not 200/403/404/503-disabled). Inspect the per-probe results; "
            "this may indicate a misconfigured proxy, upstream outage, or "
            "regression in the review_auth gate."
        )
    if result.passed:
        return (
            "PASS: review API is enabled and token-gated. No-token and "
            "wrong-token probes returned 403; correct-token GET /review/tasks "
            "returned 200; correct-token nonexistent-id probes returned 404 "
            "after auth. No records created."
        )
    return (
        "FAIL (token-gate mode): one or more checks did not match the "
        "expected token-gate classification. Inspect the per-probe results."
    )


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_token_gate_smoke(
    base_url: str,
    *,
    correct_token: str,
    token_env_var: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    fetch_fn: Optional[FetchFn] = None,
    probes: Sequence[Probe] = PROBES,
) -> SmokeResult:
    """Pure-ish entry point. Tests inject a stub ``fetch_fn`` so no
    real HTTP request is fired during the offline suite."""
    normalized = _normalize_base_url(base_url)
    fetch = fetch_fn or (
        lambda method, url, token_header:
            _make_request(method, url,
                          token_header=token_header,
                          timeout_seconds=timeout_seconds)
    )

    results: List[ProbeResult] = []
    counts = {
        CLASS_TOKEN_REQUIRED: 0, CLASS_DISABLED: 0,
        CLASS_VALID_TOKEN_OK: 0, CLASS_AUTH_PASSED_NOT_FOUND: 0,
        CLASS_PUBLIC: 0, CLASS_TOKEN_REJECTED: 0,
        CLASS_DISABLED_WHEN_ENABLED_EXPECTED: 0, CLASS_UNEXPECTED: 0,
    }
    errors: List[str] = []
    warnings: List[str] = []

    for probe in probes:
        url = normalized + probe.path
        token_header = _resolve_token_header(probe.token_mode, correct_token)
        status, body_text, err = fetch(probe.method, url, token_header)
        classification = classify_response(status, body_text, probe.token_mode)
        # Promote 503-disabled to the profile-specific
        # "disabled_when_enabled_expected" so the runner sees a clear
        # mismatch signal vs. the generic "disabled" exposure smoke uses.
        if classification == CLASS_DISABLED:
            classification = CLASS_DISABLED_WHEN_ENABLED_EXPECTED
        counts[classification] = counts.get(classification, 0) + 1

        if err and classification == CLASS_UNEXPECTED:
            errors.append(f"{probe.method} {probe.path} ({probe.token_mode}): {err}")

        results.append(ProbeResult(
            method=probe.method,
            path=probe.path,
            token_mode=probe.token_mode,
            status_code=status,
            classification=classification,
            body_snippet=_trim_body_for_report(body_text),
            error=err,
        ))

    public_access_detected = counts[CLASS_PUBLIC] > 0
    disabled_detected = counts[CLASS_DISABLED_WHEN_ENABLED_EXPECTED] > 0
    unexpected_count = counts[CLASS_UNEXPECTED]
    token_required_count = counts[CLASS_TOKEN_REQUIRED]
    not_found_count = counts[CLASS_AUTH_PASSED_NOT_FOUND]
    valid_token_read_ok = counts[CLASS_VALID_TOKEN_OK] >= 1
    token_rejected = counts[CLASS_TOKEN_REJECTED] > 0

    # token_gate_ok = the *expected* shape held: no-token 403 +
    # wrong-token 403 + correct-token 200 + the three nonexistent 404s.
    expected_pass = (
        not public_access_detected
        and not disabled_detected
        and not token_rejected
        and unexpected_count == 0
        and token_required_count == 2          # no_token + wrong_token
        and valid_token_read_ok                 # at least one correct_token 200
        and not_found_count == 3                # three nonexistent 404s
    )
    token_gate_ok = bool(expected_pass)
    overall_pass = token_gate_ok

    smoke = SmokeResult(
        passed=overall_pass,
        base_url=normalized,
        token_env_var=token_env_var,
        token_present=True,
        token_value_printed=False,
        public_access_detected=public_access_detected,
        disabled_detected=disabled_detected,
        token_gate_ok=token_gate_ok,
        valid_token_read_ok=valid_token_read_ok,
        auth_passed_not_found_count=not_found_count,
        token_required_count=token_required_count,
        disabled_count=counts[CLASS_DISABLED_WHEN_ENABLED_EXPECTED],
        unexpected_count=unexpected_count,
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
        "token_env_var": smoke.token_env_var,
        "token_present": smoke.token_present,
        "token_value_printed": smoke.token_value_printed,
        "public_access_detected": smoke.public_access_detected,
        "disabled_detected": smoke.disabled_detected,
        "token_gate_ok": smoke.token_gate_ok,
        "valid_token_read_ok": smoke.valid_token_read_ok,
        "auth_passed_not_found_count": smoke.auth_passed_not_found_count,
        "token_required_count": smoke.token_required_count,
        "disabled_count": smoke.disabled_count,
        "unexpected_count": smoke.unexpected_count,
        "results": [
            {
                "method": r.method,
                "path": r.path,
                "token_mode": r.token_mode,
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
    print(f"[token-gate] base_url={smoke.base_url}")
    print(f"[token-gate] token_env_var={smoke.token_env_var} "
          f"token_present={smoke.token_present} "
          f"token_value_printed={smoke.token_value_printed}")
    print(
        "[token-gate] public_access_detected="
        f"{smoke.public_access_detected} "
        f"disabled_detected={smoke.disabled_detected} "
        f"token_gate_ok={smoke.token_gate_ok} "
        f"valid_token_read_ok={smoke.valid_token_read_ok}"
    )
    print(
        "[token-gate] counts: "
        f"token_required={smoke.token_required_count} "
        f"auth_passed_not_found={smoke.auth_passed_not_found_count} "
        f"disabled={smoke.disabled_count} "
        f"unexpected={smoke.unexpected_count}"
    )
    for r in smoke.results:
        print(
            f"[token-gate]   {r.method} {r.path} ({r.token_mode}) "
            f"-> {r.status_code} [{r.classification}]"
        )
    for w in smoke.warnings:
        print(f"[token-gate] warn: {w}")
    for e in smoke.errors:
        print(f"[token-gate] error: {e}")
    print(f"[token-gate] recommendation: {smoke.recommendation}")
    print(f"[token-gate] passed={smoke.passed}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Controlled token-gate smoke for the M8.0+ review API. Reads "
            "the token from a local environment variable (default name: "
            f"{DEFAULT_TOKEN_ENV}) — NEVER from a CLI flag. Issues six "
            "read-only GETs and verifies the gate behavior. No writes, "
            "no records created, no OpenAI calls, no Render env "
            "modification, no token value ever printed."
        ),
    )
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL to probe (e.g. https://policy-ai-q5ax.onrender.com).",
    )
    parser.add_argument(
        "--token-env", default=DEFAULT_TOKEN_ENV,
        help=(
            f"Env var name that holds the correct token. Default: "
            f"{DEFAULT_TOKEN_ENV}. The script reads "
            "os.environ[<name>]; it never accepts a token via CLI."
        ),
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
        print("[token-gate] --timeout-seconds must be > 0.", file=sys.stderr)
        return 2

    if not args.base_url or not _normalize_base_url(args.base_url):
        print("[token-gate] --base-url must be a non-empty URL.", file=sys.stderr)
        return 2

    token_env_var = (args.token_env or "").strip()
    if not token_env_var:
        print("[token-gate] --token-env must be a non-empty env var name.",
              file=sys.stderr)
        return 2

    correct_token = os.environ.get(token_env_var) or ""
    if not correct_token.strip():
        # IMPORTANT: do NOT echo the env var contents (even when empty),
        # do NOT suggest pasting a token into chat / CLI, and do NOT
        # fall back to any other env var.
        print(
            f"[token-gate] {token_env_var} is missing or empty. Set it "
            f"locally before running, e.g. in PowerShell:\n"
            f"    $env:{token_env_var} = \"<paste the token locally only>\"\n"
            f"    python scripts/smoke_review_api_token_gate.py --base-url <url>\n"
            f"    Remove-Item Env:\\{token_env_var}\n"
            f"Do not paste the token into chat or any committed file.",
            file=sys.stderr,
        )
        return 2

    smoke = run_token_gate_smoke(
        args.base_url,
        correct_token=correct_token,
        token_env_var=token_env_var,
        timeout_seconds=args.timeout_seconds,
    )

    payload = smoke_to_dict(smoke)
    if not args.json:
        _print_human_summary(smoke)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0 if smoke.passed else 1


if __name__ == "__main__":
    sys.exit(main())
