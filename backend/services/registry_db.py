"""
services.registry_db
──────────────────────
The local, zero-ops substitute for Redis in the hybrid retrieval
architecture (see implementation_plan/new_db_architecture_plan/): near-
duplicate news detection, a company-name → ticker alias cache, and a generic
TTL cache table. SQLite, kept separate from search_index.db (this is
operational/dedup metadata, not searchable content) and from portfolio.db
(unrelated to the user's own account data).

Schema + CRUD only — Phase 1 of the migration. Nothing calls
find_near_duplicate()/register_article() yet; Phase 2 wires SimHash dedup
and entity tagging into the news ingestion track.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from providers.news_dedup import hamming_distance

logger = logging.getLogger(__name__)

DB_PATH: Path = Path(__file__).parent.parent / "registry.db"


# =============================================================================
# Schema
# =============================================================================

_SCHEMA_NEWS_DEDUP = """
CREATE TABLE IF NOT EXISTS news_dedup (
    article_id   TEXT PRIMARY KEY,
    simhash      INTEGER NOT NULL,
    canonical_id TEXT NOT NULL,   -- points at the article_id this one is a duplicate of (self if canonical)
    url          TEXT,
    ticker       TEXT,
    created_at   TEXT NOT NULL
)
"""

# The dedup scan in find_near_duplicate() is scoped to one ticker's rows —
# this index is what keeps that scan cheap as the table grows.
_SCHEMA_NEWS_DEDUP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_news_dedup_ticker ON news_dedup (ticker, created_at)
"""

_SCHEMA_ENTITY_ALIASES = """
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias             TEXT PRIMARY KEY,   -- normalized company name or alt-name
    canonical_ticker  TEXT NOT NULL,
    cik               INTEGER,
    source            TEXT                -- 'sec_ticker_map' | 'manual' | 'learned'
)
"""

_SCHEMA_HOT_CACHE = """
CREATE TABLE IF NOT EXISTS hot_cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
)
"""

_SCHEMA_STATEMENTS = [
    _SCHEMA_NEWS_DEDUP,
    _SCHEMA_NEWS_DEDUP_INDEX,
    _SCHEMA_ENTITY_ALIASES,
    _SCHEMA_HOT_CACHE,
]


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
                logger.info(f"Opened registry database: {DB_PATH}")
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
    logger.info("Registry database schema ready (news_dedup, entity_aliases, hot_cache).")


def close_db() -> None:
    """Close the connection, if one was opened. Used on shutdown."""
    global _connection
    with _connection_lock:
        if _connection is not None:
            _connection.close()
            _connection = None


# =============================================================================
# Near-duplicate news detection
# =============================================================================
# providers.news_dedup.simhash() produces a 64-bit UNSIGNED fingerprint
# (any bit 0-63 may be set), but SQLite's INTEGER column is signed 64-bit
# (-2^63 .. 2^63-1) — storing an unsigned value with bit 63 set raises
# "Python int too large to convert to SQLite INTEGER". These two helpers are
# the ONLY place that distinction matters: every function in this module
# past this point works with the canonical unsigned form Python already
# uses elsewhere (news_dedup.hamming_distance, the caller's own fingerprint),
# converting only at the SQLite read/write boundary.

_UINT64_MASK = 0xFFFFFFFFFFFFFFFF
_INT64_SIGN_BIT = 0x8000000000000000
_INT64_WRAP = 0x10000000000000000


def _to_sqlite_int64(v: int) -> int:
    v &= _UINT64_MASK
    return v - _INT64_WRAP if v >= _INT64_SIGN_BIT else v


def _from_sqlite_int64(v: int) -> int:
    return v & _UINT64_MASK


