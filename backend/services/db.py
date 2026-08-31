"""
services.db
───────────
The app's first *durable* store: a SQLite database for the trading portfolio and
journal.

Why this exists alongside ``services.storage``
──────────────────────────────────────────────
Everything in :mod:`services.storage` is in-memory and process-local — restarting
the server clears it, which is fine for re-ingestible filing data. A portfolio is
different: the user's holdings, their trades, and the "entry rationale" they wrote
at the time are irreplaceable. They must outlive the process.

Why SQLite, and why no ORM
──────────────────────────
It needs no new dependency (``sqlite3`` is stdlib), no server, and it gives real
transactions. Transactions are the point: updating a holding's average price is a
read-modify-write (read old qty/avg → compute → write back), and two trades landing
concurrently must not interleave and lose one another's update.

Concurrency model
─────────────────
One process-wide connection opened with ``check_same_thread=False``, because
FastAPI serves requests from a threadpool and the connection is shared across
those threads. SQLite allows exactly one writer at a time, so every write goes
through :func:`transaction`, which holds ``_write_lock``. Reads need no lock —
WAL mode (enabled below) lets readers proceed while a write is in flight.

Swapping the backend later
──────────────────────────
Callers (phases 2-6) depend on the *repository* functions in
``services.portfolio_service``, never on this module's driver details. Moving to
Postgres means re-implementing this file and that repository, not the routers.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


# The database lives beside `backend/analysis_history/`, the app's other on-disk
# store, so all persistent user data sits together under `backend/`.
DB_PATH: Path = Path(__file__).parent.parent / "portfolio.db"


# =============================================================================
# Schema
# =============================================================================
# Kept as module constants so the shape of the data is readable in one place.
# Every statement is `IF NOT EXISTS`, so `init_db()` is safe on every startup.

_SCHEMA_HOLDINGS = """
CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL UNIQUE,
    quantity        REAL    NOT NULL,
    avg_price       REAL    NOT NULL,
    initial_fx_rate REAL,
    currency        TEXT    NOT NULL DEFAULT 'USD',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
)
"""

# `side` is constrained at the schema level: a typo'd side would silently corrupt
# every downstream average-price calculation, so the database rejects it outright.
#
# The FK to holdings(ticker) — rather than holdings(id) — is deliberate: the
# ticker is the natural key the whole app already routes on (see
# `DocumentStore._normalize`), and it keeps trade rows readable on their own.
# It requires holdings.ticker to be UNIQUE, which it is.
_SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL    NOT NULL,
    executed_at     TEXT    NOT NULL,
    execution_price REAL,
    total_value     REAL,
    fx_rate         REAL,
    entry_rationale TEXT,
    avg_price_after REAL,
    created_at      TEXT    NOT NULL,
    FOREIGN KEY (ticker) REFERENCES holdings (ticker) ON DELETE CASCADE
)
"""

# The Coach agent (phase 6) reads one ticker's trades in time order on every run,
# and the journal view pages through them the same way.
_SCHEMA_TRADES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_trades_ticker_executed
    ON trades (ticker, executed_at)
"""

_SCHEMA_STATEMENTS = (
    _SCHEMA_HOLDINGS,
    _SCHEMA_TRADES,
    _SCHEMA_TRADES_INDEX,
)


# =============================================================================
# Connection management
# =============================================================================

_connection: sqlite3.Connection | None = None
_connection_lock = threading.Lock()   # guards lazy creation of `_connection`
_write_lock = threading.Lock()        # serializes writers (SQLite allows one)


def _configure(conn: sqlite3.Connection) -> None:
    """Apply the per-connection PRAGMAs the schema depends on."""
    # Foreign keys are OFF by default in SQLite — without this the
    # `trades → holdings` ON DELETE CASCADE is silently not enforced, and
    # deleting a holding would leave orphaned trades behind.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers run concurrently with a writer, which matters because the
    # connection is shared across FastAPI's threadpool.
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait rather than immediately raising "database is locked" under contention.
    conn.execute("PRAGMA busy_timeout = 5000")


def get_connection() -> sqlite3.Connection:
    """
    Return the process-wide connection, opening it on first use.

    Rows come back as :class:`sqlite3.Row`, so callers can use ``row["ticker"]``
    and ``dict(row)`` instead of positional indexing.
    """
    global _connection
    if _connection is None:
        with _connection_lock:
            if _connection is None:   # re-check: another thread may have won
                conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                _configure(conn)
                _connection = conn
                logger.info(f"Opened portfolio database: {DB_PATH}")
    return _connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    Run a write inside a single serialized transaction.

    Commits on success, rolls back on any exception, and holds ``_write_lock``
    for the duration so two concurrent writers cannot interleave a
    read-modify-write (the average-price update in phase 2 depends on this).

    Usage::

        with transaction() as conn:
            conn.execute("INSERT INTO trades (...) VALUES (...)", params)
    """
    conn = get_connection()
    with _write_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """
    Create the schema if it isn't there yet. Idempotent — safe on every startup.
    """
    with transaction() as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
    logger.info("Portfolio database schema ready (holdings, trades).")


def close_db() -> None:
    """Close the connection, if one was opened. Used on shutdown."""
    global _connection
    with _connection_lock:
        if _connection is not None:
            _connection.close()
            _connection = None
            logger.info("Closed portfolio database.")


# =============================================================================
# Helpers
# =============================================================================

def utc_now_iso() -> str:
    """
    Current UTC time as an ISO-8601 string.

    Timestamps are stored as TEXT, matching the convention already used by
    ``rag/history_store.py``. ISO-8601 sorts lexicographically in the same order
    it sorts chronologically, so `ORDER BY executed_at` is correct as written.
    """
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# FastAPI dependency provider
# =============================================================================
# Mirrors the `get_*` providers at the bottom of `services/storage.py`: routers
# depend on this via `Depends(get_db)` rather than importing the connection, so
# the storage backend can change without touching an endpoint.

def get_db() -> sqlite3.Connection:
    return get_connection()
