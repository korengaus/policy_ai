"""Tests for the M14.3a request_context module.

Run with: python tests/test_request_context.py

Covers basic get/set/reset, the ``request_id_scope`` context
manager (including exception-safe restore + nesting), async safety
via ``asyncio.gather`` with two concurrent tasks, and the integration
with ``structured_logging.JsonFormatter``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import sys
import threading
import unittest
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import request_context  # noqa: E402
import structured_logging  # noqa: E402


# ---------------------------------------------------------------------------
# Basic get / set / reset / clear
# ---------------------------------------------------------------------------


class BasicOperationsTests(unittest.TestCase):
    def setUp(self):
        # Each test starts with no request_id (the ContextVar default).
        # We use a fresh scope so any prior test leakage is contained.
        self._scope_token = request_context.set_request_id(None)

    def tearDown(self):
        request_context.reset_request_id(self._scope_token)

    def test_default_is_none(self):
        self.assertIsNone(request_context.get_request_id())

    def test_set_then_get_returns_value(self):
        token = request_context.set_request_id("abc12345")
        try:
            self.assertEqual(request_context.get_request_id(), "abc12345")
        finally:
            request_context.reset_request_id(token)

    def test_reset_restores_previous(self):
        token1 = request_context.set_request_id("first")
        token2 = request_context.set_request_id("second")
        try:
            self.assertEqual(request_context.get_request_id(), "second")
            request_context.reset_request_id(token2)
            self.assertEqual(request_context.get_request_id(), "first")
        finally:
            request_context.reset_request_id(token1)

    def test_clear_writes_none(self):
        token = request_context.set_request_id("something")
        try:
            self.assertEqual(
                request_context.get_request_id(), "something",
            )
            request_context.clear_request_id()
            self.assertIsNone(request_context.get_request_id())
        finally:
            request_context.reset_request_id(token)


# ---------------------------------------------------------------------------
# new_request_id properties
# ---------------------------------------------------------------------------


class NewRequestIdTests(unittest.TestCase):
    def test_returns_nonempty_string(self):
        rid = request_context.new_request_id()
        self.assertIsInstance(rid, str)
        self.assertGreater(len(rid), 0)

    def test_returns_12_hex_chars(self):
        rid = request_context.new_request_id()
        self.assertEqual(len(rid), 12)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{12}", rid))

    def test_unique_across_calls(self):
        ids = {request_context.new_request_id() for _ in range(1000)}
        # 12 hex chars = 48 bits. 1000 IDs should never collide.
        self.assertEqual(len(ids), 1000)


# ---------------------------------------------------------------------------
# request_id_scope context manager
# ---------------------------------------------------------------------------


class RequestIdScopeTests(unittest.TestCase):
    def setUp(self):
        request_context.clear_request_id()

    def tearDown(self):
        request_context.clear_request_id()

    def test_scope_yields_request_id(self):
        with request_context.request_id_scope() as rid:
            self.assertIsNotNone(rid)
            self.assertIsInstance(rid, str)
            self.assertEqual(request_context.get_request_id(), rid)

    def test_scope_restores_none_on_exit(self):
        with request_context.request_id_scope():
            self.assertIsNotNone(request_context.get_request_id())
        self.assertIsNone(request_context.get_request_id())

    def test_scope_restores_previous_on_exit(self):
        token = request_context.set_request_id("outer-id")
        try:
            with request_context.request_id_scope("inner-id") as rid:
                self.assertEqual(rid, "inner-id")
                self.assertEqual(
                    request_context.get_request_id(), "inner-id",
                )
            self.assertEqual(
                request_context.get_request_id(), "outer-id",
            )
        finally:
            request_context.reset_request_id(token)

    def test_scope_restores_on_exception(self):
        with self.assertRaises(RuntimeError):
            with request_context.request_id_scope("error-id"):
                self.assertEqual(
                    request_context.get_request_id(), "error-id",
                )
                raise RuntimeError("boom")
        self.assertIsNone(request_context.get_request_id())

    def test_explicit_request_id_used(self):
        with request_context.request_id_scope("my-custom-id") as rid:
            self.assertEqual(rid, "my-custom-id")
            self.assertEqual(
                request_context.get_request_id(), "my-custom-id",
            )

    def test_nested_scopes_isolate(self):
        with request_context.request_id_scope("outer") as outer:
            self.assertEqual(outer, "outer")
            with request_context.request_id_scope("inner") as inner:
                self.assertEqual(inner, "inner")
                self.assertEqual(
                    request_context.get_request_id(), "inner",
                )
            self.assertEqual(
                request_context.get_request_id(), "outer",
            )
        self.assertIsNone(request_context.get_request_id())


# ---------------------------------------------------------------------------
# Async safety — the killer test for contextvars correctness.
# ---------------------------------------------------------------------------


class AsyncSafetyTests(unittest.TestCase):
    def setUp(self):
        request_context.clear_request_id()

    def tearDown(self):
        request_context.clear_request_id()

    def test_concurrent_async_tasks_have_isolated_ids(self):
        observations = {}

        async def task(name: str, rid: str) -> None:
            # Set in a fresh scope so each task starts isolated.
            with request_context.request_id_scope(rid):
                # Yield to the event loop a few times — this forces
                # interleaving between the two tasks and is the
                # condition under which threading.local would fail.
                for _ in range(5):
                    await asyncio.sleep(0)
                observations[name] = request_context.get_request_id()

        async def main():
            await asyncio.gather(
                task("alpha", "rid-alpha-1234"),
                task("beta", "rid-beta-5678"),
            )

        asyncio.run(main())

        self.assertEqual(observations["alpha"], "rid-alpha-1234")
        self.assertEqual(observations["beta"], "rid-beta-5678")
        # Outer (test runner) context never inherited either ID.
        self.assertIsNone(request_context.get_request_id())

    def test_async_task_does_not_leak_to_parent(self):
        async def task():
            with request_context.request_id_scope("child-id"):
                self.assertEqual(
                    request_context.get_request_id(), "child-id",
                )

        async def main():
            self.assertIsNone(request_context.get_request_id())
            await task()
            # Parent context unaffected.
            self.assertIsNone(request_context.get_request_id())

        asyncio.run(main())

    def test_threading_isolation(self):
        """contextvars are also thread-local: each OS thread has its
        own ContextVar slot."""
        observations = {}

        def worker(name: str, rid: str) -> None:
            with request_context.request_id_scope(rid):
                observations[name] = request_context.get_request_id()

        threads = [
            threading.Thread(target=worker, args=(f"t{i}", f"rid-t{i}-9999"))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(5):
            self.assertEqual(observations[f"t{i}"], f"rid-t{i}-9999")
        # Main thread context unaffected.
        self.assertIsNone(request_context.get_request_id())


# ---------------------------------------------------------------------------
# JSON formatter integration
# ---------------------------------------------------------------------------


def _capture_json_log(name: str, fn) -> list:
    """Configure a fresh JSON-mode root logger handler, invoke
    ``fn(log)`` with a child logger, and return the captured
    stderr-style lines."""
    structured_logging.reset_for_tests()
    structured_logging.configure_logging(force=True)
    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler._m14_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(structured_logging.JsonFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_m14_managed", False):
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        log = logging.getLogger(name)
        fn(log)
    finally:
        root.removeHandler(handler)
    return [line for line in buf.getvalue().splitlines() if line.strip()]


class JsonFormatterIntegrationTests(unittest.TestCase):
    def setUp(self):
        request_context.clear_request_id()

    def tearDown(self):
        request_context.clear_request_id()
        structured_logging.reset_for_tests()

    def test_no_request_id_field_when_context_unset(self):
        lines = _capture_json_log(
            "m14_3a.test.unset",
            lambda log: log.info("no request id"),
        )
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertNotIn(
            "request_id", payload,
            msg=(
                "JsonFormatter must omit request_id entirely when "
                "the ContextVar is unset (backward compatibility)."
            ),
        )

    def test_request_id_field_appears_when_context_set(self):
        def emit(log):
            with request_context.request_id_scope("rid-format-test"):
                log.info("inside scope")

        lines = _capture_json_log("m14_3a.test.set", emit)
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload.get("request_id"), "rid-format-test")

    def test_request_id_field_position_after_module_before_msg(self):
        """The JSON dump preserves insertion order. With M14.3a's
        placement, ``request_id`` should appear between ``module``
        and ``msg``. Operators reading the raw stream can find it
        without searching."""
        def emit(log):
            with request_context.request_id_scope("rid-position"):
                log.info("position test")

        lines = _capture_json_log("m14_3a.test.position", emit)
        raw = lines[0]
        # Use index-based ordering to assert the field positions.
        idx_module = raw.index('"module"')
        idx_rid = raw.index('"request_id"')
        idx_msg = raw.index('"msg"')
        self.assertLess(idx_module, idx_rid)
        self.assertLess(idx_rid, idx_msg)

    def test_korean_text_still_utf8_with_request_id(self):
        def emit(log):
            with request_context.request_id_scope("rid-korean"):
                log.info("의미 매칭 근거 부족")

        lines = _capture_json_log("m14_3a.test.korean", emit)
        raw = lines[0]
        self.assertNotIn("\\u", raw)
        self.assertIn("의미 매칭 근거 부족", raw)
        payload = json.loads(raw)
        self.assertEqual(payload.get("request_id"), "rid-korean")
        self.assertEqual(payload.get("msg"), "의미 매칭 근거 부족")


# ---------------------------------------------------------------------------
# Backward compatibility: existing JSON shape unchanged when context unset.
# ---------------------------------------------------------------------------


class BackwardCompatibilityPin(unittest.TestCase):
    """The M14.0a JSON shape (ts, level, module, msg, optional extra,
    optional exc) must be preserved when no request_id is set. This
    pin prevents a future change that accidentally injects an empty
    or null request_id field."""

    def setUp(self):
        request_context.clear_request_id()

    def tearDown(self):
        request_context.clear_request_id()
        structured_logging.reset_for_tests()

    def test_shape_identical_to_m14_0a_when_no_rid(self):
        lines = _capture_json_log(
            "m14_3a.test.backcompat",
            lambda log: log.info("plain"),
        )
        payload = json.loads(lines[0])
        self.assertSetEqual(
            set(payload.keys()),
            {"ts", "level", "module", "msg"},
            msg=(
                "Without a request_id set, JSON output must have "
                "exactly ts/level/module/msg keys (no extras)."
            ),
        )

    def test_extras_still_serialized_when_rid_set(self):
        def emit(log):
            with request_context.request_id_scope("rid-extras"):
                log.info("with extras", extra={"foo": "bar"})

        lines = _capture_json_log("m14_3a.test.extras", emit)
        payload = json.loads(lines[0])
        self.assertEqual(payload["request_id"], "rid-extras")
        self.assertEqual(payload["extra"]["foo"], "bar")


if __name__ == "__main__":
    unittest.main()
