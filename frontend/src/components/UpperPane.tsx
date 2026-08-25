/**
 * UpperPane.tsx
 * ─────────────
 * The top half of the split-screen layout — Quantitative Viewer.
 *
 * Displays financial table data in four tabs:
 *   1. Balance Sheet
 *   2. Income Statement
 *   3. Cash Flow
 *   4. Financial Ratios (historical — computed from the statements' XBRL data)
 *
 * Each tab fetches merged table data from GET /financials and
 * renders it in a scrollable HTML table with:
 *   - Sticky header row (stays visible while scrolling vertically)
 *   - Sticky first column (Line Item names stay visible while scrolling horizontally)
 *   - Null values displayed as "—" with muted styling
 *
 * The currently displayed table can be exported to CSV (opens directly
 * in Excel) via the Download button in the pane header.
 */

import { useState, useEffect } from "react";
import type { FinancialTableResponse } from "../types";
import { getFinancials } from "../api";

// Tab definitions — key matches the backend's statement_type parameter
const TABS = [
  { key: "balance_sheet", label: "Balance Sheet" },
  { key: "income_statement", label: "Income Statement" },
  { key: "cash_flow", label: "Cash Flow" },
  { key: "ratios", label: "Financial Ratios" },
] as const;

/**
 * Serialize a financial table to CSV text.
 *
 * Every field is wrapped in double quotes (and internal quotes doubled)
 * because the values contain commas — e.g. "20,586.3" — which would
 * otherwise break column alignment. A UTF-8 BOM is prepended so Excel
 * renders the "—"/currency characters correctly.
 */
function toCsv(data: FinancialTableResponse): string {
  const esc = (v: string) => `"${v.replace(/"/g, '""')}"`;
  const headerLine = data.columns.map(esc).join(",");
  const bodyLines = data.rows.map((row) =>
    data.columns.map((col) => esc(row[col] ?? "")).join(",")
  );
  return "﻿" + [headerLine, ...bodyLines].join("\r\n");
}

/** Trigger a client-side download of the current table as a .csv file. */
function downloadCsv(data: FinancialTableResponse): void {
  const blob = new Blob([toCsv(data)], {
    type: "text/csv;charset=utf-8;",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${data.statement_type}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

interface UpperPaneProps {
  /** Incremented after upload to trigger data refresh */
  refreshKey: number;
  /** Company whose financials to show; null when none is selected */
  ticker: string | null;
}

export default function UpperPane({ refreshKey, ticker }: UpperPaneProps) {
  // Currently selected tab
  const [activeTab, setActiveTab] = useState("balance_sheet");
  // Financial table data from the backend
  const [data, setData] = useState<FinancialTableResponse | null>(null);
  // Loading state for the fetch call
  const [loading, setLoading] = useState(false);
  // Error message if fetch fails
  const [error, setError] = useState<string | null>(null);

  // Fetch financial data whenever the company, active tab, or refreshKey changes
  useEffect(() => {
    let cancelled = false;  // Prevent state updates after unmount

    // No company selected — clear rather than showing the previous one's data.
    if (!ticker) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }

    async function fetchData(tk: string) {
      setLoading(true);
      setError(null);

      try {
        const result = await getFinancials(tk, activeTab);
        if (!cancelled) {
          setData(result);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load data");
          setData(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchData(ticker);

    // Cleanup: cancel if component unmounts or deps change before fetch completes
    return () => { cancelled = true; };
  }, [ticker, activeTab, refreshKey]);

  return (
    <div className="pane upper-pane">
      {/* Pane header with title, download button, and tabs */}
      <div className="pane-header">
        <div className="pane-header-top">
          <h2 className="pane-title">Quantitative Data</h2>
          <button
            className="download-btn"
            onClick={() => data && downloadCsv(data)}
            disabled={!data || data.rows.length === 0}
            title="Download the current table as a CSV file (opens in Excel)"
          >
            ↓ Download CSV
          </button>
        </div>
        <div className="tab-bar">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              className={`tab ${activeTab === tab.key ? "tab-active" : ""}`}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Table content area */}
      <div className="pane-body">
        {loading && (
          <div className="pane-status">Loading...</div>
        )}

        {error && (
          <div className="pane-status pane-error">{error}</div>
        )}

        {!loading && !error && data && data.rows.length > 0 && (
          <div className="table-wrapper">
            <table className="fin-table">
              {/* Table header — sticky */}
              <thead>
                <tr>
                  {data.columns.map((col) => (
                    <th key={col}>{col}</th>
                  ))}
                </tr>
              </thead>

              {/* Table body */}
              <tbody>
                {data.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {data.columns.map((col) => (
                      <td
                        key={col}
                        className={row[col] == null ? "cell-null" : ""}
                      >
                        {row[col] ?? "—"}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Empty state — no data and no error */}
        {!loading && !error && (!data || data.rows.length === 0) && (
          <div className="pane-status pane-empty">
            {ticker
              ? "Upload SEC filing PDFs to see financial data here."
              : "Select a company to view its financial data."}
          </div>
        )}
      </div>
    </div>
  );
}
