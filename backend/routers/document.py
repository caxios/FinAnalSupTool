"""
routers.document
─────────────────
Filing ingestion & retrieval endpoints:

  POST /upload       — Batch PDF upload → extract tables + text sections
  GET  /financials   — Return merged financial table data (outer-joined)
  GET  /filing-text  — Return parsed text section for a specific period
  GET  /periods      — List all uploaded filing periods (for dropdowns)
  GET  /filing-pdf   — Stream the PDF pages for one section of a filing

All state lives in the injected ``DocumentStore`` (see services.storage).
"""

from __future__ import annotations

import io
import logging

import pandas as pd
from fastapi import APIRouter, Depends, UploadFile, File, Query, HTTPException
from fastapi.responses import StreamingResponse

from schemas import (
    UploadResponse,
    FilingMeta,
    FinancialTableResponse,
    FilingTextResponse,
)
from parsers.pdf_utils import (
    chars_to_pages,
    extract_section_pages,
    pdf_to_text,
    _find_section_span,
    SECTION_LABELS,
    SECTION_MAP_10K,
    SECTION_MAP_10Q,
)
from services.ingestion import ingest_pdf, staging_path
from services.storage import CompanyStore, DocumentStore, get_document_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["document"])


def _company_or_404(store: DocumentStore, ticker: str) -> CompanyStore:
    """
    Resolve a ticker to its :class:`CompanyStore`, or 404 with what IS available.

    Deliberately does not auto-create: a typo'd symbol must not register an empty
    store and then look like a company with no data.
    """
    if not store.has_company(ticker):
        available = store.list_tickers()
        raise HTTPException(
            status_code=404,
            detail=f"No data for ticker '{ticker}'. "
                   f"Available: {available or '(none — upload filings first)'}.",
        )
    return store.get_company_store(ticker)


# =============================================================================
# POST /upload
# =============================================================================

@router.post("/upload", response_model=UploadResponse)
async def upload_filings(
    files: list[UploadFile] = File(...),
    store: DocumentStore = Depends(get_document_store),
):
    """
    Upload one or more SEC filing PDFs for processing.

    For each uploaded file, this endpoint:
      1. Saves the PDF to a temporary directory
      2. Detects the form type (10-K/10-Q) and filing period from the cover page
      3. Extracts financial tables (XBRL-first, pdfplumber fallback)
      4. Extracts text sections (MD&A, Footnotes, etc.) using PyMuPDF + regex
      5. Stores everything in memory for later retrieval

    Returns:
        UploadResponse with per-file metadata and processing status.
    """
    results: list[FilingMeta] = []
    # Tickers touched this batch — each gets its merged-tables cache rebuilt once
    # at the end (ingest_pdf routes each filing to its own company store).
    affected_tickers: set[str] = set()

    for upload_file in files:
        filename = upload_file.filename or "unknown.pdf"
        logger.info(f"Processing upload: {filename}")

        # ── Step 1: Stage the uploaded file; ingest_pdf relocates it into the
        # resolved company's temp dir once the ticker is known. ──
        dest = staging_path(filename)
        try:
            content = await upload_file.read()
            dest.write_bytes(content)
        except Exception as e:
            logger.error(f"Failed to save {filename}: {e}")
            results.append(FilingMeta(
                filename=filename,
                status="failed",
                message=f"Failed to save file: {e}",
            ))
            continue

        # ── Steps 2-5: Extract tables + text and persist into the company store. ──
        # Shared with POST /sec/fetch so both entry points ingest identically.
        meta = await ingest_pdf(dest, filename, store)
        results.append(meta)
        if meta.ticker:
            affected_tickers.add(meta.ticker)

    # Rebuild the merged tables cache for each company touched this batch, so
    # GET /financials reflects the newly uploaded data.
    for tk in affected_tickers:
        store.get_company_store(tk).rebuild_merged_tables()

    return UploadResponse(total_files=len(results), filings=results)


# =============================================================================
# GET /financials
# =============================================================================

