import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import describe_ai_config
from database import (
    get_recent_results,
    get_result_by_id,
    get_result_id_by_url,
    init_db,
    save_analysis_result,
)
from db.postgres import (
    is_dual_write_enabled,
    is_postgres_enabled,
    postgres_dual_write,
)
import job_manager
from main import analyze_pipeline
from text_utils import sanitize_data


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("policy_ai.api")


def _log_ai_config_startup() -> None:
    ai_config = describe_ai_config()
    logger.info(
        "AI reasoning config: model=%s (from_env=%s, default=%s) api_key_present=%s",
        ai_config.get("ai_model"),
        ai_config.get("ai_model_from_env"),
        ai_config.get("ai_model_default"),
        ai_config.get("ai_api_key_present"),
    )
    if not ai_config.get("ai_api_key_present"):
        logger.warning(
            "OPENAI_API_KEY is not set; /analyze will fall back to rule-based analysis only "
            "and report ai_status=unavailable."
        )


def _log_postgres_startup() -> None:
    if is_dual_write_enabled():
        logger.info(
            "Postgres dual-write enabled (DATABASE_URL set, USE_POSTGRES_WRITE=true); "
            "SQLite remains source of truth."
        )
    elif is_postgres_enabled():
        logger.info(
            "Postgres reachable (DATABASE_URL set) but dual-write disabled "
            "(USE_POSTGRES_WRITE not true); SQLite-only writes."
        )
    else:
        logger.info("Postgres disabled (DATABASE_URL not set); SQLite-only writes.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("SQLite database initialized")
    _log_ai_config_startup()
    _log_postgres_startup()
    yield


app = FastAPI(title="Policy AI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/web", StaticFiles(directory="web"), name="web")


class AnalyzeRequest(BaseModel):
    query: str
    max_news: int = 3


class AnalyzeResult(BaseModel):
    title: str
    original_url: str
    topic: str
    claims: list = []
    normalized_claims: list = []
    source_candidates: list = []
    source_queries: list = []
    evidence_snippets: list = []
    claim_evidence_map: dict = {}
    claim_evidence_quality_summary: list = []
    evidence_quality_summary: dict = {}
    contradiction_checks: list = []
    contradiction_summary: dict = {}
    bias_framing_analysis: list = []
    bias_framing_summary: dict = {}
    debug_summary: dict = {}
    policy_confidence: dict
    policy_impact: dict
    final_decision: dict
    verification_card: dict = {}
    claim_text: str = ""
    verdict_label: str = ""
    verdict_confidence: int = 0
    evidence_sources: list = []
    source_reliability_score: int = 0
    source_reliability_reason: str = ""
    evidence_summary: str = ""
    missing_context: list = []
    last_checked_at: str = ""
    review_status: str = ""
    ai_status: str = "unavailable"
    ai_status_reason: str = "unknown"
    ai_model: str = ""
    ai_available: bool = False


class AnalyzeResponse(BaseModel):
    status: str
    results: List[AnalyzeResult]
    news_collection_debug: dict = {}
    ai_status: dict = {}


class HistoryResponse(BaseModel):
    status: str
    count: int
    results: List[dict]


@app.get("/")
def root():
    return FileResponse("web/index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "healthy"}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if request.max_news <= 0:
        raise HTTPException(status_code=400, detail="max_news must be greater than 0")

    started = time.perf_counter()
    logger.info("Analyze request received: query=%s max_news=%s", query, request.max_news)

    report = analyze_pipeline(query=query, max_news=request.max_news)
    results = []
    for item in report.get("news_results", []):
        api_result = item.get("api_result") or {}
        if not api_result:
            continue
        api_result = sanitize_data(api_result)

        results.append(
            AnalyzeResult(
                title=api_result.get("title") or "",
                original_url=api_result.get("original_url") or "",
                topic=api_result.get("topic") or "",
                claims=api_result.get("claims") or [],
                normalized_claims=api_result.get("normalized_claims") or [],
                source_candidates=api_result.get("source_candidates") or [],
                source_queries=api_result.get("source_queries") or [],
                evidence_snippets=api_result.get("evidence_snippets") or [],
                claim_evidence_map=api_result.get("claim_evidence_map") or {},
                claim_evidence_quality_summary=api_result.get("claim_evidence_quality_summary") or [],
                evidence_quality_summary=api_result.get("evidence_quality_summary") or {},
                contradiction_checks=api_result.get("contradiction_checks") or [],
                contradiction_summary=api_result.get("contradiction_summary") or {},
                bias_framing_analysis=api_result.get("bias_framing_analysis") or [],
                bias_framing_summary=api_result.get("bias_framing_summary") or {},
                debug_summary=api_result.get("debug_summary") or {},
                policy_confidence=api_result.get("policy_confidence") or {},
                policy_impact=api_result.get("policy_impact") or {},
                final_decision=api_result.get("final_decision") or {},
                verification_card=api_result.get("verification_card") or {},
                claim_text=api_result.get("claim_text") or "",
                verdict_label=api_result.get("verdict_label") or "",
                verdict_confidence=api_result.get("verdict_confidence") or 0,
                evidence_sources=api_result.get("evidence_sources") or [],
                source_reliability_score=api_result.get("source_reliability_score") or 0,
                source_reliability_reason=api_result.get("source_reliability_reason") or "",
                evidence_summary=api_result.get("evidence_summary") or "",
                missing_context=api_result.get("missing_context") or [],
                last_checked_at=api_result.get("last_checked_at") or "",
                review_status=api_result.get("review_status") or "",
                ai_status=api_result.get("ai_status") or "unavailable",
                ai_status_reason=api_result.get("ai_status_reason") or "unknown",
                ai_model=api_result.get("ai_model") or "",
                ai_available=bool(api_result.get("ai_available")),
            )
        )
        try:
            save_status = save_analysis_result(api_result, query=query)
            if save_status.get("duplicate"):
                logger.info("Duplicate skipped in SQLite: %s", api_result.get("title"))
            else:
                try:
                    pg_status = postgres_dual_write(api_result, query=query)
                    if pg_status.get("attempted") and not pg_status.get("ok"):
                        logger.warning(
                            "Postgres dual-write failed (SQLite remains source of truth): %s",
                            pg_status.get("error"),
                        )
                except Exception:
                    logger.exception(
                        "Postgres dual-write raised unexpectedly; SQLite remains source of truth"
                    )
        except Exception:
            logger.exception("Failed to save analysis result to SQLite")

    elapsed = time.perf_counter() - started
    logger.info(
        "Analyze request completed: query=%s results=%s elapsed=%.2fs",
        query,
        len(results),
        elapsed,
    )

    ai_status_summary = report.get("ai_status_summary") or {}
    if not ai_status_summary:
        ai_config = describe_ai_config()
        ai_status_summary = {
            "ai_status": "unavailable",
            "ai_status_reason": "no_results",
            "ai_model": ai_config.get("ai_model"),
            "ai_available": False,
            "ai_api_key_present": ai_config.get("ai_api_key_present", False),
        }
    logger.info(
        "Analyze AI status: status=%s reason=%s model=%s available=%s",
        ai_status_summary.get("ai_status"),
        ai_status_summary.get("ai_status_reason"),
        ai_status_summary.get("ai_model"),
        ai_status_summary.get("ai_available"),
    )

    return AnalyzeResponse(
        status="ok",
        results=results,
        news_collection_debug=report.get("news_collection_debug") or {},
        ai_status=ai_status_summary,
    )


@app.get("/history", response_model=HistoryResponse)
def history(limit: int = 20) -> HistoryResponse:
    try:
        results = get_recent_results(limit=limit)
    except Exception:
        logger.exception("Failed to load analysis history")
        raise HTTPException(status_code=500, detail="failed to load history")

    return HistoryResponse(status="ok", count=len(results), results=results)


class JobCreateRequest(BaseModel):
    query: str
    max_news: int = 3
    timeout_seconds: Optional[int] = None


class JobStatusResponse(BaseModel):
    status: str
    job_id: str
    job_status: str
    current_stage: Optional[str] = None
    progress_percent: int = 0
    query: Optional[str] = None
    max_news: Optional[int] = None
    result_id: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    pipeline_version: Optional[str] = None


def _job_status_to_response(record: dict) -> JobStatusResponse:
    return JobStatusResponse(
        status="ok",
        job_id=record.get("id") or record.get("job_id") or "",
        job_status=record.get("status") or "",
        current_stage=record.get("current_stage"),
        progress_percent=int(record.get("progress_percent") or 0),
        query=record.get("query"),
        max_news=record.get("max_news"),
        result_id=record.get("result_id"),
        error_message=record.get("error_message"),
        created_at=record.get("created_at"),
        started_at=record.get("started_at"),
        completed_at=record.get("completed_at"),
        pipeline_version=record.get("pipeline_version"),
    )


# Phase 2 M2: jobs are process-local. The in-memory cache below holds the
# rich AnalyzeResponse-shaped payload for the lifetime of THIS uvicorn worker.
# Across restarts or multiple workers, /jobs/{id}/result reconstructs the
# payload from the linked analysis_results row in SQLite (see
# _build_async_payload_from_stored_row). Redis/Celery is intentionally out of
# scope until Phase 3 — this milestone only introduces the lifecycle surface.
_JOB_REPORT_CACHE: dict[str, dict] = {}
_JOB_REPORT_CACHE_MAX = 32


def _cache_job_report(job_id: str, payload: dict) -> None:
    _JOB_REPORT_CACHE[job_id] = payload
    if len(_JOB_REPORT_CACHE) > _JOB_REPORT_CACHE_MAX:
        oldest = next(iter(_JOB_REPORT_CACHE))
        _JOB_REPORT_CACHE.pop(oldest, None)


# Strong references to background tasks. asyncio's event loop only keeps weak
# refs to tasks created via asyncio.create_task, so an unreferenced task can be
# garbage collected mid-run. We retain refs here and drop them via a done
# callback. (Process-local — same scope note as the cache above.)
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _track_background_task(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _build_async_analyze_payload(report: dict, query: str) -> dict:
    """Shape the pipeline report like AnalyzeResponse so the UI can render it."""
    results = []
    for item in report.get("news_results", []) or []:
        api_result = item.get("api_result") or {}
        if not api_result:
            continue
        api_result = sanitize_data(api_result)
        results.append(_api_result_to_dict(api_result))

    ai_status_summary = report.get("ai_status_summary") or {}
    if not ai_status_summary:
        ai_config = describe_ai_config()
        ai_status_summary = {
            "ai_status": "unavailable",
            "ai_status_reason": "no_results",
            "ai_model": ai_config.get("ai_model"),
            "ai_available": False,
            "ai_api_key_present": ai_config.get("ai_api_key_present", False),
        }
    return {
        "status": "ok",
        "results": results,
        "news_collection_debug": report.get("news_collection_debug") or {},
        "ai_status": ai_status_summary,
    }


def _api_result_to_dict(api_result: dict) -> dict:
    return {
        "title": api_result.get("title") or "",
        "original_url": api_result.get("original_url") or "",
        "topic": api_result.get("topic") or "",
        "claims": api_result.get("claims") or [],
        "normalized_claims": api_result.get("normalized_claims") or [],
        "source_candidates": api_result.get("source_candidates") or [],
        "source_queries": api_result.get("source_queries") or [],
        "evidence_snippets": api_result.get("evidence_snippets") or [],
        "claim_evidence_map": api_result.get("claim_evidence_map") or {},
        "claim_evidence_quality_summary": api_result.get("claim_evidence_quality_summary") or [],
        "evidence_quality_summary": api_result.get("evidence_quality_summary") or {},
        "contradiction_checks": api_result.get("contradiction_checks") or [],
        "contradiction_summary": api_result.get("contradiction_summary") or {},
        "bias_framing_analysis": api_result.get("bias_framing_analysis") or [],
        "bias_framing_summary": api_result.get("bias_framing_summary") or {},
        "debug_summary": api_result.get("debug_summary") or {},
        "policy_confidence": api_result.get("policy_confidence") or {},
        "policy_impact": api_result.get("policy_impact") or {},
        "final_decision": api_result.get("final_decision") or {},
        "verification_card": api_result.get("verification_card") or {},
        "claim_text": api_result.get("claim_text") or "",
        "verdict_label": api_result.get("verdict_label") or "",
        "verdict_confidence": api_result.get("verdict_confidence") or 0,
        "evidence_sources": api_result.get("evidence_sources") or [],
        "source_reliability_score": api_result.get("source_reliability_score") or 0,
        "source_reliability_reason": api_result.get("source_reliability_reason") or "",
        "evidence_summary": api_result.get("evidence_summary") or "",
        "missing_context": api_result.get("missing_context") or [],
        "last_checked_at": api_result.get("last_checked_at") or "",
        "review_status": api_result.get("review_status") or "",
        "ai_status": api_result.get("ai_status") or "unavailable",
        "ai_status_reason": api_result.get("ai_status_reason") or "unknown",
        "ai_model": api_result.get("ai_model") or "",
        "ai_available": bool(api_result.get("ai_available")),
    }


def _persist_pipeline_report(report: dict, *, query: str) -> Optional[int]:
    """Save each news_result to SQLite; return an id that links the job to a row.

    When a URL is a duplicate (already saved by an earlier run), the save call
    returns ``id=None``. We then look up the existing row's id so the job stays
    linked to a real analysis_results row — this is what lets /jobs/{id}/result
    recover after the in-memory cache is gone.
    """
    last_linked_id: Optional[int] = None
    for item in report.get("news_results", []) or []:
        api_result = item.get("api_result") or {}
        if not api_result:
            continue
        api_result = sanitize_data(api_result)
        try:
            save_status = save_analysis_result(api_result, query=query)
            if save_status.get("saved"):
                last_linked_id = save_status.get("id") or last_linked_id
                try:
                    pg_status = postgres_dual_write(api_result, query=query)
                    if pg_status.get("attempted") and not pg_status.get("ok"):
                        logger.warning(
                            "Postgres dual-write failed during job save: %s",
                            pg_status.get("error"),
                        )
                except Exception:
                    logger.exception(
                        "Postgres dual-write raised during job save; SQLite remains source of truth"
                    )
            else:
                logger.info(
                    "Duplicate skipped in SQLite during job save: %s",
                    api_result.get("title"),
                )
                try:
                    existing_id = get_result_id_by_url(api_result.get("original_url") or "")
                    if existing_id is not None:
                        last_linked_id = existing_id
                except Exception:
                    logger.exception(
                        "Failed to resolve existing analysis_results id for duplicate URL"
                    )
        except Exception:
            logger.exception("Failed to save analysis result during job execution")
    return last_linked_id


def _run_pipeline_for_job(job_id: str, query: str, max_news: int) -> Optional[int]:
    """Synchronous wrapper that runs the pipeline and stamps coarse progress stages."""
    job_manager.update_progress(job_id, job_manager.STAGE_PIPELINE_STARTED, 10)
    report = analyze_pipeline(query=query, max_news=max_news)
    job_manager.update_progress(job_id, job_manager.STAGE_SAVING_RESULT, 90)
    result_id = _persist_pipeline_report(report, query=query)
    try:
        _cache_job_report(job_id, _build_async_analyze_payload(report, query))
    except Exception:
        logger.exception("Failed to cache job report payload: id=%s", job_id)
    return result_id


def _parse_json_column(value):
    """Inflate a JSON-string column from analysis_results back to a Python value."""
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _inflate_stored_result_row(row: dict) -> dict:
    """Convert an analysis_results SQLite row into an AnalyzeResult-shaped dict.

    Used when the in-memory job report cache has been evicted (or the server
    restarted) but the linked SQLite row is still available. This is a best-
    effort reconstruction — fields that were never persisted are returned as
    empty/zero defaults, never as fabricated values.
    """
    verification_card = _parse_json_column(row.get("debug_summary"))
    debug_summary = verification_card if isinstance(verification_card, dict) else {}

    return {
        "title": row.get("title") or "",
        "original_url": row.get("original_url") or "",
        "topic": row.get("topic") or "",
        "claims": _parse_json_column(row.get("claims")) or [],
        "normalized_claims": _parse_json_column(row.get("normalized_claims")) or [],
        "source_candidates": _parse_json_column(row.get("source_candidates")) or [],
        "source_queries": _parse_json_column(row.get("source_queries")) or [],
        "evidence_snippets": _parse_json_column(row.get("evidence_snippets")) or [],
        "claim_evidence_map": _parse_json_column(row.get("claim_evidence_map")) or {},
        "claim_evidence_quality_summary": [],
        "evidence_quality_summary": {},
        "contradiction_checks": _parse_json_column(row.get("contradiction_checks")) or [],
        "contradiction_summary": _parse_json_column(row.get("contradiction_summary")) or {},
        "bias_framing_analysis": _parse_json_column(row.get("bias_framing_analysis")) or [],
        "bias_framing_summary": _parse_json_column(row.get("bias_framing_summary")) or {},
        "debug_summary": debug_summary,
        "policy_confidence": {
            "policy_confidence_score": row.get("policy_confidence_score"),
            "verification_strength": row.get("verification_strength"),
            "risk_level": row.get("risk_level"),
            "action_priority": row.get("action_priority"),
        },
        "policy_impact": {
            "impact_level": row.get("impact_level"),
            "impact_direction": row.get("impact_direction"),
            "market_sensitivity": row.get("market_sensitivity"),
            "consumer_sensitivity": row.get("consumer_sensitivity"),
            "business_sensitivity": row.get("business_sensitivity"),
        },
        "final_decision": {
            "policy_alert_level": row.get("policy_alert_level"),
            "market_signal": _parse_json_column(row.get("market_signal")),
        },
        "verification_card": {
            "claim_text": row.get("claim_text") or "",
            "verdict_label": row.get("verdict_label") or "",
            "verdict_confidence": row.get("verdict_confidence") or 0,
            "evidence_sources": _parse_json_column(row.get("evidence_sources")) or [],
            "source_reliability_score": row.get("source_reliability_score") or 0,
            "source_reliability_reason": row.get("source_reliability_reason") or "",
            "evidence_summary": row.get("evidence_summary") or "",
            "missing_context": _parse_json_column(row.get("missing_context")) or [],
            "last_checked_at": row.get("last_checked_at") or "",
            "review_status": row.get("review_status") or "",
        },
        "claim_text": row.get("claim_text") or "",
        "verdict_label": row.get("verdict_label") or "",
        "verdict_confidence": row.get("verdict_confidence") or 0,
        "evidence_sources": _parse_json_column(row.get("evidence_sources")) or [],
        "source_reliability_score": row.get("source_reliability_score") or 0,
        "source_reliability_reason": row.get("source_reliability_reason") or "",
        "evidence_summary": row.get("evidence_summary") or "",
        "missing_context": _parse_json_column(row.get("missing_context")) or [],
        "last_checked_at": row.get("last_checked_at") or "",
        "review_status": row.get("review_status") or "",
        "ai_status": "ok",
        "ai_status_reason": "stored_result_reconstructed",
        "ai_model": "",
        "ai_available": False,
    }


def _build_async_payload_from_stored_row(row: dict) -> dict:
    """Wrap a stored analysis_results row in the same shape as the in-memory cache."""
    return {
        "status": "ok",
        "results": [_inflate_stored_result_row(row)],
        "news_collection_debug": {},
        "ai_status": {
            "ai_status": "ok",
            "ai_status_reason": "stored_result_reconstructed",
            "ai_model": "",
            "ai_available": False,
            "ai_api_key_present": describe_ai_config().get("ai_api_key_present", False),
        },
    }


async def _execute_job(job_id: str, query: str, max_news: int, timeout_seconds: int) -> None:
    """Background coroutine: drives the job through its stages with timeout protection."""
    try:
        job_manager.start_job(job_id)
        try:
            result_id = await asyncio.wait_for(
                asyncio.to_thread(_run_pipeline_for_job, job_id, query, max_news),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            job_manager.timeout_job(
                job_id,
                error_message=f"pipeline exceeded {timeout_seconds}s timeout",
            )
            return
        job_manager.complete_job(job_id, result_id)
    except Exception as error:
        logger.exception("Job execution failed: id=%s", job_id)
        try:
            job_manager.fail_job(job_id, f"{type(error).__name__}: {error}")
        except Exception:
            logger.exception("Failed to record job failure: id=%s", job_id)


@app.post("/jobs/analyze", response_model=JobStatusResponse)
async def jobs_analyze(request: JobCreateRequest) -> JobStatusResponse:
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if request.max_news <= 0:
        raise HTTPException(status_code=400, detail="max_news must be greater than 0")

    timeout_seconds = request.timeout_seconds or job_manager.get_default_job_timeout_seconds()
    timeout_seconds = max(30, min(int(timeout_seconds), 3600))

    record = job_manager.create_job(query=query, max_news=request.max_news)
    logger.info(
        "Async job accepted: id=%s query=%s max_news=%s timeout=%ss",
        record["id"], query, request.max_news, timeout_seconds,
    )
    task = asyncio.create_task(
        _execute_job(record["id"], query, request.max_news, timeout_seconds)
    )
    _track_background_task(task)
    return _job_status_to_response(record)


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def jobs_status(job_id: str) -> JobStatusResponse:
    record = job_manager.get_job_status(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_status_to_response(record)


@app.get("/jobs/{job_id}/result")
def jobs_result(job_id: str) -> dict:
    record = job_manager.get_job_status(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")

    job_status = record.get("status")
    job_dump = _job_status_to_response(record).model_dump()

    if job_status in {job_manager.STATUS_QUEUED, job_manager.STATUS_RUNNING}:
        raise HTTPException(status_code=409, detail=f"job is {job_status}")

    if job_status in {job_manager.STATUS_FAILED, job_manager.STATUS_TIMEOUT}:
        return {
            "status": "error",
            "job_status": job_status,
            "result": None,
            "stored_result": None,
            "result_source": None,
            "error_message": record.get("error_message") or "",
            "job": job_dump,
        }

    # Completed: prefer the rich in-memory payload; if evicted (or server
    # restarted), reconstruct from the linked SQLite row so the UI still has a
    # usable response. Only when *neither* exists do we report unavailability,
    # so the client never gets a false success with result=null.
    cached_payload = _JOB_REPORT_CACHE.get(job_id)
    result_row = job_manager.get_job_result(job_id)

    if cached_payload is not None:
        return {
            "status": "ok",
            "job_status": job_status,
            "result": cached_payload,
            "stored_result": result_row,
            "result_source": "cache",
            "error_message": None,
            "job": job_dump,
        }

    if result_row is not None:
        try:
            reconstructed = _build_async_payload_from_stored_row(result_row)
        except Exception:
            logger.exception(
                "Failed to reconstruct payload from stored row: job=%s result_id=%s",
                job_id, record.get("result_id"),
            )
            reconstructed = None
        if reconstructed is not None:
            return {
                "status": "ok",
                "job_status": job_status,
                "result": reconstructed,
                "stored_result": result_row,
                "result_source": "stored_result",
                "error_message": None,
                "job": job_dump,
            }

    # Job is marked completed but no payload is available anywhere. Return a
    # clearly-flagged response — not a false ok — so the UI can fall back.
    return {
        "status": "result_unavailable",
        "job_status": job_status,
        "result": None,
        "stored_result": None,
        "result_source": None,
        "error_message": (
            "completed job has no cached payload and no linked SQLite row "
            "(cache evicted after restart, or no rows were saved)"
        ),
        "job": job_dump,
    }


@app.get("/history/{result_id}")
def history_detail(result_id: int) -> dict:
    try:
        result = get_result_by_id(result_id)
    except Exception:
        logger.exception("Failed to load analysis history item")
        raise HTTPException(status_code=500, detail="failed to load history item")

    if not result:
        raise HTTPException(status_code=404, detail="history item not found")

    return {"status": "ok", "result": result}
