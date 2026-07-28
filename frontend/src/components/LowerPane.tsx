/**
 * LowerPane.tsx
 * ─────────────
 * The bottom half of the split-screen layout — Qualitative Viewer.
 *
 * Allows the user to:
 *   1. Select a filing period from a dropdown (e.g., "2023-10K")
 *   2. Switch between section tabs: MD&A, Footnotes, Supplementary
 *   3. Toggle between PDF view (original pages) and Text view (fallback)
 *   4. View the content in a scrollable container
 *
 * PDF view uses an iframe to embed the browser's native PDF viewer,
 * which preserves original formatting and tables.
 * Text view shows the extracted plain text as a fallback.
 */

import { useState, useEffect, useMemo } from "react";
import type { PeriodInfo, FilingTextResponse } from "../types";
import { getFilingText, getFilingPdfUrl } from "../api";

// Section tab definitions — key matches the backend's section parameter
const SECTION_TABS = [
  { key: "mda", label: "MD&A" },
  { key: "footnotes", label: "Financial Footnotes" },
  { key: "supplementary", label: "Supplementary Data" },
  { key: "risk_factors", label: "Risk Factors" },
  { key: "business", label: "Business" },
] as const;

type ViewMode = "pdf" | "text";

interface LowerPaneProps {
  /** Available filing periods (from GET /periods) */
  periods: PeriodInfo[];
}

export default function LowerPane({ periods }: LowerPaneProps) {
  // Currently selected period key (e.g., "2023-10K")
  const [selectedPeriod, setSelectedPeriod] = useState<string>("");
  // Currently selected section tab
  const [activeSection, setActiveSection] = useState("mda");
  // View mode: PDF (original pages) or Text (plain text fallback)
  const [viewMode, setViewMode] = useState<ViewMode>("pdf");
  // Fetched text data from the backend (only loaded when in text mode)
  const [textData, setTextData] = useState<FilingTextResponse | null>(null);
  // Loading state (for text mode)
  const [loading, setLoading] = useState(false);
  // Error message
  const [error, setError] = useState<string | null>(null);

  // Auto-select the first period when periods list changes
  // (e.g., after initial load or after a new upload)
  useEffect(() => {
    if (periods.length > 0 && !selectedPeriod) {
      setSelectedPeriod(periods[0].period_key);
    }
  }, [periods, selectedPeriod]);

  // Build the PDF iframe URL — recalculates when period or section changes
  const pdfUrl = useMemo(() => {
    if (!selectedPeriod) return "";
    return getFilingPdfUrl(selectedPeriod, activeSection);
  }, [selectedPeriod, activeSection]);

  // Fetch text data when in text mode and period/section changes
  useEffect(() => {
    // Only fetch text when in text mode
    if (viewMode !== "text" || !selectedPeriod) return;

    let cancelled = false;

    async function fetchText() {
      setLoading(true);
      setError(null);

      try {
        const result = await getFilingText(selectedPeriod, activeSection);
        if (!cancelled) {
          setTextData(result);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load text");
          setTextData(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchText();
    return () => { cancelled = true; };
  }, [selectedPeriod, activeSection, viewMode]);

  return (
    <div className="pane lower-pane">
      {/* Pane header with title, period dropdown, and section tabs */}
      <div className="pane-header">
        <div className="pane-header-top">
          <h2 className="pane-title">Qualitative Data</h2>

          <div className="pane-header-controls">
            {/* View mode toggle */}
            <div className="view-toggle">
              <button
                className={`view-toggle-btn ${viewMode === "pdf" ? "view-toggle-active" : ""}`}
                onClick={() => setViewMode("pdf")}
                title="Show original PDF pages"
              >
                PDF
              </button>
              <button
                className={`view-toggle-btn ${viewMode === "text" ? "view-toggle-active" : ""}`}
                onClick={() => setViewMode("text")}
                title="Show extracted plain text"
              >
                Text
              </button>
            </div>

            {/* Period selector dropdown */}
            {periods.length > 0 && (
              <select
                className="period-select"
                value={selectedPeriod}
                onChange={(e) => setSelectedPeriod(e.target.value)}
              >
                {periods.map((p) => (
                  <option key={p.period_key} value={p.period_key}>
                    {p.period_key}
                    {p.period ? ` — ${p.period}` : ""}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        {/* Section tabs */}
        <div className="tab-bar">
          {SECTION_TABS.map((tab) => (
            <button
              key={tab.key}
              className={`tab ${activeSection === tab.key ? "tab-active" : ""}`}
              onClick={() => setActiveSection(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Content area */}
      <div className="pane-body">
        {/* No periods uploaded yet */}
        {periods.length === 0 && (
          <div className="pane-status pane-empty">
            Upload SEC filing PDFs to see content here.
          </div>
        )}

        {/* ── PDF View Mode ── */}
        {periods.length > 0 && viewMode === "pdf" && selectedPeriod && (
          <iframe
            className="pdf-viewer"
            src={pdfUrl}
            title={`${selectedPeriod} - ${activeSection}`}
          />
        )}

        {/* ── Text View Mode ── */}
        {periods.length > 0 && viewMode === "text" && (
          <>
            {/* Loading indicator */}
            {loading && (
              <div className="pane-status">Loading...</div>
            )}

            {/* Error message */}
            {error && (
              <div className="pane-status pane-error">{error}</div>
            )}

            {/* Text content — displayed with preserved whitespace */}
            {!loading && !error && textData?.content && (
              <div className="text-viewer">
                <h3 className="text-title">{textData.title}</h3>
                <div className="text-content">
                  {textData.content}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
