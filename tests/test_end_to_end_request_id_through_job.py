"""M14.3b — End-to-end request_id propagation pin.

Simulates the full hot path:

  HTTP request (X-Request-ID header)
    -> api_server middleware sets request_id ContextVar
    -> handler submits a job via the M14.3b context-aware helpers
    -> worker runs in a *different* thread
    -> worker emits a log line
    -> JsonFormatter writes a JSON record
    -> the JSON record contains the originating request's request_id

The point of this test is to catch any plumbing regression that
would silently drop request_id between the HTTP middleware and the
worker thread's log output. We do not exercise the real
``analyze_pipeline`` — that's covered by other suites and would
require live AI/network. We exercise the *context propagation* path
end-to-end.

Run with: ``python tests/test_end_to_end_request_id_through_job.py``
"""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import job_manager  # noqa: E402
import request_context  # noqa: E402
import structured_logging  # noqa: E402


# ---------------------------------------------------------------------------
# Harness: install a JsonFormatter on a captured handler so log calls
# made anywhere in the test produce JSON records we can inspect.
# ---------------------------------------------------------------------------


class _JsonCaptureHandler(logging.Handler):
    """Captures log records' JSON output without writing to stderr."""

    def __init__(self):
        super().__init__()
        self.setFormatter(structured_logging.JsonFormatter())
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock:
            self.lines.append(line)

    def parsed_records(self) -> list[dict]:
        out = []
        with self._lock:
            for line in self.lines:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out


def _install_capture(logger_name: str) -> _JsonCaptureHandler:
    handler = _JsonCaptureHandler()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


def _uninstall_capture(logger_name: str, handler: logging.Handler) -> None:
    logger = logging.getLogger(logger_name)
    logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Simulated middleware + handler + worker — the full M14.3a → M14.3b path
# without the FastAPI / asyncio overhead.
# ---------------------------------------------------------------------------


def _simulate_request_handler(
    incoming_request_id: str,
    pool: ThreadPoolExecutor,
    worker_logger_name: str,
):
    """Re-create what api_server.py does for /jobs/analyze:

    1. Middleware sets request_id (M14.3a).
    2. Handler queues work on a thread pool, capturing context (M14.3b).
    3. Handler returns the future.

    The middleware-reset step is handled by ``request_id_scope`` so
    the caller's request_id is restored on scope exit.
    """
    def _worker():
        # Worker is in a different thread. If propagation works,
        # its get_request_id() returns the value the middleware set.
        log = logging.getLogger(worker_logger_name)
        observed_rid = request_context.get_request_id()
        log.info("worker emitted log — observed rid=%s", observed_rid)
        return observed_rid

    with request_context.request_id_scope(incoming_request_id):
        future = job_manager.submit_in_context(pool, _worker)
    return future


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class EndToEndPropagationTests(unittest.TestCase):
    """The point pin: a request_id set by the middleware appears in
    JSON log lines emitted by the worker thread."""

    LOGGER_NAME = "m14_3b.e2e.worker"

    def setUp(self):
        self._token = request_context.set_request_id(None)
        self.handler = _install_capture(self.LOGGER_NAME)

    def tearDown(self):
        _uninstall_capture(self.LOGGER_NAME, self.handler)
        request_context.reset_request_id(self._token)

    def test_rid_appears_in_worker_log_line(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = _simulate_request_handler(
                "e2e-test-abc123", pool, self.LOGGER_NAME,
            )
            observed_rid_in_worker = future.result(timeout=5.0)

        self.assertEqual(observed_rid_in_worker, "e2e-test-abc123")

        records = self.handler.parsed_records()
        self.assertGreaterEqual(len(records), 1)
        for record in records:
            self.assertEqual(
                record.get("request_id"),
                "e2e-test-abc123",
                f"Worker JSON log line missing or wrong rid: {record!r}",
            )

    def test_two_concurrent_requests_logs_isolated(self):
        """Two HTTP requests arrive simultaneously. Their workers run
        on a shared pool. Each worker's JSON log must carry only its
        OWN request_id — no cross-pollination."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = _simulate_request_handler("req-A-aaaa", pool, self.LOGGER_NAME)
            f2 = _simulate_request_handler("req-B-bbbb", pool, self.LOGGER_NAME)
            f1.result(timeout=5.0)
            f2.result(timeout=5.0)

        records = self.handler.parsed_records()
        # Each worker emits exactly one log; we expect 2 records.
        self.assertEqual(
            len(records), 2,
            f"Expected 2 worker log records, got {len(records)}",
        )
        observed_rids = sorted(r.get("request_id") for r in records)
        self.assertEqual(observed_rids, ["req-A-aaaa", "req-B-bbbb"])

    def test_no_rid_at_request_time_no_rid_in_log(self):
        """A code path with no request_id (scheduler.py, CLI) must
        still produce log records — just without the request_id key.
        This is the backward-compat path."""
        def worker():
            log = logging.getLogger(self.LOGGER_NAME)
            log.info("worker without rid")

        # Run with NO scope wrapping — request_id is None.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = job_manager.submit_in_context(pool, worker)
            future.result(timeout=5.0)

        records = self.handler.parsed_records()
        self.assertEqual(len(records), 1)
        self.assertNotIn(
            "request_id", records[0],
            f"Expected no request_id key when none set; got {records[0]!r}",
        )


class MiddlewareSimulationResetTests(unittest.TestCase):
    """The middleware sets a rid, the handler submits, the middleware
    resets — at no point should the caller's request_id be observable
    *after* the with-block exits."""

    LOGGER_NAME = "m14_3b.e2e.middleware_reset"

    def setUp(self):
        self._token = request_context.set_request_id(None)
        self.handler = _install_capture(self.LOGGER_NAME)

    def tearDown(self):
        _uninstall_capture(self.LOGGER_NAME, self.handler)
        request_context.reset_request_id(self._token)

    def test_rid_reset_after_scope_exit_in_caller(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = _simulate_request_handler(
                "rid-scoped-only", pool, self.LOGGER_NAME,
            )
            self.assertIsNone(
                request_context.get_request_id(),
                "Caller's rid should be None after middleware scope exit.",
            )
            future.result(timeout=5.0)

        # Worker still logged the correct rid.
        records = self.handler.parsed_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], "rid-scoped-only")


if __name__ == "__main__":
    unittest.main()
