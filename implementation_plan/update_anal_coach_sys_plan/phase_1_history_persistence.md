# Phase 1: History Persistence & Ticker Independence

**Goal**: Decouple analysis history and ticker discovery from the volatile in-memory `DocumentStore` so all past analyses persist and load seamlessly on page load without requiring manual SEC filing re-fetching.

---

## Background & Problem

Currently:
1. `App.tsx` loads available tickers solely via `GET /companies`, which queries the backend's volatile in-memory `DocumentStore`.
2. When the backend restarts or a new browser session opens, `DocumentStore` is empty, causing `activeTicker` to default to `null`.
3. `DeepAnalysis.tsx` suppresses the history sidebar when `ticker` is `null`, even though past analysis runs are fully preserved on disk in `backend/analysis_history/{TICKER}_{run_id}.json`.
4. The user is forced to re-fetch SEC filings just to populate `DocumentStore` before past history can be viewed.

---

## Tasks

### 1. Backend History Store Enhancement (`backend/rag/history_store.py`)
- Update `get_analysis_history(ticker: str | None = None, limit: int = 50) -> list[dict]`:
  - When `ticker` is provided: filter by `f"{_safe(ticker)}_*.json"` as before.
  - When `ticker` is `None` or omitted: scan **all** `*.json` files in `_HISTORY_DIR`, parse lightweight summaries via `_summary()`, sort descending by encoded run timestamp, and return the most recent `limit` entries.
- Add `get_latest_analysis(ticker: str) -> dict | None`:
  - Quickly retrieves the most recent full analysis record for a given ticker from disk without iterating the entire directory.
- Verify `list_tickers()` returns all distinct tickers with run counts: `[{"ticker": "MRVL", "runs": 1}, ...]`.

### 2. Backend Router Updates (`backend/routers/analysis.py`)
- Update `GET /analysis/history`:
  - Change parameter from `ticker: str = Query(...)` to `ticker: str | None = Query(None, description="Ticker symbol (optional; returns recent runs across all tickers if omitted)")`.
  - Pass `ticker` to `history_store.get_analysis_history(ticker, limit=limit)`.
- Ensure `GET /analysis/tickers` endpoint is clearly exposed and documented for frontend discovery.

### 3. Frontend Global Ticker Aggregation (`frontend/src/App.tsx`)
- Enhance `refreshCompanies()` to aggregate tickers from three persistent and transient sources:
  1. `getCompanies()`: in-memory filings from active session.
  2. `getAnalysisTickers()` (`GET /analysis/tickers`): all tickers with saved analyses on disk.
  3. `getPortfolio()` (`GET /portfolio`): all tickers held in SQLite portfolio.
- Deduplicate and sort the merged ticker list into `availableTickers`.
- On cold boot (when in-memory filings are empty):
  - Instead of resetting `activeTicker` to `null`, auto-select the most recently analyzed ticker from `getAnalysisHistory()` or the first portfolio holding.

### 4. Frontend Deep Analysis History UI (`frontend/src/views/DeepAnalysis.tsx`)
- Update `loadHistory`:
  - If `ticker` is selected, load history for that ticker: `getAnalysisHistory(ticker)`.
  - If `ticker` is `null`, call `getAnalysisHistory()` to load global past analyses across all companies.
- History Sidebar rendering:
  - When showing multi-ticker history, render a clear company/ticker badge on each history card.
  - Clicking any historical item:
    - Calls `getAnalysis(run_id)` to load the full record into `viewingPast`.
    - Automatically syncs `setActiveTicker(h.ticker)` in `DashboardContext`.
    - Displays the complete report, 3-axis scores, manager verdict, and agent details **instantly without requesting SEC re-fetch**.
- Add persistent status banner when viewing archived reports:
  - *"Viewing archived analysis from [Date] for [Ticker]. [Run Fresh Analysis] [Fetch Fresh SEC]"*.

---

## Verification & Acceptance Criteria

1. **Cold Boot Verification**:
   - Restart the backend server (`uvicorn`) and refresh the browser.
   - Enter the Deep Analysis tab without uploading or fetching any SEC filings.
   - Confirm all previous runs (`MRVL`, `MU`, `RKLB`) are immediately visible in the history sidebar.
2. **Instant Archival Load**:
   - Click a past analysis card in the sidebar.
   - Confirm the full report, executive summary, debate log, and scores render immediately without network errors or prompt to fetch SEC.
3. **Context Synchronization**:
   - Confirm that selecting a past run updates `activeTicker` across the application shell header.
