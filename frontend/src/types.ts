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

// =============================================================================
// Chat Endpoint Types (POST /chat)
// =============================================================================

/** One turn in the AI assistant conversation. */
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

/** Response from POST /chat — the assistant's answer. */
export interface ChatResponse {
  answer: string;
}

// =============================================================================
// Company Types (GET /company)
// =============================================================================

export interface CompanyInfo {
  cik: number | null;
  name: string | null;
  ticker: string | null;
  filing_count: number;
}

export interface CompanyResponse {
  primary: CompanyInfo | null;
  companies: CompanyInfo[];
}

// =============================================================================
// Media / Macro Types (GET /media/*, GET /macro/*)
// =============================================================================

export interface NewsArticle {
  title: string;
  url: string;
  source: string;
  snippet: string;
  published: string | null;
}

export interface NewsResponse {
  configured: boolean;
  scope: "company" | "macro";
  company: CompanyInfo | null;
  articles: NewsArticle[];
  message: string | null;
}

export interface Video {
  video_id: string;
  title: string;
  channel: string;
  url: string;
  embed_url: string;
  thumbnail: string | null;
  published: string | null;
  description: string;
}

export interface VideoResponse {
  configured: boolean;
  scope: "company" | "macro";
  videos: Video[];
  message: string | null;
}

export interface TranscriptResponse {
  available: boolean;
  video_id: string;
  text: string;
  language: string | null;
  summary: string | null;
  message: string | null;
}

export interface EarningsResponse {
  configured: boolean;
  company: CompanyInfo | null;
  year: number | null;
  quarter: number | null;
  found: boolean;
  transcript: string | null;
  source: string | null; // "investing.com" | "fool.com"
  url: string | null;
  title: string | null;
  published: string | null;
  message: string | null;
}

export interface ChannelInfo {
  channel_id: string;
  title: string;
  handle: string | null;
}

export interface ChannelsResponse {
  configured: boolean;
  channels: ChannelInfo[];
  message: string | null;
}

export interface SentimentIndicator {
  theme: string;
  direction: "bullish" | "neutral" | "bearish" | string;
  note: string;
}

export interface SentimentResponse {
  configured: boolean;
  label: string;
  score: number | null;
  summary: string;
  indicators: SentimentIndicator[];
  headline_count: number;
  message: string | null;
}

/**
 * A date-range selection for news/video feeds. Presets carry a `days`
 * look-back; the "custom" preset carries explicit `start`/`end` (YYYY-MM-DD).
 */
export interface NewsRange {
  preset: "1d" | "1w" | "1m" | "3m" | "6m" | "1y" | "custom";
  days?: number;
  start?: string;
  end?: string;
}
