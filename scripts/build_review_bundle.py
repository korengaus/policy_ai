"""Phase 2 M8.6: post-implementation review bundle helper.

A *local-only* operator tool that produces a compact, ChatGPT-friendly
summary of the current uncommitted work. It reuses
``scripts/operator_preflight.py`` for path classification (forbidden /
excluded / safe-expected) and adds:

    * a short header (project, timestamp, latest commit, mode)
    * a preflight summary
    * a recommended ``git add`` command listing **only** safe expected
      files
    * an optional, length-capped ``git diff`` section restricted to
      safe expected files
    * a copy-paste-friendly ChatGPT review block
    * a fixed safety reminder

This script must never stage, commit, push, modify git state, modify
Render env, call OpenAI, or hit any network. The only subprocess
invocations it makes are read-only git commands:

    * ``git status --porcelain``           (via preflight)
    * ``git log --oneline -1``
    * ``git diff --no-color HEAD -- <safe expected path>``

Usage:

    python scripts/build_review_bundle.py --expected scripts/foo.py tests/test_foo.py
    python scripts/build_review_bundle.py --expected ... --milestone "Phase 2 M8.6"
    python scripts/build_review_bundle.py --expected ... --include-diff
    python scripts/build_review_bundle.py --expected ... --stdout
    python scripts/build_review_bundle.py --expected ... --json
    python scripts/build_review_bundle.py --expected ... --chatgpt-summary

Exit codes:
    0 — bundle written / printed successfully
    1 — bundle written but ``commit_ready=False`` (something to review)
    2 — bad CLI usage
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

import scripts.operator_preflight as preflight  # noqa: E402


PROJECT_NAME = "policy_ai"

# review_bundle_*.txt is this script's own output. Always treat as a
# gitignored generated artifact, even if for some reason it appears
# outside reports/ (which would already be forbidden by preflight).
_REVIEW_BUNDLE_NAME_RE = re.compile(r"^(?:.*/)?review_bundle_[^/]+\.txt$")

DEFAULT_MAX_DIFF_CHARS = 30000

REPORTS_DIR_NAME = "reports"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bundle_is_forbidden(path: str) -> bool:
    """Superset of ``preflight.is_forbidden_path`` that also matches the
    helper's own output naming pattern (``review_bundle_*.txt``)."""
    if preflight.is_forbidden_path(path):
        return True
    p = preflight.normalize_path(path)
    return bool(_REVIEW_BUNDLE_NAME_RE.match(p))


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run_git_log_oneline(cwd: Optional[Path] = None) -> str:
    """Return the most recent commit as ``<short-sha> <subject>``. Never
    raises; returns an empty string on any git failure."""
    cmd = ["git", "-c", "core.quotePath=false", "log", "--oneline", "-1"]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _run_git_diff_for_path(path: str, cwd: Optional[Path] = None) -> str:
    """Read-only ``git diff HEAD -- <path>`` for a single path. Returns
    empty string on any failure or for untracked files (where HEAD has
    nothing to diff against)."""
    cmd = [
        "git", "-c", "core.quotePath=false",
        "diff", "--no-color", "HEAD", "--", path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class BundleResult:
    """Stable shape consumed by the formatters and the JSON output."""
    timestamp: str
    milestone: Optional[str]
    latest_commit: str
    mode: str
    test_notes: List[str] = field(default_factory=list)
    summary: Optional[preflight.PreflightSummary] = None
    diff_section: Optional[str] = None
    diff_truncated: bool = False
    diff_skipped_forbidden: List[str] = field(default_factory=list)
    bundle_text: str = ""
    chatgpt_block: str = ""
    output_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def _enforce_review_bundle_forbidden(
    summary: preflight.PreflightSummary,
) -> preflight.PreflightSummary:
    """Promote any ``review_bundle_*.txt`` paths the operator listed in
    ``--expected`` into ``forbidden_files_present``. preflight's own
    classifier already handles them via the ``reports/`` prefix, but a
    bare ``review_bundle_X.txt`` at the repo root would otherwise sneak
    through. Idempotent."""
    promote: List[str] = []
    keep_changed: List[str] = []
    for path in summary.expected_changed_files:
        if _REVIEW_BUNDLE_NAME_RE.match(path):
            promote.append(path)
        else:
            keep_changed.append(path)
    keep_missing: List[str] = []
    for path in summary.expected_missing_files:
        if _REVIEW_BUNDLE_NAME_RE.match(path):
            promote.append(path)
        else:
            keep_missing.append(path)
    if not promote:
        return summary

    new_forbidden = list(summary.forbidden_files_present)
    for path in promote:
        if path not in new_forbidden:
            new_forbidden.append(path)

    new_errors = list(summary.errors)
    for path in promote:
        new_errors.append(
            f"--expected listed a generated review bundle file: {path!r}. "
            "Review bundles are gitignored helper output; remove from --expected."
        )

    summary.expected_changed_files = keep_changed
    summary.expected_missing_files = keep_missing
    summary.forbidden_files_present = new_forbidden
    summary.errors = new_errors
    summary.recommended_git_add_command = preflight.build_recommended_git_add(
        keep_changed,
    )
    summary.commit_ready = False
    summary.passed = False
    return summary


def _build_diff_section(
    summary: preflight.PreflightSummary,
    *,
    max_diff_chars: int,
    diff_provider: Callable[[str], str],
) -> tuple[str, bool, List[str]]:
    """Build the diff block. Skips any path classified as forbidden by
    the bundle's superset check. Truncates the combined text at
    ``max_diff_chars`` characters, appending a clear truncation note.

    Returns (text, truncated, skipped_forbidden_paths)."""
    lines: List[str] = []
    truncated = False
    skipped_forbidden: List[str] = []
    running_len = 0

    for path in summary.expected_changed_files:
        if _bundle_is_forbidden(path):
            skipped_forbidden.append(path)
            continue
        header = f"--- diff: {path} ---"
        diff_body = diff_provider(path) or "(no diff available — possibly untracked)"
        block = header + "\n" + diff_body.rstrip() + "\n"
        # Reserve some room for the truncation note when we have to cut.
        if running_len + len(block) > max_diff_chars:
            remaining = max_diff_chars - running_len
            if remaining > 0:
                lines.append(block[:remaining])
            truncated = True
            lines.append(
                f"\n... [diff truncated at {max_diff_chars} characters]"
            )
            break
        lines.append(block)
        running_len += len(block)

    if not lines:
        return ("", truncated, skipped_forbidden)
    return ("\n".join(lines), truncated, skipped_forbidden)


def _format_list(label: str, items: Sequence[str]) -> List[str]:
    if not items:
        return [f"{label}: (none)"]
    out = [f"{label}:"]
    for item in items:
        out.append(f"  - {item}")
    return out


def _format_header(result: BundleResult) -> str:
    lines: List[str] = []
    lines.append(f"[review bundle] project: {PROJECT_NAME}")
    lines.append(f"[review bundle] timestamp_utc: {result.timestamp}")
    if result.milestone:
        lines.append(f"[review bundle] milestone: {result.milestone}")
    lines.append(
        f"[review bundle] latest_commit: "
        f"{result.latest_commit or '(unavailable — git log returned nothing)'}"
    )
    lines.append(f"[review bundle] mode: {result.mode}")
    return "\n".join(lines)


def _format_preflight_block(summary: preflight.PreflightSummary) -> str:
    lines: List[str] = []
    lines.append("[preflight summary]")
    lines.extend(_format_list("changed files", summary.changed_files))
    lines.extend(_format_list("untracked files", summary.untracked_files))
    lines.extend(_format_list("expected files", summary.expected_files))
    lines.extend(_format_list("expected changed", summary.expected_changed_files))
    lines.extend(_format_list("expected missing", summary.expected_missing_files))
    lines.extend(_format_list("unexpected changed", summary.unexpected_changed_files))
    lines.extend(_format_list(
        "excluded local-only files", summary.excluded_local_only_files,
    ))
    lines.extend(_format_list(
        "forbidden files present", summary.forbidden_files_present,
    ))
    lines.append(f"commit_ready: {summary.commit_ready}")
    if summary.warnings:
        lines.append("warnings:")
        for w in summary.warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("warnings: (none)")
    if summary.errors:
        lines.append("errors:")
        for e in summary.errors:
            lines.append(f"  - {e}")
    else:
        lines.append("errors: (none)")
    return "\n".join(lines)


def _format_recommended_command_block(summary: preflight.PreflightSummary) -> str:
    lines = ["[recommended git add command]"]
    if summary.recommended_git_add_command:
        lines.append(f"  {summary.recommended_git_add_command}")
    else:
        lines.append("  (none — nothing safe to add yet)")
    lines.append(
        "Reminder: do NOT use `git add .`. Stage only the files above."
    )
    return "\n".join(lines)


def _format_chatgpt_block(result: BundleResult) -> str:
    summary = result.summary
    assert summary is not None  # caller guarantees
    lines: List[str] = []
    lines.append("[chatgpt review block — copy/paste below]")
    lines.append("")
    if result.milestone:
        lines.append(f"Milestone: {result.milestone}")
    lines.append(f"Latest commit: {result.latest_commit or '(unavailable)'}")
    lines.append("")
    lines.append("Intended files:")
    if summary.expected_changed_files:
        for p in summary.expected_changed_files:
            lines.append(f"  - {p}")
    else:
        lines.append("  (none — re-run with --expected ...)")
    lines.append("")
    lines.append("Excluded local-only files:")
    if summary.excluded_local_only_files:
        for p in summary.excluded_local_only_files:
            lines.append(f"  - {p}")
    else:
        lines.append("  (none)")
    lines.append("")
    if summary.unexpected_changed_files:
        lines.append("Unexpected changed files (safe, not in --expected):")
        for p in summary.unexpected_changed_files:
            lines.append(f"  - {p}")
        lines.append("")
    if summary.forbidden_files_present:
        lines.append("DANGEROUS files listed in --expected (excluded):")
        for p in summary.forbidden_files_present:
            lines.append(f"  - {p}")
        lines.append("")
    lines.append("Recommended git add command:")
    if summary.recommended_git_add_command:
        lines.append(f"  {summary.recommended_git_add_command}")
    else:
        lines.append("  (none — nothing safe to add yet)")
    lines.append("")
    if result.test_notes:
        lines.append("Manual test notes:")
        for note in result.test_notes:
            lines.append(f"  - {note}")
        lines.append("")
    lines.append(f"commit_ready: {summary.commit_ready}")
    lines.append(
        "Note: this bundle is for review only — nothing has been "
        "staged or committed yet."
    )
    return "\n".join(lines)


def _format_safety_reminder() -> str:
    return "\n".join([
        "[safety reminder]",
        "  - Do NOT use `git add .`.",
        "  - Do NOT commit .claude/settings.local.json.",
        "  - Do NOT commit reports/ (this bundle lives there).",
        "  - Do NOT paste API keys into chat.",
        "  - Review the diff and the recommended command before staging.",
    ])


def _format_test_notes_block(notes: Sequence[str]) -> str:
    if not notes:
        return "[manual test notes]\n  (none provided — pass --test-note ... to include)"
    lines = ["[manual test notes]"]
    for note in notes:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def _format_diff_block(result: BundleResult, *, max_diff_chars: int) -> str:
    if result.diff_section is None:
        return ""
    lines = [f"[diff — safe expected files only, max {max_diff_chars} chars]"]
    if result.diff_skipped_forbidden:
        lines.append("Skipped diff for forbidden expected file(s):")
        for p in result.diff_skipped_forbidden:
            lines.append(f"  - {p}")
    if result.diff_section:
        lines.append(result.diff_section)
    else:
        lines.append("(no diff content — nothing safe to diff)")
    if result.diff_truncated:
        lines.append(f"[truncated at {max_diff_chars} characters]")
    return "\n".join(lines)


def _compose_bundle_text(
    result: BundleResult,
    *,
    include_diff: bool,
    max_diff_chars: int,
) -> str:
    parts: List[str] = [
        _format_header(result),
        "",
        _format_preflight_block(result.summary),  # type: ignore[arg-type]
        "",
        _format_recommended_command_block(result.summary),  # type: ignore[arg-type]
        "",
        _format_chatgpt_block(result),
        "",
        _format_test_notes_block(result.test_notes),
    ]
    if include_diff:
        diff_block = _format_diff_block(result, max_diff_chars=max_diff_chars)
        if diff_block:
            parts.append("")
            parts.append(diff_block)
    parts.append("")
    parts.append(_format_safety_reminder())
    return "\n".join(parts)


def build_bundle(
    expected: Optional[Sequence[str]],
    *,
    milestone: Optional[str] = None,
    test_notes: Optional[Sequence[str]] = None,
    include_diff: bool = False,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    mode: str = "default",
    status_lines: Optional[Sequence[str]] = None,
    latest_commit: Optional[str] = None,
    diff_provider: Optional[Callable[[str], str]] = None,
    timestamp: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> BundleResult:
    """Build the in-memory bundle. Pure-ish — every external dependency
    can be injected (``status_lines``, ``latest_commit``, ``diff_provider``,
    ``timestamp``), so tests do not need real git.
    """
    ts = timestamp or _utc_timestamp()
    if status_lines is None:
        status_lines = preflight.run_git_status(cwd=cwd)
    entries = preflight.parse_git_status_lines(status_lines)
    summary = preflight.classify_paths(entries, expected_files=expected)
    summary = _enforce_review_bundle_forbidden(summary)

    if latest_commit is None:
        latest_commit = _run_git_log_oneline(cwd=cwd)

    notes_list = list(test_notes or [])

    result = BundleResult(
        timestamp=ts,
        milestone=milestone,
        latest_commit=latest_commit or "",
        mode=mode,
        test_notes=notes_list,
        summary=summary,
    )

    if include_diff:
        provider = diff_provider or (lambda p: _run_git_diff_for_path(p, cwd=cwd))
        diff_text, truncated, skipped = _build_diff_section(
            summary,
            max_diff_chars=max_diff_chars,
            diff_provider=provider,
        )
        result.diff_section = diff_text
        result.diff_truncated = truncated
        result.diff_skipped_forbidden = skipped

    result.chatgpt_block = _format_chatgpt_block(result)
    result.bundle_text = _compose_bundle_text(
        result,
        include_diff=include_diff,
        max_diff_chars=max_diff_chars,
    )
    return result


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


JSON_KEYS = (
    "commit_ready",
    "errors",
    "excluded_local_only_files",
    "expected_changed_files",
    "expected_files",
    "expected_missing_files",
    "forbidden_files_present",
    "latest_commit",
    "milestone",
    "output_path",
    "passed",
    "recommended_git_add_command",
    "test_notes",
    "unexpected_changed_files",
    "warnings",
)


def result_to_json_payload(result: BundleResult) -> Dict[str, object]:
    summary = result.summary
    assert summary is not None
    return {
        "commit_ready": summary.commit_ready,
        "errors": list(summary.errors),
        "excluded_local_only_files": list(summary.excluded_local_only_files),
        "expected_changed_files": list(summary.expected_changed_files),
        "expected_files": list(summary.expected_files),
        "expected_missing_files": list(summary.expected_missing_files),
        "forbidden_files_present": list(summary.forbidden_files_present),
        "latest_commit": result.latest_commit,
        "milestone": result.milestone,
        "output_path": result.output_path,
        "passed": summary.passed,
        "recommended_git_add_command": summary.recommended_git_add_command,
        "test_notes": list(result.test_notes),
        "unexpected_changed_files": list(summary.unexpected_changed_files),
        "warnings": list(summary.warnings),
    }


def result_to_json(result: BundleResult) -> str:
    payload = result_to_json_payload(result)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


def _default_output_path(ts: str, repo_root: Path) -> Path:
    return repo_root / REPORTS_DIR_NAME / f"review_bundle_{ts}.txt"


def write_bundle_file(text: str, target: Path) -> Path:
    """Create parent directory if needed, write ``text`` as UTF-8."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a local post-implementation review bundle: a compact, "
            "ChatGPT-friendly summary of the current uncommitted work. "
            "Never stages, commits, pushes, or modifies any file."
        ),
    )
    parser.add_argument(
        "--expected", nargs="*", default=None,
        help=(
            "Whitelist of files the operator intends to stage. Without "
            "this flag the bundle still builds but commit_ready stays "
            "False."
        ),
    )
    parser.add_argument(
        "--milestone", default=None,
        help="Optional milestone label, e.g. \"Phase 2 M8.6\".",
    )
    parser.add_argument(
        "--test-note", dest="test_notes", action="append", default=[],
        help=(
            "Manual test note string (the bundle does NOT run these "
            "commands). Repeat the flag to include multiple notes."
        ),
    )
    parser.add_argument(
        "--include-diff", action="store_true",
        help="Include `git diff HEAD --` output for safe expected files.",
    )
    parser.add_argument(
        "--max-diff-chars", type=int, default=DEFAULT_MAX_DIFF_CHARS,
        help=(
            f"Max characters of diff content before truncation "
            f"(default {DEFAULT_MAX_DIFF_CHARS})."
        ),
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="Print the bundle to stdout; do NOT write a report file.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print JSON to stdout; do NOT write a report file.",
    )
    parser.add_argument(
        "--chatgpt-summary", action="store_true",
        help=(
            "Print only the ChatGPT-pasteable block to stdout; do NOT "
            "write a report file."
        ),
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help=(
            "Optional custom output path (must end in .txt and live "
            "under reports/). Default: reports/review_bundle_<ts>.txt."
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Repository root (defaults to the script's parent directory).",
    )
    return parser


def _validate_out_path(out: Path, repo_root: Path) -> Optional[str]:
    """Return an error string if the explicit ``--out`` is unsafe;
    otherwise return ``None``."""
    try:
        resolved = (out if out.is_absolute() else (repo_root / out)).resolve()
    except Exception:
        return f"could not resolve --out path: {out}"
    if resolved.suffix.lower() != ".txt":
        return f"--out must end in .txt (got {resolved.suffix or '(none)'})"
    try:
        resolved.relative_to(repo_root / REPORTS_DIR_NAME)
    except ValueError:
        return (
            f"--out must live under reports/; refusing to write to {resolved}"
        )
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    exclusive_flags = sum(int(x) for x in (
        args.stdout, args.json, args.chatgpt_summary,
    ))
    if exclusive_flags > 1:
        print(
            "[bundle] --stdout, --json, and --chatgpt-summary are mutually "
            "exclusive.",
            file=sys.stderr,
        )
        return 2

    if args.max_diff_chars <= 0:
        print(
            "[bundle] --max-diff-chars must be > 0.", file=sys.stderr,
        )
        return 2

    repo_root = args.repo_root or ROOT

    if args.stdout:
        mode = "stdout"
    elif args.json:
        mode = "json"
    elif args.chatgpt_summary:
        mode = "chatgpt-summary"
    else:
        mode = "default"

    result = build_bundle(
        expected=args.expected,
        milestone=args.milestone,
        test_notes=args.test_notes,
        include_diff=args.include_diff,
        max_diff_chars=args.max_diff_chars,
        mode=mode,
        cwd=repo_root,
    )

    if mode == "default":
        if args.out is not None:
            err = _validate_out_path(args.out, repo_root)
            if err:
                print(f"[bundle] {err}", file=sys.stderr)
                return 2
            target = (args.out if args.out.is_absolute()
                      else (repo_root / args.out))
        else:
            target = _default_output_path(result.timestamp, repo_root)
        write_bundle_file(result.bundle_text, target)
        result.output_path = str(target)
        # Re-render parts that include output_path? Only the JSON payload
        # uses it, and JSON is its own mode. The text bundle on disk
        # doesn't reference its own path, so no rewrite needed.
        print(f"[bundle] wrote {target}")
        print(
            "[bundle] reminder: do not commit "
            ".claude/settings.local.json or reports/."
        )
    elif mode == "stdout":
        print(result.bundle_text)
        print(
            "[bundle] reminder: do not commit "
            ".claude/settings.local.json or reports/.",
            file=sys.stderr,
        )
    elif mode == "json":
        print(result_to_json(result))
    elif mode == "chatgpt-summary":
        print(result.chatgpt_block)

    summary = result.summary
    assert summary is not None
    if args.expected is None:
        return 0 if not summary.errors else 1
    return 0 if summary.commit_ready else 1


if __name__ == "__main__":
    sys.exit(main())
