"""Tests for Phase 2 M2 async job execution.

Run with: python tests/test_jobs.py

Note: Starlette TestClient creates a fresh event loop per request, so tasks
scheduled via ``asyncio.create_task`` from inside a route do not survive across
client calls. Under real uvicorn the long-running loop keeps background tasks
alive. To keep tests deterministic without spinning up a real server, the
background coroutine ``_execute_job`` is invoked directly via ``asyncio.run``.
End-to-end HTTP routes (status, result, legacy /analyze) are verified through
TestClient against pre-seeded job state.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import postgres as pg


def _fake_pipeline_report(query: str, max_news: int) -> dict:
    return {
        "run_started_at": "2026-05-19T00:00:00+00:00",
        "run_finished_at": "2026-05-19T00:00:01+00:00",
        "query": query,
        "total_news_count": 1,
        "saved_event_count": 1,
        "duplicate_count": 0,
        "news_collection_debug": {"news_collection_mode": "test"},
        "topics_summary": {},
        "ai_status_summary": {
            "ai_status": "ok",
            "ai_status_reason": "ok",
            "ai_model": "gpt-test",
            "ai_available": True,
            "ai_api_key_present": True,
        },
        "news_results": [
            {
                "api_result": {
                    "title": f"Result for {query}",
                    "original_url": f"https://example.com/job-test/{query}",
                    "topic": "테스트",
                    "claim_text": "테스트 주장",
                    "verdict_label": "draft_likely_true",
                    "verdict_confidence": 75,
                    "verification_card": {
                        "claim_text": "테스트 주장",
                        "verdict_label": "draft_likely_true",
                        "verdict_confidence": 75,
                        "last_checked_at": "2026-05-19T00:00:00+00:00",
                    },
                    "final_decision": {"policy_alert_level": "MONITOR"},
                    "policy_confidence": {"policy_confidence_score": 75},
                    "policy_impact": {"impact_level": "low"},
                    "ai_status": "ok",
                    "ai_status_reason": "ok",
                    "ai_model": "gpt-test",
                    "ai_available": True,
                }
            }
        ],
    }


class _TempDBScope:
    """Run each test against a fresh SQLite DB and clean Postgres env."""

    def __enter__(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp_dir = self._tmp_ctx.__enter__()
        self._db_path = Path(tmp_dir) / "jobs_test.db"
        self._pg_snapshot = {
            key: os.environ.get(key) for key in ("DATABASE_URL", "USE_POSTGRES_WRITE")
        }
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("USE_POSTGRES_WRITE", None)
        pg.reset_state_for_tests()

        # Make sure modules see a clean DB_PATH before any connection is opened.
        import database
        importlib.reload(database)
        database.DB_PATH = self._db_path
        database.init_db()

        import job_manager
        importlib.reload(job_manager)

        self.database = database
        self.job_manager = job_manager
        return self

    def __exit__(self, *exc):
        for key, value in self._pg_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        pg.reset_state_for_tests()
        try:
            self._tmp_ctx.__exit__(*exc)
        except Exception:
            pass


class JobLifecycleTests(unittest.TestCase):
    def test_create_then_get_status(self):
        with _TempDBScope() as scope:
            record = scope.job_manager.create_job(query="전세대출", max_news=2)
            self.assertEqual(record["status"], "queued")
            self.assertEqual(record["current_stage"], "queued")
            self.assertEqual(record["progress_percent"], 0)
            self.assertEqual(record["query"], "전세대출")
            fetched = scope.job_manager.get_job_status(record["id"])
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched["id"], record["id"])
            self.assertEqual(fetched["status"], "queued")

    def test_lifecycle_transitions(self):
        with _TempDBScope() as scope:
            record = scope.job_manager.create_job(query="q", max_news=1)
            job_id = record["id"]

            scope.job_manager.start_job(job_id)
            running = scope.job_manager.get_job_status(job_id)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["current_stage"], "running")
            self.assertIsNotNone(running["started_at"])

            scope.job_manager.update_progress(job_id, "pipeline_started", 30)
            progress = scope.job_manager.get_job_status(job_id)
            self.assertEqual(progress["current_stage"], "pipeline_started")
            self.assertEqual(progress["progress_percent"], 30)

            scope.job_manager.complete_job(job_id, result_id=42)
            completed = scope.job_manager.get_job_status(job_id)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["current_stage"], "completed")
            self.assertEqual(completed["progress_percent"], 100)
            self.assertEqual(completed["result_id"], 42)
            self.assertIsNotNone(completed["completed_at"])

    def test_failure_records_error_message(self):
        with _TempDBScope() as scope:
            record = scope.job_manager.create_job(query="q", max_news=1)
            job_id = record["id"]
            scope.job_manager.start_job(job_id)
            scope.job_manager.fail_job(job_id, "boom: simulated")
            failed = scope.job_manager.get_job_status(job_id)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["current_stage"], "failed")
            self.assertIn("simulated", failed["error_message"])

    def test_timeout_status_distinct_from_failure(self):
        with _TempDBScope() as scope:
            record = scope.job_manager.create_job(query="q", max_news=1)
            scope.job_manager.timeout_job(record["id"], "took too long")
            row = scope.job_manager.get_job_status(record["id"])
            self.assertEqual(row["status"], "timeout")
            self.assertEqual(row["current_stage"], "timeout")
            self.assertIn("took too long", row["error_message"])

    def test_get_job_status_missing_returns_none(self):
        with _TempDBScope() as scope:
            self.assertIsNone(scope.job_manager.get_job_status("does-not-exist"))


def _reload_api_server(scope, pipeline_fn=None):
    import api_server
    importlib.reload(api_server)
    api_server.database = scope.database
    api_server.job_manager = scope.job_manager
    if pipeline_fn is None:
        pipeline_fn = _fake_pipeline_report
    api_server.analyze_pipeline = pipeline_fn
    return api_server


class ExecuteJobCoroutineTests(unittest.TestCase):
    """Drive _execute_job directly via asyncio.run for deterministic completion."""

    def test_successful_run_completes_and_caches_report(self):
        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            record = scope.job_manager.create_job(query="전세대출", max_news=1)

            asyncio.run(
                api_server._execute_job(
                    record["id"], "전세대출", 1, timeout_seconds=30
                )
            )

            final = scope.job_manager.get_job_status(record["id"])
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["current_stage"], "completed")
            self.assertEqual(final["progress_percent"], 100)
            self.assertIsNotNone(final["result_id"])
            self.assertIsNotNone(final["completed_at"])

            cached = api_server._JOB_REPORT_CACHE.get(record["id"])
            self.assertIsNotNone(cached)
            self.assertEqual(cached["status"], "ok")
            self.assertEqual(len(cached["results"]), 1)
            self.assertEqual(cached["results"][0]["topic"], "테스트")

    def test_pipeline_exception_marks_failed(self):
        def boom(query, max_news):
            raise RuntimeError("pipeline exploded")

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope, pipeline_fn=boom)
            record = scope.job_manager.create_job("q", 1)
            asyncio.run(api_server._execute_job(record["id"], "q", 1, timeout_seconds=30))
            row = scope.job_manager.get_job_status(record["id"])
            self.assertEqual(row["status"], "failed")
            self.assertIn("pipeline exploded", row["error_message"])

    def test_timeout_marks_job_timed_out(self):
        import time

        def slow(query, max_news):
            time.sleep(1.5)
            return _fake_pipeline_report(query, max_news)

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope, pipeline_fn=slow)
            record = scope.job_manager.create_job("q", 1)
            # Drive coroutine with a tight artificial timeout. The route clamps
            # to >=30s in production but _execute_job honors the explicit value.
            # asyncio.run will block on executor shutdown until the slow thread
            # finishes; the terminal-state guard in job_manager prevents the
            # late thread from overwriting the timeout state.
            asyncio.run(api_server._execute_job(record["id"], "q", 1, timeout_seconds=1))
            row = scope.job_manager.get_job_status(record["id"])
            self.assertEqual(row["status"], "timeout")
            self.assertEqual(row["current_stage"], "timeout")
            self.assertIn("timeout", row["error_message"].lower())

    def test_concurrent_failure_does_not_crash_server_state(self):
        def boom(query, max_news):
            raise RuntimeError("boom")

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope, pipeline_fn=boom)
            # Two jobs in parallel — both fail, neither leaves the server unusable.
            r1 = scope.job_manager.create_job("a", 1)
            r2 = scope.job_manager.create_job("b", 1)

            async def runner():
                await asyncio.gather(
                    api_server._execute_job(r1["id"], "a", 1, timeout_seconds=10),
                    api_server._execute_job(r2["id"], "b", 1, timeout_seconds=10),
                )

            asyncio.run(runner())
            self.assertEqual(scope.job_manager.get_job_status(r1["id"])["status"], "failed")
            self.assertEqual(scope.job_manager.get_job_status(r2["id"])["status"], "failed")


class ApiServerRouteTests(unittest.TestCase):
    """HTTP surface: validate request/response handling for the new routes."""

    def test_create_job_returns_queued_payload(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            client = TestClient(api_server.app)
            resp = client.post("/jobs/analyze", json={"query": "q", "max_news": 1})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["job_status"], "queued")
            self.assertTrue(body["job_id"])
            # Even though the background task does not progress under TestClient,
            # the job row must exist in SQLite.
            row = scope.job_manager.get_job_status(body["job_id"])
            self.assertIsNotNone(row)

    def test_status_route_returns_seeded_state(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            record = scope.job_manager.create_job("q", 1)
            scope.job_manager.start_job(record["id"])
            scope.job_manager.update_progress(record["id"], "pipeline_started", 25)

            client = TestClient(api_server.app)
            resp = client.get(f"/jobs/{record['id']}")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["job_status"], "running")
            self.assertEqual(body["current_stage"], "pipeline_started")
            self.assertEqual(body["progress_percent"], 25)

    def test_status_route_404_for_unknown_job(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            client = TestClient(api_server.app)
            self.assertEqual(client.get("/jobs/nope").status_code, 404)

    def test_result_route_returns_cached_payload_after_completion(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            record = scope.job_manager.create_job("전세대출", 1)
            asyncio.run(api_server._execute_job(record["id"], "전세대출", 1, 30))
            client = TestClient(api_server.app)
            resp = client.get(f"/jobs/{record['id']}/result")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["job_status"], "completed")
            self.assertEqual(body["result_source"], "cache")
            self.assertIsNone(body["error_message"])
            self.assertIsNotNone(body["result"])
            self.assertEqual(body["result"]["status"], "ok")
            self.assertEqual(len(body["result"]["results"]), 1)

    def test_result_route_reconstructs_from_sqlite_after_cache_eviction(self):
        """Simulate server restart: cache is empty but job row + SQLite row remain."""
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            record = scope.job_manager.create_job("전세대출", 1)
            asyncio.run(api_server._execute_job(record["id"], "전세대출", 1, 30))
            # Wipe the in-memory cache to mimic a fresh process.
            api_server._JOB_REPORT_CACHE.clear()

            client = TestClient(api_server.app)
            resp = client.get(f"/jobs/{record['id']}/result")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["job_status"], "completed")
            self.assertEqual(body["result_source"], "stored_result")
            self.assertIsNone(body["error_message"])
            self.assertIsNotNone(body["result"])
            self.assertIsNotNone(body["stored_result"])
            self.assertEqual(len(body["result"]["results"]), 1)
            self.assertEqual(
                body["result"]["results"][0]["verdict_label"],
                "draft_likely_true",
            )
            self.assertEqual(
                body["result"]["ai_status"]["ai_status_reason"],
                "stored_result_reconstructed",
            )

    def test_result_route_returns_unavailable_when_nothing_stored(self):
        """Completed job with no linked row and no cache must NOT claim success."""
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            record = scope.job_manager.create_job("q", 1)
            # Mark completed but with no result_id and no cache.
            scope.job_manager.start_job(record["id"])
            scope.job_manager.complete_job(record["id"], result_id=None)
            api_server._JOB_REPORT_CACHE.clear()

            client = TestClient(api_server.app)
            resp = client.get(f"/jobs/{record['id']}/result")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "result_unavailable")
            self.assertEqual(body["job_status"], "completed")
            self.assertIsNone(body["result"])
            self.assertIsNone(body["stored_result"])
            self.assertIsNone(body["result_source"])
            self.assertTrue(body["error_message"])
            self.assertIn("no cached payload", body["error_message"])

    def test_result_route_returns_error_for_failed_job(self):
        from fastapi.testclient import TestClient

        def nope(query, max_news):
            raise RuntimeError("nope")

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope, pipeline_fn=nope)
            record = scope.job_manager.create_job("q", 1)
            asyncio.run(api_server._execute_job(record["id"], "q", 1, 30))
            client = TestClient(api_server.app)
            resp = client.get(f"/jobs/{record['id']}/result")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["job_status"], "failed")
            self.assertIn("nope", body["error_message"])

    def test_result_route_409_while_job_running(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            record = scope.job_manager.create_job("q", 1)
            scope.job_manager.start_job(record["id"])
            client = TestClient(api_server.app)
            resp = client.get(f"/jobs/{record['id']}/result")
            self.assertEqual(resp.status_code, 409)

    def test_legacy_analyze_route_still_works(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            client = TestClient(api_server.app)
            resp = client.post("/analyze", json={"query": "q", "max_news": 1})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(len(body["results"]), 1)
            self.assertEqual(body["results"][0]["title"], "Result for q")
            # Phase 2 M3: /analyze must surface result_id so the frontend can
            # rehydrate the row from /history/{result_id} without storing the
            # full payload in localStorage.
            self.assertIn("result_id", body["results"][0])
            self.assertIsNotNone(body["results"][0]["result_id"])

    def test_history_detail_round_trip_supports_hydration(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            client = TestClient(api_server.app)
            analyze_resp = client.post("/analyze", json={"query": "q", "max_news": 1})
            self.assertEqual(analyze_resp.status_code, 200)
            result_id = analyze_resp.json()["results"][0]["result_id"]
            self.assertIsNotNone(result_id)

            detail_resp = client.get(f"/history/{result_id}")
            self.assertEqual(detail_resp.status_code, 200)
            detail = detail_resp.json()
            self.assertEqual(detail["status"], "ok")
            self.assertIsNotNone(detail["result"])
            self.assertEqual(detail["result"]["id"], result_id)
            self.assertEqual(detail["result"]["query"], "q")

    def test_invalid_request_returns_400(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            client = TestClient(api_server.app)
            self.assertEqual(
                client.post("/jobs/analyze", json={"query": "", "max_news": 1}).status_code,
                400,
            )
            self.assertEqual(
                client.post("/jobs/analyze", json={"query": "q", "max_news": 0}).status_code,
                400,
            )


class DurabilitySupportTests(unittest.TestCase):
    """Hardening behaviors that protect /jobs/{id}/result after restart."""

    def test_duplicate_url_still_links_job_to_existing_sqlite_row(self):
        """If the same URL is analyzed twice, the second job must link to row #1."""
        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            r1 = scope.job_manager.create_job("q", 1)
            asyncio.run(api_server._execute_job(r1["id"], "q", 1, 30))
            first_id = scope.job_manager.get_job_status(r1["id"])["result_id"]
            self.assertIsNotNone(first_id)

            r2 = scope.job_manager.create_job("q", 1)
            asyncio.run(api_server._execute_job(r2["id"], "q", 1, 30))
            second_id = scope.job_manager.get_job_status(r2["id"])["result_id"]
            # Second run hits the duplicate-URL path but must still be linked
            # to the existing analysis_results row for durability.
            self.assertEqual(second_id, first_id)

    def test_fail_job_does_not_overwrite_terminal_state(self):
        with _TempDBScope() as scope:
            record = scope.job_manager.create_job("q", 1)
            scope.job_manager.start_job(record["id"])
            scope.job_manager.complete_job(record["id"], result_id=7)
            # Second failure must not overwrite the completed state.
            scope.job_manager.fail_job(record["id"], "late failure")
            row = scope.job_manager.get_job_status(record["id"])
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["result_id"], 7)
            self.assertIsNone(row["error_message"])

    def test_timeout_then_fail_does_not_overwrite(self):
        with _TempDBScope() as scope:
            record = scope.job_manager.create_job("q", 1)
            scope.job_manager.start_job(record["id"])
            scope.job_manager.timeout_job(record["id"], "first timeout")
            scope.job_manager.fail_job(record["id"], "second failure")
            row = scope.job_manager.get_job_status(record["id"])
            self.assertEqual(row["status"], "timeout")
            self.assertIn("first timeout", row["error_message"])


