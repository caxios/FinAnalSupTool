"""
services.data_lake
────────────────────
Immutable raw-document archive — every raw artifact this app fetches (filing
HTML, transcript source text, raw news JSON, insider JSON) is written here
BEFORE any parsing, as the audit trail the parsed JSON caches (filing_cache/,
transcript_cache/, news_cache/, insider_cache/) don't provide: those store
the PARSED result, this stores the original bytes, unmodified.

Path convention: data_lake/{TICKER}/{doc_type}/{period_label}.{ext}

Immutable by design: SEC filings, past news articles, and past earnings
transcripts never change once fetched, so a write to a path that already
exists is a no-op — the original bytes are never silently overwritten by a
later, possibly-different fetch of "the same" document.

Part of Phase 1 (storage foundations) of the hybrid retrieval architecture —
see implementation_plan/new_db_architecture_plan/. Nothing calls save_raw()
yet; Phase 2 wires the actual ingestion tracks into this module.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_LAKE_ROOT: Path = Path(__file__).parent.parent / "data_lake"

# Fixed doc_type vocabulary — used consistently by every ingestion track in
# Phase 2 and beyond. Keep this list in sync with what actually gets archived.
DOC_TYPES = (
    "sec_filing_html", "sec_filing_pdf", "earnings_transcript",
    "news_article", "insider_json",
)


def _safe(part: str) -> str:
    """Filesystem-safe path segment — mirrors the ``_safe()`` helper already
    used by filing_cache.py / transcript_cache.py / news_cache.py."""
    s = "".join(c for c in (part or "").strip() if c.isalnum() or c in "-._ ")
    return s.strip() or "UNKNOWN"


def _path(ticker: str, doc_type: str, period_label: str, ext: str) -> Path:
    return (
        _LAKE_ROOT / _safe(ticker).upper() / _safe(doc_type)
        / f"{_safe(period_label)}.{ext.lstrip('.')}"
    )


def save_raw(
    ticker: str, doc_type: str, period_label: str, ext: str, content: bytes | str
) -> Path:
    """
    Write raw content to ``data_lake/{TICKER}/{doc_type}/{period_label}.{ext}``.

    Immutable: if the path already exists, this is a no-op — returns the
    existing path without rewriting or even reading it. Never raises; a
    write failure is logged and the (unwritten) path is still returned so
    callers never need a try/except at the call site.
    """
    path = _path(ticker, doc_type, period_label, ext)
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        logger.info(f"[data_lake] archived {doc_type} for {ticker} @ {period_label}")
    except Exception as e:  # noqa: BLE001 — archiving must never fail the caller
        logger.warning(f"[data_lake] failed to archive {path}: {e}")
    return path


def get_raw(ticker: str, doc_type: str, period_label: str, ext: str) -> bytes | None:
    """The raw bytes at this path, or ``None`` if never archived (or unreadable)."""
    path = _path(ticker, doc_type, period_label, ext)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except Exception as e:  # noqa: BLE001 — a read failure just misses, doesn't crash
        logger.warning(f"[data_lake] failed to read {path}: {e}")
        return None


def list_raw(ticker: str, doc_type: str | None = None) -> list[Path]:
    """Every archived file for this ticker, optionally scoped to one doc_type."""
    root = _LAKE_ROOT / _safe(ticker).upper()
    if not root.exists():
        return []
    if doc_type:
        target = root / _safe(doc_type)
        return sorted(target.glob("*")) if target.exists() else []
    return sorted(p for p in root.glob("*/*") if p.is_file())
