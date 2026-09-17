# Phase 1 — Storage Foundations

Stand up the four storage components as empty, schema-defined stores. No
ingestion logic here — that's Phase 2. This phase is safe to merge on its own:
it adds new files and one new dependency, touches nothing existing.

---

## [NEW] `backend/services/data_lake.py`

Immutable raw-document archive. Every raw artifact this app ever fetches
(filing HTML, transcript source text, raw news JSON) gets written here
**before** any parsing — the audit trail the current JSON caches don't provide
(those store the *parsed* result, not the original bytes).

```python
_LAKE_ROOT = Path(__file__).parent.parent / "data_lake"

def save_raw(ticker: str, doc_type: str, period_label: str, ext: str, content: bytes | str) -> Path:
    """
    Write raw content to data_lake/{TICKER}/{doc_type}/{period_label}.{ext}.
    Immutable: if the path already exists, this is a no-op (SEC filings and
    past news/transcripts never change once fetched) — returns the existing
    path without rewriting. Never raises; logs and returns the path either way.
    """

def get_raw(ticker: str, doc_type: str, period_label: str, ext: str) -> bytes | None: ...

def list_raw(ticker: str, doc_type: str | None = None) -> list[Path]: ...
```

`doc_type` values (fixed vocabulary, used consistently in later phases):
`sec_filing_html`, `sec_filing_pdf` (already exists today as the staged
upload — Phase 2 just also archives it here), `earnings_transcript`,
`news_article`, `insider_json`.

Path convention matches the diagram exactly: `raw/{ticker}/{doc_type}/{year}_{period}.{ext}`.

---

## [NEW] `backend/services/structured_db.py`

DuckDB connection + schema, modeled on `services/db.py`'s design (single
process-wide connection, `init_db()` idempotent, a `transaction()`
contextmanager for writes). DuckDB is embedded and single-process like SQLite,
so the same concurrency story applies.

```python
DB_PATH = Path(__file__).parent.parent / "financial_facts.duckdb"

_SCHEMA_FINANCIAL_FACTS = """
CREATE TABLE IF NOT EXISTS financial_facts (
    id              BIGINT PRIMARY KEY,
    ticker          VARCHAR NOT NULL,
    cik             BIGINT,
    statement       VARCHAR NOT NULL,   -- 'balance_sheet' | 'income_statement' | 'cash_flow'
    concept         VARCHAR NOT NULL,   -- e.g. 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'
    label           VARCHAR,            -- human label, e.g. 'Revenue'
    value           DOUBLE,
    unit            VARCHAR,            -- 'USD' | 'USD/shares' | 'shares'
    fiscal_year     INTEGER,
    fiscal_period   VARCHAR,            -- 'Q1'..'Q4' | 'FY'
    period_start    DATE,
    period_end      DATE,
    is_instant      BOOLEAN NOT NULL,
    form_type       VARCHAR,            -- '10-K' | '10-Q'
    accession_number VARCHAR,
    source          VARCHAR,            -- 'xbrl-direct' | 'xbrl-derived-q4' (see providers/edgar_xbrl.py)
    ingested_at     TIMESTAMP NOT NULL,
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
```

The `UNIQUE` constraints are what make Phase 2's ingestion an **upsert**
(`INSERT ... ON CONFLICT DO UPDATE`), not an append-only log that duplicates
on every re-fetch.

Public API mirrors `services/db.py`'s shape:

```python
def init_db() -> None: ...
@contextmanager
def transaction() -> Iterator[duckdb.DuckDBPyConnection]: ...
def upsert_facts(rows: list[dict]) -> int: ...          # bulk upsert, returns count
def upsert_footnote_links(rows: list[dict]) -> int: ...
def query_facts(ticker: str, concepts: list[str] | None = None,
                 fiscal_years: list[int] | None = None) -> list[dict]: ...
def query_footnotes(ticker: str, period_key: str, statement_item: str | None = None) -> list[dict]: ...
```

---

## [NEW] `backend/services/search_index.py`

SQLite FTS5 (BM25) index, separate file from `portfolio.db` (that file is
scoped to the user's account data per its own docstring — don't mix concerns).

```python
DB_PATH = Path(__file__).parent.parent / "search_index.db"

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
    doc_id       TEXT PRIMARY KEY,   -- SAME id used in Chroma — the join key for RRF fusion
    ticker       TEXT NOT NULL,
    doc_type     TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
)
"""
```

