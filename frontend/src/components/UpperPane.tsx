/**
 * UpperPane.tsx
 * ─────────────
 * The top half of the split-screen layout — Quantitative Viewer.
 *
 * Displays financial table data in three tabs:
 *   1. Balance Sheet
 *   2. Income Statement
 *   3. Cash Flow
 *
 * Each tab fetches merged table data from GET /financials and
 * renders it in a scrollable HTML table with:
 *   - Sticky header row (stays visible while scrolling vertically)
 *   - Sticky first column (Line Item names stay visible while scrolling horizontally)
 *   - Null values displayed as "—" with muted styling
 */

import { useState, useEffect } from "react";
import type { FinancialTableResponse } from "../types";
import { getFinancials } from "../api";

// Tab definitions — key matches the backend's statement_type parameter
const TABS = [
  { key: "balance_sheet", label: "Balance Sheet" },
  { key: "income_statement", label: "Income Statement" },
  { key: "cash_flow", label: "Cash Flow" },
] as const;

interface UpperPaneProps {
  /** Incremented after upload to trigger data refresh */
  refreshKey: number;
}

export default function UpperPane({ refreshKey }: UpperPaneProps) {
  // Currently selected tab
  const [activeTab, setActiveTab] = useState("balance_sheet");
  // Financial table data from the backend
  const [data, setData] = useState<FinancialTableResponse | null>(null);
  // Loading state for the fetch call
  const [loading, setLoading] = useState(false);
  // Error message if fetch fails
  const [error, setError] = useState<string | null>(null);

  // Fetch financial data whenever the active tab or refreshKey changes
  useEffect(() => {
    let cancelled = false;  // Prevent state updates after unmount

    async function fetchData() {
      setLoading(true);
      setError(null);

      try {
        const result = await getFinancials(activeTab);
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

    fetchData();

    // Cleanup: cancel if component unmounts or deps change before fetch completes
    return () => { cancelled = true; };
  }, [activeTab, refreshKey]);

  return (
    <div className="pane upper-pane">
      {/* Pane header with title and tabs */}
      <div className="pane-header">
        <h2 className="pane-title">Quantitative Data</h2>
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
            Upload SEC filing PDFs to see financial data here.
          </div>
        )}
      </div>
    </div>
  );
}
