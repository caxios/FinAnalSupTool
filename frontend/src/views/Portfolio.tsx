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

import { useCallback, useEffect, useState } from "react";
import type {
  Holding,
  JournalReport,
  PerformanceWindow,
  TradeResponse,
} from "../types";
import { useDashboard } from "../context/DashboardContext";
import { useAsync } from "../hooks/useAsync";
import {
  getPortfolio,
  getTrades,
  addHolding,
  removeHolding,
  reviewJournal,
  getCash,
  getCashFlows,
  getPerformance,
  getPortfolioRisk,
} from "../api";
import TradeForm from "../components/portfolio/TradeForm";
import TradeHistory from "../components/portfolio/TradeHistory";
import JournalReview from "../components/portfolio/JournalReview";
import BaselineProgress, {
  baselineInFlight,
} from "../components/portfolio/BaselineProgress";
import Money, {
  CurrencyToggle,
  CurrencyViewProvider,
  formatNative,
  useCurrencyViewState,
} from "../components/portfolio/Money";
import NetWorthHeader from "../components/portfolio/NetWorthHeader";
import CashPanel from "../components/portfolio/CashPanel";
import CashLedger from "../components/portfolio/CashLedger";
import AttributionPanel from "../components/portfolio/AttributionPanel";
import PerformancePanel from "../components/portfolio/PerformancePanel";
import PortfolioRiskPanel from "../components/portfolio/PortfolioRiskPanel";
import PersonalEdgeDashboard from "../components/portfolio/PersonalEdgeDashboard";

