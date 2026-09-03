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
    realized_pnl      REAL,
    realized_pnl_base REAL,
    fee               REAL,
    tax               REAL,
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

# Coaching reviews, kept so the coach can remember what it has already said and
# the user can read back what they were told. Before this table every review was
# rendered once and discarded.
#
# `report_json` holds the whole report rather than exploded columns: the report
# schema will keep growing, and an old review must stay readable after it does.
# `rationale_snapshot` freezes the text that was actually judged — if the trade's
# rationale is later edited, the review must not appear to have assessed words it
# never saw.
_SCHEMA_COACH_REVIEWS = """
CREATE TABLE IF NOT EXISTS coach_reviews (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    review_type        TEXT    NOT NULL CHECK (review_type IN
                         ('pre_trade', 'retrospective', 'journal')),
    trade_id           INTEGER,
    ticker             TEXT,
    scope              TEXT,
    rationale_snapshot TEXT,
    report_json        TEXT    NOT NULL,
    model              TEXT,
    data_as_of         TEXT,
    created_at         TEXT    NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE
)
"""

# The journal view asks "has this trade been reviewed?" for every row it renders,
# and the pending-backlog query is an anti-join over the same column.
_SCHEMA_REVIEWS_TRADE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_reviews_trade ON coach_reviews (trade_id)
"""

_SCHEMA_REVIEWS_TYPE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_reviews_type_time
    ON coach_reviews (review_type, created_at)
"""

# The cash ledger. Every movement of money is one row here, and a balance is a
# SUM over them — there is deliberately no stored balance column anywhere, because
# a stored balance eventually disagrees with its own ledger and nothing can then
# decide which is right.
#
# Two properties carry the whole multi-currency design:
#
#   `amount` is denominated in `currency` and SIGNED (+ into the account, - out),
#   so a balance is `SUM(amount) WHERE currency = ?` with no per-type sign table
#   to get wrong. The sign is enforced against `flow_type` in `cash_service`,
#   where the error message can be useful.
#
#   `fx_to_krw` is NOT NULL on every row, 1.0 on KRW rows. It is the only record
#   of what the money was worth in base currency AT THE MOMENT IT MOVED, and it
#   cannot be reconstructed afterwards. Without it there is no cost basis in won
#   and no realized FX gain — only a number that silently assumes today's rate
#   always applied.
#
# `fx_out`/`fx_in` are the two legs of a 환전, linked by `conversion_id` so the
# pair renders as one event. They are internal: no money entered or left.
# `adjustment` exists for reconciling against a broker statement — a visible,
# dated, note-carrying row, never a silent correction.
_SCHEMA_CASH_FLOWS = """
CREATE TABLE IF NOT EXISTS cash_flows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_type     TEXT    NOT NULL CHECK (flow_type IN
                    ('deposit', 'withdrawal', 'buy', 'sell', 'dividend',
                     'fee', 'tax', 'interest', 'fx_out', 'fx_in', 'adjustment')),
    currency      TEXT    NOT NULL,
    amount        REAL    NOT NULL,
    fx_to_krw     REAL    NOT NULL,
    occurred_at   TEXT    NOT NULL,
    trade_id      INTEGER,
    conversion_id TEXT,
    market_rate         REAL,
    realized_fx_pnl_krw REAL,
    note          TEXT,
    created_at    TEXT    NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE
)
"""

# Every balance query is "this currency, up to this instant"; the net-worth
# series in phase 4 walks exactly that, one date at a time.
_SCHEMA_CASH_FLOWS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cash_flows_ccy_time
    ON cash_flows (currency, occurred_at)
"""

_SCHEMA_STATEMENTS = (
    _SCHEMA_HOLDINGS,
    _SCHEMA_TRADES,
    _SCHEMA_TRADES_INDEX,
    _SCHEMA_COACH_REVIEWS,
    _SCHEMA_REVIEWS_TRADE_INDEX,
    _SCHEMA_REVIEWS_TYPE_INDEX,
    _SCHEMA_CASH_FLOWS,
    _SCHEMA_CASH_FLOWS_INDEX,
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


# Columns added after a table shipped. `CREATE TABLE IF NOT EXISTS` will not
# alter a table that already exists, and SQLite has no `ADD COLUMN IF NOT
# EXISTS`, so each one is applied only when `PRAGMA table_info` says it is
# missing. Every entry must be nullable — SQLite cannot add a NOT NULL column
# without a default, and back-filling a real value is the caller's job, not the
# migration's.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "trades": (
        # Realized in the asset's own currency, and in base currency at the
        # rates that actually applied. Neither is derivable from the other:
        # their difference IS the exchange-rate component of the result.
        ("realized_pnl", "REAL"),
        ("realized_pnl_base", "REAL"),
        ("fee", "REAL"),
        ("tax", "REAL"),
    ),
    "cash_flows": (
        # On a conversion: the mid-market rate that day, kept beside the rate
        # the user actually got, so the spread they paid is visible.
        ("market_rate", "REAL"),
        # On the `fx_in` leg of a conversion back to base currency.
        ("realized_fx_pnl_krw", "REAL"),
    ),
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Apply `_ADDED_COLUMNS` to tables that predate them."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue   # table not created yet; its CREATE already has them
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                logger.info(f"Added column {table}.{name}")


def init_db() -> None:
    """
    Create the schema if it isn't there yet. Idempotent — safe on every startup.
    """
    with transaction() as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        _ensure_columns(conn)
    logger.info(
        "Portfolio database schema ready "
        "(holdings, trades, coach_reviews, cash_flows)."
    )


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
