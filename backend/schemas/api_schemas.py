"""
schemas.api_schemas
────────────────────
Pydantic models that define the API request/response contract — the envelopes
FastAPI uses for request validation and response serialization. Each model
corresponds to one endpoint's input or output shape and composes the reusable
entities from ``domain_schemas``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .domain_schemas import (
    CompanyInfo,
    NewsArticleModel,
    VideoModel,
    ChannelModel,
    SentimentIndicatorModel,
)


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
# SEC Auto-Fetch Endpoint Models (POST /sec/fetch)
# ─────────────────────────────────────────────────────────────

class SecFetchRequest(BaseModel):
    """
    Request body for POST /sec/fetch.

    Instead of uploading a PDF, the user names the filing they want and the
    backend pulls it straight from SEC EDGAR via ``findata``, renders it to PDF,
    and runs it through the same ingestion pipeline as a manual upload.
    """

    ticker: str = Field(description="Stock ticker, e.g. 'AAPL' (case-insensitive)")
    form_type: str = Field(
        "10-K", description="SEC form type: '10-K' or '10-Q'"
    )
    year: int = Field(description="Fiscal year, e.g. 2024")
    quarter: int | None = Field(
        None,
        description="Fiscal quarter (1-3), required for a 10-Q, ignored for a 10-K",
    )

    @field_validator("ticker")
    @classmethod
    def _ticker_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("ticker must not be empty")
        return v.upper()

    @field_validator("form_type")
    @classmethod
    def _form_supported(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in ("10-K", "10-Q"):
            raise ValueError("form_type must be '10-K' or '10-Q'")
        return v


class ResolvedFiling(BaseModel):
    """Provenance of the filing that SEC auto-fetch actually retrieved.

    Because EDGAR filing rows carry a *filing date* (not the period of report),
    this echoes back the concrete document that was matched so the user can
    confirm they got the filing they intended.
    """

    ticker: str
    form_type: str = Field(description="Form of the retrieved document")
    filing_date: str = Field(description="SEC filing date, YYYY-MM-DD")
    accession_number: str | None = None
    document_url: str = Field(description="URL of the primary document rendered")


class SecFetchResponse(BaseModel):
    """
    Response from POST /sec/fetch.

    Mirrors :class:`UploadResponse` (so the frontend can render the result with
    the same code path as a manual upload) and adds ``resolved_filing`` — what
    was actually pulled from EDGAR.
    """

    total_files: int
    filings: list[FilingMeta]
    resolved_filing: ResolvedFiling


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
# Chat Endpoint Models (POST /chat)
# ─────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    """One turn in the conversation history."""

    # "user" for the person, "assistant" (or "model") for prior AI answers
    role: str = Field(description="'user' or 'assistant'")
    content: str = Field(description="The message text")


class ChatRequest(BaseModel):
    """
    Request body for POST /chat.

    `history` carries prior turns so the assistant has conversational context;
    the backend re-assembles the filing-data context on every call, so newly
    uploaded filings are always available without resending them.
    """

    question: str = Field(description="The user's current question")
    history: list[ChatMessage] = Field(
        default_factory=list,
        description="Prior conversation turns (oldest first), excluding the current question",
    )
    agent_id: str | None = Field(
        None,
        description=(
            "Optional persona to chat with in isolation: a field agent id "
            "(sec_filings, earnings_call, company_news, youtube_analysis, "
            "macro_market, technical_analysis), 'manager', or omit/'general' for "
            "the cross-view assistant. Field agents see ONLY their own data + the "
            "debate transcript; the manager sees all reports + the transcript."
        ),
    )


class ChatResponse(BaseModel):
    """Response from POST /chat — the assistant's answer."""

    answer: str = Field(description="The assistant's Markdown answer")


# ─────────────────────────────────────────────────────────────
# Company Endpoint Models (GET /company)
# ─────────────────────────────────────────────────────────────

class CompanyResponse(BaseModel):
    """Response from GET /company — companies behind the uploaded filings."""
    primary: CompanyInfo | None = Field(
        None, description="The company with the most uploaded filings"
    )
    companies: list[CompanyInfo] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Media Endpoint Models (GET /media/*, GET /macro/*)
# ─────────────────────────────────────────────────────────────

class NewsResponse(BaseModel):
    """News feed (company or macro). `configured=False` → show a connect-key card."""
    configured: bool
    scope: str = Field(description="'company' or 'macro'")
    company: CompanyInfo | None = None
    articles: list[NewsArticleModel] = Field(default_factory=list)
    message: str | None = None


class VideoResponse(BaseModel):
    configured: bool
    scope: str = Field(description="'company' or 'macro'")
    videos: list[VideoModel] = Field(default_factory=list)
    message: str | None = None


class TranscriptResponse(BaseModel):
    available: bool
    video_id: str
    text: str = ""
    language: str | None = Field(
        None, description="Language code of the returned transcript (e.g. 'en', 'ko')"
    )
    summary: str | None = Field(
        None, description="Optional Gemini summary of the transcript"
    )
    message: str | None = None


class EarningsResponse(BaseModel):
    """
    Earnings-call transcript for a chosen fiscal quarter, sourced from
    investing.com (preferred) or Motley Fool (fallback).
    """
    configured: bool
    company: CompanyInfo | None = None
    year: int | None = None
    quarter: int | None = None
    found: bool = False
    transcript: str | None = None
    source: str | None = None       # "investing.com" | "fool.com"
    url: str | None = None
    title: str | None = None
    published: str | None = None
    message: str | None = None


# ── YouTube channel management (GET/POST/DELETE /channels) ──

class ChannelsResponse(BaseModel):
    configured: bool = True
    channels: list[ChannelModel] = Field(default_factory=list)
    message: str | None = None


class AddChannelRequest(BaseModel):
    """Add a channel by URL, @handle, UC… id, or name."""
    input: str = Field(description="Channel URL, @handle, UC id, or name")


class SentimentResponse(BaseModel):
    configured: bool
    label: str = "unknown"
    score: int | None = None
    summary: str = ""
    indicators: list[SentimentIndicatorModel] = Field(default_factory=list)
    headline_count: int = 0
    message: str | None = None


# ─────────────────────────────────────────────────────────────
# Multi-Agent Analysis Model (POST /analyze)
# ─────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    """
    Request body for POST /analyze — the user-specified analysis period.

    This single date range drives EVERY agent's data fetching: the price history
    window, which quarters' earnings transcripts are pulled, the news search
    windows, and the video publish window.

    Both fields are optional; omitting them (or posting no body at all) falls
    back to a trailing 18-month window, which is long enough for a reliable
    SMA200 and several quarters of transcripts.
    """

    start_date: str | None = Field(
        None, description="Analysis period start, YYYY-MM-DD (e.g. '2025-01-01')"
    )
    end_date: str | None = Field(
        None, description="Analysis period end, YYYY-MM-DD (e.g. '2026-06-30')"
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
