"""
main.py
───────
FastAPI backend for the Financial Analysis Support Tool.

This is the main entry point for the backend server.  It exposes four
API endpoints that the React frontend calls:

  POST /upload       — Batch PDF upload → extract tables + text sections
  GET  /financials   — Return merged financial table data (outer-joined)
  GET  /filing-text  — Return parsed text section for a specific period
  GET  /periods      — List all uploaded filing periods (for dropdowns)

Data Storage:
─────────────
All extracted data is stored in module-level Python dicts (in-memory).
This is intentional — we're building a localhost prototype, not a
production system.  Restarting the server clears all data.

CORS:
─────
Configured to allow requests from React dev servers (ports 3000, 5173, 5174).
"""

from __future__ import annotations

import logging
import tempfile
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd

from schemas import (
    UploadResponse,
    FilingMeta,
    FinancialTableResponse,
    FilingTextResponse,
)
from pdf_utils import (
    detect_filing_metadata,
    extract_all_sections,
    extract_tables,
    merge_tables_across_periods,
    SECTION_LABELS,
)


# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# FastAPI App Initialization
# =============================================================================

app = FastAPI(
    title="Financial Analysis Support Tool",
    description=(
        "Parse historical SEC filings (10-K, 10-Q) from uploaded PDFs. "
        "Extract financial tables and qualitative text sections."
    ),
    version="0.1.0",
)

# Allow the React frontend dev servers to make cross-origin requests.
# Without this, browser security blocks fetch() calls from localhost:5173
# to localhost:8000 (different ports = different origins).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",   # Create React App default
        "http://localhost:5173",   # Vite default
        "http://localhost:5174",   # Vite fallback port
    ],
    allow_credentials=True,
    allow_methods=["*"],           # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],           # Allow all headers
)


# =============================================================================
# In-Memory Data Storage
# =============================================================================
# These dicts hold all extracted data, keyed by period_key (e.g., "2023-10K").
# They are populated by POST /upload and read by GET /financials and
# GET /filing-text.

# Stores extracted text sections for each filing period.
# Structure: {"2023-10K": {"mda": "text...", "footnotes": "text...", ...}}
_text_store: dict[str, dict[str, str | None]] = {}

# Stores classified table DataFrames for each filing period.
# Structure: {"2023-10K": {"balance_sheet": [df1, df2], "income_statement": [df3], ...}}
_table_store: dict[str, dict[str, list[pd.DataFrame]]] = {}

# Stores metadata about each uploaded filing (filename, form type, period).
# Structure: {"2023-10K": {"filename": "apple-10k.pdf", "form_type": "10-K", ...}}
_filing_meta: dict[str, dict] = {}

# Cached merged tables — rebuilt after every upload so GET /financials
# can return the result instantly without re-computing.
# Structure: {"balance_sheet": merged_df, "income_statement": merged_df, ...}
_merged_tables: dict[str, pd.DataFrame] = {}


def _rebuild_merged_tables() -> None:
    """
    Rebuild the cross-period merged tables from _table_store.
    
    Called after every upload to ensure GET /financials returns
    up-to-date data that includes the newly uploaded filings.
    """
    global _merged_tables
    if _table_store:
        _merged_tables = merge_tables_across_periods(_table_store)
    else:
        _merged_tables = {}
    logger.info(f"Rebuilt merged tables for {len(_table_store)} period(s)")


# =============================================================================
# Temporary Directory for Uploaded Files
# =============================================================================
# PDFs are saved here temporarily during processing.
# The directory is cleaned up when the server shuts down.

_upload_dir = Path(tempfile.mkdtemp(prefix="finanalst_"))
logger.info(f"Upload temp directory: {_upload_dir}")


# =============================================================================
# ENDPOINT: POST /upload
# =============================================================================

