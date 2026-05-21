"""Phase 2 M8.5: operator preflight + commit-safety helper.

A small *local* operator tool that reads ``git status`` and helps the
operator review pending changes before composing a ``git add`` command.
It never stages, commits, pushes, or modifies any file. It never calls
external services, never reads ``OPENAI_API_KEY``, and never modifies
Render env.

Goals:
    * Avoid ``git add .`` by giving the operator an explicit, narrow
      ``git add`` command that lists only the files they intended.
    * Surface dangerous files (``.claude/settings.local.json``,
      ``reports/`` outputs, ``.env`` files, build caches) so they
      can be excluded from the recommended command.
    * Tell the operator whether the current change set matches the
      ``--expected`` whitelist, and which expected files are still
      missing.
    * Produce a copy-paste ChatGPT review summary on request.

Usage:

    python scripts/operator_preflight.py
    python scripts/operator_preflight.py --expected web/index.html docs/REVIEW_WORKFLOW.md
    python scripts/operator_preflight.py --expected ... --chatgpt-summary
    python scripts/operator_preflight.py --expected ... --json

Exit codes:
    0 — clean preflight (or basic mode with no --expected list)
    1 — commit_ready=False with --expected (something to fix)
    2 — bad CLI usage / git invocation failed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# Excluded / forbidden patterns
# ---------------------------------------------------------------------------
#
# Files matching any of these patterns are excluded from the recommended
# ``git add`` command. If the operator explicitly lists one in ``--expected``
# the preflight refuses to mark the commit as ready.
#
# Patterns are normalized to forward slashes before matching. Suffix matches
# (``ENDSWITH``) are applied to the path tail; substring matches (``CONTAINS``)
# anywhere in the path; prefix matches (``PREFIX``) only at the start.

_EXACT_FORBIDDEN: tuple = (
    ".claude/settings.local.json",
    ".coverage",
)

_PREFIX_FORBIDDEN: tuple = (
    "reports/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "coverage/",
    "dist/",
    "build/",
)

_CONTAINS_FORBIDDEN: tuple = (
    "/__pycache__/",
    "/node_modules/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
    "/coverage/",
)

_SUFFIX_FORBIDDEN: tuple = (
    ".pyc",
)

# Regex-style operational-check report filenames live under reports/ (already
# matched by _PREFIX_FORBIDDEN) and also under operational_check_*.json/.md
# at the repo root in older copies. Keep an explicit regex for the bare names.
_OP_REPORT_RE = re.compile(
    r"^(?:|reports/)operational_check_[^/]+\.(?:json|md)$",
)

# .env and .env.* (.env.local, .env.production, …) anywhere in the tree.
_ENV_NAME_RE = re.compile(r"(?:^|/)\.env(?:\.[^/]+)?$")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitStatusEntry:
    """One row from ``git status --porcelain``.

    ``index_status`` and ``worktree_status`` mirror the two status chars
    git prints in columns 0–1. ``path`` is the (rename-resolved) target
    path the operator would stage. ``original_path`` is set for renames /
    copies — kept for debugging only, not used by classification.
    """
    index_status: str
    worktree_status: str
    path: str
    original_path: Optional[str]
    is_untracked: bool


@dataclass
class PreflightSummary:
    """Stable shape consumed by the human / ChatGPT / JSON formatters."""
    changed_files: List[str] = field(default_factory=list)
    untracked_files: List[str] = field(default_factory=list)
    expected_files: List[str] = field(default_factory=list)
    expected_changed_files: List[str] = field(default_factory=list)
    expected_missing_files: List[str] = field(default_factory=list)
    unexpected_changed_files: List[str] = field(default_factory=list)
    excluded_local_only_files: List[str] = field(default_factory=list)
    forbidden_files_present: List[str] = field(default_factory=list)
    recommended_git_add_command: str = ""
    commit_ready: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    passed: bool = False


# ---------------------------------------------------------------------------
# Path classification — pure helpers, exercisable without git
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    """Convert backslashes to forward slashes; strip surrounding whitespace.

    Quoted paths (``"foo bar.md"``) — produced by git when ``core.quotePath``
    surfaces special characters — are unquoted so the path matches the
    operator's typed form.
    """
    if path is None:
        return ""
    s = str(path).strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.replace("\\", "/")


def is_forbidden_path(path: str) -> bool:
    """True when ``path`` matches any excluded pattern (local-only, secrets,
    caches, build outputs, gitignored reports). Forward-slash-normalized."""
    p = normalize_path(path)
    if not p:
        return False
    if p in _EXACT_FORBIDDEN:
        return True
    for prefix in _PREFIX_FORBIDDEN:
        if p.startswith(prefix):
            return True
    for needle in _CONTAINS_FORBIDDEN:
        if needle in p:
            return True
    for suffix in _SUFFIX_FORBIDDEN:
        if p.endswith(suffix):
            return True
    if _ENV_NAME_RE.search(p):
        return True
    if _OP_REPORT_RE.match(p):
        return True
    return False


def parse_git_status_lines(lines: Sequence[str]) -> List[GitStatusEntry]:
    """Parse ``git status --porcelain`` output (one entry per line).

    Tolerant of trailing newlines, blank lines, and rename arrows. Never
    raises on malformed input — malformed lines are skipped silently so
    a corrupted git environment doesn't block the operator's review.
    """
    out: List[GitStatusEntry] = []
    for raw in lines:
        if raw is None:
            continue
        line = raw.rstrip("\r\n")
        if not line:
            continue
        # Each porcelain row is "XY<space>path". Need at least 3 chars + path.
        if len(line) < 4:
            continue
        index_status = line[0]
        worktree_status = line[1]
        # Column 2 is a literal space separator. Skip if not.
        if line[2] != " ":
            continue
        body = line[3:]
        original_path: Optional[str] = None
        path: str
        if " -> " in body:
            head, _, tail = body.partition(" -> ")
            original_path = normalize_path(head)
            path = normalize_path(tail)
        else:
            path = normalize_path(body)
        is_untracked = (index_status == "?" and worktree_status == "?")
        out.append(
            GitStatusEntry(
                index_status=index_status,
                worktree_status=worktree_status,
                path=path,
                original_path=original_path,
                is_untracked=is_untracked,
            )
        )
    return out


def _shell_quote(path: str) -> str:
    """Quote ``path`` for inclusion in a shell command line.

    Conservative quoting: paths with whitespace or shell metacharacters get
    wrapped in double quotes. Plain paths are passed through unchanged so
    the recommended command stays readable.
    """
    if not path:
        return '""'
    if re.search(r"[\s\"'`$\\&|;<>(){}\[\]*?]", path):
        escaped = path.replace('"', r"\"")
        return f'"{escaped}"'
    return path


def build_recommended_git_add(expected_safe_files: Sequence[str]) -> str:
    """Build the ``git add ...`` command line for the safe expected files.

    Returns an empty string when no safe files are provided — the caller
    surfaces that as a "nothing to add yet" warning rather than emitting
    a bare ``git add`` (which would fail noisily for the operator).
    """
    cleaned = [normalize_path(p) for p in expected_safe_files if p]
    if not cleaned:
        return ""
    parts = ["git", "add"]
    parts.extend(_shell_quote(p) for p in cleaned)
    return " ".join(parts)


def classify_paths(
    entries: Sequence[GitStatusEntry],
    expected_files: Optional[Sequence[str]] = None,
) -> PreflightSummary:
    """Classify a parsed git status against an optional ``--expected`` list.

    The summary fields are deterministic — same input always produces the
    same output — so tests can pin behavior without running git.
    """
    expected_list = [normalize_path(p) for p in (expected_files or []) if p]
    expected_set = set(expected_list)

    changed_files: List[str] = []
    untracked_files: List[str] = []
    all_paths: List[str] = []
    for entry in entries:
        if entry.is_untracked:
            untracked_files.append(entry.path)
        else:
            changed_files.append(entry.path)
        all_paths.append(entry.path)
    all_paths_set = set(all_paths)

    excluded_local_only_files: List[str] = []
    unexpected_changed_files: List[str] = []
    for path in all_paths:
        if is_forbidden_path(path):
            if path not in expected_set:
                excluded_local_only_files.append(path)
        else:
            if expected_files is not None and path not in expected_set:
                unexpected_changed_files.append(path)

    expected_changed_files: List[str] = []
    expected_missing_files: List[str] = []
    forbidden_files_present: List[str] = []
    for path in expected_list:
        if is_forbidden_path(path):
            # Dangerous file explicitly listed by the operator. Refuse to
            # include it in the recommended command and surface a hard error.
            forbidden_files_present.append(path)
            continue
        if path in all_paths_set:
            expected_changed_files.append(path)
        else:
            expected_missing_files.append(path)

    warnings: List[str] = []
    errors: List[str] = []

    if excluded_local_only_files:
        if ".claude/settings.local.json" in excluded_local_only_files:
            warnings.append(
                ".claude/settings.local.json is modified locally — excluded "
                "from the recommended git add command (operator-specific file)."
            )
        reports_present = [p for p in excluded_local_only_files
                           if p.startswith("reports/") or _OP_REPORT_RE.match(p)]
        if reports_present:
            warnings.append(
                f"{len(reports_present)} reports/ output(s) present — "
                "always excluded from commits (gitignored)."
            )
        other_excluded = [
            p for p in excluded_local_only_files
            if p != ".claude/settings.local.json"
            and not (p.startswith("reports/") or _OP_REPORT_RE.match(p))
        ]
        if other_excluded:
            warnings.append(
                f"{len(other_excluded)} other excluded file(s) present "
                "(env / cache / build outputs) — excluded from recommended command."
            )

    for forbidden in forbidden_files_present:
        errors.append(
            f"--expected listed a dangerous file: {forbidden!r}. "
            "It is excluded from the recommended git add command. "
            "Remove it from --expected before staging."
        )

    if expected_missing_files:
        joined = ", ".join(expected_missing_files)
        warnings.append(
            f"{len(expected_missing_files)} expected file(s) not currently "
            f"changed: {joined}. Re-check after saving / editing those files."
        )

    if unexpected_changed_files:
        joined = ", ".join(unexpected_changed_files)
        warnings.append(
            f"{len(unexpected_changed_files)} safe file(s) changed but not "
            f"listed in --expected: {joined}. Review and decide whether to add."
        )

    recommended = build_recommended_git_add(expected_changed_files)

    # commit_ready is only meaningful when --expected was supplied. In basic
    # mode (no --expected) we report False; the operator should rerun with
    # the intended file list before staging.
    if expected_files is None:
        commit_ready = False
    else:
        commit_ready = (
            not errors
            and not forbidden_files_present
            and not expected_missing_files
            and bool(expected_changed_files)
        )

    summary = PreflightSummary(
        changed_files=changed_files,
        untracked_files=untracked_files,
        expected_files=list(expected_list),
        expected_changed_files=expected_changed_files,
        expected_missing_files=expected_missing_files,
        unexpected_changed_files=unexpected_changed_files,
        excluded_local_only_files=excluded_local_only_files,
        forbidden_files_present=forbidden_files_present,
        recommended_git_add_command=recommended,
        commit_ready=commit_ready,
        warnings=warnings,
        errors=errors,
        passed=(not errors and not forbidden_files_present),
    )
    return summary


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _format_list(label: str, items: Sequence[str], *, indent: str = "  ") -> List[str]:
    if not items:
        return [f"{label}: (none)"]
    lines = [f"{label}:"]
    for item in items:
        lines.append(f"{indent}- {item}")
    return lines


def format_human_summary(summary: PreflightSummary) -> str:
    """Multi-line operator-readable report — what the basic CLI mode prints."""
    lines: List[str] = []
    lines.append("[preflight] operator preflight summary")
    lines.extend(_format_list("changed files", summary.changed_files))
    lines.extend(_format_list("untracked files", summary.untracked_files))
    lines.extend(_format_list("excluded local-only files",
                              summary.excluded_local_only_files))
    if summary.expected_files:
        lines.extend(_format_list("expected files", summary.expected_files))
        lines.extend(_format_list("expected changed",
                                  summary.expected_changed_files))
        lines.extend(_format_list("expected missing",
                                  summary.expected_missing_files))
        lines.extend(_format_list("unexpected changed",
                                  summary.unexpected_changed_files))
        lines.extend(_format_list("forbidden in --expected",
                                  summary.forbidden_files_present))
    if summary.warnings:
        lines.append("warnings:")
        for w in summary.warnings:
            lines.append(f"  - {w}")
    if summary.errors:
        lines.append("errors:")
        for e in summary.errors:
            lines.append(f"  - {e}")
    if summary.expected_files:
        lines.append(f"commit_ready: {summary.commit_ready}")
        lines.append("recommended git add command:")
        if summary.recommended_git_add_command:
            lines.append(f"  {summary.recommended_git_add_command}")
        else:
            lines.append("  (none — nothing safe to add yet)")
        if summary.commit_ready:
            lines.append("next: review the diff, then run the recommended command.")
        else:
            lines.append("next: address warnings/errors above; do NOT use git add .")
    else:
        lines.append(
            "next: re-run with --expected <paths...> to get a recommended "
            "git add command and a commit_ready flag."
        )
    lines.append(
        "note: this tool never stages, commits, or pushes. Operator review "
        "still required."
    )
    return "\n".join(lines)


def format_chatgpt_summary(summary: PreflightSummary) -> str:
    """Short copy-paste-friendly block intended for pasting into ChatGPT.

    Intentionally compact — no diffs, no stdout dumps, no secrets.
    """
    lines: List[str] = []
    lines.append("Operator preflight summary (M8.5):")
    lines.append("")
    lines.append("Files intended for commit:")
    if summary.expected_changed_files:
        for path in summary.expected_changed_files:
            lines.append(f"  - {path}")
    else:
        lines.append("  (none — re-run with --expected ...)")
    lines.append("")
    lines.append("Excluded local-only files:")
    if summary.excluded_local_only_files:
        for path in summary.excluded_local_only_files:
            lines.append(f"  - {path}")
    else:
        lines.append("  (none)")
    lines.append("")
    if summary.unexpected_changed_files:
        lines.append("Unexpected changed files (safe, not in --expected):")
        for path in summary.unexpected_changed_files:
            lines.append(f"  - {path}")
        lines.append("")
    if summary.expected_missing_files:
        lines.append("Expected files not currently changed:")
        for path in summary.expected_missing_files:
            lines.append(f"  - {path}")
        lines.append("")
    if summary.forbidden_files_present:
        lines.append("DANGEROUS files explicitly listed in --expected:")
        for path in summary.forbidden_files_present:
            lines.append(f"  - {path}")
        lines.append("")
    if ".claude/settings.local.json" in summary.excluded_local_only_files:
        lines.append(
            "Warning: .claude/settings.local.json is modified locally — "
            "do NOT stage; this is operator-specific local config."
        )
    reports_present = [
        p for p in summary.excluded_local_only_files
        if p.startswith("reports/") or _OP_REPORT_RE.match(p)
    ]
    if reports_present:
        lines.append(
            f"Warning: {len(reports_present)} reports/ output(s) present — "
            "always excluded; gitignored generated artifacts."
        )
    lines.append("")
    lines.append("Recommended git add command:")
    if summary.recommended_git_add_command:
        lines.append(f"  {summary.recommended_git_add_command}")
    else:
        lines.append("  (none — nothing safe to add yet)")
    lines.append("")
    lines.append(f"commit_ready: {summary.commit_ready}")
    lines.append(
        "Reminder: this preflight does not stage or commit anything; "
        "operator review still required before running git add."
    )
    return "\n".join(lines)


def summary_to_json(summary: PreflightSummary) -> str:
    """JSON representation with stable key order."""
    data = asdict(summary)
    # Stable, sorted-key serialization makes diffing report outputs easier.
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Git invocation (single side-effecting entry point)
# ---------------------------------------------------------------------------


def run_git_status(cwd: Optional[Path] = None) -> List[str]:
    """Return ``git status --porcelain`` lines, never raises on operator-side
    git failures — returns an empty list instead so the preflight still
    prints something useful.

    ``core.quotePath=false`` is passed inline so Korean / non-ASCII paths
    come through verbatim instead of as ``\\xxx`` octal escapes.
    """
    cmd = ["git", "-c", "core.quotePath=false", "status", "--porcelain"]
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
        return []
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return (completed.stdout or "").splitlines()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local operator preflight + commit-safety helper. Reads git "
            "status, classifies changed files against an --expected list, "
            "and prints a recommended git add command (never runs git add)."
        ),
    )
    parser.add_argument(
        "--expected", nargs="*", default=None,
        help=(
            "Whitelist of files the operator intends to stage. Repeat paths "
            "separated by spaces. Without this flag the script only prints "
            "the current change summary."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the summary as JSON. Suppresses the human report.",
    )
    parser.add_argument(
        "--chatgpt-summary", action="store_true",
        help=(
            "Print a short copy-paste-friendly summary intended for ChatGPT "
            "review. Suppresses the human report; combine with --expected."
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help=(
            "Repository root (defaults to the directory containing this "
            "script's parent). Mostly useful for tests."
        ),
    )
    return parser


def run_preflight(expected: Optional[Sequence[str]], *,
                  status_lines: Optional[Sequence[str]] = None,
                  cwd: Optional[Path] = None) -> PreflightSummary:
    """Pure-ish entry point: read git, classify, return the summary.

    Tests pass ``status_lines=[...]`` to skip the subprocess entirely.
    """
    if status_lines is None:
        status_lines = run_git_status(cwd=cwd)
    entries = parse_git_status_lines(status_lines)
    return classify_paths(entries, expected_files=expected)


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.json and args.chatgpt_summary:
        print(
            "[preflight] --json and --chatgpt-summary are mutually exclusive.",
            file=sys.stderr,
        )
        return 2

    repo_root = args.repo_root or ROOT
    summary = run_preflight(args.expected, cwd=repo_root)

    if args.json:
        print(summary_to_json(summary))
    elif args.chatgpt_summary:
        print(format_chatgpt_summary(summary))
    else:
        print(format_human_summary(summary))

    if args.expected is None:
        # Basic mode never claims success or failure — just shows status.
        return 0
    return 0 if summary.commit_ready else 1


if __name__ == "__main__":
    sys.exit(main())
