"""Phase 2 M9.3: tests for ``scripts/prepare_review_ui_local_demo.py``.

Every test exercises the helper offline. No real server is spawned;
``--verify`` mode uses FastAPI TestClient. No Render call, no OpenAI
call, no real ``REVIEW_API_TOKEN`` is read from the environment.

Covers the M9.3 spec items A–P:
    A. demo DB defaults to a path under reports/
    B. unsafe --db-path outside reports/ is rejected
    C. printed PowerShell commands carry the dummy token + DB path
    D. helper never reads REVIEW_API_TOKEN from the environment
    E. helper never reads OPENAI_API_KEY
    F. helper never calls Render / external network (no requests/httpx
       import; no fetch outside FastAPI TestClient for --verify)
    G. seeds at least one review task
    H. seeded task carries conservative Korean wording
    I. audit-packet endpoint works in --verify mode
    J. audit packet's safety_contract.publication is False
    K. audit packet response carries no token literal
    L. --json output has stable keys
    M. --reset replaces an existing demo DB
    N. Korean filenames / claim text remain readable
    O. helper never invokes any git verb
    P. reports/ outputs are not recommended for commit
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

import scripts.prepare_review_ui_local_demo as demo  # noqa: E402


SCRIPT_PATH = ROOT / "scripts" / "prepare_review_ui_local_demo.py"
LAUNCHER_PATH = ROOT / "scripts" / "serve_review_ui_local_demo.py"


def _unique_temp_db_under_reports() -> Path:
    """Return a unique demo DB path under reports/ for a single test."""
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    fh = tempfile.NamedTemporaryFile(
        prefix="test_demo_", suffix=".sqlite",
        dir=reports, delete=False,
    )
    fh.close()
    p = Path(fh.name)
    p.unlink()  # we want the path; --reset / first run will create it.
    return p


class _DemoTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_paths: list = []

    def tearDown(self):
        for p in self._tmp_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    def _new_demo_db(self) -> Path:
        p = _unique_temp_db_under_reports()
        self._tmp_paths.append(p)
        return p


# ---------------------------------------------------------------------------
# A — default path
# ---------------------------------------------------------------------------


class DefaultPathTests(unittest.TestCase):
    def test_default_db_path_is_under_reports(self):
        resolved = demo._normalize_db_path(None)
        self.assertTrue(
            demo._path_is_under_reports(resolved),
            f"default demo DB must live under reports/: {resolved}",
        )
        # And the filename matches the documented constant.
        self.assertEqual(resolved.name, demo.DEMO_DB_FILENAME)


# ---------------------------------------------------------------------------
# B — unsafe path is rejected
# ---------------------------------------------------------------------------


class UnsafePathTests(unittest.TestCase):
    def test_path_outside_reports_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            unsafe = Path(td) / "unsafe.sqlite"
            result = demo.prepare_demo(db_path=unsafe.resolve())
        self.assertFalse(result.passed)
        joined = " ".join(result.errors)
        self.assertIn("outside reports/", joined)

    def test_main_rejects_unsafe_path_with_exit_2(self):
        out = io.StringIO()
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            unsafe = str(Path(td) / "x.sqlite")
            with redirect_stdout(out), redirect_stderr(err):
                rc = demo.main(["--db-path", unsafe])
        self.assertEqual(rc, 2, msg=err.getvalue())


# ---------------------------------------------------------------------------
# C — runbook output / PowerShell commands carry dummy token + DB path
# ---------------------------------------------------------------------------


class PowershellCommandsTests(_DemoTestBase):
    def test_powershell_commands_carry_dummy_token_and_db_path(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(
            db_path=target, token="my-dummy-tag", reset=True,
        )
        self.assertTrue(result.passed, msg=result.errors)
        joined = " ".join(result.powershell_commands)
        self.assertIn('$env:REVIEW_API_ENABLED = "true"', joined)
        self.assertIn('$env:REVIEW_API_TOKEN = "my-dummy-tag"', joined)
        self.assertIn(str(target), joined)
        # The serve launcher is the path the operator should run.
        self.assertIn("serve_review_ui_local_demo.py", joined)

    def test_runbook_includes_admin_step_and_audit_packet_button(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(db_path=target, reset=True)
        body = io.StringIO()
        with redirect_stdout(body):
            demo._print_runbook(result)
        text = body.getvalue()
        # Conservative Korean wording is present.
        self.assertIn("내부 검수 도구 열기 (관리자 전용)", text)
        self.assertIn("감사 패킷 보기", text)
        self.assertIn("감사 패킷 복사", text)
        self.assertIn("토큰 적용", text)
        self.assertIn("큐 새로고침", text)


# ---------------------------------------------------------------------------
# D — never reads REVIEW_API_TOKEN
# ---------------------------------------------------------------------------


class TokenSafetyTests(_DemoTestBase):
    def test_helper_does_not_read_review_api_token_from_env(self):
        # Seed REVIEW_API_TOKEN with a sentinel value; the helper must
        # not echo it, must not let it leak into output, and must use
        # its own dummy.
        sentinel = "DO-NOT-LEAK-REAL-TOKEN-VALUE"
        original = os.environ.get("REVIEW_API_TOKEN")
        os.environ["REVIEW_API_TOKEN"] = sentinel
        try:
            target = self._new_demo_db()
            result = demo.prepare_demo(
                db_path=target, reset=True, verify=False,
            )
            self.assertTrue(result.passed, msg=result.errors)
            # The token the helper uses in printed commands must be the
            # dummy default, NOT the env sentinel.
            self.assertEqual(result.token_label, demo.DEFAULT_DEMO_TOKEN)
            joined = " ".join(result.powershell_commands)
            self.assertNotIn(sentinel, joined)
            # And the helper emits a warning that REVIEW_API_TOKEN was set
            # so the operator notices.
            joined_warnings = " ".join(result.warnings)
            self.assertIn("REVIEW_API_TOKEN", joined_warnings)
        finally:
            if original is None:
                os.environ.pop("REVIEW_API_TOKEN", None)
            else:
                os.environ["REVIEW_API_TOKEN"] = original

    def test_empty_token_rejected(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = demo.main(["--token", "   "])
        self.assertEqual(rc, 2, msg=err.getvalue())


# ---------------------------------------------------------------------------
# E + F — never reads OPENAI_API_KEY, never imports network libs
# ---------------------------------------------------------------------------


class NetworkSafetyTests(unittest.TestCase):
    def test_script_does_not_import_network_or_openai(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic", "requests", "httpx",
            "urllib.request", "urllib3",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"prepare_review_ui_local_demo.py must not import {forbidden!r}",
            )

    def test_script_does_not_reference_openai_api_key(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn(
            "OPENAI_API_KEY", text,
            "helper must never reference OPENAI_API_KEY",
        )

    def test_script_carries_no_git_state_changing_verbs(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        # Any "git" word coupled with a state-changing verb in source
        # would be a regression.
        for line in text.splitlines():
            if '"git"' not in line and "'git'" not in line:
                continue
            for forbidden in (
                '"add"', '"commit"', '"push"', '"reset"',
                '"checkout"', '"clean"',
            ):
                self.assertNotIn(
                    forbidden, line,
                    f"git verb {forbidden} must not appear with 'git' in helper",
                )


# ---------------------------------------------------------------------------
# G + H — seeds task with conservative Korean wording
# ---------------------------------------------------------------------------


class SeedingTests(_DemoTestBase):
    def test_seeds_at_least_one_task_with_conservative_wording(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(db_path=target, reset=True)
        self.assertTrue(result.passed, msg=result.errors)
        self.assertGreaterEqual(len(result.seeded_task_ids), 1)
        # Inspect the seeded row via the database helpers directly.
        import database, review_workflow
        original = database.DB_PATH
        database.DB_PATH = target
        try:
            tasks = database.list_review_tasks()
            self.assertGreaterEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task["final_decision"], "사람 검토 필요")
            self.assertEqual(task["policy_confidence"], "moderate")
            # No truth-style wording leaked into the seed.
            joined = json.dumps(task, default=str, ensure_ascii=False)
            for banned in ("100% 사실", "확정 참", "확정 거짓", "auto-publish"):
                self.assertNotIn(banned, joined)
            # Decision audit row is present.
            decisions = database.list_review_decisions(task["task_id"])
            self.assertGreaterEqual(len(decisions), 1)
            self.assertEqual(
                decisions[0]["decision"], "needs_more_evidence",
            )
        finally:
            database.DB_PATH = original


# ---------------------------------------------------------------------------
# I + J + K — --verify mode exercises endpoints and packet stays safe
# ---------------------------------------------------------------------------


class VerifyModeTests(_DemoTestBase):
    def test_verify_mode_passes_against_seeded_db(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(
            db_path=target, reset=True, verify=True,
        )
        self.assertTrue(result.passed, msg=result.errors)
        self.assertIsNotNone(result.verify)
        verify = result.verify
        self.assertTrue(verify["passed"], msg=verify)
        self.assertEqual(verify["list_status"], 200)
        self.assertEqual(verify["detail_status"], 200)
        self.assertEqual(verify["audit_packet_status"], 200)
        self.assertTrue(verify["audit_packet_publication_false"])
        self.assertTrue(verify["no_token_in_audit_packet"])

    def test_verify_uses_dummy_token_only(self):
        # If the operator passes a custom dummy via --token, verify must
        # use that exact value and the audit packet must still pass the
        # "no token literal" check (the M9.1 endpoint never echoes it).
        target = self._new_demo_db()
        custom = "alt-dummy-token-for-test"
        result = demo.prepare_demo(
            db_path=target, token=custom, reset=True, verify=True,
        )
        self.assertTrue(result.passed, msg=result.errors)
        verify = result.verify
        self.assertTrue(verify["passed"])
        self.assertTrue(verify["no_token_in_audit_packet"])


# ---------------------------------------------------------------------------
# L — JSON mode has stable keys
# ---------------------------------------------------------------------------


class JSONOutputTests(_DemoTestBase):
    EXPECTED_KEYS = {
        "passed", "db_path", "token_is_dummy", "token_label",
        "seeded_task_ids", "expected_local_url", "powershell_commands",
        "warnings", "errors", "verify",
    }

    def test_json_payload_has_stable_keys(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(db_path=target, reset=True)
        payload = demo.result_to_dict(result)
        self.assertEqual(set(payload.keys()), self.EXPECTED_KEYS)

    def test_json_payload_no_secret_like_values(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(db_path=target, reset=True)
        body = json.dumps(demo.result_to_dict(result), ensure_ascii=False)
        for needle in ("OPENAI_API_KEY", "sk-"):
            self.assertNotIn(needle, body)
        # The body contains the dummy token by design — that's fine.
        # But it must not carry a hex-token-shaped literal.
        import re as _re
        self.assertFalse(
            _re.search(r"[0-9a-fA-F]{32,}", body),
            f"JSON output unexpectedly contains a hex token literal: {body}",
        )


# ---------------------------------------------------------------------------
# M — --reset replaces an existing demo DB
# ---------------------------------------------------------------------------


class ResetBehaviorTests(_DemoTestBase):
    def test_refuses_overwrite_without_reset(self):
        target = self._new_demo_db()
        first = demo.prepare_demo(db_path=target, reset=True)
        self.assertTrue(first.passed)
        # Second run without --reset must refuse.
        second = demo.prepare_demo(db_path=target, reset=False)
        self.assertFalse(second.passed)
        joined = " ".join(second.errors)
        self.assertIn("already exists", joined)
        self.assertIn("--reset", joined)

    def test_reset_replaces_existing_db(self):
        target = self._new_demo_db()
        first = demo.prepare_demo(db_path=target, reset=True)
        self.assertTrue(first.passed, msg=first.errors)
        # Touch a marker so we can tell the DB was replaced.
        mtime_before = target.stat().st_mtime_ns
        import time
        time.sleep(0.01)
        second = demo.prepare_demo(db_path=target, reset=True)
        self.assertTrue(second.passed, msg=second.errors)
        mtime_after = target.stat().st_mtime_ns
        self.assertGreater(mtime_after, mtime_before)


# ---------------------------------------------------------------------------
# N — Korean text remains readable
# ---------------------------------------------------------------------------


class KoreanReadabilityTests(_DemoTestBase):
    def test_korean_claim_text_round_trips(self):
        target = self._new_demo_db()
        result = demo.prepare_demo(db_path=target, reset=True)
        self.assertTrue(result.passed)
        import database
        original = database.DB_PATH
        database.DB_PATH = target
        try:
            tasks = database.list_review_tasks()
            self.assertGreaterEqual(len(tasks), 1)
            self.assertIn(
                "청년 월세 지원 정책은 사람 검토가 필요한 상태입니다.",
                tasks[0]["claim_text"],
            )
            self.assertIn("청년 월세 지원", tasks[0]["query"])
            self.assertIn("청년 월세 지원", tasks[0]["title"])
        finally:
            database.DB_PATH = original


# ---------------------------------------------------------------------------
# O — helper never invokes any git verb
# ---------------------------------------------------------------------------


class GitSafetyTests(unittest.TestCase):
    def test_neither_helper_nor_launcher_invokes_git(self):
        for path in (SCRIPT_PATH, LAUNCHER_PATH):
            text = path.read_text(encoding="utf-8")
            # subprocess shouldn't be used at all here — the helpers
            # only talk to FastAPI TestClient + the database directly.
            # The serve launcher uses uvicorn but no subprocess.
            for token in ("subprocess.run", "subprocess.call",
                          "subprocess.Popen", "os.system"):
                self.assertNotIn(
                    token, text,
                    f"{path.name}: must not call out via {token}",
                )


# ---------------------------------------------------------------------------
# P — reports/ outputs are not recommended for commit
# ---------------------------------------------------------------------------


class ReportsExclusionTests(_DemoTestBase):
    def test_demo_db_lives_under_reports_so_gitignore_covers_it(self):
        # The demo DB extension/path family must be one the existing
        # operator_preflight forbidden classifier rejects from --expected.
        import scripts.operator_preflight as preflight
        target = self._new_demo_db()
        result = demo.prepare_demo(db_path=target, reset=True)
        self.assertTrue(result.passed)
        # The default helper path is under reports/, which the
        # preflight forbidden-path classifier treats as excluded.
        self.assertTrue(
            preflight.is_forbidden_path(
                str(Path("reports") / demo.DEMO_DB_FILENAME)
            ),
            "the default demo DB path must register as forbidden in "
            "operator_preflight so it never lands in a recommended git add",
        )


# ---------------------------------------------------------------------------
# Launcher safety
# ---------------------------------------------------------------------------


class LauncherSafetyTests(unittest.TestCase):
    def test_launcher_refuses_unsafe_path(self):
        import scripts.serve_review_ui_local_demo as serve
        out = io.StringIO()
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            unsafe = str(Path(td) / "outside.sqlite")
            with redirect_stdout(out), redirect_stderr(err):
                rc = serve.main(["--db-path", unsafe])
        self.assertEqual(rc, 1, msg=err.getvalue())
        self.assertIn("outside reports/", err.getvalue())

    def test_launcher_refuses_missing_db(self):
        import scripts.serve_review_ui_local_demo as serve
        out = io.StringIO()
        err = io.StringIO()
        missing = str(ROOT / "reports" / "definitely_missing_demo.sqlite")
        with redirect_stdout(out), redirect_stderr(err):
            rc = serve.main(["--db-path", missing])
        self.assertEqual(rc, 1, msg=err.getvalue())
        self.assertIn("demo DB not found", err.getvalue())

    def test_launcher_does_not_import_network_or_openai(self):
        text = LAUNCHER_PATH.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic", "requests", "httpx",
            "urllib.request", "urllib3",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"serve_review_ui_local_demo.py must not import {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main()
