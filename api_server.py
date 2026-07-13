import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import accounts
from config import cors_allowed_origins, describe_ai_config, session_secret_key
from database import (
    create_review_task,
    get_account_by_username,
    get_recent_results_slim,
    get_result_by_id,
    get_weekly_verification_stats,
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
# HONESTY-GUARD B3 Phase 3 — pure structural validator (stdlib-only,
# import-side-effect-free; see honesty_guard.py + _honesty_guard_b3_design.md).
import honesty_guard
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


# ---------------------------------------------------------------------------
# HONESTY-GUARD B3 Phase 3 — response-boundary honesty middleware.
#
# DEFAULT OFF (HONESTY_GUARD_MODE unset/"off" = pure no-op passthrough: the
# body is never parsed, never buffered — zero live behavior change). Modes:
#   off     -> passthrough (committed default; this slice ships dormant)
#   report  -> validate outgoing JSON; on violation LOG rule+path ONLY (never
#              the payload text) and return the ORIGINAL bytes unchanged.
#              Fail-OPEN: any guard/validator error passes the response through.
#   enforce -> fail-CLOSED: on violation (or a guard error that prevents
#              validation) return a generic 500 + ntfy; never leak the payload.
#
# Scope guards: acts ONLY on application/json responses with a known,
# sane content-length — SSE (/v2/jobs/{id}/stream, text/event-stream),
# static files, HTML, and unknown-length/huge bodies pass through UNTOUCHED
# and are never buffered. A passing payload is re-wrapped from the EXACT
# buffered bytes with the EXACT original raw headers (no re-serialization,
# byte-identical to no-middleware).
#
# The middleware never mutates a payload and raises no verdict (the validator
# only CHECKS — see honesty_guard.py). api_server.py is pin-OUT (not in
# MIGRATED_FILES), so these log sites do not move the 331/16 log pins.
# ---------------------------------------------------------------------------

_HONESTY_GUARD_MODES = ("off", "report", "enforce")
# Never buffer a body larger than this (validation skipped, response passes
# through untouched). Card payloads are far below it.
_HONESTY_GUARD_MAX_BYTES = 5 * 1024 * 1024


def _honesty_guard_mode() -> str:
    raw = (os.environ.get("HONESTY_GUARD_MODE") or "off").strip().lower()
    if raw not in _HONESTY_GUARD_MODES:
        # Unrecognized value: safest interpretation is the committed default.
        logger.warning("honesty-guard: unrecognized HONESTY_GUARD_MODE=%r -> off", raw)
        return "off"
    return raw


def _honesty_notify(title: str, message: str) -> None:
    """weekly_spine ntfy pattern: NTFY_URL > NTFY_TOPIC (ntfy.sh), print/log
    fallback when unset, best-effort — a notify failure never affects the
    response. Used only by report(optional)/enforce, never in off."""
    endpoint = (os.environ.get("NTFY_URL") or "").strip()
    if not endpoint:
        topic = (os.environ.get("NTFY_TOPIC") or "").strip()
        endpoint = "https://ntfy.sh/%s" % topic if topic else ""
    if not endpoint:
        logger.warning("honesty-guard notify (NTFY_* unset): %s | %s", title, message)
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            endpoint, data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:  # noqa: BLE001 — notify must never break a response
        logger.warning("honesty-guard notify send failed: %s | %s", title, message)


def _honesty_blocked_response():
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "response blocked"}, status_code=500)


@app.middleware("http")
async def honesty_guard_middleware(request, call_next):
    response = await call_next(request)
    mode = _honesty_guard_mode()
    if mode == "off":
        return response

    # Scope: JSON bodies only. SSE (text/event-stream), HTML, static files
    # and anything else stream through untouched — never buffered.
    content_type = (response.headers.get("content-type") or "").lower()
    if not content_type.startswith("application/json"):
        return response
    # Unknown-length (chunked/streaming) or oversized bodies: never buffer.
    length = response.headers.get("content-length") or ""
    if not length.isdigit() or int(length) > _HONESTY_GUARD_MAX_BYTES:
        return response

    # Buffer ONCE; every non-blocking path below re-wraps these exact bytes
    # with the exact original raw headers (incl. duplicate Set-Cookie pairs)
    # — byte-identical, no double-serialization.
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    body = b"".join(chunks)
    rebuilt = Response(content=body, status_code=response.status_code)
    rebuilt.raw_headers = list(response.headers.raw)

    try:
        ok, violations = honesty_guard.validate_payload(json.loads(body))
    except Exception:  # noqa: BLE001 — guard errors must not break report mode
        if mode == "enforce":
            # Fail-CLOSED: cannot prove the payload honest.
            logger.exception("honesty-guard: validation error in enforce mode "
                             "-> blocking %s", request.url.path)
            _honesty_notify("honesty-guard BLOCKED (guard error)",
                            "endpoint=%s (validation error)" % request.url.path)
            return _honesty_blocked_response()
        logger.exception("honesty-guard: validation error (report) -> "
                         "passing response through for %s", request.url.path)
        return rebuilt

    if ok:
        return rebuilt

    # Violation. Log rule + JSON path ONLY — never the payload text, never
    # the violation `detail` (it may embed payload values).
    summary = [{"rule": v.get("rule"), "path": v.get("path")}
               for v in violations[:20]]
    logger.warning("honesty-guard violation: mode=%s endpoint=%s count=%d "
                   "rules=%s", mode, request.url.path, len(violations), summary)
    if mode == "report":
        return rebuilt  # observe only: never block, never mutate
    _honesty_notify(
        "honesty-guard BLOCKED",
        "endpoint=%s violations=%s" % (request.url.path, summary))
    return _honesty_blocked_response()


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


