"""
services.search_index
───────────────────────
SQLite FTS5 (BM25 keyword search) index — the keyword-precision half of the
hybrid retrieval architecture (see implementation_plan/new_db_architecture_plan/).
Paired with rag/vector_store.py's Chroma vectors: Phase 3's
rag/hybrid_search.py fuses the two via Reciprocal Rank Fusion, joined on the
`doc_id` every chunk shares between both stores.

Separate file from portfolio.db (services.db) — that database is scoped to
the user's own account/journal data per its own docstring; this is a content
search index, a different concern with a different write pattern (chunks are
rewritten wholesale on re-ingestion, never edited in place like a trade).

Schema + CRUD only — Phase 1 of the migration. Nothing calls index_chunk()
yet; Phase 2 wires the SEC/earnings/news ingestion tracks into this store.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

DB_PATH: Path = Path(__file__).parent.parent / "search_index.db"


# =============================================================================
# Schema
# =============================================================================

_SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    doc_id UNINDEXED,
    text,
    ticker UNINDEXED,
    doc_type UNINDEXED,      -- 'sec_filing_text' | 'earnings_transcript' | 'news_article' | 'youtube_transcript'
    period UNINDEXED,        -- period_key / quarter label, as applicable
    speaker_role UNINDEXED,  -- earnings only
    published_at UNINDEXED   -- news/youtube only
)
"""

_SCHEMA_REGISTRY = """
CREATE TABLE IF NOT EXISTS chunk_registry (
    doc_id        TEXT PRIMARY KEY,   -- SAME id used in Chroma — the join key for RRF fusion
    ticker        TEXT NOT NULL,
    doc_type      TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""

_SCHEMA_STATEMENTS = [_SCHEMA_FTS, _SCHEMA_REGISTRY]


# =============================================================================
# Connection management
# =============================================================================

_connection: sqlite3.Connection | None = None
_connection_lock = threading.Lock()
_write_lock = threading.Lock()


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")


def get_connection() -> sqlite3.Connection:
    """Return the process-wide connection, opening it on first use."""
    global _connection
    if _connection is None:
        with _connection_lock:
            if _connection is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                _configure(conn)
                _connection = conn
                logger.info(f"Opened search index database: {DB_PATH}")
    return _connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run a write inside a single serialized transaction, matching
    services.db's pattern exactly."""
    conn = get_connection()
    with _write_lock:
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
    logger.info("Search index database schema ready (chunk_fts, chunk_registry).")


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

def index_chunk(doc_id: str, text: str, ticker: str, doc_type: str, metadata: dict) -> None:
    """
    Upsert one chunk into both chunk_fts and chunk_registry. FTS5 has no
    native upsert, so this deletes any existing row for doc_id first (a
    no-op if it's new) then inserts fresh — inside one transaction, so a
    concurrent reader never observes doc_id as transiently missing.
    """
    now = datetime.now(timezone.utc).isoformat()
    period = metadata.get("period") or metadata.get("quarter") or ""
    speaker_role = metadata.get("speaker_role") or ""
    published_at = metadata.get("published_at") or metadata.get("published") or ""
    with transaction() as conn:
        conn.execute("DELETE FROM chunk_fts WHERE doc_id = ?", (doc_id,))
        conn.execute(
            "INSERT INTO chunk_fts (doc_id, text, ticker, doc_type, period, speaker_role, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, text, (ticker or "").upper(), doc_type, str(period), speaker_role, str(published_at)),
        )
        conn.execute(
            "INSERT INTO chunk_registry (doc_id, ticker, doc_type, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(doc_id) DO UPDATE SET "
            "ticker = excluded.ticker, doc_type = excluded.doc_type, "
            "metadata_json = excluded.metadata_json, created_at = excluded.created_at",
            (doc_id, (ticker or "").upper(), doc_type, json.dumps(metadata, default=str), now),
        )


def delete_chunks(doc_id_prefix: str) -> int:
    """Delete every chunk whose doc_id starts with the given prefix (e.g. to
    re-index one period from scratch). Returns the number of chunk_fts rows deleted."""
    with transaction() as conn:
        cur = conn.execute("DELETE FROM chunk_fts WHERE doc_id LIKE ?", (f"{doc_id_prefix}%",))
        conn.execute("DELETE FROM chunk_registry WHERE doc_id LIKE ?", (f"{doc_id_prefix}%",))
        return cur.rowcount


# =============================================================================
# Reads
# =============================================================================

def search_bm25(
    query: str, *, ticker: str | None = None, doc_type: str | None = None, k: int = 20
) -> list[dict]:
    """
    FTS5 MATCH query, ranked by SQLite's bm25() function (LOWER score = better
    match — this function already sorts ascending by score, so callers such as
    rag/hybrid_search.py can assign rank 1 to the first result directly).

    Returns [{doc_id, text, metadata, bm25_score}]. A blank/whitespace query
    returns [] rather than raising an FTS5 syntax error; a malformed FTS5
    query string (stray quotes, bare operators) is caught and logged the
    same way, returning [] rather than propagating.
    """
    q = (query or "").strip()
    if not q:
        return []
    sql = (
        "SELECT f.doc_id AS doc_id, f.text AS text, r.metadata_json AS metadata_json, "
        "bm25(chunk_fts) AS score "
        "FROM chunk_fts f JOIN chunk_registry r ON r.doc_id = f.doc_id "
        "WHERE chunk_fts MATCH ?"
    )
    params: list = [q]
    if ticker:
        sql += " AND f.ticker = ?"
        params.append(ticker.strip().upper())
    if doc_type:
        sql += " AND f.doc_type = ?"
        params.append(doc_type)
    sql += " ORDER BY score LIMIT ?"
    params.append(k)

    conn = get_connection()
    with _write_lock:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"[search_index] FTS5 query failed for {q!r}: {e}")
            return []
    return [
        {
            "doc_id": row["doc_id"],
            "text": row["text"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "bm25_score": row["score"],
        }
        for row in rows
    ]
