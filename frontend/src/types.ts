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

// =============================================================================
// Portfolio & Trading Journal (GET/POST /portfolio*)
// =============================================================================
// Mirrors the Pydantic models in `backend/schemas/api_schemas.py`. Unlike the
// filing types these are portfolio-scoped, not company-scoped: the portfolio
// spans every ticker at once, so nothing here is keyed on `activeTicker`.

/** One position. Valuation fields are null until the backend can price it. */
export interface Holding {
  ticker: string;
  quantity: number;
  avg_price: number;
  initial_fx_rate: number | null;
  currency: string;
  created_at: string | null;
  updated_at: string | null;
  /** Null means "couldn't be priced", NOT "worth zero". */
  current_price: number | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  /** Fractional, e.g. 0.1542 = +15.42%. */
  unrealized_roi: number | null;

  /** Share of NET WORTH (positions + cash), not of equity. */
  weight: number | null;
  // The same wealth stated twice — exactly one of each pair equals the native
  // figure above. The redundancy is deliberate: no consumer needs a conversion
  // rule, and the frontend never multiplies by a rate.
  market_value_krw: number | null;
  market_value_usd: number | null;
  cost_basis_krw: number | null;
  cost_basis_usd: number | null;
  unrealized_pnl_krw: number | null;
  unrealized_pnl_usd: number | null;
  /** The stock's own return, in its trading currency. */
  roi_local: number | null;
  /** The currency's return since the position was funded; exactly 0 for KRW. */
  roi_fx: number | null;
  roi_krw: number | null;
  roi_usd: number | null;
}

/** One journal entry. */
export interface Trade {
  /** On a sell, net of fees, in the asset's own currency. */
  realized_pnl?: number | null;
  /** The same sale in KRW at the rates that applied — its gap from
   * `realized_pnl` is the exchange-rate component. */
  realized_pnl_base?: number | null;
  fee?: number | null;
  tax?: number | null;
  id: number;
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  executed_at: string;
  execution_price: number | null;
  total_value: number | null;
  fx_rate: number | null;
  /** The user's own words — what the Coach agent will evaluate. */
  entry_rationale: string | null;
  avg_price_after: number | null;
  created_at: string | null;
}

/**
 * How the backend derived a fill price. `is_approximate` is false only when an
 * exact 1-minute bar was found — Yahoo serves those for ~30 days only, so an
 * older trade degrades to an hourly or daily bar and must be labeled as such.
 */
export interface PriceResolution {
  resolution: "1m" | "1h" | "1d";
  bar_time: string;
  is_approximate: boolean;
  message: string;
}

/** Per-ticker state of the background 8-quarter SEC baseline fetch. */
/** Progress of the quarterly Deep Analysis runs kicked off with a new holding. */
export interface BaselineAnalysisStatus {
  state:
    | "pending"
    | "queued"
    | "running"
    | "complete"
    | "partial"
    | "failed"
    | "skipped";
  completed: number;
  total: number;
  message: string;
  failures?: string[];
  /** run_ids of the analyses produced, viewable in the Deep Analysis view. */
  run_ids?: string[];
}

export interface BaselineStatus {
  state:
    | "none"
    | "queued"
    | "running"
    | "complete"
    | "partial"
    | "failed"
    /** Not a US listing — SEC EDGAR has nothing to fetch. */
    | "unsupported";
  message: string;
  ingested?: number;
  start_year?: number;
  end_year?: number;
  analysis?: BaselineAnalysisStatus;
}

/** The exchange rate a response's converted figures used. One per response. */
export interface FxInfo {
  pair: string;
  /** KRW per 1 USD. Null when unavailable — render a dash, never a zero. */
  rate: number | null;
  as_of: string | null;
  is_stale: boolean;
  source: string | null;
}

