"""
routers.data
─────────────
The Unified Data Tab's backend: selective, cache-first fetching across all 5
company-level raw data types, plus read-only cache inspection so the frontend
can render "cached" / "not fetched" indicators without triggering a fetch.

  POST /data/fetch          — fetch any subset of the 5 data types at once
  GET  /data/status/{t}     — per-data-type cache status for one ticker
  GET  /data/news/{t}       — cached news articles (read-only)
  GET  /data/price/{t}      — cached price/technical data (read-only)
  GET  /data/insider/{t}    — cached Form 4 + 8-K data (read-only)

SEC 10-K/10-Q filings and earnings-call transcripts already have their own
cache-first layers (``services.sec_ingest`` / ``services.filing_cache`` and
``services.transcript_cache``) built for the existing upload/chat flows — this
router drives them, rather than re-implementing caching for them. News, price,
and insider data go through the new caches added alongside this router
(``services.news_cache``, ``services.price_cache``, ``services.insider_cache``,
fronted by ``services.data_fetcher``).
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from agents import date_range_to_quarters
from agents.date_windows import month_windows
from providers import news_provider
from schemas import (
    CachedNewsResponse,
    CachedPriceResponse,
    DataFetchRequest,
    DataFetchResponse,
    DataFetchResult,
    DataStatusResponse,
    DataTypeStatus,
    InsiderDataResponse,
)
from services import (
    company_service,
    data_fetcher,
    filing_cache,
    insider_cache,
    news_cache,
    price_cache,
    sec_fetch,
    sec_ingest,
    transcript_cache,
)
from services.storage import DocumentStore, get_document_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])


# =============================================================================
# Helpers
# =============================================================================

def _company_label(store: DocumentStore, ticker: str) -> str:
    """
    Best-effort human-readable company name for search queries.

    Data types that need a company name (news, earnings) must not be blocked
    on SEC filings having been fetched first — each checkbox in the Fetch
    Control Panel is independently selectable — so this degrades to the bare
    ticker when no identity has been resolved yet.
    """
    if not store.has_company(ticker):
        filing_cache.rehydrate_company_store(ticker, store)
    if not store.has_company(ticker):
        return ticker
    primary = company_service.primary_company(store.get_company_store(ticker))
    return (primary.name if primary and primary.name else None) or ticker


def _year_range(start_date: str, end_date: str) -> tuple[int, int]:
    return date.fromisoformat(start_date).year, date.fromisoformat(end_date).year


# =============================================================================
# Per-data-type fetch handlers — each returns a DataFetchResult
# =============================================================================

async def _fetch_sec_10k_10q(
    ticker: str, start_date: str, end_date: str, store: DocumentStore
) -> DataFetchResult:
    start_year, end_year = _year_range(start_date, end_date)
    succeeded, failed_msgs = 0, []
    for form_type in ("10-K", "10-Q"):
        try:
            result = await sec_ingest.fetch_and_ingest_range(
                ticker=ticker, form_type=form_type,
                start_year=start_year, end_year=end_year,
                start_quarter=None, end_quarter=None, store=store,
            )
            succeeded += result.succeeded
        except sec_fetch.SecFetchError as e:
            failed_msgs.append(f"{form_type}: {e}")

    if succeeded == 0 and failed_msgs:
        return DataFetchResult(status="error", message="; ".join(failed_msgs), count=0)
    message = "; ".join(failed_msgs) if failed_msgs else None
    return DataFetchResult(
        status="ok", count=succeeded,
        message=message or f"Ingested {succeeded} filing(s) for {start_year}-{end_year}.",
    )


async def _fetch_sec_other(
    ticker: str, start_date: str, end_date: str, force: bool
) -> DataFetchResult:
    try:
        data = await data_fetcher.fetch_insider_data(
            ticker, start_date=start_date, end_date=end_date, force=force,
        )
    except Exception as e:  # noqa: BLE001 — surface as a per-type error, not a 500
        return DataFetchResult(status="error", message=str(e))
    count = len(data["trades"]) + len(data["filings_8k"])
    return DataFetchResult(
        status="ok", count=count,
        message=f"{len(data['trades'])} Form 4 trade(s), {len(data['filings_8k'])} 8-K filing(s).",
    )


async def _fetch_news(
    ticker: str, start_date: str, end_date: str, store: DocumentStore, force: bool
) -> DataFetchResult:
    label = _company_label(store, ticker)
    windows = month_windows(start_date, end_date)
    total, errors = 0, []
    for w_start, w_end in windows:
        try:
            articles = await data_fetcher.fetch_company_news(
                label, ticker, w_start, w_end, force=force,
            )
            total += len(articles)
        except data_fetcher.NotConfigured as e:
            return DataFetchResult(status="error", message=str(e))
        except Exception as e:  # noqa: BLE001 — one bad window shouldn't fail the rest
            errors.append(f"{w_start}..{w_end}: {e}")
    message = "; ".join(errors) if errors else None
    return DataFetchResult(status="ok", count=total, message=message)


async def _fetch_earnings(
    ticker: str, start_date: str, end_date: str, store: DocumentStore, force: bool
) -> DataFetchResult:
    if not news_provider.tavily_api_key():
        return DataFetchResult(
            status="error",
            message="Earnings transcripts are not configured: set TAVILY_API_KEY.",
        )
    label = _company_label(store, ticker)
    quarters = date_range_to_quarters(start_date, end_date)
    found = 0
    for year, quarter in quarters:
        cached = None if force else transcript_cache.get_transcript(ticker, year, quarter)
        if cached is not None:
            if cached.found:
                found += 1
            continue
        doc = await news_provider.search_earnings_transcript(label, ticker, year, quarter)
        transcript_cache.save_transcript(ticker, year, quarter, doc)
        if doc.found:
            found += 1
    return DataFetchResult(
        status="ok", count=found,
        message=f"{found} of {len(quarters)} quarter(s) had a transcript available.",
    )


async def _fetch_price(
    ticker: str, start_date: str, end_date: str, force: bool
) -> DataFetchResult:
    try:
        await data_fetcher.fetch_price_data(ticker, start_date, end_date, force=force)
    except Exception as e:  # noqa: BLE001 — surface as a per-type error, not a 500
        return DataFetchResult(status="error", message=str(e))
    return DataFetchResult(status="ok", count=1)


# =============================================================================
# POST /data/fetch
# =============================================================================

@router.post("/fetch", response_model=DataFetchResponse)
async def fetch_data(
    req: DataFetchRequest,
    store: DocumentStore = Depends(get_document_store),
):
    """
    Fetch any subset of {sec_10k_10q, sec_other, news, earnings, price} for one
    ticker + date range in a single call, cache-first unless ``force_refresh``.

    Each requested type is fetched independently — one failing (e.g. Tavily not
    configured) does not block the others — so the response always has one
    ``DataFetchResult`` per requested type.
    """
    ticker = req.ticker.strip().upper()
    results: dict[str, DataFetchResult] = {}

    if "sec_10k_10q" in req.include:
        results["sec_10k_10q"] = await _fetch_sec_10k_10q(
            ticker, req.start_date, req.end_date, store
        )
    if "sec_other" in req.include:
        results["sec_other"] = await _fetch_sec_other(
            ticker, req.start_date, req.end_date, req.force_refresh
        )
    if "news" in req.include:
        results["news"] = await _fetch_news(
            ticker, req.start_date, req.end_date, store, req.force_refresh
        )
    if "earnings" in req.include:
        results["earnings"] = await _fetch_earnings(
            ticker, req.start_date, req.end_date, store, req.force_refresh
        )
    if "price" in req.include:
        results["price"] = await _fetch_price(
            ticker, req.start_date, req.end_date, req.force_refresh
        )

    return DataFetchResponse(ticker=ticker, results=results)


# =============================================================================
# GET /data/status/{ticker}
# =============================================================================

@router.get("/status/{ticker}", response_model=DataStatusResponse)
async def get_data_status(
    ticker: str,
    store: DocumentStore = Depends(get_document_store),
):
    """Which of the 5 data types are cached for this ticker, without fetching anything."""
    t = ticker.strip().upper()

    sec_periods = 0
    if store.has_company(t):
        sec_periods = len(store.get_company_store(t).filing_meta)
    elif filing_cache.has_cache(t):
        sec_periods = -1  # cached on disk but not loaded into this session yet

    news_ranges = news_cache.list_cached_ranges(t)
    price_ranges = price_cache.list_cached_ranges(t)
    quarters = transcript_cache.list_cached_quarters(t)

    status = {
        "sec_10k_10q": DataTypeStatus(
            cached=sec_periods != 0,
            detail=(
                f"{sec_periods} period(s) loaded" if sec_periods > 0
                else "cached on disk" if sec_periods < 0 else None
            ),
        ),
        "sec_other": DataTypeStatus(
            cached=insider_cache.has_cache(t),
            detail="Form 4 / 8-K cached" if insider_cache.has_cache(t) else None,
        ),
        "news": DataTypeStatus(
            cached=bool(news_ranges),
            detail=f"{len(news_ranges)} window(s) cached" if news_ranges else None,
        ),
        "earnings": DataTypeStatus(
            cached=bool(quarters),
            detail=f"{len(quarters)} quarter(s) cached" if quarters else None,
        ),
        "price": DataTypeStatus(
            cached=bool(price_ranges),
            detail=f"{len(price_ranges)} window(s) cached" if price_ranges else None,
        ),
    }
    return DataStatusResponse(ticker=t, status=status)


# =============================================================================
# GET /data/news/{ticker}  (read-only)
# =============================================================================

@router.get("/news/{ticker}", response_model=CachedNewsResponse)
async def get_cached_news(ticker: str):
    """Every cached news article for this ticker, merged across all cached
    windows and deduplicated by URL. Never fetches — see POST /data/fetch."""
    t = ticker.strip().upper()
    ranges = news_cache.list_cached_ranges(t)
    seen: set[str] = set()
    merged = []
    for start, end in ranges:
        for row in news_cache.get_news(t, start, end) or []:
            url = row.get("url")
            if url and url not in seen:
                seen.add(url)
                merged.append(row)
    merged.sort(key=lambda r: r.get("published") or "", reverse=True)
    return CachedNewsResponse(
        ticker=t, articles=merged, ranges=[[s, e] for s, e in ranges]
    )


# =============================================================================
# GET /data/price/{ticker}  (read-only)
# =============================================================================

@router.get("/price/{ticker}", response_model=CachedPriceResponse)
async def get_cached_price(ticker: str):
    """The most recently cached (by end_date) price/technical data for this
    ticker. Never fetches — see POST /data/fetch."""
    t = ticker.strip().upper()
    ranges = price_cache.list_cached_ranges(t)
    if not ranges:
        return CachedPriceResponse(ticker=t, data=None, ranges=[])
    latest_start, latest_end = ranges[-1]
    data = price_cache.get_price_data(t, latest_start, latest_end)
    return CachedPriceResponse(
        ticker=t, data=data, ranges=[[s, e] for s, e in ranges]
    )


# =============================================================================
# GET /data/insider/{ticker}  (read-only)
# =============================================================================

@router.get("/insider/{ticker}", response_model=InsiderDataResponse)
async def get_cached_insider(ticker: str):
    """Cached Form 4 trades + 8-K filings for this ticker. Never fetches —
    see POST /data/fetch."""
    t = ticker.strip().upper()
    trades = insider_cache.get_insider_trades(t) or []
    filings = insider_cache.get_8k_filings(t) or []
    return InsiderDataResponse(ticker=t, trades=trades, filings_8k=filings)