# SEARCH-FIX Slice B — cheap pre-analysis garbage guard shared by all three
# analyze endpoints. Policy news is Korean: a query with no Hangul/CJK
# character ("asdfqwer1234") is rejected with a 400 BEFORE any collector/LLM
# cost. Known limitation (accepted): rare all-Latin legitimate queries are
# blocked too — the save-quality gate in database.save_analysis_result is the
# real backstop; this guard just stops the common garbage case cheaply.
# Verdict-isolated: an HTTP 400 before the pipeline ever runs.
_POLICY_QUERY_CJK_RE = re.compile(r"[가-힣぀-ヿ一-鿿]")
_POLICY_QUERY_MESSAGE = "정책 뉴스와 관련된 검색어를 입력해주세요."


def _require_policy_shaped_query(query: str) -> None:
    if not _POLICY_QUERY_CJK_RE.search(query):
        raise HTTPException(status_code=400, detail=_POLICY_QUERY_MESSAGE)


@app.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(analyze_rate_limiter)])
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    query = (request.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="검색어는 200자 이내로 입력해주세요.")
    _require_policy_shaped_query(query)
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
def history(limit: int = 20, domain: Optional[str] = None) -> HistoryResponse:
    # PERF-2: the homepage card list only needs lightweight columns, so this
    # uses the slim reader (heavy JSON body columns dropped) to cut the ~16MB
    # response. The DETAIL view still fetches the full row via GET /history/{id}
    # (get_result_by_id), which is unchanged.
    # STABLE-TABS S1: optional ?domain=<d> scopes the recent feed to one domain
    # (id-DESC, same slim shape) so a stable category tab can load its cards even
    # when they're outside the recent-100 window. domain=None → byte-identical to
    # the original 전체 feed. Read-only; no verdict field touched.
    try:
        results = get_recent_results_slim(limit=limit, domain=domain)
    except Exception:
        logger.exception("Failed to load analysis history")
        raise HTTPException(status_code=500, detail="failed to load history")

    return HistoryResponse(status="ok", count=len(results), results=results)


class StatsResponse(BaseModel):
    status: str
    total: int
    official: int
    draft: int
    range_start: str
    range_end: str


@app.get("/stats", response_model=StatsResponse)
def stats() -> StatsResponse:
    # SIDEBAR-RANK-B2: read-only weekly counts for the homepage sidebar's
    # "이번 주 검증 현황" panel. READ-ONLY — no write, no verdict path, no schema
    # change. Rolling 7-day window over created_at (ISO-8601 TEXT, lexicographic
    # >=). total = COUNT(created_at >= cutoff); official = of those, the
    # has_genuine_official_support count (persisted boolean + official_body_matches
    # old-row fallback, parsed in Python — see read_weekly_verification_stats);
    # draft = total - official (the AI-draft remainder).
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    cutoff_iso = cutoff.isoformat()
    try:
        counts = get_weekly_verification_stats(cutoff_iso)
    except Exception:
        logger.exception("Failed to compute weekly stats")
        raise HTTPException(status_code=500, detail="failed to compute stats")
    total = int(counts.get("total", 0))
    official = int(counts.get("official", 0))
    draft = max(0, total - official)
    return StatsResponse(
        status="ok",
        total=total,
        official=official,
        draft=draft,
        range_start=cutoff.date().isoformat(),
        range_end=now.date().isoformat(),
    )


# BRAINMAP 2d-i — the empty shape served when the graph doesn't exist yet
# (engine off / table absent / no row). A normal 200 so the brain-map page
# has exactly one code path. Matches graph_json's top-level keys.
_BRAINMAP_EMPTY_JSON = '{"nodes": [], "edges": [], "clusters": [], "empty": true}'


@app.get("/api/brainmap")
def brainmap_graph() -> Response:
    # BRAINMAP 2d-i: read-only 유통/연결 (spread/connection) graph for the
    # brain-map page. Serves the NEWEST brainmap_graph row's graph_json AS-IS —
    # the stored value is already valid JSON written by
    # scripts/build_brainmap_graph.py, so the ~441KB blob is NOT parsed and
    # re-serialized here. READ-ONLY: one SELECT, no write, no verdict field
    # (the graph is metadata; its labels carry kind="spread" / "N개 매체 보도
    # 중", never 검증). Mirrors /stats' error shape (logger.exception → 500).
    try:
        # Lazy imports keep api_server's module import surface unchanged —
        # postgres_storage is only needed by this route.
        import sqlalchemy as sa
        from sqlalchemy.exc import ProgrammingError

        import postgres_storage

        engine = postgres_storage.get_engine()
        if engine is None:
            # Dual-write disabled (local dev without PG) — empty, not 500.
            return Response(
                content=_BRAINMAP_EMPTY_JSON, media_type="application/json",
            )
        try:
            with engine.connect() as conn:
                row = conn.execute(sa.text(
                    "SELECT graph_json FROM brainmap_graph "
                    "ORDER BY id DESC LIMIT 1"
                )).fetchone()
        except ProgrammingError:
            # Table not created yet (build_brainmap_graph.py hasn't run
            # against this DB) — empty, not 500.
            return Response(
                content=_BRAINMAP_EMPTY_JSON, media_type="application/json",
            )
        graph_json = row[0] if row else None
        if not graph_json:
            return Response(
                content=_BRAINMAP_EMPTY_JSON, media_type="application/json",
            )
        return Response(
            content=graph_json,
            media_type="application/json",
            # The graph regenerates manually/rarely; let clients cache 5 min.
            headers={"Cache-Control": "max-age=300"},
        )
    except Exception:
        logger.exception("Failed to load brainmap graph")
        raise HTTPException(status_code=500, detail="failed to load brainmap")


