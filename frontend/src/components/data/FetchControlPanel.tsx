/**
 * FetchControlPanel.tsx
 * ──────────────────────
 * The Data tab's checkbox-based selective fetch control: pick any subset of
 * the 5 data types (SEC 10-K/10-Q, Other SEC, Company News, Earnings Call,
 * Price & Technicals) and fetch them for the active ticker + date range in
 * one call. Shows a cached/not-fetched indicator per type (from
 * GET /data/status/{ticker}) and per-type results after a fetch.
 */

import { useCallback, useEffect, useState } from "react";
import type { DataFetchResult, DataStatusResponse, DataType } from "../../types";
import { fetchData, getDataStatus } from "../../api";

interface DataTypeDef {
  id: DataType;
  label: string;
  icon: string;
}

const DATA_TYPES: DataTypeDef[] = [
  { id: "sec_10k_10q", label: "SEC 10-K / 10-Q", icon: "📄" },
  { id: "sec_other", label: "Other SEC (Form 4 / 8-K)", icon: "📋" },
  { id: "news", label: "Company News", icon: "📰" },
  { id: "earnings", label: "Earnings Call Transcript", icon: "📞" },
  { id: "price", label: "Price & Technicals", icon: "📈" },
];

interface FetchControlPanelProps {
  ticker: string;
  startDate: string;
  endDate: string;
  /** Fired after a fetch completes — parent should refresh its data viewers. */
  onFetchComplete: () => void;
}

export default function FetchControlPanel({
  ticker, startDate, endDate, onFetchComplete,
}: FetchControlPanelProps) {
  const [checked, setChecked] = useState<Record<DataType, boolean>>(
    Object.fromEntries(DATA_TYPES.map((d) => [d.id, true])) as Record<DataType, boolean>
  );
  const [forceRefresh, setForceRefresh] = useState(false);
  const [status, setStatus] = useState<DataStatusResponse | null>(null);
  const [results, setResults] = useState<Partial<Record<DataType, DataFetchResult>> | null>(null);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await getDataStatus(ticker));
    } catch {
      setStatus(null); // status is a convenience — the panel still works without it
    }
  }, [ticker]);

  useEffect(() => {
    setResults(null);
    setError(null);
    loadStatus();
  }, [loadStatus]);

  const toggle = (id: DataType) =>
    setChecked((prev) => ({ ...prev, [id]: !prev[id] }));

  const allChecked = DATA_TYPES.every((d) => checked[d.id]);
  const selectAll = () =>
    setChecked(
      Object.fromEntries(DATA_TYPES.map((d) => [d.id, !allChecked])) as Record<DataType, boolean>
    );

  const selected = DATA_TYPES.filter((d) => checked[d.id]).map((d) => d.id);

  const handleFetch = async () => {
    if (selected.length === 0) return;
    setFetching(true);
    setError(null);
    setResults(null);
    try {
      const res = await fetchData({
        ticker, start_date: startDate, end_date: endDate,
        include: selected, force_refresh: forceRefresh,
      });
      setResults(res.results);
      await loadStatus();
      onFetchComplete();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fetch failed.");
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="fetch-control-panel">
      <div className="fetch-control-list">
        {DATA_TYPES.map((d) => {
          const s = status?.status[d.id];
          const r = results?.[d.id];
          return (
            <label key={d.id} className="fetch-control-row">
              <input
                type="checkbox"
                checked={checked[d.id]}
                onChange={() => toggle(d.id)}
                disabled={fetching}
              />
              <span className="fetch-control-icon">{d.icon}</span>
              <span className="fetch-control-label">{d.label}</span>
              {r ? (
                <span className={`fetch-control-result fetch-control-result-${r.status}`}>
                  {r.status === "error" ? `⚠️ ${r.message ?? "failed"}` : `✓ ${r.count} item(s)`}
                </span>
              ) : s ? (
                <span className={`fetch-control-status ${s.cached ? "is-cached" : "is-empty"}`}>
                  {s.cached ? `● cached${s.detail ? ` (${s.detail})` : ""}` : "○ not fetched"}
                </span>
              ) : null}
            </label>
          );
        })}
      </div>

      <div className="fetch-control-actions">
        <button className="btn-secondary" onClick={selectAll} disabled={fetching}>
          {allChecked ? "Deselect All" : "Select All"}
        </button>
        <button
          className="btn-primary"
          onClick={handleFetch}
          disabled={fetching || selected.length === 0}
        >
          {fetching ? "Fetching…" : "Fetch Selected ▶"}
        </button>
        <label className="fetch-control-force">
          <input
            type="checkbox"
            checked={forceRefresh}
            onChange={(e) => setForceRefresh(e.target.checked)}
            disabled={fetching}
          />
          Force refresh
        </label>
      </div>

      {error && <p className="error-message">{error}</p>}
    </div>
  );
}
