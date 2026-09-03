/**
 * CashLedger.tsx
 * ──────────────
 * Every movement of money, newest first, with a running per-currency balance.
 *
 * Two presentational rules that follow from how the ledger is built:
 *
 *   - **A conversion renders as one row.** Its two legs share a `conversion_id`
 *     and describe a single event; showing them separately would read as money
 *     vanishing from one currency and appearing in another.
 *   - **Synthetic setup rows are tagged**, the same way `TradeHistory` tags a
 *     seeded opening trade. An anchor must never be mistaken for something that
 *     actually happened on that date.
 */

import { useMemo, useState } from "react";
import type { CashFlow } from "../../types";
import { usePagination } from "../../hooks/usePagination";
import Pagination from "../media/Pagination";
import { formatNative } from "./Money";

const OPENING_NOTE = "Opening cash balance recorded at portfolio setup.";
const SEED_NOTE = "Synthetic funding for a position seeded at setup.";

const TYPE_LABELS: Record<string, string> = {
  deposit: "Deposit", withdrawal: "Withdrawal", buy: "Buy", sell: "Sell",
  dividend: "Dividend", fee: "Fee", tax: "Tax", interest: "Interest",
  fx_out: "Convert", fx_in: "Convert", adjustment: "Adjustment",
};

interface LedgerRow {
  key: string;
  occurred_at: string;
  type: string;
  label: string;
  /** A conversion carries both sides; everything else carries one. */
  out?: CashFlow;
  in?: CashFlow;
  flow?: CashFlow;
  note: string | null;
  tag: "opening" | "seed" | null;
}

function tagOf(f: CashFlow): "opening" | "seed" | null {
  if (f.note === OPENING_NOTE) return "opening";
  if (f.note === SEED_NOTE) return "seed";
  return null;
}

/** Collapse the two legs of each conversion into a single row. */
function buildRows(flows: CashFlow[]): LedgerRow[] {
  const byConversion = new Map<string, CashFlow[]>();
  const rows: LedgerRow[] = [];

  for (const f of flows) {
    if (f.conversion_id) {
      const list = byConversion.get(f.conversion_id) ?? [];
      list.push(f);
      byConversion.set(f.conversion_id, list);
      continue;
    }
    rows.push({
      key: `f${f.id}`,
      occurred_at: f.occurred_at,
      type: f.flow_type,
      label: TYPE_LABELS[f.flow_type] ?? f.flow_type,
      flow: f,
      note: f.note,
      tag: tagOf(f),
    });
  }

  for (const [id, legs] of byConversion) {
    const out = legs.find((l) => l.flow_type === "fx_out");
    const inn = legs.find((l) => l.flow_type === "fx_in");
    rows.push({
      key: `c${id}`,
      occurred_at: (out ?? inn)!.occurred_at,
      type: "conversion",
      label: "Convert",
      out, in: inn,
      note: (out ?? inn)!.note,
      tag: null,
    });
  }

  return rows.sort((a, b) => b.occurred_at.localeCompare(a.occurred_at));
}

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
}

export default function CashLedger({
  flows,
  loading,
  error,
}: {
  flows: CashFlow[];
  loading: boolean;
  error: string | null;
}) {
  const [currency, setCurrency] = useState<string>("");
  const [type, setType] = useState<string>("");

  const rows = useMemo(() => {
    let list = buildRows(flows);
    if (currency) {
      list = list.filter((r) =>
        r.flow
          ? r.flow.currency === currency
          : r.out?.currency === currency || r.in?.currency === currency
      );
    }
    if (type) list = list.filter((r) => r.type === type);
    return list;
  }, [flows, currency, type]);

  const pager = usePagination(rows, 12, `${currency}:${type}:${rows.length}`);

  return (
    <>
      <div className="range-bar">
        <span className="range-bar-label">Currency</span>
        <select className="trade-input trade-filter-select" value={currency}
                onChange={(e) => setCurrency(e.target.value)}>
          <option value="">All</option>
          <option value="KRW">KRW</option>
          <option value="USD">USD</option>
        </select>
        <span className="range-bar-label">Type</span>
        <select className="trade-input trade-filter-select" value={type}
                onChange={(e) => setType(e.target.value)}>
          <option value="">All</option>
          {["deposit", "withdrawal", "buy", "sell", "conversion",
            "dividend", "fee", "tax", "interest", "adjustment"].map((t) => (
            <option key={t} value={t}>{TYPE_LABELS[t] ?? "Convert"}</option>
          ))}
        </select>
      </div>

      {loading && <div className="journal-notice">Loading the ledger…</div>}
      {error && <div className="journal-notice journal-error">{error}</div>}

      {!loading && !error && rows.length === 0 && (
        <div className="journal-notice">
          No cash movements recorded for this filter.
        </div>
      )}

      {!loading && !error && rows.length > 0 && (
        <>
          <div className="table-wrapper">
            <table className="fin-table ledger-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Type</th>
                  <th>Amount</th>
                  <th>Rate</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {pager.pageItems.map((r) => (
                  <tr key={r.key}>
                    <td className="ledger-when">{when(r.occurred_at)}</td>
                    <td>
                      <span className={`ledger-type ledger-type-${r.type}`}>
                        {r.label}
                      </span>
                      {r.tag && (
                        <span className="journal-seed-tag" title={r.note ?? ""}>
                          {r.tag === "opening" ? "anchor" : "seed"}
                        </span>
                      )}
                    </td>
                    <td className="ledger-amount">
                      {r.flow ? (
                        <span className={r.flow.amount >= 0 ? "tone-positive" : "tone-negative"}>
                          {formatNative(r.flow.amount, r.flow.currency)}
                        </span>
                      ) : (
                        <span className="ledger-conversion">
                          <span className="tone-negative">
                            {formatNative(r.out?.amount, r.out?.currency)}
                          </span>
                          <span className="ledger-arrow">→</span>
                          <span className="tone-positive">
                            {formatNative(r.in?.amount, r.in?.currency)}
                          </span>
                        </span>
                      )}
                    </td>
                    <td className="ledger-rate">
                      {(r.flow ?? r.in)?.fx_to_krw
                        ? (r.flow ?? r.in)!.fx_to_krw.toLocaleString("ko-KR", {
                            maximumFractionDigits: 2,
                          })
                        : "—"}
                    </td>
                    <td className="ledger-detail">
                      {r.flow?.trade_id && (
                        <span className="ledger-linked">trade #{r.flow.trade_id}</span>
                      )}
                      {r.in?.realized_fx_pnl_krw != null && (
                        <span
                          className={
                            r.in.realized_fx_pnl_krw >= 0 ? "tone-positive" : "tone-negative"
                          }
                        >
                          currency P/L{" "}
                          {r.in.realized_fx_pnl_krw.toLocaleString("ko-KR", {
                            style: "currency", currency: "KRW",
                            maximumFractionDigits: 0,
                          })}
                        </span>
                      )}
                      {r.note && !r.tag && (
                        <span className="ledger-note">{r.note}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
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
