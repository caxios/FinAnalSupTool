/**
 * api.ts
 * ──────
 * Centralized API layer for communicating with the FastAPI backend.
 *
 * All fetch calls go through this module so that:
 *   1. The base URL is configured in one place
 *   2. Error handling is consistent (extracts `detail` from error responses)
 *   3. Response typing is enforced via generics
 *
 * The backend runs on localhost:8000 during development.
 */

import type {
  UploadResponse,
  SecFetchRequest,
  SecFetchResponse,
  FinancialTableResponse,
  FilingTextResponse,
  PeriodsResponse,
  ChatMessage,
  ChatResponse,
  CompanyResponse,
  NewsResponse,
  VideoResponse,
  TranscriptResponse,
  EarningsResponse,
  SentimentResponse,
  NewsRange,
  ChannelsResponse,
  AnalyzeResult,
  AnalyzeProgressEvent,
  AnalysisHistoryResponse,
  AnalysisRecord,
} from "./types";

// Base URL for the FastAPI backend (change this if using a different port)
const API_BASE = "http://localhost:8000";

// =============================================================================
// Generic Fetch Helper
// =============================================================================

/**
 * Make a fetch request and return typed JSON data.
 *
 * If the response is not OK (e.g., 404), extracts the `detail` field
 * from the error body and throws it as an Error — this is the format
 * FastAPI's HTTPException uses.
 *
 * @param url     - Full URL to fetch
 * @param options - Standard fetch RequestInit options
 * @returns       - Parsed JSON response, typed as T
 */
async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);

  if (!response.ok) {
    // Try to extract FastAPI's error detail message
    let errorMessage = `HTTP ${response.status}`;
    try {
      const errorBody = await response.json();
      if (errorBody.detail) {
        errorMessage = errorBody.detail;
      }
    } catch {
      // If error body isn't JSON, use status text
      errorMessage = response.statusText || errorMessage;
    }
    throw new Error(errorMessage);
  }

  return response.json() as Promise<T>;
}

// =============================================================================
// API Functions — one per backend endpoint
// =============================================================================

/**
 * Upload one or more PDF files to the backend for processing.
 *
 * Uses FormData to send files as multipart/form-data, which is what
 * FastAPI's UploadFile expects.
 *
 * @param files - Array of File objects selected by the user
 */
export async function uploadFiles(files: File[]): Promise<UploadResponse> {
  const formData = new FormData();

  // FastAPI expects the field name "files" (matching the endpoint parameter)
  for (const file of files) {
    formData.append("files", file);
  }

  return fetchJson<UploadResponse>(`${API_BASE}/upload`, {
    method: "POST",
    body: formData,
    // Note: Do NOT set Content-Type header — the browser sets it
    // automatically with the correct multipart boundary
  });
}

/**
 * Fetch a SEC filing automatically from EDGAR by ticker/form/period.
 *
 * The backend resolves the filing, renders it to PDF, and runs it through the
 * same ingestion pipeline as a manual upload — so the result shape matches
 * uploadFiles (per-file FilingMeta), plus provenance of what was retrieved.
 *
 * Note: this is slower than a normal upload (SEC lookup + headless-browser PDF
 * render happen server-side), so callers should show a staged loading state.
 */
