/**
 * WhatIfSimulator.tsx
 * ────────────────────
 * Pre-Trade "What-If" Position Simulator — a sandbox for testing the impact
 * of adding (or resizing) a position BEFORE committing real capital.
 *
 * Weight-sensitive by construction: the backend (`services.risk_metrics
 * .simulate_new_position_impact`, via `POST /portfolio/simulate`) builds two
 * REAL simulated portfolio return series — before and after the trade — so a
 * 1% allocation and a 35% allocation of the same volatile ticker naturally
 * produce very different deltas. There is no separate "size adjustment" step
 * that could get that wrong.
 *
 * Also the only simulator in the app that works for a ticker the user does
 * NOT hold: the backend fetches its price history on the fly and aligns it
 * to the existing book's trading days.
 */

import { useState } from "react";
import type { PortfolioSimulationResponse, TradeSide } from "../../types";
import { simulatePortfolioAddition } from "../../api";

function pct(n: number | null | undefined, digits = 1): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(digits)}%`;
}

function signedPp(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${n > 0 ? "+" : ""}${(n * 100).toFixed(1)}pp`;
}

function ratio(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toFixed(2);
}

/** Green when the delta is the "good" direction for this metric, red when not. */
function deltaTone(metric: "sharpe_ratio" | "volatility" | "max_drawdown" | "value_at_risk", delta: number | null | undefined): string {
  if (delta === null || delta === undefined || Math.abs(delta) < 1e-9) return "neutral";
  const higherIsBetter = metric === "sharpe_ratio";
  const improved = higherIsBetter ? delta > 0 : delta < 0;
  return improved ? "positive" : "negative";
}

const VERDICT_LABEL: Record<string, { emoji: string; label: string }> = {
  diversifying: { emoji: "🟢", label: "Diversifying" },
  concentrating: { emoji: "🔴", label: "Concentrating" },
  high_impact: { emoji: "🟡", label: "High Impact" },
  neutral: { emoji: "⚪", label: "Modest Impact" },
};