# ---------------------------------------------------------------------------
# SPREAD-TIMELINE Slice 1 — read-only spread annotation for ONE analysis id.
#
# GET /api/spread/{analysis_id} finds the cluster containing that id in the
# NEWEST brainmap_graph row and returns a small circulation aggregate:
# distinct outlet_count (precomputed by scripts/build_brainmap_graph.py —
# never recomputed here), the cluster's rebuild-stable stable_id, and a
# publish-date timeline (first/last/span/daily counts) from the members'
# analysis_results.published_at values.
#
# SAFETY / HONESTY:
#   * READ-ONLY: reads brainmap_graph.graph_json + analysis_results
#     .published_at only. NO verdict field is read or written (verdict_label
#     / policy_confidence_score / truth_claim / operator_review_required /
#     has_genuine_official_support untouched).
#   * The payload is CIRCULATION metadata only — spread, never 검증. No truth
#     vocabulary, no probability, no verdict-shaped field.
#   * NEVER 500: id not clustered, no graph row, table missing, PG disabled,
#     or any unexpected failure -> {"found": false} (mirrors the brainmap
#     endpoint's empty-not-error posture).
#   * The ~441KB graph JSON is parsed ONCE per brainmap_graph row: a
#     module-level cache keyed by the newest row id (checked with a cheap
#     SELECT id per request) serves the prebuilt indexes until a rebuild
#     inserts a newer row.
# ---------------------------------------------------------------------------
_SPREAD_NOT_FOUND_JSON = '{"found": false}'
_SPREAD_CACHE: dict = {"row_id": None, "indexes": None}


def _build_spread_indexes(graph: dict) -> dict:
    """Pure: graph JSON dict -> {clusters: cid->cluster meta,
    members: cid->[analysis ids], cluster_of: analysis id->cid,
    title_of: analysis id->node title}.
    Membership lives on NODES (node.id + node.cluster_id); singleton nodes
    (cluster_id null) are skipped for membership. title_of (CLUSTER-SURFACE
    S-a, additive) keeps each clustered node's display title so the member
    list can render sibling links without a second graph parse."""
    clusters = {}
    for cluster in graph.get("clusters") or []:
        cid = cluster.get("cluster_id")
        if cid is not None:
            clusters[cid] = cluster
    members: dict = {}
    cluster_of: dict = {}
    title_of: dict = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        node_id = node.get("id")
        if cid is None or node_id is None:
            continue
        cluster_of[node_id] = cid
        members.setdefault(cid, []).append(node_id)
        title_of[node_id] = node.get("title") or ""
    return {"clusters": clusters, "members": members, "cluster_of": cluster_of,
            "title_of": title_of}


def _load_spread_indexes():
    """Newest brainmap_graph row -> parsed indexes (module-cached by row id).
    Returns None on every expected-empty path (PG disabled, table missing,
    no row, bad JSON) — the route maps None to {"found": false}."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT id FROM brainmap_graph ORDER BY id DESC LIMIT 1"
            )).fetchone()
    except ProgrammingError:
        return None
    if not row:
        return None
    row_id = row[0]
    if _SPREAD_CACHE["row_id"] == row_id and _SPREAD_CACHE["indexes"] is not None:
        return _SPREAD_CACHE["indexes"]
    with engine.connect() as conn:
        graph_row = conn.execute(sa.text(
            "SELECT graph_json FROM brainmap_graph WHERE id = :row_id"
        ), {"row_id": row_id}).fetchone()
    if not graph_row or not graph_row[0]:
        return None
    try:
        graph = json.loads(graph_row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(graph, dict):
        return None
    indexes = _build_spread_indexes(graph)
    _SPREAD_CACHE["row_id"] = row_id
    _SPREAD_CACHE["indexes"] = indexes
    return indexes


def _fetch_published_at(member_ids):
    """Read-only: the members' published_at values (may include None — the
    97.9% backfill leaves genuinely dateless rows NULL). Expanding IN keeps
    the statement dialect-portable."""
    if not member_ids:
        return []
    import sqlalchemy as sa

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return []
    stmt = sa.text(
        "SELECT published_at FROM analysis_results WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"ids": list(member_ids)}).fetchall()
    return [row[0] for row in rows]


def _build_spread_payload(cluster_meta: dict, member_ids, published_values) -> dict:
    """Pure aggregate: NULL/empty published_at members are EXCLUDED from the
    timeline (first/last/span/daily/dated_members) but stay inside the
    cluster's precomputed outlet_count; they surface as undated_members."""
    dated = sorted(v for v in published_values if v)
    daily_counts: dict = {}
    for value in dated:
        day = value[:10]
        daily_counts[day] = daily_counts.get(day, 0) + 1
    first_at = dated[0] if dated else None
    last_at = dated[-1] if dated else None
    span_days = None
    if dated:
        span_days = (date.fromisoformat(last_at[:10])
                     - date.fromisoformat(first_at[:10])).days
    return {
        "found": True,
        "cluster": {
            "stable_id": cluster_meta.get("stable_id"),
            "outlet_count": cluster_meta.get("outlet_count"),
            "size": cluster_meta.get("size"),
            "size_label": cluster_meta.get("size_label"),
        },
        "timeline": {
            "first_at": first_at,
            "last_at": last_at,
            "span_days": span_days,
            "daily": [{"date": day, "count": count}
                      for day, count in sorted(daily_counts.items())],
            "dated_members": len(dated),
            "undated_members": max(0, len(list(member_ids)) - len(dated)),
        },
    }


