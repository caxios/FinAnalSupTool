/**
 * LowerPane.tsx
 * ─────────────
 * The bottom half of the split-screen layout — Qualitative Viewer.
 *
 * Allows the user to:
 *   1. Select a filing period from a dropdown (e.g., "2023-10K")
 *   2. Switch between section tabs: MD&A, Footnotes, Supplementary
 *   3. View the extracted text in a scrollable container
 *
 * The text is displayed with preserved line breaks (white-space: pre-wrap)
 * since SEC filing text is plain text, not Markdown.
 */

import { useState, useEffect } from "react";
import type { PeriodInfo, FilingTextResponse } from "../types";
import { getFilingText } from "../api";

// Section tab definitions — key matches the backend's section parameter
const SECTION_TABS = [
  { key: "mda", label: "MD&A" },
  { key: "footnotes", label: "Financial Footnotes" },
  { key: "supplementary", label: "Supplementary Data" },
  { key: "risk_factors", label: "Risk Factors" },
  { key: "business", label: "Business" },
] as const;

interface LowerPaneProps {
  /** Available filing periods (from GET /periods) */
  periods: PeriodInfo[];
}

export default function LowerPane({ periods }: LowerPaneProps) {
  // Currently selected period key (e.g., "2023-10K")
  const [selectedPeriod, setSelectedPeriod] = useState<string>("");
  // Currently selected section tab
  const [activeSection, setActiveSection] = useState("mda");
  // Fetched text data from the backend
  const [textData, setTextData] = useState<FilingTextResponse | null>(null);
  // Loading state
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

  // Fetch text data whenever the selected period or section changes
  useEffect(() => {
    // Don't fetch if no period is selected
    if (!selectedPeriod) return;

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
  }, [selectedPeriod, activeSection]);

  return (
    <div className="pane lower-pane">
      {/* Pane header with title, period dropdown, and section tabs */}
      <div className="pane-header">
        <div className="pane-header-top">
          <h2 className="pane-title">Qualitative Data</h2>

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

      {/* Text content area */}
      <div className="pane-body">
        {/* No periods uploaded yet */}
        {periods.length === 0 && (
          <div className="pane-status pane-empty">
            Upload SEC filing PDFs to see text sections here.
          </div>
        )}

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
      </div>
    </div>
  );
}
