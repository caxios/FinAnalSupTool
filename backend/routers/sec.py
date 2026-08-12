"""
routers.sec
────────────
Automated SEC EDGAR filing retrieval.

  POST /sec/fetch — Resolve ticker + form + fiscal period to a filing on SEC
                    EDGAR, render it to PDF, and run it through the *same*
                    ingestion pipeline as a manual upload.

This is the automated counterpart to ``POST /upload``: instead of the user
finding, downloading, and dragging in a filing PDF, they name what they want and
the backend fetches it. Once the PDF exists on disk it is handed to
``services.ingestion.ingest_pdf`` unchanged — so every downstream feature
(financial tables, text sections, Deep Analysis) works identically.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from schemas import SecFetchRequest, SecFetchResponse, ResolvedFiling
from services import sec_fetch
from services.ingestion import ingest_pdf
from services.storage import DocumentStore, get_document_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sec", tags=["sec"])


@router.post("/fetch", response_model=SecFetchResponse)
async def fetch_and_ingest(
    req: SecFetchRequest,
    store: DocumentStore = Depends(get_document_store),
):
    """
    Fetch a filing from SEC EDGAR by ticker/form/period and ingest it.

    Steps:
      1. Resolve + render the filing to PDF via ``findata`` (in a threadpool,
         since it does blocking network I/O and spawns a Chromium subprocess).
      2. Save the PDF into the store's temp dir (same place uploads live).
      3. Run the shared ``ingest_pdf`` pipeline (tables + text extraction).
      4. Rebuild the merged tables cache so GET /financials sees it.

    Returns the standard upload result plus provenance of the retrieved filing.
    After this succeeds the frontend can run Deep Analysis exactly as it would
    for a manually uploaded filing.
    """
    # ── Step 1: fetch + render (blocking → offload to a worker thread) ──
    try:
        fetched = await run_in_threadpool(
            sec_fetch.fetch_filing_pdf,
            req.ticker,
            req.form_type,
            req.year,
            req.quarter,
        )
    except sec_fetch.InvalidRequest as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sec_fetch.TickerNotFound as e:
        raise HTTPException(
            status_code=404,
            detail=f"Ticker not found on SEC EDGAR: {e}",
        )
    except sec_fetch.FilingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except sec_fetch.SecRateLimited as e:
        # 503 + Retry-After hint: SEC is throttling automated traffic.
        raise HTTPException(
            status_code=503,
            detail=str(e),
            headers={"Retry-After": "120"},
        )
    except sec_fetch.RenderFailed as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not render the filing to PDF: {e}",
        )
    except sec_fetch.SecFetchError as e:
        raise HTTPException(status_code=502, detail=str(e))

    logger.info(
        f"[sec] fetched {fetched.ticker} {fetched.form_type} "
        f"filed {fetched.filing_date} → {fetched.filename}"
    )

    # ── Step 2: persist the PDF where uploads live ──
    dest = store.upload_dir / fetched.filename
    try:
        dest.write_bytes(fetched.pdf_bytes)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save the fetched filing to disk: {e}",
        )

    # ── Step 3: shared ingestion pipeline ──
    meta = await ingest_pdf(dest, fetched.filename, store)

    # ── Step 4: refresh the merged-tables cache (as /upload does) ──
    store.rebuild_merged_tables()

    return SecFetchResponse(
        total_files=1,
        filings=[meta],
        resolved_filing=ResolvedFiling(
            ticker=fetched.ticker,
            form_type=fetched.form_type,
            filing_date=fetched.filing_date,
            accession_number=fetched.accession_number or None,
            document_url=fetched.document_url,
        ),
    )