def _spread_response(payload_json: str) -> Response:
    # The graph regenerates manually/rarely; 5-min client cache mirrors
    # /api/brainmap.
    return Response(
        content=payload_json,
        media_type="application/json",
        headers={"Cache-Control": "max-age=300"},
    )


# ---------------------------------------------------------------------------
# SEARCH-TO-ANALYZE Slice 1 — read-only corpus text search over EXISTING
# analysis_results, so a search hit returns instantly with ZERO LLM cost
# (today the search box runs the full analyze pipeline on every submit; the
# frontend rewires to try this first in Slice 2).
#
# SAFETY:
#   * READ-ONLY: one parameterized SELECT of display fields (id, title,
#     claim_text preview, published_at, review_status). NEVER triggers the
#     analyze pipeline; computes/derives no verdict (review_status is the
#     existing draft label the UI already shows, passed through as-is).
#   * INJECTION-SAFE: q is BOUND, never string-formatted into SQL, and ILIKE
#     wildcards (%/_/\) in q are escaped with ESCAPE '\' so a user typing
#     "%" cannot match everything.
#   * NEVER 500: empty/too-short q, no match, PG disabled, or any failure
#     -> {"results": []} at HTTP 200 (the spread/weekly posture).
# ---------------------------------------------------------------------------
_SEARCH_EMPTY_JSON = '{"results": []}'
_SEARCH_LIMIT = 10
_SEARCH_MAX_QUERY_CHARS = 200  # mirrors the /analyze 200-char cap
_SEARCH_SNIPPET_CHARS = 120


def _build_search_pattern(q: str) -> str:
    """Escape LIKE/ILIKE metacharacters in the USER's text (backslash first),
    then wrap in %...% for substring match. Paired with ESCAPE '\\' in the
    statement so %/_ typed by a user are literals, never wildcards."""
    escaped = (q.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))
    return f"%{escaped}%"


def _search_corpus_rows(q: str):
    """Read-only: newest-first substring match on title OR claim_text.
    Returns a list of row tuples (id, title, claim_text, published_at,
    review_status), or [] when PG is disabled. ILIKE is PostgreSQL's
    case-insensitive LIKE — the live store; ~7.6k rows, so a sequential
    scan is fine for v1 (a pg_trgm index is the later optimization if the
    corpus grows 10x)."""
    import sqlalchemy as sa

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return []
    stmt = sa.text(
        "SELECT id, title, claim_text, published_at, review_status "
        "FROM analysis_results "
        "WHERE title ILIKE :pattern ESCAPE '\\' "
        "OR claim_text ILIKE :pattern ESCAPE '\\' "
        "ORDER BY id DESC LIMIT :row_limit"
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt, {
            "pattern": _build_search_pattern(q),
            "row_limit": _SEARCH_LIMIT,
        }).fetchall()
    return list(rows)