Public API:

```python
def init_db() -> None: ...
def index_chunk(doc_id: str, text: str, ticker: str, doc_type: str, metadata: dict) -> None:
    """Upsert into both chunk_fts and chunk_registry (delete+insert — FTS5 has no native upsert)."""
def search_bm25(query: str, *, ticker: str | None = None, doc_type: str | None = None,
                 k: int = 20) -> list[dict]:
    """Returns [{doc_id, text, metadata, bm25_score}], ranked by FTS5's bm25() function."""
def delete_chunks(doc_id_prefix: str) -> int: ...
```

**Verify FTS5 is compiled into this Python's `sqlite3` before building on it**
(some distributions ship without it):

```python
import sqlite3
con = sqlite3.connect(":memory:")
con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")  # raises OperationalError if unsupported
```

If unsupported on this machine's Python, the fallback is `pip install
sqlite-utils` or building against a newer bundled SQLite — flag this as a
blocking check at the start of Phase 1, not something to discover in Phase 3.

---

## [NEW] `backend/services/registry_db.py`

The Redis substitute: near-duplicate detection registry, entity-alias cache,
and a generic TTL cache table. One more small SQLite file, kept separate from
`search_index.db` because its rows churn/expire differently (this is
operational metadata, not content).

```python
DB_PATH = Path(__file__).parent.parent / "registry.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_dedup (
    article_id    TEXT PRIMARY KEY,
    simhash       INTEGER NOT NULL,
    canonical_id  TEXT NOT NULL,      -- points at the article_id this one is a duplicate of (self if canonical)
    url           TEXT,
    ticker        TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias             TEXT PRIMARY KEY,   -- normalized company name or alt-name
    canonical_ticker  TEXT NOT NULL,
    cik               INTEGER,
    source            TEXT                -- 'sec_ticker_map' | 'manual' | 'learned'
);
CREATE TABLE IF NOT EXISTS hot_cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""
```

Public API:

```python
def init_db() -> None: ...
def find_near_duplicate(simhash: int, ticker: str, *, max_hamming: int = 3) -> str | None:
    """Scan this ticker's recent news_dedup rows for a hash within max_hamming bits.
    O(n) over one ticker's rows is fine at this scale — no LSH needed yet."""
def register_article(article_id: str, simhash: int, canonical_id: str, url: str, ticker: str) -> None: ...
def resolve_alias(name: str) -> str | None:  # -> ticker, or None
def learn_alias(alias: str, ticker: str, cik: int | None, source: str = "learned") -> None: ...
def cache_get(key: str) -> dict | None: ...   # None if missing or expired
def cache_set(key: str, value: dict, ttl_seconds: int) -> None: ...
```

---

## [MODIFY] `backend/requirements.txt`

Add: `duckdb`. (FTS5/registry use stdlib `sqlite3` — no new dependency.)

---

## [MODIFY] `backend/main.py`

Call the three new `init_db()` functions at startup, next to wherever
`services.db.init_db()` (portfolio) is already called:

```python
from services import structured_db, search_index, registry_db
...
@app.on_event("startup")
async def startup():
    ...
    structured_db.init_db()
    search_index.init_db()
    registry_db.init_db()
```

---

## Execution Order

| Step | Description |
|---|---|
| 1 | Verify FTS5 support in this Python's sqlite3 (blocking check) |
| 2 | `pip install duckdb`, add to `requirements.txt` |
| 3 | Create `services/data_lake.py` |
| 4 | Create `services/structured_db.py` (DuckDB) |
| 5 | Create `services/search_index.py` (FTS5) |
| 6 | Create `services/registry_db.py` |
| 7 | Wire `init_db()` calls into `main.py` startup |

## Verification Plan

- `python -c "from services import structured_db; structured_db.init_db(); print('ok')"` — DB file created at `backend/financial_facts.duckdb`.
- Same for `search_index.init_db()` and `registry_db.init_db()`.
- Manually `upsert_facts([...one fake row...])` then `query_facts(...)` round-trips.
- Manually `index_chunk(...)` then `search_bm25(...)` round-trips and actually ranks a keyword match above a non-match.
- Restart the server, confirm `init_db()` calls are idempotent (no errors on existing files).
- No existing endpoint's behavior changes — this phase adds files only.
