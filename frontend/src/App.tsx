/**
 * App.tsx
 * ───────
 * Root shell of the multi-view SPA.
 *
 * Layout: Header (top) → app-body [ Sidebar | routed view | AI ChatPanel ].
 * The shell owns global state and shares it via DashboardContext. Routing swaps
 * only the center view; the sidebar, header, and assistant persist across routes
 * — so the AI is available everywhere and can reference data from all views.
 *
 * Multi-company: the backend keeps each company's filings in an isolated store,
 * so the shell tracks the list of ingested companies and which one is active.
 * The active ticker drives every per-company load below; switching it in the
 * header re-loads the whole app's data for the newly selected company.
 */

import { useState, useEffect, useCallback, useMemo } from "react";
import { Routes, Route } from "react-router-dom";
import type { PeriodInfo, CompanyInfo, FilingMeta } from "./types";
import { getPeriods, getCompany, getCompanies } from "./api";
import { DashboardContext } from "./context/DashboardContext";
import Header from "./components/Header";
import Sidebar from "./components/Sidebar";
import ChatPanel from "./components/ChatPanel";
import FilingDashboard from "./views/FilingDashboard";
import CompanyMedia from "./views/CompanyMedia";
import MacroSentiment from "./views/MacroSentiment";
import DeepAnalysis from "./views/DeepAnalysis";
import Portfolio from "./views/Portfolio";

export default function App() {
  const [activeTicker, setActiveTicker] = useState<string | null>(null);
  const [availableTickers, setAvailableTickers] = useState<string[]>([]);
  const [periods, setPeriods] = useState<PeriodInfo[]>([]);
  const [company, setCompany] = useState<CompanyInfo | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [chatOpen, setChatOpen] = useState(false);

  // ── Which companies do we have data for? ───────────────────
  // Returns the fresh list so callers can act on it immediately (the state
  // update below won't be visible until the next render).
  const refreshCompanies = useCallback(async (): Promise<string[]> => {
    try {
      const res = await getCompanies();
      const tickers = res.companies
        .map((c) => c.ticker)
        .filter((t): t is string => Boolean(t));
      setAvailableTickers(tickers);
      return tickers;
    } catch (err) {
      console.error("Failed to load company list:", err);
      return [];
    }
  }, []);

  useEffect(() => {
    refreshCompanies();
  }, [refreshCompanies]);

  // Auto-select on first load, and recover if the active company disappears
  // (e.g. the backend restarted and its in-memory stores were cleared).
  useEffect(() => {
    if (availableTickers.length === 0) {
      if (activeTicker !== null) setActiveTicker(null);
      return;
    }
    if (!activeTicker || !availableTickers.includes(activeTicker)) {
      setActiveTicker(availableTickers[0]);
    }
  }, [availableTickers, activeTicker]);

  // ── Load the ACTIVE company's data (periods + identity) ────
  useEffect(() => {
    if (!activeTicker) {
      // No company in view — clear so stale data can't linger on screen.
      setPeriods([]);
      setCompany(null);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const [periodsRes, companyRes] = await Promise.all([
          getPeriods(activeTicker),
          getCompany(activeTicker),
        ]);
        // A newer ticker selection may have landed while these were in flight;
        // dropping the stale response keeps the view consistent with the header.
        if (cancelled) return;
        setPeriods(periodsRes.periods);
        setCompany(companyRes.primary);
      } catch (err) {
        if (cancelled) return;
        console.error("Failed to load dashboard state:", err);
        setPeriods([]);
        setCompany(null);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [activeTicker, refreshKey]);

  // After an upload: refresh the company list, jump to the company just
  // ingested (so the user sees what they uploaded), and bump refreshKey to
  // re-fetch data in the views.
  const handleUploadComplete = useCallback(
    async (filings: FilingMeta[]) => {
      const tickers = await refreshCompanies();
      const ingested = filings.find(
        (f) => f.ticker && f.status !== "failed"
      )?.ticker;
      if (ingested && tickers.includes(ingested)) {
        setActiveTicker(ingested);
      }
      setRefreshKey((prev) => prev + 1);
    },
    [refreshCompanies]
  );

  const contextValue = useMemo(
    () => ({
      activeTicker,
      setActiveTicker,
      availableTickers,
      periods,
      company,
      refreshKey,
    }),
    [activeTicker, availableTickers, periods, company, refreshKey]
  );

  return (
    <DashboardContext.Provider value={contextValue}>
      <div className="app">
        <Header periods={periods} onUploadComplete={handleUploadComplete} />

        <div className="app-body">
          <Sidebar />

          <main className="view-area">
            <Routes>
              <Route path="/" element={<FilingDashboard />} />
              <Route path="/media" element={<CompanyMedia />} />
              <Route path="/macro" element={<MacroSentiment />} />
              <Route path="/analysis" element={<DeepAnalysis />} />
              <Route path="/portfolio" element={<Portfolio />} />
            </Routes>
          </main>

          <ChatPanel isOpen={chatOpen} onClose={() => setChatOpen(false)} />
        </div>

        {!chatOpen && (
          <button
            className="chat-fab"
            onClick={() => setChatOpen(true)}
            title="Ask the AI assistant"
          >
            <span className="chat-title-dot" />
            Ask AI
          </button>
        )}
      </div>
    </DashboardContext.Provider>
  );
}
