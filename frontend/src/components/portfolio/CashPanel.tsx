/**
 * CashPanel.tsx
 * ─────────────
 * Cash as a position: one card per currency, plus the forms that move it.
 *
 * When no opening balance has been recorded this is instead a short setup form.
 * That anchor is worth explaining rather than just collecting: it describes the
 * **whole state at its own instant**, so a trade dated before it moves no cash —
 * its effect is already inside the balance being entered. Back-date the anchor
 * to the start of any history the user intends to enter.
 *
 * The conversion form asks for **both amounts** rather than a rate. That is what
 * a bank statement shows, and it captures the spread actually paid — a rate
 * fetched from the market would quietly erase a real cost.
 */

import { useState } from "react";
import type { CashPosition, ConversionResult } from "../../types";
import {
  createCashFlow,
  createConversion,
  initializeLedger,
} from "../../api";
import Money, { formatKrw, formatNative } from "./Money";

const CURRENCIES = ["KRW", "USD"] as const;

/** Flow types the user records by hand. Trades and conversions write their own. */
const MANUAL_FLOWS = [
  { value: "deposit", label: "Deposit" },
  { value: "withdrawal", label: "Withdrawal" },
  { value: "dividend", label: "Dividend" },
  { value: "interest", label: "Interest" },
  { value: "fee", label: "Fee" },
  { value: "tax", label: "Tax" },
  { value: "adjustment", label: "Adjustment (reconcile)" },
];

/** Local wall-clock value for a `datetime-local` input. */
function nowLocalInput(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/** `datetime-local` is naive local time; the backend stores UTC. */
function toUtcIso(local: string): string {
  return new Date(local).toISOString();
}

// =============================================================================
// Setup
// =============================================================================

function LedgerSetup({ onDone }: { onDone: () => void }) {
  const [krw, setKrw] = useState("");
  const [usd, setUsd] = useState("");
  const [when, setWhen] = useState(nowLocalInput());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !busy && (Number(krw) > 0 || Number(usd) > 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const opening: Record<string, number> = {};
      if (Number(krw) > 0) opening.KRW = Number(krw);
      if (Number(usd) > 0) opening.USD = Number(usd);
      await initializeLedger({ opening, occurred_at: toUtcIso(when) });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open the ledger.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="cash-setup" onSubmit={submit}>
      <h3 className="cash-setup-title">How much cash do you hold?</h3>
      <p className="cash-setup-note">
        This is an <strong>anchor</strong>, not reconstructed history — the same
        way a seeded position records what you hold without claiming to know when
        you bought it. It describes your whole position at the moment you give,
        so a trade dated before that moment moves no cash: its effect is already
        inside this balance.
      </p>
      <div className="cash-setup-fields">
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Won (₩)</span>
          <input
            className="trade-input" type="number" min="0" step="any"
            value={krw} onChange={(e) => setKrw(e.target.value)}
            placeholder="16000000" disabled={busy}
          />
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Dollars ($)</span>
          <input
            className="trade-input" type="number" min="0" step="any"
            value={usd} onChange={(e) => setUsd(e.target.value)}
            placeholder="0" disabled={busy}
          />
        </label>
        <label className="trade-field">
          <span className="trade-label">As of</span>
          <input
            className="trade-input" type="datetime-local"
            value={when} onChange={(e) => setWhen(e.target.value)} disabled={busy}
          />
        </label>
        <button className="btn-primary" type="submit" disabled={!canSubmit}>
          {busy ? "Opening…" : "Open the ledger"}
        </button>
      </div>
      {error && <div className="trade-error">{error}</div>}
    </form>
  );
}

// =============================================================================
// Forms
// =============================================================================

function CashFlowForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [flowType, setFlowType] = useState("deposit");
  const [currency, setCurrency] = useState("KRW");
  const [amount, setAmount] = useState("");
  const [when, setWhen] = useState(nowLocalInput());
  const [rate, setRate] = useState("");
  const [note, setNote] = useState("");
  const [showRate, setShowRate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !busy && Number(amount) !== 0 && amount.trim() !== "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      await createCashFlow({
        flow_type: flowType,
        currency,
        amount: Number(amount),
        occurred_at: toUtcIso(when),
        fx_to_krw: showRate && Number(rate) > 0 ? Number(rate) : null,
        note: note.trim() || null,
      });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the flow.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="cash-form" onSubmit={submit}>
      <div className="cash-form-row">
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Type</span>
          <select className="trade-input" value={flowType} disabled={busy}
                  onChange={(e) => setFlowType(e.target.value)}>
            {MANUAL_FLOWS.map((f) => (
              <option key={f.value} value={f.value}>{f.label}</option>
            ))}
          </select>
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Currency</span>
          <select className="trade-input" value={currency} disabled={busy}
                  onChange={(e) => setCurrency(e.target.value)}>
            {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Amount</span>
          <input className="trade-input" type="number" step="any" value={amount}
                 onChange={(e) => setAmount(e.target.value)} disabled={busy}
                 placeholder={flowType === "adjustment" ? "±0" : "0"} />
        </label>
        <label className="trade-field">
          <span className="trade-label">When</span>
          <input className="trade-input" type="datetime-local" value={when}
                 onChange={(e) => setWhen(e.target.value)} disabled={busy} />
        </label>
      </div>

      <label className="trade-field">
        <span className="trade-label">Note (optional)</span>
        <input className="trade-input" value={note} disabled={busy}
               onChange={(e) => setNote(e.target.value)}
               placeholder="Broker statement reference, reason for an adjustment…" />
      </label>

      {currency !== "KRW" && (
        <details className="cash-form-override" open={showRate}>
          <summary onClick={() => setShowRate((v) => !v)}>
            Exchange rate — resolved automatically
          </summary>
          <label className="trade-field trade-field-narrow">
            <span className="trade-label">KRW per USD</span>
            <input className="trade-input" type="number" step="any" value={rate}
                   onChange={(e) => setRate(e.target.value)} disabled={busy}
                   placeholder="from that day's close" />
          </label>
          <p className="cash-form-hint">
            Override with your statement's rate when you have it — it includes the
            spread you actually paid, which a market close does not.
          </p>
        </details>
      )}

      <div className="cash-form-actions">
        <button className="btn-primary" type="submit" disabled={!canSubmit}>
          {busy ? "Recording…" : "Record"}
        </button>
        <button className="btn-secondary-sm" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <div className="trade-error">{error}</div>}
    </form>
  );
}

function ConvertForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [from, setFrom] = useState("KRW");
  const [to, setTo] = useState("USD");
  const [fromAmount, setFromAmount] = useState("");
  const [toAmount, setToAmount] = useState("");
  const [when, setWhen] = useState(nowLocalInput());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConversionResult | null>(null);

  const canSubmit =
    !busy && from !== to && Number(fromAmount) > 0 && Number(toAmount) > 0;

  const impliedRate =
    Number(fromAmount) > 0 && Number(toAmount) > 0
      ? from === "USD"
        ? Number(toAmount) / Number(fromAmount)
        : Number(fromAmount) / Number(toAmount)
      : null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      setResult(
        await createConversion({
          from_currency: from,
          from_amount: Number(fromAmount),
          to_currency: to,
          to_amount: Number(toAmount),
          occurred_at: toUtcIso(when),
        })
      );
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the conversion.");
    } finally {
      setBusy(false);
    }
  }

  function swap() {
    setFrom(to);
    setTo(from);
    setFromAmount(toAmount);
    setToAmount(fromAmount);
  }

  return (
    <form className="cash-form" onSubmit={submit}>
      <p className="cash-form-hint">
        Enter <strong>both amounts</strong> exactly as your statement shows them.
        The rate is derived from them, so the spread you paid is captured rather
        than replaced by a market close.
      </p>
      <div className="cash-form-row">
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">From</span>
          <select className="trade-input" value={from} disabled={busy}
                  onChange={(e) => setFrom(e.target.value)}>
            {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Amount out</span>
          <input className="trade-input" type="number" min="0" step="any"
                 value={fromAmount} disabled={busy}
                 onChange={(e) => setFromAmount(e.target.value)} />
        </label>
        <button className="btn-secondary-sm cash-swap" type="button" onClick={swap}
                title="Swap direction">⇄</button>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">To</span>
          <select className="trade-input" value={to} disabled={busy}
                  onChange={(e) => setTo(e.target.value)}>
            {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="trade-field trade-field-narrow">
          <span className="trade-label">Amount in</span>
          <input className="trade-input" type="number" min="0" step="any"
                 value={toAmount} disabled={busy}
                 onChange={(e) => setToAmount(e.target.value)} />
        </label>
        <label className="trade-field">
          <span className="trade-label">When</span>
          <input className="trade-input" type="datetime-local" value={when}
                 onChange={(e) => setWhen(e.target.value)} disabled={busy} />
        </label>
      </div>

      {impliedRate !== null && (
        <div className="cash-form-implied">
          Effective rate: <strong>{impliedRate.toLocaleString("ko-KR", {
            maximumFractionDigits: 2,
          })}</strong> KRW per USD
        </div>
      )}

      {result && (
        <div className="cash-conv-result">
          <div>
            Recorded at <strong>{result.rate.toFixed(2)}</strong>
            {result.market_rate && (
              <> against a market rate of {result.market_rate.toFixed(2)}</>
            )}
          </div>
          {result.spread_krw !== null && (
            <div className={result.spread_krw > 0 ? "tone-negative" : "tone-positive"}>
              Spread paid: {formatKrw(result.spread_krw)}
            </div>
          )}
          {result.realized_fx_pnl_krw !== null && (
            <div className={result.realized_fx_pnl_krw >= 0 ? "tone-positive" : "tone-negative"}>
              Realized currency P/L: {formatKrw(result.realized_fx_pnl_krw)}
              <span className="cash-conv-caveat">
                {" "}— average cost, for decision-making rather than a tax filing
              </span>
            </div>
          )}
        </div>
      )}

      <div className="cash-form-actions">
        <button className="btn-primary" type="submit" disabled={!canSubmit}>
          {busy ? "Recording…" : "Record conversion"}
        </button>
        <button className="btn-secondary-sm" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <div className="trade-error">{error}</div>}
    </form>
  );
}