@app.get("/api/search")
def search_corpus(q: str = "") -> Response:
    def _empty() -> Response:
        return Response(
            content=_SEARCH_EMPTY_JSON,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    try:
        query = (q or "").strip()[:_SEARCH_MAX_QUERY_CHARS]
        if len(query) < 2:
            return _empty()
        results = []
        for row_id, title, claim_text, published_at, review_status in _search_corpus_rows(query):
            snippet = (claim_text or "").strip()
            if len(snippet) > _SEARCH_SNIPPET_CHARS:
                snippet = snippet[:_SEARCH_SNIPPET_CHARS] + "…"
            results.append({
                "result_id": row_id,
                "title": title or "",
                "snippet": snippet,
                "published_at": published_at,
                "review_status": review_status,
            })
        return Response(
            content=json.dumps({"results": results}, ensure_ascii=False),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        logger.exception("Failed to search corpus")
        return _empty()


# ---------------------------------------------------------------------------
# WEEKLY-REPORT Slice 1 — read-only weekly "most-amplified claims" snapshots.
#
# Serves the STORED payload_json written by scripts/generate_weekly_report.py
# — no recomputation. Ranking inside the payload is by CIRCULATION
# (distinct outlet_count) only; it carries the mandatory framing
# "확산 규모 기준 · 사실 검증 아님" and no verdict/score field of any kind.
# NEVER 500: no row, table missing (generator hasn't run), PG disabled, or
# any unexpected failure -> {"found": false} (the brainmap/spread posture).
# ---------------------------------------------------------------------------
_WEEKLY_NOT_FOUND_JSON = '{"found": false}'


def _load_weekly_report_row(week_start=None):
    """Newest weekly_reports payload_json (optionally for one week_start).
    Returns the raw JSON TEXT or None on every expected-empty path."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return None
    if week_start is None:
        stmt = sa.text(
            "SELECT payload_json FROM weekly_reports ORDER BY id DESC LIMIT 1"
        )
        params = {}
    else:
        stmt = sa.text(
            "SELECT payload_json FROM weekly_reports "
            "WHERE week_start = :week_start ORDER BY id DESC LIMIT 1"
        )
        params = {"week_start": week_start}
    try:
        with engine.connect() as conn:
            row = conn.execute(stmt, params).fetchone()
    except ProgrammingError:
        # Table not created yet — the generator creates it on first run.
        return None
    return row[0] if row and row[0] else None


def _weekly_report_response(week_start=None) -> Response:
    try:
        payload_json = _load_weekly_report_row(week_start)
        if not payload_json:
            return _spread_response(_WEEKLY_NOT_FOUND_JSON)
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return _spread_response(_WEEKLY_NOT_FOUND_JSON)
        return _spread_response(json.dumps(
            {"found": True, "report": payload}, ensure_ascii=False,
        ))
    except Exception:
        logger.exception("Failed to load weekly report")
        return _spread_response(_WEEKLY_NOT_FOUND_JSON)


@app.get("/api/weekly-report")
def weekly_report_latest() -> Response:
    return _weekly_report_response(None)


@app.get("/api/weekly-report/{week_start}")
def weekly_report_for_week(week_start: str) -> Response:
    return _weekly_report_response(week_start)


@app.get("/api/spread/{analysis_id}")
def spread_annotation(analysis_id: int) -> Response:
    try:
        indexes = _load_spread_indexes()
        if indexes is None:
            return _spread_response(_SPREAD_NOT_FOUND_JSON)
        # cluster_id 0 is a real cluster — explicit None checks only.
        cluster_id = indexes["cluster_of"].get(analysis_id)
        if cluster_id is None:
            return _spread_response(_SPREAD_NOT_FOUND_JSON)
        member_ids = indexes["members"].get(cluster_id) or []
        cluster_meta = indexes["clusters"].get(cluster_id) or {}
        published_values = _fetch_published_at(member_ids)
        payload = _build_spread_payload(cluster_meta, member_ids, published_values)
        return _spread_response(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to build spread annotation")
        return _spread_response(_SPREAD_NOT_FOUND_JSON)


# ---------------------------------------------------------------------------
# CLUSTER-SURFACE S-a — read-only sibling coverage for ONE analysis id.
#
# GET /api/cluster/{result_id}/members finds the card's cluster in the NEWEST
# brainmap_graph (the SAME cached indexes /api/spread uses — no second parse,
# no rebuild needed) and returns the OTHER member articles' {analysis_id,
# title} so the card detail can render "이 주장을 보도한 다른 기사들".
#
# SAFETY / HONESTY:
#   * READ-ONLY: stable_id / outlet_count / member ids / node titles ONLY.
#     NO verdict/score/confidence column anywhere. The payload is sibling
#     CIRCULATION (같은 주장을 다룬 다른 보도), never 검증 — the note says so.
#   * NEVER 500: id not clustered, single-member cluster, no graph, PG off,
#     or any failure -> {"found": false, "members": []} at HTTP 200.
#   * Node `domain` is the TOPIC domain, not the outlet — deliberately not
#     surfaced. published_at / outlet host are deferred.
# ---------------------------------------------------------------------------
_CLUSTER_MEMBERS_EMPTY_JSON = '{"found": false, "members": []}'
_CLUSTER_MEMBERS_CAP = 10
_CLUSTER_MEMBERS_NOTE = "같은 주장을 다룬 다른 보도 — 검증이 아닙니다"


@app.get("/api/cluster/{result_id}/members")
def cluster_members(result_id: int) -> Response:
    try:
        indexes = _load_spread_indexes()
        if indexes is None:
            return _spread_response(_CLUSTER_MEMBERS_EMPTY_JSON)
        # cluster_id 0 is a real cluster — explicit None checks only.
        cluster_id = indexes["cluster_of"].get(result_id)
        if cluster_id is None:
            return _spread_response(_CLUSTER_MEMBERS_EMPTY_JSON)
        member_ids = indexes["members"].get(cluster_id) or []
        sibling_ids = sorted(
            mid for mid in member_ids if mid != result_id
        )[:_CLUSTER_MEMBERS_CAP]
        if not sibling_ids:
            return _spread_response(_CLUSTER_MEMBERS_EMPTY_JSON)
        cluster_meta = indexes["clusters"].get(cluster_id) or {}
        # .get: a pre-title_of in-process cache entry lacks the key; empty
        # titles then fall back client-side rather than erroring here.
        title_of = indexes.get("title_of") or {}
        payload = {
            "found": True,
            "cluster": {
                "stable_id": cluster_meta.get("stable_id"),
                "outlet_count": cluster_meta.get("outlet_count"),
            },
            "members": [
                {"analysis_id": mid, "title": title_of.get(mid) or ""}
                for mid in sibling_ids
            ],
            "note": _CLUSTER_MEMBERS_NOTE,
        }
        return _spread_response(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to build cluster members")
        return _spread_response(_CLUSTER_MEMBERS_EMPTY_JSON)


# ---------------------------------------------------------------------------
# CLUSTER-SURFACE S-b — read-only BATCH cluster sizes for the home feed.
#
# GET /api/cluster-sizes?ids=1,2,3 answers ONE call for the whole visible
# card grid (the feed must not fire N per-card requests): each id is looked
# up in the SAME cached spread indexes (cluster_of -> cluster outlet_count).
# Ids not in the graph — and clusters below 2 outlets, mirroring the spread
# section's >=2 gate — are simply OMITTED from the map, so the frontend
# renders no chip for them.
#
# SAFETY / HONESTY:
#   * READ-ONLY: cluster_of + outlet_count ONLY — a pure {id: count} map.
#     NO verdict/score/confidence column. "N개 매체" is circulation, never 검증.
#   * NEVER 500: empty/malformed ids, no graph, PG off, any failure ->
#     {"sizes": {}} at HTTP 200. Ids capped at 60 per call.
# ---------------------------------------------------------------------------
_CLUSTER_SIZES_EMPTY_JSON = '{"sizes": {}}'
_CLUSTER_SIZES_MAX_IDS = 60


@app.get("/api/cluster-sizes")
def cluster_sizes(ids: Optional[str] = None) -> Response:
    try:
        parsed = []
        for token in (ids or "").split(","):
            token = token.strip()
            if token.isdigit():  # malformed / negative tokens are ignored
                parsed.append(int(token))
        parsed = parsed[:_CLUSTER_SIZES_MAX_IDS]
        if not parsed:
            return _spread_response(_CLUSTER_SIZES_EMPTY_JSON)
        indexes = _load_spread_indexes()
        if indexes is None:
            return _spread_response(_CLUSTER_SIZES_EMPTY_JSON)
        sizes = {}
        for rid in parsed:
            # cluster_id 0 is a real cluster — explicit None checks only.
            cluster_id = indexes["cluster_of"].get(rid)
            if cluster_id is None:
                continue
            cluster_meta = indexes["clusters"].get(cluster_id) or {}
            outlet_count = cluster_meta.get("outlet_count")
            if isinstance(outlet_count, int) and outlet_count >= 2:
                sizes[str(rid)] = outlet_count
        return _spread_response(json.dumps({"sizes": sizes},
                                           ensure_ascii=False))
    except Exception:
        logger.exception("Failed to build cluster sizes")
        return _spread_response(_CLUSTER_SIZES_EMPTY_JSON)


# ---------------------------------------------------------------------------
# TRENDING-API Slice 1 — read-only spread-GROWTH Top-N from the
# brainmap_snapshots time series (§27c passive accumulation).
#
# GET /api/trending diffs the TWO most recent snapshot batches (batch =
# all rows sharing one snapshot_date + graph_ref; batches ordered by their
# newest row id) and ranks clusters by outlet_count growth. Each row is
# enriched with a display title + representative analysis_id resolved from
# the NEWEST brainmap_graph (label-title member, min-id fallback — the
# generate_faded_candidates representative rule).
#
# SAFETY / HONESTY:
#   * READ-ONLY: selects brainmap_snapshots ids/counts/dates and graph_json
#     title/id fields ONLY. NO verdict field is ever read (verdict_label /
#     policy_confidence_score / truth_claim untouched) — the payload is
#     circulation GROWTH metadata, spread, never 검증.
#   * NEVER 500: fewer than two batches (today's real state: one §27c batch)
#     -> {"trending": [], "note": "insufficient snapshot history"}; PG
#     disabled / table missing / any failure -> {"trending": []} — all 200.
#   * Graph parsed ONCE per brainmap_graph row (module cache keyed by row
#     id, the _SPREAD_CACHE pattern).
# ---------------------------------------------------------------------------
_TRENDING_EMPTY_JSON = '{"trending": []}'
_TRENDING_DEFAULT_LIMIT = 10  # UI takes the Top 5; return a little headroom.
_TRENDING_MAX_LIMIT = 20
_TRENDING_DISPLAY_CACHE: dict = {"row_id": None, "display": None}


def _fetch_snapshot_batches():
    """The two most recent DISTINCT snapshot batches, newest first. Each is
    {"snapshot_date", "graph_ref", "rows": [(stable_id, outlet_count,
    member_count), ...]}. Batches are keyed by (snapshot_date, graph_ref) —
    the append guard's own key — and ordered by MAX(id) so a same-day
    re-snapshot of a newer build still ranks as 'current'. Returns None on
    every expected-empty path (PG disabled, table missing)."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            keys = conn.execute(sa.text(
                "SELECT snapshot_date, graph_ref, MAX(id) AS max_id "
                "FROM brainmap_snapshots "
                "GROUP BY snapshot_date, graph_ref "
                "ORDER BY max_id DESC LIMIT 2"
            )).fetchall()
            batches = []
            for snapshot_date, graph_ref, _max_id in keys:
                rows = conn.execute(sa.text(
                    "SELECT cluster_stable_id, outlet_count, member_count "
                    "FROM brainmap_snapshots "
                    "WHERE snapshot_date = :d AND graph_ref = :g"
                ), {"d": snapshot_date, "g": graph_ref}).fetchall()
                batches.append({
                    "snapshot_date": snapshot_date,
                    "graph_ref": graph_ref,
                    "rows": [(r[0], r[1], r[2]) for r in rows],
                })
    except ProgrammingError:
        return None
    return batches


