"""
services.structured_db
────────────────────────
DuckDB-backed store for XBRL financial facts and the SEC filing footnote
graph — the "verified numbers" tier of the hybrid retrieval architecture
(see implementation_plan/new_db_architecture_plan/).

Why DuckDB
──────────
Embedded, zero-ops, real SQL (proper types, real UPSERT) — no server to
install or run, unlike Postgres. Mirrors services.db's design: one process-
wide connection, a `transaction()` contextmanager, an idempotent `init_db()`.

Concurrency model
──────────────────
A single lock guards EVERY statement — reads included, not just writes.
services.db can let SQLite readers run lock-free because WAL mode guarantees
they never block on an in-flight writer; DuckDB's Python connection object
has no equivalent documented guarantee for unsynchronized concurrent access
from multiple threads, so this module serializes everything through one
lock. Fine at this app's scale (one local user, one process); if it ever
becomes a bottleneck, DuckDB's `.cursor()` gives each thread its own cursor
against the same database file without needing a second connection scheme.

This module is schema + CRUD only — Phase 1 of the migration. Nothing calls
upsert_facts()/upsert_footnote_links() yet; Phase 2 wires providers/edgar_xbrl.py
and the new SEC HTML footnote parser into this store.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import duckdb

logger = logging.getLogger(__name__)

DB_PATH: Path = Path(__file__).parent.parent / "financial_facts.duckdb"


# =============================================================================
# Schema
# =============================================================================
# Kept as module constants so the shape of the data is readable in one place,
# matching services.db's convention. Every CREATE is IF NOT EXISTS, so
# init_db() is safe on every startup.

_SCHEMA_FACTS_SEQ = "CREATE SEQUENCE IF NOT EXISTS financial_facts_id_seq START 1"
_SCHEMA_FOOTNOTE_SEQ = "CREATE SEQUENCE IF NOT EXISTS footnote_links_id_seq START 1"

# UNIQUE on the natural key (ticker, concept, fiscal_year, fiscal_period,
# is_instant) is what makes upsert_facts() an idempotent UPSERT rather than
# an append-only log that duplicates on every re-fetch of the same period.
_SCHEMA_FINANCIAL_FACTS = """
CREATE TABLE IF NOT EXISTS financial_facts (
    id                BIGINT PRIMARY KEY,
    ticker            VARCHAR NOT NULL,
    cik               BIGINT,
    statement         VARCHAR NOT NULL,   -- 'balance_sheet' | 'income_statement' | 'cash_flow'
    concept           VARCHAR NOT NULL,   -- e.g. 'us-gaap:Revenues'
    label             VARCHAR,            -- human label, e.g. 'Revenue'
    value             DOUBLE,
    unit              VARCHAR,            -- 'USD' | 'USD/shares' | 'shares'
    fiscal_year       INTEGER,
    fiscal_period     VARCHAR,            -- 'Q1'..'Q4' | 'FY'
    period_start      DATE,
    period_end        DATE,
    is_instant        BOOLEAN NOT NULL,
    form_type         VARCHAR,            -- '10-K' | '10-Q'
    accession_number  VARCHAR,
    source            VARCHAR,            -- 'xbrl-direct' | 'xbrl-derived-q4'
    ingested_at       TIMESTAMP NOT NULL,
    UNIQUE (ticker, concept, fiscal_year, fiscal_period, is_instant)
)
"""

_SCHEMA_FOOTNOTE_LINKS = """
CREATE TABLE IF NOT EXISTS statement_footnote_links (
    id              BIGINT PRIMARY KEY,
    ticker          VARCHAR NOT NULL,
    period_key      VARCHAR NOT NULL,   -- e.g. 'FY2025', 'Q2 FY2026' — matches CompanyStore period_key
    statement_item  VARCHAR NOT NULL,   -- line-item label, e.g. 'Long-term Debt'
    note_id         VARCHAR NOT NULL,   -- e.g. 'note_7'
    note_title      VARCHAR,            -- e.g. 'Debt'
    note_text       VARCHAR,            -- extracted footnote body
    source_url      VARCHAR,
    UNIQUE (ticker, period_key, statement_item, note_id)
)
"""

_SCHEMA_STATEMENTS = [
    _SCHEMA_FACTS_SEQ,
    _SCHEMA_FOOTNOTE_SEQ,
    _SCHEMA_FINANCIAL_FACTS,
    _SCHEMA_FOOTNOTE_LINKS,
]

_FACT_COLUMNS = (
    "ticker", "cik", "statement", "concept", "label", "value", "unit",
    "fiscal_year", "fiscal_period", "period_start", "period_end", "is_instant",
    "form_type", "accession_number", "source", "ingested_at",
)
_FOOTNOTE_COLUMNS = (
    "ticker", "period_key", "statement_item", "note_id", "note_title",
    "note_text", "source_url",
)


# =============================================================================
# Connection management
# =============================================================================

_connection: duckdb.DuckDBPyConnection | None = None
_connection_lock = threading.Lock()
_lock = threading.Lock()   # guards every statement — see module docstring


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return the process-wide connection, opening it on first use."""
    global _connection
    if _connection is None:
        with _connection_lock:
            if _connection is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _connection = duckdb.connect(str(DB_PATH))
                logger.info(f"Opened structured facts database: {DB_PATH}")
    return _connection


