/**
 * TradeForm.tsx
 * ─────────────
 * Log a trade — blueprint §1's "Smart Trading Journal Automation".
 *
 * The user types only WHEN and HOW MUCH. There is deliberately no price field:
 * the backend resolves the fill from intraday market data at that timestamp and
 * returns it, which is the whole point of the automation. The one price input is
 * an explicit "correct the fill" escape hatch, kept behind a toggle so it never
 * reads as a required step.
 *
 * The **Entry Rationale** textarea is the other half of the feature. The Coach
 * agent (phase 6) evaluates exactly this text against objective data to name
 * psychological biases, so it gets real vertical space and a prompt that invites
 * honesty rather than post-hoc justification.
 */

import { useState } from "react";
import type { CoachReport, Trade, TradeResponse } from "../../types";
import { logTrade, reviewTrade } from "../../api";
import CoachReview from "./CoachReview";

interface TradeFormProps {
  /** Tickers already held, offered as suggestions. */
  knownTickers: string[];
  /** Pre-fill the ticker (e.g. the row the user clicked). */
  defaultTicker?: string | null;
  /** Called after a successful log so the parent can refetch. */
  onLogged: (result: TradeResponse) => void;
}

/**
 * `datetime-local` wants "YYYY-MM-DDTHH:mm" in LOCAL time with no zone. Build
 * that from the current time so the field opens on "now" — the common case is
 * logging a trade just after making it.
 */