def _compute_trending(current_rows, previous_rows, limit):
    """Pure: two batches of (stable_id, outlet_count, member_count) ->
    ranked growth entries. A cluster only in 'current' is new (is_new=true,
    growth = its full outlet_count — a fresh widely-covered cluster is
    legitimately trending). Clusters only in 'previous' dropped out — not
    growth. Duplicate stable_ids within a batch (a --force re-append)
    collapse to the last row."""
    current = {sid: (outlets, members)
               for sid, outlets, members in current_rows if sid}
    previous = {sid: outlets for sid, outlets, _ in previous_rows if sid}
    entries = []
    for sid, (outlets, members) in current.items():
        prev_outlets = previous.get(sid)
        is_new = prev_outlets is None
        growth = outlets if is_new else outlets - prev_outlets
        entries.append({
            "cluster_stable_id": sid,
            "representative_analysis_id": None,
            "title": "",
            "current_outlet_count": outlets,
            "previous_outlet_count": prev_outlets,
            "growth": growth,
            "is_new": is_new,
            "member_count": members,
        })
    entries.sort(key=lambda e: (-e["growth"], -e["current_outlet_count"],
                                e["cluster_stable_id"]))
    return entries[:limit]


def _build_trending_display_index(graph: dict) -> dict:
    """Pure: graph JSON -> {stable_id: {"title", "representative_analysis_id"}}.
    Title is the cluster's label_title; the representative is the lowest-id
    member whose node title equals it, min member id otherwise (the
    generate_faded_candidates rule). Display fields only — no verdict."""
    members_by_cluster: dict = {}
    for node in graph.get("nodes") or []:
        cid = node.get("cluster_id")
        node_id = node.get("id")
        if cid is None or node_id is None:
            continue
        members_by_cluster.setdefault(cid, []).append(
            (node_id, node.get("title") or ""))
    display = {}
    for cluster in graph.get("clusters") or []:
        stable_id = cluster.get("stable_id")
        members = members_by_cluster.get(cluster.get("cluster_id")) or []
        if not stable_id or not members:
            continue
        label_title = cluster.get("label_title") or ""
        representative_id = min(mid for mid, _ in members)
        if label_title:
            for mid, title in sorted(members):
                if title == label_title:
                    representative_id = mid
                    break
        display[stable_id] = {
            "title": label_title,
            "representative_analysis_id": representative_id,
        }
    return display


