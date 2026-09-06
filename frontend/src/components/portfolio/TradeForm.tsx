/**
 * TradeForm.tsx
 * ─────────────
 * Log a trade — blueprint §1's "Smart Trading Journal Automation" — unified
 * with non-executed decisions/reflections into ONE form via the Side field.
 *
 * `side` is the single control that decides everything: 'buy'/'sell' is a
 * real execution (quantity required, updates holdings/cash); 'observe'/
 * 'contemplating'/'note' is a non-executed reflection (no quantity, nothing
 * about the portfolio changes). There is no separate diary tab or mode toggle
 * — expanding this one dropdown is the whole feature, matching the single
 * `trades.side` column it is stored in.
 *
 * There is deliberately no price field for a trade: the backend resolves the
 * fill from intraday market data at that timestamp and returns it, which is
 * the whole point of the automation. For a reflection, the same lookup gives
 * a BENCHMARK price snapshot instead, so a later query can ask what the stock
 * did afterwards. The one manual price input is an explicit "correct it"
 * escape hatch, kept behind a toggle so it never reads as a required step.
 *
 * The **Entry Rationale** textarea is the other half of the feature. The Coach
 * agent evaluates exactly this text against objective data to name
 * psychological biases — whether the user pulled the trigger or hesitated —
 * so it gets real vertical space and a prompt that invites honesty rather
 * than post-hoc justification.
 */

import { useState } from "react";
import type { CoachReport, EmotionTag, Trade, TradeCreate, TradeResponse } from "../../types";
import { logTrade, reviewTrade } from "../../api";
import CoachReview from "./CoachReview";

type Side = TradeCreate["side"];

const EMOTION_OPTIONS: { tag: EmotionTag; emoji: string; label: string }[] = [
  { tag: "calm", emoji: "😌", label: "Calm/Systematic" },
  { tag: "fomo", emoji: "⚡", label: "FOMO/Rush" },
  { tag: "revenge", emoji: "🔥", label: "Revenge/Impulsive" },
  { tag: "boredom", emoji: "🥱", label: "Boredom" },
  { tag: "overconfidence", emoji: "🚀", label: "Overconfident" },
  { tag: "fear", emoji: "😨", label: "Fear" },
];

const SIDE_OPTIONS: { value: Side; label: string }[] = [
  { value: "buy", label: "Buy (매수)" },
  { value: "sell", label: "Sell (매도)" },
  { value: "observe", label: "Observe / Pass (관망/매수 보류)" },
  { value: "contemplating", label: "Contemplating (진입 고민/갈등)" },
  { value: "note", label: "Note / Review (복기/메모)" },
];

const EXECUTION_SIDES: Side[] = ["buy", "sell"];

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

