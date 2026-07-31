/**
 * SentimentDashboard.tsx
 * ──────────────────────
 * Market-sentiment panel for View 3: a 0–100 gauge + label, a narrative
 * summary, and themed indicator cards. Data is Gemini-synthesized from
 * aggregated macro headlines (see backend/market_sentiment.py).
 */

import type { SentimentResponse } from "../../types";
import MediaNotice from "./MediaNotice";

interface Props {
  data: SentimentResponse | null;
  loading: boolean;
  error: string | null;
}

function directionColor(direction: string): string {
  const d = direction.toLowerCase();
  if (d === "bullish") return "var(--color-success)";
  if (d === "bearish") return "var(--color-error)";
  return "var(--text-secondary)";
}

export default function SentimentDashboard({ data, loading, error }: Props) {
  if (loading)
    return <MediaNotice variant="loading" message="Synthesizing market sentiment…" />;
  if (error) return <MediaNotice variant="error" message={error} />;
  if (!data) return null;

  if (!data.configured) {
    return (
      <MediaNotice
        title="Sentiment not configured"
        message={
          data.message ??
          "Set TAVILY_API_KEY (macro news) and GEMINI_API_KEY (synthesis) to enable market sentiment."
        }
      />
    );
  }

  const score = data.score ?? 50;
  const labelColor = directionColor(data.label);

  return (
    <div className="sentiment-dashboard">
      <div className="sentiment-hero">
        <div className="sentiment-gauge">
          <div className="sentiment-gauge-track">
            <div
              className="sentiment-gauge-fill"
              style={{ width: `${Math.max(0, Math.min(100, score))}%` }}
            />
            <div
              className="sentiment-gauge-marker"
              style={{ left: `${Math.max(0, Math.min(100, score))}%` }}
            />
          </div>
          <div className="sentiment-gauge-scale">
            <span>Bearish</span>
            <span>Neutral</span>
            <span>Bullish</span>
          </div>
        </div>
        <div className="sentiment-readout">
          <div className="sentiment-label" style={{ color: labelColor }}>
            {data.label.toUpperCase()}
          </div>
          <div className="sentiment-score">
            {data.score !== null ? `${data.score}/100` : "—"}
          </div>
          {data.headline_count > 0 && (
            <div className="sentiment-basis">
              from {data.headline_count} headlines
            </div>
          )}
        </div>
      </div>

      {data.summary && <p className="sentiment-summary">{data.summary}</p>}

      {data.indicators.length > 0 && (
        <div className="indicator-grid">
          {data.indicators.map((ind, i) => (
            <div key={i} className="indicator-card">
              <div className="indicator-head">
                <span className="indicator-theme">{ind.theme}</span>
                <span
                  className="indicator-dir"
                  style={{ color: directionColor(ind.direction) }}
                >
                  {ind.direction}
                </span>
              </div>
              <div className="indicator-note">{ind.note}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
