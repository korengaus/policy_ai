"""Phase 2 M8.3: tests for ``scripts/smoke_review_workflow.py``.

Verifies that:
    * the self-contained smoke passes end-to-end against a temp SQLite DB,
    * every documented sub-check returns ``passed=True``,
    * the dummy token never appears in stdout, stderr, or the JSON summary,
    * the script does not require ``OPENAI_API_KEY`` / network / Postgres,
    * the script does not modify Render env or import verdict modules,
    * the CLI rejects a run without ``--self-contained``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SMOKE_SCRIPT = ROOT / "scripts" / "smoke_review_workflow.py"


@contextmanager
def _env(**overrides):
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_smoke(*extra_args: str, env_overrides: dict | None = None):
    """Spawn the smoke script with a clean env. Return (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SMOKE_SCRIPT)] + list(extra_args)
    proc_env = os.environ.copy()
    # Strip Render / OpenAI / review env so the smoke proves it doesn't need them.
    for k in (
        "OPENAI_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_PROVIDER",
        "SEMANTIC_MATCHING_ENABLED",
        "REVIEW_API_ENABLED", "REVIEW_API_TOKEN",
        "DATABASE_URL", "USE_POSTGRES_WRITE",
    ):
        proc_env.pop(k, None)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                proc_env.pop(k, None)
            else:
                proc_env[k] = v
    result = subprocess.run(
        cmd, cwd=str(ROOT), env=proc_env,
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.returncode, result.stdout or "", result.stderr or ""


def _extract_json_summary(stdout: str) -> dict:
    """Pull the trailing JSON blob the smoke prints after the human summary."""
    start = stdout.find("\n{")
    assert start != -1, f"JSON block not found in stdout:\n{stdout}"
    candidate = stdout[start + 1:]
    # JSON ends at the final closing brace at column 0.
    end = candidate.rfind("\n}")
    assert end != -1
    return json.loads(candidate[: end + 2])


class CLIContractTests(unittest.TestCase):
    def test_help_exits_cleanly(self):
        rc, out, err = _run_smoke("--help")
        self.assertEqual(rc, 0, msg=err or out)
        self.assertIn("--self-contained", out)
        # API keys never appear in --help.
        self.assertNotIn("sk-", out)
        self.assertNotIn("sk-", err)

    def test_requires_self_contained_flag(self):
        rc, _out, err = _run_smoke()
        self.assertEqual(rc, 2, msg=err)
        self.assertIn("--self-contained", err)


class SelfContainedSmokeTests(unittest.TestCase):
    def test_smoke_passes_end_to_end(self):
        rc, out, err = _run_smoke("--self-contained")
        self.assertEqual(rc, 0, msg=(out + "\n---\n" + err)[-4000:])
        summary = _extract_json_summary(out)
        self.assertTrue(summary["passed"], msg=summary)
        for key in (
            "disabled_check", "token_check", "task_creation_check",
            "idempotency_check", "list_detail_check", "decision_check",
            "verdict_isolation_check", "publication_absent_check",
        ):
            self.assertTrue(
                summary[key]["passed"],
                msg=f"{key} did not pass: {summary[key]}",
            )

    def test_decision_check_covers_all_allowed_decisions(self):
        rc, out, _err = _run_smoke("--self-contained")
        self.assertEqual(rc, 0)
        summary = _extract_json_summary(out)
        decisions = summary["decision_check"]["decisions"]
        self.assertEqual(
            set(decisions.keys()),
            {"approve", "reject", "needs_more_evidence", "comment"},
        )
        # comment-only decisions must not change status.
        self.assertEqual(
            decisions["comment"]["new_status"], "pending_review",
        )
        # The other three move into their named status.
        self.assertEqual(decisions["approve"]["new_status"], "approved")
        self.assertEqual(decisions["reject"]["new_status"], "rejected")
        self.assertEqual(
            decisions["needs_more_evidence"]["new_status"], "needs_more_evidence",
        )

    def test_no_publish_endpoint_and_no_reserved_status_decisions(self):
        rc, out, _err = _run_smoke("--self-contained")
        self.assertEqual(rc, 0)
        summary = _extract_json_summary(out)
        pub = summary["publication_absent_check"]
        self.assertTrue(pub["publish_blocked"])
        self.assertIn(pub["publish_status_code"], (404, 405))
        self.assertTrue(pub["reserved_blocked"])
        for status_name in ("published", "corrected"):
            self.assertIn(pub["reserved_decision_attempts"][status_name],
                          (400, 409, 422))

    def test_verdict_snapshot_fields_are_unchanged_after_decisions(self):
        rc, out, _err = _run_smoke("--self-contained")
        self.assertEqual(rc, 0)
        summary = _extract_json_summary(out)
        vi = summary["verdict_isolation_check"]
        self.assertTrue(vi["payload_unchanged"])
        self.assertTrue(vi["final_decision_label_stable"])
        self.assertTrue(vi["policy_confidence_label_stable"])
        self.assertTrue(vi["verification_card_unchanged"])


class SecretsAndIsolationTests(unittest.TestCase):
    DUMMY_TOKEN_LITERAL = "smoke-dummy-token-internal-only-do-not-publish"

    def test_dummy_token_never_appears_in_smoke_output(self):
        rc, out, err = _run_smoke("--self-contained")
        self.assertEqual(rc, 0)
        self.assertNotIn(self.DUMMY_TOKEN_LITERAL, out)
        self.assertNotIn(self.DUMMY_TOKEN_LITERAL, err)
        # API-key style literals also never appear.
        self.assertNotIn("sk-", out)

    def test_script_does_not_require_openai_key(self):
        # _run_smoke already strips OPENAI_API_KEY; passing succeeds anyway.
        rc, out, _err = _run_smoke("--self-contained")
        self.assertEqual(rc, 0, msg=out[-2000:])

    def test_script_does_not_import_verdict_modules_or_render(self):
        text = SMOKE_SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import policy_decision",
            "from policy_decision",
            "import policy_scoring",
            "from policy_scoring",
            "import verification_card",
            "from verification_card",
            "render.yaml",
            "render_env",
            "openai.",
            "OPENAI_API_KEY",
        ):
            self.assertNotIn(
                forbidden, text,
                f"smoke_review_workflow.py must not reference {forbidden!r}",
            )

    def test_script_does_not_modify_render_env(self):
        text = SMOKE_SCRIPT.read_text(encoding="utf-8")
        # Render-side env keys must never be set/popped by this script.
        for needle in (
            'os.environ["SEMANTIC_MATCHING_ENABLED"]',
            'os.environ["EMBEDDING_PROVIDER"]',
            'os.environ["OPENAI_API_KEY"]',
        ):
            self.assertNotIn(needle, text, f"must not mutate {needle}")


if __name__ == "__main__":
    unittest.main()
