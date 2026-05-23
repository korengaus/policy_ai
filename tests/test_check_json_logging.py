"""Tests for ``scripts/check_json_logging.py`` (M14.2).

Run with: python tests/test_check_json_logging.py

The verifier is exercised in three ways:

1. CLI argument parsing via direct ``main()`` invocations with captured
   stdout/stderr.
2. JSON schema and Korean preservation logic via unit-level calls to
   the script's helpers (``_validate_record``,
   ``_check_korean_preservation``).
3. End-to-end ``--local`` path via a mocked subprocess that returns
   canned bytes — no real ``check_logging.py`` execution required.

The ``--base-url`` mode is exercised with mocked
``urllib.request.urlopen`` so no real Render traffic occurs.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "check_json_logging_cli",
        str(_PROJECT_ROOT / "scripts" / "check_json_logging.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_main(module, argv):
    """Call module.main directly with stdout/stderr captured."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        rc = module.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class CliArgumentTests(unittest.TestCase):
    def test_help_exits_zero(self):
        module = _load_cli_module()
        rc, out, _ = _invoke_main(module, ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("check_json_logging", out)
        self.assertIn("Exit codes", out)

    def test_local_and_base_url_are_mutually_exclusive(self):
        module = _load_cli_module()
        rc, _, err = _invoke_main(
            module, ["--local", "--base-url", "http://x"],
        )
        self.assertEqual(rc, 2)
        self.assertIn("not allowed", err.lower())


# ---------------------------------------------------------------------------
# Schema validation helpers
# ---------------------------------------------------------------------------


_VALID_RECORD = {
    "ts": "2026-05-23T14:32:01.234567+00:00",
    "level": "INFO",
    "module": "structured_logging.sample",
    "msg": "This is an INFO message",
}


class RecordValidationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()

    def test_complete_record_validates(self):
        self.assertEqual(
            self.module._validate_record(dict(_VALID_RECORD)), [],
        )

    def test_extras_tolerated(self):
        record = dict(_VALID_RECORD)
        record["extra"] = {"foo": "bar"}
        record["exc"] = "Traceback (...)"
        self.assertEqual(self.module._validate_record(record), [])

    def test_missing_ts_fails(self):
        record = {k: v for k, v in _VALID_RECORD.items() if k != "ts"}
        errors = self.module._validate_record(record)
        self.assertTrue(any("ts" in e for e in errors))

    def test_missing_level_fails(self):
        record = {k: v for k, v in _VALID_RECORD.items() if k != "level"}
        errors = self.module._validate_record(record)
        self.assertTrue(any("level" in e for e in errors))

    def test_missing_module_fails(self):
        record = {k: v for k, v in _VALID_RECORD.items() if k != "module"}
        errors = self.module._validate_record(record)
        self.assertTrue(any("module" in e for e in errors))

    def test_missing_msg_fails(self):
        record = {k: v for k, v in _VALID_RECORD.items() if k != "msg"}
        errors = self.module._validate_record(record)
        self.assertTrue(any("msg" in e for e in errors))

    def test_invalid_level_value_fails(self):
        record = dict(_VALID_RECORD)
        record["level"] = "TRACE"
        errors = self.module._validate_record(record)
        self.assertTrue(any("TRACE" in e for e in errors))

    def test_invalid_ts_string_fails(self):
        record = dict(_VALID_RECORD)
        record["ts"] = "not-a-timestamp"
        errors = self.module._validate_record(record)
        self.assertTrue(any("ISO" in e for e in errors))

    def test_non_string_msg_fails(self):
        record = dict(_VALID_RECORD)
        record["msg"] = 12345
        errors = self.module._validate_record(record)
        self.assertTrue(any("msg" in e for e in errors))


class KoreanPreservationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()

    def test_non_korean_line_returns_none(self):
        record = {"msg": "no korean here"}
        result = self.module._check_korean_preservation(
            '{"msg":"no korean here"}', record,
        )
        self.assertIsNone(result)

    def test_korean_preserved_returns_none(self):
        record = {"msg": "의미 매칭 근거 부족"}
        raw = '{"msg":"의미 매칭 근거 부족"}'
        result = self.module._check_korean_preservation(raw, record)
        self.assertIsNone(result)

    def test_korean_ascii_escaped_returns_error(self):
        record = {"msg": "의미 매칭 근거 부족"}
        # ensure_ascii=True would produce \uXXXX escapes for Hangul.
        raw = (
            '{"msg":"\\uc758\\ubbf8 \\ub9e4\\uce6d \\uadfc\\uac70 \\ubd80\\uc871"}'
        )
        result = self.module._check_korean_preservation(raw, record)
        self.assertIsNotNone(result)
        self.assertIn("ASCII-escaped", result)


# ---------------------------------------------------------------------------
# --local mode with mocked subprocess
# ---------------------------------------------------------------------------


def _build_canned_subprocess(stderr_bytes: bytes, returncode: int = 0):
    """Return a callable suitable for patching ``subprocess.run`` in
    the verifier module."""
    class _Completed:
        def __init__(self):
            self.stdout = b""
            self.stderr = stderr_bytes
            self.returncode = returncode

    def fake_run(*args, **kwargs):
        return _Completed()

    return fake_run


_GOOD_LINES = (
    b'{"ts": "2026-05-23T14:32:01.234567+00:00", '
    b'"level": "INFO", "module": "structured_logging.sample", '
    b'"msg": "This is an INFO message"}\n'
    b'{"ts": "2026-05-23T14:32:01.234600+00:00", '
    b'"level": "WARNING", "module": "structured_logging.sample", '
    b'"msg": "This is a WARNING message"}\n'
    b'{"ts": "2026-05-23T14:32:01.234650+00:00", '
    b'"level": "ERROR", "module": "structured_logging.sample", '
    b'"msg": "This is an ERROR message"}\n'
    b'{"ts": "2026-05-23T14:32:01.234700+00:00", '
    b'"level": "INFO", "module": "structured_logging.sample", '
    b'"msg": "\xec\x9d\x98\xeb\xaf\xb8 \xeb\xa7\xa4\xec\xb9\xad '
    b'\xea\xb7\xbc\xea\xb1\xb0 \xeb\xb6\x80\xec\xa1\xb1"}\n'
)


class LocalModeMockedSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()

    def test_four_valid_json_lines_passes(self):
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(_GOOD_LINES),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local"])
        self.assertEqual(rc, 0)
        self.assertIn("PASS", out)
        self.assertIn("Korean preserved", out)

    def test_invalid_json_line_fails(self):
        bad = _GOOD_LINES + b"this is not json\n"
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(bad),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local"])
        self.assertEqual(rc, 1)
        self.assertIn("INVALID", out)
        self.assertIn("FAIL", out)

    def test_missing_level_fails(self):
        bad = (
            b'{"ts": "2026-05-23T14:32:01.234567+00:00", '
            b'"module": "x", "msg": "no level"}\n'
        )
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(bad),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local"])
        self.assertEqual(rc, 1)
        self.assertIn("level", out)

    def test_empty_stderr_fails(self):
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(b""),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local"])
        self.assertEqual(rc, 1)
        self.assertIn("no stderr lines", out.lower())

    def test_subprocess_nonzero_exit_fails(self):
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(b"some error", returncode=2),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local"])
        self.assertEqual(rc, 1)
        self.assertIn("subprocess exit_code=2", out)

    def test_ascii_escaped_korean_fails(self):
        bad = (
            b'{"ts": "2026-05-23T14:32:01.234700+00:00", '
            b'"level": "INFO", "module": "structured_logging.sample", '
            b'"msg": "\\uc758\\ubbf8 \\ub9e4\\uce6d \\uadfc\\uac70 \\ubd80\\uc871"}\n'
        )
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(bad),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local"])
        self.assertEqual(rc, 1)
        self.assertIn("ASCII-escaped", out)

    def test_json_output_parses(self):
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(_GOOD_LINES),
        ):
            rc, out, _ = _invoke_main(self.module, ["--local", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["mode"], "local")
        self.assertTrue(data["passed"])
        self.assertEqual(data["lines_total"], 4)
        self.assertEqual(len(data["lines"]), 4)


# ---------------------------------------------------------------------------
# --base-url mode with mocked HTTP
# ---------------------------------------------------------------------------


class _FakeUrlOpen:
    """Context-manager that returns a fake HTTP response."""

    def __init__(self, status=200, body=b'{"status":"healthy"}'):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class BaseUrlModeTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()

    def test_health_ok_reports_pass(self):
        with patch.object(
            self.module.urllib.request, "urlopen",
            lambda *a, **k: _FakeUrlOpen(status=200),
        ):
            # Skip smoke to avoid the smoke subprocess in unit tests.
            rc, out, _ = _invoke_main(self.module, [
                "--base-url", "http://fake-render.example.com",
                "--skip-smoke",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("GET /health", out)
        self.assertIn("200 OK", out)
        self.assertIn("LOG_FORMAT=json is set on Render", out)
        self.assertIn(
            "This script does NOT modify Render env vars", out,
        )

    def test_health_failure_exits_one(self):
        def fake_urlopen(*args, **kwargs):
            raise self.module.urllib.error.URLError(
                "Name or service not known",
            )

        with patch.object(
            self.module.urllib.request, "urlopen", fake_urlopen,
        ):
            rc, out, _ = _invoke_main(self.module, [
                "--base-url", "http://unreachable.example.com",
                "--skip-smoke",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("did not return 200", out)

    def test_render_mode_json_output(self):
        with patch.object(
            self.module.urllib.request, "urlopen",
            lambda *a, **k: _FakeUrlOpen(status=200),
        ):
            rc, out, _ = _invoke_main(self.module, [
                "--base-url", "http://fake-render.example.com",
                "--skip-smoke", "--json",
            ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["mode"], "render")
        self.assertTrue(data["passed"])
        self.assertFalse(data["smoke_invoked"])
        self.assertTrue(data["health_ok"])


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------


class StaticShapeTests(unittest.TestCase):
    def setUp(self):
        self.source = (
            _PROJECT_ROOT / "scripts" / "check_json_logging.py"
        ).read_text(encoding="utf-8")

    def test_no_requests_or_httpx_import(self):
        # The script uses stdlib urllib only.
        for needle in ("requests", "httpx"):
            pattern = re.compile(
                rf"^(?:from\s+{re.escape(needle)}\b|import\s+{re.escape(needle)}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=(
                    f"check_json_logging.py must not import "
                    f"{needle!r} -- stdlib only"
                ),
            )

    def test_no_render_api_token_references(self):
        for needle in ("RENDER_API_KEY", "RENDER_SERVICE_ID"):
            self.assertNotIn(
                needle, self.source,
                msg=(
                    f"check_json_logging.py must not reference "
                    f"{needle}"
                ),
            )

    def test_env_var_mutation_only_inside_subprocess_env_dict(self):
        """The verifier may write LOG_FORMAT into a child env dict
        but MUST NOT mutate ``os.environ`` directly (would persist
        after exit)."""
        # ``os.environ[...] =`` or ``os.environ.update(`` would imply
        # process-level env mutation. ``env["LOG_FORMAT"] = "json"``
        # in a local dict is fine.
        forbidden_patterns = (
            r"os\.environ\[",
            r"os\.environ\.update\b",
            r"os\.environ\.setdefault\b",
            r"os\.environ\.pop\b",
        )
        for pat in forbidden_patterns:
            self.assertIsNone(
                re.search(pat, self.source),
                msg=(
                    f"check_json_logging.py must not mutate "
                    f"os.environ (pattern {pat!r})"
                ),
            )


# ---------------------------------------------------------------------------
# Idempotency: the script must NOT leave LOG_FORMAT set in the parent
# env after returning.
# ---------------------------------------------------------------------------


class ParentEnvNotMutatedTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_cli_module()
        import os
        self._snapshot = os.environ.get("LOG_FORMAT")

    def tearDown(self):
        import os
        if self._snapshot is None:
            os.environ.pop("LOG_FORMAT", None)
        else:
            os.environ["LOG_FORMAT"] = self._snapshot

    def test_local_mode_leaves_parent_log_format_unchanged(self):
        import os
        before = os.environ.get("LOG_FORMAT")
        with patch.object(
            self.module.subprocess, "run",
            _build_canned_subprocess(_GOOD_LINES),
        ):
            _invoke_main(self.module, ["--local"])
        after = os.environ.get("LOG_FORMAT")
        self.assertEqual(
            before, after,
            msg="Parent process LOG_FORMAT was mutated by --local mode",
        )


if __name__ == "__main__":
    unittest.main()