export async function fetchSecFiling(
  req: SecFetchRequest
): Promise<SecFetchResponse> {
  return fetchJson<SecFetchResponse>(`${API_BASE}/sec/fetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

/**
 * Fetch merged financial table data for one company and statement type.
 *
 * @param ticker        - Company whose filings to read, e.g. "AAPL"
 * @param statementType - One of: "balance_sheet", "income_statement", "cash_flow"
 */
export async function getFinancials(
  ticker: string,
  statementType: string
): Promise<FinancialTableResponse> {
  return fetchJson<FinancialTableResponse>(
    `${API_BASE}/financials?ticker=${encodeURIComponent(ticker)}` +
      `&statement_type=${encodeURIComponent(statementType)}`
  );
}

/**
 * Fetch extracted text for a specific section of one company's filing period.
 *
 * @param ticker  - Company whose filings to read, e.g. "AAPL"
 * @param period  - Filing period key, e.g., "2023-10K"
 * @param section - Section key: "mda", "footnotes", "supplementary", etc.
 */
export async function getFilingText(
  ticker: string,
  period: string,
  section: string
): Promise<FilingTextResponse> {
  return fetchJson<FilingTextResponse>(
    `${API_BASE}/filing-text?ticker=${encodeURIComponent(ticker)}` +
      `&period=${encodeURIComponent(period)}&section=${encodeURIComponent(section)}`
  );
}

/**
 * Fetch one company's uploaded filing periods.
 * Used to populate the period dropdown in the Lower Pane.
 *
 * @param ticker - Company to list periods for, e.g. "AAPL"
 */
export async function getPeriods(ticker: string): Promise<PeriodsResponse> {
  return fetchJson<PeriodsResponse>(
    `${API_BASE}/periods?ticker=${encodeURIComponent(ticker)}`
  );
}

/**
 * Build the URL for fetching a section's PDF pages.
 *
 * This returns a URL string (not a fetch call) because the frontend
 * loads it directly in an <iframe src="...">.
 *
 * @param ticker  - Company whose filing to read, e.g. "AAPL"
 * @param period  - Filing period key, e.g., "2023-10K"
 * @param section - Section key: "mda", "footnotes", etc.
 */
export function getFilingPdfUrl(
  ticker: string,
  period: string,
  section: string
): string {
  return (
    `${API_BASE}/filing-pdf?ticker=${encodeURIComponent(ticker)}` +
    `&period=${encodeURIComponent(period)}&section=${encodeURIComponent(section)}`
  );
}

/**
 * Ask the AI assistant a question about the uploaded filings.
 *
 * @param question - The user's current question
 * @param history  - Prior conversation turns (oldest first), excluding the
 *                    current question. Gives the assistant follow-up context.
 * @param agentId  - Optional persona to chat with in isolation: a field agent id
 *                    (sec_filings, earnings_call, …), "manager", or omit for the
 *                    general cross-view assistant. Field agents see only their own
 *                    data + the debate transcript; the manager sees all reports +
 *                    the transcript.
 * @param ticker   - Company to ground the answer in. Its filings + media are the
 *                    only company data in scope. Required for agent personas;
 *                    omit for a macro-only conversation.
 */
export async function askChat(
  question: string,
  history: ChatMessage[],
  agentId?: string,
  ticker?: string | null
): Promise<ChatResponse> {
  return fetchJson<ChatResponse>(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      history,
      agent_id: agentId,
      ticker: ticker ?? undefined,
    }),
  });
}

// =============================================================================
// Company / Media / Macro
// =============================================================================

/**
 * EVERY company with ingested filings, across all per-company stores.
 * Backs the header's company switcher — each entry's `ticker` is the key the
 * ticker-scoped endpoints expect.
 */
export async function getCompanies(): Promise<CompanyResponse> {
  return fetchJson<CompanyResponse>(`${API_BASE}/companies`);
}

/** The company derived from ONE ticker's uploaded filings. */
export async function getCompany(ticker: string): Promise<CompanyResponse> {
  return fetchJson<CompanyResponse>(
    `${API_BASE}/company?ticker=${encodeURIComponent(ticker)}`
  );
}

/**
 * Build a query string for a date-range selection.
 * Presets pass `days`; the "custom" preset passes `start`/`end`.
 */
function rangeQuery(range?: NewsRange, maxResults?: number): string {
  const p = new URLSearchParams();
  if (range) {
    if (range.preset === "custom") {
      if (range.start) p.set("start", range.start);
      if (range.end) p.set("end", range.end);
    } else if (range.days) {
      p.set("days", String(range.days));
    }
  }
  if (maxResults) p.set("max_results", String(maxResults));
  const qs = p.toString();
  return qs ? `?${qs}` : "";
}

/** Prefix a ticker onto a range query string built by `rangeQuery`. */
function withTicker(qs: string, ticker: string): string {
  const t = `ticker=${encodeURIComponent(ticker)}`;
  return qs ? `?${t}&${qs.slice(1)}` : `?${t}`;
}

/** One company's news feed (View 2). Up to 30 articles within `range`. */
export async function getCompanyNews(
  ticker: string,
  range?: NewsRange
): Promise<NewsResponse> {
  return fetchJson<NewsResponse>(
    `${API_BASE}/media/news${withTicker(rangeQuery(range, 30), ticker)}`
  );
}

/** Append a channel_id filter to an existing query string. */
function withChannel(qs: string, channelId?: string): string {
  if (!channelId) return qs;
  const sep = qs ? "&" : "?";
  return `${qs}${sep}channel_id=${encodeURIComponent(channelId)}`;
}

/** One company's analysis videos (View 2), filtered to `range` and channel. */
export async function getCompanyVideos(
  ticker: string,
  range?: NewsRange,
  channelId?: string
): Promise<VideoResponse> {
  return fetchJson<VideoResponse>(
    `${API_BASE}/media/videos` +
      withChannel(withTicker(rangeQuery(range, 50), ticker), channelId)
  );
}

/**
 * Fetch a YouTube video's full transcript (captions permitting).
 *
 * `ticker` scopes the cached excerpt to that company so the assistant doesn't
 * see another company's videos; omit it for a macro video.
 */
export async function getTranscript(
  videoId: string,
  ticker?: string | null
): Promise<TranscriptResponse> {
  const t = ticker ? `&ticker=${encodeURIComponent(ticker)}` : "";
  return fetchJson<TranscriptResponse>(
    `${API_BASE}/media/transcript?video_id=${encodeURIComponent(videoId)}${t}`
  );
}

/** Best-effort earnings material for one company and fiscal quarter (View 2). */
export async function getEarnings(
  ticker: string,
  year: number,
  quarter: number
): Promise<EarningsResponse> {
  return fetchJson<EarningsResponse>(
    `${API_BASE}/media/earnings?ticker=${encodeURIComponent(ticker)}` +
      `&year=${year}&quarter=${quarter}`
  );
}

// ── Curated YouTube channels (per scope: "company" | "macro") ──

export type ChannelScope = "company" | "macro";

export async function getChannels(scope: ChannelScope): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(`${API_BASE}/channels?scope=${scope}`);
}

/** Add a channel (to a scope) by URL, @handle, UC… id, or name. */
export async function addChannel(
  scope: ChannelScope,
  input: string
): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(`${API_BASE}/channels?scope=${scope}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input }),
  });
}

