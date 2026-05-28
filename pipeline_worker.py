"""M15.0b — RQ-callable wrapper around ``main.analyze_pipeline``.

This module is the bridge between RQ's worker process and the
existing synchronous ``analyze_pipeline`` function. It is **kept
deliberately thin**: the heavy lifting (M11.0d-3a disagreement_signal,
M11.0d-3b P2 authority codification, all verdict-producing logic)
happens inside ``analyze_pipeline`` and is not touched here.

Design contracts
================

1. **No verdict-producing logic.** This module never imports
   ``policy_decision`` / ``policy_scoring`` / ``verification_card``.
   It re-uses ``main.analyze_pipeline`` whole.

2. **Importable as ``pipeline_worker.run_analyze_pipeline_job``.**
   RQ requires that job functions be importable by qualified name
   from a non-``__main__`` module. RQ workers will import this
   module fresh in their own process.

3. **Best-effort progress reporting.** ``report_progress`` publishes
   to Redis pub/sub channel ``job:{job_id}:progress``. If Redis is
   unavailable, the call is silently dropped — the pipeline still
   runs to completion.

4. **No exceptions escape ``run_analyze_pipeline_job``.** RQ would
   record them as job failures, but for operator clarity the
   wrapper always returns a serializable dict with an explicit
   ``status`` field.

5. **Same persistence behaviour as ``api_server.analyze``.** Each
   per-news ``api_result`` is saved via ``save_analysis_result`` (the
   same dedup-by-URL path the sync endpoint uses). Postgres
   dual-write is invoked when enabled.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional


log = logging.getLogger("policy_ai.pipeline_worker")


# Pipeline checkpoints — coarse stages reported via Redis pub/sub.
# Mirrors the existing api_server `_run_pipeline_for_job` stage names
# (job_manager.STAGE_PIPELINE_STARTED / STAGE_SAVING_RESULT) so an
# operator looking at logs from both flows sees the same vocabulary.
STAGE_QUEUED = "queued"
STAGE_PIPELINE_STARTED = "pipeline_started"
STAGE_SAVING_RESULTS = "saving_results"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress_channel(job_id: str) -> str:
    return f"job:{job_id}:progress"


def report_progress(
    job_id: str,
    *,
    stage: str,
    percent: int,
    detail: Optional[str] = None,
) -> bool:
    """Publish a progress event to Redis pub/sub. Returns True on a
    successful publish, False on any degraded path. Never raises.

    The payload is intentionally minimal so the SSE generator can
    forward it verbatim:

        {"stage": "pipeline_started", "percent": 10,
         "detail": null, "at": "2026-05-25T..."}

    """
    payload = {
        "stage": stage,
        "percent": int(max(0, min(100, percent))),
        "detail": detail,
        "at": _utc_now_iso(),
        "job_id": job_id,
    }
    try:
        import json
        import job_queue

        client = job_queue.get_redis_connection()
        if client is None:
            return False
        client.publish(_progress_channel(job_id), json.dumps(payload))
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort contract
        log.warning(
            "pipeline_worker.report_progress_failed",
            extra={
                "job_id": job_id,
                "stage": stage,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:300],
            },
        )
        return False


def _persist_results(report: dict, *, query: str) -> list[int]:
    """Persist per-news api_result rows via save_analysis_result.

    Mirrors the existing api_server.analyze loop at L266-301 exactly.
    Returns the list of saved row ids (may include None for dedup hits).
    """
    from database import (
        get_result_id_by_url,
        save_analysis_result,
    )
    from text_utils import sanitize_data

    saved_ids: list[int] = []
    # M15-dedup-1 Part B — defensive dedup at the saved_ids
    # boundary. main.py's post-resolve dedup is the primary guard,
    # but this list flows into _build_summary_payload's
    # saved_result_ids field used by SSE consumers and the
    # /v2/jobs/{id} endpoint — a duplicate id here would surface as
    # a duplicate entry in the operator-visible job summary.
    seen_saved_ids: set = set()
    for item in report.get("news_results", []) or []:
        api_result = item.get("api_result") or {}
        if not api_result:
            continue
        api_result = sanitize_data(api_result)
        try:
            save_status = save_analysis_result(api_result, query=query)
            if save_status.get("duplicate"):
                try:
                    existing = get_result_id_by_url(
                        api_result.get("original_url") or ""
                    )
                    if existing is not None and int(existing) not in seen_saved_ids:
                        saved_ids.append(int(existing))
                        seen_saved_ids.add(int(existing))
                except Exception:  # noqa: BLE001
                    log.warning(
                        "pipeline_worker.dedup_id_lookup_failed",
                        extra={"job_query": query},
                    )
            else:
                new_id = save_status.get("id")
                if new_id is not None and int(new_id) not in seen_saved_ids:
                    saved_ids.append(int(new_id))
                    seen_saved_ids.add(int(new_id))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "pipeline_worker.save_failed",
                extra={
                    "job_query": query,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:300],
                },
            )
    return saved_ids


def _build_summary_payload(report: dict, saved_ids: list[int], query: str) -> dict:
    """Distil a stable, JSON-serializable summary so SSE consumers
    and ``/v2/jobs/{id}`` callers see a small payload (the full
    report can be ~MB on a multi-news run; we don't want to bloat
    Redis or the SSE message size).

    The full report is still persisted to SQLite (one row per
    news_result), so callers can reconstruct via the existing
    ``GET /history/{result_id}`` endpoint.
    """
    news_results = report.get("news_results") or []
    return {
        "status": "ok",
        "query": query,
        "total_news_count": int(report.get("total_news_count") or len(news_results)),
        "saved_event_count": int(report.get("saved_event_count") or 0),
        "duplicate_count": int(report.get("duplicate_count") or 0),
        "saved_result_ids": saved_ids,
        "ai_status_summary": report.get("ai_status_summary") or {},
        "news_collection_debug": report.get("news_collection_debug") or {},
    }


def run_analyze_pipeline_job(query: str, max_news: int, job_id: str) -> dict:
    """RQ entry point: run the analysis pipeline + persist results +
    emit progress events. Returns a serializable summary dict.

    Per the M15.0b contract, this wrapper:

      * Does NOT modify ``main.analyze_pipeline``.
      * Does NOT modify any verdict-producing code.
      * Emits progress events on a best-effort basis (silent on
        Redis pub/sub failures).
      * Returns a small summary dict so RQ's ``result`` storage
        stays compact. Full results are in SQLite (link by
        ``saved_result_ids``).
      * NEVER raises — any unexpected exception is recorded in the
        return value and emits a final ``failed`` progress event.
    """
    # Import locally so module import never triggers main.py's
    # top-level cost in tests that only need the helper functions.
    from main import analyze_pipeline

    log.info(
        "pipeline_worker.job_started",
        extra={"job_id": job_id, "job_query": query, "max_news": int(max_news)},
    )
    report_progress(job_id, stage=STAGE_PIPELINE_STARTED, percent=10,
                    detail=f"query={query}")
    # M15.0d: bridge analyze_pipeline's parallel-phase progress
    # callback to our existing report_progress channel. Each per-news
    # Phase A completion fires a "news_item_completed" event with
    # {index, total}; we translate to a percent in the 10-85 band so
    # the bar moves smoothly during parallel work.
    def _phase_a_progress_bridge(stage: str, payload: dict) -> None:
        try:
            if stage == "news_item_parallel_started":
                report_progress(
                    job_id,
                    stage=stage,
                    percent=12,
                    detail=f"total={payload.get('total')} workers={payload.get('workers')}",
                )
            elif stage == "news_item_completed":
                idx = int(payload.get("index") or 0)
                total = max(1, int(payload.get("total") or 1))
                # Map to the 15-80 band so news_item completions appear
                # between pipeline_started (10) and saving_results (85).
                pct = int(round(15 + (idx / total) * 65))
                report_progress(
                    job_id,
                    stage=stage,
                    percent=pct,
                    detail=f"index={idx}/{total}",
                )
        except Exception:  # noqa: BLE001 — best-effort progress
            pass

    try:
        report = analyze_pipeline(
            query=query,
            max_news=int(max_news),
            progress_callback=_phase_a_progress_bridge,
        )
    except Exception as exc:  # noqa: BLE001 — wrap-and-report contract
        log.warning(
            "pipeline_worker.analyze_pipeline_failed",
            extra={
                "job_id": job_id,
                "job_query": query,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:500],
            },
        )
        report_progress(
            job_id, stage=STAGE_FAILED, percent=100,
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return {
            "status": "failed",
            "query": query,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
            "saved_result_ids": [],
        }

    report_progress(job_id, stage=STAGE_SAVING_RESULTS, percent=85,
                    detail="persisting per-news results")
    saved_ids = _persist_results(report, query=query)
    payload = _build_summary_payload(report, saved_ids, query)
    report_progress(job_id, stage=STAGE_COMPLETED, percent=100,
                    detail=f"saved={len(saved_ids)} news_count={payload['total_news_count']}")
    log.info(
        "pipeline_worker.job_completed",
        extra={
            "job_id": job_id,
            "job_query": query,
            "saved_count": len(saved_ids),
            "total_news_count": payload["total_news_count"],
        },
    )
    return payload
