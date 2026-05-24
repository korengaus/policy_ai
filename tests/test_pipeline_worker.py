"""M15.0b — pins for the RQ-callable pipeline wrapper.

Verifies the contracts of ``pipeline_worker.py``:

  * ``run_analyze_pipeline_job`` wraps ``main.analyze_pipeline``,
    calls ``database.save_analysis_result`` per news item, emits
    progress events to Redis pub/sub, and returns a serializable
    summary dict.
  * Progress reporting is best-effort: a Redis-pubsub failure
    never escapes ``report_progress``.
  * The wrapper NEVER raises — pipeline-side exceptions are
    captured and surfaced via the return value + a final
    ``failed`` progress event.
  * The wrapper has no LLM imports (Constraint #12 cross-check).

All tests are fully offline using ``fakeredis`` (M15.0a pattern)
and a mocked ``analyze_pipeline``. The real 174s pipeline is never
invoked.
"""

from __future__ import annotations

import ast
import json
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
# Shared fake-redis helpers (M15.0a pattern: shared FakeServer so
# successive _redis_factory calls see each other's writes / pub-sub
# messages).
# ---------------------------------------------------------------------------


import fakeredis as _fakeredis
_SHARED_FAKE_SERVER = _fakeredis.FakeServer()


def _fake_factory(url: str):
    return _fakeredis.FakeRedis(server=_SHARED_FAKE_SERVER)


def _reset_fake_server():
    global _SHARED_FAKE_SERVER
    _SHARED_FAKE_SERVER = _fakeredis.FakeServer()


_FAKE_URL = "redis://test-host:6379/0"


# ---------------------------------------------------------------------------
# Sample analyze_pipeline report — minimal shape the wrapper needs.
# ---------------------------------------------------------------------------


def _sample_report(query: str, max_news: int) -> dict:
    """Return a stub report with the shape api_server.analyze + the
    wrapper both consume: news_results[*].api_result + a couple of
    top-level summary fields."""
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
        "run_started_at": "2026-05-25T00:00:00+00:00",
        "run_finished_at": "2026-05-25T00:00:05+00:00",
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
    return {"duplicate": False, "id": 42}


def _stub_get_result_id_by_url(url):
    return None


def _stub_postgres_dual_write(result, query):
    return {"attempted": False, "ok": True}


# ---------------------------------------------------------------------------
# report_progress contracts
# ---------------------------------------------------------------------------


