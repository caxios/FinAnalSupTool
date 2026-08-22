/**
 * agentMeta.ts
 * ────────────
 * Shared display metadata for the six field agents + the manager: human names,
 * icons, and stance/tone → color-class helpers. Kept in one place so the
 * progress tracker, report panels, and debate log all label agents identically.
 */

export const AGENT_ORDER: string[] = [
  "sec_filings",
  "earnings_call",
  "company_news",
  "youtube_analysis",
  "macro_market",
  "macro_history",
  "technical_analysis",
];

export const AGENT_NAMES: Record<string, string> = {
  sec_filings: "SEC Filings",
  earnings_call: "Earnings Call",
  company_news: "Company News",
  youtube_analysis: "Analyst Videos",
  macro_market: "Macro & Market",
  macro_history: "Macro History",
  technical_analysis: "Technical (Price)",
  manager: "Manager (Synthesis)",
};

export const AGENT_ICONS: Record<string, string> = {
  sec_filings: "📑",
  earnings_call: "🎙️",
  company_news: "📰",
  youtube_analysis: "▶️",
  macro_market: "🌐",
  macro_history: "🏛️",
  technical_analysis: "📈",
  manager: "🧭",
};

/** Map a bullish/bearish/neutral stance to a tone class suffix. */
export function stanceTone(stance: string): "positive" | "negative" | "neutral" {
  const s = (stance || "").toLowerCase();
  if (s.includes("bull")) return "positive";
  if (s.includes("bear")) return "negative";
  return "neutral";
}

/** Map a manager recommendation to a tone class suffix. */
export function recommendationTone(rec: string): "positive" | "negative" | "neutral" {
  return stanceTone(rec);
}
