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

/** Company-specific news feed (View 2). */
export async function getCompanyNews(): Promise<NewsResponse> {
  return fetchJson<NewsResponse>(`${API_BASE}/media/news`);
}

/** Company-specific analysis videos (View 2). */
export async function getCompanyVideos(): Promise<VideoResponse> {
  return fetchJson<VideoResponse>(`${API_BASE}/media/videos`);
}

/** Fetch (and optionally summarize) a YouTube video transcript. */
export async function getTranscript(
  videoId: string,
  summarize = true
): Promise<TranscriptResponse> {
  return fetchJson<TranscriptResponse>(
    `${API_BASE}/media/transcript?video_id=${encodeURIComponent(videoId)}&summarize=${summarize}`
  );
}

/** Best-effort earnings material for the company (View 2). */
export async function getEarnings(): Promise<EarningsResponse> {
  return fetchJson<EarningsResponse>(`${API_BASE}/media/earnings`);
}

/** Aggregated macro/market news (View 3). */
export async function getMacroNews(): Promise<NewsResponse> {
  return fetchJson<NewsResponse>(`${API_BASE}/macro/news`);
}

/** Macro/economic videos (View 3). */
export async function getMacroVideos(): Promise<VideoResponse> {
  return fetchJson<VideoResponse>(`${API_BASE}/macro/videos`);
}

/** Market sentiment synthesis (View 3). */
export async function getMarketSentiment(): Promise<SentimentResponse> {
  return fetchJson<SentimentResponse>(`${API_BASE}/macro/sentiment`);
}
