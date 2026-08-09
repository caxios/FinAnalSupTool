/**
 * App.tsx
 * ───────
 * Root shell of the multi-view SPA.
 *
 * Layout: Header (top) → app-body [ Sidebar | routed view | AI ChatPanel ].
 * The shell owns global state (uploaded periods, derived company, a refresh
 * counter) and shares it via DashboardContext. Routing swaps only the center
 * view; the sidebar, header, and assistant persist across routes — so the AI
 * is available everywhere and can reference data from all views.
 */

import { useState, useEffect, useCallback } from "react";
import { Routes, Route } from "react-router-dom";
import type { PeriodInfo, CompanyInfo } from "./types";
import { getPeriods, getCompany } from "./api";
import { DashboardContext } from "./context/DashboardContext";
import Header from "./components/Header";
import Sidebar from "./components/Sidebar";
import ChatPanel from "./components/ChatPanel";
import FilingDashboard from "./views/FilingDashboard";
import CompanyMedia from "./views/CompanyMedia";
import MacroSentiment from "./views/MacroSentiment";
import DeepAnalysis from "./views/DeepAnalysis";

export default function App() {
  const [periods, setPeriods] = useState<PeriodInfo[]>([]);
  const [company, setCompany] = useState<CompanyInfo | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [chatOpen, setChatOpen] = useState(false);

  // ── Load shared state (periods + derived company) ──────────
  const refreshShared = useCallback(async () => {
    try {
      const [periodsRes, companyRes] = await Promise.all([
        getPeriods(),
        getCompany(),
      ]);
      setPeriods(periodsRes.periods);
      setCompany(companyRes.primary);
    } catch (err) {
      console.error("Failed to load dashboard state:", err);
    }
  }, []);

  useEffect(() => {
    refreshShared();
  }, [refreshShared]);

  // After an upload: bump refreshKey (re-fetches data in views) + reload shared.
  const handleUploadComplete = useCallback(() => {
    setRefreshKey((prev) => prev + 1);
    refreshShared();
  }, [refreshShared]);

  return (
    <DashboardContext.Provider value={{ periods, company, refreshKey }}>
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
