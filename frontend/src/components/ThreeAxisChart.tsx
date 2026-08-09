/**
 * ThreeAxisChart.tsx
 * ──────────────────
 * The centerpiece visualization of the Deep Analysis view: three gauge meters
 * (Fundamental / Sentiment / Technical) plus the derived signal badge and the
 * fundamental-vs-market gap. Pure inline SVG — no chart library — so it stays
 * self-contained and theme-token-driven.
 *
 * The gap between Fundamental and Sentiment/Technical is the product's core
 * insight (e.g. "Hidden Gem" = strong fundamentals the market hasn't priced in).
 */

import type { ThreeAxisScores } from "../types";

/** Score → semantic color. High ≥65, mid 50-64, low <50 (matches the backend). */
function scoreColor(score: number | null): string {
  if (score === null || score === undefined) return "var(--text-muted)";
  if (score >= 65) return "var(--color-success)";
  if (score >= 50) return "var(--color-warning)";
  return "var(--color-error)";
}

function Gauge({ label, score }: { label: string; score: number | null }) {
  const size = 132;
  const stroke = 12;
  const r = (size - stroke) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const circumference = 2 * Math.PI * r;
  const pct = score === null || score === undefined ? 0 : Math.max(0, Math.min(100, score));
  // Leave a small gap at the bottom (270° arc) for a speedometer feel.
  const arc = 0.75; // fraction of the circle used
  const dash = circumference * arc;
  const filled = dash * (pct / 100);
  const color = scoreColor(score);

  return (
    <div className="axis-gauge">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
        aria-label={`${label} score ${score ?? "not available"}`}>
        <g transform={`rotate(135 ${cx} ${cy})`}>
          <circle
            cx={cx} cy={cy} r={r} fill="none"
            stroke="var(--border-subtle)" strokeWidth={stroke}
            strokeDasharray={`${dash} ${circumference}`} strokeLinecap="round"
          />
          <circle
            cx={cx} cy={cy} r={r} fill="none"
            stroke={color} strokeWidth={stroke}
            strokeDasharray={`${filled} ${circumference}`} strokeLinecap="round"
            style={{ transition: "stroke-dasharray 600ms ease" }}
          />
        </g>
        <text x={cx} y={cy - 2} textAnchor="middle" className="axis-gauge-value"
          fill={color}>
          {score === null || score === undefined ? "—" : score}
        </text>
        <text x={cx} y={cy + 18} textAnchor="middle" className="axis-gauge-max">
          / 100
        </text>
      </svg>
      <div className="axis-gauge-label">{label}</div>
    </div>
  );
}

function GapPill({ label, value }: { label: string; value: number | null }) {
  if (value === null || value === undefined) return null;
  const sign = value > 0 ? "+" : "";
  const tone = value >= 15 ? "positive" : value <= -15 ? "negative" : "neutral";
  return (
    <span className={`gap-pill gap-pill-${tone}`}>
      {label}: {sign}
      {value}
    </span>
  );
}

export default function ThreeAxisChart({ scores }: { scores: ThreeAxisScores }) {
  const tone = scores.signal_tone || "neutral";
  return (
    <div className="three-axis">
      <div className={`signal-badge signal-${tone}`}>
        <span className="signal-dot" />
        <span className="signal-label">{scores.signal_label || scores.overall_signal}</span>
      </div>

      <div className="axis-gauges">
        <Gauge label="Fundamental" score={scores.fundamental_score} />
        <Gauge label="Sentiment" score={scores.sentiment_score} />
        <Gauge label="Technical" score={scores.technical_score} />
      </div>

      <div className="axis-gaps">
        <GapPill label="Fundamental − Sentiment" value={scores.fundamental_sentiment_gap} />
        <GapPill label="Fundamental − Technical" value={scores.fundamental_technical_gap} />
      </div>
    </div>
  );
}
