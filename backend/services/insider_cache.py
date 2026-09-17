"""
services.insider_cache
────────────────────────
Disk cache for Form 4 insider trades and 8-K filing metadata, one JSON file
per ticker per data type:

    backend/insider_cache/{TICKER}_form4.json
    backend/insider_cache/{TICKER}_8k.json

Why this exists
────────────────
``findata.get_insider_trades()`` and ``findata.find_filings(form_type="8-K")``
both hit SEC EDGAR. Unlike filings/transcripts, this data is genuinely
append-only-with-a-tail — new Form 4s and 8-Ks appear over time — so this
cache is a snapshot, not an immutable fact: a fresh fetch always overwrites
it (there's no "already resolved, never changes" case like a past quarter's
earnings transcript). It still saves a round-trip for the common case of
opening the Data tab repeatedly without wanting the latest filings every time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "insider_cache"


def _safe(ticker: str | None) -> str:
    t = "".join(c for c in (ticker or "").strip().upper() if c.isalnum() or c in "-._")
    return t or "UNKNOWN"


def _path(ticker: str, kind: str) -> Path:
    return _CACHE_DIR / f"{_safe(ticker)}_{kind}.json"


def _read(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — a corrupt cache file just misses
        logger.warning(f"[insider_cache] failed to read {path.name}: {e}")
        return None


def _write(path: Path, rows: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8"
        )
        logger.info(f"[insider_cache] cached {path.name} ({len(rows)} row(s))")
    except Exception as e:  # noqa: BLE001 — caching must never fail the caller
        logger.warning(f"[insider_cache] failed to cache {path.name}: {e}")


def get_insider_trades(ticker: str) -> list[dict] | None:
    """Cached Form 4 trades for this ticker, or ``None`` if never fetched."""
    return _read(_path(ticker, "form4"))


def save_insider_trades(ticker: str, trades: list[dict]) -> None:
    """Persist Form 4 trades for this ticker (overwrites any prior snapshot)."""
    _write(_path(ticker, "form4"), trades)


def get_8k_filings(ticker: str) -> list[dict] | None:
    """Cached 8-K filing metadata for this ticker, or ``None`` if never fetched."""
    return _read(_path(ticker, "8k"))


def save_8k_filings(ticker: str, filings: list[dict]) -> None:
    """Persist 8-K filing metadata for this ticker (overwrites any prior snapshot)."""
    _write(_path(ticker, "8k"), filings)


def has_cache(ticker: str) -> bool:
    """Whether EITHER Form 4 or 8-K data has ever been cached for this ticker."""
    return _path(ticker, "form4").exists() or _path(ticker, "8k").exists()