/** Confirmation for a non-executed entry: no fill, no position, no cash. */
function ReflectionConfirmation({
  result,
  onDismiss,
}: {
  result: TradeResponse;
  onDismiss: () => void;
}) {
  const { trade } = result;
  return (
    <div className="trade-confirm">
      <div className="trade-confirm-head">
        <span className="trade-confirm-title">
          ✓ Logged {trade.side}{trade.ticker ? ` — ${trade.ticker}` : ""}
        </span>
        <button className="btn-close" onClick={onDismiss} title="Dismiss">
          ✕
        </button>
      </div>
      {trade.execution_price !== null && (
        <div className="trade-confirm-note">
          Benchmark price at this moment: {money(trade.execution_price)} —
          holdings and cash are unchanged.
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
  const [side, setSide] = useState<Side>("buy");
  const [ticker, setTicker] = useState(defaultTicker ?? "");
  const [executedAt, setExecutedAt] = useState(nowLocalInput());
  const [quantity, setQuantity] = useState("");
  const [rationale, setRationale] = useState("");
  const [emotionTag, setEmotionTag] = useState<EmotionTag | null>(null);
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

  const isExecution = EXECUTION_SIDES.includes(side);
  const qty = Number(quantity);
  const canSubmit = isExecution
    ? !submitting && ticker.trim() !== "" && quantity !== "" && qty > 0 && !!executedAt
    : !submitting && rationale.trim().length > 0 && !!executedAt;
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
          proposed_quantity: isExecution && qty > 0 ? qty : null,
          entry_rationale: rationale.trim(),
          emotion_tag: emotionTag,
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
        ticker: ticker.trim().toUpperCase() || null,
        side,
        quantity: isExecution ? qty : undefined,
        executed_at: toUtcIso(executedAt),
        entry_rationale: rationale.trim() || null,
        emotion_tag: emotionTag,
        // Omitted unless the user explicitly opened the override.
        execution_price:
          showOverride && overridePrice !== "" ? Number(overridePrice) : null,
      });
      setResult(res);
      onLogged(res);
      // Clear the per-entry fields; keep ticker and side so logging a
      // follow-up entry on the same position doesn't mean retyping everything.
      setQuantity("");
      setRationale("");
      setEmotionTag(null);
      setOverridePrice("");
      setExecutedAt(nowLocalInput());
      // The review described a decision that is now made; keeping it on screen
      // would read as commentary on the NEXT entry.
      setCoachReport(null);
      setCoachError(null);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : isExecution ? "Failed to log the trade." : "Failed to log the decision."
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="trade-form" onSubmit={handleSubmit}>
      <div className="trade-form-row">
        <label className="trade-field">
          <span className="trade-label">
            Ticker{!isExecution && " (optional)"}
          </span>
          <input
            className="trade-input"
            list="portfolio-tickers"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            placeholder={isExecution ? "AAPL" : "AAPL, or leave blank for a general note"}
            disabled={submitting}
          />
          <datalist id="portfolio-tickers">
            {knownTickers.map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>
        </label>

        <label className="trade-field">
          <span className="trade-label">Side</span>
          <select
            className="trade-input trade-side-select"
            value={side}
            onChange={(e) => setSide(e.target.value as Side)}
            disabled={submitting}
          >
            {SIDE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
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

        {isExecution && (
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
        )}
      </div>

      <p className="trade-form-hint">
        {isExecution
          ? "No price field — the execution price is looked up from market data at the " +
            "time you enter above."
          : "No quantity, no holdings change — this is a thought, not an execution. " +
            "A benchmark price at this moment is still recorded, so you can later see what the stock did."}
      </p>

      {/* ── Entry Rationale: the feature's centerpiece, not a footnote. ── */}
      <label className="trade-field trade-field-rationale">
        <span className="trade-label trade-label-emphasis">
          Entry Rationale / Reflection (진입 이유 / 생각)
        </span>
        <textarea
          className="trade-textarea"
          value={rationale}
          onChange={(e) => setRationale(e.target.value)}
          rows={4}
          disabled={submitting}
          placeholder={
            isExecution
              ? "Why now? What are you feeling — conviction, FOMO, fear?\n" +
                "Be honest: your coach compares this against what the data actually said."
              : "What are you weighing? A dilemma, a reason you're passing, an idea " +
                "you're not ready to act on — write it as it actually feels."
          }
        />
        <span className="trade-help">
          Written in your own words. The Coach agent reads this to spot patterns
          across your trades — whether you pulled the trigger or hesitated.
        </span>
      </label>

      {/* 1-click emotion tag — feeds the Personal Edge dashboard's emotion-
          segmented expectancy and the pre-trade coach's toxic-pattern matcher. */}
      <div className="trade-field">
        <span className="trade-label">How are you feeling about this? (optional)</span>
        <div className="emotion-selector">
          {EMOTION_OPTIONS.map((o) => (
            <button
              key={o.tag}
              type="button"
              className={`emotion-btn ${emotionTag === o.tag ? "is-active" : ""}`}
              onClick={() => setEmotionTag(emotionTag === o.tag ? null : o.tag)}
              disabled={submitting}
              title={o.label}
            >
              <span className="emotion-btn-emoji">{o.emoji}</span>
              <span className="emotion-btn-label">{o.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Manual override, tucked away so it never looks like a required field. */}
      <div className="trade-override">
        <button
          type="button"
          className="trade-override-toggle"
          onClick={() => setShowOverride((v) => !v)}
          disabled={submitting}
        >
          {showOverride ? "▾" : "▸"} Correct the price manually
        </button>
        {showOverride && (
          <label className="trade-field trade-field-narrow">
            <span className="trade-label">
              {isExecution ? "Execution price" : "Benchmark price"}
            </span>
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
          {isExecution
            ? (submitting ? "Looking up the fill…" : "Log Trade")
            : (submitting ? "Saving…" : "Log Decision / Reflection")}
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
        EXECUTION_SIDES.includes(result.trade.side) ? (
          <TradeConfirmation result={result} onDismiss={() => setResult(null)} />
        ) : (
          <ReflectionConfirmation result={result} onDismiss={() => setResult(null)} />
        )
      )}
    </form>
  );
}

export type { Trade };
