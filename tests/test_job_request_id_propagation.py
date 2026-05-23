"""M14.3b — Worker context propagation pins for job_manager helpers.

These tests verify that ``job_manager.submit_in_context`` and
``job_manager.run_in_thread_with_context`` correctly propagate the
caller's ``request_id`` (and any other contextvars state) to a worker
running in a different thread.

The killer test is :class:`ConcurrentJobIsolationTests` — five jobs
enqueued with five distinct request_ids must each see *their own*
request_id inside the worker, even when the workers run concurrently
on the same thread pool. A naive implementation that captured the
context once at module import time, or that mutated a shared variable,
would fail this test.

Run with: ``python tests/test_job_request_id_propagation.py``
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import job_manager  # noqa: E402
import request_context  # noqa: E402


# ---------------------------------------------------------------------------
# run_in_thread_with_context — the synchronous "spawn a fresh thread"
# helper. Most tests below use this because it has no shared state
# across tests.
# ---------------------------------------------------------------------------


class WorkerInheritsRequestIdTests(unittest.TestCase):
    """Worker sees the request_id that was set in the caller at the
    time the helper was called."""

    def setUp(self):
        self._token = request_context.set_request_id(None)

    def tearDown(self):
        request_context.reset_request_id(self._token)

    def test_worker_inherits_request_id(self):
        def worker():
            return request_context.get_request_id()

        with request_context.request_id_scope("test-rid-123"):
            observed = job_manager.run_in_thread_with_context(worker)
        self.assertEqual(observed, "test-rid-123")

    def test_worker_sees_none_when_no_request_id_set(self):
        """Backward-compat path: no rid at enqueue → worker sees
        None. This is the scheduler.py / CLI path."""
        def worker():
            return request_context.get_request_id()

        observed = job_manager.run_in_thread_with_context(worker)
        self.assertIsNone(observed)


class WorkerReturnValueAndExceptionTests(unittest.TestCase):
    def test_return_value_propagated(self):
        def worker():
            return ("payload", request_context.get_request_id())

        with request_context.request_id_scope("rid-return"):
            result = job_manager.run_in_thread_with_context(worker)
        self.assertEqual(result, ("payload", "rid-return"))

    def test_args_and_kwargs_passed(self):
        def worker(a, b, *, mode):
            return (a, b, mode, request_context.get_request_id())

        with request_context.request_id_scope("rid-args"):
            result = job_manager.run_in_thread_with_context(
                worker, 1, 2, mode="fast",
            )
        self.assertEqual(result, (1, 2, "fast", "rid-args"))

    def test_worker_exception_propagated(self):
        def worker():
            raise RuntimeError("worker exploded")

        with request_context.request_id_scope("rid-exc"):
            with self.assertRaises(RuntimeError) as cm:
                job_manager.run_in_thread_with_context(worker)
        self.assertIn("worker exploded", str(cm.exception))


class WorkerDoesNotLeakBackToCallerTests(unittest.TestCase):
    """Setting request_id inside the worker must not affect the caller.

    Pin against a naive implementation that runs the worker in the
    *current* context (which would mutate the caller's ContextVars).
    """

    def setUp(self):
        self._token = request_context.set_request_id(None)

    def tearDown(self):
        request_context.reset_request_id(self._token)

    def test_worker_set_does_not_leak(self):
        def worker():
            request_context.set_request_id("inner-rid-XYZ")
            return request_context.get_request_id()

        with request_context.request_id_scope("outer-rid"):
            inner = job_manager.run_in_thread_with_context(worker)
            self.assertEqual(inner, "inner-rid-XYZ")
            self.assertEqual(
                request_context.get_request_id(), "outer-rid",
                "Worker mutated the caller's request_id — context leaked.",
            )

    def test_unset_caller_unchanged_when_worker_sets_rid(self):
        def worker():
            request_context.set_request_id("worker-only-rid")
            return request_context.get_request_id()

        # Caller has no rid.
        inner = job_manager.run_in_thread_with_context(worker)
        self.assertEqual(inner, "worker-only-rid")
        self.assertIsNone(
            request_context.get_request_id(),
            "Caller's rid became non-None after worker ran — leak.",
        )


class ConcurrentJobIsolationTests(unittest.TestCase):
    """The pin test for M14.3b.

    Submit 5 jobs concurrently, each with a different request_id set at
    submit time via ``submit_in_context``. Each worker records its
    observed request_id. Every worker must see *its own* request_id
    — not another job's, not None.
    """

    def setUp(self):
        self._token = request_context.set_request_id(None)

    def tearDown(self):
        request_context.reset_request_id(self._token)

    def test_five_concurrent_jobs_each_see_own_request_id(self):
        # Synchronize workers so they all run "at once" — maximises
        # the chance of catching a naive global-mutation bug.
        gate = threading.Event()

        def worker(job_index: int):
            # Wait until all submitters have queued — then race.
            gate.wait(timeout=5.0)
            # Tiny stagger so context-switch scheduling matters.
            time.sleep(0.001 * (job_index % 3))
            return request_context.get_request_id()

        expected: list[str] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = []
            for i in range(5):
                rid = f"concurrent-rid-{i:02d}"
                expected.append(rid)
                # Each submit happens inside its own request_id_scope so
                # the captured context contains rid-i.
                with request_context.request_id_scope(rid):
                    futures.append(
                        job_manager.submit_in_context(pool, worker, i),
                    )
            # Release all workers simultaneously.
            gate.set()
            observed = [f.result(timeout=10.0) for f in futures]

        self.assertEqual(
            observed,
            expected,
            f"Expected each worker to see its own rid; got {observed!r}",
        )

    def test_thread_pool_reuse_does_not_leak_rid(self):
        """Re-using a thread pool worker across two jobs must not
        leak the first job's rid into the second.

        A naive ``executor.submit(func)`` (no ctx.run wrapper) would
        leave the prior call's rid in the worker thread's ContextVars
        on the second invocation."""
        observed = []

        def worker():
            observed.append(request_context.get_request_id())

        # ThreadPoolExecutor with max_workers=1 forces the same
        # worker thread to be reused. We submit job-A with rid-A,
        # wait for it, then submit job-B with NO rid. Job B must
        # see None, not "rid-A".
        with ThreadPoolExecutor(max_workers=1) as pool:
            with request_context.request_id_scope("rid-A"):
                f1 = job_manager.submit_in_context(pool, worker)
            f1.result(timeout=5.0)

            # No request_id at submit.
            f2 = job_manager.submit_in_context(pool, worker)
            f2.result(timeout=5.0)

        self.assertEqual(
            observed,
            ["rid-A", None],
            "Reusing the same worker thread leaked rid across jobs.",
        )


# ---------------------------------------------------------------------------
# submit_in_context — the concurrent.futures path. The
# ConcurrentJobIsolationTests above already exercise this path; the
# tests below pin the simpler contract.
# ---------------------------------------------------------------------------


class SubmitInContextBasicTests(unittest.TestCase):
    def setUp(self):
        self._token = request_context.set_request_id(None)

    def tearDown(self):
        request_context.reset_request_id(self._token)

    def test_submit_returns_future(self):
        from concurrent.futures import Future

        def worker():
            return "ok"

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = job_manager.submit_in_context(pool, worker)
            self.assertIsInstance(future, Future)
            self.assertEqual(future.result(timeout=5.0), "ok")

    def test_submit_propagates_args_kwargs(self):
        def worker(x, *, y):
            return x + y

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = job_manager.submit_in_context(pool, worker, 10, y=5)
            self.assertEqual(future.result(timeout=5.0), 15)

    def test_submit_propagates_exception(self):
        def worker():
            raise ValueError("submit boom")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = job_manager.submit_in_context(pool, worker)
            with self.assertRaises(ValueError) as cm:
                future.result(timeout=5.0)
            self.assertIn("submit boom", str(cm.exception))

    def test_submit_captures_at_submit_time_not_at_call_time(self):
        """The context captured for the worker must be the context as
        it was at ``submit_in_context(...)`` call time — not the
        context as it is when the worker actually starts running.

        We submit a job inside a scope, then exit the scope, then
        force the worker to run. The worker must still see the rid
        from inside the scope.
        """
        gate = threading.Event()

        def worker():
            gate.wait(timeout=5.0)
            return request_context.get_request_id()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with request_context.request_id_scope("rid-at-submit"):
                future = job_manager.submit_in_context(pool, worker)

            # Scope exited. The caller's rid is now None.
            self.assertIsNone(request_context.get_request_id())

            # Now let the worker run.
            gate.set()
            observed = future.result(timeout=5.0)

        self.assertEqual(
            observed,
            "rid-at-submit",
            "Worker observed the post-scope-exit rid; context was "
            "not captured at submit time as required.",
        )


# ---------------------------------------------------------------------------
# Backward compatibility — workers without request_id still work, and
# no behavioural change for callers that ignore these helpers.
# ---------------------------------------------------------------------------


class BackwardCompatibilityPin(unittest.TestCase):
    """When request_id is None at submit, the worker observes None and
    the system behaves exactly as before M14.3a. This is the
    scheduler.py / CLI / unit-test path."""

    def test_pipeline_call_without_request_id_works(self):
        # Simulate a sync caller that has no HTTP context.
        # (Equivalent to what scheduler.py does.)
        def worker():
            rid = request_context.get_request_id()
            # Returning the rid lets us verify None.
            return ("ran", rid)

        result = job_manager.run_in_thread_with_context(worker)
        self.assertEqual(result, ("ran", None))

    def test_submit_in_context_without_rid_works(self):
        def worker():
            return request_context.get_request_id()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = job_manager.submit_in_context(pool, worker)
            self.assertIsNone(future.result(timeout=5.0))


if __name__ == "__main__":
    unittest.main()
