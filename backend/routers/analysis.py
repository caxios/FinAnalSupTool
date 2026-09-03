"""
routers.analysis
─────────────────
The Multi-Agent System endpoints and the persisted analysis history:

  POST /analyze          — Run the full three-phase pipeline, return final report
  POST /analyze/stream   — Same pipeline, streamed as Server-Sent Events
  GET  /analysis/history — Past-run summaries (optionally scoped to a ticker)
  GET  /analysis/tickers — Distinct tickers with stored runs
  GET  /analysis/{run_id}— Full stored record for one run

The orchestration itself lives in ``services.pipeline``; these endpoints only
wire up dependencies and adapt the event stream to the HTTP response shape.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse

from schemas import AnalyzeRequest, QueryDataRequest
from rag import history_store
from services import research_copilot
from services.storage import (
    DocumentStore,
    DebateStore,
    get_document_store,
    get_debate_store,
)
from services.pipeline import analyze_pipeline, analyze_preconditions

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analysis"])


# =============================================================================
# POST /analyze
# =============================================================================

@router.post("/analyze")
async def run_analysis(
    request: AnalyzeRequest,
    store: DocumentStore = Depends(get_document_store),
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Run the full three-phase MAS pipeline for ONE company and return the report.

    Phase 1 (six agents, rate-limited) → Phase 2 (sequential debate) → Phase 3
    (manager synthesis + 3-axis gap scoring). The run is saved to history. For
    live per-phase progress on the ~60-120s pipeline, use POST /analyze/stream.
    """
    analyze_preconditions(store, request.ticker)
    final: dict = {}
    async for event in analyze_pipeline(request, store, debate_store):
        if event.get("status") == "complete":
            final = event["result"]
    return final


# =============================================================================
# POST /analyze/stream
# =============================================================================

@router.post("/analyze/stream")
async def run_analysis_stream(
    request: AnalyzeRequest,
    store: DocumentStore = Depends(get_document_store),
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Same pipeline as POST /analyze, but streamed as Server-Sent Events so the UI
    can show real-time progress: one event per agent as it finishes, then the
    debate and synthesis phases, then a final `complete` event carrying the full
    report. Each SSE line is `data: {json}`.
    """
    analyze_preconditions(store, request.ticker)

    async def event_generator():
        try:
            async for event in analyze_pipeline(request, store, debate_store):
                yield f"data: {json.dumps(event)}\n\n"
        except HTTPException as e:
            yield f"data: {json.dumps({'phase': 0, 'status': 'error', 'detail': e.detail})}\n\n"
        except Exception as e:             # noqa: BLE001
            logger.error(f"Streaming analysis failed: {e}")
            yield f"data: {json.dumps({'phase': 0, 'status': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering so events flush
            "Connection": "keep-alive",
        },
    )


# =============================================================================
# Analysis history  (persisted past runs)
# =============================================================================
# NOTE: the specific routes (/analysis/history, /analysis/tickers) MUST be
# declared before the catch-all /analysis/{run_id} so they aren't shadowed.

@router.get("/analysis/history")
async def analysis_history(
    ticker: str | None = Query(
        None, description="Ticker symbol (optional; returns recent runs across all tickers if omitted)"
    ),
    limit: int = Query(10, ge=1, le=50),
):
    """Lightweight summaries of past analysis runs, newest first. Omit `ticker` for a global feed."""
    return {
        "ticker": ticker,
        "history": history_store.get_analysis_history(ticker, limit=limit),
    }


@router.get("/analysis/tickers")
async def analysis_tickers():
    """Distinct tickers that have stored runs, with run counts (for the sidebar)."""
    return {"tickers": history_store.list_tickers()}


# =============================================================================
# Research Data Copilot
# =============================================================================

@router.post("/analysis/query-data", response_model=research_copilot.QueryDataResponse)
async def query_data(
    body: QueryDataRequest,
    store: DocumentStore = Depends(get_document_store),
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Answer one ad-hoc, grounded data question against a company's already-
    available data — financial tables, filing text, the last analysis run's
    captured earnings-call material, and/or live peer metrics.

    This is NOT a MAS agent and does not run the pipeline: it is a single
    extraction call scoped to `data_scope`, meant for drafting a research note
    without leaving the Deep Analysis workspace.
    """
    company_store = store.get_company_store(body.ticker.strip().upper())
    try:
        return await research_copilot.query_data(
            company_store=company_store,
            debate_store=debate_store,
            ticker=body.ticker,
            query=body.query,
            data_scope=body.data_scope,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — an extraction failure is not a 500
        logger.error(f"Research copilot query failed: {e}")
        raise HTTPException(status_code=502, detail=f"Research copilot query failed: {e}")


@router.get("/analysis/{run_id}")
async def get_analysis(run_id: str):
    """Full stored record for a specific past run."""
    record = history_store.get_analysis(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No analysis run '{run_id}' found.")
    return record
