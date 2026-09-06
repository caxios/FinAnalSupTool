"""
services.transcript_cache
──────────────────────────
Disk cache for full earnings-call transcripts, keyed by (ticker, year, quarter)
— `backend/transcript_cache/{TICKER}_{year}Q{quarter}.json`.

Why this exists
────────────────
``providers.news_provider.search_earnings_transcript()`` calls the Tavily
search API with ``include_raw_content=True`` — a slow (advanced search depth),
credit-consuming call that returns the SAME transcript every time it is asked
for the same quarter. A transcript for AVGO Q2 2026 does not change after it
is fetched once, so there is no reason to ever search for it again once found.

A NEGATIVE result (``found=False``, no transcript exists on either source yet)
is cached too — the alternative is that a chatty user re-triggers the same two
fruitless Tavily searches on every question about a quarter with no posted
transcript. This trades a small staleness risk (a transcript that appears
DAYS after the call won't be picked up until the cache is cleared) for
eliminating repeat searches — matching this plan's "never re-fetch unless
explicitly requested" design.

This is a CACHE, not a second source of truth, mirroring ``services.filing_
cache``: one JSON file per (ticker, quarter), so a corrupt or missing file for
one quarter can never affect another.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "transcript_cache"


def _safe(ticker: str | None) -> str:
    t = "".join(c for c in (ticker or "").strip().upper() if c.isalnum() or c in "-._")
    return t or "UNKNOWN"


def _key(ticker: str, year: int, quarter: int) -> str:
    return f"{_safe(ticker)}_{int(year)}Q{int(quarter)}"


def _path(ticker: str, year: int, quarter: int) -> Path:
    return _CACHE_DIR / f"{_key(ticker, year, quarter)}.json"


def get_transcript(ticker: str, year: int, quarter: int):
    """
    The cached transcript search outcome for this quarter, or ``None`` if it
    has never been searched for.

    Returns a ``providers.news_provider.TranscriptDoc`` (not a bare dict) —
    the type it was saved as — so a caller can use it exactly like a fresh
    result from :func:`providers.news_provider.search_earnings_transcript`.
    """
    from providers.news_provider import TranscriptDoc

    path = _path(ticker, year, quarter)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TranscriptDoc(**data)
    except Exception as e:  # noqa: BLE001 — a corrupt cache file just misses, doesn't crash
        logger.warning(f"[transcript_cache] failed to read {path.name}: {e}")
        return None


def save_transcript(ticker: str, year: int, quarter: int, doc) -> None:
    """
    Persist a transcript search outcome (found or not) so it is never
    re-searched for. Never raises — a cache-write failure must not fail the
    search that just succeeded.

    Skips caching when ``doc.configured`` is False: a missing TAVILY_API_KEY
    is a server configuration problem, not a fact about this quarter, and
    must not be remembered as "no transcript exists".
    """
    if not getattr(doc, "configured", False):
        return
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        _path(ticker, year, quarter).write_text(
            json.dumps(asdict(doc), ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info(
            f"[transcript_cache] cached {_key(ticker, year, quarter)} "
            f"(found={getattr(doc, 'found', False)})"
        )
    except Exception as e:  # noqa: BLE001 — caching must never fail the caller
        logger.warning(
            f"[transcript_cache] failed to cache {_key(ticker, year, quarter)}: {e}"
        )


def list_cached_quarters(ticker: str) -> list[str]:
    """Quarter keys (e.g. ``'2026Q2'``) cached for this ticker, sorted."""
    if not _CACHE_DIR.exists():
        return []
    prefix = f"{_safe(ticker)}_"
    return sorted(
        f.stem[len(prefix):] for f in _CACHE_DIR.glob(f"{prefix}*.json")
    )
