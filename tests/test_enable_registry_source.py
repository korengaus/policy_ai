"""Phase 2 M10.3: tests for ``scripts/enable_registry_source.py``.

Every test that writes uses a temp copy of ``data/source_registry.json``
or a synthetic fixture. The real seed registry is never touched.
Subprocess tests confirm the full CLI exit-code policy end-to-end.

Covers the M10.3 spec items:
    A. --list prints all entries and exits 0
    B. --list --json valid JSON with expected counts
    C. --source-id not found → exit 1, clear error
    D. --justification too short → exit 1, no write
    E. --justification missing → exit 2 (usage error)
    F. truth_claim=true entry → refused → exit 1 (synthetic fixture)
    G. Already-enabled entry → idempotent exit 0, no write
    H. --dry-run → summary printed, no write, exit 0
    I. --yes skips confirmation and writes successfully
    J. Atomic write: no .tmp left behind
    K. operator_enable_record present in written JSON
    L. All other fields preserved exactly after write
    M. --json on success carries expected fields
    N. Confirmation 'NO' → aborts → exit 1, no write
    O. Confirmation 'YES' → writes → exit 0
    P. Safety notes present in every output mode
    Q. No network / OpenAI / browser imports in CLI source
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_registry as registry_mod  # noqa: E402
import scripts.enable_registry_source as enable_cli  # noqa: E402


CLI_SCRIPT = ROOT / "scripts" / "enable_registry_source.py"
SEED_REGISTRY = ROOT / "data" / "source_registry.json"

CLI_TIMEOUT_SECONDS = 10.0

ENABLE_JUSTIFICATION = (
    "operator dry run test justification text"
)
SEED_TARGET_SOURCE_ID = "kr_law_open_data_candidate"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_seed_registry(tmp_dir: Path) -> Path:
    """Copy the real seed registry into a temp directory so tests can
    safely write it without ever touching the repo file."""
    dst = tmp_dir / "source_registry.json"
    shutil.copy2(SEED_REGISTRY, dst)
    return dst


def _make_synthetic_registry(tmp_dir: Path, *, extra_sources=None) -> Path:
    """Build a synthetic registry with a deterministic shape so tests
    can flip individual fields (truth_claim=true, etc.) without
    relying on the real seed."""
    sources = [
        {
            "source_id": "test_clean_source",
            "display_name": "Test clean source",
            "source_type": "law_or_regulation",
            "jurisdiction": "KR",
            "base_url": "https://example.go.kr",
            "allowed_domains": ["example.go.kr"],
            "allow_subdomains": False,
            "default_enabled": False,
            "capture_method": "manual_or_http",
            "browser_automation": "not_required",
            "operator_review_required": True,
            "official_source_candidate": True,
            "truth_claim": False,
            "semantic_debug_only": False,
            "notes": "Synthetic test fixture",
            "tags": ["test"],
        },
        {
            "source_id": "test_already_enabled",
            "display_name": "Already enabled",
            "source_type": "demo",
            "jurisdiction": "KR",
            "base_url": "https://example.go.kr",
            "allowed_domains": ["example.go.kr"],
            "allow_subdomains": False,
            "default_enabled": True,
            "capture_method": "manual_or_http",
            "browser_automation": "not_required",
            "operator_review_required": False,
            "operator_review_required_justification": (
                "pre-existing justification at least twenty chars long"
            ),
            "official_source_candidate": False,
            "truth_claim": False,
            "semantic_debug_only": False,
            "notes": "Already-enabled fixture",
            "tags": ["test"],
        },
        {
            "source_id": "test_truth_claim_true",
            "display_name": "Bad truth_claim",
            "source_type": "demo",
            "jurisdiction": "KR",
            "base_url": "https://example.go.kr",
            "allowed_domains": ["example.go.kr"],
            "allow_subdomains": False,
            "default_enabled": False,
            "capture_method": "manual_or_http",
            "browser_automation": "not_required",
            "operator_review_required": True,
            "official_source_candidate": False,
            "truth_claim": True,   # deliberately bad
            "semantic_debug_only": False,
            "notes": "Synthetic fixture; truth_claim deliberately true",
            "tags": ["test"],
        },
        {
            "source_id": "test_browser_required",
            "display_name": "Needs browser automation",
            "source_type": "government_policy",
            "jurisdiction": "KR",
            "base_url": "https://example.go.kr",
            "allowed_domains": ["example.go.kr"],
            "allow_subdomains": False,
            "default_enabled": False,
            "capture_method": "browser_required",
            "browser_automation": "required",
            "operator_review_required": True,
            "official_source_candidate": False,
            "truth_claim": False,
            "semantic_debug_only": False,
            "notes": "Synthetic fixture",
            "tags": ["test"],
        },
    ]
    if extra_sources:
        sources.extend(extra_sources)
    payload = {
        "schema_version": 1,
        "registry_name": "policy_ai_source_registry",
        "registry_notes": "Synthetic test registry",
        "sources": sources,
    }
    path = tmp_dir / "synthetic_registry.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _run_cli_subprocess(*args, timeout=CLI_TIMEOUT_SECONDS, env=None,
                        stdin_text=None):
    """Run the CLI via subprocess. Returns (rc, stdout, stderr)."""
    completed = subprocess.run(
        [sys.executable, str(CLI_SCRIPT)] + [str(a) for a in args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, **(env or {})},
        input=stdin_text,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _run_cli_inproc(argv):
    """Run the CLI's ``main()`` in-process. Returns (rc, stdout, stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = enable_cli.main(argv)
    except SystemExit as e:
        rc = int(e.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _load_registry(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _find_source(registry, source_id):
    for s in registry.get("sources") or []:
        if s.get("source_id") == source_id:
            return s
    return None


# ---------------------------------------------------------------------------
# A / B / P. --list mode
# ---------------------------------------------------------------------------


class ListModeTests(unittest.TestCase):
    def test_list_human_exits_0_and_prints_all_entries(self):
        rc, stdout, stderr = _run_cli_subprocess("--list")
        self.assertEqual(rc, 0, msg=stdout + stderr)
        self.assertIn("Registry Source Status", stdout)
        self.assertIn(SEED_TARGET_SOURCE_ID, stdout)
        # Five seed entries in the real registry.
        self.assertIn("Total: 5", stdout)
        # Safety note in human output.
        self.assertIn("does NOT imply truth", stdout)

    def test_list_json_carries_expected_summary(self):
        rc, stdout, _ = _run_cli_subprocess("--list", "--json")
        self.assertEqual(rc, 0, msg=stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["mode"], "list")
        self.assertEqual(payload["cli_version"], enable_cli.CLI_VERSION)
        self.assertIn("safety_notes", payload)
        self.assertIn("not_truth", payload["safety_notes"])
        summary = payload["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["enabled"], 0)
        self.assertEqual(summary["review_required"], 5)
        # Per-row keys.
        for row in payload["sources"]:
            for key in (
                "source_id", "default_enabled", "operator_review_required",
                "source_type", "capture_method", "browser_automation",
                "official_source_candidate", "truth_claim",
            ):
                self.assertIn(key, row, msg=f"missing key: {key}")

    def test_list_with_custom_registry_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _make_synthetic_registry(tmp_dir)
            rc, stdout, _ = _run_cli_subprocess(
                "--list", "--registry-path", str(reg), "--json",
            )
            self.assertEqual(rc, 0)
            payload = json.loads(stdout)
            ids = {row["source_id"] for row in payload["sources"]}
            self.assertEqual(
                ids,
                {
                    "test_clean_source", "test_already_enabled",
                    "test_truth_claim_true", "test_browser_required",
                },
            )


# ---------------------------------------------------------------------------
# C. Source not found
# ---------------------------------------------------------------------------


class SourceNotFoundTests(unittest.TestCase):
    def test_unknown_source_id_exits_1(self):
        rc, stdout, _ = _run_cli_subprocess(
            "--source-id", "no_such_source_anywhere",
            "--justification", ENABLE_JUSTIFICATION,
            "--dry-run",
        )
        self.assertEqual(rc, 1)
        self.assertIn("not in registry", stdout)


# ---------------------------------------------------------------------------
# D / E. Justification handling
# ---------------------------------------------------------------------------


class JustificationTests(unittest.TestCase):
    def test_justification_too_short_refuses(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            before = reg.read_bytes()
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", "too short",
                "--registry-path", str(reg),
                "--dry-run",
            )
            self.assertEqual(rc, 1)
            self.assertIn("justification too short", stdout)
            self.assertEqual(reg.read_bytes(), before,
                             "registry file must not be modified on refusal")

    def test_justification_missing_is_usage_error(self):
        rc, _stdout, stderr = _run_cli_subprocess(
            "--source-id", SEED_TARGET_SOURCE_ID,
            "--dry-run",
        )
        self.assertEqual(rc, 2)
        self.assertIn("--justification", stderr)


# ---------------------------------------------------------------------------
# F. truth_claim=true refuses
# ---------------------------------------------------------------------------


class TruthClaimRefusalTests(unittest.TestCase):
    def test_truth_claim_true_refuses(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _make_synthetic_registry(tmp_dir)
            before = reg.read_bytes()
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", "test_truth_claim_true",
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes",
            )
            self.assertEqual(rc, 1)
            self.assertIn("truth_claim", stdout)
            self.assertEqual(reg.read_bytes(), before,
                             "registry must not be written for truth_claim=true")


# ---------------------------------------------------------------------------
# G. Idempotent on already-enabled
# ---------------------------------------------------------------------------


class IdempotencyTests(unittest.TestCase):
    def test_already_enabled_exits_0_no_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _make_synthetic_registry(tmp_dir)
            before = reg.read_bytes()
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", "test_already_enabled",
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes",
            )
            self.assertEqual(rc, 0, msg=stdout)
            self.assertIn("already enabled", stdout)
            self.assertEqual(reg.read_bytes(), before,
                             "registry must not be re-written when idempotent")


# ---------------------------------------------------------------------------
# H. Dry-run summary, no write
# ---------------------------------------------------------------------------


class DryRunTests(unittest.TestCase):
    def test_dry_run_prints_summary_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            before = reg.read_bytes()
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--dry-run",
            )
            self.assertEqual(rc, 0, msg=stdout)
            self.assertIn("DRY RUN", stdout)
            self.assertIn("default_enabled: False -> True", stdout)
            self.assertIn(
                "operator_review_required: True -> False", stdout,
            )
            self.assertIn(ENABLE_JUSTIFICATION, stdout)
            self.assertIn("does NOT imply truth", stdout)
            self.assertEqual(
                reg.read_bytes(), before,
                "dry-run must not modify the registry file",
            )

    def test_dry_run_json_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--dry-run", "--json",
            )
            self.assertEqual(rc, 0, msg=stdout)
            payload = json.loads(stdout)
            self.assertEqual(payload["mode"], "dry_run")
            self.assertTrue(payload["source_found"])
            self.assertFalse(payload["written"])
            self.assertIs(payload["truth_claim"], False)
            self.assertIsNone(payload["refusal_reason"])
            self.assertEqual(
                payload["current_state"]["default_enabled"], False,
            )
            self.assertEqual(
                payload["proposed_state"]["default_enabled"], True,
            )
            self.assertEqual(
                payload["proposed_state"]["operator_review_required"], False,
            )
            self.assertIn("safety_notes", payload)


