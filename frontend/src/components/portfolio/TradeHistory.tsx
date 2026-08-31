/**
 * TradeHistory.tsx
 * ────────────────
 * The trading journal: every logged trade in reverse-chronological order, with
 * the user's own **entry rationale** shown in full alongside the numbers.
 *
 * The rationale is not truncated to a tooltip. Reading back what you were
 * thinking at the time — next to what the trade actually cost — is the whole
 * point of keeping the journal, and it is the raw material the Coach agent
 * works from.
 *
 * Paging reuses `usePagination` + the shared `Pagination` component, same as the
 * media feeds.
 */

import type { Trade } from "../../types";
import { usePagination } from "../../hooks/usePagination";
import Pagination from "../media/Pagination";

/** Marker the backend writes for a position seeded at portfolio setup. */
const OPENING_RATIONALE = "Opening position recorded at portfolio setup.";

interface TradeHistoryProps {
  trades: Trade[];
  loading: boolean;
  error: string | null;
  /** Narrow the journal to one ticker; null shows every company. */
  filterTicker: string | null;
  onFilterChange: (ticker: string | null) => void;
  /** Tickers available to filter by. */
  tickers: string[];
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function money(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `$${n.toFixed(2)}`;
}

export default function TradeHistory({
  trades,
  loading,
  error,
  filterTicker,
  onFilterChange,
  tickers,
}: TradeHistoryProps) {
  // Reset to page 1 when the filter changes or a new trade lands.
  const pager = usePagination(trades, 10, `${filterTicker ?? "all"}:${trades.length}`);

  return (
    <>
      <div className="range-bar">
        <span className="range-bar-label">Company</span>
        <select
          className="trade-input trade-filter-select"
          value={filterTicker ?? ""}
          onChange={(e) => onFilterChange(e.target.value || null)}
        >
          <option value="">All companies</option>
          {tickers.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>

      {loading && <div className="journal-notice">Loading the journal…</div>}
      {error && <div className="journal-notice journal-error">{error}</div>}

      {!loading && !error && trades.length === 0 && (
        <div className="journal-notice">
          No trades logged yet. Your first entry — and the reasoning behind it —
          starts the history the coach will learn from.
        </div>
      )}

      {!loading && !error && trades.length > 0 && (
        <>
          <div className="journal-list">
            {pager.pageItems.map((t) => {
              const isOpening = t.entry_rationale === OPENING_RATIONALE;
              return (
                <article key={t.id} className="journal-entry">
                  <div className="journal-entry-head">
                    <span
                      className={`journal-side journal-side-${t.side}`}
                      title={t.side === "buy" ? "Bought" : "Sold"}
                    >
                      {t.side === "buy" ? "BUY" : "SELL"}
                    </span>
                    <span className="journal-ticker">{t.ticker}</span>
                    <span className="journal-qty">{t.quantity} sh</span>
                    <span className="journal-price">
                      @ {money(t.execution_price)}
                    </span>
                    <span className="journal-total">{money(t.total_value)}</span>
                    <span className="journal-when">{formatWhen(t.executed_at)}</span>
                  </div>

                  {t.entry_rationale ? (
                    <p
                      className={`journal-rationale ${
                        isOpening ? "journal-rationale-seed" : ""
                      }`}
                    >
                      {isOpening && (
                        <span className="journal-seed-tag">seeded</span>
                      )}
                      {t.entry_rationale}
                    </p>
                  ) : (
                    <p className="journal-rationale journal-rationale-empty">
                      No rationale recorded — the coach has nothing to evaluate
                      for this trade.
                    </p>
                  )}

                  {t.avg_price_after !== null && (
                    <div className="journal-meta">
                      Average after this trade: {money(t.avg_price_after)}
                    </div>
                  )}
                </article>
              );
            })}
          </div>

          <Pagination
            page={pager.page}
            pageCount={pager.pageCount}
            onChange={pager.setPage}
            rangeStart={pager.rangeStart}
            rangeEnd={pager.rangeEnd}
            total={pager.total}
          />
        </>
      )}
    </>
  );
}
