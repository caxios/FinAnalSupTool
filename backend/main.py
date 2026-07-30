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
import io
import tempfile
import shutil
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd

from schemas import (
    UploadResponse,
    FilingMeta,
    FinancialTableResponse,
    FilingTextResponse,
    ChatRequest,
    ChatResponse,
)
from pdf_utils import (
    detect_filing_metadata,
    extract_all_sections,
    extract_tables,
    merge_tables_across_periods,
    chars_to_pages,
    extract_section_pages,
    _find_section_span,
    SECTION_LABELS,
    SECTION_MAP_10K,
    SECTION_MAP_10Q,
)
from edgar_xbrl import (
    build_xbrl_statement_tables,
    build_ratios_table,
    parse_period_end,
)
from gemini_chat import build_context, ask_gemini, gemini_api_key


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
# Structure: {"balance_sheet": merged_df, "income_statement": merged_df,
#             "cash_flow": merged_df, "ratios": merged_df}
_merged_tables: dict[str, pd.DataFrame] = {}

# Raw numeric metrics per period, used to compute historical financial ratios.
# Populated only for filings whose data came from XBRL (the ratios need exact
# numbers, and reuse the facts already fetched for the statement tables).
# Structure: {"2025-Q2-10-Q": {"revenue": 2.0e9, "total_assets": ..., ...}}
_metrics_store: dict[str, dict] = {}

# Stores the page offset map for each filing period.
# Used to reverse-map section character spans back to PDF page numbers.
# Structure: {"2023-10K": [(page_num, char_start, char_end), ...]}
_page_map_store: dict[str, list[tuple[int, int, int]]] = {}


def _derive_period_key(
    form_type: str, period_end, fallback: str
) -> str:
    """
    Build a unique, human-readable period key from the period-end date.

    Used only when XBRL doesn't supply an authoritative fiscal label
    (e.g. "Q2 FY2026"). The key MUST be unique per filing so that Q1/Q2/Q3
    of the same year don't collapse onto one shared key and overwrite each
    other in the in-memory stores.

      - 10-K → "FY2025"
      - 10-Q → "Aug 2025"  (month + year — unique per quarter)
      - unknown period → the provided fallback (usually the filename stem)
    """
    if period_end is None:
        return fallback
    if form_type == "10-K":
        return f"FY{period_end.year}"
    return period_end.strftime("%b %Y")