function KpiRow({
  title, before, after, delta, format, metric,
}: {
  title: string;
  before: number | null | undefined;
  after: number | null | undefined;
  delta: number | null | undefined;
  format: (n: number | null | undefined) => string;
  metric: "sharpe_ratio" | "volatility" | "max_drawdown" | "value_at_risk";
}) {
  const tone = deltaTone(metric, delta);
  return (
    <div className="whatif-kpi-row">
      <span className="whatif-kpi-title">{title}</span>
      <span className="whatif-kpi-before">{format(before)}</span>
      <span className="whatif-kpi-arrow">→</span>
      <span className="whatif-kpi-after">{format(after)}</span>
      <span className={`whatif-kpi-delta tone-${tone}`}>
        {tone === "positive" ? "🟢 " : tone === "negative" ? "🔴 " : ""}
        {metric === "sharpe_ratio" ? (delta === null || delta === undefined ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(2)}`) : signedPp(delta)}
      </span>
    </div>
  );
}

interface WhatIfSimulatorProps {
  /** Tickers already held, offered as suggestions. */
  knownTickers: string[];
  /** "Log Trade as Buy" / "Log as Observe/Pass" — hands the ticker+side up
   * to the parent so it can pre-fill the Trade Form. */
  onQuickAction: (ticker: string, side: TradeSide) => void;
}

export default function WhatIfSimulator({ knownTickers, onQuickAction }: WhatIfSimulatorProps) {
  const [ticker, setTicker] = useState("");
  const [sizeMode, setSizeMode] = useState<"weight" | "dollar">("weight");
  const [weightPct, setWeightPct] = useState("10");
  const [dollarAmount, setDollarAmount] = useState("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PortfolioSimulationResponse | null>(null);

  const canSimulate =
    !loading &&
    ticker.trim() !== "" &&
    (sizeMode === "weight"
      ? weightPct !== "" && Number(weightPct) > 0
      : dollarAmount !== "" && Number(dollarAmount) > 0);

  async function handleSimulate() {
    if (!canSimulate) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await simulatePortfolioAddition(
        ticker.trim().toUpperCase(),
        sizeMode === "weight" ? Number(weightPct) / 100 : null,
        sizeMode === "dollar" ? Number(dollarAmount) : null
      );
      setResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Simulation failed.");
    } finally {
      setLoading(false);
    }
  }

  const t = ticker.trim().toUpperCase();
  const verdict = result?.verdict ? VERDICT_LABEL[result.verdict] : null;

  return (
    <div className="whatif-simulator">
      <p className="risk-section-note">
        Test the impact of adding (or resizing) a position — held or brand
        new — before committing real capital. The comparison is
        weight-sensitive: a small allocation of a volatile stock and a large
        one produce correspondingly different results.
      </p>

      <div className="whatif-inputs">
        <label className="trade-field">
          <span className="trade-label">Ticker</span>
          <input
            className="trade-input"
            list="whatif-tickers"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="AVGO, TSLA, MRVL…"
            disabled={loading}
          />
          <datalist id="whatif-tickers">
            {knownTickers.map((kt) => (
              <option key={kt} value={kt} />
            ))}
          </datalist>
        </label>

        <div className="whatif-size-toggle">
          <button
            type="button"
            className={`whatif-size-btn ${sizeMode === "weight" ? "is-active" : ""}`}
            onClick={() => setSizeMode("weight")}
            disabled={loading}
          >
            % of Net Worth
          </button>
          <button
            type="button"
            className={`whatif-size-btn ${sizeMode === "dollar" ? "is-active" : ""}`}
            onClick={() => setSizeMode("dollar")}
            disabled={loading}
          >
            Dollar Amount
          </button>
        </div>

        {sizeMode === "weight" ? (
          <label className="trade-field">
            <span className="trade-label">Target weight: {weightPct}%</span>
            <input
              type="range"
              min={1}
              max={50}
              step={1}
              value={weightPct}
              onChange={(e) => setWeightPct(e.target.value)}
              disabled={loading}
              className="whatif-slider"
            />
          </label>
        ) : (
          <label className="trade-field trade-field-narrow">
            <span className="trade-label">Amount (base currency)</span>
            <input
              className="trade-input"
              type="number"
              min="0"
              step="any"
              value={dollarAmount}
              onChange={(e) => setDollarAmount(e.target.value)}
              placeholder="10000000"
              disabled={loading}
            />
          </label>
        )}

        <button
          className="btn-primary"
          onClick={handleSimulate}
          disabled={!canSimulate}
        >
          {loading ? "Simulating…" : "🧪 Simulate Position Impact"}
        </button>
      </div>

      {error && <div className="trade-error">{error}</div>}

      {result && result.error && (
        <div className="trade-error">{result.error}</div>
      )}

      {result && !result.error && result.before && result.after && (
        <div className="whatif-result">
          <div className="whatif-result-head">
            <span>
              <strong>{result.ticker}</strong> from {pct(result.weight_before)} to{" "}
              {pct(result.weight_after)} of net worth
              {result.funded_from && ` (funded from ${result.funded_from})`}
            </span>
            {verdict && (
              <span className={`whatif-verdict whatif-verdict-${result.verdict}`}>
                {verdict.emoji} {verdict.label}
              </span>
            )}
          </div>

          <div className="whatif-kpi-grid">
            <div className="whatif-kpi-header">
              <span />
              <span>Before</span>
              <span />
              <span>After</span>
              <span>Δ</span>
            </div>
            <KpiRow
              title="Sharpe Ratio" metric="sharpe_ratio"
              before={result.before.sharpe_ratio} after={result.after.sharpe_ratio}
              delta={result.delta?.sharpe_ratio_delta} format={ratio}
            />
            <KpiRow
              title="Annualized Volatility" metric="volatility"
              before={result.before.volatility} after={result.after.volatility}
              delta={result.delta?.volatility_delta} format={(n) => pct(n)}
            />
            <KpiRow
              title="Max Drawdown" metric="max_drawdown"
              before={result.before.max_drawdown} after={result.after.max_drawdown}
              delta={result.delta?.max_drawdown_delta} format={(n) => pct(n)}
            />
            <KpiRow
              title="95% Daily VaR" metric="value_at_risk"
              before={result.before.value_at_risk} after={result.after.value_at_risk}
              delta={result.delta?.value_at_risk_delta} format={(n) => pct(n)}
            />
          </div>

          {result.correlation_to_book !== null && result.correlation_to_book !== undefined && (
            <div className="whatif-corr-badge">
              Correlation to existing book:{" "}
              <strong
                className={result.correlation_to_book > 0.75 ? "tone-negative" : "tone-neutral"}
              >
                {result.correlation_to_book.toFixed(2)}
              </strong>
            </div>
          )}

          {result.commentary && (
            <p className="whatif-commentary">{result.commentary}</p>
          )}

          <div className="whatif-actions">
            <button
              className="btn-secondary-sm"
              onClick={() => onQuickAction(t, "buy")}
            >
              📥 Log Trade as Buy
            </button>
            <button
              className="btn-secondary-sm"
              onClick={() => onQuickAction(t, "observe")}
            >
              📓 Log as Observe/Pass
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