// =============================================================================
// Panel
// =============================================================================

export default function CashPanel({
  cash,
  cashTotals,
  onChanged,
}: {
  cash: CashPosition | null;
  /** Per-currency cash restated in both, from the portfolio response. */
  cashTotals: { krw: number | null; usd: number | null };
  onChanged: () => void;
}) {
  const [open, setOpen] = useState<"flow" | "convert" | null>(null);

  if (!cash) return null;

  if (!cash.is_initialized) {
    return (
      <section className="cash-panel">
        <LedgerSetup onDone={onChanged} />
      </section>
    );
  }

  const rate = cash.fx?.rate ?? null;
  const negative = Object.entries(cash.balances).filter(([, v]) => v < -1e-9);

  return (
    <section className="cash-panel">
      <div className="cash-cards">
        {Object.entries(cash.balances).map(([currency, amount]) => {
          // Both figures come from the API's rate; the component never converts.
          const krw = currency === "KRW" ? amount : rate ? amount * rate : null;
          const usd = currency === "USD" ? amount : rate ? amount / rate : null;
          return (
            <div key={currency} className="cash-card">
              <div className="cash-card-head">
                <span className="cash-card-ccy">{currency}</span>
                <span className="cash-card-native">
                  {formatNative(amount, currency)}
                </span>
              </div>
              <Money krw={krw} usd={usd} compact className="cash-card-value" />
            </div>
          );
        })}

        <div className="cash-card cash-card-total">
          <div className="cash-card-head">
            <span className="cash-card-ccy">Total cash</span>
          </div>
          <Money krw={cashTotals.krw} usd={cashTotals.usd} />
        </div>
      </div>

      {negative.length > 0 && (
        <div className="cash-negative">
          {negative.map(([c, v]) => (
            <div key={c}>
              Your {c} balance is {formatNative(v, c)}. Something is missing — a
              deposit, or a conversion that was never recorded. Record it, or use
              an <strong>Adjustment</strong> to reconcile against your statement.
            </div>
          ))}
        </div>
      )}

      <div className="cash-actions">
        <button className="btn-secondary-sm"
                onClick={() => setOpen(open === "flow" ? null : "flow")}>
          {open === "flow" ? "Cancel" : "+ Deposit / withdraw"}
        </button>
        <button className="btn-secondary-sm"
                onClick={() => setOpen(open === "convert" ? null : "convert")}>
          {open === "convert" ? "Cancel" : "⇄ Convert (환전)"}
        </button>
      </div>

      {open === "flow" && (
        <CashFlowForm onDone={() => { setOpen(null); onChanged(); }}
                      onCancel={() => setOpen(null)} />
      )}
      {open === "convert" && (
        <ConvertForm onDone={() => { onChanged(); }}
                     onCancel={() => setOpen(null)} />
      )}
    </section>
  );
}
