"""
services.news_cache
────────────────────
Disk cache for company news search results, keyed by (ticker, start_date,
end_date) — `backend/news_cache/{TICKER}_{start}_{end}.json`.

Why this exists
────────────────
``providers.news_provider.search_company_news()`` calls the Tavily search API,
which is slow and credit-consuming. A news window that has already been
searched for a ticker will return the same articles (Tavily results for a
past date range don't change), so there is no reason to search for it again.

Mirrors ``services.transcript_cache``: one JSON file per (ticker, range), so a
corrupt or missing file for one window can never affect another. Callers pass
the EXACT (start_date, end_date) they searched with — this is a plain range
cache, not an interval index, so a lookup only hits on an identical range
(the caller is expected to search in matching windows, e.g. calendar months,
so cache entries are reused consistently across callers — see
``services.data_fetcher``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "news_cache"


def _safe(ticker: str | None) -> str:
    t = "".join(c for c in (ticker or "").strip().upper() if c.isalnum() or c in "-._")
    return t or "UNKNOWN"


def _key(ticker: str, start_date: str, end_date: str) -> str:
    return f"{_safe(ticker)}_{start_date}_{end_date}"


def _path(ticker: str, start_date: str, end_date: str) -> Path:
    return _CACHE_DIR / f"{_key(ticker, start_date, end_date)}.json"


def get_news(ticker: str, start_date: str, end_date: str) -> list[dict] | None:
    """
    Cached articles (as plain dicts, matching ``news_provider.NewsArticle``
    fields) for this exact (ticker, range), or ``None`` if never cached.
    """
    path = _path(ticker, start_date, end_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — a corrupt cache file just misses
        logger.warning(f"[news_cache] failed to read {path.name}: {e}")
        return None


def save_news(ticker: str, start_date: str, end_date: str, articles: list) -> None:
    """
    Persist a news search result for this window. Never raises — a cache-write
    failure must not fail the search that just succeeded.

    ``articles`` may be ``news_provider.NewsArticle`` dataclasses or plain
    dicts; either serializes the same way.
    """
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        rows = [asdict(a) if hasattr(a, "__dataclass_fields__") else a for a in articles]
        _path(ticker, start_date, end_date).write_text(
            json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8"
        )
        logger.info(
            f"[news_cache] cached {_key(ticker, start_date, end_date)} "
            f"({len(rows)} article(s))"
        )
    except Exception as e:  # noqa: BLE001 — caching must never fail the caller
        logger.warning(
            f"[news_cache] failed to cache {_key(ticker, start_date, end_date)}: {e}"
        )


def list_cached_ranges(ticker: str) -> list[tuple[str, str]]:
    """All cached (start_date, end_date) windows for this ticker, sorted."""
    if not _CACHE_DIR.exists():
        return []
    prefix = f"{_safe(ticker)}_"
    out: list[tuple[str, str]] = []
    for f in _CACHE_DIR.glob(f"{prefix}*.json"):
        rest = f.stem[len(prefix):]
        parts = rest.split("_")
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return sorted(out)
