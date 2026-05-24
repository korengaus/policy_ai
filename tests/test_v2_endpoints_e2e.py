"""M15.0c — End-to-end pins for the browser→V2 flow.

Simulates what the browser does after M15.0c:

  1. POST /v2/analyze → 202 + job_id
  2. EventSource on /v2/jobs/{job_id}/stream → at least one status event
  3. GET /v2/jobs/{job_id} → status payload
  4. (when finished) GET /history/{id} for each saved_result_ids entry

All tests use fakeredis (from M15.0a) + a worker simulation via
RQ's SimpleWorker (in-process burst mode). The real 174s pipeline
is never invoked — main.analyze_pipeline is mocked to return a
synthetic report identical in shape to what production returns.

This file is complementary to:
  - tests/test_v2_endpoints.py (M15.0b — endpoint-level pins)
  - tests/test_pipeline_worker.py (M15.0b — wrapper-level pins)
  - tests/test_frontend_v2_client.test.js (M15.0c — JS-side static pins)

It adds the missing layer: enqueue → worker → fetch result, end to end.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import job_queue  # noqa: E402
import pipeline_worker  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fake-redis helpers (M15.0a/b pattern)
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
    from fastapi.testclient import TestClient
    import api_server
    return TestClient(api_server.app)


# ---------------------------------------------------------------------------
# Synthetic analyze_pipeline report (same shape M15.0b uses)
# ---------------------------------------------------------------------------


def _sample_report(query: str, max_news: int) -> dict:
    items = []
    for i in range(max_news):
        items.append({
            "api_result": {
                "title": f"제목 {i}",
                "original_url": f"https://example.com/news/{query}/{i}",
                "topic": "금융",
                "policy_confidence": {"policy_confidence_score": 60},
                "policy_impact": {"impact_level": "medium"},
                "final_decision": {"policy_alert_level": "WATCH"},
                "verification_card": {"verdict_label": "draft_unverified"},
            },
        })
    return {
        "query": query,
        "total_news_count": max_news,
        "saved_event_count": max_news,
        "duplicate_count": 0,
        "news_results": items,
        "ai_status_summary": {
            "ai_status": "ok",
            "ai_status_reason": "ok",
            "ai_model": "gpt-test",
            "ai_available": True,
        },
        "news_collection_debug": {"news_cache_hit": False},
    }


def _stub_save_analysis_result(result, query):
    # Use the URL hash as a stable synthetic id so tests can assert
    # against specific ids deterministically.
    import hashlib
    url = result.get("original_url") or ""
    h = int(hashlib.sha1(url.encode("utf-8")).hexdigest()[:6], 16)
    return {"duplicate": False, "id": h}


def _stub_get_result_id_by_url(url):
    return None


def _stub_postgres_dual_write(result, query):
    return {"attempted": False, "ok": True}


def _stub_get_result_by_id(result_id):
    return {
        "id": result_id,
        "title": f"제목 #{result_id}",
        "original_url": f"https://example.com/news/{result_id}",
        "topic": "금융",
        "policy_alert_level": "WATCH",
        "policy_confidence_score": 60,
        "verification_strength": "medium",
        "risk_level": "medium",
        "action_priority": "medium",
        "impact_level": "medium",
        "impact_direction": "uncertain",
        "verdict_label": "draft_unverified",
        "verdict_confidence": 60,
        "claim_text": "테스트 주장",
        "evidence_summary": "테스트 근거 요약",
        "source_reliability_score": 3,
        "source_reliability_reason": "established_news",
        "review_status": "draft_unverified",
        "last_checked_at": "2026-05-25T00:00:00+00:00",
        "claims": "[]",
        "normalized_claims": "[]",
        "source_candidates": "[]",
        "source_queries": "[]",
        "evidence_snippets": "[]",
        "claim_evidence_map": "{}",
        "evidence_sources": "[]",
        "contradiction_checks": "[]",
        "contradiction_summary": "{}",
        "bias_framing_analysis": "[]",
        "bias_framing_summary": "{}",
        "source_reliability_summary": "{}",
        "missing_context": "[]",
        "market_signal": "[]",
        "debug_summary": '{"disagreement_signal":{"p1_label":"WATCH","p2_label":"WATCH","p3_label":"draft_unverified","agreed":true}}',
    }


# ---------------------------------------------------------------------------
# End-to-end: enqueue + execute (via RQ SimpleWorker burst) + verify
# ---------------------------------------------------------------------------


class V2EndToEndFlowTests(unittest.TestCase):
    """The whole flow: POST /v2/analyze → worker executes →
    GET /v2/jobs/{id} returns finished + result."""

    def setUp(self):
        _reset_fake_server()
        self.client = _make_client()

    def _execute_pending_jobs_via_simpleworker(self):
        """Run any pending jobs in the fakeredis queue using RQ's
        SimpleWorker (which executes jobs in the current process —
        no separate worker process needed for tests)."""
        with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
            client = job_queue.get_redis_connection()
            self.assertIsNotNone(client)
            import rq
            queue = rq.Queue("default", connection=client)
            # SimpleWorker runs each pending job synchronously in
            # this Python process — no fork, no subprocess, no extra
            # Redis hop. burst=True returns once the queue is empty.
            worker = rq.SimpleWorker([queue], connection=client)
            worker.work(burst=True, with_scheduler=False)

    def test_enqueue_then_execute_then_status_finished(self):
        """End-to-end: enqueue a job, run the worker in burst mode,
        verify /v2/jobs/{id} reports finished + result.saved_result_ids."""
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                with mock.patch(
                    "main.analyze_pipeline",
                    return_value=_sample_report("전세사기", max_news=1),
                ):
                    with mock.patch(
                        "database.save_analysis_result",
                        side_effect=_stub_save_analysis_result,
                    ):
                        with mock.patch(
                            "database.get_result_id_by_url",
                            side_effect=_stub_get_result_id_by_url,
                        ):
                            with mock.patch(
                                "db.postgres.postgres_dual_write",
                                side_effect=_stub_postgres_dual_write,
                            ):
                                # 1. POST /v2/analyze
                                enq = self.client.post(
                                    "/v2/analyze",
                                    json={"query": "전세사기", "max_news": 1},
                                )
                                self.assertEqual(enq.status_code, 202)
                                job_id = enq.json()["job_id"]

                                # 2. Run the worker in burst mode.
                                self._execute_pending_jobs_via_simpleworker()

                                # 3. Status check.
                                status = self.client.get(f"/v2/jobs/{job_id}")
                                self.assertEqual(status.status_code, 200)
                                body = status.json()
                                self.assertEqual(body["status"], "finished")
                                result = body.get("result") or {}
                                self.assertEqual(result.get("status"), "ok")
                                self.assertEqual(result.get("query"), "전세사기")
                                self.assertEqual(result.get("total_news_count"), 1)
                                self.assertEqual(len(result.get("saved_result_ids") or []), 1)

    def test_enqueue_then_execute_then_inflate_via_history_endpoint(self):
        """Simulates the M15.0c frontend completion path: after the
        job finishes, the browser uses /history/{id} for each
        saved_result_ids entry to inflate the AnalyzeResult shape."""
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                with mock.patch(
                    "main.analyze_pipeline",
                    return_value=_sample_report("DSR", max_news=2),
                ):
                    with mock.patch(
                        "database.save_analysis_result",
                        side_effect=_stub_save_analysis_result,
                    ):
                        with mock.patch(
                            "database.get_result_id_by_url",
                            side_effect=_stub_get_result_id_by_url,
                        ):
                            with mock.patch(
                                "db.postgres.postgres_dual_write",
                                side_effect=_stub_postgres_dual_write,
                            ):
                                enq = self.client.post(
                                    "/v2/analyze",
                                    json={"query": "DSR", "max_news": 2},
                                )
                                job_id = enq.json()["job_id"]
                                self._execute_pending_jobs_via_simpleworker()
                                status = self.client.get(f"/v2/jobs/{job_id}")
                                ids = status.json()["result"]["saved_result_ids"]
                                self.assertEqual(len(ids), 2)

                                # 4. Inflate each result via /history/{id}.
                                # /history/{id} uses get_result_by_id which
                                # we stub so tests run offline (no SQLite
                                # row was actually written by the in-process
                                # mocked persist call).
                                with mock.patch(
                                    "api_server.get_result_by_id",
                                    side_effect=_stub_get_result_by_id,
                                ):
                                    for result_id in ids:
                                        history = self.client.get(
                                            f"/history/{result_id}"
                                        )
                                        self.assertEqual(history.status_code, 200)
                                        body = history.json()
                                        self.assertEqual(body["status"], "ok")
                                        self.assertIn("result", body)
                                        # Fields the frontend's
                                        # mapHistoryRowToResult needs:
                                        for key in (
                                            "id", "title", "original_url",
                                            "topic", "verdict_label",
                                            "verdict_confidence", "claims",
                                            "evidence_summary",
                                        ):
                                            self.assertIn(key, body["result"])


# ---------------------------------------------------------------------------
# Pre-existing endpoints unchanged by M15.0c
# ---------------------------------------------------------------------------


class PreexistingEndpointsByteIdenticalTests(unittest.TestCase):
    """M15.0c is frontend-only — every backend endpoint must be
    byte-identical. We re-run the most important sanity smokes here
    so any future M15.0c maintenance instantly catches a regression."""

    def setUp(self):
        self.client = _make_client()

    def test_health_endpoint(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "healthy"})

    def test_health_queue_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            r = self.client.get("/health/queue")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in (
            "redis_connected", "queue_depth", "workers_count",
            "queue_name", "redis_url_set",
        ):
            self.assertIn(key, body)

    def test_existing_analyze_route_still_registered(self):
        # Empty body → 422 (validation), proving the route exists
        # and is NOT 404.
        r = self.client.post("/analyze", json={})
        self.assertEqual(r.status_code, 422)

    def test_existing_jobs_analyze_route_still_registered(self):
        r = self.client.post("/jobs/analyze", json={})
        self.assertEqual(r.status_code, 422)

    def test_v2_endpoints_still_registered(self):
        # 503 (Redis unavailable) proves the route exists and the
        # M15.0b graceful-degradation contract is intact.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            r = self.client.post(
                "/v2/analyze", json={"query": "test", "max_news": 1},
            )
            self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
