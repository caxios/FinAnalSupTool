/**
 * RawDataView.tsx
 * ────────────────
 * The "Raw Source Data" tab of the Deep Analysis view: lets the user inspect
 * the full, unsummarized primary source data an agent actually reasoned over
 * — earnings-call transcripts, SEC filing text/tables, technical/macro
 * indicators, news articles, YouTube scripts — rather than only its
 * structured findings.
 *
 * Fetched on demand (GET /analysis/{run_id}/raw/{agent_id}) per agent, not
 * bundled with the main run payload: every agent's raw_data together can run
 * into the hundreds of KB, which the report view never needs up front.
 */

import { useEffect, useMemo, useState } from "react";
import type { AgentRawDataResponse, AgentSlot } from "../../types";
import { getAgentRawData } from "../../api";
import { AGENT_ORDER, AGENT_NAMES, AGENT_ICONS } from "../agentMeta";

interface RawDataViewProps {
  runId: string;
  reports: Record<string, AgentSlot>;
}

const SOURCE_LABEL: Record<string, string> = {
  captured: "captured with this run",
  rehydrated: "recovered from a disk cache",
  unavailable: "unavailable for this run",
};

export default function RawDataView({ runId, reports }: RawDataViewProps) {
  // Only agents that actually reported have raw data worth showing — a
  // skipped/failed slot (has `.error`) has nothing to inspect.
  const availableAgents = useMemo(
    () => AGENT_ORDER.filter((id) => reports[id] && !(reports[id] as AgentSlot).error),
    [reports]
  );

  const [selectedAgent, setSelectedAgent] = useState<string | null>(availableAgents[0] ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<AgentRawDataResponse | null>(null);
  const [search, setSearch] = useState("");

  // A different run may have a different (or no) set of successful agents —
  // re-pick the default tab and clear any stale search when it changes.
  useEffect(() => {
    setSelectedAgent(availableAgents[0] ?? null);
    setSearch("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (!selectedAgent) {
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    getAgentRawData(runId, selectedAgent)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load raw data.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, selectedAgent]);

  // A simple client-side grep over the already-fetched text — filters to
  // matching lines (with their original line number) rather than a second
  // network round trip for what is, at most, a few hundred KB of text.
  const { filteredText, matchCount } = useMemo(() => {
    const text = data?.raw_data ?? "";
    const q = search.trim().toLowerCase();
    if (!q) return { filteredText: text, matchCount: null as number | null };
    const matches: string[] = [];
    text.split("\n").forEach((line, i) => {
      if (line.toLowerCase().includes(q)) matches.push(`${i + 1}: ${line}`);
    });
    return { filteredText: matches.join("\n"), matchCount: matches.length };
  }, [data, search]);

  if (availableAgents.length === 0) {
    return (
      <p className="deep-history-empty">
        No agent in this run produced data to inspect.
      </p>
    );
  }

  return (
    <div className="raw-data-view">
      <div className="raw-data-agent-tabs">
        {availableAgents.map((id) => (
          <button
            key={id}
            className={`ledger-tab ${selectedAgent === id ? "is-active" : ""}`}
            onClick={() => setSelectedAgent(id)}
          >
            {AGENT_ICONS[id]} {AGENT_NAMES[id] ?? id}
          </button>
        ))}
      </div>

      <div className="raw-data-toolbar">
        <input
          className="trade-input raw-data-search"
          placeholder="Search this agent's raw data…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          disabled={loading || !data || data.source === "unavailable"}
        />
        {search.trim() && matchCount !== null && data?.source !== "unavailable" && (
          <span className="raw-data-match-count">
            {matchCount} matching line{matchCount === 1 ? "" : "s"}
          </span>
        )}
        {data && (
          <span className={`raw-data-source raw-data-source-${data.source}`}>
            {SOURCE_LABEL[data.source] ?? data.source}
          </span>
        )}
      </div>

      {loading && <div className="journal-notice">Loading raw source data…</div>}
      {error && <div className="journal-notice journal-error">{error}</div>}

      {!loading && !error && data && (
        data.source === "unavailable" ? (
          <p className="deep-history-empty">{data.raw_data}</p>
        ) : search.trim() && matchCount === 0 ? (
          <p className="deep-history-empty">No lines match "{search.trim()}".</p>
        ) : (
          <pre className="raw-data-text">{filteredText}</pre>
        )
      )}
    </div>
  );
}
