/**
 * AttributionPanel.tsx
 * ────────────────────
 * Return split into the two decisions that produced it: **what you bought** and
 * **when you converted**.
 *
 * Those are separate decisions and deserve separate scorecards. A position can
 * be up 8% in dollars and down in won; presenting only the blended figure hides
 * which of the two the user actually got right.
 *
 * The composition is **multiplicative, with a cross term**: 8.2% and 3.1% make
 * 11.55%, not 11.3%. This component therefore never adds the two bars together —
 * the total comes from `roi_krw`, the field the backend asserts the identity
 * `(1 + roi_krw) == (1 + roi_local)(1 + roi_fx)` against.
 */

import type { Holding } from "../../types";

function pct(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined) return "—";
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
}

function tone(n: number | null | undefined): "positive" | "negative" | "neutral" {
  if (n === null || n === undefined || Math.abs(n) < 1e-9) return "neutral";
  return n > 0 ? "positive" : "negative";
}

/** Bar width relative to the largest absolute component on the row. */
function width(n: number | null | undefined, scale: number): string {
  if (n === null || n === undefined || scale <= 0) return "0%";
  return `${Math.min(100, (Math.abs(n) / scale) * 100)}%`;
}

function Row({ h }: { h: Holding }) {
  const parts = [h.roi_local, h.roi_fx, h.roi_krw].filter(
    (v): v is number => v !== null && v !== undefined
  );
  const scale = parts.length ? Math.max(...parts.map(Math.abs)) : 0;

  const isBase = (h.currency || "").toUpperCase() === "KRW";

  return (
    <div className="attr-row">
      <div className="attr-head">
        <span className="attr-ticker">{h.ticker}</span>
        <span className="attr-ccy">{h.currency}</span>
      </div>

      <div className="attr-bars">
        {[
          { label: "Stock", value: h.roi_local },
          { label: "Currency", value: h.roi_fx },
          { label: "Total (₩)", value: h.roi_krw, total: true },
        ].map((bar) => (
          <div key={bar.label} className={`attr-bar-row ${bar.total ? "attr-total" : ""}`}>
            <span className="attr-bar-label">{bar.label}</span>
            <div className="attr-bar-track">
              <div
                className={`attr-bar-fill tone-bg-${tone(bar.value)}`}
                style={{ width: width(bar.value, scale) }}
              />
            </div>
            <span className={`attr-bar-value tone-${tone(bar.value)}`}>
              {pct(bar.value)}
            </span>
          </div>
        ))}
      </div>

      <p className="attr-sentence">
        {isBase ? (
          <>
            {h.ticker} trades in won, so none of its return came from the
            exchange rate. In dollars the same position is{" "}
            <strong className={`tone-${tone(h.roi_usd)}`}>{pct(h.roi_usd)}</strong> —
            a different number, because the won moved against the dollar.
          </>
        ) : (
          <>
            The stock is <strong className={`tone-${tone(h.roi_local)}`}>
              {pct(h.roi_local)}
            </strong>{" "}
            in its own currency; the exchange rate added{" "}
            <strong className={`tone-${tone(h.roi_fx)}`}>{pct(h.roi_fx)}</strong>.
            In won: <strong className={`tone-${tone(h.roi_krw)}`}>
              {pct(h.roi_krw)}
            </strong>.
          </>
        )}
      </p>
    </div>
  );
}

export default function AttributionPanel({
  holdings,
  totals,
}: {
  holdings: Holding[];
  totals: { roi_krw_total: number | null; roi_usd_total: number | null };
}) {
  const rows = holdings.filter((h) => h.roi_krw !== null || h.roi_local !== null);
  if (rows.length === 0) return null;

  return (
    <section className="attr-panel">
      <div className="attr-panel-head">
        <h3 className="attr-panel-title">Where the return came from</h3>
        <div className="attr-panel-totals">
          <span>
            Portfolio in won:{" "}
            <strong className={`tone-${tone(totals.roi_krw_total)}`}>
              {pct(totals.roi_krw_total)}
            </strong>
          </span>
          <span>
            in dollars:{" "}
            <strong className={`tone-${tone(totals.roi_usd_total)}`}>
              {pct(totals.roi_usd_total)}
            </strong>
          </span>
        </div>
      </div>

      {rows.map((h) => <Row key={h.ticker} h={h} />)}

      <p className="attr-footnote">
        The stock and currency figures compose multiplicatively, not additively —
        a 10% gain on a 10% currency move is +21%. The total shown is the
        backend's computed figure, never a sum of the two bars.
      </p>
    </section>
  );
}
