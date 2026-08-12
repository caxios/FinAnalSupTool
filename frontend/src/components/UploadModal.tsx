/**
 * UploadModal.tsx
 * ───────────────
 * Full-screen overlay modal for adding SEC filings, in two modes:
 *
 *   Mode A — "Upload":    manual PDF upload (drag & drop / browse).
 *   Mode B — "SEC Fetch": name a filing (ticker + form + fiscal period) and the
 *                         backend pulls it straight from SEC EDGAR, renders it
 *                         to PDF, and ingests it through the same pipeline.
 *
 * Both modes converge on the same per-file results view.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import type { FilingMeta, ResolvedFiling } from "../types";
import { uploadFiles, fetchSecFiling } from "../api";

interface UploadModalProps {
  /** Which tab to open on: "upload" (manual PDF) or "sec" (SEC fetch) */
  initialMode?: Mode;
  /** Close the modal without uploading */
  onClose: () => void;
  /** Callback with results — parent refreshes its data */
  onUploadComplete: (filings: FilingMeta[]) => void;
}

type Mode = "upload" | "sec";
type FormType = "10-K" | "10-Q";

// Staged messages shown, in order, while a SEC fetch is in flight. A range
// request renders several PDFs server-side, so we advance through these on a
// timer purely for feedback.
const SEC_STAGES = [
  "Searching SEC EDGAR…",
  "Rendering filings to PDF…",
  "Parsing & extracting financials…",
];

const CURRENT_YEAR = new Date().getFullYear();
// Must match the backend's MAX_YEAR_SPAN (schemas.api_schemas / sec_fetch).
const MAX_YEAR_SPAN = 5;

/** Provenance summary shown in the results view after a SEC range fetch. */
interface SecMeta {
  ticker: string;
  rangeLabel: string;
  succeeded: number;
  total: number;
  resolved: ResolvedFiling[];
}

