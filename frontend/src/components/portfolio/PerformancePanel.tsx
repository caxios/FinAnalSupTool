/**
 * PerformancePanel.tsx
 * ────────────────────
 * Net worth over time, and the two returns that answer different questions.
 *
 * **TWR** is return per unit of capital — it measures selection and is
 * unaffected by when money was deposited. **MWR** is what the user's money
 * actually did. They diverge exactly when deposit timing was good or bad, and
 * showing only one answers a question nobody asked.
 *
 * The region before `coverage_start` is greyed: the ledger cannot reconstruct
 * what it was never told, and a confident line through invented history is worse
 * than a visible gap.
 */

import { useState } from "react";
import type { PerformanceReport, PerformanceWindow, ReturnFigure } from "../../types";
import { formatKrw, formatUsd, useCurrencyView } from "./Money";

const WINDOWS: PerformanceWindow[] = ["1m", "3m", "6m", "1y", "all"];

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
}

function tone(n: number | null | undefined): string {
  if (n === null || n === undefined) return "neutral";
  return n > 0 ? "positive" : n < 0 ? "negative" : "neutral";
}

function ReturnCard({
  title, figure, explanation,
}: { title: string; figure: ReturnFigure | undefined; explanation: string }) {
  return (
    <div className="perf-card">
      <div className="perf-card-title">{title}</div>
      <div className={`perf-card-value tone-${tone(figure?.cumulative)}`}>
        {pct(figure?.cumulative)}
      </div>
      {figure?.annualized !== null && figure?.annualized !== undefined && (
        <div className="perf-card-annual">{pct(figure.annualized)} annualized</div>
      )}
      <p className="perf-card-note">{figure?.note || explanation}</p>
    </div>
  );
}

/** A minimal inline sparkline — no chart library for one series. */
function Sparkline({ report, unit }: { report: PerformanceReport; unit: "krw" | "usd" }) {
  const key = unit === "krw" ? "net_worth_krw" : "net_worth_usd";
  const points = report.series
    .map((p) => ({ date: p.date, value: p[key] as number | null }))
    .filter((p): p is { date: string; value: number } => p.value !== null);

  if (points.length < 2) return null;

  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const W = 600, H = 90;

  const path = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * W;
      const y = H - ((p.value - min) / span) * H;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const first = points[0].value;
  const last = points[points.length - 1].value;
  const rising = last >= first;
  const fmt = unit === "krw" ? formatKrw : formatUsd;

  return (
    <div className="perf-chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           className="perf-chart-svg" role="img"
           aria-label={`Net worth from ${points[0].date} to ${points[points.length - 1].date}`}>
        <path d={path} fill="none" strokeWidth="2"
              className={rising ? "perf-line-up" : "perf-line-down"} />
      </svg>
      <div className="perf-chart-axis">
        <span>{points[0].date} · {fmt(first)}</span>
        <span>{points[points.length - 1].date} · {fmt(last)}</span>
      </div>
    </div>
  );
}

export default function PerformancePanel({
  report,
  loading,
  error,
  onWindowChange,
  window: activeWindow,
}: {
  report: PerformanceReport | null;
  loading: boolean;
  error: string | null;
  onWindowChange: (w: PerformanceWindow) => void;
  window: PerformanceWindow;
}) {
  const view = useCurrencyView();
  const [unit, setUnit] = useState<"krw" | "usd">("krw");
  const shown = view === "usd" ? "usd" : view === "krw" ? "krw" : unit;

  return (
    <section className="perf-panel">
      <div className="perf-head">
        <div className="range-bar">
          <span className="range-bar-label">Window</span>
          {WINDOWS.map((w) => (
            <button key={w}
                    className={`btn-secondary-sm ${w === activeWindow ? "is-active" : ""}`}
                    onClick={() => onWindowChange(w)}>
              {w}
            </button>
          ))}
        </div>
        {view === "both" && (
          <div className="currency-toggle">
            {(["krw", "usd"] as const).map((u) => (
              <button key={u}
                      className={`currency-toggle-btn ${unit === u ? "is-active" : ""}`}
                      onClick={() => setUnit(u)}>
                {u === "krw" ? "₩" : "$"}
              </button>
            ))}
          </div>
        )}
      </div>

      {loading && <div className="journal-notice">Reconstructing net worth…</div>}
      {error && <div className="journal-notice journal-error">{error}</div>}

      {report && (
        <>
          {report.note && <div className="perf-note">{report.note}</div>}
          {report.coverage_start && (
            <div className="perf-coverage">
              History begins {report.coverage_start} — where your ledger begins.
              Nothing before it can be reconstructed.
            </div>
          )}

          <Sparkline report={report} unit={shown} />

          <div className="perf-cards">
            <ReturnCard
              title="Time-weighted (TWR)"
              figure={report.twr[shown]}
              explanation="Return per unit of capital — your selection, unaffected by when you deposited."
            />
            <ReturnCard
              title="Money-weighted (MWR)"
              figure={report.mwr[shown]}
              explanation="What your money actually did, including the timing of deposits."
            />
          </div>

          <p className="perf-divergence">
            Where these two differ, the gap is the timing of your deposits — not
            your stock picking.
          </p>

          <div className="perf-realized">
            <div>
              <span className="perf-realized-label">Realized P/L</span>
              <span className={`tone-${tone(report.realized.realized_pnl_krw)}`}>
                {formatKrw(report.realized.realized_pnl_krw)}
              </span>
            </div>
            <div>
              <span className="perf-realized-label">Realized currency P/L</span>
              <span className={`tone-${tone(report.realized.realized_fx_pnl_krw)}`}>
                {formatKrw(report.realized.realized_fx_pnl_krw)}
              </span>
            </div>
            <div>
              <span className="perf-realized-label">Fees</span>
              <span>{formatKrw(report.realized.fees.krw)}</span>
            </div>
            <div>
              <span className="perf-realized-label">Taxes</span>
              <span>{formatKrw(report.realized.taxes.krw)}</span>
            </div>
          </div>

          <p className="perf-basis">
            Computed on an average-cost basis. Korean tax treatment of overseas
            gains is FIFO, so these are figures for deciding with — not for
            filing with.
          </p>
        </>
      )}
    </section>
  );
}
