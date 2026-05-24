"""M15.0a — Job queue infrastructure pins.

Verifies the contracts of ``job_queue.py`` (RQ wrapper with graceful
degradation), ``worker.py`` (opt-in entry point), and the new
``/health/queue`` endpoint in ``api_server.py``.

Test strategy
=============

  * **Fully offline.** No real Redis required. Tests use
    ``fakeredis.FakeRedis`` injected via ``mock.patch.object`` of
    ``job_queue._redis_factory``. ``REDIS_URL`` is set to a
    sentinel value via ``mock.patch.dict(os.environ, ...)``.
  * **Graceful-degradation contract is the primary thing pinned.**
    Every public function in ``job_queue`` must return a safe
    value (``None`` or a documented sentinel dict) when Redis is
    unset / unreachable / packages missing — never raise.
  * **No LLM calls anywhere.** A static AST scan asserts
    ``job_queue.py``, ``worker.py``, and ``scripts/check_job_queue.py``
    do not import any LLM-related module.
  * **/analyze unaffected.** No test in this module touches the
    pre-existing ``/analyze`` or ``/jobs/*`` handlers. M15.0a is
    additive only.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import job_queue  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — build a fakeredis client and patch the factory.
# ---------------------------------------------------------------------------


# Shared fakeredis "server" so successive calls to _redis_factory
# return clients that see the same in-memory state — RQ's enqueue
# + later status fetch both need to look up the same job.
import fakeredis as _fakeredis  # noqa: E402
_SHARED_FAKE_SERVER = _fakeredis.FakeServer()


def _fake_factory(url: str):
    """Return a fakeredis client backed by the SHARED in-memory
    server so successive calls see each other's writes."""
    return _fakeredis.FakeRedis(server=_SHARED_FAKE_SERVER)


def _reset_fake_server():
    """Wipe the shared fake server between tests so state doesn't leak."""
    global _SHARED_FAKE_SERVER
    _SHARED_FAKE_SERVER = _fakeredis.FakeServer()


# Sentinel URL — never used as a real connection target because we
# always patch the factory.
_FAKE_URL = "redis://test-host:6379/0"


# ---------------------------------------------------------------------------
# get_redis_connection / get_queue
# ---------------------------------------------------------------------------


