/**
 * views/Portfolio.tsx
 * ───────────────────
 * View 5 — Portfolio & Trading Journal (blueprint §1).
 *
 * Three sections:
 *   - Holdings   → live valuation table + "add a position" form
 *   - Log a trade → the automation: time + quantity in, fill price out
 *   - Journal    → every trade with the rationale written at the time
 *
 * Unlike the other views this one is NOT scoped to `activeTicker` — a portfolio
 * spans every company at once. It *writes* to that context instead: clicking a
 * holding sets the active company, which is the join between this view and the
 * filing/media/analysis views.
 */

import { useCallback, useState } from "react";
import type { Holding, TradeResponse } from "../types";
import { useDashboard } from "../context/DashboardContext";
import { useAsync } from "../hooks/useAsync";
import { getPortfolio, getTrades, addHolding, removeHolding } from "../api";
import TradeForm from "../components/portfolio/TradeForm";
import TradeHistory from "../components/portfolio/TradeHistory";

/** Map a signed number to the app's existing tone classes. */
function tone(n: number | null | undefined): "positive" | "negative" | "neutral" {
  if (n === null || n === undefined) return "neutral";
  if (n > 0) return "positive";
  if (n < 0) return "negative";
  return "neutral";
}

function money(n: number | null | undefined): string {
  return n === null || n === undefined
    ? "—"
    : n.toLocaleString(undefined, {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      });
}