@router.get("/financials", response_model=FinancialTableResponse)
async def get_financials(
    ticker: str = Query(..., description="Company ticker, e.g. 'AAPL'"),
    statement_type: str = Query(
        "balance_sheet",
        description="One of: balance_sheet, income_statement, cash_flow, ratios",
    ),
    store: DocumentStore = Depends(get_document_store),
):
    """
    Return one company's merged financial table for a given statement type.

    The table merges the same statement type across all of that company's filing
    periods using a pandas outer join. Columns represent filing periods; rows are
    line items. "ratios" is a synthetic statement computed from raw XBRL metrics.
    """
    # Validate the statement_type parameter
    valid_types = ["balance_sheet", "income_statement", "cash_flow", "ratios"]
    if statement_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid statement_type '{statement_type}'. "
                   f"Must be one of: {valid_types}",
        )

    company = _company_or_404(store, ticker)

    # Check if any data has been extracted for this company
    if not company.merged_tables:
        raise HTTPException(
            status_code=404,
            detail=f"No financial data available for {ticker}. "
                   f"Upload filing PDFs first via POST /upload.",
        )

    # Get the merged DataFrame for this statement type
    df = company.merged_tables.get(statement_type)

    if df is None or df.empty:
        if statement_type == "ratios":
            raise HTTPException(
                status_code=404,
                detail=f"No financial ratios available for {ticker}. Ratios are "
                       "computed from SEC XBRL data, which was not available for "
                       "the uploaded filings (the company could not be matched to "
                       "an EDGAR CIK).",
            )
        raise HTTPException(
            status_code=404,
            detail=f"No '{statement_type}' tables were found in {ticker}'s filings. "
                   f"The PDF may not contain recognizable "
                   f"{statement_type.replace('_', ' ')} tables.",
        )

    # Convert NaN → None for clean JSON serialization
    # (NaN is not valid JSON, but None becomes null)
    df_clean = df.where(pd.notna(df), None)

    return FinancialTableResponse(
        statement_type=statement_type,
        columns=list(df_clean.columns),
        rows=df_clean.to_dict(orient="records"),
    )


# =============================================================================
# GET /filing-text
# =============================================================================

@router.get("/filing-text", response_model=FilingTextResponse)
async def get_filing_text(
    ticker: str = Query(..., description="Company ticker, e.g. 'AAPL'"),
    period: str = Query(..., description="Filing period key, e.g. '2023-10K'"),
    section: str = Query(..., description="Section key: 'mda', 'footnotes', 'supplementary', 'risk_factors', 'business'"),
    store: DocumentStore = Depends(get_document_store),
):
    """
    Return the extracted text for a specific section of a specific filing.

    The frontend calls this when the user selects a period from the
    dropdown and a tab in the Lower Pane.
    """
    company = _company_or_404(store, ticker)

    # Check if the requested period exists in this company's data
    if period not in company.text_store:
        available = list(company.text_store.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Period '{period}' not found for {ticker}. "
                   f"Available periods: {available}. "
                   f"Upload the corresponding filing PDF first via POST /upload.",
        )

    sections = company.text_store[period]

    # Validate the section key
    valid_sections = list(SECTION_LABELS.keys())
    if section not in valid_sections:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid section '{section}'. Must be one of: {valid_sections}",
        )

    content = sections.get(section)
    label = SECTION_LABELS.get(section, section)

    # If the section exists in the map but its content is None,
    # the regex didn't find it in the PDF
    if content is None:
        meta = company.filing_meta.get(period, {})
        form_type = meta.get("form_type", "unknown")

        raise HTTPException(
            status_code=404,
            detail=f"Section '{label}' was not found in the {period} filing. "
                   f"This may be because: (1) the PDF does not contain this section, "
                   f"(2) the section headers don't match expected patterns for "
                   f"{form_type} filings, or (3) the section is in the first 5 pages "
                   f"which are skipped to avoid ToC traps.",
        )

    return FilingTextResponse(
        period=period,
        section=section,
        title=label,
        content=content,
    )


