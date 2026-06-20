import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import accounts
from config import cors_allowed_origins, describe_ai_config, session_secret_key
from database import (
    create_review_task,
    get_account_by_username,
    get_recent_results,
    get_result_by_id,
    get_result_id_by_url,
    get_review_task,
    list_review_decisions,
    list_review_tasks,
    record_review_decision,
    save_analysis_result,
    set_analysis_human_review,
    update_review_task_status,
)
from db.postgres import (
    is_dual_write_enabled,
    is_postgres_enabled,
)
import job_manager
from main import analyze_pipeline
from rate_limit import analyze_rate_limiter
from request_context import (
    new_request_id,
    reset_request_id,
    set_request_id,
)
import review_workflow
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
    # M12.0e-7: SQLite is fully retired (0e-6b-3); Postgres is the single
    # source of truth. These startup lines were updated to drop the stale
    # "dual-write" / "SQLite remains source of truth" framing. The
    # is_dual_write_enabled / is_postgres_enabled gate names are
    # unchanged (db.postgres API), only the log text.
    if is_dual_write_enabled():
        logger.info(
            "Postgres enabled (DATABASE_URL set, USE_POSTGRES_WRITE=true); "
            "Postgres is the source of truth."
        )
    elif is_postgres_enabled():
        logger.info(
            "Postgres reachable (DATABASE_URL set) but writes disabled "
            "(USE_POSTGRES_WRITE not true); durable persistence unavailable."
        )
    else:
        logger.info(
            "Postgres not configured (DATABASE_URL not set); "
            "durable persistence unavailable."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # M12.0e-6b-3: SQLite machinery retired. The Postgres mirror schema
    # is created lazily by postgres_storage.ensure_schema on the first
    # engine build (inside get_engine), so no startup DB init is needed.
    _log_ai_config_startup()
    _log_postgres_startup()
    yield


app = FastAPI(title="Policy AI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# AUTH-2b — signed httponly session cookie for the operator login. secret_key
# comes from config.session_secret_key() (SESSION_SECRET_KEY env; per-process
# random fallback + WARNING when unset — never a hardcoded constant). https_only
# is False for now (same-origin app served by this service); can harden once
# the login UI ships in 2c.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret_key(),
    session_cookie="policy_ai_session",
    same_site="lax",
    https_only=False,
)


# M14.3a — per-request correlation. Generate (or accept) a request ID,
# inject it into the contextvars context so structured_logging's
# JsonFormatter automatically includes it in every log line emitted
# during the request, then echo it back in the response so the client
# can correlate. The middleware is purely additive: no business logic
# is touched and the context is reset in a ``finally`` block so an
# exception inside a handler does not leak the ID to subsequent
# unrelated requests.


_REQUEST_ID_CHARS = frozenset(
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "_-"
)


def _is_valid_request_id(rid: str) -> bool:
    """Accept URL-safe trace IDs: ASCII alphanumerics, underscores,
    and hyphens, length 8–64. Permits hex / UUID forms ("abc12345",
    "550e8400-e29b-41d4-a716-446655440000") AND human-readable
    client-supplied traces ("my-trace-123abc", "client_session_42").

    Rejects anything that would be unsafe to echo into a response
    header or surface in logs: empty, too short, too long, spaces,
    newlines, slashes, quotes, non-ASCII characters, path traversal
    payloads, etc.
    """
    if not rid:
        return False
    if len(rid) > 64 or len(rid) < 8:
        return False
    return all(c in _REQUEST_ID_CHARS for c in rid)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    incoming = (request.headers.get("x-request-id") or "").strip()
    rid = incoming if _is_valid_request_id(incoming) else new_request_id()
    token = set_request_id(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        reset_request_id(token)


app.mount("/web", StaticFiles(directory="web"), name="web")


class AnalyzeRequest(BaseModel):
    query: str
    max_news: int = 3


class AnalyzeResult(BaseModel):
    # Phase 2 M3: result_id links a per-result response payload back to the
    # analysis_results row. The frontend persists only this id in localStorage
    # and rehydrates the full result via GET /history/{result_id} on demand.
    result_id: Optional[int] = None
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
    human_reviewed_at: Optional[str] = None
    human_reviewed_by: Optional[str] = None
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


# M15.0a — Phase 2 job-queue foundation. Returns the RQ queue's
# current health so an operator can verify Redis connectivity, queue
# depth, and worker count without touching `/health` (which is the
# liveness probe and must stay byte-identical). Graceful degradation
# is baked in: if REDIS_URL is unset or Redis is unreachable, the
# response still returns 200 with `redis_connected=False`,
# `queue_depth=0`, `workers_count=0` — the endpoint never raises and
# never causes an outage. See `job_queue.py` for the full contract.
@app.get("/health/queue")
def health_queue() -> dict:
    try:
        import job_queue
        return job_queue.get_queue_health()
    except Exception as error:
        # Defence-in-depth: even an unexpected exception inside
        # job_queue must not 5xx the health endpoint. Report it
        # as degraded with the exception type so operators can
        # diagnose without an outage.
        logger.warning(
            "health_queue endpoint degraded: %s: %s",
            type(error).__name__,
            error,
        )
        return {
            "redis_connected": False,
            "queue_depth": 0,
            "workers_count": 0,
            "queue_name": "default",
            "redis_url_set": False,
            "error": f"{type(error).__name__}: {str(error)[:200]}",
        }


@app.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(analyze_rate_limiter)])
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="검색어는 200자 이내로 입력해주세요.")
    if request.max_news <= 0:
        raise HTTPException(status_code=400, detail="max_news must be greater than 0")
    # SEC-4 — clamp max_news to [1, 10] so a large value can't multiply
    # per-item pipeline cost/latency. Silent clamp (mirrors the
    # timeout_seconds clamp in /jobs/analyze); the <=0 guard above still
    # rejects non-positive values with a 400 first.
    max_news = max(1, min(request.max_news, 10))

    started = time.perf_counter()
    logger.info("Analyze request received: query=%s max_news=%s", query, max_news)

    report = analyze_pipeline(query=query, max_news=max_news)
    results = []
    # M15-dedup-1 Part B — defensive dedup at response boundary.
    # main.py's post-resolve URL dedup pass should suppress duplicates
    # before Phase B, but if any slip through (e.g., gnewsdecoder
    # returns the same google_link for two different syndications and
    # both are treated as decode-failures → preserved), we filter the
    # response array here so the frontend never sees two cards with
    # the same result_id. Belt-and-suspenders.
    seen_result_ids: set = set()
    for item in report.get("news_results", []):
        api_result = item.get("api_result") or {}
        if not api_result:
            continue
        api_result = sanitize_data(api_result)

        # Persist first so we can attach the resulting row id to the response,
        # which lets the frontend keep only a slim reference in localStorage
        # and rehydrate the full result via GET /history/{result_id}.
        result_id: Optional[int] = None
        try:
            save_status = save_analysis_result(api_result, query=query)
            if save_status.get("duplicate"):
                logger.info("Duplicate skipped in SQLite: %s", api_result.get("title"))
                try:
                    result_id = get_result_id_by_url(api_result.get("original_url") or "")
                except Exception:
                    logger.exception(
                        "Failed to resolve existing analysis_results id for duplicate URL"
                    )
            else:
                result_id = save_status.get("id")
        except Exception:
            logger.exception("Failed to save analysis result to SQLite")

        # M15-dedup-1 Part B — skip if we've already emitted this
        # result_id this request. Null result_ids (save failed) are
        # passed through unfiltered since we have no key to compare.
        if result_id is not None and result_id in seen_result_ids:
            continue
        if result_id is not None:
            seen_result_ids.add(result_id)

        results.append(
            AnalyzeResult(
                result_id=result_id,
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
                human_reviewed_at=api_result.get("human_reviewed_at"),
                human_reviewed_by=api_result.get("human_reviewed_by"),
                ai_status=api_result.get("ai_status") or "unavailable",
                ai_status_reason=api_result.get("ai_status_reason") or "unknown",
                ai_model=api_result.get("ai_model") or "",
                ai_available=bool(api_result.get("ai_available")),
            )
        )

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
    # M15-dedup-1 Part B — defensive dedup at response boundary (async
    # path). main.py's post-resolve dedup is the primary guard;
    # this is belt-and-suspenders so the UI never sees two cards
    # with the same result_id even if a duplicate slips through.
    seen_result_ids: set = set()
    for item in report.get("news_results", []) or []:
        api_result = item.get("api_result") or {}
        if not api_result:
            continue
        api_result = sanitize_data(api_result)
        rid = api_result.get("result_id")
        if rid is not None and rid in seen_result_ids:
            continue
        if rid is not None:
            seen_result_ids.add(rid)
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
        "result_id": api_result.get("result_id"),
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
        "human_reviewed_at": api_result.get("human_reviewed_at"),
        "human_reviewed_by": api_result.get("human_reviewed_by"),
        "ai_status": api_result.get("ai_status") or "unavailable",
        "ai_status_reason": api_result.get("ai_status_reason") or "unknown",
        "ai_model": api_result.get("ai_model") or "",
        "ai_available": bool(api_result.get("ai_available")),
        # DISPLAY-CATEGORY 2-A: forward the domain category label (metadata
        # only; never a verdict field). Default None when absent so the live
        # /analyze response shape carries domain just like /history rows do.
        "domain": api_result.get("domain"),
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
                saved_id = save_status.get("id")
                last_linked_id = saved_id or last_linked_id
                if saved_id is not None:
                    api_result["result_id"] = saved_id
                    item["api_result"] = api_result
            else:
                logger.info(
                    "Duplicate skipped in SQLite during job save: %s",
                    api_result.get("title"),
                )
                try:
                    existing_id = get_result_id_by_url(api_result.get("original_url") or "")
                    if existing_id is not None:
                        last_linked_id = existing_id
                        api_result["result_id"] = existing_id
                        item["api_result"] = api_result
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
        "result_id": row.get("id"),
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
            "human_reviewed_at": row.get("human_reviewed_at"),
            "human_reviewed_by": row.get("human_reviewed_by"),
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
        "human_reviewed_at": row.get("human_reviewed_at"),
        "human_reviewed_by": row.get("human_reviewed_by"),
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