function percent(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${(n * 100).toFixed(2)}%`;
}

/** Inline form for seeding an existing position. */
function AddHoldingForm({ onAdded }: { onAdded: (ticker: string) => void }) {
  const [ticker, setTicker] = useState("");
  const [quantity, setQuantity] = useState("");
  const [avgPrice, setAvgPrice] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const canSubmit =
    !busy && ticker.trim() !== "" && Number(quantity) > 0 && Number(avgPrice) > 0;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const res = await addHolding({
        ticker: ticker.trim().toUpperCase(),
        quantity: Number(quantity),
        avg_price: Number(avgPrice),
      });
      // Adding a company the app has no filings for kicks off a ~2-year SEC
      // fetch in the background; say so, because it takes a while and the
      // filings will appear in the other views only once it finishes.
      setNote(
        res.baseline_started
          ? `Fetching 2 years of SEC filings for ${res.holding.ticker} in the background…`
          : `${res.holding.ticker} added.`
      );
      setTicker("");
      setQuantity("");
      setAvgPrice("");
      onAdded(res.holding.ticker);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add the holding.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="add-holding-form" onSubmit={submit}>
      <label className="trade-field trade-field-narrow">
        <span className="trade-label">Ticker</span>
        <input
          className="trade-input"
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="AAPL"
          disabled={busy}
        />
      </label>
      <label className="trade-field trade-field-narrow">
        <span className="trade-label">Quantity</span>
        <input
          className="trade-input"
          type="number"
          min="0"
          step="any"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
          placeholder="10"
          disabled={busy}
        />
      </label>
      <label className="trade-field trade-field-narrow">
        <span className="trade-label">Average price</span>
        <input
          className="trade-input"
          type="number"
          min="0"
          step="any"
          value={avgPrice}
          onChange={(e) => setAvgPrice(e.target.value)}
          placeholder="150.00"
          disabled={busy}
        />
      </label>
      <button className="btn-primary" type="submit" disabled={!canSubmit}>
        {busy ? "Adding…" : "Add position"}
      </button>
      {error && <div className="trade-error">{error}</div>}
      {note && <div className="add-holding-note">{note}</div>}
    </form>
  );
}

export default function Portfolio() {
  const { activeTicker, setActiveTicker } = useDashboard();

  // Bumped after any write, to re-run both fetches below.
  const [version, setVersion] = useState(0);
  const [filterTicker, setFilterTicker] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);

  const portfolio = useAsync(() => getPortfolio(), [version]);
  const journal = useAsync(() => getTrades(filterTicker), [version, filterTicker]);

  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  const holdings: Holding[] = portfolio.data?.holdings ?? [];
  const tickers = holdings.map((h) => h.ticker);

  const handleLogged = useCallback(
    (res: TradeResponse) => {
      refresh();
      // Follow the user to the company they just traded, so the filing, media,
      // and analysis views line up with what they were thinking about.
      setActiveTicker(res.trade.ticker);
    },
    [refresh, setActiveTicker]
  );

  async function handleRemove(ticker: string) {
    try {
      await removeHolding(ticker);
      refresh();
    } catch (err) {
      console.error("Failed to remove holding:", err);
    }
  }

  const totals = portfolio.data;

  return (
    <div className="view-scroll">
      <div className="view-head">
        <h1 className="view-title">Portfolio &amp; Trading Journal</h1>
        <div className="view-subtitle">
          {holdings.length === 0
            ? "Add a position to start tracking"
            : `${holdings.length} position${holdings.length === 1 ? "" : "s"}`}
        </div>
      </div>

      {/* ── Holdings ───────────────────────────────────────── */}
      <section className="view-section">
        <div className="section-head-row">
          <h2 className="section-title">💼 Holdings</h2>
          <button
            className="btn-secondary-sm"
            onClick={() => setShowAdd((v) => !v)}
          >
            {showAdd ? "Cancel" : "+ Add position"}
          </button>
        </div>

        {showAdd && (
          <AddHoldingForm
            onAdded={(t) => {
              refresh();
              setActiveTicker(t);
              setShowAdd(false);
            }}
          />
        )}

        {portfolio.loading && (
          <div className="journal-notice">Loading your portfolio…</div>
        )}
        {portfolio.error && (
          <div className="journal-notice journal-error">{portfolio.error}</div>
        )}

        {!portfolio.loading && !portfolio.error && holdings.length === 0 && (
          <div className="journal-notice">
            No positions yet. Add one above — the app will pull its last 2 years
            of SEC filings automatically.
          </div>
        )}

        {holdings.length > 0 && (
          <>
            <div className="table-wrapper">
              <table className="fin-table portfolio-table">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Qty</th>
                    <th>Avg price</th>
                    <th>Current</th>
                    <th>Market value</th>
                    <th>Unrealized P/L</th>
                    <th>ROI</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {holdings.map((h) => (
                    <tr
                      key={h.ticker}
                      className={`portfolio-row ${
                        activeTicker === h.ticker ? "portfolio-row-active" : ""
                      }`}
                      onClick={() => setActiveTicker(h.ticker)}
                      title={`Make ${h.ticker} the active company`}
                    >
                      <td className="portfolio-ticker">{h.ticker}</td>
                      <td>{h.quantity}</td>
                      <td>{money(h.avg_price)}</td>
                      <td>
                        {h.current_price === null ? (
                          <span
                            className="portfolio-unpriced"
                            title="No market data available for this ticker"
                          >
                            unpriced
                          </span>
                        ) : (
                          money(h.current_price)
                        )}
                      </td>
                      <td>{money(h.market_value)}</td>
                      <td className={`tone-${tone(h.unrealized_pnl)}`}>
                        {h.unrealized_pnl === null
                          ? "—"
                          : `${h.unrealized_pnl > 0 ? "+" : ""}${money(
                              h.unrealized_pnl
                            )}`}
                      </td>
                      <td className={`tone-${tone(h.unrealized_roi)}`}>
                        {h.unrealized_roi === null
                          ? "—"
                          : `${h.unrealized_roi > 0 ? "+" : ""}${percent(
                              h.unrealized_roi
                            )}`}
                      </td>
                      <td>
                        <button
                          className="btn-remove"
                          title={`Remove ${h.ticker} and its journal entries`}
                          onClick={(e) => {
                            // Don't also trigger the row's select handler.
                            e.stopPropagation();
                            handleRemove(h.ticker);
                          }}
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="portfolio-totals">
              <div className="portfolio-total">
                <span className="portfolio-total-label">Cost basis</span>
                <span className="portfolio-total-value">
                  {money(totals?.total_cost_basis)}
                </span>
              </div>
              <div className="portfolio-total">
                <span className="portfolio-total-label">Market value</span>
                <span className="portfolio-total-value">
                  {money(totals?.total_market_value)}
                </span>
              </div>
              <div className="portfolio-total">
                <span className="portfolio-total-label">Unrealized P/L</span>
                <span
                  className={`portfolio-total-value tone-${tone(
                    totals?.total_unrealized_pnl
                  )}`}
                >
                  {money(totals?.total_unrealized_pnl)}
                </span>
              </div>
              <div className="portfolio-total">
                <span className="portfolio-total-label">Total ROI</span>
                <span
                  className={`portfolio-total-value tone-${tone(totals?.total_roi)}`}
                >
                  {percent(totals?.total_roi)}
                </span>
              </div>
            </div>

            {holdings.some((h) => h.current_price === null) && (
              <p className="portfolio-footnote">
                Unpriced positions are excluded from market value and ROI — their
                cost basis is still counted.
              </p>
            )}
          </>
        )}
      </section>

      {/* ── Log a trade ────────────────────────────────────── */}
      <section className="view-section">
        <h2 className="section-title">✍️ Log a Trade</h2>
        <TradeForm
          knownTickers={tickers}
          defaultTicker={activeTicker}
          onLogged={handleLogged}
        />
      </section>

      {/* ── Journal ────────────────────────────────────────── */}
      <section className="view-section">
        <h2 className="section-title">📓 Trading Journal</h2>
        <TradeHistory
          trades={journal.data?.trades ?? []}
          loading={journal.loading}
          error={journal.error}
          filterTicker={filterTicker}
          onFilterChange={setFilterTicker}
          tickers={tickers}
        />
      </section>
    </div>
  );
}
