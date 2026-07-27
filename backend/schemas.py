"""
schemas.py
──────────
Pydantic models that define the API request/response contract.

These models serve two purposes:
  1. Automatic request validation (FastAPI uses them to validate incoming data)
  2. Response serialization (FastAPI uses them to serialize outgoing JSON)

Each model corresponds to one endpoint's response shape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# Upload Endpoint Models (POST /upload)
# ─────────────────────────────────────────────────────────────

class FilingMeta(BaseModel):
    """
    Metadata for a single processed filing.
    
    One of these is returned per uploaded PDF file, telling the
    frontend whether the file was processed successfully and what
    was detected inside it.
    """

    # Original filename of the uploaded PDF
    filename: str

    # Auto-detected filing period, e.g. "2023-10K" or "2024Q1-10Q"
    # None if detection failed
    detected_period: str | None = Field(
        None,
        description="Detected filing period, e.g. '2023-10K', '2024Q1-10Q'",
    )

    # Detected SEC form type: "10-K" or "10-Q"
    # None if the PDF doesn't appear to be a recognized filing
    form_type: str | None = Field(
        None,
        description="Detected form type: '10-K' or '10-Q'",
    )

    # Processing outcome: "success" (tables + text), "partial" (only one),
    # or "failed" (neither)
    status: str = Field(
        description="Processing status: 'success', 'partial', or 'failed'",
    )

    # Human-readable explanation of the status
    # e.g., "Partial extraction: no tables detected"
    message: str | None = Field(
        None,
        description="Human-readable status detail",
    )


class UploadResponse(BaseModel):
    """
    Response from POST /upload.
    
    Contains the total count and per-file results so the frontend
    can display a summary of what was successfully parsed.
    """

    # How many files were in the upload batch
    total_files: int

    # Per-file processing results
    filings: list[FilingMeta]


# ─────────────────────────────────────────────────────────────
# Financials Endpoint Model (GET /financials)
# ─────────────────────────────────────────────────────────────

class FinancialTableResponse(BaseModel):
    """
    Merged financial table returned by GET /financials.

    The table merges the same statement type (e.g., Balance Sheet)
    across multiple filing periods using a pandas outer join.
    
    Structure:
      columns = ["Line Item", "2023-10K", "2022-10K", ...]
      rows    = [{"Line Item": "Total Assets", "2023-10K": "100", "2022-10K": "95"}, ...]
    
    Values for periods that didn't contain a line item are null
    (outer-join semantics — unmatched items appear as gaps).
    """

    # Which financial statement this table represents
    # One of: "balance_sheet", "income_statement", "cash_flow"
    statement_type: str = Field(
        description="One of: balance_sheet, income_statement, cash_flow",
    )

    # Column headers — first is always "Line Item", rest are period keys
    columns: list[str]

    # Row data — each dict maps column name → cell value (or null)
    rows: list[dict]


# ─────────────────────────────────────────────────────────────
# Filing Text Endpoint Model (GET /filing-text)
# ─────────────────────────────────────────────────────────────

class FilingTextResponse(BaseModel):
    """
    Extracted text section returned by GET /filing-text.
    
    Contains the raw text of one section (e.g., MD&A) from one
    specific filing period. The frontend renders this in the
    Lower Pane's text viewer.
    """

    # Which filing period this text belongs to, e.g. "2023-10K"
    period: str = Field(description="e.g. '2023-10K'")

    # Section identifier, e.g. "mda", "footnotes", "supplementary"
    section: str = Field(description="e.g. 'mda', 'footnotes', 'supplementary'")

    # Human-readable section title for display
    title: str = Field(description="Human-readable section title")

    # The extracted text content. null if the section was not found
    # in the filing PDF.
    content: str | None = Field(
        None,
        description="Extracted text content. null if section was not found.",
    )


# ─────────────────────────────────────────────────────────────
# Error Model
# ─────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    """
    Standard error response body.
    
    FastAPI's HTTPException already returns {"detail": "..."} by default,
    but this model documents the shape explicitly for OpenAPI docs.
    """
    detail: str
