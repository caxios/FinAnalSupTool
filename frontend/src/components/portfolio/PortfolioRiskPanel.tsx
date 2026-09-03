/**
 * PortfolioRiskPanel.tsx
 * ───────────────────────
 * Whole-portfolio quantitative risk — VaR, CVaR, annualized volatility, max
 * drawdown, per-position risk contribution vs. capital weight, the pairwise
 * correlation matrix, and FX risk.
 *
 * This used to be computed inside Deep Analysis (the retired `quant_risk`
 * agent buried in an accordion, one company's report at a time). It is
 * portfolio-wide math, so it now lives here instead, fed directly by
 * GET /portfolio/risk (`services.portfolio_risk` → `services.risk_metrics`,
 * unchanged) — no LLM, just the numbers.
 *
 * Everything is in BASE CURRENCY (KRW) and weights are shares of NET WORTH
 * (positions plus cash), matching the rest of the dual-currency system —
 * position weights sum to less than 1; the remainder is cash.
 */

import type { PortfolioRiskReport, RiskPosition, RiskCashPosition } from "../../types";

function pct(n: number | null | undefined, signed = false): string {
  if (n === null || n === undefined) return "—";
  const sign = signed && n > 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(2)}%`;
}

/** Diverging green (low/negative) → yellow → red (>0.7) scale for a correlation cell. */
function corrColor(v: number): string {
  const clamped = Math.max(-1, Math.min(1, v));
  const hue = (1 - (clamped + 1) / 2) * 120; // -1 -> 120 (green), 1 -> 0 (red)
  return `hsl(${hue.toFixed(0)}, 62%, 42%)`;
}

function KpiCard({
  title, value, tone, note,
}: { title: string; value: string; tone?: "warn" | "neutral"; note?: string }) {
  return (
    <div className={`risk-kpi-card ${tone === "warn" ? "risk-kpi-warn" : ""}`}>
      <div className="risk-kpi-title">{title}</div>
      <div className="risk-kpi-value">{value}</div>
      {note && <div className="risk-kpi-note">{note}</div>}
    </div>
  );
}

/** One row of the Risk vs Capital table: a position's weight bar next to its risk-share bar. */
function AllocationRow({
  label, ccy, weight, riskShare, note,
}: {
  label: string; ccy?: string; weight: number; riskShare: number | null; note?: string;
}) {
  const punchesAbove = riskShare !== null && riskShare > weight * 1.5 && riskShare > 0.15;
  return (
    <div className="risk-alloc-row">
      <div className="risk-alloc-label">
        {label}
        {ccy && <span className="portfolio-ccy">{ccy}</span>}
      </div>
      <div className="risk-alloc-bars">
        <div className="risk-alloc-bar-line">
          <span className="risk-alloc-bar-tag">capital</span>
          <div className="attr-bar-track">
            <div
              className="attr-bar-fill tone-bg-neutral"
              style={{ width: `${Math.min(Math.abs(weight) * 100, 100)}%` }}
            />
          </div>
          <span className="risk-alloc-bar-value">{pct(weight)}</span>
        </div>
        <div className="risk-alloc-bar-line">
          <span className="risk-alloc-bar-tag">risk</span>
          <div className="attr-bar-track">
            <div
              className={`attr-bar-fill ${punchesAbove ? "tone-bg-negative" : "tone-bg-neutral"}`}
              style={{ width: `${Math.min(Math.abs(riskShare ?? 0) * 100, 100)}%` }}
            />
          </div>
          <span className="risk-alloc-bar-value">
            {riskShare === null ? "—" : pct(riskShare)}
          </span>
        </div>
      </div>
      {(punchesAbove || note) && (
        <div className="risk-alloc-flag">
          {note ?? "carries more risk than its capital weight suggests"}
        </div>
      )}
    </div>
  );
}

export default function PortfolioRiskPanel({
  report, loading, error, onRefresh,
}: {
  report: PortfolioRiskReport | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  if (loading) {
    return <div className="journal-notice">Computing portfolio risk…</div>;
  }
  if (error) {
    return <div className="journal-notice journal-error">{error}</div>;
  }
  if (!report || report.portfolio_volatility === null) {
    return (
      <div className="journal-notice">
        {report?.data_quality?.note ??
          "Not enough price history yet to compute portfolio risk."}
      </div>
    );
  }

  const positions: RiskPosition[] = report.positions ?? [];
  const cashRows: RiskCashPosition[] = report.cash_positions ?? [];
  const corr = report.correlation_matrix ?? {};
  const corrTickers = Object.keys(corr);
  const fx = report.fx_risk ?? {};
  const cashBlock = report.cash ?? {};
  const conc = report.concentration ?? {};

  return (
    <div className="risk-panel">
      <div className="risk-panel-head">
        {!report.data_quality.sufficient && (
          <div className="perf-note">{report.data_quality.note}</div>
        )}
        <button className="btn-secondary-sm" onClick={onRefresh} title="Refetch (bypasses the 5-minute cache)">
          ↻ Refresh
        </button>
      </div>

      <div className="risk-kpis">
        <KpiCard
          title={`95% Daily VaR`}
          value={pct(report.value_at_risk)}
          tone={report.value_at_risk && report.value_at_risk > 0.03 ? "warn" : "neutral"}
          note="On 95% of trading days, loss should not exceed this."
        />
        <KpiCard
          title="Expected Shortfall (CVaR)"
          value={pct(report.conditional_var)}
          note="Average loss on the days that breach VaR."
        />
        <KpiCard
          title="Annualized Volatility"
          value={pct(report.portfolio_volatility)}
          note="Standard deviation of daily returns, annualized."
        />
        <KpiCard
          title="Max Drawdown"
          value={pct(report.max_drawdown)}
          note={report.period ? `Over ${report.period}` : "Peak-to-trough decline"}
        />
      </div>

      {/* ── Risk vs Capital Allocation ─────────────────────── */}
      <div className="risk-section">
        <h3 className="risk-section-title">Risk vs. Capital Allocation</h3>
        <p className="risk-section-note">
          Capital weight vs. share of total portfolio risk — a position far
          above its own weight in the risk bar is concentration a position
          size alone does not show.
        </p>
        <div className="risk-alloc-list">
          {positions.map((p) => (
            <AllocationRow
              key={p.ticker}
              label={p.ticker}
              weight={p.weight}
              riskShare={p.risk_contribution_pct}
            />
          ))}
          {cashRows.map((c) => (
            <AllocationRow
              key={`cash-${c.currency}`}
              label="Cash"
              ccy={c.currency}
              weight={c.weight}
              riskShare={c.risk_contribution_pct}
              note={
                c.currency !== "KRW"
                  ? "foreign cash carries the exchange rate's own volatility"
                  : undefined
              }
            />
          ))}
        </div>
        <p className="risk-alloc-footnote">
          Largest position: <strong>{conc.largest_position ?? "—"}</strong> at{" "}
          {pct(conc.largest_weight)} of net worth · Herfindahl{" "}
          {conc.herfindahl?.toFixed(3) ?? "—"} · Cash {pct(conc.cash_weight)}
        </p>
      </div>

      {/* ── Correlation matrix ─────────────────────────────── */}
      {corrTickers.length >= 2 && (
        <div className="risk-section">
          <h3 className="risk-section-title">Correlation Matrix</h3>
          <p className="risk-section-note">
            Average pairwise correlation {report.average_correlation?.toFixed(2) ?? "—"}
            {report.average_correlation !== null && report.average_correlation !== undefined &&
              report.average_correlation > 0.7 &&
              " — these holdings largely move together; diversification here is limited."}
          </p>
          <div className="table-wrapper">
            <table className="risk-corr-table">
              <thead>
                <tr>
                  <th />
                  {corrTickers.map((t) => (
                    <th key={t}>{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {corrTickers.map((row) => (
                  <tr key={row}>
                    <th>{row}</th>
                    {corrTickers.map((col) => {
                      const v = corr[row]?.[col];
                      return (
                        <td
                          key={col}
                          className="risk-corr-cell"
                          style={
                            v === undefined
                              ? undefined
                              : { background: corrColor(v), color: "#fff" }
                          }
                        >
                          {v === undefined ? "—" : v.toFixed(2)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Currency & FX risk ──────────────────────────────── */}
      <div className="risk-section">
        <h3 className="risk-section-title">Currency &amp; FX Risk</h3>
        <div className="risk-fx-grid">
          <div>
            <span className="risk-fx-label">Foreign-currency exposure</span>
            <span>{pct(fx.exposure)} of net worth</span>
          </div>
          <div>
            <span className="risk-fx-label">FX volatility</span>
            <span>{pct(fx.fx_volatility)}</span>
          </div>
          <div>
            <span className="risk-fx-label">FX VaR (95%)</span>
            <span>{pct(fx.fx_var)}</span>
          </div>
          <div>
            <span className="risk-fx-label">Equity ↔ FX correlation</span>
            <span>{fx.equity_fx_correlation?.toFixed(2) ?? "—"}</span>
          </div>
          <div>
            <span className="risk-fx-label">Hedged volatility (no FX)</span>
            <span>{pct(fx.hedged_volatility)}</span>
          </div>
          <div>
            <span className="risk-fx-label">FX contribution</span>
            <span className={`tone-${(fx.fx_contribution ?? 0) < 0 ? "positive" : "negative"}`}>
              {pct(fx.fx_contribution, true)}
            </span>
          </div>
        </div>
        {fx.note && <p className="risk-section-note">{fx.note}</p>}
        {cashBlock.cash_drag_note && (
          <p className="risk-section-note">
            Cash drag: {cashBlock.cash_drag !== null && cashBlock.cash_drag !== undefined
              ? pct(cashBlock.cash_drag)
              : "—"} — {cashBlock.cash_drag_note}
          </p>
        )}
      </div>

      {/* ── Scenarios ───────────────────────────────────────── */}
      {report.scenarios && report.scenarios.length > 0 && (
        <div className="risk-section">
          <h3 className="risk-section-title">What-If Scenarios</h3>
          <ul className="risk-scenario-list">
            {report.scenarios.map((s, i) => (
              <li key={i} className="risk-scenario-item">
                {s.ticker ? (
                  <>
                    <strong>{s.ticker}</strong> +{pct(s.delta_weight)} of net worth:{" "}
                    volatility {pct(s.volatility_before)} → {pct(s.volatility_after)}
                    {s.funded_from ? ` (funded from ${s.funded_from})` : ""}
                  </>
                ) : (
                  <>
                    Convert {pct(s.share)} of {s.from_currency} cash to {s.to_currency}:{" "}
                    volatility {pct(s.volatility_before)} → {pct(s.volatility_after)}
                  </>
                )}
                {s.note && <div className="risk-scenario-note">{s.note}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.excluded_tickers && report.excluded_tickers.length > 0 && (
        <p className="risk-alloc-footnote">
          Excluded (no usable price history): {report.excluded_tickers.join(", ")}
        </p>
      )}
    </div>
  );
}
