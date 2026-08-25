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

from schemas import SecFetchRequest, SecFetchResponse, ResolvedFiling, FilingMeta
from services import sec_fetch
from services.ingestion import ingest_pdf, staging_path
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

    # ── Step 1: plan the range (single metadata query, blocking → threadpool) ──
    try:
        planned = await run_in_threadpool(
            sec_fetch.plan_filings,
            req.ticker,
            req.form_type,
            req.start_year,
            req.end_year,
            req.start_quarter,
            req.end_quarter,
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
        f"{len(planned)} filing(s) planned"
    )

    filings: list[FilingMeta] = []
    resolved: list[ResolvedFiling] = []
    affected_tickers: set[str] = set()
    rate_limited = False

    # ── Step 2: render + ingest each period sequentially ──
    for p in planned:
        if rate_limited:
            # SEC throttled us earlier this run — don't keep hitting EDGAR.
            filings.append(FilingMeta(
                filename=p.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message="Skipped: SEC rate-limited an earlier filing in this "
                        "request. Wait a few minutes and retry a smaller range.",
            ))
            continue

        try:
            fetched = await run_in_threadpool(sec_fetch.render_planned, p)
        except sec_fetch.SecRateLimited as e:
            rate_limited = True
            filings.append(FilingMeta(
                filename=p.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message=f"SEC rate-limited this request: {e}",
            ))
            continue
        except sec_fetch.SecFetchError as e:
            # Render failure for this one period — record and keep going.
            filings.append(FilingMeta(
                filename=p.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message=f"Could not retrieve {p.period_label}: {e}",
            ))
            continue

        # Stage the PDF, then run the shared ingestion pipeline (which routes it
        # into the resolved company's store).
        dest = staging_path(fetched.filename)
        try:
            dest.write_bytes(fetched.pdf_bytes)
            meta = await ingest_pdf(dest, fetched.filename, store)
        except Exception as e:  # noqa: BLE001 — isolate per-period ingestion
            logger.error(f"[sec] ingest failed for {p.period_label}: {e}")
            filings.append(FilingMeta(
                filename=fetched.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message=f"Retrieved but failed to ingest {p.period_label}: {e}",
            ))
            continue

        filings.append(meta)
        if meta.ticker:
            affected_tickers.add(meta.ticker)
        resolved.append(ResolvedFiling(
            ticker=fetched.ticker,
            form_type=fetched.form_type,
            period_label=fetched.period_label,
            filing_date=fetched.filing_date,
            accession_number=fetched.accession_number or None,
            document_url=fetched.document_url,
        ))

    # ── Step 3: refresh the merged-tables cache once per company touched ──
    for tk in affected_tickers:
        store.get_company_store(tk).rebuild_merged_tables()

    succeeded = sum(1 for f in filings if f.status in ("success", "partial"))
    logger.info(
        f"[sec] {req.ticker} {req.form_type} {range_label}: "
        f"{succeeded}/{len(filings)} ingested"
    )

    return SecFetchResponse(
        ticker=req.ticker,
        range_label=range_label,
        total_files=len(filings),
        succeeded=succeeded,
        filings=filings,
        resolved_filings=resolved,
    )