class BackgroundTaskTrackingTests(unittest.TestCase):
    def test_background_tasks_set_holds_strong_references(self):
        """asyncio only keeps weak refs; tasks must be retained explicitly."""
        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)

            async def runner():
                async def noop():
                    await asyncio.sleep(0)

                task = asyncio.create_task(noop())
                api_server._track_background_task(task)
                self.assertIn(task, api_server._BACKGROUND_TASKS)
                await task
                # done_callback drops the reference after completion.
                await asyncio.sleep(0)
                self.assertNotIn(task, api_server._BACKGROUND_TASKS)

            asyncio.run(runner())


class PollingFlowTests(unittest.TestCase):
    """Simulate the UI's create-then-poll loop against the FastAPI routes."""

    def test_poll_status_until_completed(self):
        from fastapi.testclient import TestClient

        with _TempDBScope() as scope:
            api_server = _reload_api_server(scope)
            client = TestClient(api_server.app)
            created = client.post(
                "/jobs/analyze", json={"query": "전세대출", "max_news": 1}
            ).json()
            job_id = created["job_id"]

            # The background task does not run under TestClient, so we drive
            # _execute_job manually to mimic the asyncio scheduler. The polling
            # contract — create, GET status repeatedly, GET result — is the same.
            asyncio.run(api_server._execute_job(job_id, "전세대출", 1, 30))

            statuses_observed = []
            for _ in range(3):
                s = client.get(f"/jobs/{job_id}").json()
                statuses_observed.append(s["job_status"])
                if s["job_status"] == "completed":
                    break
            self.assertIn("completed", statuses_observed)

            result_payload = client.get(f"/jobs/{job_id}/result").json()
            self.assertEqual(result_payload["job_status"], "completed")
            self.assertIsNotNone(result_payload["result"])


