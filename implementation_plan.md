# Replace Upper Pane with XBRL Data from SEC EDGAR

## Problem
The current pdfplumber-based table extraction is unreliable — it blindly takes column[1] as the value, but SEC tables often have blank columns, `$` markers, or multi-column layouts. This causes blank values in the upper pane.

## Solution
Use the SEC EDGAR **Company Facts API** (`https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`) to fetch machine-readable XBRL financial data. This gives exact numbers with standardized concept names — no PDF parsing needed.

## Proposed Changes

### New Module — edgar_xbrl.py

#### [NEW] [edgar_xbrl.py](file:///c:/Users/mrsim/FinAnalSupTool/backend/edgar_xbrl.py)

New module with all EDGAR/XBRL logic:

1. **`fetch_ticker_to_cik_map()`** — Downloads SEC's `company_tickers.json` (cached in memory). Maps tickers → CIK numbers.

2. **`detect_cik_from_pdf(pdf_path)`** — Tries to detect the company CIK from the PDF:
   - First: extract ticker from filename (e.g., `mrvl-20250802.pdf` → `mrvl`)
   - Fallback: search for company name in the first pages and fuzzy-match against the ticker map

3. **`fetch_company_facts(cik)`** — Calls the EDGAR API to get all XBRL facts for a CIK. Cached per-CIK (the first upload for a company fetches; subsequent uploads reuse).

4. **`build_financial_tables(facts, period_end_dates)`** — Builds DataFrames for each statement type by:
   - Using curated concept mappings (30+ key line items per statement)
   - Matching facts to the uploaded filing periods via `end` date
   - Formatting values (raw integers → human-readable like `$1,234.5M`)

5. **Curated concept mappings** — Defines the key financial line items:

   **Balance Sheet** (~15 items): Total Assets, Current Assets, Cash, Receivables, Inventory, Total Liabilities, Current Liabilities, Accounts Payable, Long-term Debt, Stockholders' Equity, etc.

   **Income Statement** (~12 items): Revenue, Cost of Revenue, Gross Profit, R&D, SG&A, Operating Income, Interest Expense, Income Tax, Net Income, EPS (Basic/Diluted), etc.

   **Cash Flow** (~6 items): Operating Activities, Investing Activities, Financing Activities, Net Change in Cash, CapEx, etc.

   Each item has **multiple XBRL concept aliases** since companies use different US-GAAP tags (e.g., Revenue can be `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, or `SalesRevenueNet`).

---

### Backend — pdf_utils.py

#### [MODIFY] [pdf_utils.py](file:///c:/Users/mrsim/FinAnalSupTool/backend/pdf_utils.py)

- Keep all existing pdfplumber code (it becomes the fallback for non-SEC filings in the future)
- No functional changes needed — just keep as-is

---

### Backend — main.py

#### [MODIFY] [main.py](file:///c:/Users/mrsim/FinAnalSupTool/backend/main.py)

- Import the new `edgar_xbrl` module
- In the upload handler, **before** pdfplumber extraction:
  1. Try to detect CIK from the PDF
  2. If found, fetch XBRL data from EDGAR
  3. Build financial tables from XBRL
  4. Store in `_table_store` (same structure as before)
  5. Skip pdfplumber table extraction (since XBRL is better)
  6. Fall back to pdfplumber if CIK detection or EDGAR fetch fails
- Store the detected CIK in `_filing_meta` so subsequent uploads for the same company reuse cached data
- Update `_rebuild_merged_tables()` — works the same, but now the data comes from XBRL

---

### Backend — requirements.txt

#### [MODIFY] [requirements.txt](file:///c:/Users/mrsim/FinAnalSupTool/backend/requirements.txt)

- Add `httpx` (async HTTP client for EDGAR API calls)

---

### Frontend — No Changes

The `/financials` endpoint response shape stays identical:
```json
{
  "statement_type": "balance_sheet",
  "columns": ["Line Item", "2025-10-Q", "2026-10-Q"],
  "rows": [{"Line Item": "Total Assets", "2025-10-Q": "20,230.8", ...}]
}
```

The frontend will display the same table — just with better, more complete data.

## Key Design Decisions

1. **XBRL-first, pdfplumber fallback** — If EDGAR fetch fails, we fall back to pdfplumber silently
2. **Company facts cached per CIK** — One EDGAR API call gives ALL historical data for a company
3. **Period matching by `end` date** — We match XBRL facts to uploaded filings using the period end date detected from the PDF cover page
4. **Values formatted as millions** — Raw XBRL values (in raw USD) are divided by 1M and formatted for readability (e.g., `1,234.5`)

> [!IMPORTANT]
> **SEC EDGAR requires a `User-Agent` header** with a company name and email. I'll use a generic placeholder like `"FinAnalSupTool dev@finanalst.local"`. You can change this in the code later.

## Verification Plan

### Manual Verification
- Upload Marvell 10-Q PDFs → verify CIK detected as `1058290`
- Check upper pane shows complete balance sheet, income statement, cash flow data
- Verify values match the actual filing numbers
- Test with a different company to confirm CIK lookup works
