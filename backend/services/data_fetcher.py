"""
services.data_fetcher
──────────────────────
Centralized, cache-first data-fetching orchestrator for the three data types
that didn't already have one: company news, price/technical data, and insider
(Form 4 / 8-K) data. SEC 10-K/10-Q filings and earnings-call transcripts
already have their own cache-first layers (``services.filing_cache`` /
``services.sec_ingest`` and ``services.transcript_cache`` /
``services.research_copilot``), so they are not duplicated here.

Both ``routers.data`` (the Data tab's "Fetch Selected" button) and the MAS
pipeline's agents (``agents.company_news_agent``,
``agents.technical_analysis_agent``) call through this one layer, so a range
fetched from either place is available to the other without a second live
call — the whole point of Phase 3 of the Unified Data Tab plan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from providers import news_provider, price_provider
from services import insider_cache, news_cache, price_cache

logger = logging.getLogger(__name__)


class NotConfigured(RuntimeError):
    """Raised when the underlying provider has no API key configured — a
    server configuration problem, distinct from a per-window fetch failure,
    so callers running several fetches concurrently can tell the two apart
    (see ``agents.company_news_agent``)."""


# =============================================================================
# Company News
# =============================================================================

async def fetch_company_news(
    company: str,
    ticker: str | None,
    start_date: str,
    end_date: str,
    *,
    max_results: int = 15,
    force: bool = False,
) -> list[news_provider.NewsArticle]:
    """
    Cache-first news fetch for one (ticker, exact window).

    Callers should pass the SAME window boundaries consistently (e.g. the
    calendar-month windows ``agents.date_windows.month_windows`` already
    splits a range into) so the Data tab and the Deep Analysis pipeline hit
    the same cache entries instead of each keeping its own copy.
    """
    if not force:
        cached = news_cache.get_news(ticker or company, start_date, end_date)
        if cached is not None:
            return [news_provider.NewsArticle(**row) for row in cached]

    result = await news_provider.search_company_news(
        company, ticker, max_results=max_results,
        days=None, start_date=start_date, end_date=end_date,
    )
    if not result.configured:
        raise NotConfigured(
            result.message or "News is not configured: set TAVILY_API_KEY on the backend."
        )
    news_cache.save_news(ticker or company, start_date, end_date, result.articles)
    return result.articles


# =============================================================================
# Price / Technical Data
# =============================================================================

def _is_recent_window(end_date: str) -> bool:
    """Whether ``end_date`` includes "today" — such a window's trailing
    indicators (current price, latest RSI, ...) can still move intraday, so it
    is never served from cache."""
    try:
        end = date.fromisoformat(end_date)
    except ValueError:
        return True  # unparsable — safest to treat as live
    return end >= datetime.now(timezone.utc).date()


async def fetch_price_data(
    ticker: str, start_date: str, end_date: str, *, force: bool = False
) -> price_provider.TechnicalData:
    """
    Cache-first price/technical data fetch.

    A window ending today (or later) is always re-fetched live — its trailing
    indicators are still moving. A window that has fully closed (end_date in
    the past) is immutable, so a cache hit is reused indefinitely.
    """
    stale_by_policy = force or _is_recent_window(end_date)
    if not stale_by_policy:
        cached = price_cache.get_price_data(ticker, start_date, end_date)
        if cached is not None:
            return price_provider.TechnicalData(**cached)

    td = await price_provider.fetch_technical_data(ticker, start_date, end_date)
    price_cache.save_price_data(ticker, start_date, end_date, td)
    return td


# =============================================================================
# Insider Data (Form 4 + 8-K)
# =============================================================================

async def fetch_insider_data(
    ticker: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    count: int = 20,
    force: bool = False,
) -> dict:
    """
    Cache-first Form 4 (insider trades) + 8-K (filing metadata) fetch.

    Returns ``{"trades": [...], "filings_8k": [...]}``. Both ``findata`` calls
    are blocking (synchronous HTTP), so they run in a worker thread.
    """
    if not force:
        trades = insider_cache.get_insider_trades(ticker)
        filings = insider_cache.get_8k_filings(ticker)
        if trades is not None and filings is not None:
            return {"trades": trades, "filings_8k": filings}

    import findata

    def _fetch_trades() -> list[dict]:
        return findata.get_insider_trades(ticker, count=count)

    def _fetch_8k() -> list[dict]:
        return findata.find_filings(
            ticker, form_type="8-K",
            date_from=start_date, date_to=end_date, count=count,
        )

    trades, filings = await asyncio.gather(
        asyncio.to_thread(_fetch_trades),
        asyncio.to_thread(_fetch_8k),
    )
    insider_cache.save_insider_trades(ticker, trades)
    insider_cache.save_8k_filings(ticker, filings)
    return {"trades": trades, "filings_8k": filings}