def _load_trending_display_index():
    """Newest brainmap_graph row -> stable_id display map (module-cached by
    row id, mirroring _load_spread_indexes). None on every expected-empty
    path — trending rows then ship without title/representative."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT id FROM brainmap_graph ORDER BY id DESC LIMIT 1"
            )).fetchone()
    except ProgrammingError:
        return None
    if not row:
        return None
    row_id = row[0]
    if (_TRENDING_DISPLAY_CACHE["row_id"] == row_id
            and _TRENDING_DISPLAY_CACHE["display"] is not None):
        return _TRENDING_DISPLAY_CACHE["display"]
    with engine.connect() as conn:
        graph_row = conn.execute(sa.text(
            "SELECT graph_json FROM brainmap_graph WHERE id = :row_id"
        ), {"row_id": row_id}).fetchone()
    if not graph_row or not graph_row[0]:
        return None
    try:
        graph = json.loads(graph_row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(graph, dict):
        return None
    display = _build_trending_display_index(graph)
    _TRENDING_DISPLAY_CACHE["row_id"] = row_id
    _TRENDING_DISPLAY_CACHE["display"] = display
    return display


@app.get("/api/trending")
def trending_growth(limit: Optional[str] = None) -> Response:
    try:
        # Defensive parse: ?limit=abc falls back to the default instead of a
        # validation error — this endpoint answers 200 for every input.
        try:
            n = int(limit) if limit is not None else _TRENDING_DEFAULT_LIMIT
        except (TypeError, ValueError):
            n = _TRENDING_DEFAULT_LIMIT
        n = max(1, min(n, _TRENDING_MAX_LIMIT))

        batches = _fetch_snapshot_batches()
        if not batches:
            return _spread_response(_TRENDING_EMPTY_JSON)
        if len(batches) < 2:
            only = batches[0]
            return _spread_response(json.dumps({
                "trending": [],
                "window": {
                    "current_date": only["snapshot_date"],
                    "previous_date": None,
                    "graph_ref": only["graph_ref"],
                },
                "note": "insufficient snapshot history",
            }, ensure_ascii=False))
        current, previous = batches[0], batches[1]
        entries = _compute_trending(current["rows"], previous["rows"], n)
        display = _load_trending_display_index() or {}
        for entry in entries:
            info = display.get(entry["cluster_stable_id"]) or {}
            entry["title"] = info.get("title") or ""
            entry["representative_analysis_id"] = info.get(
                "representative_analysis_id")
        return _spread_response(json.dumps({
            "trending": entries,
            "window": {
                "current_date": current["snapshot_date"],
                "previous_date": previous["snapshot_date"],
                "graph_ref": current["graph_ref"],
            },
        }, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to build trending growth ranking")
        return _spread_response(_TRENDING_EMPTY_JSON)


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
    _require_policy_shaped_query(query)
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
    _require_policy_shaped_query(query)
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


# ---------------------------------------------------------------------------
# FADED-CLAIMS Slice 2 — the semi-auto review layer over the
# faded_claim_candidates table (populated by scripts/generate_faded_candidates
# .py). The dry-run proved thresholds cannot separate genuinely-faded claims
# from CONCLUDED events (a successful 61-outlet MOU ranks #1 at any
# threshold), so a human approve/dismiss gate is mandatory before anything
# shows publicly.
#
#   * The two /review/faded-candidates* routes are ADMIN-GATED via the same
#     require_admin session dependency as every other /review/* route — no
#     new auth, no weakened gating.
#   * GET /api/faded-claims (public) serves APPROVED rows ONLY — the
#     status='approved' bind is the only query it can run; pending/dismissed
#     can never leak. The mandatory honesty framing is baked into the payload
#     so no consumer can omit it.
#   * VERDICT-ISOLATED: spread/date/title + curation status only. The
#     approve/dismiss status is curation metadata, never a verdict; no
#     verdict field is read or written anywhere here.
# ---------------------------------------------------------------------------
_FADED_FRAMING = (
    "이 목록은 후속 보도가 끊긴 사실만 보여줍니다. 주장의 진위나 정책의 "
    "추진·성패에 대한 판단이 아니며, 후속 보도가 저희 수집망 밖에 "
    "있었을 수도 있습니다."
)
_FADED_REVIEW_STATUSES = ("pending", "approved", "dismissed")
_FADED_SET_STATUSES = ("approved", "dismissed")


def _fetch_faded_rows(status: str):
    """Seam: candidate rows for one status, score desc. [] on PG disabled or
    table missing (the generator creates it on its first real run)."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return []
    stmt = sa.text(
        "SELECT id, cluster_stable_id, representative_analysis_id, title, "
        "outlet_count, first_at, last_at, silence_days, marker_hit, score, "
        "status, reviewed_at, generated_at "
        "FROM faded_claim_candidates WHERE status = :status "
        "ORDER BY score DESC, id"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"status": status}).fetchall()
    except ProgrammingError:
        return []
    return [dict(row._mapping) for row in rows]


