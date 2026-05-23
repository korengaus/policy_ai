"""JSON logging production verification (M14.2).

Validates that ``structured_logging`` produces well-formed JSON when
``LOG_FORMAT=json`` is set, and helps operators verify the same is
true on Render after they activate the env var.

The script has two modes:

* ``--local`` (default): subprocesses ``scripts/check_logging.py
  --emit-sample`` with ``LOG_FORMAT=json`` in the child env, captures
  stderr, parses each line as JSON, and validates the schema
  (``ts``, ``level``, ``module``, ``msg``) plus Korean UTF-8
  preservation. No Render call.
* ``--base-url``: GETs ``/health`` and runs a single
  ``scripts/smoke_async_job.py`` invocation, then prints what the
  operator should look for in the Render dashboard. The script does
  NOT parse Render logs (no Render API token is configured); the
  operator must inspect the dashboard themselves.

Usage::

    python scripts/check_json_logging.py --help
    python scripts/check_json_logging.py --local
    python scripts/check_json_logging.py --base-url https://policy-ai-q5ax.onrender.com
    python scripts/check_json_logging.py --local --json

Exit codes::

    0 -- verification completed (local PASS, or Render reachable)
    1 -- local found malformed JSON, OR Render is unreachable
    2 -- CLI usage error

Safety:
    * Does NOT modify any Render env var.
    * Does NOT call any Render API.
    * Does NOT modify the local process env after exit -- env mutations
      happen only inside the subprocess invocation for ``--local`` mode.
    * Does NOT modify any local file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# JSON schema
# ---------------------------------------------------------------------------


REQUIRED_KEYS = ("ts", "level", "module", "msg")
ALLOWED_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Substring the Korean sample line should contain. Used to verify
# UTF-8 preservation across the subprocess -> file -> parser chain.
KOREAN_SAMPLE_FRAGMENT = "의미 매칭"
KOREAN_SAMPLE_FULL = "의미 매칭 근거 부족"


def _validate_iso_ts(value) -> Optional[str]:
    """Return None if value parses as an ISO 8601 timestamp,
    otherwise an error message."""
    if not isinstance(value, str):
        return f"ts is not a string (got {type(value).__name__})"
    try:
        # fromisoformat handles offsets like '+00:00' on 3.11+.
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        return f"ts is not ISO 8601: {exc}"
    return None


def _validate_record(record: dict) -> list:
    """Return a list of human-readable errors (empty list = valid)."""
    errors = []
    for key in REQUIRED_KEYS:
        if key not in record:
            errors.append(f"missing required key '{key}'")
    if not errors:
        level = record.get("level")
        if level not in ALLOWED_LEVELS:
            errors.append(
                f"level {level!r} is not one of {ALLOWED_LEVELS}"
            )
        ts_err = _validate_iso_ts(record.get("ts"))
        if ts_err:
            errors.append(ts_err)
        if not isinstance(record.get("module"), str):
            errors.append("module is not a string")
        if not isinstance(record.get("msg"), str):
            errors.append("msg is not a string")
    return errors


def _check_korean_preservation(line_text: str, record: dict) -> Optional[str]:
    """If this is the Korean sample line, verify it's preserved as
    UTF-8 in the raw line and as Hangul in the parsed message. Returns
    None on success or when the line isn't the Korean sample, or an
    error message on failure."""
    msg = record.get("msg") or ""
    if KOREAN_SAMPLE_FRAGMENT not in msg:
        return None
    # The decoded message contains Hangul characters as expected.
    # Now check the raw line: it must contain the literal UTF-8 form,
    # not the ASCII-escaped form. ensure_ascii=False is the M14.0a
    # contract.
    if re.search(r"\\u[0-9a-fA-F]{4}", line_text):
        return (
            "Korean sample line is ASCII-escaped (contains \\uXXXX). "
            "Expected literal UTF-8 — JsonFormatter must use "
            "ensure_ascii=False."
        )
    if KOREAN_SAMPLE_FRAGMENT not in line_text:
        return (
            "Korean sample fragment "
            f"{KOREAN_SAMPLE_FRAGMENT!r} not found verbatim in raw "
            "line — may have been transcoded by the subprocess pipe."
        )
    return None


# ---------------------------------------------------------------------------
# --local mode: subprocess + capture
# ---------------------------------------------------------------------------


def _run_local_subprocess() -> tuple:
    """Run check_logging.py --emit-sample with LOG_FORMAT=json in a
    child env. Returns (returncode, stdout_text, stderr_bytes).
    Stderr is bytes so we can verify UTF-8 byte sequences directly."""
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "scripts" / "check_logging.py"),
        "--emit-sample",
    ]
    env = dict(os.environ)
    env["LOG_FORMAT"] = "json"
    # Force the child stdout/stderr to use UTF-8 (Windows defaults to
    # cp949 on Korean locales; structured_logging reconfigures stderr
    # internally but the operator's shell may have set PYTHONIOENCODING).
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=False,  # bytes so we can verify UTF-8 directly
            timeout=30,
            env=env,
        )
        return (
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr,
        )
    except subprocess.TimeoutExpired:
        return (-1, "", b"subprocess timeout")
    except Exception as exc:  # noqa: BLE001 — verification must not propagate
        return (-1, "", f"subprocess error: {exc}".encode("utf-8"))


def _verify_local() -> dict:
    """Run the local subprocess and validate every captured line.
    Returns a result dict suitable for both human and JSON output."""
    rc, stdout_text, stderr_bytes = _run_local_subprocess()
    if rc != 0:
        return {
            "mode": "local",
            "passed": False,
            "reason": (
                f"subprocess exit_code={rc}; stderr_tail="
                + stderr_bytes[-400:].decode("utf-8", errors="replace")
            ),
            "lines": [],
            "subprocess_rc": rc,
        }

    try:
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return {
            "mode": "local",
            "passed": False,
            "reason": (
                "subprocess stderr is not valid UTF-8: "
                f"{exc} (byte {exc.start})"
            ),
            "lines": [],
            "subprocess_rc": rc,
        }

    raw_lines = [ln for ln in stderr_text.splitlines() if ln.strip()]
    line_results = []
    all_passed = True
    for index, raw in enumerate(raw_lines, start=1):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            line_results.append({
                "index": index,
                "passed": False,
                "errors": [f"json parse error: {exc.msg}"],
                "raw": raw[:200],
            })
            all_passed = False
            continue
        if not isinstance(record, dict):
            line_results.append({
                "index": index,
                "passed": False,
                "errors": [
                    f"record is not a JSON object (got {type(record).__name__})"
                ],
                "raw": raw[:200],
            })
            all_passed = False
            continue
        errors = _validate_record(record)
        korean_err = _check_korean_preservation(raw, record)
        if korean_err is not None:
            errors.append(korean_err)
        line_results.append({
            "index": index,
            "passed": not errors,
            "errors": errors,
            "ts": record.get("ts"),
            "level": record.get("level"),
            "module": record.get("module"),
            "msg": record.get("msg"),
        })
        if errors:
            all_passed = False

    if not raw_lines:
        return {
            "mode": "local",
            "passed": False,
            "reason": (
                "subprocess produced no stderr lines -- JSON formatter "
                "may not be installed correctly."
            ),
            "lines": [],
            "subprocess_rc": rc,
        }

    return {
        "mode": "local",
        "passed": all_passed,
        "reason": (
            "all lines valid"
            if all_passed
            else "one or more lines failed schema validation"
        ),
        "lines": line_results,
        "subprocess_rc": rc,
        "lines_total": len(raw_lines),
    }


# ---------------------------------------------------------------------------
# --base-url mode: smoke + operator guidance
# ---------------------------------------------------------------------------


def _http_get_status(url: str, timeout: float = 15.0) -> tuple:
    """Returns ``(status_code, error_message)``. Uses stdlib only;
    never imports requests."""
    try:
        request = urllib.request.Request(
            url=url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP error {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return -1, f"network error: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return -1, f"unexpected error: {exc}"


def _run_smoke_subprocess(base_url: str) -> tuple:
    """Invoke smoke_async_job.py against ``base_url``. Returns
    ``(returncode, stdout_text, stderr_text)``."""
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "scripts" / "smoke_async_job.py"),
        "--base-url", base_url,
        "--query", "전세사기",
        "--max-news", "1",
        "--timeout-seconds", "300",
        "--poll-interval", "2",
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=360,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "smoke subprocess timeout"
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"smoke subprocess error: {exc}"


def _verify_render(base_url: str, run_smoke: bool = True) -> dict:
    base_url = base_url.rstrip("/")
    health_url = base_url + "/health"
    health_status, health_err = _http_get_status(health_url)
    health_ok = health_status == 200
    result = {
        "mode": "render",
        "base_url": base_url,
        "health_status": health_status,
        "health_error": health_err,
        "health_ok": health_ok,
        "smoke_invoked": False,
        "smoke_rc": None,
        "smoke_elapsed_summary": None,
    }
    if not health_ok:
        result["passed"] = False
        result["reason"] = (
            f"Render /health did not return 200 "
            f"(status={health_status}, err={health_err})"
        )
        return result

    if run_smoke:
        result["smoke_invoked"] = True
        rc, stdout, stderr = _run_smoke_subprocess(base_url)
        result["smoke_rc"] = rc
        # Extract a short summary line (no full smoke output included
        # because operators inspect Render logs separately).
        combined = (stdout or "") + "\n" + (stderr or "")
        elapsed_match = re.search(r"elapsed\s*=\s*([\d.]+)s", combined)
        if elapsed_match:
            result["smoke_elapsed_summary"] = (
                f"elapsed={elapsed_match.group(1)}s"
            )
        if rc != 0:
            result["passed"] = False
            result["reason"] = (
                f"smoke_async_job exited {rc}; check Render health"
            )
            return result

    result["passed"] = True
    result["reason"] = "Render reachable; inspect log viewer manually"
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_local_human(result: dict) -> str:
    lines = [
        "=== JSON Logging Verification (local) ===",
        "",
        "Mode: local subprocess with LOG_FORMAT=json",
        "",
    ]
    lines.append(
        f"Captured {result.get('lines_total', 0)} stderr lines."
    )
    lines.append("")
    for entry in result.get("lines", []):
        if entry["passed"]:
            lines.append(f"Line {entry['index']}: VALID JSON")
            lines.append(f"  ts: {entry['ts']}")
            lines.append(f"  level: {entry['level']}")
            lines.append(f"  module: {entry['module']}")
            msg = entry["msg"] or ""
            if KOREAN_SAMPLE_FRAGMENT in msg:
                lines.append(f"  msg: {msg}")
                lines.append(
                    f"  Korean preserved: {KOREAN_SAMPLE_FULL!r} (UTF-8 OK)"
                )
            else:
                lines.append(f'  msg: "{msg}"')
        else:
            lines.append(f"Line {entry['index']}: INVALID")
            for err in entry["errors"]:
                lines.append(f"  - {err}")
            if entry.get("raw"):
                lines.append(f"  raw: {entry['raw']}")
        lines.append("")
    lines.append(
        "[Safety] M14.2 verification is local-only. Render activation "
        "is a separate operator step. See "
        "docs/JSON_LOGGING_ACTIVATION_GUIDE.md."
    )
    if result["passed"]:
        lines.append("")
        lines.append(
            "Result: PASS -- JSON logging works correctly in local mode."
        )
    else:
        lines.append("")
        lines.append(
            "Result: FAIL -- JSON output does not match expected schema."
        )
        lines.append(
            "Investigate structured_logging.JsonFormatter changes since "
            "M14.0a."
        )
        if result.get("reason"):
            lines.append(f"  reason: {result['reason']}")
    return "\n".join(lines)


def _render_render_human(result: dict) -> str:
    lines = [
        "=== JSON Logging Verification (Render) ===",
        "",
        f"Base URL: {result['base_url']}",
        "",
        f"Step 1: Confirming Render is up",
    ]
    if result["health_ok"]:
        lines.append(f"  GET /health -> 200 OK")
    else:
        lines.append(
            f"  GET /health -> {result['health_status']} "
            f"({result.get('health_error') or 'unreachable'})"
        )
        if result.get("reason"):
            lines.append(f"  reason: {result['reason']}")

    if result.get("smoke_invoked"):
        lines.append("")
        lines.append(
            f"Step 2: Running smoke_async_job to generate fresh log entries"
        )
        if result["smoke_rc"] == 0:
            elapsed = (
                result.get("smoke_elapsed_summary") or "elapsed=?"
            )
            lines.append(f"  ...")
            lines.append(f"  status=pass {elapsed}")
        else:
            lines.append(
                f"  smoke exited {result['smoke_rc']} (check Render "
                "service health before continuing)"
            )

    lines.append("")
    lines.append("Step 3: What to check in Render dashboard")
    lines.append("")
    lines.append(
        "Go to Render dashboard -> policy-ai service -> Logs tab."
    )
    lines.append(
        "Filter for logs since the last ~2 minutes."
    )
    lines.append("")
    lines.append("If LOG_FORMAT=json is set on Render:")
    lines.append(
        "  - Each log line should be a JSON object (starts with `{`)"
    )
    lines.append(
        "  - Each should have ts, level, module, msg"
    )
    lines.append("  - Examples to search for:")
    lines.append(
        "      - \"official_crawler_cache_event\"  (HTTP cache events)"
    )
    lines.append("      - \"module\":\"verification_card\"")
    lines.append("      - \"module\":\"policy_decision\"")
    lines.append("")
    lines.append("If LOG_FORMAT is NOT set on Render:")
    lines.append(
        "  - Logs appear as plain text:"
    )
    lines.append(
        "      \"2026-05-23 14:32:01 INFO module: message\""
    )
    lines.append(
        "  - To activate JSON mode, add LOG_FORMAT=json on Render. "
        "See docs/JSON_LOGGING_ACTIVATION_GUIDE.md."
    )
    lines.append("")
    lines.append(
        "[Safety] This script does NOT modify Render env vars."
    )
    lines.append(
        "[Safety] No verdict, no analysis result depends on log format."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_json_logging",
        description=(
            "Verify that structured_logging produces well-formed JSON "
            "in LOG_FORMAT=json mode, and (in --base-url mode) help "
            "the operator confirm the same on Render. Does NOT modify "
            "Render env vars and does NOT call any Render API."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- verification completed (local PASS / Render "
            "reachable)\n"
            "  1 -- local found malformed JSON OR Render is "
            "unreachable\n"
            "  2 -- CLI usage error\n\n"
            "Safety: this script never modifies env vars after exit. "
            "See docs/JSON_LOGGING_ACTIVATION_GUIDE.md for the operator "
            "activation procedure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--local", action="store_true",
        help=(
            "Verify JSON output locally by subprocessing "
            "check_logging.py --emit-sample with LOG_FORMAT=json. "
            "Default when neither --local nor --base-url is given."
        ),
    )
    mode.add_argument(
        "--base-url", default=None,
        help=(
            "Render base URL. Runs /health + smoke_async_job and "
            "prints what the operator should look for in the Render "
            "log viewer. Does NOT call any Render API."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the human report.",
    )
    parser.add_argument(
        "--skip-smoke", action="store_true",
        help=(
            "When --base-url is set, only check /health -- skip the "
            "smoke_async_job invocation. Useful when Render is slow "
            "or unreachable."
        ),
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.base_url:
        result = _verify_render(
            args.base_url, run_smoke=not args.skip_smoke,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(_render_render_human(result))
        return 0 if result.get("passed") else 1

    # Default: --local
    result = _verify_local()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_local_human(result))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
