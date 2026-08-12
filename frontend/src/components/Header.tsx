/**
 * Header.tsx
 * ──────────
 * Top bar of the application.
 *
 * Displays the app title and two action buttons:
 *   • "Upload PDF"     — opens the modal in manual-upload mode
 *   • "Fetch from SEC" — opens the modal in SEC auto-fetch mode
 *
 * Also shows a count of how many filings are currently loaded,
 * giving the user quick feedback about the app state.
 */

import { useState } from "react";
import type { PeriodInfo, FilingMeta } from "../types";
import UploadModal from "./UploadModal";

type ModalMode = "upload" | "sec";

interface HeaderProps {
  /** List of currently loaded filing periods (for showing count) */
  periods: PeriodInfo[];
  /** Callback fired after a successful upload — parent should refresh data */
  onUploadComplete: (filings: FilingMeta[]) => void;
}

export default function Header({ periods, onUploadComplete }: HeaderProps) {
  // null = modal closed; "upload" | "sec" = modal open in that mode
  const [modalMode, setModalMode] = useState<ModalMode | null>(null);

  return (
    <>
      <header className="app-header">
        {/* App title */}
        <h1 className="app-title">Financial Analysis Tool</h1>

        {/* Right side: filing count + two action buttons */}
        <div className="header-actions">
          {/* Show how many filings are loaded */}
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
