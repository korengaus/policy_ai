"""Phase 2 M8.5: tests for ``scripts/operator_preflight.py``.

Every test exercises the script's pure helpers with synthetic
``git status --porcelain`` lines — no real git invocation, no network,
no OpenAI, no secrets.

Covers the M8.5 spec cases A–M:
    A. parse M modified file
    B. parse ?? untracked file
    C. classify intended safe files correctly
    D. exclude .claude/settings.local.json from recommended git add
    E. exclude reports/operational_check_*.json and .md
    F. exclude .env and .env.local
    G. dangerous file passed via --expected makes commit_ready=False
    H. normal safe expected files produce exact git add command
    I. unexpected safe changed file is surfaced
    J. JSON output contains stable keys and no secret-like values
    K. ChatGPT summary contains the recommended git add command + warnings
    L. paths with backslashes are normalized for classification
    M. the script does not call git add / commit / push
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.operator_preflight as preflight  # noqa: E402


PREFLIGHT_SCRIPT = ROOT / "scripts" / "operator_preflight.py"


# ---------------------------------------------------------------------------
# A / B — parsing
# ---------------------------------------------------------------------------


class ParseGitStatusTests(unittest.TestCase):
    def test_parses_modified_file(self):
        entries = preflight.parse_git_status_lines([" M docs/REVIEW_WORKFLOW.md"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "docs/REVIEW_WORKFLOW.md")
        self.assertEqual(entries[0].worktree_status, "M")
        self.assertFalse(entries[0].is_untracked)

    def test_parses_untracked_file(self):
        entries = preflight.parse_git_status_lines(["?? scripts/new_script.py"])
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_untracked)
        self.assertEqual(entries[0].path, "scripts/new_script.py")

    def test_parses_renamed_file(self):
        entries = preflight.parse_git_status_lines([
            "R  old/path.py -> new/path.py",
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "new/path.py")
        self.assertEqual(entries[0].original_path, "old/path.py")

    def test_skips_blank_and_malformed_lines(self):
        entries = preflight.parse_git_status_lines(["", "abc", " M docs/x.md"])
        # Only the well-formed " M docs/x.md" line should survive.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "docs/x.md")

    def test_parses_korean_path(self):
        entries = preflight.parse_git_status_lines([" M docs/한국어_파일.md"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "docs/한국어_파일.md")


# ---------------------------------------------------------------------------
# Forbidden / excluded path classification
# ---------------------------------------------------------------------------


class ForbiddenPathTests(unittest.TestCase):
    def test_settings_local_json_is_forbidden(self):
        self.assertTrue(preflight.is_forbidden_path(".claude/settings.local.json"))

    def test_reports_directory_is_forbidden(self):
        self.assertTrue(preflight.is_forbidden_path("reports/anything.json"))
        self.assertTrue(preflight.is_forbidden_path(
            "reports/operational_check_20260521T010101Z.json"
        ))
        self.assertTrue(preflight.is_forbidden_path(
            "reports/operational_check_20260521T010101Z.md"
        ))

    def test_root_operational_check_files_are_forbidden(self):
        self.assertTrue(preflight.is_forbidden_path(
            "operational_check_20260521T010101Z.json"
        ))
        self.assertTrue(preflight.is_forbidden_path(
            "operational_check_20260521T010101Z.md"
        ))

    def test_env_files_are_forbidden(self):
        for path in (".env", ".env.local", ".env.production",
                     "some/dir/.env", "some/dir/.env.staging"):
            self.assertTrue(
                preflight.is_forbidden_path(path),
                msg=f"{path!r} should be forbidden",
            )

    def test_cache_and_build_outputs_are_forbidden(self):
        for path in (
            "__pycache__/foo.pyc",
            "scripts/__pycache__/x.pyc",
            ".pytest_cache/v",
            ".mypy_cache/3.14",
            ".ruff_cache/abc",
            "node_modules/lodash/index.js",
            "dist/web/bundle.js",
            "build/lib/foo.py",
            "coverage/index.html",
            ".coverage",
            "build/foo.pyc",
        ):
            self.assertTrue(
                preflight.is_forbidden_path(path),
                msg=f"{path!r} should be forbidden",
            )

    def test_safe_paths_are_not_forbidden(self):
        for path in (
            "web/index.html",
            "docs/REVIEW_WORKFLOW.md",
            "scripts/operator_preflight.py",
            "tests/test_operator_preflight.py",
            "api_server.py",
        ):
            self.assertFalse(preflight.is_forbidden_path(path), msg=path)


class NormalizePathTests(unittest.TestCase):
    def test_backslash_normalized_to_forward_slash(self):
        self.assertEqual(
            preflight.normalize_path("docs\\REVIEW_WORKFLOW.md"),
            "docs/REVIEW_WORKFLOW.md",
        )

    def test_quoted_path_unquoted(self):
        self.assertEqual(
            preflight.normalize_path('"docs/file with space.md"'),
            "docs/file with space.md",
        )


# ---------------------------------------------------------------------------
# Classification: --expected interaction
# ---------------------------------------------------------------------------


class ClassifyPathsTests(unittest.TestCase):
    def test_safe_expected_files_produce_recommended_command(self):
        lines = [
            " M docs/REVIEW_WORKFLOW.md",
            "?? scripts/new_script.py",
        ]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries,
            expected_files=["docs/REVIEW_WORKFLOW.md", "scripts/new_script.py"],
        )
        self.assertTrue(summary.commit_ready)
        self.assertEqual(
            summary.expected_changed_files,
            ["docs/REVIEW_WORKFLOW.md", "scripts/new_script.py"],
        )
        self.assertEqual(summary.expected_missing_files, [])
        self.assertEqual(summary.unexpected_changed_files, [])
        # Recommended command lists exactly those two files in order.
        self.assertEqual(
            summary.recommended_git_add_command,
            "git add docs/REVIEW_WORKFLOW.md scripts/new_script.py",
        )

    def test_settings_local_json_excluded_even_when_modified(self):
        lines = [
            " M .claude/settings.local.json",
            " M docs/REVIEW_WORKFLOW.md",
        ]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries, expected_files=["docs/REVIEW_WORKFLOW.md"],
        )
        # Recommended command excludes the settings file even though it is
        # modified locally.
        self.assertEqual(
            summary.recommended_git_add_command,
            "git add docs/REVIEW_WORKFLOW.md",
        )
        self.assertIn(".claude/settings.local.json",
                      summary.excluded_local_only_files)
        # commit_ready stays True — the settings file is local-only noise,
        # not a hard block.
        self.assertTrue(summary.commit_ready)
        # A warning calls it out.
        joined = " ".join(summary.warnings)
        self.assertIn(".claude/settings.local.json", joined)

    def test_reports_outputs_excluded(self):
        lines = [
            "?? reports/operational_check_20260521T010101Z.json",
            "?? reports/operational_check_20260521T010101Z.md",
            " M docs/VALIDATION.md",
        ]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries, expected_files=["docs/VALIDATION.md"],
        )
        self.assertEqual(
            summary.recommended_git_add_command, "git add docs/VALIDATION.md",
        )
        for path in (
            "reports/operational_check_20260521T010101Z.json",
            "reports/operational_check_20260521T010101Z.md",
        ):
            self.assertIn(path, summary.excluded_local_only_files)
        joined = " ".join(summary.warnings)
        self.assertIn("reports/", joined)
        self.assertTrue(summary.commit_ready)

    def test_env_files_excluded(self):
        lines = [
            "?? .env",
            "?? .env.local",
            " M scripts/operator_preflight.py",
        ]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries, expected_files=["scripts/operator_preflight.py"],
        )
        for path in (".env", ".env.local"):
            self.assertIn(path, summary.excluded_local_only_files)
        self.assertNotIn(".env", summary.recommended_git_add_command)
        self.assertNotIn(".env.local", summary.recommended_git_add_command)
        self.assertTrue(summary.commit_ready)

    def test_dangerous_file_in_expected_blocks_commit_ready(self):
        lines = [
            " M .claude/settings.local.json",
            " M docs/x.md",
        ]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries,
            expected_files=[".claude/settings.local.json", "docs/x.md"],
        )
        self.assertFalse(summary.commit_ready)
        self.assertIn(".claude/settings.local.json",
                      summary.forbidden_files_present)
        # Recommended command must NOT include the dangerous file.
        self.assertEqual(summary.recommended_git_add_command,
                         "git add docs/x.md")
        joined = " ".join(summary.errors)
        self.assertIn(".claude/settings.local.json", joined)
        self.assertFalse(summary.passed)

    def test_unexpected_safe_changed_file_surfaced(self):
        lines = [
            " M docs/REVIEW_WORKFLOW.md",
            " M scripts/unrelated.py",
        ]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries, expected_files=["docs/REVIEW_WORKFLOW.md"],
        )
        self.assertIn("scripts/unrelated.py", summary.unexpected_changed_files)
        # The unexpected safe file does NOT appear in the recommended command.
        self.assertEqual(
            summary.recommended_git_add_command,
            "git add docs/REVIEW_WORKFLOW.md",
        )
        # commit_ready remains True — the operator is welcome to ignore the
        # unexpected file.
        self.assertTrue(summary.commit_ready)
        joined = " ".join(summary.warnings)
        self.assertIn("scripts/unrelated.py", joined)

    def test_expected_missing_blocks_commit_ready(self):
        lines = [" M docs/x.md"]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries,
            expected_files=["docs/x.md", "docs/missing.md"],
        )
        self.assertFalse(summary.commit_ready)
        self.assertIn("docs/missing.md", summary.expected_missing_files)

    def test_backslash_paths_in_expected_normalized(self):
        lines = [" M docs/REVIEW_WORKFLOW.md"]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(
            entries, expected_files=[r"docs\REVIEW_WORKFLOW.md"],
        )
        self.assertTrue(summary.commit_ready)
        self.assertEqual(
            summary.recommended_git_add_command,
            "git add docs/REVIEW_WORKFLOW.md",
        )

    def test_basic_mode_never_marks_commit_ready(self):
        # Without --expected, commit_ready stays False — operator must
        # explicitly whitelist files.
        lines = [" M docs/x.md"]
        entries = preflight.parse_git_status_lines(lines)
        summary = preflight.classify_paths(entries, expected_files=None)
        self.assertFalse(summary.commit_ready)


class ShellQuoteTests(unittest.TestCase):
    def test_plain_path_unquoted(self):
        self.assertEqual(preflight._shell_quote("docs/x.md"), "docs/x.md")

    def test_path_with_space_is_quoted(self):
        self.assertEqual(
            preflight._shell_quote("docs/file with space.md"),
            '"docs/file with space.md"',
        )

    def test_path_with_metacharacters_is_quoted(self):
        self.assertEqual(
            preflight._shell_quote("foo$bar.md"),
            '"foo$bar.md"',
        )


# ---------------------------------------------------------------------------
# Formatters: JSON + ChatGPT summary
# ---------------------------------------------------------------------------


class JSONFormatterTests(unittest.TestCase):
    EXPECTED_KEYS = {
        "changed_files", "commit_ready", "errors",
        "excluded_local_only_files", "expected_changed_files",
        "expected_files", "expected_missing_files", "forbidden_files_present",
        "passed", "recommended_git_add_command", "unexpected_changed_files",
        "untracked_files", "warnings",
    }

    def test_json_has_stable_keys(self):
        summary = preflight.run_preflight(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
        )
        payload = json.loads(preflight.summary_to_json(summary))
        self.assertEqual(set(payload.keys()), self.EXPECTED_KEYS)

    def test_json_does_not_include_secret_like_values(self):
        summary = preflight.run_preflight(
            expected=["docs/x.md"], status_lines=[" M docs/x.md"],
        )
        body = preflight.summary_to_json(summary)
        # No API key / token literals embedded in the JSON.
        self.assertNotIn("sk-", body)
        self.assertNotIn("OPENAI_API_KEY", body)
        self.assertNotIn("REVIEW_API_TOKEN", body)


class ChatGPTSummaryTests(unittest.TestCase):
    def test_summary_includes_recommended_command_and_excluded_warning(self):
        summary = preflight.run_preflight(
            expected=["docs/REVIEW_WORKFLOW.md"],
            status_lines=[
                " M docs/REVIEW_WORKFLOW.md",
                " M .claude/settings.local.json",
                "?? reports/operational_check_20260521T010101Z.json",
            ],
        )
        text = preflight.format_chatgpt_summary(summary)
        self.assertIn(
            "git add docs/REVIEW_WORKFLOW.md", text,
        )
        self.assertIn(".claude/settings.local.json", text)
        self.assertIn("reports/", text)
        self.assertIn("commit_ready: True", text)
        # No secret-like content.
        self.assertNotIn("sk-", text)
        self.assertNotIn("OPENAI_API_KEY", text)

    def test_summary_handles_basic_mode_gracefully(self):
        summary = preflight.run_preflight(
            expected=None, status_lines=[" M docs/x.md"],
        )
        text = preflight.format_chatgpt_summary(summary)
        # No exception, and the "nothing safe to add yet" copy appears.
        self.assertIn("nothing safe to add yet", text)


# ---------------------------------------------------------------------------
# Static safety check: script never invokes git add / commit / push
# ---------------------------------------------------------------------------


class ScriptSafetyTests(unittest.TestCase):
    def test_script_has_exactly_one_subprocess_invocation(self):
        """Only one ``subprocess.run`` call may appear in the script, and the
        cmd it builds must be a read-only ``git status`` call.

        We check shape statically. The third test below also wraps
        ``subprocess.run`` at runtime and verifies no ``git add``/``commit``/
        ``push`` ever reaches it — that's the real safety check.
        """
        text = PREFLIGHT_SCRIPT.read_text(encoding="utf-8")
        # Count subprocess.run( occurrences — one and only one is allowed.
        self.assertEqual(
            text.count("subprocess.run("), 1,
            "operator_preflight.py must contain exactly one subprocess.run call",
        )
        # The single git command list must contain "status" and "--porcelain"
        # and must not contain any state-changing verb.
        self.assertIn('["git", "-c", "core.quotePath=false", "status", "--porcelain"]',
                      text,
                      "the documented read-only git status command shape was not found")
        # The display helper builds a string-only "git add ..." (returned to
        # the operator). That literal does NOT appear as subprocess arguments
        # — verified at runtime by test_main_does_not_attempt_to_stage_files.

    def test_script_does_not_import_openai_or_verdict_modules(self):
        """Imports (not docstrings) must not reference forbidden modules.

        The docstring mentions ``OPENAI_API_KEY`` and ``render.yaml`` to
        explain what the script does **not** do — those literals in prose
        are fine. We check the actual import lines instead.
        """
        text = PREFLIGHT_SCRIPT.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined_imports = "\n".join(import_lines)
        for forbidden in (
            "openai",
            "policy_decision",
            "policy_scoring",
            "verification_card",
            "render",
            "anthropic",
        ):
            self.assertNotIn(
                forbidden, joined_imports,
                f"operator_preflight.py must not import {forbidden!r}",
            )

    def test_main_does_not_attempt_to_stage_files(self):
        # End-to-end: run main() with synthetic status (skip git via the
        # repo_root pointing nowhere meaningful — basic mode never errors).
        # Mock subprocess so a regression that *does* call git add would fail.
        import subprocess as real_subprocess
        calls: list = []
        original_run = real_subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return original_run(cmd, *args, **kwargs)

        real_subprocess.run = tracking_run  # type: ignore[assignment]
        try:
            preflight.main([])
        finally:
            real_subprocess.run = original_run  # type: ignore[assignment]
        for cmd in calls:
            self.assertNotIn("add", cmd,
                             f"preflight invoked git with 'add' in {cmd}")
            self.assertNotIn("commit", cmd,
                             f"preflight invoked git with 'commit' in {cmd}")
            self.assertNotIn("push", cmd,
                             f"preflight invoked git with 'push' in {cmd}")


if __name__ == "__main__":
    unittest.main()
