"""
services.storage
────────────────
In-memory state containers for the app, plus the FastAPI dependency providers
that hand them to the routers.

Why classes instead of module-level dicts?
──────────────────────────────────────────
The prototype kept all extracted data in bare module-level dicts in ``main.py``.
That coupled state lifespan to import time and made every endpoint reach into
globals. Wrapping the state in small classes and injecting them via
``Depends()`` decouples the two: the routers no longer know *where* state lives,
so swapping these for a real database (PostgreSQL/Redis) later is a matter of
re-implementing these classes and their providers — no endpoint changes.

Everything here is still in-memory and process-local: restarting the server
clears all data, exactly as before.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pandas as pd

from parsers.pdf_utils import merge_tables_across_periods
from providers.edgar_xbrl import build_ratios_table

logger = logging.getLogger(__name__)


# =============================================================================
# DocumentStore — everything extracted from uploaded filings
# =============================================================================

class DocumentStore:
    """
    Holds all per-period data extracted from uploaded SEC filings, keyed by a
    unique ``period_key`` (e.g. "FY2025", "Aug 2025", or an XBRL fiscal label).

    Also owns the temp directory the raw PDFs are saved to, so section pages can
    be re-extracted on demand by GET /filing-pdf.
    """

    def __init__(self) -> None:
        # Extracted text sections per period.
        # {"2023-10K": {"mda": "text...", "footnotes": "text...", ...}}
        self.text_store: dict[str, dict[str, str | None]] = {}

        # Classified table DataFrames per period.
        # {"2023-10K": {"balance_sheet": [df1, df2], "income_statement": [df3]}}
        self.table_store: dict[str, dict[str, list[pd.DataFrame]]] = {}

        # Metadata about each uploaded filing (filename, form type, period, …).
        self.filing_meta: dict[str, dict] = {}

        # Cached cross-period merged tables — rebuilt after every upload so
        # GET /financials returns instantly without re-computing.
        self.merged_tables: dict[str, pd.DataFrame] = {}

        # Raw numeric metrics per period (XBRL only), used to build ratios.
        # {"2025-Q2-10-Q": {"revenue": 2.0e9, "total_assets": ..., ...}}
        self.metrics_store: dict[str, dict] = {}

        # Char-span → page-number map per period, for GET /filing-pdf.
        # {"2023-10K": [(page_num, char_start, char_end), ...]}
        self.page_map_store: dict[str, list[tuple[int, int, int]]] = {}

        # Uploaded PDFs live here temporarily; cleaned up on shutdown.
        self.upload_dir: Path = Path(tempfile.mkdtemp(prefix="finanalst_"))
        logger.info(f"Upload temp directory: {self.upload_dir}")

    # ── period-key derivation ────────────────────────────────────────────
    @staticmethod
    def derive_period_key(form_type: str, period_end, fallback: str) -> str:
        """
        Build a unique, human-readable period key from the period-end date.

        Used only when XBRL doesn't supply an authoritative fiscal label
        (e.g. "Q2 FY2026"). The key MUST be unique per filing so that Q1/Q2/Q3
        of the same year don't collapse onto one shared key and overwrite each
        other in the stores.

          - 10-K → "FY2025"
          - 10-Q → "Aug 2025"  (month + year — unique per quarter)
          - unknown period → the provided fallback (usually the filename stem)
        """
        if period_end is None:
            return fallback
        if form_type == "10-K":
            return f"FY{period_end.year}"
        return period_end.strftime("%b %Y")

    # ── merged-table maintenance ─────────────────────────────────────────
    def order_period_columns(
        self, df: pd.DataFrame | None
    ) -> pd.DataFrame | None:
        """
        Reorder a merged table's period columns chronologically (oldest → newest).

        The first column ("Line Item" or "Ratio") is kept in place; the remaining
        period columns are sorted by each period's stored ``sort_date``. Periods
        without a parseable date sort to the end (by name) so nothing is dropped.
        """
        if df is None or df.empty or len(df.columns) <= 1:
            return df

        first_col = df.columns[0]
        period_cols = [c for c in df.columns if c != first_col]

        def sort_key(pk: str):
            sort_date = self.filing_meta.get(pk, {}).get("sort_date")
            # (0, date) dated periods first in chronological order;
            # (1, name) undated periods after, ordered by key for stability.
            return (0, sort_date) if sort_date else (1, str(pk))

        ordered = sorted(period_cols, key=sort_key)
        return df[[first_col] + ordered]

    def rebuild_merged_tables(self) -> None:
        """
        Rebuild the cross-period merged tables from ``table_store``.

        Called after every upload so GET /financials reflects the newly
        uploaded filings.
        """
        merged = (
            merge_tables_across_periods(self.table_store)
            if self.table_store else {}
        )
        # Historical ratios table — computed from the raw per-period metrics.
        # Shares the same shape as the statement tables so GET /financials and
        # the frontend renderer treat "ratios" like any other statement type.
        merged["ratios"] = build_ratios_table(self.metrics_store)
        # Order period columns as a chronological time series (not upload order).
        for key in list(merged.keys()):
            merged[key] = self.order_period_columns(merged[key])
        self.merged_tables = merged
        logger.info(
            f"Rebuilt merged tables for {len(self.table_store)} period(s), "
            f"ratios for {len(self.metrics_store)} period(s)"
        )


# =============================================================================
# MediaCache — most recent media/macro data fetched by Views 2 & 3
# =============================================================================

class MediaCache:
    """
    Caches the most recent media/macro data (news, videos, sentiment,
    transcripts) so the AI assistant's context can reference it — this is what
    lets the assistant "see" all views. Cleared on restart.

    ``data`` keys: "company_news", "company_videos", "macro_news",
    "macro_videos", "sentiment"; plus "transcripts" (video_id → {text}).
    """

    def __init__(self) -> None:
        self.data: dict = {"transcripts": {}}

    @property
    def transcripts(self) -> dict:
        return self.data["transcripts"]


# =============================================================================
# DebateStore — the most recent /analyze run (for role-based chat)
# =============================================================================

class DebateStore:
    """
    Holds the most recent /analyze run so the role-based chat can talk to a
    single agent (or the Manager) in isolation. Overwritten on each /analyze.

    ``data`` keys: "reports", "agent_contexts", "transcript", "manager",
    "period", "company".
    """

    def __init__(self) -> None:
        self.data: dict = {}

    def replace(self, payload: dict) -> None:
        """Atomically swap in a fresh run's data (clears the previous run)."""
        self.data.clear()
        self.data.update(payload)


# =============================================================================
# Singletons + FastAPI dependency providers
# =============================================================================
# One process-wide instance of each store. The ``get_*`` functions are what the
# routers depend on via ``Depends(...)``; swapping the backing implementation
# later means changing only these providers, not the endpoints.

_document_store = DocumentStore()
_media_cache = MediaCache()
_debate_store = DebateStore()


def get_document_store() -> DocumentStore:
    return _document_store


def get_media_cache() -> MediaCache:
    return _media_cache


def get_debate_store() -> DebateStore:
    return _debate_store
