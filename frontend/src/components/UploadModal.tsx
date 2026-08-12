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
  /** Close the modal without uploading */
  onClose: () => void;
  /** Callback with results — parent refreshes its data */
  onUploadComplete: (filings: FilingMeta[]) => void;
}

type Mode = "upload" | "sec";
type FormType = "10-K" | "10-Q";

// Staged messages shown, in order, while a SEC fetch is in flight. The request
// is a single call, so we advance through these on a timer purely for feedback.
const SEC_STAGES = [
  "Searching SEC EDGAR…",
  "Rendering filing to PDF…",
  "Parsing & extracting financials…",
];

const CURRENT_YEAR = new Date().getFullYear();

export default function UploadModal({ onClose, onUploadComplete }: UploadModalProps) {
  // Which input mode is active
  const [mode, setMode] = useState<Mode>("upload");

  // ── Shared results (populated by either mode) ──
  const [results, setResults] = useState<FilingMeta[] | null>(null);
  const [resolved, setResolved] = useState<ResolvedFiling | null>(null);

  // ── Mode A: manual upload state ──
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Mode B: SEC fetch form state ──
  const [ticker, setTicker] = useState("");
  const [formType, setFormType] = useState<FormType>("10-K");
  const [year, setYear] = useState<number>(CURRENT_YEAR - 1);
  const [quarter, setQuarter] = useState<number>(1);
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
    setFetching(true);
    setError(null);
    try {
      const response = await fetchSecFiling({
        ticker: sym,
        form_type: formType,
        year,
        // Only send quarter for 10-Q — a 10-K has no quarter.
        ...(formType === "10-Q" ? { quarter } : {}),
      });
      setResults(response.filings);
      setResolved(response.resolved_filing);
      onUploadComplete(response.filings);
    } catch (err) {
      setError(err instanceof Error ? err.message : "SEC fetch failed");
    } finally {
      setFetching(false);
    }
  };

  const busy = uploading || fetching;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="modal-header">
          <h2>Add SEC Filings</h2>
          <button className="btn-close" onClick={onClose}>✕</button>
        </div>

        {results ? (
          // ── Results (shared by both modes) ────────────────
          <div className="upload-results">
            <h3>Results</h3>
            {resolved && (
              <div className="sec-resolved">
                Retrieved <strong>{resolved.ticker} {resolved.form_type}</strong>{" "}
                filed <strong>{resolved.filing_date}</strong> from SEC EDGAR.{" "}
                <a href={resolved.document_url} target="_blank" rel="noreferrer">
                  View source ↗
                </a>
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

                <div className="sec-field-row">
                  <label className="sec-field">
                    <span className="sec-label">Fiscal year</span>
                    <input
                      type="number"
                      className="sec-input"
                      min={1994}
                      max={CURRENT_YEAR + 1}
                      value={year}
                      onChange={(e) => setYear(Number(e.target.value))}
                      disabled={fetching}
                    />
                  </label>

                  {formType === "10-Q" && (
                    <label className="sec-field">
                      <span className="sec-label">Quarter</span>
                      <select
                        className="sec-input"
                        value={quarter}
                        onChange={(e) => setQuarter(Number(e.target.value))}
                        disabled={fetching}
                      >
                        <option value={1}>Q1</option>
                        <option value={2}>Q2</option>
                        <option value={3}>Q3</option>
                      </select>
                    </label>
                  )}
                </div>

                <p className="sec-note">
                  Pulled live from SEC EDGAR. Q4 results appear in the annual 10-K,
                  not a 10-Q.
                </p>

                {error && <p className="error-message">{error}</p>}

                {fetching ? (
                  <div className="sec-loading">
                    <span className="sec-spinner" />
                    <span>{SEC_STAGES[stageIndex]}</span>
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