export interface PortfolioResponse {
  holdings: Holding[];
  /**
   * Totals are null whenever no single figure can honestly be stated — no live
   * prices yet, or holdings spanning more than one currency. `note` says which.
   * Per-currency subtotals below always hold, and every holding carries its own
   * `currency`, so no position is unreadable when an aggregate is withheld.
   */
  total_cost_basis: number | null;
  total_market_value: number | null;
  total_unrealized_pnl: number | null;
  total_roi: number | null;
  note: string | null;
  currencies: string[];
  cost_basis_by_currency: Record<string, number>;
  market_value_by_currency: Record<string, number>;
  fx: FxInfo | null;
  /** Per-currency cash balances. */
  cash: Record<string, number>;
  cash_initialized: boolean;
  /**
   * The money actually held in each currency — distinct from `cash_total_krw` /
   * `cash_total_usd`, which state the WHOLE cash pile in each. Confusing the two
   * is the easiest mistake to make here.
   */
  cash_balances: Record<string, number>;
  cash_total_krw: number | null;
  cash_total_usd: number | null;
  equity_total_krw: number | null;
  equity_total_usd: number | null;
  net_worth_krw: number | null;
  net_worth_usd: number | null;
  cost_basis_krw: number | null;
  cost_basis_usd: number | null;
  cash_weight: number | null;
  equity_weight: number | null;
  /** Share of net worth denominated in a foreign currency. */
  fx_exposure: number | null;
  roi_krw_total: number | null;
  roi_usd_total: number | null;
  baseline_status: Record<string, BaselineStatus>;
}

/** One movement of money in the cash ledger. */
export interface CashFlow {
  id: number;
  flow_type: string;
  currency: string;
  /** Signed and denominated in `currency`: positive in, negative out. */
  amount: number;
  /** USDKRW rate when the money moved (1.0 on a KRW row). */
  fx_to_krw: number;
  occurred_at: string;
  trade_id: number | null;
  /** Links the two legs of a currency conversion. */
  conversion_id: string | null;
  note: string | null;
  created_at: string;
}

export interface CashPosition {
  balances: Record<string, number>;
  base_currency: string;
  is_initialized: boolean;
  fx: FxInfo | null;
  recent_flows: CashFlow[];
}

export interface TradesResponse {
  trades: Trade[];
  total: number;
  ticker: string | null;
}

/** POST /portfolio/trades — what the user types, and nothing more. */
export interface TradeCreate {
  ticker: string;
  side: "buy" | "sell";
  quantity: number;
  executed_at: string;
  entry_rationale?: string | null;
  /** Manual override; omit so the backend derives the fill from market data. */
  execution_price?: number | null;
  fx_rate?: number | null;
}

export interface TradeResponse {
  trade: Trade;
  holding: Holding | null;
  /** Null when the caller supplied an explicit price (no lookup happened). */
  price_resolution: PriceResolution | null;
}

export interface HoldingCreate {
  ticker: string;
  quantity: number;
  avg_price: number;
  initial_fx_rate?: number | null;
  currency?: string;
}

export interface HoldingCreatedResponse {
  holding: Holding;
  baseline_started: boolean;
  baseline_status: BaselineStatus;
}

// =============================================================================
// Trading Coach (POST /coach/review)
// =============================================================================

/** One bias the coach believes it can evidence from the journal. */
export interface DetectedBias {
  bias: string;
  evidence: string;
  /** Dates of real past trades. The backend strips any it can't verify. */
  past_occurrences: string[];
  severity: "mild" | "moderate" | "strong" | string;
}

export interface CoachReport {
  agent: string;
  confidence: number;
  reasoning: string;
  /** 'pre_trade' for a trade being considered, 'retrospective' for a logged one. */
  review_type: "pre_trade" | "retrospective" | string;
  trade_id: number | null;
  ticker: string | null;
  proposed_action: string | null;
  rationale_evaluation: string;
  detected_biases: DetectedBias[];
  /** Null when the journal is too short for a pattern to mean anything. */
  historical_pattern: string | null;
  coaching_feedback: string;
  /** 0 = contradicts the objective data, 100 = fully consistent with it. */
  alignment_score: number;
  supporting_data_points: string[];
  data_limitations: string[];
  history_sufficient: boolean;

  // ── Retrospective only; null on a pre-trade review. ──
  /**
   * Quality of the REASONING, scored before the outcome was shown to the model.
   * Deliberately separate from the outcome: a decision can be sound and still
   * lose money, and collapsing the two teaches outcome-chasing.
   */
  process_quality: number | null;
  what_was_knowable: string | null;
  outcome_summary: string | null;
  /** Which of the four quadrants this trade fell in. */
  luck_vs_skill: string | null;
  hindsight_note: string | null;
  /** run_id of the analysis that existed at the trade's timestamp, if any. */
  data_as_of: string | null;
}

