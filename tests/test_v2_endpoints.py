"""M15.0b — pins for the V2 async endpoints.

Verifies the contracts of three new opt-in endpoints added by
M15.0b to ``api_server.py``:

  * ``POST /v2/analyze``             — 202 + job_id when Redis OK; 503 when down
  * ``GET  /v2/jobs/{job_id}``       — full status dict; 404 when missing; 503 when down
  * ``GET  /v2/jobs/{job_id}/stream`` — SSE stream that emits at
                                       least one terminal event

Critical safety pins
====================

  1. ``GET /health`` (the liveness probe) stays byte-identical:
     200 + ``{"status": "healthy"}``.
  2. ``GET /health/queue`` (M15.0a) stays byte-identical.
  3. The existing ``POST /analyze`` and ``POST /jobs/analyze``
     endpoints still EXIST and respond (we don't run their full
     bodies — that would invoke the 174s pipeline — but we verify
     the routes are registered).

All tests run fully offline using fakeredis (M15.0a pattern) and
do NOT require a worker process. They mock ``main.analyze_pipeline``
where needed so the 174s pipeline is never executed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import job_queue  # noqa: E402
import pipeline_worker  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fake-redis helpers — same pattern as M15.0a tests.
# ---------------------------------------------------------------------------


import fakeredis as _fakeredis
_SHARED_FAKE_SERVER = _fakeredis.FakeServer()


def _fake_factory(url: str):
    return _fakeredis.FakeRedis(server=_SHARED_FAKE_SERVER)


def _reset_fake_server():
    global _SHARED_FAKE_SERVER
    _SHARED_FAKE_SERVER = _fakeredis.FakeServer()


_FAKE_URL = "redis://test-host:6379/0"


def _make_client():
    """FastAPI TestClient bound to the api_server app."""
    from fastapi.testclient import TestClient
    import api_server
    return TestClient(api_server.app)


# ---------------------------------------------------------------------------
# Pre-existing endpoints stay byte-identical
# ---------------------------------------------------------------------------


class PreexistingEndpointsUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.client = _make_client()

    def test_health_endpoint_byte_identical(self):
        """``/health`` is the liveness probe; M15.0b must not touch it."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    def test_health_queue_endpoint_still_present(self):
        """M15.0a's ``/health/queue`` endpoint must still respond."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            response = self.client.get("/health/queue")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "redis_connected", "queue_depth", "workers_count",
            "queue_name", "redis_url_set",
        ):
            self.assertIn(key, body)

    def test_existing_analyze_route_still_registered(self):
        """``POST /analyze`` must still be in the route table.
        We don't invoke the body (would call the 174s pipeline);
        we just confirm the route exists by sending a malformed
        body and asserting 422 (validation error), not 404."""
        response = self.client.post("/analyze", json={})
        self.assertNotEqual(
            response.status_code, 404,
            "POST /analyze must remain registered after M15.0b.",
        )
        # Empty body fails pydantic validation → 422.
        self.assertEqual(response.status_code, 422)

    def test_existing_jobs_analyze_route_still_registered(self):
        """``POST /jobs/analyze`` (the pre-existing process-local
        job system) must still be in the route table."""
        response = self.client.post("/jobs/analyze", json={})
        self.assertNotEqual(response.status_code, 404)
        self.assertEqual(response.status_code, 422)


# ---------------------------------------------------------------------------
# POST /v2/analyze
# ---------------------------------------------------------------------------


class V2AnalyzeEndpointTests(unittest.TestCase):
    def setUp(self):
        _reset_fake_server()
        self.client = _make_client()

    def test_returns_503_when_redis_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            response = self.client.post(
                "/v2/analyze",
                json={"query": "전세사기", "max_news": 1},
            )
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertIn("redis_unavailable", body.get("detail", ""))

    def test_returns_202_and_job_id_when_redis_available(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                response = self.client.post(
                    "/v2/analyze",
                    json={"query": "전세사기", "max_news": 1},
                )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("job_id", body)
        self.assertEqual(body["status"], "queued")
        self.assertIn("created_at", body)
        self.assertEqual(body.get("queue_name"), "default")
        self.assertGreater(len(body["job_id"]), 8)

    def test_validates_empty_query(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                response = self.client.post(
                    "/v2/analyze", json={"query": "   ", "max_news": 1},
                )
        self.assertEqual(response.status_code, 400)

    def test_validates_max_news_must_be_positive(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                response = self.client.post(
                    "/v2/analyze", json={"query": "test", "max_news": 0},
                )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# GET /v2/jobs/{job_id}
# ---------------------------------------------------------------------------


class V2JobStatusEndpointTests(unittest.TestCase):
    def setUp(self):
        _reset_fake_server()
        self.client = _make_client()

    def test_returns_404_for_unknown_job_id(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                response = self.client.get("/v2/jobs/no-such-job-id-xyz")
        self.assertEqual(response.status_code, 404)

    def test_returns_503_when_redis_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            response = self.client.get("/v2/jobs/any-id")
        self.assertEqual(response.status_code, 503)

    def test_returns_status_payload_for_real_enqueued_job(self):
        """Enqueue a job (no worker runs it; it sits in the queue)
        and confirm the status endpoint reports queued + the
        expected payload shape."""
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                enqueue = self.client.post(
                    "/v2/analyze",
                    json={"query": "DSR", "max_news": 1},
                )
                self.assertEqual(enqueue.status_code, 202)
                job_id = enqueue.json()["job_id"]
                response = self.client.get(f"/v2/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "job_id", "status", "result", "error",
            "enqueued_at", "started_at", "ended_at",
            "progress_percent", "current_step",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["job_id"], job_id)
        # The job is queued (no worker running it).
        self.assertIn(body["status"], {"queued", "scheduled"})


# ---------------------------------------------------------------------------
# GET /v2/jobs/{job_id}/stream
# ---------------------------------------------------------------------------


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse SSE wire format into [{event, data_dict}, ...]."""
    events: list[dict] = []
    current_event = None
    current_data_lines: list[str] = []
    for line in raw.split("\n"):
        stripped = line.rstrip("\r")
        if stripped.startswith("event:"):
            current_event = stripped[len("event:"):].strip()
        elif stripped.startswith("data:"):
            current_data_lines.append(stripped[len("data:"):].strip())
        elif stripped == "":
            if current_event or current_data_lines:
                data_str = "\n".join(current_data_lines)
                try:
                    data = json.loads(data_str) if data_str else {}
                except json.JSONDecodeError:
                    data = {"raw": data_str}
                events.append({"event": current_event or "message", "data": data})
                current_event = None
                current_data_lines = []
    return events