@app.post("/upload", response_model=UploadResponse)
async def upload_filings(files: list[UploadFile] = File(...)):
    """
    Upload one or more SEC filing PDFs for processing.

    For each uploaded file, this endpoint:
      1. Saves the PDF to a temporary directory
      2. Detects the form type (10-K/10-Q) and filing period from the cover page
      3. Extracts financial tables using pdfplumber
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
        dest = _upload_dir / filename
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
        period_key = meta.get("period_key")

        # Can't proceed without knowing the form type
        if not form_type:
            logger.warning(f"Could not detect form type for {filename}")
            results.append(FilingMeta(
                filename=filename,
                status="failed",
                message="Could not detect form type (10-K or 10-Q) from the PDF.",
            ))
            continue

        # If period detection failed, use the filename as a fallback key
        if not period_key:
            period_key = Path(filename).stem
            logger.warning(
                f"Could not detect period for {filename}, using '{period_key}'"
            )

        # ── Step 3: Extract financial tables ──────────────────
        try:
            classified_tables = extract_tables(dest)
            _table_store[period_key] = classified_tables
            table_count = sum(len(v) for v in classified_tables.values())
            logger.info(f"  Tables extracted: {table_count}")
        except Exception as e:
            logger.error(f"Table extraction failed for {filename}: {e}")
            classified_tables = {}

        # ── Step 4: Extract text sections ─────────────────────
        try:
            sections = extract_all_sections(dest, form_type=form_type)
            _text_store[period_key] = sections
            found = [k for k, v in sections.items() if v is not None]
            logger.info(f"  Text sections extracted: {found}")
        except Exception as e:
            logger.error(f"Text extraction failed for {filename}: {e}")
            sections = {}

        # ── Step 5: Store filing metadata ─────────────────────
        _filing_meta[period_key] = {
            "filename": filename,
            "form_type": form_type,
            "period": meta.get("period"),
            "period_key": period_key,
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
    _rebuild_merged_tables()

    return UploadResponse(total_files=len(results), filings=results)


# =============================================================================
# ENDPOINT: GET /financials
# =============================================================================

@app.get("/financials", response_model=FinancialTableResponse)
async def get_financials(
    statement_type: str = Query(
        "balance_sheet",
        description="One of: balance_sheet, income_statement, cash_flow",
    ),
):
    """
    Return the merged financial table for a given statement type.

    The table merges the same statement type across all uploaded
    filing periods using a pandas outer join.  Columns represent
    filing periods; rows are line items.  Unmatched items across
    periods appear as null (outer-join semantics).
    """
    # Validate the statement_type parameter
    valid_types = ["balance_sheet", "income_statement", "cash_flow"]
    if statement_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid statement_type '{statement_type}'. "
                   f"Must be one of: {valid_types}",
        )

    # Check if any data has been uploaded
    if not _merged_tables:
        raise HTTPException(
            status_code=404,
            detail="No financial data available. Upload filing PDFs first via POST /upload.",
        )

    # Get the merged DataFrame for this statement type
    df = _merged_tables.get(statement_type)

    if df is None or df.empty:
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
# ENDPOINT: GET /filing-text
# =============================================================================

@app.get("/filing-text", response_model=FilingTextResponse)
async def get_filing_text(
    period: str = Query(..., description="Filing period key, e.g. '2023-10K'"),
    section: str = Query(..., description="Section key: 'mda', 'footnotes', 'supplementary', 'risk_factors', 'business'"),
):
    """
    Return the extracted text for a specific section of a specific filing.

    The frontend calls this when the user selects a period from the
    dropdown and a tab in the Lower Pane.
    """
    # Check if the requested period exists in our data
    if period not in _text_store:
        available = list(_text_store.keys()) if _text_store else []
        raise HTTPException(
            status_code=404,
            detail=f"Period '{period}' not found. "
                   f"Available periods: {available}. "
                   f"Upload the corresponding filing PDF first via POST /upload.",
        )

    sections = _text_store[period]

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
        meta = _filing_meta.get(period, {})
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
# ENDPOINT: GET /periods
# =============================================================================

@app.get("/periods")
async def list_periods():
    """
    List all uploaded filing periods and their metadata.

    The frontend calls this on page load and after each upload to
    populate the period dropdown in the Lower Pane.
    """
    if not _filing_meta:
        return {"periods": []}

    return {
        "periods": [
            {
                "period_key": key,
                "form_type": meta.get("form_type"),
                "period": meta.get("period"),
                "filename": meta.get("filename"),
            }
            for key, meta in _filing_meta.items()
        ]
    }


# =============================================================================
# Cleanup on Shutdown
# =============================================================================

@app.on_event("shutdown")
async def cleanup():
    """
    Remove the temporary upload directory when the server shuts down.
    This prevents leftover PDF files from accumulating on disk.
    """
    if _upload_dir.exists():
        shutil.rmtree(_upload_dir, ignore_errors=True)
        logger.info(f"Cleaned up temp dir: {_upload_dir}")