@app.post("/jobs/analyze", response_model=JobStatusResponse, dependencies=[Depends(analyze_rate_limiter)])
async def jobs_analyze(request: JobCreateRequest) -> JobStatusResponse:
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="검색어는 200자 이내로 입력해주세요.")
    if request.max_news <= 0:
        raise HTTPException(status_code=400, detail="max_news must be greater than 0")
    # SEC-4 — clamp max_news to [1, 10] (silent; mirrors the timeout_seconds
    # clamp below). The <=0 guard above still rejects non-positive values.
    max_news = max(1, min(request.max_news, 10))

    timeout_seconds = request.timeout_seconds or job_manager.get_default_job_timeout_seconds()
    timeout_seconds = max(30, min(int(timeout_seconds), 3600))

    record = job_manager.create_job(query=query, max_news=max_news)
    logger.info(
        "Async job accepted: id=%s query=%s max_news=%s timeout=%ss",
        record["id"], query, max_news, timeout_seconds,
    )
    task = asyncio.create_task(
        _execute_job(record["id"], query, max_news, timeout_seconds)
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


# ---------------------------------------------------------------------------
# M15.0b — V2 async endpoints (RQ + SSE).
#
# These endpoints are OPT-IN and live in a separate URL namespace
# (/v2/*) so the existing /analyze and /jobs/* contracts stay byte-
# identical for current clients. M15.0c will rewire the frontend to
# prefer the V2 flow.
#
# Graceful degradation:
#   - POST /v2/analyze            → 503 when Redis is unavailable
#   - GET  /v2/jobs/{job_id}      → 503 when Redis is unavailable
#   - GET  /v2/jobs/{job_id}/stream → SSE stream that emits a single
#                                     "unavailable" event when Redis
#                                     is down, then closes
#
# No Background Worker is auto-started — operator decides separately.
# Without a worker, jobs queue successfully but never execute (status
# stays "queued" until RQ's default expiry).
# ---------------------------------------------------------------------------


class V2AnalyzeResponse(BaseModel):
    job_id: str
    status: str = "queued"
    created_at: str
    queue_name: str = "default"


def _v2_serialize_job_status(payload: dict) -> dict:
    """Distil ``job_queue.get_job_status``'s payload into the V2
    response shape. We keep the original keys + add a top-level
    ``progress_percent`` derived from the most-recent progress
    event (if available) for parity with the existing /jobs/{id}
    response shape. M15.0b leaves progress_percent at 0 here
    because RQ doesn't natively track it — the SSE stream is the
    progress channel."""
    return {
        "job_id": payload.get("job_id") or "",
        "status": payload.get("status") or "unavailable",
        "result": payload.get("result"),
        "error": payload.get("error"),
        "enqueued_at": payload.get("enqueued_at"),
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "progress_percent": 0,
        "current_step": None,
    }


@app.post("/v2/analyze", response_model=V2AnalyzeResponse, status_code=202, dependencies=[Depends(analyze_rate_limiter)])
def v2_analyze(request: AnalyzeRequest) -> V2AnalyzeResponse:
    """Enqueue an analysis job and return immediately with a job_id.

    The job is executed by a separate RQ worker process (see
    ``worker.py`` and ``docs/JOB_QUEUE.md``). The existing /analyze
    endpoint is unchanged.
    """
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="검색어는 200자 이내로 입력해주세요.")
    if request.max_news <= 0:
        raise HTTPException(status_code=400, detail="max_news must be greater than 0")
    # SEC-4 — clamp max_news to [1, 10] (silent). The <=0 guard above still
    # rejects non-positive values with a 400 first.
    max_news = max(1, min(request.max_news, 10))

    import job_queue
    queue = job_queue.get_queue()
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "redis_unavailable: /v2/analyze requires a reachable "
                "REDIS_URL. The existing /analyze endpoint remains "
                "available as a synchronous fallback."
            ),
        )

    import pipeline_worker
    # RQ ignores keyword args destined for the job itself when the
    # job's own kwargs collide with RQ's reserved names; we pass via
    # positional args + an explicit kwargs dict to keep the contract
    # explicit. The job_id is generated by RQ when not pre-supplied;
    # we capture it AFTER enqueue.
    try:
        job = queue.enqueue(
            pipeline_worker.run_analyze_pipeline_job,
            args=(query, int(max_news)),
            kwargs={"job_id": ""},
            job_timeout=600,
        )
    except Exception as error:  # noqa: BLE001 — graceful degradation
        logger.warning(
            "v2_analyze.enqueue_failed: %s: %s",
            type(error).__name__, error,
        )
        raise HTTPException(
            status_code=503,
            detail=f"redis_enqueue_failed: {type(error).__name__}",
        )

    # Patch the job's args to include its own RQ-assigned id so the
    # worker can publish progress events to the right pub/sub channel.
    try:
        job.kwargs = {**(job.kwargs or {}), "job_id": str(job.id)}
        job.save()
    except Exception:
        logger.warning(
            "v2_analyze.job_id_attach_failed: id=%s",
            getattr(job, "id", "<unknown>"),
        )

    logger.info(
        "v2_analyze.enqueued: job_id=%s query=%s max_news=%s",
        job.id, query, max_news,
    )
    return V2AnalyzeResponse(
        job_id=str(job.id),
        status="queued",
        created_at=datetime.now(timezone.utc).isoformat(),
        queue_name="default",
    )


