/**
 * UploadModal.tsx
 * ───────────────
 * Full-screen overlay modal for uploading SEC filing PDFs.
 *
 * Features:
 *   - Drag & drop zone for PDF files
 *   - "Browse files" button as fallback
 *   - File list showing selected files before upload
 *   - Upload progress indicator
 *   - Results display showing per-file status after upload
 */

import { useState, useRef, useCallback } from "react";
import type { FilingMeta } from "../types";
import { uploadFiles } from "../api";

interface UploadModalProps {
  /** Close the modal without uploading */
  onClose: () => void;
  /** Callback with upload results — parent refreshes its data */
  onUploadComplete: (filings: FilingMeta[]) => void;
}

export default function UploadModal({ onClose, onUploadComplete }: UploadModalProps) {
  // Files selected by the user (before upload)
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  // Upload in progress flag
  const [uploading, setUploading] = useState(false);
  // Upload results (shown after upload completes)
  const [results, setResults] = useState<FilingMeta[] | null>(null);
  // Error message if upload fails entirely
  const [error, setError] = useState<string | null>(null);
  // Whether the user is currently dragging files over the drop zone
  const [dragOver, setDragOver] = useState(false);

  // Ref for the hidden file input element
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Handle file selection (from input or drop) ────────────
  const addFiles = useCallback((files: FileList | null) => {
    if (!files) return;
    // Filter to only accept PDF files
    const pdfFiles = Array.from(files).filter(
      (f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")
    );
    // Append to existing selection (user can add files in multiple batches)
    setSelectedFiles((prev) => [...prev, ...pdfFiles]);
    setError(null);
  }, []);

  // ── Drag & drop handlers ──────────────────────────────────
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();       // Required to allow drop
    setDragOver(true);
  };

  const handleDragLeave = () => {
    setDragOver(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    addFiles(e.dataTransfer.files);
  };

  // ── Upload handler ────────────────────────────────────────
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

  // ── Remove a file from selection ──────────────────────────
  const removeFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      {/* Stop clicks inside the modal from closing it */}
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        {/* Modal header */}
        <div className="modal-header">
          <h2>Upload SEC Filings</h2>
          <button className="btn-close" onClick={onClose}>✕</button>
        </div>

        {/* Show results if upload is done, otherwise show upload form */}
        {results ? (
          // ── Upload Results ────────────────────────────────
          <div className="upload-results">
            <h3>Upload Results</h3>
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
            <button className="btn-primary" onClick={onClose}>
              Done
            </button>
          </div>
        ) : (
          // ── Upload Form ───────────────────────────────────
          <>
            {/* Drag & drop zone */}
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

            {/* Hidden file input triggered by the drop zone click */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf"
              multiple
              hidden
              onChange={(e) => addFiles(e.target.files)}
            />

            {/* Selected file list */}
            {selectedFiles.length > 0 && (
              <div className="file-list">
                <h3>{selectedFiles.length} file(s) selected</h3>
                {selectedFiles.map((file, i) => (
                  <div key={i} className="file-item">
                    <span className="file-name">{file.name}</span>
                    <span className="file-size">
                      {(file.size / 1024 / 1024).toFixed(1)} MB
                    </span>
                    <button
                      className="btn-remove"
                      onClick={() => removeFile(i)}
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Error message */}
            {error && <p className="error-message">{error}</p>}

            {/* Upload button */}
            <button
              className="btn-primary"
              onClick={handleUpload}
              disabled={selectedFiles.length === 0 || uploading}
            >
              {uploading ? "Uploading..." : `Upload ${selectedFiles.length} file(s)`}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
