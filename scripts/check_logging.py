"""Structured logging diagnostic CLI (M14.0a).

Read-only by default. Reports the current logging configuration and,
with ``--emit-sample`` / ``--emit-sample-with-extra``, produces a few
sample log lines in the active format so operators can verify what a
Render log line will look like.

The CLI NEVER touches ``policy_ai.db``, never makes network calls, and
never writes to ``reports/``.

Usage::

    python scripts/check_logging.py --help
    python scripts/check_logging.py                       # human-readable
    python scripts/check_logging.py --status              # alias
    python scripts/check_logging.py --json                # machine-readable
    python scripts/check_logging.py --emit-sample
    python scripts/check_logging.py --emit-sample-with-extra
    LOG_FORMAT=json python scripts/check_logging.py --emit-sample

Exit codes::

    0 — status reported / sample emitted successfully
    1 — sample emission failed
    2 — CLI usage error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


import structured_logging  # noqa: E402


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def _render_status_human(status: dict) -> str:
    fmt = status["log_format"]
    enabled_suffix = (
        "" if fmt == "json"
        else "  (env unset or != \"json\"; default text format)"
    )
    lines = ["=== Structured Logging Status ===", ""]
    lines.append(f"LOG_FORMAT:         {fmt}{enabled_suffix}")
    lines.append(f"LOG_LEVEL:          {status['log_level_name']}")
    lines.append(f"Configured:         {status['configured']}")
    lines.append(f"Managed handlers:   {status['managed_handler_count']}")
    lines.append(f"Total handlers:     {status['total_handler_count']}")
    lines.append("")
    if fmt == "json":
        lines.append(
            "[Safety] JSON output enabled. Each log line is a single "
            "JSON object on stderr."
        )
    else:
        lines.append(
            "[Safety] M14.0a is opt-in. print() calls throughout the "
            "codebase are unchanged."
        )
        lines.append(
            "[Safety] Set LOG_FORMAT=json to enable JSON output."
        )
    lines.append(
        "[Safety] Recent M13.x modules use structured_logging.get_logger(__name__)."
    )
    lines.append(
        "[Safety] Legacy modules (api_server, main, official_crawler, etc.) "
        "still use print() and stdlib logging directly -- M14.0b migrates those."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sample emission
# ---------------------------------------------------------------------------


_SAMPLE_LOGGER_NAME = "structured_logging.sample"


def _emit_samples(with_extra: bool = False) -> bool:
    """Emit one INFO, one WARNING, one ERROR record. Returns True on
    success."""
    try:
        # Force a fresh configure cycle so an operator who set
        # LOG_FORMAT mid-shell sees the new format immediately. This
        # mirrors what every real-process boot does.
        structured_logging.configure_logging(force=True)
        sample_log = structured_logging.get_logger(_SAMPLE_LOGGER_NAME)

        # Surface the human header to stdout so operators can tell the
        # emission run apart from the actual log lines (which go to
        # stderr).
        print("=== Sample log emission ===")
        sys.stdout.flush()

        if with_extra:
            sample_log.info(
                "Judge action",
                extra={"action": "confirm", "analysis_id": 105},
            )
            sample_log.warning(
                "Cache miss",
                extra={"url": "https://example.gov.kr/x", "domain": "example.gov.kr"},
            )
            sample_log.error(
                "Postgres backfill failed",
                extra={"table": "analysis_results", "rows_errored": 1},
            )
        else:
            sample_log.info("This is an INFO message")
            sample_log.warning("This is a WARNING message")
            sample_log.error("This is an ERROR message")

        # Korean text smoke — proves UTF-8 preservation in JSON mode.
        sample_log.info("의미 매칭 근거 부족")
        return True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[check_logging] sample emission failed: {exc}\n")
        return False


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_logging",
        description=(
            "Diagnostic CLI for the M14.0a structured logging "
            "infrastructure. Reports current config and, with "
            "--emit-sample, produces representative log lines in "
            "the active format."
        ),
        epilog=(
            "Exit codes:\n"
            "  0 -- status reported / sample emitted\n"
            "  1 -- sample emission failed\n"
            "  2 -- CLI usage error\n\n"
            "Safety: M14.0a is opt-in. print() calls in the codebase "
            "are unchanged. Setting LOG_FORMAT=json only affects logs "
            "produced via the structured logger (M13.x modules and any "
            "new code)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true",
        help="Report the current logging configuration (default).",
    )
    mode.add_argument(
        "--emit-sample", action="store_true",
        help=(
            "Emit one INFO, WARNING, ERROR record plus a Korean-text "
            "smoke line via the structured logger."
        ),
    )
    mode.add_argument(
        "--emit-sample-with-extra", action="store_true",
        help=(
            "Same as --emit-sample but each record carries an "
            "extra={...} payload so operators can see the JSON "
            "'extra' field."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the human report.",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.emit_sample or args.emit_sample_with_extra:
        ok = _emit_samples(with_extra=args.emit_sample_with_extra)
        return 0 if ok else 1

    # Default mode: status report.
    status = structured_logging.health_check()
    if args.json:
        payload = dict(status)
        payload["safety"] = {
            "milestone": "M14.0a",
            "print_calls_replaced": False,
            "legacy_modules_modified": False,
            "external_logging_service": None,
        }
        print(json.dumps(
            payload, indent=2, ensure_ascii=False, sort_keys=True,
        ))
    else:
        print(_render_status_human(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