# ---------------------------------------------------------------------------
# I / J / K / L. --yes writes successfully with atomic semantics
# ---------------------------------------------------------------------------


class YesWriteTests(unittest.TestCase):
    def test_yes_writes_successfully(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            original = _load_registry(reg)
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes",
            )
            self.assertEqual(rc, 0, msg=stdout)
            written = _load_registry(reg)
            updated = _find_source(written, SEED_TARGET_SOURCE_ID)
            self.assertIsNotNone(updated)
            self.assertIs(updated["default_enabled"], True)
            self.assertIs(updated["operator_review_required"], False)
            self.assertIs(updated["truth_claim"], False)
            # operator_enable_record present with all three fields.
            record = updated.get("operator_enable_record")
            self.assertIsInstance(record, dict)
            self.assertEqual(record.get("justification"), ENABLE_JUSTIFICATION)
            self.assertIn("enabled_at", record)
            self.assertIn("cli_version", record)
            # All other fields preserved exactly.
            original_entry = _find_source(original, SEED_TARGET_SOURCE_ID)
            for key in (
                "source_id", "display_name", "source_type", "jurisdiction",
                "base_url", "allowed_domains", "allow_subdomains",
                "capture_method", "browser_automation",
                "official_source_candidate", "semantic_debug_only",
                "notes", "tags",
            ):
                self.assertEqual(
                    updated.get(key), original_entry.get(key),
                    msg=f"field {key!r} should be preserved",
                )
            # Other sources unchanged.
            for sid in (
                "demo_official_policy_source", "demo_local_fixture_source",
                "kr_national_assembly_candidate",
                "kr_gov_open_portal_candidate",
            ):
                self.assertEqual(
                    _find_source(written, sid),
                    _find_source(original, sid),
                    msg=f"unrelated source {sid!r} must be unchanged",
                )
            # Top-level fields preserved.
            for key in ("schema_version", "registry_name", "registry_notes"):
                self.assertEqual(written.get(key), original.get(key))

    def test_yes_leaves_no_tmp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            rc, _stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes",
            )
            self.assertEqual(rc, 0)
            # No .tmp files lingering anywhere in the temp dir.
            for child in tmp_dir.iterdir():
                self.assertFalse(
                    child.name.endswith(".tmp"),
                    f"unexpected tmp file left behind: {child}",
                )

    def test_yes_json_payload_shape(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes", "--json",
            )
            self.assertEqual(rc, 0, msg=stdout)
            # Strip the optional stderr note from stdout: --json on stdout
            # is the only payload we wrote.
            payload = json.loads(stdout)
            for key in (
                "cli_version", "mode", "processed_at", "registry_path",
                "source_id", "source_found", "already_enabled",
                "justification", "refusal_reason", "current_state",
                "proposed_state", "written", "truth_claim", "safety_notes",
            ):
                self.assertIn(key, payload)
            self.assertEqual(payload["mode"], "enable")
            self.assertTrue(payload["written"])
            self.assertIs(payload["truth_claim"], False)
            self.assertIsNone(payload["refusal_reason"])
            self.assertIs(
                payload["proposed_state"]["default_enabled"], True,
            )


