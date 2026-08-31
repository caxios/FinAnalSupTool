"""
routers.sec
────────────
Automated SEC EDGAR filing retrieval.

  POST /sec/fetch — Resolve ticker + form + a *fiscal-year range* to filings on
                    SEC EDGAR, render each to PDF, and run them through the
                    *same* ingestion pipeline as a manual upload.

This is the automated counterpart to ``POST /upload``: instead of the user
finding, downloading, and dragging in filing PDFs, they name a company + form +
year range and the backend fetches every matching filing. Once each PDF exists
on disk it is handed to ``services.ingestion.ingest_pdf`` unchanged — so every
downstream feature (financial tables, text sections, Deep Analysis) works
identically. Longitudinal trend analysis just means requesting several years.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from schemas import SecFetchRequest, SecFetchResponse
from services import sec_fetch, sec_ingest
from services.storage import DocumentStore, get_document_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sec", tags=["sec"])


@router.post("/fetch", response_model=SecFetchResponse)
async def fetch_and_ingest(
    req: SecFetchRequest,
    store: DocumentStore = Depends(get_document_store),
):
    """
    Fetch every filing for a ticker/form over a fiscal-year range and ingest them.

    Steps:
      1. Plan: one EDGAR metadata query resolves which filings fall in the range
         (one 10-K per year, or every 10-Q quarter per year).
      2. For each planned filing, sequentially (to respect SEC rate limits):
         render it to PDF, save it beside uploads, and run the shared
         ``ingest_pdf`` pipeline. Blocking work runs in a threadpool.
      3. Rebuild the merged-tables cache once at the end.

    Partial failures are graceful: a period that fails to render or ingest comes
    back as a ``failed`` result while the others still succeed. If SEC starts
    rate-limiting mid-run, the remaining periods are skipped (rather than
    hammering EDGAR) and reported as failed.

    Returns one result per attempted period plus the provenance of each filing
    retrieved. After this the frontend can run Deep Analysis exactly as it would
    for manually uploaded filings.
    """
    if req.form_type == "10-Q" and (req.start_quarter or req.end_quarter):
        sq, eq = req.start_quarter or 1, req.end_quarter or 3
        range_label = f"{req.start_year} Q{sq}–{req.end_year} Q{eq}"
    else:
        range_label = f"{req.start_year}–{req.end_year}"

    # The whole plan → render → ingest sequence lives in `services.sec_ingest`,
    # shared with the portfolio's baseline auto-fetch. This router only maps the
    # domain exceptions onto HTTP status codes.
    try:
        result = await sec_ingest.fetch_and_ingest_range(
            ticker=req.ticker,
            form_type=req.form_type,
            start_year=req.start_year,
            end_year=req.end_year,
            start_quarter=req.start_quarter,
            end_quarter=req.end_quarter,
            store=store,
        )
    except sec_fetch.InvalidRequest as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sec_fetch.TickerNotFound as e:
        raise HTTPException(status_code=404, detail=f"Ticker not found on SEC EDGAR: {e}")
    except sec_fetch.FilingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except sec_fetch.SecFetchError as e:
        raise HTTPException(status_code=502, detail=str(e))

    logger.info(
        f"[sec] {req.ticker} {req.form_type} {range_label}: "
        f"{result.succeeded}/{len(result.filings)} ingested"
    )

    return SecFetchResponse(
        ticker=req.ticker,
        range_label=range_label,
        total_files=len(result.filings),
        succeeded=result.succeeded,
        filings=result.filings,
        resolved_filings=result.resolved,
    )
