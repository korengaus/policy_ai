"""Tests for the M14.0a structured logging module + CLI.

Run with: python tests/test_structured_logging.py

No real network. Tests use ``io.StringIO`` to capture stderr where
needed; env-var changes are scoped via :class:`_EnvScope`. Static
checks pin the module-adoption contract for the 10 M13.x modules and
the legacy-isolation contract for the 16 untouched files.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import logging
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import structured_logging  # noqa: E402


# ---------------------------------------------------------------------------
# Env-scope helper + per-test reset
# ---------------------------------------------------------------------------


class _EnvScope:
    KEYS = ("LOG_FORMAT", "LOG_LEVEL")

    def __enter__(self):
        self._snap = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        structured_logging.reset_for_tests()


def _set_env(**values):
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------


class EnvParsingTests(unittest.TestCase):
    def test_log_format_default_is_text(self):
        with _EnvScope():
            _set_env(LOG_FORMAT=None)
            self.assertEqual(structured_logging.get_log_format(), "text")

    def test_log_format_explicit_text(self):
        for value in ("text", "TEXT", "TeXt", "  text  "):
            with _EnvScope():
                _set_env(LOG_FORMAT=value)
                self.assertEqual(
                    structured_logging.get_log_format(), "text",
                    msg=f"value={value!r} should map to text",
                )

    def test_log_format_json_case_insensitive(self):
        for value in ("json", "JSON", "Json", "  json  "):
            with _EnvScope():
                _set_env(LOG_FORMAT=value)
                self.assertEqual(
                    structured_logging.get_log_format(), "json",
                    msg=f"value={value!r} should map to json",
                )

    def test_log_format_other_values_default_to_text(self):
        for value in ("xml", "yaml", "json\n\n", "json;DROP TABLE",
                      "1", "true", "Bear"):
            with _EnvScope():
                _set_env(LOG_FORMAT=value)
                self.assertEqual(
                    structured_logging.get_log_format(), "text",
                    msg=f"value={value!r} should default to text",
                )

    def test_log_level_default_is_info(self):
        with _EnvScope():
            _set_env(LOG_LEVEL=None)
            self.assertEqual(
                structured_logging.get_log_level(), logging.INFO,
            )

    def test_log_level_debug(self):
        with _EnvScope():
            _set_env(LOG_LEVEL="DEBUG")
            self.assertEqual(
                structured_logging.get_log_level(), logging.DEBUG,
            )

    def test_log_level_case_insensitive(self):
        with _EnvScope():
            _set_env(LOG_LEVEL="warning")
            self.assertEqual(
                structured_logging.get_log_level(), logging.WARNING,
            )

    def test_log_level_invalid_falls_back_to_info(self):
        for value in ("TRACE", "verbose", "9", "info-ish"):
            with _EnvScope():
                _set_env(LOG_LEVEL=value)
                self.assertEqual(
                    structured_logging.get_log_level(), logging.INFO,
                    msg=f"value={value!r} should fall back to INFO",
                )


# ---------------------------------------------------------------------------
# configure_logging idempotency + handler management
# ---------------------------------------------------------------------------


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        structured_logging.reset_for_tests()

    def tearDown(self):
        structured_logging.reset_for_tests()

    def test_idempotent_calls(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="text")
            structured_logging.configure_logging()
            structured_logging.configure_logging()
            structured_logging.configure_logging()
            health = structured_logging.health_check()
            self.assertEqual(health["managed_handler_count"], 1)

    def test_force_reconfigure_replaces_managed_handler(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="text")
            structured_logging.configure_logging()
            first_handlers = [
                h for h in logging.getLogger().handlers
                if getattr(h, "_m14_managed", False)
            ]
            structured_logging.configure_logging(force=True)
            after_handlers = [
                h for h in logging.getLogger().handlers
                if getattr(h, "_m14_managed", False)
            ]
            self.assertEqual(len(first_handlers), 1)
            self.assertEqual(len(after_handlers), 1)
            # The handler instance should be different after force.
            self.assertIsNot(first_handlers[0], after_handlers[0])

    def test_preserves_non_managed_handlers(self):
        """A test-runner handler added by pytest or another tool MUST
        survive configure_logging calls."""
        third_party = logging.StreamHandler(stream=io.StringIO())
        # Deliberately NOT tagged with _m14_managed.
        root = logging.getLogger()
        root.addHandler(third_party)
        try:
            structured_logging.configure_logging()
            structured_logging.configure_logging(force=True)
            # The third-party handler should still be present.
            self.assertIn(third_party, root.handlers)
            managed = [
                h for h in root.handlers
                if getattr(h, "_m14_managed", False)
            ]
            self.assertEqual(len(managed), 1)
        finally:
            root.removeHandler(third_party)

    def test_reset_clears_managed_only(self):
        third_party = logging.StreamHandler(stream=io.StringIO())
        root = logging.getLogger()
        root.addHandler(third_party)
        try:
            structured_logging.configure_logging()
            structured_logging.reset_for_tests()
            # Managed gone; third-party stays.
            self.assertIn(third_party, root.handlers)
            managed = [
                h for h in root.handlers
                if getattr(h, "_m14_managed", False)
            ]
            self.assertEqual(len(managed), 0)
            health = structured_logging.health_check()
            self.assertFalse(health["configured"])
        finally:
            root.removeHandler(third_party)

    def test_configure_does_not_raise_on_bad_env(self):
        # Null bytes cannot be set via os.environ on Windows -- they
        # raise ValueError at the env-var assignment site, before our
        # code is reached. Stick to inputs that are actually
        # representable as environment values.
        for value in ("json\n\n", "json;DROP TABLE", "  garbage  "):
            with _EnvScope():
                _set_env(LOG_FORMAT=value)
                try:
                    structured_logging.configure_logging(force=True)
                except Exception as exc:  # noqa: BLE001
                    self.fail(
                        f"configure_logging raised on "
                        f"LOG_FORMAT={value!r}: {exc!r}"
                    )


# ---------------------------------------------------------------------------
# JSON output shape
# ---------------------------------------------------------------------------


def _emit_and_capture(logger_name: str, fn) -> list:
    """Configure logging fresh, invoke ``fn(log)`` with a logger, and
    return the captured stderr split into non-empty lines."""
    structured_logging.reset_for_tests()
    structured_logging.configure_logging(force=True)
    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler._m14_managed = True  # type: ignore[attr-defined]
    if structured_logging.is_json_logging_enabled():
        handler.setFormatter(structured_logging.JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        ))
    # Swap in OUR capturing handler at root.
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_m14_managed", False):
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(structured_logging.get_log_level())
    try:
        log = logging.getLogger(logger_name)
        fn(log)
    finally:
        root.removeHandler(handler)
    return [line for line in buf.getvalue().splitlines() if line.strip()]


class JsonOutputShapeTests(unittest.TestCase):
    def tearDown(self):
        structured_logging.reset_for_tests()

    def test_json_record_has_required_keys(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            lines = _emit_and_capture(
                "policy_ai.test.shape",
                lambda log: log.info("hello"),
            )
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertSetEqual(
                set(payload.keys()),
                {"ts", "level", "module", "msg"},
            )
            self.assertEqual(payload["level"], "INFO")
            self.assertEqual(payload["msg"], "hello")
            self.assertEqual(payload["module"], "policy_ai.test.shape")
            # ISO 8601 timestamp — fromisoformat parses it.
            from datetime import datetime
            datetime.fromisoformat(payload["ts"])

    def test_text_record_is_not_json(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="text")
            lines = _emit_and_capture(
                "policy_ai.test.text",
                lambda log: log.warning("warning text"),
            )
            self.assertEqual(len(lines), 1)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(lines[0])
            self.assertIn("WARNING", lines[0])
            self.assertIn("warning text", lines[0])

    def test_extras_serialized_to_extra_key(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            lines = _emit_and_capture(
                "policy_ai.test.extras",
                lambda log: log.info(
                    "judge action",
                    extra={"foo": "bar", "n": 42},
                ),
            )
            payload = json.loads(lines[0])
            self.assertEqual(payload["extra"]["foo"], "bar")
            self.assertEqual(payload["extra"]["n"], 42)

    def test_unserializable_extra_falls_back_to_repr(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")

            class _Weird:
                def __repr__(self):
                    return "<weird repr>"

            lines = _emit_and_capture(
                "policy_ai.test.unser",
                lambda log: log.info("event", extra={"obj": _Weird()}),
            )
            payload = json.loads(lines[0])
            self.assertEqual(payload["extra"]["obj"], "<weird repr>")

    def test_korean_text_preserved_as_utf8(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            lines = _emit_and_capture(
                "policy_ai.test.korean",
                lambda log: log.info("의미 매칭 근거 부족"),
            )
            payload = json.loads(lines[0])
            self.assertEqual(payload["msg"], "의미 매칭 근거 부족")
            # No \u-escaped form in the raw line.
            self.assertNotIn("\\u", lines[0])

    # ------------------------------------------------------------------
    # M14.3a — request_id field appears when ContextVar is set, omitted
    # otherwise. Backward-compat pin against the M14.0a JSON shape.
    # ------------------------------------------------------------------

    def test_request_id_omitted_when_context_unset(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            import request_context

            request_context.clear_request_id()
            lines = _emit_and_capture(
                "policy_ai.test.rid_omitted",
                lambda log: log.info("plain"),
            )
            payload = json.loads(lines[0])
            self.assertNotIn(
                "request_id", payload,
                msg=(
                    "JsonFormatter must omit request_id when the "
                    "ContextVar is unset (M14.3a backward compat)."
                ),
            )

    def test_request_id_included_when_context_set(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            import request_context

            def emit(log):
                with request_context.request_id_scope("rid-shape-test"):
                    log.info("inside scope")

            lines = _emit_and_capture(
                "policy_ai.test.rid_included", emit,
            )
            payload = json.loads(lines[0])
            self.assertEqual(
                payload.get("request_id"), "rid-shape-test",
            )
            # Korean preservation must survive the new field too.
            self.assertNotIn("\\u", lines[0])

    def test_exception_serialized_to_exc_key(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")

            def emit(log):
                try:
                    raise RuntimeError("boom")
                except RuntimeError:
                    log.exception("caught")

            lines = _emit_and_capture("policy_ai.test.exc", emit)
            payload = json.loads(lines[0])
            self.assertIn("exc", payload)
            self.assertIn("RuntimeError", payload["exc"])
            self.assertIn("boom", payload["exc"])

    def test_no_exc_key_for_normal_record(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            lines = _emit_and_capture(
                "policy_ai.test.noexc",
                lambda log: log.info("ordinary"),
            )
            payload = json.loads(lines[0])
            self.assertNotIn("exc", payload)

    def test_message_with_args_renders(self):
        with _EnvScope():
            _set_env(LOG_FORMAT="json")
            lines = _emit_and_capture(
                "policy_ai.test.args",
                lambda log: log.info("count=%d host=%s", 42, "a.b"),
            )
            payload = json.loads(lines[0])
            self.assertEqual(payload["msg"], "count=42 host=a.b")


# ---------------------------------------------------------------------------
# get_logger lookup
# ---------------------------------------------------------------------------


class GetLoggerTests(unittest.TestCase):
    def setUp(self):
        structured_logging.reset_for_tests()

    def tearDown(self):
        structured_logging.reset_for_tests()

    def test_returns_logging_logger(self):
        log = structured_logging.get_logger("policy_ai.test.x")
        self.assertIsInstance(log, logging.Logger)

    def test_same_instance_per_name(self):
        a = structured_logging.get_logger("policy_ai.test.same")
        b = logging.getLogger("policy_ai.test.same")
        self.assertIs(a, b)

    def test_configures_on_first_call(self):
        structured_logging.reset_for_tests()
        self.assertFalse(structured_logging.health_check()["configured"])
        structured_logging.get_logger("policy_ai.test.bootstrap")
        self.assertTrue(structured_logging.health_check()["configured"])


# ---------------------------------------------------------------------------
# Module adoption pin — the key contract for M14.0a.
# ---------------------------------------------------------------------------


_ADOPTED_MODULES = (
    "llm_judge.py",
    "http_cache.py",
    "postgres_storage.py",
    "postgres_backfill.py",
    "legacy_review_enrollment.py",
    "verdict_label_diagnostic.py",
    "verdict_producer_comparison.py",
    "artifact_extractor.py",
    "artifact_evidence_linker.py",
    "source_crawler.py",
)


def _module_assigns_logger_via_get_logger(source: str) -> tuple:
    """Returns (imports_get_logger, uses_get_logger_for_module_logger).
    Both must be True for the adoption pin to pass."""
    tree = ast.parse(source)
    imports_get_logger = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "structured_logging"
        ):
            for alias in node.names:
                if alias.name == "get_logger":
                    imports_get_logger = True
                    break
    uses_get_logger_for_module_logger = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [
            t.id for t in node.targets if isinstance(t, ast.Name)
        ]
        if not any(t in ("log", "logger") for t in targets):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and func.id == "get_logger":
            uses_get_logger_for_module_logger = True
            break
    return imports_get_logger, uses_get_logger_for_module_logger


class ModuleAdoptionPin(unittest.TestCase):
    """The contract for M14.0a: each adopted module imports
    ``get_logger`` from ``structured_logging`` AND assigns its
    module-level ``log`` / ``logger`` from a call to ``get_logger``."""

    def test_all_ten_modules_adopt_get_logger(self):
        offenders_no_import = []
        offenders_no_init = []
        for filename in _ADOPTED_MODULES:
            path = _PROJECT_ROOT / filename
            self.assertTrue(
                path.exists(), msg=f"missing module: {filename}",
            )
            source = path.read_text(encoding="utf-8")
            imports_ok, init_ok = (
                _module_assigns_logger_via_get_logger(source)
            )
            if not imports_ok:
                offenders_no_import.append(filename)
            if not init_ok:
                offenders_no_init.append(filename)
        self.assertFalse(
            offenders_no_import,
            msg=(
                "Adopted modules that fail to import get_logger: "
                f"{offenders_no_import}"
            ),
        )
        self.assertFalse(
            offenders_no_init,
            msg=(
                "Adopted modules whose module-level logger is NOT "
                f"assigned via get_logger(...): {offenders_no_init}"
            ),
        )


# ---------------------------------------------------------------------------
# Legacy isolation pin — these files MUST NOT import structured_logging.
# ---------------------------------------------------------------------------


# M14.0c completed migration of the remaining 8 files originally
# listed by M14.0a. Every print-bearing module from the original
# inventory now imports structured_logging. The legacy list now
# contains only the still-untouched pipeline / storage modules
# that never had print()s in the first place (so M14.0a/b/c had
# no reason to migrate them). They remain pinned here as a guard
# against any future PR adding the import without a clear
# justification.
_LEGACY_FILES = (
    "api_server.py",
    "policy_scoring.py",
    "database.py",
    "ai_reasoner.py",
    "job_manager.py",
)


class LegacyIsolationPin(unittest.TestCase):
    """Legacy modules must not adopt the new helper in M14.0a — that's
    M14.0b's job. Static-text scan keeps the contract cheap."""

    def test_no_legacy_module_imports_structured_logging(self):
        forbidden = re.compile(
            r"^(?:from\s+structured_logging\b|import\s+structured_logging\b)",
            re.MULTILINE,
        )
        offenders = []
        for filename in _LEGACY_FILES:
            path = _PROJECT_ROOT / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if forbidden.search(text):
                offenders.append(filename)
        self.assertFalse(
            offenders,
            msg=(
                f"Legacy modules adopted structured_logging in M14.0a "
                f"(should be left for M14.0b): {offenders}"
            ),
        )