class PostgresIsolationTests(unittest.TestCase):
    """Postgres dual-write *write* failures must never break SQLite job writes.

    M12.0d-1 update: the read contract changed. Pre-Stage-1, a broken
    PG engine made ``get_job_status`` silently fall back to SQLite, so
    this test asserted the SQLite row came back even with PG down.
    Stage 1 removed that silent fallback — when dual-write is enabled
    and the engine returns None (e.g., psycopg2 missing in this test
    env), ``get_job_status`` returns the not-found sentinel (None).
    The test now asserts the original *write* contract (no raise) and
    documents the Stage 1 read-contract change."""

    def test_postgres_failure_does_not_break_sqlite_job_writes(self):
        with _TempDBScope() as scope:
            os.environ["DATABASE_URL"] = "postgresql://invalid:invalid@127.0.0.1:1/none"
            os.environ["USE_POSTGRES_WRITE"] = "true"
            pg.reset_state_for_tests()

            with patch.object(pg, "get_session", side_effect=RuntimeError("pg-down")):
                try:
                    record = scope.job_manager.create_job("q", 1)
                except Exception as error:
                    self.fail(f"create_job must not raise on pg failure: {error}")
                scope.job_manager.start_job(record["id"])
                scope.job_manager.update_progress(record["id"], "pipeline_started", 50)
                scope.job_manager.complete_job(record["id"], result_id=None)

            # Writes did not raise — primary contract preserved.
            # SQLite row check: drop down to the raw connection because
            # job_manager.get_job_status now prefers PG (and PG engine
            # is None in this env → returns None per Stage 1).
            with scope.job_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT status, progress_percent FROM jobs WHERE id = ?",
                    (record["id"],),
                ).fetchone()
            self.assertIsNotNone(row, "SQLite job row must persist even with PG misconfigured")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["progress_percent"], 100)


if __name__ == "__main__":
    unittest.main()