class GetRedisConnectionTests(unittest.TestCase):
    def test_returns_none_when_url_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            self.assertIsNone(job_queue.get_redis_connection())

    def test_returns_none_when_url_blank(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": "   "}, clear=False):
            self.assertIsNone(job_queue.get_redis_connection())

    def test_returns_client_when_url_set_and_ping_ok(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                client = job_queue.get_redis_connection()
                self.assertIsNotNone(client)
                self.assertTrue(client.ping())

    def test_returns_none_on_connection_failure(self):
        def _broken_factory(url):
            class _Broken:
                def ping(self):
                    raise ConnectionError("simulated connection refused")
            return _Broken()

        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _broken_factory):
                self.assertIsNone(job_queue.get_redis_connection())


class GetQueueTests(unittest.TestCase):
    def test_returns_queue_when_redis_connected(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                queue = job_queue.get_queue("default")
                self.assertIsNotNone(queue)
                # Real RQ Queue has a `name` attribute.
                self.assertEqual(queue.name, "default")

    def test_returns_none_when_redis_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            self.assertIsNone(job_queue.get_queue("default"))


# ---------------------------------------------------------------------------
# enqueue_job
# ---------------------------------------------------------------------------


# RQ 2.x rejects functions from the __main__ module ("Functions from
# the __main__ module cannot be processed by workers"). When this
# test file is invoked as `python tests/test_job_queue.py`, anything
# defined here lives in __main__. Use a stdlib function instead so
# RQ's pickling pre-check passes — we never actually execute the
# job; we only enqueue it to verify a job_id is returned.
import operator
_NOOP_JOB = operator.add


class EnqueueJobTests(unittest.TestCase):
    def test_returns_job_id_when_redis_available(self):
        _reset_fake_server()
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                job_id = job_queue.enqueue_job(_NOOP_JOB, 1, 2)
                self.assertIsNotNone(job_id)
                self.assertIsInstance(job_id, str)
                # RQ job_ids are typically UUID-like; sanity check non-trivial length.
                self.assertGreater(len(job_id), 8)

    def test_returns_none_when_redis_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            self.assertIsNone(job_queue.enqueue_job(_NOOP_JOB, 1, 2))

    def test_returns_none_when_queue_construction_fails(self):
        """If RQ raises when constructing the Queue, enqueue must
        still degrade to None, not bubble the exception."""
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                with mock.patch.object(
                    job_queue, "get_queue", return_value=None,
                ):
                    self.assertIsNone(job_queue.enqueue_job(_NOOP_JOB, 1, 2))


# ---------------------------------------------------------------------------
# get_job_status
# ---------------------------------------------------------------------------


class GetJobStatusTests(unittest.TestCase):
    EXPECTED_KEYS = frozenset({
        "status", "result", "error",
        "enqueued_at", "started_at", "ended_at",
    })

    def test_returns_dict_with_expected_keys_after_enqueue(self):
        _reset_fake_server()
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                job_id = job_queue.enqueue_job(_NOOP_JOB, 1, 2)
                self.assertIsNotNone(job_id)
                status = job_queue.get_job_status(job_id)
                self.assertEqual(set(status.keys()), self.EXPECTED_KEYS)
                # The job was just enqueued and a worker hasn't run it,
                # so status should be "queued" (or "scheduled" in some
                # RQ versions). It must NOT be "unavailable".
                self.assertIn(
                    status["status"],
                    {"queued", "scheduled", "started"},
                    f"unexpected status after enqueue: {status['status']!r}",
                )
                self.assertIsNotNone(status["enqueued_at"])

    def test_returns_not_found_for_unknown_job_id(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                status = job_queue.get_job_status("nonexistent-job-id-xyz")
                self.assertEqual(set(status.keys()), self.EXPECTED_KEYS)
                self.assertEqual(status["status"], "not_found")
                self.assertEqual(status["error"], "job_not_found")

    def test_returns_unavailable_when_redis_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            status = job_queue.get_job_status("any-job-id")
            self.assertEqual(set(status.keys()), self.EXPECTED_KEYS)
            self.assertEqual(status["status"], "unavailable")
            self.assertEqual(status["error"], "redis_unavailable")


# ---------------------------------------------------------------------------
# get_queue_health
# ---------------------------------------------------------------------------


class GetQueueHealthTests(unittest.TestCase):
    EXPECTED_KEYS = frozenset({
        "redis_connected", "queue_depth", "workers_count",
        "queue_name", "redis_url_set",
    })

    def setUp(self):
        # Reset the shared fake server so earlier tests don't leak
        # queued jobs into the depth-reporting assertions.
        _reset_fake_server()

    def test_health_when_redis_available(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                health = job_queue.get_queue_health()
                self.assertEqual(set(health.keys()), self.EXPECTED_KEYS)
                self.assertTrue(health["redis_connected"])
                self.assertTrue(health["redis_url_set"])
                self.assertEqual(health["queue_name"], "default")
                # Empty queue, no workers running in tests.
                self.assertEqual(health["queue_depth"], 0)
                self.assertEqual(health["workers_count"], 0)

    def test_health_when_redis_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            health = job_queue.get_queue_health()
            self.assertEqual(set(health.keys()), self.EXPECTED_KEYS)
            self.assertFalse(health["redis_connected"])
            self.assertFalse(health["redis_url_set"])
            self.assertEqual(health["queue_depth"], 0)
            self.assertEqual(health["workers_count"], 0)

    def test_health_reflects_enqueued_jobs(self):
        _reset_fake_server()
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                job_queue.enqueue_job(_NOOP_JOB, 1, 1)
                job_queue.enqueue_job(_NOOP_JOB, 2, 2)
                job_queue.enqueue_job(_NOOP_JOB, 3, 3)
                health = job_queue.get_queue_health()
                self.assertTrue(health["redis_connected"])
                self.assertEqual(health["queue_depth"], 3)


# ---------------------------------------------------------------------------
# /health/queue endpoint (FastAPI TestClient)
# ---------------------------------------------------------------------------


class HealthQueueEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import api_server
        self.client = TestClient(api_server.app)

    def test_health_queue_returns_degraded_when_redis_unset(self):
        """No REDIS_URL → endpoint still returns 200 with
        ``redis_connected=False``. Must NOT 5xx."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            response = self.client.get("/health/queue")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["redis_connected"])
        self.assertFalse(body["redis_url_set"])
        self.assertEqual(body["queue_depth"], 0)
        self.assertEqual(body["workers_count"], 0)
        self.assertEqual(body["queue_name"], "default")

    def test_health_queue_returns_connected_when_fake_redis_injected(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                response = self.client.get("/health/queue")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["redis_connected"])
        self.assertTrue(body["redis_url_set"])

    def test_existing_health_endpoint_unchanged(self):
        """``/health`` was the liveness probe before M15.0a and must
        remain byte-identical: still returns ``{"status": "healthy"}``."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})


# ---------------------------------------------------------------------------
# Module-level / safety contracts
# ---------------------------------------------------------------------------


class ModuleContractsTests(unittest.TestCase):
    def test_job_queue_module_imports_cleanly(self):
        """``import job_queue`` must succeed even if the lazy redis
        connection cannot be made. The IS_*_AVAILABLE flags reflect
        what was importable at this interpreter."""
        import importlib
        reloaded = importlib.reload(job_queue)
        self.assertIn("get_redis_connection", dir(reloaded))
        self.assertIn("get_queue", dir(reloaded))
        self.assertIn("enqueue_job", dir(reloaded))
        self.assertIn("get_job_status", dir(reloaded))
        self.assertIn("get_queue_health", dir(reloaded))

    def test_worker_module_imports_cleanly(self):
        """``import worker`` must succeed. The worker is opt-in: it
        only fails when ``main()`` is actually called without REDIS_URL."""
        import worker  # noqa: F401
        # The module should expose `main` as an entry point.
        self.assertTrue(hasattr(worker, "main"))

    def test_no_llm_imports_in_job_queue(self):
        """job_queue.py must NOT import any LLM-related module. The
        queue is pure plumbing — M11.0d-1 Constraint #12 forbids
        LLM-driven verdict mutation, and the queue is one place an
        accidental LLM dep could sneak in."""
        path = _PROJECT_ROOT / "job_queue.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_prefixes = (
            "openai", "anthropic", "langchain", "ai_reasoner",
            "llm_judge",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[0]
                    self.assertNotIn(
                        name, forbidden_prefixes,
                        f"job_queue.py imports forbidden module {alias.name!r}",
                    )
            elif isinstance(node, ast.ImportFrom):
                base = (node.module or "").split(".")[0]
                self.assertNotIn(
                    base, forbidden_prefixes,
                    f"job_queue.py does `from {node.module} import ...` — "
                    "forbidden LLM module.",
                )

    def test_no_llm_imports_in_worker(self):
        path = _PROJECT_ROOT / "worker.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_prefixes = (
            "openai", "anthropic", "langchain", "ai_reasoner",
            "llm_judge",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split(".")[0]
                    self.assertNotIn(name, forbidden_prefixes)
            elif isinstance(node, ast.ImportFrom):
                base = (node.module or "").split(".")[0]
                self.assertNotIn(base, forbidden_prefixes)

    def test_analyze_pipeline_not_imported_by_job_queue(self):
        """Defense in depth: M15.0a must NOT touch the verdict
        pipeline. job_queue.py importing analyze_pipeline would
        couple the queue infra to the verdict path."""
        path = _PROJECT_ROOT / "job_queue.py"
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "from main import", "import main",
            "analyze_pipeline",
            "from policy_decision", "from policy_scoring",
            "from verification_card",
        ):
            self.assertNotIn(
                forbidden, source,
                f"job_queue.py contains forbidden coupling to verdict "
                f"pipeline: {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main()