def _fetch_faded_admin_rows(status: str):
    """ADMIN seam (Slice 4a): rows INCLUDING the ai_* recommendation fields —
    operator-side review aid only; the public route never uses this seam.
    Falls back to the base seam when the ai_* columns don't exist yet (the
    window between deploy and the generator's first --judge run), so the
    admin page keeps working either way."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return []
    stmt = sa.text(
        "SELECT id, cluster_stable_id, representative_analysis_id, title, "
        "outlet_count, first_at, last_at, silence_days, marker_hit, score, "
        "status, reviewed_at, generated_at, "
        "ai_recommendation, ai_reason, ai_confidence, ai_judged_at "
        "FROM faded_claim_candidates WHERE status = :status "
        "ORDER BY score DESC, id"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"status": status}).fetchall()
    except ProgrammingError:
        # Table missing entirely OR ai_* columns not added yet — degrade to
        # the base field set (which itself returns [] when the table is gone).
        return _fetch_faded_rows(status)
    return [dict(row._mapping) for row in rows]


def _set_faded_status(candidate_id: int, status: str, reviewed_at: str) -> bool:
    """Seam: set one candidate's curation status. True iff a row changed."""
    import sqlalchemy as sa
    from sqlalchemy.exc import ProgrammingError

    import postgres_storage

    engine = postgres_storage.get_engine()
    if engine is None:
        return False
    stmt = sa.text(
        "UPDATE faded_claim_candidates "
        "SET status = :status, reviewed_at = :reviewed_at "
        "WHERE id = :candidate_id"
    )
    try:
        with engine.begin() as conn:
            result = conn.execute(stmt, {
                "status": status,
                "reviewed_at": reviewed_at,
                "candidate_id": candidate_id,
            })
    except ProgrammingError:
        return False
    return bool(result.rowcount)


@app.get("/review/faded-candidates")
def list_faded_candidates(
    status: str = "pending",
    _: None = Depends(require_admin),
) -> dict:
    """Operator shortlist for the 30-second judgment: pending by default;
    ?status=approved|dismissed for auditing past decisions. Read-only."""
    wanted = (status or "pending").strip().lower()
    if wanted not in _FADED_REVIEW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be one of pending/approved/dismissed",
        )
    try:
        # Slice 4a: the ADMIN list carries the ai_* recommendation fields
        # (review aid). The public /api/faded-claims route keeps using the
        # base seam and its explicit slim field list — ai_* can never leak.
        candidates = _fetch_faded_admin_rows(wanted)
    except Exception:
        logger.exception("Failed to list faded candidates")
        candidates = []
    return {"status": "ok", "requested_status": wanted, "candidates": candidates}


class FadedStatusRequest(BaseModel):
    status: str


@app.post("/review/faded-candidates/{candidate_id}/status")
def set_faded_candidate_status(
    candidate_id: int,
    body: FadedStatusRequest,
    _: None = Depends(require_admin),
) -> dict:
    """The operator's approve/dismiss judgment. Only those two values are
    accepted; reviewed_at is stamped now (UTC). Curation metadata only."""
    wanted = (body.status or "").strip().lower()
    if wanted not in _FADED_SET_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="status must be 'approved' or 'dismissed'",
        )
    reviewed_at = datetime.now(timezone.utc).isoformat()
    try:
        changed = _set_faded_status(candidate_id, wanted, reviewed_at)
    except Exception:
        logger.exception("Failed to set faded candidate status")
        raise HTTPException(status_code=500, detail="failed to update candidate")
    if not changed:
        raise HTTPException(status_code=404, detail="candidate not found")
    return {
        "status": "ok",
        "id": candidate_id,
        "new_status": wanted,
        "reviewed_at": reviewed_at,
    }


@app.get("/api/faded-claims")
def faded_claims() -> Response:
    """PUBLIC read: APPROVED faded claims only, slim fields + the mandatory
    honesty framing baked into the payload. Empty/error -> {claims: [],
    framing} at HTTP 200, never 500 (the brainmap/spread/weekly posture)."""
    payload = {"claims": [], "framing": _FADED_FRAMING}
    try:
        for row in _fetch_faded_rows("approved"):
            payload["claims"].append({
                "representative_analysis_id": row.get("representative_analysis_id"),
                "title": row.get("title") or "",
                "outlet_count": row.get("outlet_count"),
                "first_at": row.get("first_at"),
                "last_at": row.get("last_at"),
                "silence_days": row.get("silence_days"),
            })
    except Exception:
        logger.exception("Failed to load faded claims")
        payload["claims"] = []
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "max-age=120"},
    )