/** Map a signed number to the app's existing tone classes. */
function tone(n: number | null | undefined): "positive" | "negative" | "neutral" {
  if (n === null || n === undefined) return "neutral";
  if (n > 0) return "positive";
  if (n < 0) return "negative";
  return "neutral";
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
          ? `${res.holding.ticker} added. Fetching 2 years of SEC filings, then ` +
            `running a Deep Analysis per quarter — this takes a while; progress ` +
            `is shown above.`
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

  const [ledgerTab, setLedgerTab] = useState<"journal" | "cash">("journal");
  const [perfWindow, setPerfWindow] = useState<PerformanceWindow>("all");
  const { view: currencyView, setView: setCurrencyView } = useCurrencyViewState();

  const portfolio = useAsync(() => getPortfolio(), [version]);
  const journal = useAsync(() => getTrades(filterTicker), [version, filterTicker]);
  const cash = useAsync(() => getCash(), [version]);
  const cashFlows = useAsync(() => getCashFlows({ limit: 300 }), [version]);
  const performance = useAsync(() => getPerformance(perfWindow), [version, perfWindow]);
  // Bypass the 5-minute backend cache once a trade/holding/cash change has
  // happened this session (any `version` bump, including the panel's own
  // manual refresh button) — the first load can still use a warm cache.
  const risk = useAsync(() => getPortfolioRisk({ refresh: version > 0 }), [version]);

  // The whole-record review. Scoped by the journal's own ticker filter, so
  // "review my AAPL trades" needs no second control.
  const [journalReport, setJournalReport] = useState<JournalReport | null>(null);
  const [journalReviewing, setJournalReviewing] = useState(false);
  const [journalError, setJournalError] = useState<string | null>(null);

  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  // Ingesting filings and running eight quarterly analyses takes minutes, so
  // poll while either is in flight and stop as soon as it settles. Without this
  // the user sees a one-off note at submit time and then silence.
  const inFlight = baselineInFlight(portfolio.data?.baseline_status);
  useEffect(() => {
    if (!inFlight) return;
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, [inFlight, refresh]);

  async function handleJournalReview() {
    setJournalReviewing(true);
    setJournalError(null);
    try {
      setJournalReport(await reviewJournal({ ticker: filterTicker }));
    } catch (err) {
      setJournalError(
        err instanceof Error ? err.message : "The journal review failed."
      );
    } finally {
      setJournalReviewing(false);
    }
  }

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
    <CurrencyViewProvider view={currencyView}>
    <div className="view-scroll">
      <div className="view-head">
        <h1 className="view-title">Portfolio &amp; Trading Journal</h1>
        <div className="view-head-right">
          <div className="view-subtitle">
            {holdings.length === 0
              ? "Add a position to start tracking"
              : `${holdings.length} position${holdings.length === 1 ? "" : "s"}`}
          </div>
          <CurrencyToggle view={currencyView} onChange={setCurrencyView} />
        </div>
      </div>

      {portfolio.data && <NetWorthHeader data={portfolio.data} />}

      {/* ── Cash ───────────────────────────────────────────── */}
      <section className="view-section">
        <h2 className="section-title">💵 Cash</h2>
        <CashPanel
          cash={cash.data ?? null}
          cashTotals={{
            krw: portfolio.data?.cash_total_krw ?? null,
            usd: portfolio.data?.cash_total_usd ?? null,
          }}
          onChanged={refresh}
        />
      </section>

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

        {portfolio.data?.baseline_status && (
          <BaselineProgress
            statuses={portfolio.data.baseline_status}
            onSelectTicker={setActiveTicker}
          />
        )}

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
                    <th>Weight</th>
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
                      <td className="portfolio-ticker">
                        {h.ticker}
                        {/* Which currency this position actually trades in.
                            Without it, ₩260,000 and $314 sit in one column
                            looking comparable. */}
                        <span className="portfolio-ccy">{h.currency}</span>
                      </td>
                      <td className="portfolio-weight">
                        {h.weight === null ? "—" : percent(h.weight)}
                      </td>
                      <td>{h.quantity}</td>
                      <td>{formatNative(h.avg_price, h.currency)}</td>
                      <td>
                        {h.current_price === null ? (
                          <span
                            className="portfolio-unpriced"
                            title="No market data available for this ticker"
                          >
                            unpriced
                          </span>
                        ) : (
                          formatNative(h.current_price, h.currency)
                        )}
                      </td>
                      <td>
                        <Money
                          krw={h.market_value_krw}
                          usd={h.market_value_usd}
                          compact
                        />
                      </td>
                      <td className={`tone-${tone(h.unrealized_pnl)}`}>
                        <Money
                          krw={h.unrealized_pnl_krw}
                          usd={h.unrealized_pnl_usd}
                          compact
                          signed
                        />
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

                  {/* Cash belongs IN the allocation table, not beside it. It is
                      a position, and separating it invites reading the equity
                      weights as if they were the whole portfolio. */}
                  {Object.entries(cash.data?.balances ?? {})
                    .filter(([, v]) => Math.abs(v) > 1e-9)
                    .map(([ccy, amount]) => {
                      const rate = portfolio.data?.fx?.rate ?? null;
                      const krw = ccy === "KRW" ? amount : rate ? amount * rate : null;
                      const usd = ccy === "USD" ? amount : rate ? amount / rate : null;
                      const netKrw = portfolio.data?.net_worth_krw ?? null;
                      return (
                        <tr key={`cash-${ccy}`} className="portfolio-row-cash">
                          <td className="portfolio-ticker">
                            Cash
                            <span className="portfolio-ccy">{ccy}</span>
                          </td>
                          <td className="portfolio-weight">
                            {netKrw && krw !== null ? percent(krw / netKrw) : "—"}
                          </td>
                          <td>—</td>
                          <td>—</td>
                          <td>—</td>
                          <td>
                            <Money krw={krw} usd={usd} compact />
                          </td>
                          <td>—</td>
                          <td>—</td>
                          <td />
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>

            <div className="portfolio-totals">
              <div className="portfolio-total">
                <span className="portfolio-total-label">Equity</span>
                <span className="portfolio-total-value">
                  <Money
                    krw={totals?.equity_total_krw}
                    usd={totals?.equity_total_usd}
                    compact
                  />
                </span>
              </div>
              <div className="portfolio-total">
                <span className="portfolio-total-label">Cash</span>
                <span className="portfolio-total-value">
                  <Money
                    krw={totals?.cash_total_krw}
                    usd={totals?.cash_total_usd}
                    compact
                  />
                </span>
              </div>
              <div className="portfolio-total">
                <span className="portfolio-total-label">Net worth</span>
                <span className="portfolio-total-value">
                  <Money
                    krw={totals?.net_worth_krw}
                    usd={totals?.net_worth_usd}
                    compact
                  />
                </span>
              </div>
              <div className="portfolio-total">
                <span className="portfolio-total-label">ROI</span>
                <span
                  className={`portfolio-total-value tone-${tone(
                    totals?.roi_krw_total
                  )}`}
                >
                  {percent(totals?.roi_krw_total)}
                  <span className="portfolio-total-alt">
                    {percent(totals?.roi_usd_total)} in USD
                  </span>
                </span>
              </div>
            </div>

            {holdings.some((h) => h.current_price === null) && (
              <p className="portfolio-footnote">
                Unpriced positions are excluded from market value and ROI — their
                cost basis is still counted.
              </p>
            )}

            {/* Silence here would read as a clean bill of health rather than an
                absence of coverage. */}
            {holdings.some((h) => (h.currency || "").toUpperCase() !== "USD") && (
              <p className="portfolio-footnote">
                SEC EDGAR covers US-listed issuers only, so no fundamental
                analysis is available for your non-US holdings. Portfolio
                tracking, risk, and the trading coach still work for them.
              </p>
            )}
          </>
        )}
      </section>

      {/* ── Portfolio risk ──────────────────────────────────── */}
      {holdings.length > 0 && (
        <section className="view-section">
          <h2 className="section-title">⚖️ Portfolio Risk</h2>
          <PortfolioRiskPanel
            report={risk.data}
            loading={risk.loading}
            error={risk.error}
            onRefresh={refresh}
          />
        </section>
      )}

      {/* ── Where the return came from ─────────────────────── */}
      {holdings.length > 0 && portfolio.data && (
        <section className="view-section">
          <h2 className="section-title">🧮 Return Attribution</h2>
          <AttributionPanel
            holdings={holdings}
            totals={{
              roi_krw_total: portfolio.data.roi_krw_total,
              roi_usd_total: portfolio.data.roi_usd_total,
            }}
          />
        </section>
      )}

      {/* ── Performance ────────────────────────────────────── */}
      <section className="view-section">
        <h2 className="section-title">📈 Performance</h2>
        <PerformancePanel
          report={performance.data ?? null}
          loading={performance.loading}
          error={performance.error}
          window={perfWindow}
          onWindowChange={setPerfWindow}
        />
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
        <div className="section-head-row">
          <div className="ledger-tabs">
            <button
              className={`ledger-tab ${ledgerTab === "journal" ? "is-active" : ""}`}
              onClick={() => setLedgerTab("journal")}
            >
              📓 Trading Journal
            </button>
            <button
              className={`ledger-tab ${ledgerTab === "cash" ? "is-active" : ""}`}
              onClick={() => setLedgerTab("cash")}
            >
              🧾 Cash Ledger
            </button>
          </div>
          <button
            className="btn-coach"
            hidden={ledgerTab !== "journal"}
            disabled={journalReviewing || (journal.data?.trades.length ?? 0) === 0}
            onClick={handleJournalReview}
            title={
              filterTicker
                ? `Review your ${filterTicker} trades as a whole`
                : "Review your whole record — patterns, not single decisions"
            }
          >
            {journalReviewing
              ? "Reviewing…"
              : `🧠 Review ${filterTicker ?? "my whole journal"}`}
          </button>
        </div>

        {journalError && (
          <div className="journal-notice journal-error">{journalError}</div>
        )}
        {journalReport && (
          <JournalReview
            report={journalReport}
            onDismiss={() => setJournalReport(null)}
          />
        )}

        {ledgerTab === "journal" ? (
          <TradeHistory
            trades={journal.data?.trades ?? []}
            loading={journal.loading}
            error={journal.error}
            filterTicker={filterTicker}
            onFilterChange={setFilterTicker}
            tickers={tickers}
          />
        ) : (
          <CashLedger
            flows={cashFlows.data?.flows ?? []}
            loading={cashFlows.loading}
            error={cashFlows.error}
          />
        )}
      </section>

      {/* ── Personal Trading Edge ──────────────────────────────── */}
      <section className="view-section">
        <h2 className="section-title">🎯 Personal Trading Edge</h2>
        <PersonalEdgeDashboard />
      </section>
    </div>
    </CurrencyViewProvider>
  );
}
