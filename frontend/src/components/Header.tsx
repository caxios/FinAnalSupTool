/**
 * Header.tsx
 * ──────────
 * Top bar of the application.
 *
 * Displays the app title, the active-company switcher, and two action buttons:
 *   • "Upload PDF"     — opens the modal in manual-upload mode
 *   • "Fetch from SEC" — opens the modal in SEC auto-fetch mode
 *
 * Also shows a count of how many filings are loaded FOR THE ACTIVE COMPANY,
 * giving the user quick feedback about the app state.
 *
 * The switcher is the app's context control: each company's filings live in an
 * isolated store, so changing it re-loads every view for the newly chosen one.
 */

import { useState } from "react";
import type { PeriodInfo, FilingMeta } from "../types";
import { useDashboard } from "../context/DashboardContext";
import UploadModal from "./UploadModal";

type ModalMode = "upload" | "sec";

interface HeaderProps {
  /** The active company's loaded filing periods (for showing count) */
  periods: PeriodInfo[];
  /** Callback fired after a successful upload — parent should refresh data */
  onUploadComplete: (filings: FilingMeta[]) => void;
}

export default function Header({ periods, onUploadComplete }: HeaderProps) {
  // null = modal closed; "upload" | "sec" = modal open in that mode
  const [modalMode, setModalMode] = useState<ModalMode | null>(null);
  const { activeTicker, setActiveTicker, availableTickers } = useDashboard();

  const hasCompanies = availableTickers.length > 0;

  return (
    <>
      <header className="app-header">
        {/* App title */}
        <h1 className="app-title">Financial Analysis Tool</h1>

        {/* Right side: company switcher + filing count + action buttons */}
        <div className="header-actions">
          {/* Active-company switcher. Until something is ingested there is
              nothing to switch between, so we show a hint instead. */}
          {hasCompanies ? (
            <label className="company-switcher">
              <span className="company-switcher-label">Company</span>
              <select
                className="company-select"
                aria-label="Active company"
                value={activeTicker ?? ""}
                onChange={(e) => setActiveTicker(e.target.value)}
              >
                {availableTickers.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <span className="company-empty">No company selected</span>
          )}

          {/* Show how many filings are loaded for the active company */}
          {periods.length > 0 && (
            <span className="filing-count">
              {periods.length} filing{periods.length !== 1 ? "s" : ""} loaded
            </span>
          )}

          {/* SEC auto-fetch button */}
          <button
            className="btn-sec-fetch"
            onClick={() => setModalMode("sec")}
          >
            🔎 Fetch from SEC
          </button>

          {/* Manual PDF upload button */}
          <button
            className="btn-upload"
            onClick={() => setModalMode("upload")}
          >
            📁 Upload PDF
          </button>
        </div>
      </header>

      {/* Upload modal overlay — only rendered when a mode is selected */}
      {modalMode && (
        <UploadModal
          initialMode={modalMode}
          onClose={() => setModalMode(null)}
          onUploadComplete={(filings) => {
            onUploadComplete(filings);
            setModalMode(null);
          }}
        />
      )}
    </>
  );
}
