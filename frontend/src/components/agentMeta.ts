/**
 * agentMeta.ts
 * ────────────
 * Shared display metadata for the field agents + the manager: human names,
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
  "quant_risk",
  "trading_coach",
];

export const AGENT_NAMES: Record<string, string> = {
  sec_filings: "SEC Filings",
  earnings_call: "Earnings Call",
  company_news: "Company News",
  youtube_analysis: "Analyst Videos",
  macro_market: "Macro & Market",
  macro_history: "Macro History",
  technical_analysis: "Technical (Price)",
  quant_risk: "Portfolio Risk",
  trading_coach: "Trading Coach",
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
  quant_risk: "⚖️",
  trading_coach: "🧠",
  manager: "🧭",
};

/** Map a bullish/bearish/neutral stance to a tone class suffix. */
export function stanceTone(stance: string | number): "positive" | "negative" | "neutral" {
  if (typeof stance === "number") {
    if (stance <= 40) return "negative";
    if (stance >= 61) return "positive";
    return "neutral";
  }
  const s = (stance || "").toLowerCase();
  if (s.includes("bull")) return "positive";
  if (s.includes("bear")) return "negative";
  return "neutral";
}

/** Map a manager recommendation to a tone class suffix. */
export function recommendationTone(rec: string): "positive" | "negative" | "neutral" {
  return stanceTone(rec);
}

/** Format a stance score into a human-readable label. */
export function formatStance(stance: string | number): string {
  if (typeof stance === "number") {
    if (stance <= 20) return `Strong Bearish (${stance})`;
    if (stance <= 40) return `Bearish (${stance})`;
    if (stance <= 60) return `Neutral (${stance})`;
    if (stance <= 80) return `Bullish (${stance})`;
    return `Strong Bullish (${stance})`;
  }
  return stance;
}
