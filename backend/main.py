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
from datetime import datetime, timedelta, timezone

# Load backend/.env (if present) so API keys — GEMINI_API_KEY, TAVILY_API_KEY,
# YOUTUBE_API_KEY, etc. — can live in a file instead of shell exports. Optional:
# if python-dotenv isn't installed, we silently fall back to the real env.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

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
    CompanyInfo,
    CompanyResponse,
    NewsArticleModel,
    NewsResponse,
    VideoModel,
    VideoResponse,
    TranscriptResponse,
    EarningsResponse,
    SentimentIndicatorModel,
    SentimentResponse,
    ChannelModel,
    ChannelsResponse,
    AddChannelRequest,
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
from edgar_xbrl import resolve_company_identity
from gemini_chat import (
    build_context,
    ask_gemini,
    gemini_api_key,
    gemini_generate,
)
import news_provider
import youtube_provider
import channel_store
from market_sentiment import compute_market_sentiment


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

# Caches the most recent media/macro data fetched by Views 2 & 3, so the AI
# assistant's context (build_context) can reference it — this is what makes the
# assistant "see" all views. Cleared on restart, like every other store.
# Keys: "company_news", "company_videos", "macro_news", "macro_videos",
#       "sentiment"; plus "transcripts" (video_id → {title, text/summary}).
_media_cache: dict = {"transcripts": {}}


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
        # Also resolve the company name/ticker (from data already fetched) so
        # the Company Media view (View 2) knows which company to look up.
        entity_name: str | None = None
        ticker: str | None = None
        if detected_cik is not None:
            try:
                entity_name, ticker = await resolve_company_identity(detected_cik)
            except Exception as e:
                logger.warning(f"Company identity resolution failed: {e}")

        _filing_meta[period_key] = {
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
# Company / Media / Macro — helpers
# =============================================================================

def _derive_companies() -> CompanyResponse:
    """Group uploaded filings by CIK to identify the company/companies."""
    groups: dict[int, dict] = {}
    for meta in _filing_meta.values():
        cik = meta.get("cik")
        if cik is None:
            continue
        g = groups.setdefault(cik, {"name": None, "ticker": None, "count": 0})
        g["count"] += 1
        if meta.get("entity_name"):
            g["name"] = meta["entity_name"]
        if meta.get("ticker"):
            g["ticker"] = meta["ticker"]

    companies = [
        CompanyInfo(cik=cik, name=g["name"], ticker=g["ticker"], filing_count=g["count"])
        for cik, g in groups.items()
    ]
    companies.sort(key=lambda c: c.filing_count, reverse=True)
    return CompanyResponse(
        primary=companies[0] if companies else None,
        companies=companies,
    )


def _primary_company() -> CompanyInfo | None:
    return _derive_companies().primary


def _news_range_kwargs(
    days: int | None, start: str | None, end: str | None
) -> dict:
    """
    Normalize a range selection into news-provider kwargs.

    A custom window (start/end, YYYY-MM-DD) takes precedence over the relative
    `days` look-back used by the preset ranges (1D/1W/1M/3M/6M/1Y).
    """
    if start or end:
        return {"days": None, "start_date": start, "end_date": end}
    return {"days": days, "start_date": None, "end_date": None}


def _video_time_bounds(
    days: int | None, start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """Translate a range selection into YouTube publishedAfter/Before (RFC-3339)."""
    if start or end:
        after = f"{start}T00:00:00Z" if start else None
        before = f"{end}T23:59:59Z" if end else None
        return after, before
    if days:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return after, None
    return None, None


async def _videos_from_channels(
    query: str,
    channel_ids: list[str],
    after: str | None,
    before: str | None,
    per_channel: int = 25,
):
    """
    Merge videos across several saved channels (newest first).

    Returns a youtube_provider.VideoResult; surfaces a not-configured result
    immediately if the API key is missing.
    """
    combined = youtube_provider.VideoResult(configured=True, videos=[])
    for cid in channel_ids[:6]:
        r = await youtube_provider.search_videos(
            query, channel_id=cid, max_results=per_channel,
            published_after=after, published_before=before,
        )
        if not r.configured:
            return r
        combined.videos.extend(r.videos)
    combined.videos.sort(key=lambda v: v.published or "", reverse=True)
    return combined


def _news_models(articles) -> list[NewsArticleModel]:
    return [
        NewsArticleModel(
            title=a.title, url=a.url, source=a.source,
            snippet=a.snippet, published=a.published,
        )
        for a in articles
    ]


def _video_models(videos) -> list[VideoModel]:
    return [
        VideoModel(
            video_id=v.video_id, title=v.title, channel=v.channel,
            url=v.url, embed_url=v.embed_url, thumbnail=v.thumbnail,
            published=v.published, description=v.description,
        )
        for v in videos
    ]


def _build_media_context() -> str:
    """
    Turn the cached media/macro data (from Views 2 & 3) into a Markdown block
    for the AI assistant, so it can answer across all views. Only includes what
    the user has actually fetched this session.
    """
    parts: list[str] = []

    cn: NewsResponse | None = _media_cache.get("company_news")
    if cn and cn.articles:
        parts.append("# Company News (recent, from web search)")
        for a in cn.articles[:10]:
            parts.append(f"- [{a.source}] {a.title} — {a.snippet[:200]}")
        parts.append("")

    mn: NewsResponse | None = _media_cache.get("macro_news")
    if mn and mn.articles:
        parts.append("# Macro / Market News (recent)")
        for a in mn.articles[:10]:
            parts.append(f"- [{a.source}] {a.title} — {a.snippet[:200]}")
        parts.append("")

    sent: SentimentResponse | None = _media_cache.get("sentiment")
    if sent and sent.configured and sent.summary:
        parts.append(
            f"# Market Sentiment: {sent.label} "
            f"({sent.score if sent.score is not None else 'n/a'}/100)"
        )
        parts.append(sent.summary)
        for ind in sent.indicators:
            parts.append(f"- {ind.theme} ({ind.direction}): {ind.note}")
        parts.append("")

    for scope_key in ("company_videos", "macro_videos"):
        vr: VideoResponse | None = _media_cache.get(scope_key)
        if vr and vr.videos:
            label = "Company" if scope_key == "company_videos" else "Macro"
            parts.append(f"# {label} Analysis Videos")
            for v in vr.videos[:8]:
                parts.append(f"- {v.title} — {v.channel}")
            parts.append("")

    transcripts: dict = _media_cache.get("transcripts", {})
    if transcripts:
        parts.append("# Video Transcript Excerpts")
        for vid, info in list(transcripts.items())[:5]:
            excerpt = info.get("text", "")[:400]
            if excerpt:
                parts.append(f"- ({vid}) {excerpt}")
        parts.append("")

    return "\n".join(parts)


# =============================================================================
# ENDPOINT: GET /company
# =============================================================================

@app.get("/company", response_model=CompanyResponse)
async def get_company():
    """Return the company/companies derived from the uploaded filings."""
    return _derive_companies()


# =============================================================================
# ENDPOINT: GET /media/news  (company-specific)
# =============================================================================

@app.get("/media/news", response_model=NewsResponse)
async def media_news(
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(30, ge=1, le=30, description="Max articles (<=30)"),
):
    """Recent news for the uploaded company (Tavily, finance domains)."""
    primary = _primary_company()
    if primary is None or not (primary.name or primary.ticker):
        return NewsResponse(
            configured=bool(news_provider.tavily_api_key()),
            scope="company",
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )
    result = await news_provider.search_company_news(
        primary.name or primary.ticker or "", primary.ticker,
        max_results=max_results, **_news_range_kwargs(days, start, end),
    )
    resp = NewsResponse(
        configured=result.configured, scope="company", company=primary,
        articles=_news_models(result.articles), message=result.message,
    )
    _media_cache["company_news"] = resp
    return resp


# =============================================================================
# ENDPOINT: GET /media/videos  (company-specific)
# =============================================================================

@app.get("/media/videos", response_model=VideoResponse)
async def media_videos(
    channel_id: str | None = Query(None, description="Saved channel id, or omit/'all'"),
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(25, ge=1, le=50, description="Max videos"),
):
    """
    YouTube analysis videos for the uploaded company.

    - `channel_id` set → videos from that channel matching the company.
    - omitted / "all" → merged across saved channels (company-filtered); if no
      channels are saved, falls back to a keyword search by company name.
    """
    primary = _primary_company()
    if primary is None or not (primary.name or primary.ticker):
        return VideoResponse(
            configured=bool(youtube_provider.youtube_api_key()),
            scope="company",
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )
    label = primary.name or primary.ticker or ""
    after, before = _video_time_bounds(days, start, end)

    if channel_id and channel_id != "all":
        result = await youtube_provider.search_videos(
            label, channel_id=channel_id, max_results=max_results,
            published_after=after, published_before=before,
        )
    else:
        saved = channel_store.channel_ids("company")
        if saved:
            result = await _videos_from_channels(label, saved, after, before)
        else:
            result = await youtube_provider.search_company_videos(
                label, primary.ticker, max_results=max_results,
                published_after=after, published_before=before,
            )

    resp = VideoResponse(
        configured=result.configured, scope="company",
        videos=_video_models(result.videos), message=result.message,
    )
    _media_cache["company_videos"] = resp
    return resp


# =============================================================================
# ENDPOINT: GET /media/transcript
# =============================================================================

@app.get("/media/transcript", response_model=TranscriptResponse)
async def media_transcript(
    video_id: str = Query(..., description="YouTube video id"),
):
    """Fetch a video's full transcript (no key needed; captions permitting)."""
    tr = youtube_provider.get_transcript(video_id)
    if not tr.available:
        return TranscriptResponse(
            available=False, video_id=video_id, message=tr.message
        )

    # Cache a slice of the transcript text so the AI assistant can reference it.
    _media_cache["transcripts"][video_id] = {"text": tr.text[:4000]}
    return TranscriptResponse(
        available=True, video_id=video_id, text=tr.text,
        language=tr.language, summary=None,
    )


# =============================================================================
# ENDPOINTS: /channels  (curated YouTube channel list)
# =============================================================================

def _channel_models(scope: str) -> list[ChannelModel]:
    return [
        ChannelModel(
            channel_id=c["channel_id"], title=c.get("title", c["channel_id"]),
            handle=c.get("handle"),
        )
        for c in channel_store.list_channels(scope)
    ]


def _valid_scope(scope: str) -> str:
    if scope not in channel_store.SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope '{scope}'. Must be one of: {list(channel_store.SCOPES)}",
        )
    return scope


@app.get("/channels", response_model=ChannelsResponse)
async def get_channels(
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """List the user's saved YouTube channels for a scope."""
    scope = _valid_scope(scope)
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


@app.post("/channels", response_model=ChannelsResponse)
async def add_channel(
    request: AddChannelRequest,
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """
    Add a channel (to the given scope) by URL, @handle, UC… id, or name.
    Resolves it to a channel_id + title via the YouTube API (an explicit UC id
    works even without a key), then persists that scope's list.
    """
    scope = _valid_scope(scope)
    raw = request.input.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Channel input cannot be empty.")

    result = await youtube_provider.resolve_channel(raw)
    if not result.ok or result.channel is None:
        raise HTTPException(
            status_code=422,
            detail=result.message or "Could not resolve that channel.",
        )

    ch = result.channel
    channel_store.add_channel(
        scope, {"channel_id": ch.channel_id, "title": ch.title, "handle": ch.handle}
    )
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


@app.delete("/channels/{channel_id}", response_model=ChannelsResponse)
async def delete_channel(
    channel_id: str,
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """Remove a saved channel from a scope."""
    scope = _valid_scope(scope)
    channel_store.remove_channel(scope, channel_id)
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


@app.patch("/channels/{channel_id}", response_model=ChannelsResponse)
async def rename_channel(
    channel_id: str,
    request: AddChannelRequest,
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """Rename a saved channel's display title (reuses the `input` field)."""
    scope = _valid_scope(scope)
    title = request.input.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    channel_store.rename_channel(scope, channel_id, title)
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


# =============================================================================
# ENDPOINT: GET /media/earnings  (best-effort)
# =============================================================================

@app.get("/media/earnings", response_model=EarningsResponse)
async def media_earnings(
    year: int = Query(..., description="Calendar/fiscal year, e.g. 2026"),
    quarter: int = Query(..., ge=1, le=4, description="Quarter 1-4"),
):
    """
    Best-effort earnings material for a chosen quarter (e.g. 2026 Q1):
    earnings-call videos (YouTube) plus a Gemini summary of earnings news
    (Tavily). No dedicated earnings-transcript provider is wired in.
    """
    primary = _primary_company()
    if primary is None or not (primary.name or primary.ticker):
        return EarningsResponse(
            configured=bool(
                youtube_provider.youtube_api_key() or news_provider.tavily_api_key()
            ),
            year=year, quarter=quarter,
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )

    label = primary.name or primary.ticker or ""
    term = f"{label} Q{quarter} {year} earnings call"

    vids = await youtube_provider.search_videos(term, max_results=3)
    videos = _video_models(vids.videos) if vids.configured else []

    news = await news_provider.search_company_news(
        f"{label} Q{quarter} {year} earnings results", primary.ticker,
        max_results=6, days=None, start_date=None, end_date=None,
    )
    summary: str | None = None
    if news.configured and news.articles and gemini_api_key():
        payload = "\n".join(
            f"- {a.title}: {a.snippet[:200]}" for a in news.articles
        )
        try:
            summary = await gemini_generate(
                f"Summarize {label}'s Q{quarter} {year} earnings highlights from "
                f"these news headlines in 3-5 concise bullets.",
                payload,
            )
        except RuntimeError:
            summary = None

    configured = vids.configured or news.configured
    msg = None if configured else (
        "Set YOUTUBE_API_KEY and/or TAVILY_API_KEY to see earnings material."
    )
    return EarningsResponse(
        configured=configured, company=primary, year=year, quarter=quarter,
        videos=videos, summary=summary,
        articles=_news_models(news.articles), message=msg,
    )


# =============================================================================
# ENDPOINT: GET /macro/news
# =============================================================================

@app.get("/macro/news", response_model=NewsResponse)
async def macro_news(
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(30, ge=1, le=30, description="Max articles (<=30)"),
):
    """Aggregated macro/market news (Tavily, finance domains)."""
    result = await news_provider.search_macro_news(
        max_results=max_results, **_news_range_kwargs(days, start, end)
    )
    resp = NewsResponse(
        configured=result.configured, scope="macro",
        articles=_news_models(result.articles), message=result.message,
    )
    _media_cache["macro_news"] = resp
    return resp


# =============================================================================
# ENDPOINT: GET /macro/videos
# =============================================================================

@app.get("/macro/videos", response_model=VideoResponse)
async def macro_videos(
    channel_id: str | None = Query(None, description="Saved channel id, or omit/'all'"),
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(25, ge=1, le=50, description="Max videos"),
):
    """
    Macro/economic YouTube videos.

    - `channel_id` set → that channel's newest videos.
    - omitted / "all" → merged newest across saved channels; if none saved,
      falls back to a broad macro keyword search.
    """
    after, before = _video_time_bounds(days, start, end)

    if channel_id and channel_id != "all":
        result = await youtube_provider.search_videos(
            channel_id=channel_id, max_results=max_results,
            published_after=after, published_before=before,
        )
    else:
        saved = channel_store.channel_ids("macro")
        if saved:
            # Empty query → browse each channel's latest uploads.
            result = await _videos_from_channels("", saved, after, before)
        else:
            result = await youtube_provider.search_macro_videos(
                max_results=max_results, published_after=after, published_before=before
            )

    resp = VideoResponse(
        configured=result.configured, scope="macro",
        videos=_video_models(result.videos), message=result.message,
    )
    _media_cache["macro_videos"] = resp
    return resp


# =============================================================================
# ENDPOINT: GET /macro/sentiment
# =============================================================================

@app.get("/macro/sentiment", response_model=SentimentResponse)
async def macro_sentiment():
    """Gemini-synthesized market sentiment from aggregated macro headlines."""
    r = await compute_market_sentiment()
    resp = SentimentResponse(
        configured=r.configured, label=r.label, score=r.score, summary=r.summary,
        indicators=[
            SentimentIndicatorModel(theme=i.theme, direction=i.direction, note=i.note)
            for i in r.indicators
        ],
        headline_count=r.headline_count, message=r.message,
    )
    _media_cache["sentiment"] = resp
    return resp


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

    # Assemble the grounding context from the current in-memory data, including
    # any media/macro data the user has fetched (so the AI sees all views).
    media_context = _build_media_context()
    context = build_context(
        _merged_tables, _text_store, _filing_meta,
        extra_context=media_context,
    )

    # Short-circuit only when there's truly nothing to talk about — no filings
    # AND no media/macro data has been fetched (the Macro view needs no upload).
    if not _filing_meta and not media_context.strip():
        return ChatResponse(
            answer="No data yet. Upload SEC 10-K / 10-Q PDFs on the Dashboard, "
                   "or open the Company Media / Macro Sentiment views to pull in "
                   "news and market data — then ask me about any of it.",
        )

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