export default function UploadModal({ initialMode = "upload", onClose, onUploadComplete }: UploadModalProps) {
  // Which input mode is active
  const [mode, setMode] = useState<Mode>(initialMode);

  // ── Shared results (populated by either mode) ──
  const [results, setResults] = useState<FilingMeta[] | null>(null);
  const [secMeta, setSecMeta] = useState<SecMeta | null>(null);

  // ── Mode A: manual upload state ──
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Mode B: SEC fetch form state ──
  const [ticker, setTicker] = useState("");
  const [formType, setFormType] = useState<FormType>("10-K");
  const [startYear, setStartYear] = useState<number>(CURRENT_YEAR - 2);
  const [endYear, setEndYear] = useState<number>(CURRENT_YEAR - 1);
  // Quarter bounds — only used (and sent) when formType === "10-Q".
  const [startQuarter, setStartQuarter] = useState<number>(1);
  const [endQuarter, setEndQuarter] = useState<number>(3);
  const [fetching, setFetching] = useState(false);
  const [stageIndex, setStageIndex] = useState(0);

  // ── Shared error ──
  const [error, setError] = useState<string | null>(null);

  // Advance the staged loading message while a SEC fetch runs.
  useEffect(() => {
    if (!fetching) return;
    setStageIndex(0);
    const id = setInterval(() => {
      setStageIndex((i) => Math.min(i + 1, SEC_STAGES.length - 1));
    }, 2500);
    return () => clearInterval(id);
  }, [fetching]);

  // ── Mode A: file selection (from input or drop) ──
  const addFiles = useCallback((files: FileList | null) => {
    if (!files) return;
    const pdfFiles = Array.from(files).filter(
      (f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")
    );
    setSelectedFiles((prev) => [...prev, ...pdfFiles]);
    setError(null);
  }, []);

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  };
  const handleDragLeave = () => setDragOver(false);
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    addFiles(e.dataTransfer.files);
  };

  const handleUpload = async () => {
    if (selectedFiles.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const response = await uploadFiles(selectedFiles);
      setResults(response.filings);
      onUploadComplete(response.filings);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const removeFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  // ── Mode B: SEC fetch submit ──
  const handleFetch = async () => {
    const sym = ticker.trim().toUpperCase();
    if (!sym) {
      setError("Enter a ticker symbol (e.g. AAPL).");
      return;
    }
    if (startYear > endYear) {
      setError("Start year must not be after end year.");
      return;
    }
    if (endYear - startYear + 1 > MAX_YEAR_SPAN) {
      setError(`Range is limited to ${MAX_YEAR_SPAN} years. Narrow it and retry.`);
      return;
    }
    if (formType === "10-Q" && startYear === endYear && startQuarter > endQuarter) {
      setError("Start quarter must not be after end quarter within the same year.");
      return;
    }
    setFetching(true);
    setError(null);
    try {
      const response = await fetchSecFiling({
        ticker: sym,
        form_type: formType,
        start_year: startYear,
        end_year: endYear,
        // Quarter bounds apply to 10-Q only.
        ...(formType === "10-Q"
          ? { start_quarter: startQuarter, end_quarter: endQuarter }
          : {}),
      });
      setResults(response.filings);
      setSecMeta({
        ticker: response.ticker,
        rangeLabel: response.range_label,
        succeeded: response.succeeded,
        total: response.total_files,
        resolved: response.resolved_filings,
      });
      onUploadComplete(response.filings);
    } catch (err) {
      setError(err instanceof Error ? err.message : "SEC fetch failed");
    } finally {
      setFetching(false);
    }
  };

  const busy = uploading || fetching;
  // While a fetch/upload runs, block closing the modal (esc/backdrop/✕).
  const requestClose = () => { if (!busy) onClose(); };

  return (
    <div className="modal-overlay" onClick={requestClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="modal-header">
          <h2>Add SEC Filings</h2>
          <button
            className="btn-close"
            onClick={requestClose}
            disabled={busy}
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {results ? (
          // ── Results (shared by both modes) ────────────────
          <div className="upload-results">
            <h3>Results</h3>
            {secMeta && (
              <div className="sec-resolved">
                Retrieved <strong>{secMeta.succeeded} of {secMeta.total}</strong>{" "}
                {secMeta.ticker} filing(s) for <strong>{secMeta.rangeLabel}</strong>{" "}
                from SEC EDGAR.
                {secMeta.resolved.length > 0 && (
                  <div className="sec-resolved-list">
                    {secMeta.resolved.map((rf, i) => (
                      <a
                        key={i}
                        href={rf.document_url}
                        target="_blank"
                        rel="noreferrer"
                        className="sec-resolved-chip"
                        title={`Filed ${rf.filing_date}`}
                      >
                        {rf.period_label} ↗
                      </a>
                    ))}
                  </div>
                )}
              </div>
            )}
            {results.map((filing, i) => (
              <div key={i} className={`result-item result-${filing.status}`}>
                <span className="result-filename">{filing.filename}</span>
                <span className="result-status">{filing.status}</span>
                {filing.detected_period && (
                  <span className="result-period">{filing.detected_period}</span>
                )}
                {filing.message && (
                  <p className="result-message">{filing.message}</p>
                )}
              </div>
            ))}
            <button className="btn-primary" onClick={onClose}>Done</button>
          </div>
        ) : (
          <>
            {/* Mode tabs */}
            <div className="mode-tabs" role="tablist">
              <button
                role="tab"
                aria-selected={mode === "upload"}
                className={`mode-tab ${mode === "upload" ? "mode-tab-active" : ""}`}
                onClick={() => { setMode("upload"); setError(null); }}
                disabled={busy}
              >
                📄 Upload PDF
              </button>
              <button
                role="tab"
                aria-selected={mode === "sec"}
                className={`mode-tab ${mode === "sec" ? "mode-tab-active" : ""}`}
                onClick={() => { setMode("sec"); setError(null); }}
                disabled={busy}
              >
                🔎 Fetch from SEC
              </button>
            </div>

            {mode === "upload" ? (
              // ── Mode A: manual upload ───────────────────────
              <>
                <div
                  className={`drop-zone ${dragOver ? "drop-zone-active" : ""}`}
                  onDragOver={handleDragOver}
                  onDragLeave={handleDragLeave}
                  onDrop={handleDrop}
                  onClick={() => fileInputRef.current?.click()}
                >
                  <p className="drop-icon">📄</p>
                  <p>Drag & drop PDF files here</p>
                  <p className="drop-hint">or click to browse</p>
                </div>

                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf"
                  multiple
                  hidden
                  onChange={(e) => addFiles(e.target.files)}
                />

                {selectedFiles.length > 0 && (
                  <div className="file-list">
                    <h3>{selectedFiles.length} file(s) selected</h3>
                    {selectedFiles.map((file, i) => (
                      <div key={i} className="file-item">
                        <span className="file-name">{file.name}</span>
                        <span className="file-size">
                          {(file.size / 1024 / 1024).toFixed(1)} MB
                        </span>
                        <button className="btn-remove" onClick={() => removeFile(i)}>
                          ✕
                        </button>
                      </div>
                    ))}
                  </div>
                )}

                {error && <p className="error-message">{error}</p>}

                <button
                  className="btn-primary"
                  onClick={handleUpload}
                  disabled={selectedFiles.length === 0 || uploading}
                >
                  {uploading ? "Uploading…" : `Upload ${selectedFiles.length} file(s)`}
                </button>
              </>
            ) : (
              // ── Mode B: SEC fetch ───────────────────────────
              <div className="sec-form">
                <label className="sec-field">
                  <span className="sec-label">Ticker</span>
                  <input
                    type="text"
                    className="sec-input"
                    placeholder="e.g. AAPL, NVDA, TSLA"
                    value={ticker}
                    onChange={(e) => setTicker(e.target.value.toUpperCase())}
                    onKeyDown={(e) => e.key === "Enter" && !fetching && handleFetch()}
                    disabled={fetching}
                    autoFocus
                  />
                </label>

                <div className="sec-field">
                  <span className="sec-label">Filing type</span>
                  <div className="sec-radio-row">
                    {(["10-K", "10-Q"] as FormType[]).map((ft) => (
                      <button
                        key={ft}
                        type="button"
                        className={`sec-chip ${formType === ft ? "sec-chip-active" : ""}`}
                        onClick={() => setFormType(ft)}
                        disabled={fetching}
                      >
                        {ft}
                        <span className="sec-chip-hint">
                          {ft === "10-K" ? "Annual" : "Quarterly"}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>

                <div className="sec-field">
                  <span className="sec-label">
                    {formType === "10-Q" ? "Fiscal quarter range" : "Fiscal year range"}
                  </span>
                  <div className="sec-range-row">
                    <div className="sec-range-group">
                      <input
                        type="number"
                        className="sec-input"
                        aria-label="Start year"
                        min={1994}
                        max={CURRENT_YEAR + 1}
                        value={startYear}
                        onChange={(e) => setStartYear(Number(e.target.value))}
                        disabled={fetching}
                      />
                      {formType === "10-Q" && (
                        <select
                          className="sec-input sec-quarter-select"
                          aria-label="Start quarter"
                          value={startQuarter}
                          onChange={(e) => setStartQuarter(Number(e.target.value))}
                          disabled={fetching}
                        >
                          <option value={1}>Q1</option>
                          <option value={2}>Q2</option>
                          <option value={3}>Q3</option>
                        </select>
                      )}
                    </div>

                    <span className="sec-range-dash">–</span>

                    <div className="sec-range-group">
                      <input
                        type="number"
                        className="sec-input"
                        aria-label="End year"
                        min={1994}
                        max={CURRENT_YEAR + 1}
                        value={endYear}
                        onChange={(e) => setEndYear(Number(e.target.value))}
                        disabled={fetching}
                      />
                      {formType === "10-Q" && (
                        <select
                          className="sec-input sec-quarter-select"
                          aria-label="End quarter"
                          value={endQuarter}
                          onChange={(e) => setEndQuarter(Number(e.target.value))}
                          disabled={fetching}
                        >
                          <option value={1}>Q1</option>
                          <option value={2}>Q2</option>
                          <option value={3}>Q3</option>
                        </select>
                      )}
                    </div>
                  </div>
                </div>

                <p className="sec-note">
                  Pulled live from SEC EDGAR (up to {MAX_YEAR_SPAN} years per
                  request).{" "}
                  {formType === "10-Q"
                    ? "Every quarter between the selected start and end quarter is fetched."
                    : "One annual report per year in the range is fetched."}
                </p>

                {error && <p className="error-message">{error}</p>}

                {fetching ? (
                  <div className="sec-loading">
                    <span className="sec-spinner" />
                    <span className="sec-loading-text">
                      Fetching {formType} filings for{" "}
                      {formType === "10-Q"
                        ? `${startYear} Q${startQuarter}–${endYear} Q${endQuarter}`
                        : `${startYear}–${endYear}`}
                      . This may take a minute…
                      <span className="sec-loading-stage">{SEC_STAGES[stageIndex]}</span>
                    </span>
                  </div>
                ) : (
                  <button
                    className="btn-primary"
                    onClick={handleFetch}
                    disabled={!ticker.trim()}
                  >
                    Fetch &amp; Analyze
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
