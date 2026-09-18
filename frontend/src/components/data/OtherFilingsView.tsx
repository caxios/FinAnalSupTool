/**
 * OtherFilingsView.tsx
 * ──────────────────────
 * The "Other Filings" data-viewer panel: Form 4 insider trades (sortable
 * table) + 8-K filing metadata (list with links to SEC). Read-only — data is
 * populated by the Fetch Control Panel's "Other SEC" checkbox
 * (POST /data/fetch → GET /data/insider/{ticker}).
 */

import { useMemo, useState } from "react";
import type { Filing8K, InsiderTrade } from "../../types";
import { useAsync } from "../../hooks/useAsync";
import { getInsiderData } from "../../api";
import MediaNotice from "../media/MediaNotice";

interface OtherFilingsViewProps {
  ticker: string | null;
  refreshKey: number;
}

type SortField = "transaction_date" | "owner_name" | "amount" | "price_per_share" | "transaction_value";

// SEC Form 4 transaction codes. Only P and S are open-market trades — the
// rest are grants, vesting, exercises and tax withholding, which read as
// misleading noise when labelled "Buy"/"Sell" (most of a big-cap's rows are
// code A grants and code F withholdings, not anyone buying or selling).
const TRADE_CODE_LABELS: Record<string, string> = {
  P: "Buy",
  S: "Sell",
  A: "Grant",
  F: "Tax withheld",
  M: "Option exercise",
  X: "Option exercise",
  C: "Conversion",
  G: "Gift",
  D: "To issuer",
  J: "Other",
};

function tradeLabel(t: InsiderTrade): string {
  const byCode = t.transaction_code ? TRADE_CODE_LABELS[t.transaction_code] : undefined;
  if (byCode) return byCode;
  if (t.acquired_or_disposed === "A") return "Acquired";
  if (t.acquired_or_disposed === "D") return "Disposed";
  return t.transaction_code_description ?? t.transaction_code ?? "—";
}

function fmtNum(v: string | number | null): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n.toLocaleString() : String(v);
}

function InsiderTable({ trades }: { trades: InsiderTrade[] }) {
  const [sortField, setSortField] = useState<SortField>("transaction_date");
  const [sortDesc, setSortDesc] = useState(true);

  const sorted = useMemo(() => {
    const copy = [...trades];
    copy.sort((a, b) => {
      const av = a[sortField] ?? "";
      const bv = b[sortField] ?? "";
      const cmp = typeof av === "number" || typeof bv === "number"
        ? Number(av) - Number(bv)
        : String(av).localeCompare(String(bv));
      return sortDesc ? -cmp : cmp;
    });
    return copy;
  }, [trades, sortField, sortDesc]);

  const sortBy = (field: SortField) => {
    if (field === sortField) setSortDesc((d) => !d);
    else {
      setSortField(field);
      setSortDesc(true);
    }
  };

  const header = (field: SortField, label: string) => (
    <th className="sortable-th" onClick={() => sortBy(field)}>
      {label} {sortField === field ? (sortDesc ? "▼" : "▲") : ""}
    </th>
  );

  if (trades.length === 0) {
    return <MediaNotice icon="📭" message="No Form 4 insider trades cached for this ticker." />;
  }

  return (
    <div className="table-scroll">
      <table className="insider-table">
        <thead>
          <tr>
            {header("transaction_date", "Date")}
            {header("owner_name", "Insider")}
            <th>Title</th>
            <th>Type</th>
            {header("amount", "Shares")}
            {header("price_per_share", "Price")}
            {header("transaction_value", "Value")}
          </tr>
        </thead>
        <tbody>
          {sorted.map((t, i) => (
            <tr key={i}>
              <td>{t.transaction_date ?? "—"}</td>
              <td>
                {t.owner_name ?? "—"}
                {t.is_director && <span className="insider-badge" title="Director"> D</span>}
                {t.is_officer && <span className="insider-badge" title="Officer"> O</span>}
                {t.is_ten_pct_owner && <span className="insider-badge" title="10% owner"> 10%</span>}
              </td>
              <td>{t.officer_title || "—"}</td>
              <td className={`trade-type trade-type-${t.acquired_or_disposed === "A" ? "buy" : "sell"}`}>
                {tradeLabel(t)}
              </td>
              <td>{fmtNum(t.amount)}</td>
              <td>{t.price_per_share && Number(t.price_per_share) > 0 ? `$${fmtNum(t.price_per_share)}` : "—"}</td>
              <td>{t.transaction_value ? `$${fmtNum(t.transaction_value)}` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Filings8KList({ filings }: { filings: Filing8K[] }) {
  if (filings.length === 0) {
    return <MediaNotice icon="📭" message="No 8-K filings cached for this ticker." />;
  }
  return (
    <ul className="filing-8k-list">
      {filings.map((f, i) => (
        <li key={i} className="filing-8k-item">
          <span className="filing-8k-date">{f.filing_date ?? "—"}</span>
          <a href={f.document_url ?? undefined} target="_blank" rel="noreferrer" className="filing-8k-link">
            {f.title ?? "8-K"} ↗
          </a>
        </li>
      ))}
    </ul>
  );
}

export default function OtherFilingsView({ ticker, refreshKey }: OtherFilingsViewProps) {
  const insider = useAsync(
    () => (ticker ? getInsiderData(ticker) : Promise.resolve(null)),
    [ticker, refreshKey]
  );

  if (!ticker) {
    return <MediaNotice icon="📄" title="No company selected" message="Pick a company to view its Form 4 and 8-K filings." />;
  }
  if (insider.loading) {
    return <MediaNotice variant="loading" message="Loading insider filings…" />;
  }
  if (insider.error) {
    return <MediaNotice variant="error" message={insider.error} />;
  }

  return (
    <div className="other-filings-view">
      <section className="view-section">
        <h2 className="section-title">👤 Insider Trades (Form 4)</h2>
        <InsiderTable trades={insider.data?.trades ?? []} />
      </section>
      <section className="view-section">
        <h2 className="section-title">📋 8-K Filings</h2>
        <Filings8KList filings={insider.data?.filings_8k ?? []} />
      </section>
    </div>
  );
}
