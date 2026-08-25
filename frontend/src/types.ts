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
  ticker: string | null;           // Company store this filing was routed to
  status: "success" | "partial" | "failed";
  message: string | null;          // Human-readable status detail
}

/** Response from POST /upload — batch upload results. */
export interface UploadResponse {
  total_files: number;
  filings: FilingMeta[];
}

// =============================================================================
// SEC Auto-Fetch Types (POST /sec/fetch)
// =============================================================================

/**
 * Request body for POST /sec/fetch — pull every filing for a ticker/form over a
 * fiscal-year range from SEC EDGAR. For a 10-K that's one report per year; for a
 * 10-Q it's every available quarter (Q1-Q3) in the range.
 */
export interface SecFetchRequest {
  ticker: string;
  form_type: "10-K" | "10-Q";
  start_year: number;
  end_year: number;
  start_quarter?: number;   // 10-Q only (1-3); omit to start at Q1
  end_quarter?: number;     // 10-Q only (1-3); omit to end at Q3
}

/** Provenance of one filing SEC auto-fetch actually retrieved. */
export interface ResolvedFiling {
  ticker: string;
  form_type: string;
  period_label: string;         // "FY2022" or "FY2022 Q1"
  filing_date: string;          // YYYY-MM-DD
  accession_number: string | null;
  document_url: string;
}

/** Response from POST /sec/fetch — one result per attempted period. */
export interface SecFetchResponse {
  ticker: string;
  range_label: string;          // "2021–2024"
  total_files: number;          // periods attempted
  succeeded: number;            // periods ingested successfully
  filings: FilingMeta[];
  resolved_filings: ResolvedFiling[];
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

/** Response from GET /periods — scoped to one company. */
export interface PeriodsResponse {
  ticker: string;
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

// =============================================================================
// Deep Analysis (POST /analyze, /analyze/stream, GET /analysis/*)
// =============================================================================

/** Programmatic 3-axis gap scores. Any axis with no supporting agent is null. */
export interface ThreeAxisScores {
  fundamental_score: number | null;
  sentiment_score: number | null;
  technical_score: number | null;
  fundamental_sentiment_gap: number | null;
  fundamental_technical_gap: number | null;
  overall_signal: string;
  signal_label: string;
  signal_tone: "positive" | "negative" | "neutral" | string;
  components: Record<string, number | null>;
}

/** One agent's contribution to the sequential debate. */
export interface DebateArgument {
  agent_id: string;
  stance: number | string;
  argument: string;
  cited_evidence: string[];
}

export interface DebateTranscript {
  rounds: number;
  history: DebateArgument[];
  consensus_reached: boolean;
}

export interface DebateResolution {
  topic: string;
  positions_summary: string;
  winning_side: string;
  resolution: string;
}

/** The Manager's synthesized final report. */
export interface ManagerReport {
  agent: string;
  confidence: number;
  reasoning: string;
  recommendation: "bullish" | "neutral" | "bearish" | string;
  conviction: "high" | "medium" | "low" | string;
  overall_score: number;
  executive_summary: string;
  bull_case: string[];
  bear_case: string[];
  key_debates: DebateResolution[];
  consensus_points: string[];
  key_risks: string[];
  recommended_actions: string[];
  agents_considered: string[];
}

/** An agent slot in the report: either a report object or an `{error}`. */
export type AgentSlot = Record<string, unknown> & { error?: string };

/** Full result from POST /analyze (and the `complete` stream event). */
export interface AnalyzeResult {
  run_id: string;
  analysis_period: string;
  company: CompanyInfo | null;
  agents_total: number;
  agents_completed: number;
  three_axis_scores: ThreeAxisScores;
  reports: Record<string, AgentSlot>;
  debate: DebateTranscript | null;
  manager: (ManagerReport & { error?: string }) | { error: string } | null;
}

/** A progress event streamed from POST /analyze/stream. */
export interface AnalyzeProgressEvent {
  phase: number;
  status:
    | "running"
    | "agent_done"
    | "debating"
    | "synthesizing"
    | "complete"
    | "error";
  agents_total?: number;
  agents_completed?: number;
  agent?: string;
  ok?: boolean;
  skipped?: boolean;
  participants?: string[];
  result?: AnalyzeResult;
  detail?: string;
}

/** Lightweight past-run summary for the history sidebar. */
export interface AnalysisHistoryItem {
  run_id: string;
  company: string | null;
  ticker: string | null;
  analysis_period: string;
  timestamp: string;
  fundamental_score: number | null;
  sentiment_score: number | null;
  technical_score: number | null;
  fundamental_sentiment_gap: number | null;
  overall_signal: string | null;
  signal_label: string | null;
  recommendation: string | null;
  overall_score: number | null;
}

export interface AnalysisHistoryResponse {
  ticker: string;
  history: AnalysisHistoryItem[];
}

/** Full stored record from GET /analysis/{run_id}. */
export interface AnalysisRecord {
  run_id: string;
  company: string | null;
  ticker: string | null;
  analysis_period: string;
  timestamp: string;
  three_axis_scores: ThreeAxisScores;
  manager: ManagerReport | { error: string } | null;
  reports: Record<string, AgentSlot>;
  debate: DebateTranscript | null;
}
