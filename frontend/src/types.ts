/**
 * types.ts
 * ────────
 * TypeScript interfaces that mirror the backend's Pydantic models.
 *
 * These ensure type safety when consuming API responses.
 * Each interface corresponds to a response shape from one of the
 * FastAPI endpoints defined in backend/schemas.py.
 */

// =============================================================================
// Upload Endpoint Types (POST /upload)
// =============================================================================

/** Metadata for a single processed filing (one per uploaded PDF). */
export interface FilingMeta {
  filename: string;
  detected_period: string | null;  // e.g., "2023-10K"
  form_type: string | null;        // "10-K" or "10-Q"
  status: "success" | "partial" | "failed";
  message: string | null;          // Human-readable status detail
}

/** Response from POST /upload — batch upload results. */
export interface UploadResponse {
  total_files: number;
  filings: FilingMeta[];
}

// =============================================================================
// Financials Endpoint Types (GET /financials)
// =============================================================================

/** Merged financial table with cross-period outer join. */
export interface FinancialTableResponse {
  statement_type: string;         // "balance_sheet", "income_statement", "cash_flow"
  columns: string[];              // ["Line Item", "2023-10K", "2022-10K", ...]
  rows: Record<string, string | null>[];  // Array of row objects
}

// =============================================================================
// Filing Text Endpoint Types (GET /filing-text)
// =============================================================================

/** Extracted text section from a specific filing period. */
export interface FilingTextResponse {
  period: string;     // e.g., "2023-10K"
  section: string;    // e.g., "mda"
  title: string;      // Human-readable section title
  content: string | null;
}

// =============================================================================
// Periods Endpoint Types (GET /periods)
// =============================================================================

/** Metadata for one uploaded filing period (for dropdown population). */
export interface PeriodInfo {
  period_key: string;             // e.g., "2023-10K"
  form_type: string | null;
  period: string | null;          // Full period string, e.g., "December 31, 2023"
  filename: string | null;
}

/** Response from GET /periods. */
export interface PeriodsResponse {
  periods: PeriodInfo[];
}
