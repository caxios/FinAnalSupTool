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
import logging
from dataclasses import dataclass, field
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
    max_results: int = 8,
    include_domains: list[str] | None = None,
    days: int | None = None,
) -> NewsResult:
    """Run one Tavily search and normalize the response to NewsArticles."""
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
        "max_results": max_results,
        "search_depth": "basic",
        "topic": "news",
    }
    if include_domains:
        body["include_domains"] = include_domains
    if days:
        body["days"] = days

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
    company: str, ticker: str | None = None, max_results: int = 8
) -> NewsResult:
    """News for a specific company, prioritizing the finance domains."""
    label = f"{company} ({ticker})" if ticker else company
    query = f"{label} stock news earnings analysis"
    return await _tavily_search(
        query,
        max_results=max_results,
        include_domains=FINANCE_DOMAINS,
        days=14,
    )


async def search_macro_news(max_results: int = 10) -> NewsResult:
    """Broad market / macroeconomic news, prioritizing the finance domains."""
    query = (
        "stock market today macroeconomic outlook Federal Reserve inflation "
        "market sentiment"
    )
    return await _tavily_search(
        query,
        max_results=max_results,
        include_domains=FINANCE_DOMAINS,
        days=7,
    )