# =============================================================================
# GET /periods
# =============================================================================

@router.get("/periods")
async def list_periods(
    ticker: str = Query(..., description="Company ticker, e.g. 'AAPL'"),
    store: DocumentStore = Depends(get_document_store),
):
    """
    List one company's uploaded filing periods and their metadata.

    The frontend calls this on page load and after each upload to
    populate the period dropdown in the Lower Pane.
    """
    # An unknown ticker here is an empty list rather than a 404: the frontend
    # polls this while switching companies, before any filing has been ingested.
    if not store.has_company(ticker):
        return {"ticker": ticker, "periods": []}

    company = store.get_company_store(ticker)
    return {
        "ticker": ticker,
        "periods": [
            {
                "period_key": key,
                "form_type": meta.get("form_type"),
                "period": meta.get("period"),
                "filename": meta.get("filename"),
            }
            for key, meta in company.filing_meta.items()
        ],
    }


# =============================================================================
# GET /filing-pdf
# =============================================================================

@router.get("/filing-pdf")
async def get_filing_pdf(
    ticker: str = Query(..., description="Company ticker, e.g. 'AAPL'"),
    period: str = Query(..., description="Filing period key, e.g. '2023-10K'"),
    section: str = Query(..., description="Section key: 'mda', 'footnotes', etc."),
    store: DocumentStore = Depends(get_document_store),
):
    """
    Return a PDF containing only the pages for a specific section of a filing.

    Looks up the section's character span in the re-extracted full text, maps it
    to PDF page numbers via the stored page-offset map, extracts those pages into
    a new mini-PDF, and streams it back for the frontend's iframe viewer.
    """
    company = _company_or_404(store, ticker)

    # Validate period exists for this company
    if period not in company.filing_meta:
        available = list(company.filing_meta.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Period '{period}' not found for {ticker}. Available: {available}",
        )

    # Validate section key
    valid_sections = list(SECTION_LABELS.keys())
    if section not in valid_sections:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid section '{section}'. Must be one of: {valid_sections}",
        )

    # Check that we have page offset data for this period
    if period not in company.page_map_store:
        raise HTTPException(
            status_code=404,
            detail=f"No page map data for period '{period}'. Re-upload the filing.",
        )

    # Get the full text and page offsets
    page_offsets = company.page_map_store[period]
    meta = company.filing_meta[period]
    form_type = meta.get("form_type", "10-K")

    pdf_path = company.upload_dir / meta["filename"]
    if not pdf_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"PDF file for period '{period}' no longer exists on disk.",
        )

    # Re-extract full text to find section char boundaries
    full_text, _ = pdf_to_text(pdf_path)

    # Find the section's character span using the same regex logic
    section_map = SECTION_MAP_10K if form_type == "10-K" else SECTION_MAP_10Q
    config = section_map.get(section)
    if config is None:
        raise HTTPException(
            status_code=400,
            detail=f"Section '{section}' not available for {form_type} filings.",
        )

    span = _find_section_span(
        full_text,
        start_patterns=config["start_patterns"],
        end_patterns=config["end_patterns"],
        exclude_patterns=config.get("exclude", []),
    )

    if span is None:
        raise HTTPException(
            status_code=404,
            detail=f"Section '{section}' was not found in the {period} filing.",
        )

    char_start, char_end = span

    # Map character span to page numbers
    page_range = chars_to_pages(char_start, char_end, page_offsets)
    if page_range is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not map section '{section}' to PDF pages.",
        )

    start_page, end_page = page_range
    logger.info(
        f"Section '{section}' for {period}: chars {char_start}-{char_end} "
        f"→ pages {start_page}-{end_page}"
    )

    # Extract the pages into a new PDF
    pdf_bytes = extract_section_pages(pdf_path, start_page, end_page)

    # Stream the PDF back to the client
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{period}_{section}.pdf"',
        },
    )