class ReportProgressTests(unittest.TestCase):
    def setUp(self):
        _reset_fake_server()

    def test_publishes_event_to_correct_channel(self):
        """A successful publish ends up on ``job:{job_id}:progress``."""
        import os
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                client = job_queue.get_redis_connection()
                self.assertIsNotNone(client)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe("job:abc123:progress")
                ok = pipeline_worker.report_progress(
                    "abc123", stage="pipeline_started", percent=10,
                    detail="news loading",
                )
                self.assertTrue(ok)
                # Drain the subscribe-confirmation message + the one
                # data message we expect.
                message = None
                for _ in range(5):
                    candidate = pubsub.get_message(timeout=0.5)
                    if candidate and candidate.get("type") == "message":
                        message = candidate
                        break
                pubsub.close()
                self.assertIsNotNone(message,
                                     "expected a pub/sub data message after publish")
                payload = json.loads(message["data"].decode("utf-8"))
                self.assertEqual(payload["stage"], "pipeline_started")
                self.assertEqual(payload["percent"], 10)
                self.assertEqual(payload["detail"], "news loading")
                self.assertEqual(payload["job_id"], "abc123")

    def test_returns_false_when_redis_unavailable(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            ok = pipeline_worker.report_progress(
                "any-job", stage="pipeline_started", percent=5,
            )
            self.assertFalse(ok)

    def test_clamps_percent_to_0_100_range(self):
        """Out-of-range percent values must be clamped, not rejected."""
        import os
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                client = job_queue.get_redis_connection()
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe("job:clamp-test:progress")
                pipeline_worker.report_progress(
                    "clamp-test", stage="x", percent=200,
                )
                pipeline_worker.report_progress(
                    "clamp-test", stage="x", percent=-50,
                )
                messages = []
                for _ in range(8):
                    cand = pubsub.get_message(timeout=0.3)
                    if cand and cand.get("type") == "message":
                        messages.append(json.loads(cand["data"].decode("utf-8")))
                pubsub.close()
                self.assertEqual(len(messages), 2)
                self.assertEqual(messages[0]["percent"], 100)
                self.assertEqual(messages[1]["percent"], 0)

    def test_never_raises_on_redis_publish_error(self):
        """If client.publish() raises, the call returns False and the
        caller continues. This is the best-effort contract."""
        class _BrokenClient:
            def ping(self):
                return True

            def publish(self, channel, message):
                raise ConnectionError("simulated pub/sub failure")

        with mock.patch.object(
            job_queue, "get_redis_connection",
            return_value=_BrokenClient(),
        ):
            ok = pipeline_worker.report_progress(
                "any-job", stage="fail-test", percent=50,
            )
            self.assertFalse(ok)


# ---------------------------------------------------------------------------
# run_analyze_pipeline_job contracts
# ---------------------------------------------------------------------------


class RunAnalyzePipelineJobTests(unittest.TestCase):
    def setUp(self):
        _reset_fake_server()

    def _patches(self, report: dict):
        """Patch analyze_pipeline + persistence helpers so the wrapper
        doesn't actually invoke the 174s pipeline. Returns a context-
        manager chain (use ``with contextlib.ExitStack`` to apply)."""
        return [
            mock.patch.object(
                pipeline_worker, "run_analyze_pipeline_job",
                wraps=pipeline_worker.run_analyze_pipeline_job,
            ),
            # Patch analyze_pipeline at the import site INSIDE the
            # wrapper. The wrapper does `from main import analyze_pipeline`
            # at function-call time so we patch main.analyze_pipeline.
            mock.patch("main.analyze_pipeline", return_value=report),
            mock.patch(
                "pipeline_worker._persist_results",
                wraps=pipeline_worker._persist_results,
            ),
            mock.patch(
                "database.save_analysis_result",
                side_effect=_stub_save_analysis_result,
            ),
            mock.patch(
                "database.get_result_id_by_url",
                side_effect=_stub_get_result_id_by_url,
            ),
            mock.patch(
                "db.postgres.postgres_dual_write",
                side_effect=_stub_postgres_dual_write,
            ),
        ]

    def test_returns_serializable_summary_dict(self):
        report = _sample_report("전세사기", max_news=2)
        with mock.patch("main.analyze_pipeline", return_value=report):
            with mock.patch("database.save_analysis_result", side_effect=_stub_save_analysis_result):
                with mock.patch("database.get_result_id_by_url", side_effect=_stub_get_result_id_by_url):
                    with mock.patch("db.postgres.postgres_dual_write", side_effect=_stub_postgres_dual_write):
                        summary = pipeline_worker.run_analyze_pipeline_job(
                            "전세사기", 2, "job-xyz-001",
                        )
        # Serializable (round-trips through json).
        json.dumps(summary, ensure_ascii=False)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["query"], "전세사기")
        self.assertEqual(summary["total_news_count"], 2)
        self.assertEqual(len(summary["saved_result_ids"]), 2)
        for rid in summary["saved_result_ids"]:
            self.assertIsInstance(rid, int)
        self.assertIn("ai_status_summary", summary)

    def test_progress_events_published_for_each_stage(self):
        report = _sample_report("청년 월세", max_news=1)
        import os
        with mock.patch.dict(os.environ, {"REDIS_URL": _FAKE_URL}, clear=False):
            with mock.patch.object(job_queue, "_redis_factory", _fake_factory):
                # Subscribe BEFORE invoking the wrapper so we catch
                # the events.
                client = job_queue.get_redis_connection()
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe("job:progress-test:progress")
                with mock.patch("main.analyze_pipeline", return_value=report):
                    with mock.patch("database.save_analysis_result", side_effect=_stub_save_analysis_result):
                        with mock.patch("database.get_result_id_by_url", side_effect=_stub_get_result_id_by_url):
                            with mock.patch("db.postgres.postgres_dual_write", side_effect=_stub_postgres_dual_write):
                                pipeline_worker.run_analyze_pipeline_job(
                                    "청년 월세", 1, "progress-test",
                                )
                # Drain — expect at least 3 events: pipeline_started,
                # saving_results, completed.
                stages_seen: list[str] = []
                for _ in range(15):
                    cand = pubsub.get_message(timeout=0.3)
                    if cand and cand.get("type") == "message":
                        payload = json.loads(cand["data"].decode("utf-8"))
                        stages_seen.append(payload["stage"])
                pubsub.close()
        self.assertIn(pipeline_worker.STAGE_PIPELINE_STARTED, stages_seen)
        self.assertIn(pipeline_worker.STAGE_SAVING_RESULTS, stages_seen)
        self.assertIn(pipeline_worker.STAGE_COMPLETED, stages_seen)

    def test_pipeline_exception_captured_in_return_value(self):
        """When analyze_pipeline raises, the wrapper must NOT
        re-raise. It captures the exception in the return value
        and emits a ``failed`` progress event."""
        with mock.patch(
            "main.analyze_pipeline",
            side_effect=RuntimeError("simulated pipeline crash"),
        ):
            summary = pipeline_worker.run_analyze_pipeline_job(
                "boom", 1, "failed-job-id",
            )
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["error_type"], "RuntimeError")
        self.assertIn("simulated pipeline crash", summary["error_message"])
        self.assertEqual(summary["saved_result_ids"], [])

    def test_wrapper_does_not_crash_when_pubsub_unavailable(self):
        """Pub/sub failure must not break the pipeline run — the
        wrapper still saves results and returns a normal summary."""
        report = _sample_report("주담대", max_news=1)
        with mock.patch("main.analyze_pipeline", return_value=report):
            with mock.patch("database.save_analysis_result", side_effect=_stub_save_analysis_result):
                with mock.patch("database.get_result_id_by_url", side_effect=_stub_get_result_id_by_url):
                    with mock.patch("db.postgres.postgres_dual_write", side_effect=_stub_postgres_dual_write):
                        # No REDIS_URL → report_progress returns False
                        # every time; the wrapper must still complete.
                        import os
                        with mock.patch.dict(os.environ, {}, clear=False):
                            os.environ.pop("REDIS_URL", None)
                            summary = pipeline_worker.run_analyze_pipeline_job(
                                "주담대", 1, "no-pubsub-job",
                            )
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["total_news_count"], 1)
        self.assertEqual(len(summary["saved_result_ids"]), 1)


