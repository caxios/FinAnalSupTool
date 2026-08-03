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


class ChatResponse(BaseModel):
    """Response from POST /chat — the assistant's answer."""

    answer: str = Field(description="The assistant's Markdown answer")


# ─────────────────────────────────────────────────────────────
# Company Endpoint Models (GET /company)
# ─────────────────────────────────────────────────────────────

class CompanyInfo(BaseModel):
    """A company derived from the uploaded filings."""
    cik: int | None = None
    name: str | None = None
    ticker: str | None = None
    filing_count: int = 0


class CompanyResponse(BaseModel):
    """Response from GET /company — companies behind the uploaded filings."""
    primary: CompanyInfo | None = Field(
        None, description="The company with the most uploaded filings"
    )
    companies: list[CompanyInfo] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Media Endpoint Models (GET /media/*, GET /macro/*)
# ─────────────────────────────────────────────────────────────

class NewsArticleModel(BaseModel):
    title: str
    url: str
    source: str
    snippet: str = ""
    published: str | None = None


class NewsResponse(BaseModel):
    """News feed (company or macro). `configured=False` → show a connect-key card."""
    configured: bool
    scope: str = Field(description="'company' or 'macro'")
    company: CompanyInfo | None = None
    articles: list[NewsArticleModel] = Field(default_factory=list)
    message: str | None = None


class VideoModel(BaseModel):
    video_id: str
    title: str
    channel: str
    url: str
    embed_url: str
    thumbnail: str | None = None
    published: str | None = None
    description: str = ""


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

class ChannelModel(BaseModel):
    channel_id: str
    title: str
    handle: str | None = None


class ChannelsResponse(BaseModel):
    configured: bool = True
    channels: list[ChannelModel] = Field(default_factory=list)
    message: str | None = None


class AddChannelRequest(BaseModel):
    """Add a channel by URL, @handle, UC… id, or name."""
    input: str = Field(description="Channel URL, @handle, UC id, or name")


class SentimentIndicatorModel(BaseModel):
    theme: str
    direction: str  # bullish | neutral | bearish
    note: str


class SentimentResponse(BaseModel):
    configured: bool
    label: str = "unknown"
    score: int | None = None
    summary: str = ""
    indicators: list[SentimentIndicatorModel] = Field(default_factory=list)
    headline_count: int = 0
    message: str | None = None


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