# ---------------------------------------------------------------------------
# N / O. Interactive confirmation prompt
# ---------------------------------------------------------------------------


class ConfirmationPromptTests(unittest.TestCase):
    """Use in-process invocation so we can monkeypatch ``input``."""

    def test_confirmation_yes_writes(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            with patch.object(enable_cli, "_read_confirmation",
                              return_value="YES"):
                rc, stdout, _ = _run_cli_inproc([
                    "--source-id", SEED_TARGET_SOURCE_ID,
                    "--justification", ENABLE_JUSTIFICATION,
                    "--registry-path", str(reg),
                ])
            self.assertEqual(rc, 0, msg=stdout)
            updated = _find_source(
                _load_registry(reg), SEED_TARGET_SOURCE_ID,
            )
            self.assertTrue(updated["default_enabled"])
            self.assertFalse(updated["operator_review_required"])

    def test_confirmation_no_aborts(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            before = reg.read_bytes()
            with patch.object(enable_cli, "_read_confirmation",
                              return_value="NO"):
                rc, stdout, _ = _run_cli_inproc([
                    "--source-id", SEED_TARGET_SOURCE_ID,
                    "--justification", ENABLE_JUSTIFICATION,
                    "--registry-path", str(reg),
                ])
            self.assertEqual(rc, 1)
            self.assertIn("confirmation aborted", stdout)
            self.assertEqual(reg.read_bytes(), before,
                             "registry must not be written when confirmation aborts")

    def test_confirmation_empty_aborts(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            before = reg.read_bytes()
            with patch.object(enable_cli, "_read_confirmation",
                              return_value=""):
                rc, _stdout, _ = _run_cli_inproc([
                    "--source-id", SEED_TARGET_SOURCE_ID,
                    "--justification", ENABLE_JUSTIFICATION,
                    "--registry-path", str(reg),
                ])
            self.assertEqual(rc, 1)
            self.assertEqual(reg.read_bytes(), before)


# ---------------------------------------------------------------------------
# capture_method=browser_required gating
# ---------------------------------------------------------------------------


class BrowserGateTests(unittest.TestCase):
    def test_browser_required_refused_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _make_synthetic_registry(tmp_dir)
            before = reg.read_bytes()
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", "test_browser_required",
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes",
            )
            self.assertEqual(rc, 1)
            self.assertIn("browser", stdout.lower())
            self.assertEqual(reg.read_bytes(), before)

    def test_browser_required_allowed_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _make_synthetic_registry(tmp_dir)
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", "test_browser_required",
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--yes", "--allow-browser",
            )
            self.assertEqual(rc, 0, msg=stdout)
            updated = _find_source(
                _load_registry(reg), "test_browser_required",
            )
            self.assertTrue(updated["default_enabled"])
            self.assertFalse(updated["operator_review_required"])


# ---------------------------------------------------------------------------
# P. Safety notes present in every output mode
# ---------------------------------------------------------------------------


class SafetyNotesTests(unittest.TestCase):
    def test_human_list_carries_safety_notes(self):
        rc, stdout, _ = _run_cli_subprocess("--list")
        self.assertEqual(rc, 0)
        self.assertIn(enable_cli.SAFETY_NOTE_NOT_TRUTH, stdout)
        self.assertIn(enable_cli.SAFETY_NOTE_REVIEW, stdout)
        self.assertIn(enable_cli.SAFETY_NOTE_NO_AUTO, stdout)

    def test_json_list_carries_safety_notes(self):
        rc, stdout, _ = _run_cli_subprocess("--list", "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(
            payload["safety_notes"]["not_truth"],
            enable_cli.SAFETY_NOTE_NOT_TRUTH,
        )
        self.assertEqual(
            payload["safety_notes"]["review"],
            enable_cli.SAFETY_NOTE_REVIEW,
        )
        self.assertEqual(
            payload["safety_notes"]["no_auto"],
            enable_cli.SAFETY_NOTE_NO_AUTO,
        )

    def test_human_dry_run_carries_safety_notes(self):
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            reg = _copy_seed_registry(tmp_dir)
            rc, stdout, _ = _run_cli_subprocess(
                "--source-id", SEED_TARGET_SOURCE_ID,
                "--justification", ENABLE_JUSTIFICATION,
                "--registry-path", str(reg),
                "--dry-run",
            )
            self.assertEqual(rc, 0)
            self.assertIn(enable_cli.SAFETY_NOTE_NOT_TRUTH, stdout)
            self.assertIn(enable_cli.SAFETY_NOTE_REVIEW, stdout)
            self.assertIn(enable_cli.SAFETY_NOTE_NO_AUTO, stdout)


# ---------------------------------------------------------------------------
# Q. Static safety — no banned imports in the CLI source
# ---------------------------------------------------------------------------


class StaticSafetyTests(unittest.TestCase):
    def test_no_network_or_browser_or_openai_imports(self):
        text = CLI_SCRIPT.read_text(encoding="utf-8")
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        joined = "\n".join(import_lines)
        for forbidden in (
            "openai", "anthropic",
            "requests", "httpx",
            "urllib.request", "socket",
            "playwright", "browser_use", "openclaw", "selenium",
        ):
            self.assertNotIn(
                forbidden, joined,
                f"enable_registry_source.py must not import {forbidden!r}",
            )

    def test_cli_not_imported_by_pipeline_entry_points(self):
        for module_name in ("main.py", "api_server.py", "scheduler.py"):
            module_path = ROOT / module_name
            if not module_path.exists():
                continue
            text = module_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "enable_registry_source", text,
                f"{module_name} must not import enable_registry_source",
            )

    def test_help_exits_0(self):
        rc, stdout, _ = _run_cli_subprocess("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Enable a source-registry entry", stdout)
        self.assertIn("Exit codes", stdout)


