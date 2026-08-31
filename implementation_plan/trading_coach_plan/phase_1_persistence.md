# Phase 1: Durable Persistence Layer

**Goal**: Introduce the app's first restart-surviving store, so the trading journal
can exist at all. Everything in `services/storage.py` today is an in-memory dict
wiped on shutdown; a portfolio must outlive the process.

**Decision assumed**: SQLite via the stdlib `sqlite3` module, no ORM. It needs no new
dependency, no server, and gives transactions — which matter because recomputing a
holding's average price on each trade is a read-modify-write that must not interleave.
Swapping to Postgres later means rewriting only `services/db.py`; phases 2-6 depend on
the repository functions, not the driver.

## Tasks:

1. **Create `backend/services/db.py`**
   - `DB_PATH = Path(__file__).parent.parent / "portfolio.db"` — sits beside
     `backend/analysis_history/`, the existing on-disk store.
   - `get_connection() -> sqlite3.Connection`: open with
     `check_same_thread=False`, `row_factory = sqlite3.Row`, and
     `PRAGMA foreign_keys = ON` (off by default in SQLite — without it the
     `trades → holdings` cascade below is silently unenforced).
   - `init_db() -> None`: idempotent `CREATE TABLE IF NOT EXISTS` for the schema
     in task 2. Safe to call on every startup.
   - Guard writes with a `threading.Lock`, since FastAPI serves requests from a
     threadpool and SQLite writers are single at a time.

2. **Define the schema** (in `db.py` as SQL constants)
   - `holdings`: `id INTEGER PRIMARY KEY`, `ticker TEXT NOT NULL UNIQUE` (stored
     upper-case to match `DocumentStore._normalize`), `quantity REAL NOT NULL`,
     `avg_price REAL NOT NULL`, `initial_fx_rate REAL`, `currency TEXT DEFAULT 'USD'`,
     `created_at TEXT`, `updated_at TEXT`.
   - `trades`: `id INTEGER PRIMARY KEY`, `ticker TEXT NOT NULL`,
     `side TEXT NOT NULL CHECK(side IN ('buy','sell'))`,
     `quantity REAL NOT NULL`, `executed_at TEXT NOT NULL` (ISO-8601 UTC),
     `execution_price REAL`, `total_value REAL`, `fx_rate REAL`,
     `entry_rationale TEXT`, `avg_price_after REAL`, `created_at TEXT`,
     plus `FOREIGN KEY(ticker) REFERENCES holdings(ticker) ON DELETE CASCADE`.
   - Index `trades(ticker, executed_at)` — the Coach agent (phase 6) reads a
     ticker's history in time order on every run.
   - Store timestamps as ISO-8601 UTC **strings**, matching the convention already
     used in `rag/history_store.py`.

3. **Wire lifecycle in `backend/main.py`**
   - Call `db.init_db()` on startup (add an `@app.on_event("startup")` hook).
   - Do **NOT** add `portfolio.db` to the shutdown `cleanup()` hook — that hook
     deletes temp dirs, and this file must persist. This is the one place the new
     store deliberately breaks the existing pattern.
   - Add `backend/portfolio.db` to `.gitignore`.

4. **Add a DI provider**
   - Follow the existing pattern at the bottom of `services/storage.py`
     (`get_document_store`, `get_media_cache`, `get_debate_store`): expose
     `get_db()` so routers use `Depends(get_db)` rather than importing the
     connection directly.

## Definition of done
- Server starts, `backend/portfolio.db` is created with both tables.
- Restarting the server preserves rows inserted via a direct `sqlite3` write.
- `python -m py_compile` clean; existing app import and MAS endpoints unaffected.