function nowLocalInput(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/**
 * Convert the local-time input back to an absolute UTC instant.
 *
 * This matters: the backend looks up the price bar at this exact moment, so
 * sending a naive local string would silently shift the fill by the user's
 * UTC offset. `new Date(localString)` parses as local time, and `toISOString()`
 * emits the correct UTC instant.
 */
function toUtcIso(localValue: string): string {
  return new Date(localValue).toISOString();
}

function money(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `$${n.toFixed(2)}`;
}

/** Confirmation panel: makes the server-derived numbers visible to the user. */
function TradeConfirmation({
  result,
  onDismiss,
}: {
  result: TradeResponse;
  onDismiss: () => void;
}) {
  const { trade, holding, price_resolution: res } = result;
  return (
    <div className="trade-confirm">
      <div className="trade-confirm-head">
        <span className="trade-confirm-title">
          ✓ Logged {trade.side} {trade.quantity} {trade.ticker}
        </span>
        <button className="btn-close" onClick={onDismiss} title="Dismiss">
          ✕
        </button>
      </div>

      <div className="trade-confirm-grid">
        <div className="trade-confirm-item">
          <span className="trade-confirm-label">Execution price</span>
          <span className="trade-confirm-value">
            {money(trade.execution_price)}
          </span>
        </div>
        <div className="trade-confirm-item">
          <span className="trade-confirm-label">Total value</span>
          <span className="trade-confirm-value">{money(trade.total_value)}</span>
        </div>
        <div className="trade-confirm-item">
          <span className="trade-confirm-label">New average</span>
          <span className="trade-confirm-value">
            {money(holding?.avg_price ?? trade.avg_price_after)}
          </span>
        </div>
        <div className="trade-confirm-item">
          <span className="trade-confirm-label">Position</span>
          <span className="trade-confirm-value">
            {holding ? `${holding.quantity} sh` : "closed"}
          </span>
        </div>
      </div>

      {/* Never present an approximate fill as an exact one. */}
      {res && (
        <div
          className={`trade-confirm-note ${
            res.is_approximate ? "trade-confirm-approx" : ""
          }`}
        >
          {res.is_approximate ? "≈ " : ""}
          {res.message}
        </div>
      )}
    </div>
  );
}

export default function TradeForm({
  knownTickers,
  defaultTicker,
  onLogged,
}: TradeFormProps) {
  const [ticker, setTicker] = useState(defaultTicker ?? "");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [executedAt, setExecutedAt] = useState(nowLocalInput());
  const [quantity, setQuantity] = useState("");
  const [rationale, setRationale] = useState("");
  const [overridePrice, setOverridePrice] = useState("");
  const [showOverride, setShowOverride] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TradeResponse | null>(null);

  // Pre-trade coaching: available once a rationale exists, because the
  // rationale is what the coach evaluates.
  const [coaching, setCoaching] = useState(false);
  const [coachReport, setCoachReport] = useState<CoachReport | null>(null);
  const [coachError, setCoachError] = useState<string | null>(null);

  const qty = Number(quantity);
  const canSubmit =
    !submitting && ticker.trim() !== "" && quantity !== "" && qty > 0 && !!executedAt;
  // The coach needs something to evaluate; a blank rationale has no logic in it.
  const canReview = !coaching && !submitting && rationale.trim().length > 0;

  async function handleReview() {
    if (!canReview) return;
    setCoaching(true);
    setCoachError(null);
    setCoachReport(null);
    try {
      setCoachReport(
        await reviewTrade({
          ticker: ticker.trim().toUpperCase() || null,
          proposed_side: side,
          proposed_quantity: qty > 0 ? qty : null,
          entry_rationale: rationale.trim(),
        })
      );
    } catch (err) {
      setCoachError(
        err instanceof Error ? err.message : "The coach review failed."
      );
    } finally {
      setCoaching(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const res = await logTrade({
        ticker: ticker.trim().toUpperCase(),
        side,
        quantity: qty,
        executed_at: toUtcIso(executedAt),
        entry_rationale: rationale.trim() || null,
        // Omitted unless the user explicitly opened the override.
        execution_price:
          showOverride && overridePrice !== "" ? Number(overridePrice) : null,
      });
      setResult(res);
      onLogged(res);
      // Clear the per-trade fields; keep ticker and side so logging a follow-up
      // trade on the same position doesn't mean retyping everything.
      setQuantity("");
      setRationale("");
      setOverridePrice("");
      setExecutedAt(nowLocalInput());
      // The review described a decision that is now made; keeping it on screen
      // would read as commentary on the NEXT trade.
      setCoachReport(null);
      setCoachError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to log the trade.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="trade-form" onSubmit={handleSubmit}>
      <div className="trade-form-row">
        <label className="trade-field">
          <span className="trade-label">Ticker</span>
          <input
            className="trade-input"
            list="portfolio-tickers"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder="AAPL"
            disabled={submitting}
          />
          <datalist id="portfolio-tickers">
            {knownTickers.map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>
        </label>

        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Side</span>
          <select
            className="trade-input trade-side-select"
            value={side}
            onChange={(e) => setSide(e.target.value as "buy" | "sell")}
            disabled={submitting}
          >
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </label>

        <label className="trade-field">
          <span className="trade-label">Transaction time</span>
          <input
            className="trade-input"
            type="datetime-local"
            value={executedAt}
            onChange={(e) => setExecutedAt(e.target.value)}
            disabled={submitting}
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
            disabled={submitting}
          />
        </label>
      </div>

      <p className="trade-form-hint">
        No price field — the execution price is looked up from market data at the
        time you enter above.
      </p>

      {/* ── Entry Rationale: the feature's centerpiece, not a footnote. ── */}
      <label className="trade-field trade-field-rationale">
        <span className="trade-label trade-label-emphasis">
          Entry Rationale (진입 이유)
        </span>
        <textarea
          className="trade-textarea"
          value={rationale}
          onChange={(e) => setRationale(e.target.value)}
          rows={4}
          disabled={submitting}
          placeholder={
            "Why now? What are you feeling — conviction, FOMO, fear?\n" +
            "Be honest: your coach compares this against what the data actually said."
          }
        />
        <span className="trade-help">
          Written in your own words. The Coach agent reads this to spot patterns
          across your trades.
        </span>
      </label>

      {/* Manual override, tucked away so it never looks like a required field. */}
      <div className="trade-override">
        <button
          type="button"
          className="trade-override-toggle"
          onClick={() => setShowOverride((v) => !v)}
          disabled={submitting}
        >
          {showOverride ? "▾" : "▸"} Correct the fill price manually
        </button>
        {showOverride && (
          <label className="trade-field trade-field-narrow">
            <span className="trade-label">Execution price</span>
            <input
              className="trade-input"
              type="number"
              min="0"
              step="any"
              value={overridePrice}
              onChange={(e) => setOverridePrice(e.target.value)}
              placeholder="Leave blank to auto-detect"
              disabled={submitting}
            />
          </label>
        )}
      </div>

      <div className="trade-form-actions">
        <button
          type="button"
          className="btn-coach"
          onClick={handleReview}
          disabled={!canReview}
          title={
            rationale.trim()
              ? "Have the coach review this before you commit"
              : "Write your entry rationale first — that's what the coach reviews"
          }
        >
          {coaching ? "Reviewing…" : "🧠 Get coach review"}
        </button>
        <button className="btn-primary" type="submit" disabled={!canSubmit}>
          {submitting ? "Looking up the fill…" : "Log trade"}
        </button>
      </div>

      {error && <div className="trade-error">{error}</div>}
      {coachError && <div className="trade-error">{coachError}</div>}
      {coachReport && (
        <CoachReview
          report={coachReport}
          onDismiss={() => setCoachReport(null)}
        />
      )}
      {result && (
        <TradeConfirmation result={result} onDismiss={() => setResult(null)} />
      )}
    </form>
  );
}

export type { Trade };