# ---------------------------------------------------------------------------
# Unit tests on internal helpers
# ---------------------------------------------------------------------------


class PrecheckUnitTests(unittest.TestCase):
    def _good_source(self):
        return {
            "source_id": "x",
            "default_enabled": False,
            "operator_review_required": True,
            "capture_method": "manual_or_http",
            "browser_automation": "not_required",
            "truth_claim": False,
        }

    def test_clean_path_returns_ok(self):
        ok, reason = enable_cli._precheck(
            source_id="x", source=self._good_source(),
            justification=ENABLE_JUSTIFICATION, allow_browser=False,
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_missing_source_returns_not_found(self):
        ok, reason = enable_cli._precheck(
            source_id="x", source=None,
            justification=ENABLE_JUSTIFICATION, allow_browser=False,
        )
        self.assertFalse(ok)
        self.assertIn("not found", reason)

    def test_truth_claim_blocks(self):
        s = self._good_source()
        s["truth_claim"] = True
        ok, reason = enable_cli._precheck(
            source_id="x", source=s,
            justification=ENABLE_JUSTIFICATION, allow_browser=False,
        )
        self.assertFalse(ok)
        self.assertIn("truth_claim", reason)

    def test_browser_required_blocks_without_flag(self):
        s = self._good_source()
        s["capture_method"] = "browser_required"
        ok, reason = enable_cli._precheck(
            source_id="x", source=s,
            justification=ENABLE_JUSTIFICATION, allow_browser=False,
        )
        self.assertFalse(ok)
        self.assertIn("browser", reason.lower())

    def test_browser_required_passes_with_flag(self):
        s = self._good_source()
        s["capture_method"] = "browser_required"
        ok, _ = enable_cli._precheck(
            source_id="x", source=s,
            justification=ENABLE_JUSTIFICATION, allow_browser=True,
        )
        self.assertTrue(ok)

    def test_short_justification_blocks(self):
        ok, reason = enable_cli._precheck(
            source_id="x", source=self._good_source(),
            justification="too short", allow_browser=False,
        )
        self.assertFalse(ok)
        self.assertIn("too short", reason)


class ApplyEnableUnitTests(unittest.TestCase):
    def test_apply_does_not_mutate_input(self):
        original = {
            "source_id": "x",
            "default_enabled": False,
            "operator_review_required": True,
            "truth_claim": False,
            "notes": "keep",
        }
        snapshot = dict(original)
        updated = enable_cli._apply_enable_to_record(
            original, justification=ENABLE_JUSTIFICATION,
        )
        self.assertEqual(original, snapshot, "input dict must not mutate")
        self.assertIs(updated["default_enabled"], True)
        self.assertIs(updated["operator_review_required"], False)
        self.assertIs(updated["truth_claim"], False)
        self.assertEqual(updated["notes"], "keep")
        self.assertIn("operator_enable_record", updated)
        self.assertEqual(
            updated["operator_enable_record"]["justification"],
            ENABLE_JUSTIFICATION,
        )

    def test_apply_forces_truth_claim_false_even_if_input_lies(self):
        original = {
            "source_id": "x",
            "default_enabled": False,
            "operator_review_required": True,
            "truth_claim": True,  # CLI should never produce this; check defense-in-depth
        }
        updated = enable_cli._apply_enable_to_record(
            original, justification=ENABLE_JUSTIFICATION,
        )
        self.assertIs(updated["truth_claim"], False)


if __name__ == "__main__":
    unittest.main()