def _order_period_columns(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """
    Reorder a merged table's period columns chronologically (oldest → newest).

    The first column ("Line Item" or "Ratio") is kept in place; the remaining
    period columns are sorted by each period's stored `sort_date`. Periods
    without a parseable date sort to the end (by name) so nothing is dropped.
    """
    if df is None or df.empty or len(df.columns) <= 1:
        return df

    first_col = df.columns[0]
    period_cols = [c for c in df.columns if c != first_col]

    def sort_key(pk: str):
        sort_date = _filing_meta.get(pk, {}).get("sort_date")
        # (0, date) dated periods first in chronological order;
        # (1, name) undated periods after, ordered by key for stability.
        return (0, sort_date) if sort_date else (1, str(pk))

    ordered = sorted(period_cols, key=sort_key)
    return df[[first_col] + ordered]


def _rebuild_merged_tables() -> None:
    """
    Rebuild the cross-period merged tables from _table_store.

    Called after every upload to ensure GET /financials returns
    up-to-date data that includes the newly uploaded filings.
    """
    global _merged_tables
    merged = merge_tables_across_periods(_table_store) if _table_store else {}
    # Historical ratios table — computed from the raw per-period metrics.
    # Shares the same shape as the statement tables so GET /financials and the
    # frontend renderer treat "ratios" like any other statement type.
    merged["ratios"] = build_ratios_table(_metrics_store)
    # Order period columns as a chronological time series (not upload order).
    for key in list(merged.keys()):
        merged[key] = _order_period_columns(merged[key])
    _merged_tables = merged
    logger.info(
        f"Rebuilt merged tables for {len(_table_store)} period(s), "
        f"ratios for {len(_metrics_store)} period(s)"
    )


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
        period_key = xbrl_label or _derive_period_key(
            form_type, period_end, provisional_key
        )

        classified_tables: dict = {}
        data_source = "pdfplumber"

        if xbrl_tables is not None:
            classified_tables = xbrl_tables
            data_source = "xbrl"
            _table_store[period_key] = classified_tables
            # Store the raw metrics so the Financial Ratios tab can be built.
            if xbrl_metrics is not None:
                _metrics_store[period_key] = xbrl_metrics
            table_count = sum(len(v) for v in classified_tables.values())
            logger.info(
                f"  [{period_key}] tables from XBRL (CIK {detected_cik}): "
                f"{table_count} statement table(s)"
            )
        else:
            # No XBRL for this period — drop any stale ratio metrics so a
            # re-upload that falls back to pdfplumber doesn't show old ratios.
            _metrics_store.pop(period_key, None)
            # Fallback: parse tables out of the PDF with pdfplumber.
            try:
                classified_tables = extract_tables(dest)
                _table_store[period_key] = classified_tables
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
            _text_store[period_key] = sections
            _page_map_store[period_key] = page_offsets
            found = [k for k, v in sections.items() if v is not None]
            logger.info(f"  Text sections extracted: {found}")
        except Exception as e:
            logger.error(f"Text extraction failed for {filename}: {e}")
            sections = {}

        # ── Step 5: Store filing metadata ─────────────────────
        # Persist the detected CIK, table source, and a `sort_date` so the
        # merged tables can be ordered chronologically. Subsequent uploads for
        # the same company reuse the cached XBRL facts (edgar_xbrl._facts_cache).
        _filing_meta[period_key] = {
            "filename": filename,
            "form_type": form_type,
            "period": meta.get("period"),
            "period_key": period_key,
            "cik": detected_cik,
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
    _rebuild_merged_tables()

    return UploadResponse(total_files=len(results), filings=results)


# =============================================================================
# ENDPOINT: GET /financials
# =============================================================================

@app.get("/financials", response_model=FinancialTableResponse)
async def get_financials(
    statement_type: str = Query(
        "balance_sheet",
        description="One of: balance_sheet, income_statement, cash_flow, ratios",
    ),
):
    """
    Return the merged financial table for a given statement type.

    The table merges the same statement type across all uploaded
    filing periods using a pandas outer join.  Columns represent
    filing periods; rows are line items.  Unmatched items across
    periods appear as null (outer-join semantics).

    "ratios" is a synthetic statement: historical financial ratios
    computed from the raw XBRL metrics of each period.
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
    if not _merged_tables:
        raise HTTPException(
            status_code=404,
            detail="No financial data available. Upload filing PDFs first via POST /upload.",
        )

    # Get the merged DataFrame for this statement type
    df = _merged_tables.get(statement_type)

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
# ENDPOINT: GET /filing-pdf
# =============================================================================

@app.get("/filing-pdf")
async def get_filing_pdf(
    period: str = Query(..., description="Filing period key, e.g. '2023-10K'"),
    section: str = Query(..., description="Section key: 'mda', 'footnotes', etc."),
):
    """
    Return a PDF containing only the pages for a specific section of a filing.

    This endpoint:
      1. Looks up the section's character span in the stored full text
      2. Maps the char span to PDF page numbers using the page offset map
      3. Extracts those pages into a new mini-PDF
      4. Streams the PDF back to the client

    The frontend embeds this in an iframe for native PDF viewing.
    """
    # Validate period exists
    if period not in _filing_meta:
        available = list(_filing_meta.keys()) if _filing_meta else []
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
    if period not in _page_map_store:
        raise HTTPException(
            status_code=404,
            detail=f"No page map data for period '{period}'. Re-upload the filing.",
        )

    # Get the full text and page offsets
    sections = _text_store.get(period, {})
    page_offsets = _page_map_store[period]
    meta = _filing_meta[period]
    form_type = meta.get("form_type", "10-K")

    # Re-extract the full text to find the section's character span.
    # We need the original full_text to locate the section boundaries.
    # Since we already stored page_offsets, we can reconstruct the text
    # position by re-running the section finder on the stored text.
    # But we don't store full_text — so we re-extract it from the PDF.
    pdf_path = _upload_dir / meta["filename"]
    if not pdf_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"PDF file for period '{period}' no longer exists on disk.",
        )

    # Re-extract full text to find section char boundaries
    from pdf_utils import pdf_to_text
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


# =============================================================================
# ENDPOINT: POST /chat
# =============================================================================

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Answer a natural-language question about the uploaded filings using Gemini.

    The assistant is grounded strictly in the app's own data — the merged
    financial statements + ratios and the extracted filing text sections. It
    does not fetch anything new from SEC EDGAR. The full data context is
    re-assembled on each call, so freshly uploaded filings are always in scope.
    """
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    # Fail fast with a clear message if the key isn't configured.
    if not gemini_api_key():
        raise HTTPException(
            status_code=503,
            detail="The AI assistant is not configured: GEMINI_API_KEY is not "
                   "set on the backend. Set it in the server environment and "
                   "restart to enable chat.",
        )

    # Short-circuit when there's nothing to talk about yet.
    if not _filing_meta:
        return ChatResponse(
            answer="No filings have been uploaded yet. Upload one or more SEC "
                   "10-K / 10-Q PDFs, then ask me about the financials, ratios, "
                   "or filing text.",
        )

    # Assemble the grounding context from the current in-memory data.
    context = build_context(_merged_tables, _text_store, _filing_meta)

    history = [{"role": m.role, "content": m.content} for m in request.history]

    try:
        answer = await ask_gemini(question, history, context)
    except RuntimeError as e:
        # Configuration / API errors from the Gemini layer.
        raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(answer=answer)


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