export interface RecurringPattern {
  pattern: string;
  /** Real journal dates. The backend strips any it cannot verify. */
  occurrences: string[];
  trend: "worsening" | "stable" | "improving" | string;
  evidence: string;
}

/** The coach's review of the whole record rather than one decision. */
export interface JournalReport {
  agent: string;
  confidence: number;
  reasoning: string;
  review_type: "journal" | string;
  scope_description: string;
  trades_reviewed: number;
  period: string | null;
  recurring_patterns: RecurringPattern[];
  process_vs_outcome: string;
  advice_followed: string | null;
  strengths: string[];
  /** Capped at 3 by the backend. */
  priorities: string[];
  history_sufficient: boolean;
  data_limitations: string[];
}

export interface JournalReviewRequest {
  ticker?: string | null;
  since?: string | null;
  limit?: number;
}

/** A persisted review as it comes back from the database. */
export interface StoredReview {
  id: number;
  review_type: "pre_trade" | "retrospective" | "journal" | string;
  trade_id: number | null;
  ticker: string | null;
  scope: string | null;
  /** The rationale text as it read when it was judged. */
  rationale_snapshot: string | null;
  model: string | null;
  data_as_of: string | null;
  created_at: string;
  /**
   * Untyped on purpose: the report schema keeps growing and old reviews must
   * stay readable. Render whichever fields are present.
   */
  report: Partial<CoachReport> & Partial<JournalReport>;
}

export interface StoredReviewsResponse {
  reviews: StoredReview[];
  count: number;
}

export interface PendingReviewsResponse {
  trades: Trade[];
  count: number;
}

export interface CoachReviewRequest {
  ticker?: string | null;
  proposed_side?: "buy" | "sell" | null;
  proposed_quantity?: number | null;
  entry_rationale: string;
}

/** Both legs of a 환전, plus the spread against the market rate that day. */
export interface ConversionResult {
  conversion_id: string;
  /** Effective rate, derived from the two amounts — spread included. */
  rate: number;
  market_rate: number | null;
  /** Positive means the conversion cost you, in won. */
  spread_krw: number | null;
  /** Set when converting back to base currency. Average cost, not FIFO. */
  realized_fx_pnl_krw: number | null;
  out: CashFlow;
  in: CashFlow;
}

export interface CashFlowCreate {
  flow_type: string;
  currency: string;
  /** Positive magnitude; the server derives the sign from `flow_type`. */
  amount: number;
  occurred_at: string;
  fx_to_krw?: number | null;
  note?: string | null;
}

export interface ConversionCreate {
  from_currency: string;
  from_amount: number;
  to_currency: string;
  to_amount: number;
  occurred_at: string;
  note?: string | null;
}

export interface CashFlowsResponse {
  flows: CashFlow[];
  count: number;
}

export interface LedgerInitResponse {
  balances: Record<string, number>;
  flows_written: number;
  holdings_funded: number;
  fx_backfill: { filled: number; unresolved: number; problems: string[] } | null;
}

export type PerformanceWindow = "1m" | "3m" | "6m" | "1y" | "all";

export interface NetWorthPoint {
  date: string;
  equity_krw: number | null;
  equity_usd: number | null;
  cash_krw: number | null;
  cash_usd: number | null;
  net_worth_krw: number | null;
  net_worth_usd: number | null;
  fx_rate: number | null;
}

export interface ReturnFigure {
  cumulative: number | null;
  annualized: number | null;
  days?: number;
  note: string | null;
}

export interface PerformanceReport {
  window: string;
  /** Where the ledger begins — the chart must not imply history before it. */
  coverage_start: string | null;
  note: string | null;
  observations: number;
  series: NetWorthPoint[];
  twr: Record<string, ReturnFigure>;
  mwr: Record<string, ReturnFigure>;
  realized: {
    realized_pnl_native: number | null;
    realized_pnl_krw: number | null;
    realized_fx_pnl_krw: number | null;
    fees: { native: number; krw: number };
    taxes: { native: number; krw: number };
    basis: string;
  };
}
