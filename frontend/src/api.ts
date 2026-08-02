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
 * Fetch merged financial table data for a specific statement type.
 *
 * @param statementType - One of: "balance_sheet", "income_statement", "cash_flow"
 */
export async function getFinancials(
  statementType: string
): Promise<FinancialTableResponse> {
  return fetchJson<FinancialTableResponse>(
    `${API_BASE}/financials?statement_type=${encodeURIComponent(statementType)}`
  );
}

/**
 * Fetch extracted text for a specific section of a specific filing period.
 *
 * @param period  - Filing period key, e.g., "2023-10K"
 * @param section - Section key: "mda", "footnotes", "supplementary", etc.
 */
export async function getFilingText(
  period: string,
  section: string
): Promise<FilingTextResponse> {
  return fetchJson<FilingTextResponse>(
    `${API_BASE}/filing-text?period=${encodeURIComponent(period)}&section=${encodeURIComponent(section)}`
  );
}

/**
 * Fetch the list of all uploaded filing periods.
 * Used to populate the period dropdown in the Lower Pane.
 */
export async function getPeriods(): Promise<PeriodsResponse> {
  return fetchJson<PeriodsResponse>(`${API_BASE}/periods`);
}

/**
 * Build the URL for fetching a section's PDF pages.
 *
 * This returns a URL string (not a fetch call) because the frontend
 * loads it directly in an <iframe src="...">.
 *
 * @param period  - Filing period key, e.g., "2023-10K"
 * @param section - Section key: "mda", "footnotes", etc.
 */
export function getFilingPdfUrl(period: string, section: string): string {
  return `${API_BASE}/filing-pdf?period=${encodeURIComponent(period)}&section=${encodeURIComponent(section)}`;
}

/**
 * Ask the AI assistant a question about the uploaded filings.
 *
 * @param question - The user's current question
 * @param history  - Prior conversation turns (oldest first), excluding the
 *                    current question. Gives the assistant follow-up context.
 */
export async function askChat(
  question: string,
  history: ChatMessage[]
): Promise<ChatResponse> {
  return fetchJson<ChatResponse>(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, history }),
  });
}

// =============================================================================
// Company / Media / Macro
// =============================================================================

/** The company/companies derived from uploaded filings. */
export async function getCompany(): Promise<CompanyResponse> {
  return fetchJson<CompanyResponse>(`${API_BASE}/company`);
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

/** Company-specific news feed (View 2). Up to 30 articles within `range`. */
export async function getCompanyNews(range?: NewsRange): Promise<NewsResponse> {
  return fetchJson<NewsResponse>(`${API_BASE}/media/news${rangeQuery(range, 30)}`);
}

/** Append a channel_id filter to an existing query string. */
function withChannel(qs: string, channelId?: string): string {
  if (!channelId) return qs;
  const sep = qs ? "&" : "?";
  return `${qs}${sep}channel_id=${encodeURIComponent(channelId)}`;
}

/** Company analysis videos (View 2), filtered to `range` and optional channel. */
export async function getCompanyVideos(
  range?: NewsRange,
  channelId?: string
): Promise<VideoResponse> {
  return fetchJson<VideoResponse>(
    `${API_BASE}/media/videos${withChannel(rangeQuery(range), channelId)}`
  );
}

/** Fetch a YouTube video's full transcript (captions permitting). */
export async function getTranscript(videoId: string): Promise<TranscriptResponse> {
  return fetchJson<TranscriptResponse>(
    `${API_BASE}/media/transcript?video_id=${encodeURIComponent(videoId)}`
  );
}

/** Best-effort earnings material for a fiscal quarter (View 2). */
export async function getEarnings(
  year: number,
  quarter: number
): Promise<EarningsResponse> {
  return fetchJson<EarningsResponse>(
    `${API_BASE}/media/earnings?year=${year}&quarter=${quarter}`
  );
}

// ── Curated YouTube channels ──

export async function getChannels(): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(`${API_BASE}/channels`);
}

/** Add a channel by URL, @handle, UC… id, or name. */
export async function addChannel(input: string): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(`${API_BASE}/channels`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input }),
  });
}

export async function deleteChannel(channelId: string): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(
    `${API_BASE}/channels/${encodeURIComponent(channelId)}`,
    { method: "DELETE" }
  );
}

export async function renameChannel(
  channelId: string,
  title: string
): Promise<ChannelsResponse> {
  return fetchJson<ChannelsResponse>(
    `${API_BASE}/channels/${encodeURIComponent(channelId)}`,
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
    `${API_BASE}/macro/videos${withChannel(rangeQuery(range), channelId)}`
  );
}

/** Market sentiment synthesis (View 3). */
export async function getMarketSentiment(): Promise<SentimentResponse> {
  return fetchJson<SentimentResponse>(`${API_BASE}/macro/sentiment`);
}