export async function deleteChannel(
  scope: ChannelScope,
  channelId: string
): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(
    `${API_BASE}/channels/${encodeURIComponent(channelId)}?scope=${scope}`,
    { method: "DELETE" }
  );
}

export async function renameChannel(
  scope: ChannelScope,
  channelId: string,
  title: string
): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(
    `${API_BASE}/channels/${encodeURIComponent(channelId)}?scope=${scope}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: title }),
    }
  );
}

/** Aggregated macro/market news (View 3). Up to 30 articles within `range`. */
export async function getMacroNews(range?: NewsRange): Promise<NewsResponse> {
  return fetchJson<NewsResponse>(`${API_BASE}/macro/news${rangeQuery(range, 30)}`);
}

/** Macro/economic videos (View 3), filtered to `range` and optional channel. */
export async function getMacroVideos(
  range?: NewsRange,
  channelId?: string
): Promise<VideoResponse> {
  return fetchJson<VideoResponse>(
    `${API_BASE}/macro/videos${withChannel(rangeQuery(range, 50), channelId)}`
  );
}

/** Market sentiment synthesis (View 3). */
export async function getMarketSentiment(): Promise<SentimentResponse> {
  return fetchJson<SentimentResponse>(`${API_BASE}/macro/sentiment`);
}

// =============================================================================
// Deep Analysis (Multi-Agent System)
// =============================================================================

interface AnalyzeBody {
  ticker: string;
  start_date?: string;
  end_date?: string;
}

function analyzeBody(
  ticker: string,
  startDate?: string,
  endDate?: string
): AnalyzeBody {
  const body: AnalyzeBody = { ticker };
  if (startDate) body.start_date = startDate;
  if (endDate) body.end_date = endDate;
  return body;
}

/**
 * Run the full MAS pipeline for one company (non-streaming). Returns the final
 * report once the whole ~60-120s pipeline completes. Prefer `runAnalysisStream`
 * for live per-agent progress.
 */
export async function runAnalysis(
  ticker: string,
  startDate?: string,
  endDate?: string
): Promise<AnalyzeResult> {
  return fetchJson<AnalyzeResult>(`${API_BASE}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(analyzeBody(ticker, startDate, endDate)),
  });
}

/**
 * Run the pipeline with live progress via Server-Sent Events.
 *
 * The backend streams one `data: {json}` line per event (agents finishing, then
 * the debate + synthesis phases). `onEvent` fires for each; the promise resolves
 * with the final `AnalyzeResult` (from the `complete` event) or rejects if the
 * stream reports an error. Pass an `AbortSignal` to cancel the request.
 */
export async function runAnalysisStream(
  ticker: string,
  startDate: string | undefined,
  endDate: string | undefined,
  onEvent: (event: AnalyzeProgressEvent) => void,
  signal?: AbortSignal
): Promise<AnalyzeResult> {
  const response = await fetch(`${API_BASE}/analyze/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(analyzeBody(ticker, startDate, endDate)),
    signal,
  });

  if (!response.ok || !response.body) {
    // Non-200 (e.g. 404 no filings, 503 no key) comes back as normal JSON.
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail) message = body.detail;
    } catch {
      message = response.statusText || message;
    }
    throw new Error(message);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: AnalyzeResult | null = null;

  // Read the stream, splitting on the SSE record separator (blank line).
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const record = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      const line = record.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;

      let event: AnalyzeProgressEvent;
      try {
        event = JSON.parse(payload) as AnalyzeProgressEvent;
      } catch {
        continue;
      }
      onEvent(event);
      if (event.status === "error") {
        throw new Error(event.detail || "Analysis failed.");
      }
      if (event.status === "complete" && event.result) {
        result = event.result;
      }
    }
  }

  if (!result) throw new Error("Analysis stream ended without a result.");
  return result;
}

/** Past analysis-run summaries for a ticker (newest first). */
export async function getAnalysisHistory(
  ticker: string,
  limit = 10
): Promise<AnalysisHistoryResponse> {
  return fetchJson<AnalysisHistoryResponse>(
    `${API_BASE}/analysis/history?ticker=${encodeURIComponent(ticker)}&limit=${limit}`
  );
}

/** Full stored record for one past run. */
export async function getAnalysisRun(runId: string): Promise<AnalysisRecord> {
  return fetchJson<AnalysisRecord>(
    `${API_BASE}/analysis/${encodeURIComponent(runId)}`
  );
}
