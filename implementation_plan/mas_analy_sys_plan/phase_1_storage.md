# Phase 1: Storage Layer Isolation

**Goal**: Refactor the backend in-memory storage to isolate data per company (ticker) instead of using a single global namespace.

## Tasks:

1. **Create `CompanyStore` Class**
   - In `backend/services/storage.py`, create a new class `CompanyStore`.
   - Move the following attributes from `DocumentStore` into `CompanyStore`:
     - `text_store`
     - `table_store`
     - `filing_meta`
     - `merged_tables`
     - `metrics_store`
     - `page_map_store`
     - `upload_dir` (initialize a unique temp directory per ticker, e.g., `prefix=f"finanalst_{ticker}_"`)
   - Move `derive_period_key`, `order_period_columns`, and `rebuild_merged_tables` methods into `CompanyStore`.

2. **Update `DocumentStore`**
   - Refactor `DocumentStore` to maintain a dictionary of `CompanyStore` instances: `self.companies: dict[str, CompanyStore]`.
   - Add a method `get_company_store(self, ticker: str) -> CompanyStore` that returns the existing store for a ticker, or initializes and returns a new one if it doesn't exist.
   - Add a method `list_tickers(self) -> list[str]` to return all available companies.

3. **Update `DebateStore` and `MediaCache`**
   - Refactor `DebateStore` to store analysis records keyed by ticker: `self.data: dict[str, dict]`. Update `replace(self, ticker: str, payload: dict)` to swap data for a specific ticker.
   - Refactor `MediaCache` to store transcripts and media data keyed by ticker.