class V2StreamEndpointTests(unittest.TestCase):
    def setUp(self):
        _reset_fake_server()
        self.client = _make_client()

    def test_stream_emits_unavailable_event_when_redis_down(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            with self.client.stream(
                "GET", "/v2/jobs/any-id/stream",
            ) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "text/event-stream",
                    response.headers.get("content-type", ""),
                )
                raw = "".join(response.iter_text())
        events = _parse_sse_events(raw)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "unavailable")
        self.assertIn("reason", events[0]["data"])

    def test_stream_emits_not_found_event_for_missing_job(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                with self.client.stream(
                    "GET", "/v2/jobs/nonexistent-job/stream",
                ) as response:
                    self.assertEqual(response.status_code, 200)
                    raw = "".join(response.iter_text())
        events = _parse_sse_events(raw)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "not_found")

    def test_stream_emits_initial_status_for_queued_job(self):
        """A real queued job (no worker) should yield at least one
        ``status`` event with status='queued' or 'scheduled' before
        the stream times out. We use a short max-seconds patch so
        the test doesn't take 600s."""
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                # Enqueue a real job (it will stay queued).
                enqueue = self.client.post(
                    "/v2/analyze", json={"query": "stream-test", "max_news": 1},
                )
                job_id = enqueue.json()["job_id"]
                # Patch the stream's max-seconds so we don't wait 600s.
                from api_server import _v2_stream_generator as _orig_gen

                def _short_gen(jid):
                    yield from _orig_gen(jid, max_seconds=2.0)

                with mock.patch("api_server._v2_stream_generator", _short_gen):
                    with self.client.stream(
                        "GET", f"/v2/jobs/{job_id}/stream",
                    ) as response:
                        self.assertEqual(response.status_code, 200)
                        raw = "".join(response.iter_text())
        events = _parse_sse_events(raw)
        self.assertGreaterEqual(len(events), 1)
        # First event is always a status (or maybe progress on race).
        kinds = [e["event"] for e in events]
        self.assertIn("status", kinds)


# ---------------------------------------------------------------------------
# Module / static safety contracts
# ---------------------------------------------------------------------------


class StaticSafetyContractsTests(unittest.TestCase):
    """Source-level pins that the M15.0b additions did not accidentally
    rewrite the pre-existing /analyze handler or remove old endpoints."""

    def setUp(self):
        self.source = (_PROJECT_ROOT / "api_server.py").read_text(encoding="utf-8")

    def test_existing_analyze_handler_still_present(self):
        self.assertIn('@app.post("/analyze"', self.source)
        self.assertIn("def analyze(request: AnalyzeRequest)", self.source)

    def test_existing_jobs_analyze_handler_still_present(self):
        self.assertIn('@app.post("/jobs/analyze"', self.source)
        self.assertIn("def jobs_analyze(request: JobCreateRequest)", self.source)

    def test_existing_jobs_result_handler_still_present(self):
        self.assertIn('@app.get("/jobs/{job_id}/result")', self.source)

    def test_v2_endpoints_registered(self):
        self.assertIn('@app.post("/v2/analyze"', self.source)
        self.assertIn('@app.get("/v2/jobs/{job_id}")', self.source)
        self.assertIn('@app.get("/v2/jobs/{job_id}/stream")', self.source)

    def test_v2_endpoints_have_503_degradation(self):
        """The 503 responses on Redis-unavailable must be visible
        in the source so a future refactor can't silently remove
        them."""
        self.assertIn("redis_unavailable", self.source)
        self.assertIn("status_code=503", self.source)


if __name__ == "__main__":
    unittest.main()
