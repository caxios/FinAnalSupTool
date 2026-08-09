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

import asyncio
import json
import logging
import io
import tempfile
import shutil
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

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
    AnalyzeRequest,
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
    ask_persona,
    gemini_api_key,
    gemini_generate,
)
import news_provider
import youtube_provider
import channel_store
from market_sentiment import compute_market_sentiment
from rag import compute_three_axis_scores, history_store
from agents import (
    SECFilingsAgent,
    TechnicalAnalysisAgent,
    EarningsCallAgent,
    CompanyNewsAgent,
    MacroMarketAgent,
    YouTubeAgent,
    ManagerAgent,
    DebateTranscript,
    run_sequential_debate,
    render_transcript,
    display_name,
    FIELD_AGENT_IDS,
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

# Holds the most recent /analyze run so the role-based chat can talk to a single
# agent in isolation. Cleared/overwritten on each /analyze. Structure:
#   {
#     "reports":        {agent_id: report_dict},        # Phase-1 initial reports
#     "agent_contexts": {agent_id: {"raw_data": str, "report": dict}},
#     "transcript":     DebateTranscript | None,        # Phase-2 relay debate
#     "manager":        ManagerReport | dict | None,    # Phase-3 synthesis
#     "period": str, "company": str | None,
#   }
_debate_store: dict = {}


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
        now = datetime.now(timezone.utc)
        after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Pass an explicit upper bound too, so the window is [now-days, now].
        before = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return after, before
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
    Earnings-call transcript for a chosen quarter (e.g. 2026 Q1).

    Fetches the full transcript from investing.com first, falling back to
    Motley Fool (fool.com) if investing.com has none. Returns a graceful
    not-found / not-configured payload otherwise.
    """
    primary = _primary_company()
    if primary is None or not (primary.name or primary.ticker):
        return EarningsResponse(
            configured=bool(news_provider.tavily_api_key()),
            year=year, quarter=quarter,
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )

    doc = await news_provider.search_earnings_transcript(
        primary.name or "", primary.ticker, year, quarter
    )

    resp = EarningsResponse(
        configured=doc.configured, company=primary, year=year, quarter=quarter,
        found=doc.found, transcript=doc.text or None, source=doc.source,
        url=doc.url, title=doc.title, published=doc.published, message=doc.message,
    )
    # Cache a slice so the AI assistant can reference the latest earnings call.
    if doc.found and doc.text:
        _media_cache["transcripts"][f"earnings-{year}Q{quarter}"] = {
            "text": doc.text[:4000]
        }
    return resp


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
# Role-based chat — isolated per-agent personas (data isolation / token saver)
# =============================================================================

# Cap on a single agent's raw data injected into an isolated chat. Bounds a long
# earnings transcript (~80K chars) while leaving room for the debate + question.
_CHAT_RAW_CAP = 60_000

_FIELD_CHAT_TEMPLATE = """\
You are the {name}, one of six specialist analysts on a financial research team.
You have completed your own analysis and taken part in a round-table debate with
the other analysts. A user now wants to talk to YOU specifically.

Ground rules:
- Answer ONLY from YOUR OWN data and findings below, plus the shared debate
  transcript. You do NOT have the other analysts' raw data. If the user asks
  about something outside your domain, say it is outside your remit and point
  them to the relevant analyst or the Manager.
- Cite specifics from your data (numbers, quotes, dates). Never invent figures or
  use outside knowledge about the company's actuals.
- You may reference what other analysts argued in the debate transcript, but you
  can only speak authoritatively about your own evidence.
- Be concise and use Markdown.

=== YOUR INITIAL FINDINGS (your Phase-1 JSON report) ===
{report}
=== END FINDINGS ===

=== YOUR RAW DATA ===
{raw_data}
=== END RAW DATA ===

=== ROUND-TABLE DEBATE TRANSCRIPT (all analysts) ===
{transcript}
=== END TRANSCRIPT ==="""

_MANAGER_CHAT_TEMPLATE = """\
You are the Lead Analyst (Manager) of a financial research team. Six specialist
analysts each produced a report and then debated each other. A user wants to
discuss the overall investment picture with you.

Ground rules:
- You see every analyst's INITIAL REPORT (JSON) and the full debate transcript —
  but NOT their raw source data (no filings text, earnings transcripts, or
  headlines). Reason from the reports and the debate only; never introduce facts
  or numbers that are not present in them.
- Weigh evidence quality across domains, resolve disagreements, and give a clear
  synthesized view. Attribute claims to the analyst/domain they came from.
- Be concise and use Markdown.

=== ALL INITIAL AGENT REPORTS (JSON) ===
{reports}
=== END REPORTS ===

=== ROUND-TABLE DEBATE TRANSCRIPT ===
{transcript}
=== END TRANSCRIPT ==="""


def _agent_chat_persona(agent_id: str) -> str:
    """
    Build the ISOLATED system prompt for a single-agent chat from the last
    /analyze run. Enforces the data-isolation rules: a field agent sees only its
    own raw data + report + the debate transcript; the Manager sees all reports +
    the transcript but no raw data.

    Raises HTTPException with a helpful message when the persona can't be served
    (no analysis yet, agent didn't report, or unknown id).
    """
    if not _debate_store:
        raise HTTPException(
            status_code=409,
            detail="No analysis has been run yet. Run POST /analyze first, then "
                   "you can chat with an individual agent.",
        )

    transcript = _debate_store.get("transcript")
    rendered = render_transcript(transcript)

    if agent_id == "manager":
        reports = _debate_store.get("reports") or {}
        if not reports:
            raise HTTPException(
                status_code=409,
                detail="The last analysis produced no agent reports to synthesize.",
            )
        return _MANAGER_CHAT_TEMPLATE.format(
            reports=json.dumps(reports, ensure_ascii=False, indent=2),
            transcript=rendered,
        )

    if agent_id in FIELD_AGENT_IDS:
        ctx = (_debate_store.get("agent_contexts") or {}).get(agent_id)
        if not ctx:
            raise HTTPException(
                status_code=409,
                detail=f"The '{agent_id}' agent did not produce a report in the "
                       f"last analysis (it may have been skipped or failed), so "
                       f"there is nothing to discuss with it.",
            )
        raw = (ctx.get("raw_data") or "")[:_CHAT_RAW_CAP] or "(no raw data captured)"
        return _FIELD_CHAT_TEMPLATE.format(
            name=display_name(agent_id),
            report=json.dumps(ctx.get("report") or {}, ensure_ascii=False, indent=2),
            raw_data=raw,
            transcript=rendered,
        )

    raise HTTPException(
        status_code=400,
        detail=f"Unknown agent_id '{agent_id}'. Use one of "
               f"{sorted(FIELD_AGENT_IDS)}, 'manager', or omit it for the "
               f"general assistant.",
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

    # ── Role-based chat: talk to ONE agent (or the Manager) in isolation ──
    # When an agent_id is supplied, we scope the system prompt to just that
    # agent's data + the debate transcript (data isolation → far fewer tokens
    # than the omniscient assistant). Omit it (or 'general') for the cross-view
    # assistant below.
    agent_id = (request.agent_id or "").strip().lower()
    if agent_id and agent_id != "general":
        system_prompt = _agent_chat_persona(agent_id)  # raises if unavailable
        history = [{"role": m.role, "content": m.content} for m in request.history]
        try:
            answer = await ask_persona(question, history, system_prompt)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return ChatResponse(answer=answer)

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
# ENDPOINT: POST /analyze  (Multi-Agent System)
#   Step 1: SEC Filings   Step 2: + Technical Analysis
#   Step 3: + Earnings Call, Company News, Macro & Market, YouTube — all six
#           run concurrently over one user-specified analysis period.
# =============================================================================

# Default window when the caller doesn't specify one: a trailing ~18 months.
# ~550 calendar days yields ~380 trading days (enough for a reliable SMA200) and
# spans 6 quarters of earnings calls.
_DEFAULT_WINDOW_DAYS = 550


def _analysis_window(request: AnalyzeRequest | None = None) -> tuple[str, str]:
    """
    Resolve the analysis period that drives EVERY agent's data fetching.

    Uses the caller's start/end when given, else falls back to the trailing
    default window. Raises HTTPException(400) on a malformed or inverted range.
    """
    def parse(value: str, field: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be a YYYY-MM-DD date (got '{value}').",
            )

    today = datetime.now(timezone.utc).date()
    end = parse(request.end_date, "end_date") if (request and request.end_date) else today
    start = (
        parse(request.start_date, "start_date")
        if (request and request.start_date)
        else end - timedelta(days=_DEFAULT_WINDOW_DAYS)
    )

    if start > end:
        raise HTTPException(
            status_code=400,
            detail=f"start_date ({start}) must not be after end_date ({end}).",
        )
    return start.isoformat(), end.isoformat()


def _analyze_preconditions() -> None:
    """Guardrails shared by the sync and streaming analyze endpoints."""
    if not _filing_meta:
        raise HTTPException(status_code=404, detail="No filings uploaded yet.")
    if not gemini_api_key():
        raise HTTPException(
            status_code=503,
            detail="Analysis is not configured: GEMINI_API_KEY is not set on "
                   "the backend. Set it and restart to enable the agents.",
        )


async def _analyze_pipeline(request: AnalyzeRequest):
    """
    The full three-phase MAS pipeline, as an async GENERATOR of progress events.

    Phase 1 — Independent analysis: the six field agents run concurrently, capped
      at two Gemini calls at a time (avoids 429s). Each is isolated; a per-agent
      `agent_done` event fires as each finishes.
    Phase 2 — True sequential debate over the agents that reported.
    Phase 3 — Manager synthesis + programmatic 3-axis gap scoring, then the run
      is persisted to history.

    The last event is `{"status": "complete", "result": <full payload>}`. Both
    POST /analyze (drains to that result) and POST /analyze/stream (relays every
    event as SSE) build on this, so the pipeline lives in exactly one place.
    """
    start_date, end_date = _analysis_window(request)
    # Unique id for this run — scopes the earnings RAG index so runs don't mix.
    analysis_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]

    primary = _primary_company()
    ticker = primary.ticker if primary else None
    company_name = primary.name if primary else None
    # Every agent except the SEC one works from the company identity; the shared
    # payload keeps their contexts identical.
    company_ctx = {
        "company": company_name or "",
        "ticker": ticker,
        "start_date": start_date,
        "end_date": end_date,
        "run_id": analysis_id,
    }

    # Each runnable agent gets a fresh `capture` dict; the agent writes its
    # assembled RAW-DATA prompt there, so Phase 2 (debate) and the isolated
    # per-agent chat can reuse the primary evidence without re-fetching it.
    captures: dict[str, dict] = {}

    def _cap(agent_id: str) -> dict:
        c: dict = {}
        captures[agent_id] = c
        return c

    # (agent_id, coroutine | skip-reason). A skip reason means the agent's
    # prerequisites are missing, so we report it without spending an LLM call.
    planned: list[tuple[str, object]] = [
        ("sec_filings", SECFilingsAgent().analyze(
            {
                "merged_tables": _merged_tables,
                "text_store": _text_store,
                "filing_meta": _filing_meta,
            },
            capture=_cap("sec_filings"),
        )),
        ("technical_analysis",
         TechnicalAnalysisAgent().analyze(
             {"ticker": ticker, "start_date": start_date, "end_date": end_date},
             capture=_cap("technical_analysis"),
         )
         if ticker else
         "No ticker could be resolved from the uploaded filings; "
         "technical analysis was skipped."),
    ]

    identified = bool(company_name or ticker)
    no_company = (
        "No company could be identified from the uploaded filings; "
        "this agent was skipped."
    )
    for agent_id, agent_cls in (
        ("earnings_call", EarningsCallAgent),
        ("company_news", CompanyNewsAgent),
        ("macro_market", MacroMarketAgent),
        ("youtube_analysis", YouTubeAgent),
    ):
        planned.append((
            agent_id,
            agent_cls().analyze(dict(company_ctx), capture=_cap(agent_id))
            if identified else no_company,
        ))

    total = len(planned)
    yield {"phase": 1, "status": "running", "agents_total": total, "agents_completed": 0}

    reports: dict[str, dict] = {}          # successful reports only (feed 2 & 3)
    slots: dict[str, dict] = {}            # every agent's slot (incl. errors)
    agent_contexts: dict[str, dict] = {}   # raw_data + report, for debate & chat
    done = 0

    # Agents skipped before launch: report immediately so progress stays honest.
    runnable: list[tuple[str, object]] = []
    for agent_id, coro in planned:
        if isinstance(coro, str):
            slots[agent_id] = {"error": coro}
            done += 1
            yield {"phase": 1, "status": "agent_done", "agent": agent_id, "ok": False,
                   "skipped": True, "agents_completed": done, "agents_total": total}
        else:
            runnable.append((agent_id, coro))

    # ── Phase 1: run the rest concurrently but capped at 2 Gemini calls. ─────
    # Firing all six at once reliably trips the per-minute quota on Flash; two at
    # a time stays under it while overlapping the slow, network-bound fetching.
    sem = asyncio.Semaphore(2)

    async def _run(aid: str, coro):
        async with sem:
            try:
                return aid, await coro
            except BaseException as e:      # noqa: BLE001 — isolate every agent
                return aid, e

    tasks = [asyncio.create_task(_run(aid, coro)) for aid, coro in runnable]
    for fut in asyncio.as_completed(tasks):
        aid, res = await fut
        done += 1
        if isinstance(res, BaseException):
            logger.error(f"{aid} agent failed: {res}")
            slots[aid] = {"error": str(res)}
            ok = False
        else:
            rd = res.model_dump()
            reports[aid] = rd
            slots[aid] = rd
            agent_contexts[aid] = {
                "raw_data": captures.get(aid, {}).get("raw_data", ""), "report": rd,
            }
            ok = True
        yield {"phase": 1, "status": "agent_done", "agent": aid, "ok": ok,
               "agents_completed": done, "agents_total": total}

    # ── Phase 2: TRUE sequential debate over the agents that reported. ───────
    transcript: DebateTranscript | None = None
    if len(agent_contexts) >= 2:
        yield {"phase": 2, "status": "debating",
               "participants": sorted(agent_contexts.keys())}
        try:
            transcript = await run_sequential_debate(agent_contexts)
        except Exception as e:             # noqa: BLE001
            logger.error(f"Sequential debate failed: {e}")
            transcript = None

    # ── Phase 3: manager synthesis (reports + transcript, NEVER raw data). ───
    yield {"phase": 3, "status": "synthesizing"}
    manager_result: object = None
    if reports:
        try:
            manager_result = await ManagerAgent().analyze({
                "reports": reports,
                "transcript": transcript,
                "period": f"{start_date}..{end_date}",
                "company": company_name or ticker,
            })
        except Exception as e:             # noqa: BLE001
            logger.error(f"Manager synthesis failed: {e}")
            manager_result = {"error": str(e)}

    # Programmatic 3-axis gap scores (deterministic, from the agent reports).
    three_axis = compute_three_axis_scores(reports)
    manager_payload = (
        manager_result.model_dump()
        if hasattr(manager_result, "model_dump") else manager_result
    )

    # Persist for the role-based chat (overwrites any previous run).
    _debate_store.clear()
    _debate_store.update({
        "reports": reports,
        "agent_contexts": agent_contexts,
        "transcript": transcript,
        "manager": manager_result,
        "period": f"{start_date}..{end_date}",
        "company": company_name or ticker,
    })

    # Persist to history: disk (authoritative) + best-effort vector store.
    run_id = history_store.save_analysis(
        company=company_name, ticker=ticker,
        analysis_period=f"{start_date}..{end_date}",
        three_axis_scores=three_axis,
        manager=manager_payload if isinstance(manager_payload, dict) else None,
        reports=slots,
        debate=transcript.model_dump() if transcript else None,
    )

    result = {
        "run_id": run_id,
        "analysis_period": f"{start_date}..{end_date}",
        "company": primary.model_dump() if primary else None,
        "agents_total": total,
        "agents_completed": len(reports),
        "three_axis_scores": three_axis,
        "reports": slots,
        "debate": transcript.model_dump() if transcript else None,
        "manager": manager_payload,
    }
    yield {"phase": 3, "status": "complete", "result": result}


@app.post("/analyze")
async def run_analysis(request: AnalyzeRequest = AnalyzeRequest()):
    """
    Run the full three-phase MAS pipeline and return the final report.

    Phase 1 (six agents, rate-limited) → Phase 2 (sequential debate) → Phase 3
    (manager synthesis + 3-axis gap scoring). The run is saved to history. For
    live per-phase progress on the ~60-120s pipeline, use POST /analyze/stream.
    """
    _analyze_preconditions()
    final: dict = {}
    async for event in _analyze_pipeline(request):
        if event.get("status") == "complete":
            final = event["result"]
    return final


@app.post("/analyze/stream")
async def run_analysis_stream(request: AnalyzeRequest = AnalyzeRequest()):
    """
    Same pipeline as POST /analyze, but streamed as Server-Sent Events so the UI
    can show real-time progress: one event per agent as it finishes, then the
    debate and synthesis phases, then a final `complete` event carrying the full
    report. Each SSE line is `data: {json}`.
    """
    _analyze_preconditions()

    async def event_generator():
        try:
            async for event in _analyze_pipeline(request):
                yield f"data: {json.dumps(event)}\n\n"
        except HTTPException as e:
            yield f"data: {json.dumps({'phase': 0, 'status': 'error', 'detail': e.detail})}\n\n"
        except Exception as e:             # noqa: BLE001
            logger.error(f"Streaming analysis failed: {e}")
            yield f"data: {json.dumps({'phase': 0, 'status': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering so events flush
            "Connection": "keep-alive",
        },
    )


# =============================================================================
# ENDPOINTS: Analysis history  (persisted past runs)
# =============================================================================

@app.get("/analysis/history")
async def analysis_history(
    ticker: str = Query(..., description="Ticker symbol, e.g. 'AAPL'"),
    limit: int = Query(10, ge=1, le=50),
):
    """Lightweight summaries of past analysis runs for a ticker, newest first."""
    return {
        "ticker": ticker,
        "history": history_store.get_analysis_history(ticker, limit=limit),
    }


@app.get("/analysis/tickers")
async def analysis_tickers():
    """Distinct tickers that have stored runs, with run counts (for the sidebar)."""
    return {"tickers": history_store.list_tickers()}


@app.get("/analysis/{run_id}")
async def get_analysis(run_id: str):
    """Full stored record for a specific past run."""
    record = history_store.get_analysis(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No analysis run '{run_id}' found.")
    return record


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