# ---------------------------------------------------------------------------
# Module-level safety contracts
# ---------------------------------------------------------------------------


class ModuleContractsTests(unittest.TestCase):
    def test_module_imports_cleanly(self):
        import importlib
        reloaded = importlib.reload(pipeline_worker)
        self.assertTrue(hasattr(reloaded, "run_analyze_pipeline_job"))
        self.assertTrue(hasattr(reloaded, "report_progress"))

    def test_run_analyze_pipeline_job_is_rq_compatible(self):
        """RQ 2.x rejects functions whose __module__ is __main__.
        The wrapper must live in a real module."""
        self.assertEqual(
            pipeline_worker.run_analyze_pipeline_job.__module__,
            "pipeline_worker",
        )

    def test_no_llm_imports_in_pipeline_worker(self):
        """The wrapper must NOT import LLM-related modules
        directly (M11.0d-1 Constraint #12). It only imports
        analyze_pipeline (which itself MAY trigger LLM calls
        downstream — that is fine; the constraint is about
        verdict-state mutation paths, not all LLM activity)."""
        path = _PROJECT_ROOT / "pipeline_worker.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_top_level = (
            "openai", "anthropic", "langchain", "ai_reasoner",
            "llm_judge",
        )
        # Walk top-level imports only (not function-local imports of
        # main which itself uses LLMs).
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    base = alias.name.split(".")[0]
                    self.assertNotIn(
                        base, forbidden_top_level,
                        f"pipeline_worker.py top-level import of {alias.name!r} is forbidden",
                    )
            elif isinstance(node, ast.ImportFrom):
                base = (node.module or "").split(".")[0]
                self.assertNotIn(
                    base, forbidden_top_level,
                    f"pipeline_worker.py top-level `from {node.module} import ...` is forbidden",
                )

    def test_analyze_pipeline_is_imported_lazily(self):
        """``from main import analyze_pipeline`` must happen INSIDE
        the wrapper so the module can be imported (e.g., in tests
        + ops checks) without the cost of importing all of main."""
        path = _PROJECT_ROOT / "pipeline_worker.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_imports_main = any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module == "main"
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name == "main" for alias in node.names)
            )
            for node in tree.body
        )
        self.assertFalse(
            top_level_imports_main,
            "pipeline_worker.py must NOT import main at module top "
            "level — keep the import inside run_analyze_pipeline_job "
            "so the module can be imported cheaply elsewhere.",
        )


if __name__ == "__main__":
    unittest.main()
