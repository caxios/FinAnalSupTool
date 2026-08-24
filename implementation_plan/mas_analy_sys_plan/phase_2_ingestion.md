# Phase 2: Ingestion Pipeline Updates

**Goal**: Route ingested PDFs to the correct company's store based on their ticker.

## Tasks:

1. **Update `ingest_pdf` signature**
   - In `backend/services/ingestion.py`, update `ingest_pdf` to identify the `ticker` from `detect_filing_metadata` or via `resolve_company_identity(detected_cik)`.
   - If a ticker is found, fetch the corresponding `CompanyStore` using `company_store = store.get_company_store(ticker)`.

2. **Redirect Data Insertion**
   - Update all references in `ingest_pdf` that previously appended to `store.*` to append to `company_store.*`.
   - For example, `store.table_store[period_key] = classified_tables` becomes `company_store.table_store[period_key] = classified_tables`.

3. **Update Rebuild Logic**
   - Ensure that `company_store.rebuild_merged_tables()` is called only for the updated ticker after an upload batch completes in `backend/routers/document.py`.
