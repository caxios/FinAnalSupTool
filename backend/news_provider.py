"""
news_provider.py
────────────────
Financial news retrieval via the Tavily Search API.

Tavily (https://tavily.com) is an LLM-oriented web-search API. We use its
`/search` endpoint with `include_domains` to prioritize high-quality finance
sources when pulling news for a company (View 2) or the broader market (View 3).

Configuration
─────────────
  TAVILY_API_KEY  (required for live data)

Graceful degradation
─────────────────────
When the key is missing, every function returns a `NewsResult` with
`configured=False` and a helpful message — the frontend renders this as a
"connect a key" card instead of failing. This mirrors the Gemini panel.
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

_TAVILY_URL = "https://api.tavily.com/search"
_HTTP_TIMEOUT = 30.0

# High-signal finance domains to prioritize (per user preference).
FINANCE_DOMAINS = [
    "investing.com",
    "finance.yahoo.com",
    "cnbc.com",
    "seekingalpha.com",
]


def tavily_api_key() -> str | None:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    return key or None


# =============================================================================
# Result types
# =============================================================================

@dataclass
class NewsArticle:
    title: str
    url: str
    source: str
    snippet: str
    published: str | None = None
    score: float | None = None


@dataclass
class NewsResult:
    """A news query outcome, including a not-configured / error state."""
    configured: bool
    articles: list[NewsArticle] = field(default_factory=list)
    message: str | None = None
    query: str | None = None


@dataclass
class TranscriptDoc:
    """An earnings-call transcript located on a source site."""
    configured: bool
    found: bool = False
    source: str | None = None       # e.g. "investing.com" / "fool.com"
    url: str | None = None
    title: str | None = None
    text: str = ""
    published: str | None = None
    message: str | None = None


# =============================================================================
# Internal
# =============================================================================

def _source_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


async def _tavily_search(
    query: str,
    *,
    max_results: int = 30,
    include_domains: list[str] | None = None,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> NewsResult:
    """
    Run one Tavily search and normalize the response to NewsArticles.

    Date filtering:
      - `days`: look back this many days from now (used for the preset ranges).
      - `start_date` / `end_date` (YYYY-MM-DD): an explicit custom window; when
        given, they take precedence over `days`.
    """
    api_key = tavily_api_key()
    if not api_key:
        return NewsResult(
            configured=False,
            query=query,
            message="News is not configured: set TAVILY_API_KEY on the backend "
                    "to enable live financial news.",
        )

    body: dict = {
        "api_key": api_key,
        "query": query,
        # Tavily may cap this on some plans; we pass the requested count through.
        "max_results": max_results,
        "search_depth": "basic",
        "topic": "news",
    }
    if include_domains:
        body["include_domains"] = include_domains
    # Enforce an explicit [start_date, end_date] window in every case so Tavily
    # returns results spread across the whole range rather than clustering on the
    # last day or two. A custom window wins; otherwise derive it from `days`.
    if not (start_date or end_date) and days:
        today = datetime.now(timezone.utc).date()
        start_date = (today - timedelta(days=days)).isoformat()
        end_date = today.isoformat()
    if start_date:
        body["start_date"] = start_date
    if end_date:
        body["end_date"] = end_date

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_TAVILY_URL, json=body)
    except httpx.HTTPError as e:
        logger.error(f"Tavily request failed: {e}")
        return NewsResult(
            configured=True, query=query,
            message=f"Could not reach the news service: {e}",
        )

    if resp.status_code != 200:
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        logger.error(f"Tavily error {resp.status_code}: {detail}")
        return NewsResult(
            configured=True, query=query,
            message=f"News service error ({resp.status_code}): {detail}",
        )

    data = resp.json()
    articles: list[NewsArticle] = []
    for r in data.get("results", []):
        url = r.get("url", "")
        articles.append(
            NewsArticle(
                title=r.get("title", "").strip() or url,
                url=url,
                source=_source_from_url(url),
                snippet=(r.get("content", "") or "").strip(),
                published=r.get("published_date"),
                score=r.get("score"),
            )
        )

    return NewsResult(configured=True, articles=articles, query=query)


# =============================================================================
# Public API
# =============================================================================

async def search_company_news(
    company: str,
    ticker: str | None = None,
    *,
    max_results: int = 30,
    days: int | None = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> NewsResult:
    """News for a specific company, prioritizing the finance domains."""
    label = f"{company} ({ticker})" if ticker else company
    query = f"{label} stock news earnings analysis"
    return await _tavily_search(
        query,
        max_results=max_results,
        include_domains=FINANCE_DOMAINS,
        days=days,
        start_date=start_date,
        end_date=end_date,
    )


# =============================================================================
# Earnings-call transcripts (investing.com first, then Motley Fool)
# =============================================================================

# Priority-ordered transcript sources. `hint` is a substring the transcript
# page URL must contain, so we skip generic quote/earnings-date pages.
EARNINGS_SOURCES: list[tuple[str, str]] = [
    ("investing.com", "transcript"),
    ("fool.com", "call-transcripts"),
]


# Corporate-suffix / filler words to drop when deriving company match tokens.
_NAME_STOPWORDS = {
    "inc", "corp", "corporation", "incorporated", "co", "company", "ltd",
    "limited", "plc", "llc", "lp", "holdings", "holding", "group", "the",
    "and", "technology", "technologies", "international", "global",
}


def _company_tokens(company: str, ticker: str | None) -> set[str]:
    """Significant lowercase tokens that identify the company in a page URL/title."""
    tokens: set[str] = set()
    if ticker:
        tokens.add(ticker.lower())
    for raw in re.split(r"[^a-z0-9]+", (company or "").lower()):
        if len(raw) >= 3 and raw not in _NAME_STOPWORDS:
            tokens.add(raw)
    return tokens


def _pick_transcript_result(
    results: list[dict],
    hint: str,
    company: str,
    ticker: str | None,
    year: int,
    quarter: int,
) -> dict | None:
    """
    From a source's search results, choose the best actual-transcript page.

    Keeps only pages whose URL looks like a transcript (`hint`) AND that mention
    the company (name token or ticker) — so a same-quarter transcript for a
    different company isn't returned — then ranks by how well the title/URL
    matches the requested year and quarter.
    """
    tokens = _company_tokens(company, ticker)

    def text_of(r: dict) -> str:
        return f"{r.get('url', '')} {r.get('title', '')}".lower()

    # A page qualifies only if it is a transcript page (`hint`), names the
    # company, and matches BOTH the requested year and quarter — so we never
    # surface a different company's or a different quarter's call. Tavily already
    # relevance-orders the results, so the first qualifying page is the best one.
    for r in results:
        s = text_of(r)
        if hint not in (r.get("url", "") or "").lower():
            continue
        if tokens and not any(t in s for t in tokens):
            continue
        if str(year) not in s:
            continue
        if f"q{quarter}" not in s and f"q{quarter} {year}" not in s:
            continue
        return r
    return None


async def _tavily_transcript(
    query: str, domain: str, hint: str,
    company: str, ticker: str | None, year: int, quarter: int,
) -> TranscriptDoc:
    """Search one source for a transcript page and pull its full text."""
    api_key = tavily_api_key()
    if not api_key:
        return TranscriptDoc(configured=False, message="TAVILY_API_KEY not set.")

    body = {
        "api_key": api_key,
        "query": query,
        "include_domains": [domain],
        "max_results": 6,
        "search_depth": "advanced",
        "include_raw_content": True,
        "topic": "news",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(_TAVILY_URL, json=body)
    except httpx.HTTPError as e:
        logger.error(f"Tavily transcript request failed ({domain}): {e}")
        return TranscriptDoc(configured=True, message=f"Could not reach {domain}: {e}")

    if resp.status_code != 200:
        return TranscriptDoc(
            configured=True,
            message=f"Transcript search error ({resp.status_code}) on {domain}.",
        )

    results = resp.json().get("results", [])
    best = _pick_transcript_result(results, hint, company, ticker, year, quarter)
    if not best:
        return TranscriptDoc(configured=True, found=False)

    text = (best.get("raw_content") or best.get("content") or "").strip()
    if not text:
        return TranscriptDoc(configured=True, found=False)

    return TranscriptDoc(
        configured=True,
        found=True,
        source=domain,
        url=best.get("url"),
        title=(best.get("title") or "").strip() or None,
        text=text,
        published=best.get("published_date"),
    )


async def search_earnings_transcript(
    company: str, ticker: str | None, year: int, quarter: int
) -> TranscriptDoc:
    """
    Find a quarter's earnings-call transcript, trying each source in priority
    order (investing.com → Motley Fool). Returns the first full transcript found.
    """
    if not tavily_api_key():
        return TranscriptDoc(
            configured=False,
            message="Transcripts are not configured: set TAVILY_API_KEY on the "
                    "backend to fetch earnings-call transcripts.",
        )

    label = company or ticker or ""
    ticker_hint = f" {ticker}" if ticker else ""
    query = f"{label}{ticker_hint} Q{quarter} {year} earnings call transcript"

    last_msg: str | None = None
    for domain, hint in EARNINGS_SOURCES:
        doc = await _tavily_transcript(
            query, domain, hint, company, ticker, year, quarter
        )
        if doc.found:
            return doc
        if doc.message:
            last_msg = doc.message

    return TranscriptDoc(
        configured=True,
        found=False,
        message=last_msg
        or f"No earnings-call transcript found for {label} Q{quarter} {year} "
           f"on investing.com or Motley Fool.",
    )


async def search_macro_news(
    *,
    max_results: int = 30,
    days: int | None = 7,
    start_date: str | None = None,
    end_date: str | None = None,
) -> NewsResult:
    """Broad market / macroeconomic news, prioritizing the finance domains."""
    query = (
        "stock market today macroeconomic outlook Federal Reserve inflation "
        "market sentiment"
    )
    return await _tavily_search(
        query,
        max_results=max_results,
        include_domains=FINANCE_DOMAINS,
        days=days,
        start_date=start_date,
        end_date=end_date,
    )