@contextmanager
def transaction() -> Iterator[duckdb.DuckDBPyConnection]:
    """
    Run a write inside a single serialized transaction. Commits on success,
    rolls back on any exception, and holds the module lock for the duration
    so two concurrent writers cannot interleave.

    Usage::

        with transaction() as conn:
            conn.execute("INSERT INTO financial_facts (...) VALUES (...)", params)
    """
    conn = get_connection()
    with _lock:
        conn.begin()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """Create the schema if it isn't there yet. Idempotent — safe on every startup."""
    with transaction() as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
    logger.info(
        "Structured facts database schema ready "
        "(financial_facts, statement_footnote_links)."
    )


def close_db() -> None:
    """Close the connection, if one was opened. Used on shutdown."""
    global _connection
    with _connection_lock:
        if _connection is not None:
            _connection.close()
            _connection = None


# =============================================================================
# Writes
# =============================================================================

def upsert_facts(rows: list[dict]) -> int:
    """
    Bulk upsert into financial_facts. Each dict must have the keys in
    _FACT_COLUMNS (missing keys default to None except `ingested_at`, which
    defaults to now). Matches on the natural key (ticker, concept,
    fiscal_year, fiscal_period, is_instant) — a re-fetch of the same period
    updates the existing row in place rather than duplicating it.

    Returns the number of rows written (0 if `rows` is empty).
    """
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        for row in rows:
            values = [row.get(c) for c in _FACT_COLUMNS]
            if values[_FACT_COLUMNS.index("ingested_at")] is None:
                values[_FACT_COLUMNS.index("ingested_at")] = now
            conn.execute(
                f"""
                INSERT INTO financial_facts (id, {", ".join(_FACT_COLUMNS)})
                VALUES (nextval('financial_facts_id_seq'), {", ".join(["?"] * len(_FACT_COLUMNS))})
                ON CONFLICT (ticker, concept, fiscal_year, fiscal_period, is_instant)
                DO UPDATE SET
                    cik = excluded.cik, label = excluded.label, value = excluded.value,
                    unit = excluded.unit, period_start = excluded.period_start,
                    period_end = excluded.period_end, form_type = excluded.form_type,
                    accession_number = excluded.accession_number, source = excluded.source,
                    ingested_at = excluded.ingested_at
                """,
                values,
            )
    return len(rows)


def upsert_footnote_links(rows: list[dict]) -> int:
    """
    Bulk upsert into statement_footnote_links. Each dict must have the keys
    in _FOOTNOTE_COLUMNS. Matches on (ticker, period_key, statement_item,
    note_id) — idempotent on re-parsing the same filing.
    """
    if not rows:
        return 0
    with transaction() as conn:
        for row in rows:
            values = [row.get(c) for c in _FOOTNOTE_COLUMNS]
            conn.execute(
                f"""
                INSERT INTO statement_footnote_links (id, {", ".join(_FOOTNOTE_COLUMNS)})
                VALUES (nextval('footnote_links_id_seq'), {", ".join(["?"] * len(_FOOTNOTE_COLUMNS))})
                ON CONFLICT (ticker, period_key, statement_item, note_id)
                DO UPDATE SET
                    note_title = excluded.note_title, note_text = excluded.note_text,
                    source_url = excluded.source_url
                """,
                values,
            )
    return len(rows)


# =============================================================================
# Reads
# =============================================================================

def _rows_as_dicts(conn: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    result = conn.execute(sql, params)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, r)) for r in result.fetchall()]


def query_facts(
    ticker: str,
    concepts: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    fiscal_periods: list[str] | None = None,
) -> list[dict]:
    """
    All financial_facts rows for a ticker, optionally narrowed to specific
    concepts, fiscal years, and/or fiscal periods ('Q1'..'Q4'|'FY'; added for
    orchestration.tools.sql_tool, which needs to ask for e.g. just Q1 of a
    year, not every period that year has). Ordered chronologically.
    """
    sql = "SELECT * FROM financial_facts WHERE ticker = ?"
    params: list = [(ticker or "").strip().upper()]
    if concepts:
        sql += f" AND concept IN ({', '.join(['?'] * len(concepts))})"
        params.extend(concepts)
    if fiscal_years:
        sql += f" AND fiscal_year IN ({', '.join(['?'] * len(fiscal_years))})"
        params.extend(fiscal_years)
    if fiscal_periods:
        sql += f" AND fiscal_period IN ({', '.join(['?'] * len(fiscal_periods))})"
        params.extend(fiscal_periods)
    sql += " ORDER BY fiscal_year, fiscal_period, is_instant"
    conn = get_connection()
    with _lock:
        return _rows_as_dicts(conn, sql, params)


def query_footnotes(
    ticker: str, period_key: str, statement_item: str | None = None
) -> list[dict]:
    """
    statement_footnote_links rows for one company/period, optionally
    narrowed to one statement line item.

    `statement_item` matches case-insensitively and as a substring in either
    direction (not an exact ``=``): the caller is typically an LLM-generated
    free-text description (orchestration.tools.footnote_tool), which won't
    reliably reproduce the exact casing/wording parsers.sec_html_parser
    extracted from the filing HTML (e.g. "commitments and contingencies"
    from a planner vs. the stored "Commitments and Contingencies") — found
    live via Phase 5's own verification, where an exact-match filter here
    silently returned [] despite the row genuinely existing.
    """
    sql = "SELECT * FROM statement_footnote_links WHERE ticker = ? AND period_key = ?"
    params: list = [(ticker or "").strip().upper(), period_key]
    if statement_item:
        needle = statement_item.strip().lower()
        sql += " AND (INSTR(LOWER(statement_item), ?) > 0 OR INSTR(?, LOWER(statement_item)) > 0)"
        params.extend([needle, needle])
    conn = get_connection()
    with _lock:
        return _rows_as_dicts(conn, sql, params)
