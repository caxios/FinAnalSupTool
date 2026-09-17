"""
services.price_cache
─────────────────────
Disk cache for pre-computed technical/price data, keyed by (ticker,
start_date, end_date) — `backend/price_cache/{TICKER}_{start}_{end}.json`.

Why this exists
────────────────
``providers.price_provider.fetch_technical_data()`` hits yfinance and
recomputes every indicator (SMA/RSI/MACD/Bollinger/...) from scratch. For a
window whose end date is in the past, none of that changes on a re-fetch, so
it is cached exactly like the other disk caches in this package.

A window ending TODAY (or very recently) is different: `current_price` and
the trailing indicators keep moving intraday. Callers should treat a cache hit
whose ``period_end`` is within the last day as stale for "current" fields and
pass ``force_refresh`` — see ``services.data_fetcher.fetch_price_data``, which
enforces that policy so cache staleness isn't every caller's problem.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "price_cache"


def _safe(ticker: str | None) -> str:
    t = "".join(c for c in (ticker or "").strip().upper() if c.isalnum() or c in "-._")
    return t or "UNKNOWN"


def _key(ticker: str, start_date: str, end_date: str) -> str:
    return f"{_safe(ticker)}_{start_date}_{end_date}"


def _path(ticker: str, start_date: str, end_date: str) -> Path:
    return _CACHE_DIR / f"{_key(ticker, start_date, end_date)}.json"


def get_price_data(ticker: str, start_date: str, end_date: str) -> dict | None:
    """The cached ``TechnicalData`` (as a plain dict) for this exact window, or
    ``None`` if never cached."""
    path = _path(ticker, start_date, end_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — a corrupt cache file just misses
        logger.warning(f"[price_cache] failed to read {path.name}: {e}")
        return None


def save_price_data(ticker: str, start_date: str, end_date: str, data) -> None:
    """
    Persist a computed ``TechnicalData`` for this window. Never raises — a
    cache-write failure must not fail the fetch that just succeeded.

    ``data`` may be the ``price_provider.TechnicalData`` dataclass or a plain
    dict; either serializes the same way.
    """
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        payload = asdict(data) if hasattr(data, "__dataclass_fields__") else data
        _path(ticker, start_date, end_date).write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        logger.info(f"[price_cache] cached {_key(ticker, start_date, end_date)}")
    except Exception as e:  # noqa: BLE001 — caching must never fail the caller
        logger.warning(
            f"[price_cache] failed to cache {_key(ticker, start_date, end_date)}: {e}"
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
