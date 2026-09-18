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
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timezone

from providers import entity_tagging, news_dedup, news_provider, price_provider
from rag import vector_store
from services import data_lake, insider_cache, news_cache, price_cache, registry_db, search_index

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
    scope = ticker or company
    if not force:
        cached = news_cache.get_news(scope, start_date, end_date)
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

    kept = await _dedup_and_index_articles(result.articles, scope, ticker)
    news_cache.save_news(scope, start_date, end_date, kept)
    return kept


async def _dedup_and_index_articles(
    articles: list[news_provider.NewsArticle], scope: str, ticker: str | None,
) -> list[news_provider.NewsArticle]:
    """
    SimHash-dedup this batch against everything already registered for
    `scope` (services.registry_db.news_dedup — persists across ALL past
    fetches for this ticker, not just this one window), then archive + index
    (Chroma + FTS5) every genuinely new article. A near-duplicate is still
    registered (pointing at its canonical article) so a LATER re-fetch of the
    same syndicated story also resolves to that same canonical id, but is
    neither kept in the returned list nor archived/indexed.

    Best-effort per article: a tagging/indexing failure is logged and that
    one article is still kept in the result (its dedup registration already
    succeeded) — a slow/broken enrichment step must never cause an article
    to silently vanish from the news feed.
    """
    kept: list[news_provider.NewsArticle] = []
    for a in articles:
        text = f"{a.title} {a.snippet}"
        h = news_dedup.simhash(text)
        article_id = f"{scope}_{hashlib.sha1(a.url.encode()).hexdigest()[:10]}"
        canonical = registry_db.find_near_duplicate(h, scope)
        if canonical is not None:
            registry_db.register_article(article_id, h, canonical, a.url, scope)
            continue

        registry_db.register_article(article_id, h, article_id, a.url, scope)
        kept.append(a)

        try:
            _, mentioned = await entity_tagging.tag_tickers(text, ticker or "")
            meta = {
                "ticker": ticker, "doc_type": "news_article", "period": a.published,
                "published_at": a.published, "mentioned_tickers": ",".join(mentioned),
                "url": a.url, "source": a.source,
            }
            # vector_store.index_chunks() always appends "-{i}" to id_prefix
            # (here always "-0", one chunk per article) — reuse that EXACT
            # id for FTS5 too, rather than inventing a separate "_news_000"
            # suffix, so the two stores share one doc_id per chunk (the join
            # key rag/hybrid_search.py's RRF fusion depends on). A mismatch
            # here silently breaks fusion for every article — caught live via
            # Phase 3's own verification, the same bug already fixed once for
            # research_copilot.index_earnings_transcript in Phase 2.
            id_prefix = f"{article_id}_news"
            doc_id = f"{id_prefix}-0"
            await vector_store.index_chunks(
                "news_articles", [{"text": text, "metadata": meta}], id_prefix=id_prefix,
            )
            search_index.index_chunk(doc_id, text, scope, "news_article", meta)
            data_lake.save_raw(scope, "news_article", article_id, "json", json.dumps(asdict(a), default=str))
        except Exception as e:  # noqa: BLE001 — enrichment only, never drops the article
            logger.warning(f"[data_fetcher] news indexing failed for {a.url}: {e}")

    return kept


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

# Each Form 4 filing parsed is one rate-limited SEC request, so the scan is
# bounded: ~40 filings per month of window, capped. A mega-cap like GOOGL files
# several Form 4s a week, so the cap is what actually binds for long windows —
# the result is "as far back as we could reasonably scan", not silently "the
# last two weeks" as before.
_FORM4_FILINGS_PER_MONTH = 40
_MAX_FORM4_FILINGS = 240
_MAX_8K_FILINGS = 100


def _form4_scan_count(start_date: str | None, end_date: str | None, minimum: int) -> int:
    """How many recent Form 4 filings to parse to cover the requested window."""
    if not start_date or not end_date:
        return minimum
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return minimum
    months = max(1, round((end - start).days / 30))
    return max(minimum, min(_MAX_FORM4_FILINGS, months * _FORM4_FILINGS_PER_MONTH))


def _is_transaction_row(t: dict) -> bool:
    """
    Whether a Form 4 row is an actual TRANSACTION.

    A Form 4 also carries "holding" rows (Table I/II lines that report a
    position with no trade): no transaction date, no code, no amount — only
    shares_owned_after. They're legitimate filing content but meaningless in a
    trades table, where they rendered as rows of dashes (~40% of GOOGL's rows).
    """
    return bool((t.get("transaction_date") or "").strip()) or bool(
        (t.get("transaction_code") or "").strip()
    )


def _trades_in_range(
    trades: list[dict], start_date: str | None, end_date: str | None
) -> list[dict]:
    """Keep only trades whose transaction_date falls inside the window. Rows
    with an unparseable/missing date are kept — dropping a real trade because
    its date is malformed is worse than showing it."""
    if not start_date and not end_date:
        return trades
    out = []
    for t in trades:
        d = (t.get("transaction_date") or "").strip()[:10]
        if not d:
            out.append(t)
            continue
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        out.append(t)
    return out


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
            return {
                "trades": _trades_in_range(trades, start_date, end_date),
                "filings_8k": filings,
            }

    import findata

    # findata.get_insider_trades takes a FILING COUNT, not a date range — its
    # `count` is "how many recent Form 4 filings to parse". Left at the default
    # it returned only the last couple of weeks no matter which period the user
    # picked, so a year-long window silently showed a fortnight of grants.
    # Scale the filing count to the requested window (then filter by
    # transaction date below, since the count is still only an approximation).
    filings_to_scan = _form4_scan_count(start_date, end_date, count)

    def _fetch_trades() -> list[dict]:
        return findata.get_insider_trades(ticker, count=filings_to_scan)

    def _fetch_8k() -> list[dict]:
        return findata.find_filings(
            ticker, form_type="8-K",
            date_from=start_date, date_to=end_date, count=_MAX_8K_FILINGS,
        )

    trades, filings = await asyncio.gather(
        asyncio.to_thread(_fetch_trades),
        asyncio.to_thread(_fetch_8k),
    )
    # Cache every TRANSACTION row regardless of window, so a later, narrower
    # window can still be served from this fetch; the range filter is applied
    # on the way out instead. Holding rows are dropped here, not cached.
    trades = [t for t in trades if _is_transaction_row(t)]
    insider_cache.save_insider_trades(ticker, trades)
    insider_cache.save_8k_filings(ticker, filings)
    return {
        "trades": _trades_in_range(trades, start_date, end_date),
        "filings_8k": filings,
    }