@app.get("/v2/jobs/{job_id}")
def v2_job_status(job_id: str) -> dict:
    """Return the current status of an RQ-backed job. Returns 404
    when the job_id is not in Redis (job expired or never enqueued)."""
    import job_queue
    payload = job_queue.get_job_status(job_id)
    if payload.get("status") == "unavailable":
        raise HTTPException(
            status_code=503,
            detail=f"redis_unavailable: {payload.get('error') or 'unknown'}",
        )
    if payload.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="job_id not found")
    payload["job_id"] = job_id
    return _v2_serialize_job_status(payload)


def _sse_format(event: str, data: dict) -> str:
    """Serialize a single SSE event in the standard
    ``event: <name>\\ndata: <json>\\n\\n`` format."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _v2_stream_generator(job_id: str, max_seconds: float = 600.0):
    """SSE generator for /v2/jobs/{job_id}/stream.

    Subscribes to ``job:{job_id}:progress`` AND polls
    ``job_queue.get_job_status`` so terminal-state transitions and
    race-conditioned progress events are both reliably surfaced.
    Auto-closes on terminal status or ``max_seconds`` timeout.

    Never raises out of the generator — any unexpected error is
    yielded as a final ``error`` event so the client gets a clean
    stream close instead of an HTTP 5xx mid-stream.
    """
    import job_queue
    started = time.monotonic()

    # Initial status probe + degraded path.
    initial = job_queue.get_job_status(job_id)
    if initial.get("status") == "unavailable":
        yield _sse_format("unavailable", {
            "job_id": job_id,
            "reason": initial.get("error") or "redis_unavailable",
        })
        return
    if initial.get("status") == "not_found":
        yield _sse_format("not_found", {"job_id": job_id})
        return

    yield _sse_format("status", _v2_serialize_job_status(
        {**initial, "job_id": job_id}
    ))

    # Try to subscribe to the pub/sub progress channel. If subscribe
    # itself fails, fall back to status polling only.
    client = job_queue.get_redis_connection()
    pubsub = None
    if client is not None:
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(f"job:{job_id}:progress")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "v2_stream.pubsub_subscribe_failed: %s: %s",
                type(exc).__name__, exc,
            )
            pubsub = None

    terminal_statuses = {"finished", "failed", "stopped", "canceled"}
    last_status = initial.get("status")
    try:
        while True:
            if time.monotonic() - started > max_seconds:
                yield _sse_format("timeout", {
                    "job_id": job_id,
                    "max_seconds": max_seconds,
                })
                return

            # Drain available pub/sub messages first.
            if pubsub is not None:
                try:
                    message = pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.5,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "v2_stream.pubsub_get_message_failed: %s: %s",
                        type(exc).__name__, exc,
                    )
                    message = None
                if message and message.get("type") == "message":
                    raw = message.get("data")
                    if isinstance(raw, (bytes, bytearray)):
                        try:
                            raw = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            raw = ""
                    try:
                        payload = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        payload = {"raw": str(raw)[:200]}
                    yield _sse_format("progress", payload)

            # Then poll status — catches terminal transitions even
            # when pub/sub missed an event.
            try:
                current = job_queue.get_job_status(job_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "v2_stream.status_poll_failed: %s: %s",
                    type(exc).__name__, exc,
                )
                current = {"status": "unavailable"}

            current_status = current.get("status")
            if current_status != last_status:
                yield _sse_format("status", _v2_serialize_job_status(
                    {**current, "job_id": job_id}
                ))
                last_status = current_status

            if current_status in terminal_statuses:
                yield _sse_format(
                    "completed" if current_status == "finished" else "failed",
                    _v2_serialize_job_status({**current, "job_id": job_id}),
                )
                return

            time.sleep(1.0)
    finally:
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass


@app.get("/v2/jobs/{job_id}/stream")
def v2_job_stream(job_id: str):
    """SSE stream of job progress events.

    Emits ``status`` events on status transitions, ``progress`` events
    from the Redis pub/sub channel, and a single terminal event
    (``completed`` | ``failed`` | ``timeout`` | ``unavailable`` |
    ``not_found``) before closing.
    """
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _v2_stream_generator(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx proxy buffering
        },
    )


# ---------------------------------------------------------------------------
# Phase 2 M8.0: server-backed reviewer workflow endpoints.
#
# Admin-only — gated by ``require_admin`` (session-only since AUTH-2d): a
# request without an authenticated session is rejected with 401.
# No endpoint here mutates analysis_results, final_decision,
# policy_confidence, verification_card, or any verdict-side field. The
# only writes are to review_tasks / review_decisions (the new tables).
# ---------------------------------------------------------------------------


# Pydantic request bodies. Keep them lean — most fields are optional so
# the reviewer client doesn't have to ship the entire payload back.
class _ReviewTaskFromResultRequest(BaseModel):
    result_id: Optional[str] = None
    job_id: Optional[str] = None
    item_index: int = 0
    result_payload: Optional[dict] = None
    query: Optional[str] = None


class _ReviewDecisionRequest(BaseModel):
    decision: str
    reviewer_id: Optional[str] = None
    comment: Optional[str] = None
    public_note: Optional[str] = None
    # Phase 2 M9.0 — optional operator-supplied audit label. NOT auth,
    # never derived from REVIEW_API_TOKEN, never echoed back as identity.
    # Unknown values fall back to "unknown" via
    # review_workflow.normalize_decision_source.
    decision_source: Optional[str] = None


class _PromoteReviewRequest(BaseModel):
    # M40a — promote=True stamps the human-reviewed columns; promote=False
    # un-promotes (NULLs both). reviewer is a display label only, NOT auth
    # (auth is the require_admin session gate); defaults to "operator".
    promote: bool = True
    reviewer: Optional[str] = None


# ---------------------------------------------------------------------------
# AUTH-2d — account login (session) is the ONLY admin auth path.
#
# /auth/login verifies username+password (bcrypt) and starts a signed session.
# require_admin authorizes the review surface ONLY when the signed session
# carries an authenticated admin marker. The legacy X-Review-Token gate was
# retired in AUTH-2d (review_auth.py deleted); there is no token fallback. A
# request without a valid session is rejected with 401. None of this touches
# any verdict/scoring field.
# ---------------------------------------------------------------------------

_SESSION_AUTH_KEY = "authenticated"
_SESSION_USERNAME_KEY = "username"
_SESSION_ROLE_KEY = "role"


def _session_is_authenticated(request: Request) -> bool:
    """True iff the signed session carries the authenticated-admin marker.
    Returns False (never raises) when the session is absent/unreadable."""
    try:
        return bool(request.session.get(_SESSION_AUTH_KEY))
    except (AssertionError, AttributeError):
        # SessionMiddleware not installed / no session scope — treat as anon.
        return False


def require_admin(request: Request) -> None:
    """Session-only admin gate for the review surface.

    Authorizes ONLY when the signed session cookie carries an authenticated
    admin marker (set by /auth/login). With no valid session the request is
    rejected with 401 — there is no token fallback (retired in AUTH-2d).
    """
    if _session_is_authenticated(request):
        return None
    raise HTTPException(status_code=401, detail="authentication required")


class _LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def auth_login(body: _LoginRequest, request: Request) -> dict:
    """Verify credentials and start an authenticated admin session.

    On success: populate the session and return ``{ok: true, role}``. On ANY
    failure (unknown username OR wrong password) return a GENERIC 401 with an
    identical shape — never reveal which was wrong (no user enumeration) and
    never echo the submitted password.
    """
    username = (body.username or "").strip()
    account = None
    if username:
        try:
            account = get_account_by_username(username)
        except Exception:
            logger.exception("auth_login: account lookup failed")
            account = None
    # verify_password returns False (never raises) on a missing/empty hash, so
    # the unknown-user and wrong-password branches collapse to one generic 401.
    stored_hash = (account or {}).get("password_hash") or ""
    password_ok = accounts.verify_password(body.password or "", stored_hash)
    if not account or not password_ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    role = account.get("role") or "admin"
    request.session[_SESSION_AUTH_KEY] = True
    request.session[_SESSION_USERNAME_KEY] = username
    request.session[_SESSION_ROLE_KEY] = role
    return {"ok": True, "role": role}


@app.post("/auth/logout")
def auth_logout(request: Request) -> dict:
    """Clear the session (logout). Idempotent — safe to call when not logged in."""
    try:
        request.session.clear()
    except (AssertionError, AttributeError):
        pass
    return {"ok": True}


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    """Report session auth state (session-only; ignores the token header).
    Read-only and secret-free — used by the 2c frontend to decide whether to
    reveal admin tools."""
    if _session_is_authenticated(request):
        return {
            "authenticated": True,
            "role": request.session.get(_SESSION_ROLE_KEY) or "admin",
        }
    return {"authenticated": False}


def _load_payload_for_review(
    *, result_id: Optional[str], job_id: Optional[str],
    explicit_payload: Optional[dict],
) -> dict:
    """Resolve the analysis payload we'll snapshot from.

    Priority: explicit ``result_payload`` body field > job cache (when
    ``job_id`` matches a recent in-process job) > stored history row
    (when ``result_id`` is a valid integer). Returns ``{}`` when nothing
    can be resolved; the caller surfaces a 400 in that case.
    """
    if isinstance(explicit_payload, dict) and explicit_payload:
        return explicit_payload
    # Try the in-process job cache first — same path /jobs/{id}/result uses.
    if job_id:
        cached = _JOB_REPORT_CACHE.get(str(job_id))
        if isinstance(cached, dict) and cached:
            return cached
    # Fall back to the stored history row.
    if result_id:
        try:
            row_id = int(result_id)
        except (TypeError, ValueError):
            row_id = None
        if row_id is not None:
            try:
                stored = get_result_by_id(row_id)
            except Exception:
                stored = None
            if isinstance(stored, dict) and stored:
                # Wrap in the same shape /jobs/{id}/result uses so the
                # snapshot extractor can find the news-results array.
                return {"result": {"results": [stored]}}
    return {}


@app.get("/review/tasks")
def review_list_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_admin),
) -> dict:
    """List review tasks (newest first). Optional ``status`` filter.

    Status filter is normalized via ``review_workflow.normalize_review_status``;
    an unknown status returns 400 rather than silently returning all rows.
    """
    status_filter: Optional[str] = None
    if status:
        try:
            status_filter = review_workflow.normalize_review_status(status)
        except review_workflow.ReviewWorkflowError as error:
            raise HTTPException(status_code=400, detail=str(error))
    try:
        rows = list_review_tasks(status=status_filter, limit=limit, offset=offset)
    except Exception:
        logger.exception("Failed to list review tasks")
        raise HTTPException(status_code=500, detail="failed to list review tasks")
    return {
        "tasks": [review_workflow.summarize_review_task(r) for r in rows],
        "count": len(rows),
        "status_filter": status_filter,
    }


@app.get("/review/tasks/{task_id}")
def review_task_detail(
    task_id: str,
    _: None = Depends(require_admin),
) -> dict:
    """Return a task plus all decisions recorded against it."""
    task = get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="review task not found")
    decisions = review_workflow.build_decision_audit_records(
        list_review_decisions(task_id)
    )
    return {
        "task": review_workflow.detail_review_task(task, decisions=decisions),
        "decisions": decisions,
        "audit_version": review_workflow.AUDIT_SCHEMA_VERSION,
    }


@app.post("/review/tasks/from-result")
def review_create_task_from_result(
    body: _ReviewTaskFromResultRequest,
    _: None = Depends(require_admin),
) -> dict:
    """Create (or idempotently fetch) a review task from a result payload.

    The reviewer client may pass ``result_payload`` directly (the
    full ``/jobs/{id}/result`` body, for example) OR pass
    ``result_id`` / ``job_id`` so the server resolves the payload from
    the existing history / in-process job cache.

    The task is created with status ``pending_review`` and
    ``human_review_required=true``. Same logical (result_id, job_id,
    item_index, claim_text) tuple returns the same task on repeat calls.
    """
    payload = _load_payload_for_review(
        result_id=body.result_id,
        job_id=body.job_id,
        explicit_payload=body.result_payload,
    )
    if not payload:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not resolve a result payload. Provide "
                "result_payload, or a result_id / job_id that the server "
                "can hydrate."
            ),
        )

    snapshot = review_workflow.extract_review_snapshot_from_result(
        payload, item_index=body.item_index or 0, query=body.query,
    )
    claim_text = snapshot.get("claim_text") or ""
    if not claim_text:
        raise HTTPException(
            status_code=400,
            detail="Could not extract a claim from the payload — nothing to review.",
        )

    task_id = review_workflow.make_review_task_id(
        result_id=body.result_id, job_id=body.job_id,
        item_index=body.item_index or 0, claim_text=claim_text,
    )
    idempotency_key = review_workflow.make_idempotency_key(
        result_id=body.result_id, job_id=body.job_id,
        item_index=body.item_index or 0, claim_text=claim_text,
    )
    now = review_workflow.now_iso()

    try:
        task, was_existing = create_review_task(
            task_id=task_id,
            result_id=body.result_id,
            job_id=body.job_id,
            item_index=body.item_index or 0,
            status=review_workflow.STATUS_PENDING_REVIEW,
            query=snapshot.get("query") or "",
            claim_text=claim_text,
            title=snapshot.get("title") or "",
            url=snapshot.get("url") or "",
            final_decision=snapshot.get("final_decision") or "",
            policy_confidence=snapshot.get("policy_confidence") or "",
            human_review_required=bool(snapshot.get("human_review_required", True)),
            snapshot=snapshot,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
    except Exception:
        logger.exception("Failed to create review task")
        raise HTTPException(status_code=500, detail="failed to create review task")

    return {
        "task": review_workflow.detail_review_task(task, decisions=[]),
        "idempotent": bool(was_existing),
    }


@app.post("/review/tasks/{task_id}/decision")
def review_record_decision(
    task_id: str,
    body: _ReviewDecisionRequest,
    _: None = Depends(require_admin),
) -> dict:
    """Append a decision and (when the decision changes status) update
    the task's status. Append-only — decisions cannot be deleted or
    modified. Comment-only decisions do not change status.

    Important: this endpoint never publishes anything, never mutates
    analysis_results, and never changes the verdict / confidence /
    verification_card the pipeline produced.
    """
    task = get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="review task not found")

    try:
        decision = review_workflow.normalize_review_decision(body.decision)
    except review_workflow.ReviewWorkflowError as error:
        raise HTTPException(status_code=400, detail=str(error))

    current_status = task.get("status") or ""
    try:
        new_status = review_workflow.validate_status_transition(current_status, decision)
    except review_workflow.ReviewWorkflowError as error:
        # transition_not_allowed → 409 conflict; reserved/unknown → 400.
        if error.reason == "transition_not_allowed":
            raise HTTPException(status_code=409, detail=str(error))
        raise HTTPException(status_code=400, detail=str(error))

    now = review_workflow.now_iso()
    decision_id = review_workflow.make_review_decision_id()
    # Phase 2 M9.0 — normalize the optional decision_source label. Default
    # to "review_api" so HTTP-API calls without an explicit label still
    # carry a stable audit marker. Never use REVIEW_API_TOKEN here.
    decision_source = review_workflow.normalize_decision_source(
        body.decision_source,
        default=review_workflow.DECISION_SOURCE_REVIEW_API,
    )
    try:
        stored_row = record_review_decision(
            decision_id=decision_id,
            task_id=task_id,
            decision=decision,
            reviewer_id=body.reviewer_id,
            comment=body.comment,
            public_note=body.public_note,
            previous_status=current_status,
            new_status=new_status,
            created_at=now,
            metadata={},
            decision_source=decision_source,
        )
        if new_status != current_status:
            update_review_task_status(task_id, new_status=new_status, updated_at=now)
    except Exception:
        logger.exception("Failed to record review decision")
        raise HTTPException(status_code=500, detail="failed to record review decision")

    updated_task = get_review_task(task_id) or task
    decisions = review_workflow.build_decision_audit_records(
        list_review_decisions(task_id)
    )
    audit_record = review_workflow.build_decision_audit_record(stored_row)
    return {
        "task": review_workflow.detail_review_task(updated_task, decisions=decisions),
        "decision_id": decision_id,
        "previous_status": current_status,
        "new_status": new_status,
        "status_changed": new_status != current_status,
        # M9.0 audit additions — additive, existing keys preserved above.
        "transition": review_workflow.transition_label(current_status, new_status),
        "decision_source": decision_source,
        "audit_version": review_workflow.AUDIT_SCHEMA_VERSION,
        "audit_record": audit_record,
    }


@app.post("/review/results/{result_id}/promote")
def review_promote_result(
    result_id: int,
    body: _PromoteReviewRequest,
    _: None = Depends(require_admin),
) -> dict:
    """M40a — set or clear the human-reviewed badge signal on a stored
    ``analysis_results`` row.

    Session-gated exactly like the other ``/review/*`` endpoints (401 when
    no authenticated session, via ``require_admin``). Sets ONLY the M39a
    ``human_reviewed_at`` / ``human_reviewed_by`` columns:

    * ``promote=true``  → stamps them (reviewer defaults to "operator").
    * ``promote=false`` → clears both back to NULL (un-promote).

    Never touches verdict_label / policy_alert_level / disagreement_signal /
    review_status / operator_review_required / truth_claim or any other
    column. Returns the re-read review columns on success; 404 when no row
    matches ``result_id``.
    """
    reviewer = (body.reviewer or "").strip() or "operator"
    try:
        updated = set_analysis_human_review(
            result_id, reviewed=body.promote, reviewer=reviewer,
        )
    except Exception:
        logger.exception("Failed to update human-review flag")
        raise HTTPException(
            status_code=500, detail="failed to update human-review flag",
        )
    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"analysis result {result_id} not found",
        )
    row = get_result_by_id(result_id) or {}
    return {
        "ok": True,
        "result_id": result_id,
        "promote": body.promote,
        "human_reviewed_at": row.get("human_reviewed_at"),
        "human_reviewed_by": row.get("human_reviewed_by"),
    }


@app.get("/review/tasks/{task_id}/decisions")
def review_list_decisions(
    task_id: str,
    _: None = Depends(require_admin),
) -> dict:
    task = get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="review task not found")
    decisions = review_workflow.build_decision_audit_records(
        list_review_decisions(task_id)
    )
    return {
        "task_id": task_id,
        "decisions": decisions,
        "count": len(decisions),
        "audit_version": review_workflow.AUDIT_SCHEMA_VERSION,
    }


@app.get("/review/tasks/{task_id}/audit-packet")
def review_audit_packet(
    task_id: str,
    _: None = Depends(require_admin),
) -> dict:
    """Phase 2 M9.1 — internal reviewer audit packet (read-only).

    Returns a structured, read-only audit snapshot for a single review
    task: the stored task summary, the verdict snapshot extracted at
    creation time, the source-identifier tuple, the M9.0 audit-rich
    decision list, and a fixed safety-contract block. Never mutates the
    task, decisions, original payload, verdict, confidence, or
    verification-card. Never publishes. Never echoes the token.

    Gated identically to the rest of the review surface:
        * 401 when there is no authenticated session (via ``require_admin``).
        * 404 when the task does not exist.
    """
    task = get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="review task not found")
    decisions = list_review_decisions(task_id)
    return review_workflow.build_review_audit_packet(task, decisions)