def find_near_duplicate(simhash: int, ticker: str, *, max_hamming: int = 10) -> str | None:
    """
    Scan this ticker's registered hashes for one within max_hamming bits of
    `simhash`, returning its canonical_id (the id near-duplicates should
    collapse onto), or None if this is genuinely new. O(n) over one ticker's
    rows — fine at this app's per-ticker news volume; an LSH bucket index is
    a future optimization, not needed yet.

    max_hamming=10 (of 64 bits) is calibrated against REAL Tavily results for
    one ticker over several weeks (providers.news_dedup.simhash operates on
    short title+snippet text, not full articles) — a first, looser attempt at
    this calibration (max_hamming=20, from two hand-picked example pairs) was
    verified WRONG against a larger real batch: dozens of genuinely distinct
    AVGO articles (earnings, Nvidia competition, credit risk, unrelated
    market roundups) measured 22-42 bits apart, while even a lightly-reworded
    real duplicate headline measured 14 — the two distributions OVERLAP for
    short text, so no threshold catches every paraphrase without also
    collapsing distinct articles. This value is deliberately on the strict
    side of that overlap: it reliably catches only near-identical text (the
    same wire copy syndicated verbatim, e.g. an AMP vs non-AMP URL of the
    same story measured 0 bits apart) and accepts missing loosely-reworded
    duplicates as the price of NEVER silently discarding a genuinely
    distinct article — a missed duplicate is cosmetic noise; a false
    collapse is real, silent data loss, which is the worse failure mode for
    an architecture whose whole point is "search the exact data fetched."
    """
    conn = get_connection()
    with _write_lock:
        rows = conn.execute(
            "SELECT simhash, canonical_id FROM news_dedup WHERE ticker = ?",
            ((ticker or "").strip().upper(),),
        ).fetchall()
    for row in rows:
        if hamming_distance(simhash, _from_sqlite_int64(row["simhash"])) <= max_hamming:
            return row["canonical_id"]
    return None


def register_article(article_id: str, simhash: int, canonical_id: str, url: str, ticker: str) -> None:
    """Record one article's hash + which canonical article it collapses onto
    (itself, if it's the canonical one). Upserts on article_id."""
    now = datetime.now(timezone.utc).isoformat()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO news_dedup (article_id, simhash, canonical_id, url, ticker, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(article_id) DO UPDATE SET "
            "simhash = excluded.simhash, canonical_id = excluded.canonical_id, "
            "url = excluded.url, ticker = excluded.ticker",
            (article_id, _to_sqlite_int64(simhash), canonical_id, url, (ticker or "").strip().upper(), now),
        )


# =============================================================================
# Entity alias cache
# =============================================================================

def resolve_alias(name: str) -> str | None:
    """The canonical ticker for a normalized company name/alias, or None if unknown."""
    key = (name or "").strip().lower()
    if not key:
        return None
    conn = get_connection()
    with _write_lock:
        row = conn.execute(
            "SELECT canonical_ticker FROM entity_aliases WHERE alias = ?", (key,)
        ).fetchone()
    return row["canonical_ticker"] if row else None


def learn_alias(alias: str, ticker: str, cik: int | None, source: str = "learned") -> None:
    """Record (or update) one alias → ticker mapping."""
    learn_aliases_bulk([(alias, ticker, cik, source)])


def learn_aliases_bulk(rows: list[tuple[str, str, int | None, str]]) -> int:
    """
    Bulk upsert many (alias, ticker, cik, source) tuples in ONE transaction —
    used by the one-time SEC ticker-map backfill (~10k rows,
    providers.entity_tagging.seed_entity_aliases), where committing once per
    row would be needlessly slow. Returns the number of rows attempted
    (including any skipped for a blank alias/ticker).
    """
    if not rows:
        return 0
    with transaction() as conn:
        for alias, ticker, cik, source in rows:
            key = (alias or "").strip().lower()
            if not key or not ticker:
                continue
            conn.execute(
                "INSERT INTO entity_aliases (alias, canonical_ticker, cik, source) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET "
                "canonical_ticker = excluded.canonical_ticker, cik = excluded.cik, source = excluded.source",
                (key, ticker.strip().upper(), cik, source),
            )
    return len(rows)


# =============================================================================
# Generic TTL cache
# =============================================================================

def cache_get(key: str) -> dict | None:
    """The cached value for `key`, or None if missing or expired (an expired
    row is left in place — not deleted here — so a bulk sweep can reclaim it
    later; reads never need to take a write lock for a delete)."""
    conn = get_connection()
    with _write_lock:
        row = conn.execute(
            "SELECT value_json, expires_at FROM hot_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    return json.loads(row["value_json"])


def cache_set(key: str, value: dict, ttl_seconds: int) -> None:
    """Store `value` under `key`, expiring after `ttl_seconds`."""
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO hot_cache (key, value_json, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value_json = excluded.value_json, expires_at = excluded.expires_at",
            (key, json.dumps(value, default=str), expires_at),
        )
