"""
schemas.api_schemas
────────────────────
Pydantic models that define the API request/response contract — the envelopes
FastAPI uses for request validation and response serialization. Each model
corresponds to one endpoint's input or output shape and composes the reusable
entities from ``domain_schemas``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

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

    # Ticker the filing was routed to (its company's isolated store). None when
    # the company couldn't be resolved (the filing lands in the 'UNKNOWN' store)
    # or when ingestion failed before routing.
    ticker: str | None = Field(
        None,
        description="Ticker of the company store this filing was routed to",
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

# The widest span (inclusive) a single /sec/fetch request may cover, to keep
# SEC EDGAR traffic and server render time bounded. A 10-Q request over the full
# span can still expand to ~15 filings (5 years × 3 quarters).
MAX_YEAR_SPAN = 5


class SecFetchRequest(BaseModel):
    """
    Request body for POST /sec/fetch.

    Instead of uploading a PDF, the user names the filing(s) they want and the
    backend pulls them straight from SEC EDGAR via ``findata``, renders each to
    PDF, and runs them through the same ingestion pipeline as a manual upload.

    The request covers a *fiscal-year range* ``[start_year, end_year]`` (up to
    ``MAX_YEAR_SPAN`` years). For a 10-K that's one annual report per year; for a
    10-Q it's every available quarter (Q1–Q3) within the range — optionally
    narrowed at the boundaries by ``start_quarter``/``end_quarter`` (e.g. from
    2023 Q3 to 2024 Q2). Quarter bounds are ignored for a 10-K.
    """

    ticker: str = Field(description="Stock ticker, e.g. 'AAPL' (case-insensitive)")
    form_type: str = Field(
        "10-K", description="SEC form type: '10-K' or '10-Q'"
    )
    start_year: int = Field(description="First fiscal year (inclusive), e.g. 2021")
    end_year: int = Field(description="Last fiscal year (inclusive), e.g. 2024")
    start_quarter: int | None = Field(
        None,
        description="First fiscal quarter (1-3) in start_year; 10-Q only. "
                    "Omit to include from Q1.",
    )
    end_quarter: int | None = Field(
        None,
        description="Last fiscal quarter (1-3) in end_year; 10-Q only. "
                    "Omit to include through Q3.",
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

    @field_validator("start_quarter", "end_quarter")
    @classmethod
    def _quarter_valid(cls, v: int | None) -> int | None:
        if v is not None and v not in (1, 2, 3):
            raise ValueError("quarter must be 1, 2, or 3 (Q4 is reported in the 10-K)")
        return v

    @model_validator(mode="after")
    def _check_range(self) -> "SecFetchRequest":
        if self.start_year > self.end_year:
            raise ValueError(
                f"start_year ({self.start_year}) must not be after "
                f"end_year ({self.end_year})."
            )
        span = self.end_year - self.start_year + 1
        if span > MAX_YEAR_SPAN:
            raise ValueError(
                f"Requested range spans {span} years; the maximum is "
                f"{MAX_YEAR_SPAN}. Narrow the range and try again."
            )
        # Within a single fiscal year, the start quarter can't come after the end.
        if (
            self.start_year == self.end_year
            and self.start_quarter is not None
            and self.end_quarter is not None
            and self.start_quarter > self.end_quarter
        ):
            raise ValueError(
                f"start_quarter (Q{self.start_quarter}) must not be after "
                f"end_quarter (Q{self.end_quarter}) within the same year."
            )
        return self


class ResolvedFiling(BaseModel):
    """Provenance of one filing that SEC auto-fetch actually retrieved.

    Because EDGAR filing rows carry a *filing date* (not the period of report),
    this echoes back the concrete document that was matched so the user can
    confirm they got the filings they intended.
    """

    ticker: str
    form_type: str = Field(description="Form of the retrieved document")
    period_label: str = Field(description="Fiscal period, e.g. 'FY2022' or 'FY2022 Q1'")
    filing_date: str = Field(description="SEC filing date, YYYY-MM-DD")
    accession_number: str | None = None
    document_url: str = Field(description="URL of the primary document rendered")


class SecFetchResponse(BaseModel):
    """
    Response from POST /sec/fetch.

    Mirrors :class:`UploadResponse` (so the frontend can render results with the
    same code path as a manual upload) and, because a request can span several
    periods, reports one :class:`FilingMeta` per attempted period plus the
    provenance of each filing successfully pulled from EDGAR.
    """

    ticker: str
    range_label: str = Field(description="Requested range, e.g. '2021–2024'")
    total_files: int = Field(description="Number of periods attempted")
    succeeded: int = Field(description="Number of periods ingested successfully")
    filings: list[FilingMeta] = Field(
        description="One entry per attempted period (success, partial, or failed)"
    )
    resolved_filings: list[ResolvedFiling] = Field(
        default_factory=list,
        description="Provenance of each filing retrieved from EDGAR",
    )


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
    ticker: str | None = Field(
        None,
        description=(
            "Company to ground the answer in. Its filings + media are the ONLY "
            "company data the assistant sees. Omit for a macro-only conversation "
            "(no company data in scope); required to chat with an agent persona."
        ),
    )
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

    ``ticker`` selects WHICH company is analyzed — each company's filings live in
    an isolated store, so the pipeline must be told which one to read.

    The date fields are optional; omitting them falls back to a trailing
    18-month window, which is long enough for a reliable SMA200 and several
    quarters of transcripts.
    """

    ticker: str = Field(
        description="Ticker of the company to analyze, e.g. 'AAPL'. Must have "
                    "filings ingested for it.",
    )
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
