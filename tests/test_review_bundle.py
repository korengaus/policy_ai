"""Phase 2 M8.6: tests for ``scripts/build_review_bundle.py``.

Every test exercises the pure ``build_bundle`` entry point with
synthetic ``git status --porcelain`` lines, a fixed ``latest_commit``,
and an injected ``diff_provider`` — no real git, no network, no
OpenAI, no secrets.

Covers the M8.6 spec cases A–P:
    A. bundle includes intended expected files
    B. bundle excludes .claude/settings.local.json from recommended git add
    C. bundle excludes reports/ files from recommended git add
    D. recommended git add command lists only safe expected files
    E. --include-diff includes diff only for safe expected files
    F. --include-diff does not include forbidden expected file diff
    G. forbidden expected file makes commit_ready=False
    H. --stdout prints the bundle without writing a file
    I. --json output has stable keys
    J. JSON output contains no secret-like values
    K. script never calls git add / commit / push
    L. Korean filenames remain readable
    M. manual --test-note values appear in the bundle
    N. latest commit field is included
    O. no --expected → safe summary, commit_ready=False
    P. reports/review_bundle_*.txt is excluded local-only when present
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.build_review_bundle as bundle  # noqa: E402
import scripts.operator_preflight as preflight  # noqa: E402


BUNDLE_SCRIPT = ROOT / "scripts" / "build_review_bundle.py"


def _no_diff(_path: str) -> str:
    return ""


def _fake_diff(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n+++ b/{path}\n+stub line"


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


class BundleBuilderTests(unittest.TestCase):
    def test_bundle_includes_intended_expected_files(self):
        result = bundle.build_bundle(
            expected=["docs/REVIEW_WORKFLOW.md", "scripts/foo.py"],
            status_lines=[
                " M docs/REVIEW_WORKFLOW.md",
                "?? scripts/foo.py",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000000Z",
        )
        self.assertIn("docs/REVIEW_WORKFLOW.md", result.bundle_text)
        self.assertIn("scripts/foo.py", result.bundle_text)
        self.assertIn(
            "git add docs/REVIEW_WORKFLOW.md scripts/foo.py",
            result.bundle_text,
        )
        # Header field is rendered.
        self.assertIn("policy_ai", result.bundle_text)
        self.assertIn("20260521T000000Z", result.bundle_text)

    def test_recommended_command_excludes_settings_local_json(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[
                " M docs/x.md",
                " M .claude/settings.local.json",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000001Z",
        )
        cmd = result.summary.recommended_git_add_command
        self.assertNotIn(".claude/settings.local.json", cmd)
        self.assertEqual(cmd, "git add docs/x.md")
        self.assertIn(
            ".claude/settings.local.json", result.summary.excluded_local_only_files,
        )

    def test_recommended_command_excludes_reports_files(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[
                " M docs/x.md",
                "?? reports/operational_check_20260521T010101Z.json",
                "?? reports/review_bundle_20260520T000000Z.txt",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000002Z",
        )
        cmd = result.summary.recommended_git_add_command
        self.assertNotIn("reports/", cmd)
        self.assertEqual(cmd, "git add docs/x.md")
        for p in (
            "reports/operational_check_20260521T010101Z.json",
            "reports/review_bundle_20260520T000000Z.txt",
        ):
            self.assertIn(p, result.summary.excluded_local_only_files)

    def test_recommended_command_lists_only_safe_expected_files(self):
        # Mix: safe expected + dangerous expected + unexpected safe change.
        result = bundle.build_bundle(
            expected=["docs/x.md", ".env.local"],
            status_lines=[
                " M docs/x.md",
                "?? .env.local",
                " M scripts/unrelated.py",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000003Z",
        )
        # .env.local explicitly listed → forbidden, removed from add command.
        self.assertEqual(
            result.summary.recommended_git_add_command, "git add docs/x.md",
        )
        self.assertIn(".env.local", result.summary.forbidden_files_present)
        # The unexpected safe change appears in unexpected_changed_files, NOT
        # in the recommended command.
        self.assertIn(
            "scripts/unrelated.py", result.summary.unexpected_changed_files,
        )
        self.assertNotIn(
            "scripts/unrelated.py", result.summary.recommended_git_add_command,
        )

    def test_forbidden_expected_blocks_commit_ready(self):
        result = bundle.build_bundle(
            expected=[".claude/settings.local.json"],
            status_lines=[" M .claude/settings.local.json"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000004Z",
        )
        self.assertFalse(result.summary.commit_ready)
        self.assertIn(
            ".claude/settings.local.json",
            result.summary.forbidden_files_present,
        )

    def test_include_diff_only_for_safe_expected_files(self):
        result = bundle.build_bundle(
            expected=["docs/x.md", "scripts/y.py"],
            status_lines=[
                " M docs/x.md",
                " M scripts/y.py",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000005Z",
            include_diff=True,
            diff_provider=_fake_diff,
        )
        self.assertIsNotNone(result.diff_section)
        self.assertIn("--- diff: docs/x.md ---", result.diff_section)
        self.assertIn("--- diff: scripts/y.py ---", result.diff_section)
        self.assertIn("diff --git a/docs/x.md b/docs/x.md", result.diff_section)
        # Diff lives inside the rendered bundle text.
        self.assertIn("[diff —", result.bundle_text)

    def test_include_diff_does_not_diff_forbidden_expected_file(self):
        captured: list = []

        def spy_diff(path: str) -> str:
            captured.append(path)
            return f"DIFF_FOR_{path}"

        result = bundle.build_bundle(
            expected=["docs/x.md", ".env"],
            status_lines=[" M docs/x.md", "?? .env"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000006Z",
            include_diff=True,
            diff_provider=spy_diff,
        )
        # The diff provider must never be asked about a forbidden path.
        self.assertNotIn(".env", captured)
        self.assertIn("docs/x.md", captured)
        # commit_ready is False because .env was explicitly listed.
        self.assertFalse(result.summary.commit_ready)
        # The rendered diff section never names .env content.
        self.assertNotIn("DIFF_FOR_.env", result.bundle_text)

    def test_include_diff_truncates_at_max_chars(self):
        long_diff = "x" * 1000

        def big_diff(_path: str) -> str:
            return long_diff

        result = bundle.build_bundle(
            expected=["docs/a.md", "docs/b.md", "docs/c.md"],
            status_lines=[" M docs/a.md", " M docs/b.md", " M docs/c.md"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000007Z",
            include_diff=True,
            max_diff_chars=500,
            diff_provider=big_diff,
        )
        self.assertTrue(result.diff_truncated)
        self.assertIn("truncated", result.bundle_text)

    def test_test_notes_appear_in_bundle(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000008Z",
            test_notes=[
                "python scripts/validate.py -> PASS",
                "python scripts/run_operational_checks.py --profile quick -> PASS",
            ],
        )
        self.assertIn("python scripts/validate.py -> PASS", result.bundle_text)
        self.assertIn(
            "python scripts/run_operational_checks.py --profile quick -> PASS",
            result.bundle_text,
        )

    def test_latest_commit_field_included(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
            latest_commit="abc1234 Add operator preflight safety helper",
            timestamp="20260521T000009Z",
        )
        self.assertIn(
            "abc1234 Add operator preflight safety helper",
            result.bundle_text,
        )
        self.assertIn(
            "abc1234 Add operator preflight safety helper",
            result.chatgpt_block,
        )

    def test_no_expected_produces_safe_summary_not_commit_ready(self):
        result = bundle.build_bundle(
            expected=None,
            status_lines=[" M docs/x.md", " M .claude/settings.local.json"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000010Z",
        )
        self.assertFalse(result.summary.commit_ready)
        # The summary still renders.
        self.assertIn("docs/x.md", result.bundle_text)
        # And the dangerous file shows up under excluded_local_only_files.
        self.assertIn(
            ".claude/settings.local.json",
            result.summary.excluded_local_only_files,
        )

    def test_review_bundle_txt_treated_as_excluded_local_only(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[
                " M docs/x.md",
                "?? reports/review_bundle_20260520T000000Z.txt",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000011Z",
        )
        self.assertIn(
            "reports/review_bundle_20260520T000000Z.txt",
            result.summary.excluded_local_only_files,
        )
        self.assertNotIn(
            "review_bundle",
            result.summary.recommended_git_add_command,
        )

    def test_review_bundle_txt_in_expected_is_forbidden(self):
        # Even if the operator points --expected at a bundle file living at
        # the repo root (which would slip past preflight's reports/ prefix
        # check), the bundle helper must refuse to stage it.
        result = bundle.build_bundle(
            expected=["docs/x.md", "review_bundle_20260520T000000Z.txt"],
            status_lines=[
                " M docs/x.md",
                "?? review_bundle_20260520T000000Z.txt",
            ],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000012Z",
        )
        self.assertFalse(result.summary.commit_ready)
        self.assertIn(
            "review_bundle_20260520T000000Z.txt",
            result.summary.forbidden_files_present,
        )
        self.assertNotIn(
            "review_bundle",
            result.summary.recommended_git_add_command,
        )

    def test_korean_filenames_remain_readable(self):
        result = bundle.build_bundle(
            expected=["docs/한국어_파일.md"],
            status_lines=[" M docs/한국어_파일.md"],
            latest_commit="deadbee1 한글 커밋 메시지",
            timestamp="20260521T000013Z",
        )
        self.assertIn("docs/한국어_파일.md", result.bundle_text)
        self.assertIn("한글 커밋 메시지", result.bundle_text)
        # Recommended command preserves the Korean filename.
        self.assertIn("docs/한국어_파일.md", result.summary.recommended_git_add_command)

    def test_milestone_label_appears_when_provided(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000014Z",
            milestone="Phase 2 M8.6",
        )
        self.assertIn("Phase 2 M8.6", result.bundle_text)
        self.assertIn("Phase 2 M8.6", result.chatgpt_block)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class JSONOutputTests(unittest.TestCase):
    EXPECTED_KEYS = {
        "commit_ready", "errors", "excluded_local_only_files",
        "expected_changed_files", "expected_files", "expected_missing_files",
        "forbidden_files_present", "latest_commit", "milestone",
        "output_path", "passed", "recommended_git_add_command",
        "test_notes", "unexpected_changed_files", "warnings",
    }

    def test_json_payload_has_stable_keys(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000020Z",
        )
        payload = json.loads(bundle.result_to_json(result))
        self.assertEqual(set(payload.keys()), self.EXPECTED_KEYS)

    def test_json_contains_no_secret_like_values(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000021Z",
            test_notes=["regular note without secrets"],
        )
        body = bundle.result_to_json(result)
        for needle in ("sk-", "OPENAI_API_KEY", "REVIEW_API_TOKEN"):
            self.assertNotIn(
                needle, body,
                msg=f"JSON output unexpectedly contained {needle!r}",
            )

    def test_json_payload_carries_milestone_and_test_notes(self):
        result = bundle.build_bundle(
            expected=["docs/x.md"],
            status_lines=[" M docs/x.md"],
            latest_commit="deadbee1 Test commit",
            timestamp="20260521T000022Z",
            milestone="Phase 2 M8.6",
            test_notes=["note one", "note two"],
        )
        payload = bundle.result_to_json_payload(result)
        self.assertEqual(payload["milestone"], "Phase 2 M8.6")
        self.assertEqual(payload["test_notes"], ["note one", "note two"])


# ---------------------------------------------------------------------------
# CLI behavior — --stdout / --json / --chatgpt-summary do not write files
# ---------------------------------------------------------------------------


class CLIBehaviorTests(unittest.TestCase):
    def setUp(self):
        # Patch the git helpers so main() never shells out during tests.
        self._orig_run_git_status = preflight.run_git_status
        self._orig_run_git_log = bundle._run_git_log_oneline
        self._orig_run_git_diff = bundle._run_git_diff_for_path
        preflight.run_git_status = lambda cwd=None: [
            " M docs/x.md",
            " M .claude/settings.local.json",
        ]
        bundle._run_git_log_oneline = lambda cwd=None: "abc1234 Test"
        bundle._run_git_diff_for_path = lambda path, cwd=None: (
            f"DIFF_FOR {path}"
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        (self.tmp_root / "reports").mkdir(exist_ok=True)

    def tearDown(self):
        preflight.run_git_status = self._orig_run_git_status
        bundle._run_git_log_oneline = self._orig_run_git_log
        bundle._run_git_diff_for_path = self._orig_run_git_diff
        self._tmp.cleanup()

    def _run_main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = bundle.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_stdout_prints_bundle_without_writing_a_file(self):
        files_before = set(os.listdir(self.tmp_root / "reports"))
        rc, stdout, _ = self._run_main([
            "--expected", "docs/x.md",
            "--stdout",
            "--repo-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("[review bundle] project: policy_ai", stdout)
        self.assertIn("git add docs/x.md", stdout)
        files_after = set(os.listdir(self.tmp_root / "reports"))
        self.assertEqual(files_before, files_after,
                         "--stdout must not create a report file")

    def test_default_mode_writes_file_under_reports(self):
        rc, stdout, _ = self._run_main([
            "--expected", "docs/x.md",
            "--repo-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 0)
        files = list((self.tmp_root / "reports").glob("review_bundle_*.txt"))
        self.assertEqual(len(files), 1, files)
        body = files[0].read_text(encoding="utf-8")
        self.assertIn("docs/x.md", body)
        self.assertIn("git add docs/x.md", body)
        # Path was printed.
        self.assertIn("review_bundle_", stdout)
        # Safety reminder printed too.
        self.assertIn(".claude/settings.local.json", stdout)

    def test_json_mode_prints_json_no_file(self):
        files_before = set(os.listdir(self.tmp_root / "reports"))
        rc, stdout, _ = self._run_main([
            "--expected", "docs/x.md",
            "--json",
            "--repo-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertIn("commit_ready", payload)
        self.assertIn("recommended_git_add_command", payload)
        self.assertNotIn("sk-", stdout)
        files_after = set(os.listdir(self.tmp_root / "reports"))
        self.assertEqual(files_before, files_after)

    def test_chatgpt_summary_mode_prints_summary_only(self):
        files_before = set(os.listdir(self.tmp_root / "reports"))
        rc, stdout, _ = self._run_main([
            "--expected", "docs/x.md",
            "--chatgpt-summary",
            "--repo-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 0)
        self.assertIn("chatgpt review block", stdout)
        self.assertIn("git add docs/x.md", stdout)
        # Header / preflight / safety reminder blocks are NOT in stdout
        # (chatgpt-summary mode prints only the block).
        self.assertNotIn("[safety reminder]", stdout)
        self.assertNotIn("[preflight summary]", stdout)
        files_after = set(os.listdir(self.tmp_root / "reports"))
        self.assertEqual(files_before, files_after)

    def test_mutually_exclusive_modes_rejected(self):
        rc, _, err = self._run_main([
            "--expected", "docs/x.md",
            "--stdout", "--json",
            "--repo-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", err)

    def test_default_mode_exit_code_when_commit_ready_false(self):
        # .claude/settings.local.json explicitly passed → forbidden, commit_ready
        # forced False → exit 1.
        rc, _, _ = self._run_main([
            "--expected", "docs/x.md", ".claude/settings.local.json",
            "--repo-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 1)

    def test_no_expected_returns_zero_when_no_errors(self):
        rc, _, _ = self._run_main([
            "--repo-root", str(self.tmp_root),
        ])
        # No --expected → no commit_ready test, no errors → exit 0.
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Static safety check: the script never invokes git add/commit/push
# ---------------------------------------------------------------------------


class ScriptSafetyTests(unittest.TestCase):
    def test_script_only_uses_read_only_git_subcommands(self):
        text = BUNDLE_SCRIPT.read_text(encoding="utf-8")
        # The bundle script's own subprocess invocations are limited to
        # `git log --oneline -1` and `git diff --no-color HEAD -- <path>`.
        # `git status --porcelain` lives in preflight and is reached via
        # preflight.run_git_status. Pin the two subcommand keywords.
        self.assertIn('"log", "--oneline", "-1"', text,
                      "expected `git log --oneline -1` invocation")
        self.assertIn('"diff", "--no-color", "HEAD"', text,
                      "expected `git diff --no-color HEAD` invocation")
        # The script must NEVER contain a list literal that pairs "git"
        # with a state-changing subcommand. Check line by line so prose
        # in docstrings (which talks about what the script does NOT do)
        # is excluded by the leading `*`/whitespace/quote shape.
        for lineno, line in enumerate(text.splitlines(), start=1):
            if '"git"' not in line:
                continue
            for forbidden in (
                '"add"', '"commit"', '"push"',
                '"reset"', '"checkout"', '"clean"',
            ):
                self.assertNotIn(
                    forbidden, line,
                    f"forbidden git verb {forbidden} appears alongside "
                    f"'git' on line {lineno}: {line!r}",
                )

    def test_main_does_not_attempt_to_stage_files(self):
        # Wrap subprocess.run at the module level and verify no git add /
        # commit / push (and friends) reaches it via the bundle script.
        import subprocess as real_subprocess
        calls: list = []
        original_run = real_subprocess.run

        def tracking_run(cmd, *args, **kwargs):
            calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
            return original_run(cmd, *args, **kwargs)

        real_subprocess.run = tracking_run  # type: ignore[assignment]
        try:
            bundle.main(["--stdout"])
        finally:
            real_subprocess.run = original_run  # type: ignore[assignment]
        for cmd in calls:
            for forbidden in ("add", "commit", "push", "reset",
                              "checkout", "clean"):
                self.assertNotIn(
                    forbidden, cmd,
                    f"bundle invoked git with {forbidden!r} in {cmd}",
                )

    def test_script_does_not_import_openai_or_verdict_modules(self):
        text = BUNDLE_SCRIPT.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "policy_decision", "policy_scoring",
            "verification_card", "render", "anthropic", "requests",
            "urllib.request", "httpx",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"build_review_bundle.py must not import {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main()