class PrintMigrationCompletionPin(unittest.TestCase):
    """Post-M14.0c: every file originally listed by the M14.0a print
    inventory has been migrated. ``official_source_body.py`` is the
    smallest file in that inventory (1 print, migrated to 1
    log.error) — if it ever regrows a print(), this pin surfaces it
    immediately.

    The M14.0a version of this pin asserted ``count > 0`` because
    M14.0a deliberately left every print alone. M14.0b/c then
    migrated all 251 prints across 13 files. The contract for
    M14.0c onwards is that legacy print-bearing modules stay at
    zero prints — flipped here under the migration-completion
    contract.
    """

    def test_official_source_body_has_no_remaining_prints(self):
        source = (
            _PROJECT_ROOT / "official_source_body.py"
        ).read_text(encoding="utf-8")
        # AST-level count: tokenize-level ``print(`` would also catch
        # string literals containing ``print(``; AST avoids false
        # positives.
        tree = ast.parse(source)
        prints = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        )
        self.assertEqual(
            prints, 0,
            msg=(
                "official_source_body.py has print() calls after "
                "M14.0c completion -- migration regressed."
            ),
        )


# ---------------------------------------------------------------------------
# Static checks on structured_logging.py
# ---------------------------------------------------------------------------


class ModuleLevelStaticChecks(unittest.TestCase):
    def setUp(self):
        self.module_path = _PROJECT_ROOT / "structured_logging.py"
        self.source = self.module_path.read_text(encoding="utf-8")

    def test_stdlib_only(self):
        forbidden = (
            "sentry_sdk", "datadog", "ddtrace", "structlog",
            "loguru", "requests", "httpx", "fastapi", "sqlalchemy",
            "openai", "anthropic",
        )
        for name in forbidden:
            pattern = re.compile(
                rf"^(?:from\s+{name}\b|import\s+{name}\b)",
                re.MULTILINE,
            )
            self.assertIsNone(
                pattern.search(self.source),
                msg=f"structured_logging.py must not import {name}",
            )


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "check_logging_cli",
        str(_PROJECT_ROOT / "scripts" / "check_logging.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliTests(unittest.TestCase):
    def _run_cli(self, argv):
        module = _load_cli_module()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = out_buf, err_buf
            rc = module.main(argv)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def setUp(self):
        structured_logging.reset_for_tests()

    def tearDown(self):
        structured_logging.reset_for_tests()
        os.environ.pop("LOG_FORMAT", None)
        os.environ.pop("LOG_LEVEL", None)

    def test_help_exits_zero(self):
        rc, out, _ = self._run_cli(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("check_logging", out)
        self.assertIn("Exit codes", out)

    def test_status_text_mode(self):
        os.environ.pop("LOG_FORMAT", None)
        rc, out, _ = self._run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("LOG_FORMAT:         text", out)

    def test_status_json_mode(self):
        os.environ["LOG_FORMAT"] = "json"
        rc, out, _ = self._run_cli([])
        self.assertEqual(rc, 0)
        self.assertIn("LOG_FORMAT:         json", out)

    def test_status_json_output_is_parseable(self):
        os.environ.pop("LOG_FORMAT", None)
        rc, out, _ = self._run_cli(["--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["log_format"], "text")
        self.assertIn("safety", data)
        self.assertFalse(data["safety"]["print_calls_replaced"])

    def test_emit_sample_text_mode(self):
        os.environ.pop("LOG_FORMAT", None)
        rc, out, err = self._run_cli(["--emit-sample"])
        self.assertEqual(rc, 0)
        self.assertIn("Sample log emission", out)
        # Each level should appear in stderr lines.
        for level_token in ("INFO", "WARNING", "ERROR"):
            self.assertIn(level_token, err)

    def test_emit_sample_json_mode_lines_parse(self):
        os.environ["LOG_FORMAT"] = "json"
        rc, out, err = self._run_cli(["--emit-sample"])
        self.assertEqual(rc, 0)
        lines = [l for l in err.splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 4)
        for line in lines:
            payload = json.loads(line)
            self.assertIn("level", payload)
            self.assertIn("msg", payload)

    def test_emit_sample_with_extra_json_contains_extra(self):
        os.environ["LOG_FORMAT"] = "json"
        rc, out, err = self._run_cli(["--emit-sample-with-extra"])
        self.assertEqual(rc, 0)
        self.assertIn('"extra"', err)
        # The first emitted record carries action=confirm.
        first_line = next(
            (l for l in err.splitlines() if l.strip()), None,
        )
        payload = json.loads(first_line)
        self.assertIn("extra", payload)
        self.assertEqual(payload["extra"]["action"], "confirm")


if __name__ == "__main__":
    unittest.main()
