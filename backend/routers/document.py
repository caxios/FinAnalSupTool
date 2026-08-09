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
from pathlib import Path

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
    detect_filing_metadata,
    extract_all_sections,
    extract_tables,
    chars_to_pages,
    extract_section_pages,
    pdf_to_text,
    _find_section_span,
    SECTION_LABELS,
    SECTION_MAP_10K,
    SECTION_MAP_10Q,
)
from providers.edgar_xbrl import (
    build_xbrl_statement_tables,
    parse_period_end,
    resolve_company_identity,
)
from services.storage import DocumentStore, get_document_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["document"])


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

    for upload_file in files:
        filename = upload_file.filename or "unknown.pdf"
        logger.info(f"Processing upload: {filename}")

        # ── Step 1: Save uploaded file to temp directory ──────
        dest = store.upload_dir / filename
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

        # ── Step 2: Detect form type and filing period ────────
        try:
            meta = detect_filing_metadata(dest)
        except Exception as e:
            logger.error(f"Metadata detection failed for {filename}: {e}")
            results.append(FilingMeta(
                filename=filename,
                status="failed",
                message=f"Could not read PDF metadata: {e}",
            ))
            continue

        form_type = meta.get("form_type")

        # Can't proceed without knowing the form type
        if not form_type:
            logger.warning(f"Could not detect form type for {filename}")
            results.append(FilingMeta(
                filename=filename,
                status="failed",
                message="Could not detect form type (10-K or 10-Q) from the PDF.",
            ))
            continue

        # Parse the period-end date — the basis for both a unique, sortable
        # period key and (later) matching XBRL facts to this filing.
        period_end = parse_period_end(meta.get("period"))
        # Provisional key used only if we can't derive anything better.
        provisional_key = meta.get("period_key") or Path(filename).stem

        # ── Step 3: Extract financial tables ──────────────────
        # XBRL-first: try SEC EDGAR's machine-readable XBRL data, which gives
        # exact, standardized numbers with no PDF-table guesswork. Fall back
        # to pdfplumber only if the company can't be identified or EDGAR fails.
        detected_cik: int | None = None
        xbrl_metrics: dict | None = None
        xbrl_label: str | None = None
        try:
            (xbrl_tables, detected_cik, xbrl_metrics, xbrl_label) = (
                await build_xbrl_statement_tables(
                    dest,
                    filename=filename,
                    form_type=form_type,
                    period_str=meta.get("period"),
                )
            )
        except Exception as e:
            logger.error(f"XBRL extraction failed for {filename}: {e}")
            xbrl_tables = None

        # Finalize the period key. Prefer XBRL's authoritative fiscal label
        # (e.g. "Q2 FY2026"); otherwise derive a unique, sortable key from the
        # period-end date. This MUST be unique per filing — otherwise Q1/Q2/Q3
        # of the same year would share one key and silently overwrite each
        # other (the bug where same-year uploads were "skipped").
        period_key = xbrl_label or store.derive_period_key(
            form_type, period_end, provisional_key
        )

        classified_tables: dict = {}
        data_source = "pdfplumber"

        if xbrl_tables is not None:
            classified_tables = xbrl_tables
            data_source = "xbrl"
            store.table_store[period_key] = classified_tables
            # Store the raw metrics so the Financial Ratios tab can be built.
            if xbrl_metrics is not None:
                store.metrics_store[period_key] = xbrl_metrics
            table_count = sum(len(v) for v in classified_tables.values())
            logger.info(
                f"  [{period_key}] tables from XBRL (CIK {detected_cik}): "
                f"{table_count} statement table(s)"
            )
        else:
            # No XBRL for this period — drop any stale ratio metrics so a
            # re-upload that falls back to pdfplumber doesn't show old ratios.
            store.metrics_store.pop(period_key, None)
            # Fallback: parse tables out of the PDF with pdfplumber.
            try:
                classified_tables = extract_tables(dest)
                store.table_store[period_key] = classified_tables
                table_count = sum(len(v) for v in classified_tables.values())
                logger.info(
                    f"  [{period_key}] tables extracted (pdfplumber): {table_count}"
                )
            except Exception as e:
                logger.error(f"Table extraction failed for {filename}: {e}")
                classified_tables = {}

        # ── Step 4: Extract text sections ─────────────────────
        try:
            sections, page_offsets = extract_all_sections(dest, form_type=form_type)
            store.text_store[period_key] = sections
            store.page_map_store[period_key] = page_offsets
            found = [k for k, v in sections.items() if v is not None]
            logger.info(f"  Text sections extracted: {found}")
        except Exception as e:
            logger.error(f"Text extraction failed for {filename}: {e}")
            sections = {}

        # ── Step 5: Store filing metadata ─────────────────────
        # Persist the detected CIK, table source, and a `sort_date` so the
        # merged tables can be ordered chronologically. Also resolve the company
        # name/ticker (from data already fetched) so the Company Media view
        # knows which company to look up.
        entity_name: str | None = None
        ticker: str | None = None
        if detected_cik is not None:
            try:
                entity_name, ticker = await resolve_company_identity(detected_cik)
            except Exception as e:
                logger.warning(f"Company identity resolution failed: {e}")

        store.filing_meta[period_key] = {
            "filename": filename,
            "form_type": form_type,
            "period": meta.get("period"),
            "period_key": period_key,
            "cik": detected_cik,
            "entity_name": entity_name,
            "ticker": ticker,
            "data_source": data_source,
            "sort_date": period_end.isoformat() if period_end else None,
        }

        # ── Determine overall processing status ──────────────
        has_tables = bool(classified_tables and any(classified_tables.values()))
        has_text = bool(sections and any(v is not None for v in sections.values()))

        if has_tables and has_text:
            status = "success"
            message = None
        elif has_tables or has_text:
            # Got something but not everything
            status = "partial"
            parts = []
            if not has_tables:
                parts.append("no tables detected")
            if not has_text:
                parts.append("no text sections detected")
            message = "Partial extraction: " + ", ".join(parts)
        else:
            status = "failed"
            message = "No tables or text sections could be extracted from this PDF."

        results.append(FilingMeta(
            filename=filename,
            detected_period=period_key,
            form_type=form_type,
            status=status,
            message=message,
        ))

    # After processing all files, rebuild the merged tables cache
    # so GET /financials reflects the newly uploaded data
    store.rebuild_merged_tables()

    return UploadResponse(total_files=len(results), filings=results)


