/**
 * Header.tsx
 * ──────────
 * Top bar of the application.
 *
 * Displays the app title and an "Upload Files" button.
 * Also shows a count of how many filings are currently loaded,
 * giving the user quick feedback about the app state.
 */

import { useState } from "react";
import type { PeriodInfo, FilingMeta } from "../types";
import UploadModal from "./UploadModal";

interface HeaderProps {
  /** List of currently loaded filing periods (for showing count) */
  periods: PeriodInfo[];
  /** Callback fired after a successful upload — parent should refresh data */
  onUploadComplete: (filings: FilingMeta[]) => void;
}

export default function Header({ periods, onUploadComplete }: HeaderProps) {
  // Controls whether the upload modal overlay is visible
  const [showUpload, setShowUpload] = useState(false);

  return (
    <>
      <header className="app-header">
        {/* App title */}
        <h1 className="app-title">Financial Analysis Tool</h1>

        {/* Right side: filing count + upload button */}
        <div className="header-actions">
          {/* Show how many filings are loaded */}
          {periods.length > 0 && (
            <span className="filing-count">
              {periods.length} filing{periods.length !== 1 ? "s" : ""} loaded
            </span>
          )}

          {/* Upload button opens the modal */}
          <button
            className="btn-upload"
            onClick={() => setShowUpload(true)}
          >
            📁 Upload Files
          </button>
        </div>
      </header>

      {/* Upload modal overlay — only rendered when showUpload is true */}
      {showUpload && (
        <UploadModal
          onClose={() => setShowUpload(false)}
          onUploadComplete={(filings) => {
            onUploadComplete(filings);
            setShowUpload(false);
          }}
        />
      )}
    </>
  );
}
