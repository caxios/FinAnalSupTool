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

Per-company isolation
─────────────────────
State is isolated per company (ticker) instead of sharing one global namespace.
A :class:`CompanyStore` owns everything extracted for a single company; the
:class:`DocumentStore` is now a thin *registry* of those per-ticker stores.
:class:`DebateStore` and :class:`MediaCache` are likewise keyed by ticker, so two
companies analyzed in the same session never bleed into each other's context.

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
# CompanyStore — everything extracted from ONE company's uploaded filings
# =============================================================================

class CompanyStore:
    """
    Holds all per-period data extracted from a single company's SEC filings,
    keyed by a unique ``period_key`` (e.g. "FY2025", "Aug 2025", or an XBRL
    fiscal label).

    Also owns the temp directory that company's raw PDFs are saved to, so
    section pages can be re-extracted on demand by GET /filing-pdf. Each company
    gets its own temp directory (``prefix=f"finanalst_{ticker}_"``) so filenames
    never collide across companies.
    """

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

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

        # This company's uploaded PDFs live here; cleaned up on shutdown. The
        # ticker is baked into the prefix so two companies can hold filings with
        # the same filename without overwriting each other.
        safe = "".join(c for c in ticker if c.isalnum()) or "NA"
        self.upload_dir: Path = Path(tempfile.mkdtemp(prefix=f"finanalst_{safe}_"))
        logger.info(f"[{ticker}] Upload temp directory: {self.upload_dir}")

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
            f"[{self.ticker}] Rebuilt merged tables for {len(self.table_store)} "
            f"period(s), ratios for {len(self.metrics_store)} period(s)"
        )


# =============================================================================
# DocumentStore — registry of per-company CompanyStores
# =============================================================================

class DocumentStore:
    """
    A registry that isolates each company's extracted filing data in its own
    :class:`CompanyStore`, keyed by ticker.

    The routers no longer read filing data straight off this object; they ask for
    the relevant company's store via :meth:`get_company_store` and work with that.
    """

    def __init__(self) -> None:
        self.companies: dict[str, CompanyStore] = {}

    @staticmethod
    def _normalize(ticker: str) -> str:
        """Canonical registry key for a ticker (upper-cased, trimmed)."""
        return (ticker or "").strip().upper()

    def get_company_store(self, ticker: str) -> CompanyStore:
        """
        Return the :class:`CompanyStore` for ``ticker``, creating (and
        registering) a fresh one on first access.
        """
        key = self._normalize(ticker)
        store = self.companies.get(key)
        if store is None:
            store = CompanyStore(key)
            self.companies[key] = store
            logger.info(f"Registered new company store for '{key}'")
        return store

    def has_company(self, ticker: str) -> bool:
        """
        Whether ``ticker`` already has a store — WITHOUT creating one.

        Routers use this to 404 on an unknown ticker instead of silently
        registering an empty store for a typo'd symbol.
        """
        return self._normalize(ticker) in self.companies

    def list_tickers(self) -> list[str]:
        """All tickers with a registered store (sorted for stable output)."""
        return sorted(self.companies.keys())


# =============================================================================
# MediaCache — most recent media/macro data fetched by Views 2 & 3, per company
# =============================================================================

# Reserved cache key for data that belongs to NO single company: the macro view's
# news, videos, and market sentiment. Not a real ticker, so it can never collide
# with one.
MACRO_SCOPE = "__MACRO__"


class MediaCache:
    """
    Caches the most recent media/macro data (news, videos, sentiment,
    transcripts) so the AI assistant's context can reference it — this is what
    lets the assistant "see" all views. Keyed by ticker so each company keeps its
    own media; cleared on restart.

    Per-company entries hold "company_news", "company_videos", and "transcripts"
    (video_id → {text}). Market-wide data ("macro_news", "macro_videos",
    "sentiment") is stored once under :data:`MACRO_SCOPE`, since it isn't tied to
    any one company.
    """

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    @staticmethod
    def _normalize(ticker: str) -> str:
        return (ticker or "").strip().upper()

    def get(self, ticker: str) -> dict:
        """
        Return the per-ticker media dict, creating an empty one (with an empty
        ``transcripts`` map) on first access.
        """
        key = self._normalize(ticker)
        entry = self.data.get(key)
        if entry is None:
            entry = {"transcripts": {}}
            self.data[key] = entry
        return entry

    def transcripts(self, ticker: str) -> dict:
        """The per-ticker transcripts map (video_id → {"text": ...})."""
        return self.get(ticker)["transcripts"]


# =============================================================================
# DebateStore — the most recent /analyze run per company (for role-based chat)
# =============================================================================

class DebateStore:
    """
    Holds the most recent /analyze run PER COMPANY so the role-based chat can
    talk to a single agent (or the Manager) in isolation. A new run for a ticker
    overwrites only that ticker's record.

    Each per-ticker payload's keys: "reports", "agent_contexts", "transcript",
    "manager", "period", "company".
    """

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    @staticmethod
    def _normalize(ticker: str) -> str:
        return (ticker or "").strip().upper()

    def replace(self, ticker: str, payload: dict) -> None:
        """Atomically swap in a fresh run's data for one ticker."""
        self.data[self._normalize(ticker)] = payload

    def get(self, ticker: str) -> dict:
        """This ticker's most recent run, or an empty dict if none yet."""
        return self.data.get(self._normalize(ticker), {})


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
