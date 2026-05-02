import logging
import time
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_recent_results, get_result_by_id, init_db, save_analysis_result
from main import analyze_pipeline


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("policy_ai.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("SQLite database initialized")
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


class AnalyzeResponse(BaseModel):
    status: str
    results: List[AnalyzeResult]
    news_collection_debug: dict = {}


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

        results.append(
            AnalyzeResult(
                title=api_result.get("title") or "",
                original_url=api_result.get("original_url") or "",
                topic=api_result.get("topic") or "",
                claims=api_result.get("claims") or [],
                normalized_claims=api_result.get("normalized_claims") or [],
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
            )
        )
        try:
            save_status = save_analysis_result(api_result, query=query)
            if save_status.get("duplicate"):
                logger.info("Duplicate skipped in SQLite: %s", api_result.get("title"))
        except Exception:
            logger.exception("Failed to save analysis result to SQLite")

    elapsed = time.perf_counter() - started
    logger.info(
        "Analyze request completed: query=%s results=%s elapsed=%.2fs",
        query,
        len(results),
        elapsed,
    )

    return AnalyzeResponse(
        status="ok",
        results=results,
        news_collection_debug=report.get("news_collection_debug") or {},
    )


@app.get("/history", response_model=HistoryResponse)
def history(limit: int = 20) -> HistoryResponse:
    try:
        results = get_recent_results(limit=limit)
    except Exception:
        logger.exception("Failed to load analysis history")
        raise HTTPException(status_code=500, detail="failed to load history")

    return HistoryResponse(status="ok", count=len(results), results=results)


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