# =============================================================================
# GET /financials
# =============================================================================

@router.get("/financials", response_model=FinancialTableResponse)
async def get_financials(
    statement_type: str = Query(
        "balance_sheet",
        description="One of: balance_sheet, income_statement, cash_flow, ratios",
    ),
    store: DocumentStore = Depends(get_document_store),
):
    """
    Return the merged financial table for a given statement type.

    The table merges the same statement type across all uploaded filing periods
    using a pandas outer join. Columns represent filing periods; rows are line
    items. "ratios" is a synthetic statement computed from raw XBRL metrics.
    """
    # Validate the statement_type parameter
    valid_types = ["balance_sheet", "income_statement", "cash_flow", "ratios"]
    if statement_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid statement_type '{statement_type}'. "
                   f"Must be one of: {valid_types}",
        )

    # Check if any data has been uploaded
    if not store.merged_tables:
        raise HTTPException(
            status_code=404,
            detail="No financial data available. Upload filing PDFs first via POST /upload.",
        )

    # Get the merged DataFrame for this statement type
    df = store.merged_tables.get(statement_type)

    if df is None or df.empty:
        if statement_type == "ratios":
            raise HTTPException(
                status_code=404,
                detail="No financial ratios available. Ratios are computed from "
                       "SEC XBRL data, which was not available for the uploaded "
                       "filings (the company could not be matched to an EDGAR CIK).",
            )
        raise HTTPException(
            status_code=404,
            detail=f"No '{statement_type}' tables were found in the uploaded filings. "
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
    period: str = Query(..., description="Filing period key, e.g. '2023-10K'"),
    section: str = Query(..., description="Section key: 'mda', 'footnotes', 'supplementary', 'risk_factors', 'business'"),
    store: DocumentStore = Depends(get_document_store),
):
    """
    Return the extracted text for a specific section of a specific filing.

    The frontend calls this when the user selects a period from the
    dropdown and a tab in the Lower Pane.
    """
    # Check if the requested period exists in our data
    if period not in store.text_store:
        available = list(store.text_store.keys()) if store.text_store else []
        raise HTTPException(
            status_code=404,
            detail=f"Period '{period}' not found. "
                   f"Available periods: {available}. "
                   f"Upload the corresponding filing PDF first via POST /upload.",
        )

    sections = store.text_store[period]

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
        meta = store.filing_meta.get(period, {})
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
    store: DocumentStore = Depends(get_document_store),
):
    """
    List all uploaded filing periods and their metadata.

    The frontend calls this on page load and after each upload to
    populate the period dropdown in the Lower Pane.
    """
    if not store.filing_meta:
        return {"periods": []}

    return {
        "periods": [
            {
                "period_key": key,
                "form_type": meta.get("form_type"),
                "period": meta.get("period"),
                "filename": meta.get("filename"),
            }
            for key, meta in store.filing_meta.items()
        ]
    }


# =============================================================================
# GET /filing-pdf
# =============================================================================

@router.get("/filing-pdf")
async def get_filing_pdf(
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
    # Validate period exists
    if period not in store.filing_meta:
        available = list(store.filing_meta.keys()) if store.filing_meta else []
        raise HTTPException(
            status_code=404,
            detail=f"Period '{period}' not found. Available: {available}",
        )

    # Validate section key
    valid_sections = list(SECTION_LABELS.keys())
    if section not in valid_sections:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid section '{section}'. Must be one of: {valid_sections}",
        )

    # Check that we have page offset data for this period
    if period not in store.page_map_store:
        raise HTTPException(
            status_code=404,
            detail=f"No page map data for period '{period}'. Re-upload the filing.",
        )

    # Get the full text and page offsets
    page_offsets = store.page_map_store[period]
    meta = store.filing_meta[period]
    form_type = meta.get("form_type", "10-K")

    pdf_path = store.upload_dir / meta["filename"]
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
