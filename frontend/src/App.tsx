/**
 * App.tsx
 * ───────
 * Root component of the Financial Analysis Support Tool.
 *
 * Responsibilities:
 *   1. Manages global state: filing periods list and refresh trigger
 *   2. Fetches available periods from the backend on mount
 *   3. Lays out the three main sections:
 *      - Header (top bar with upload button)
 *      - UpperPane (quantitative financial tables)
 *      - LowerPane (qualitative text sections)
 *   4. Implements the resizable split-screen via a drag handle
 *
 * The split-screen uses CSS flexbox with a draggable divider.
 * The user can drag the handle up/down to resize the panes.
 */

import { useState, useEffect, useCallback, useRef } from "react";
import type { PeriodInfo } from "./types";
import { getPeriods } from "./api";
import Header from "./components/Header";
import UpperPane from "./components/UpperPane";
import LowerPane from "./components/LowerPane";
import ChatPanel from "./components/ChatPanel";

export default function App() {
  // List of available filing periods (fetched from backend)
  const [periods, setPeriods] = useState<PeriodInfo[]>([]);

  // Incremented after each upload to trigger data refresh in child components.
  // UpperPane watches this to re-fetch financial data.
  const [refreshKey, setRefreshKey] = useState(0);

  // Split ratio: percentage of vertical space allocated to the upper pane.
  // 50 means 50/50 split. User can drag the handle to change this.
  const [splitRatio, setSplitRatio] = useState(50);

  // Whether the user is currently dragging the split handle
  const [isDragging, setIsDragging] = useState(false);

  // Whether the AI assistant side panel is open
  const [chatOpen, setChatOpen] = useState(false);

  // Ref for the container to calculate mouse position relative to it
  const containerRef = useRef<HTMLDivElement>(null);

  // ── Fetch periods from backend ─────────────────────────────
  // Called on mount and after each upload
  const fetchPeriods = useCallback(async () => {
    try {
      const response = await getPeriods();
      setPeriods(response.periods);
    } catch (err) {
      console.error("Failed to fetch periods:", err);
    }
  }, []);

  // Fetch periods on initial mount
  useEffect(() => {
    fetchPeriods();
  }, [fetchPeriods]);

  // ── Upload complete handler ────────────────────────────────
  // Called by Header after a successful upload
  const handleUploadComplete = useCallback(() => {
    // Increment refreshKey to trigger data refresh in UpperPane
    setRefreshKey((prev) => prev + 1);
    // Re-fetch the periods list for the LowerPane dropdown
    fetchPeriods();
  }, [fetchPeriods]);

  // ── Split-screen drag handlers ─────────────────────────────

  /** Start dragging when mouse is pressed on the handle */
  const handleMouseDown = useCallback(() => {
    setIsDragging(true);
  }, []);

  /** Update split ratio as mouse moves */
  const handleMouseMove = useCallback(
    (e: MouseEvent) => {
      if (!isDragging || !containerRef.current) return;

      const containerRect = containerRef.current.getBoundingClientRect();
      // Calculate what percentage of the container height the mouse is at
      const mouseY = e.clientY - containerRect.top;
      const ratio = (mouseY / containerRect.height) * 100;

      // Clamp between 20% and 80% to prevent either pane from disappearing
      setSplitRatio(Math.min(80, Math.max(20, ratio)));
    },
    [isDragging]
  );

  /** Stop dragging on mouse up */
  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  // Attach mousemove and mouseup to window (not just the handle)
  // so dragging works even if the cursor leaves the handle element
  useEffect(() => {
    if (isDragging) {
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
    }
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, handleMouseMove, handleMouseUp]);

  return (
    <div className="app">
      {/* Top bar with upload button */}
      <Header
        periods={periods}
        onUploadComplete={handleUploadComplete}
      />

      {/* Body: split-screen on the left, AI assistant panel on the right */}
      <div className="app-body">
        {/* Split-screen container */}
        <div
          ref={containerRef}
          className={`split-container ${isDragging ? "split-dragging" : ""}`}
        >
          {/* Upper pane — height controlled by splitRatio */}
          <div style={{ height: `${splitRatio}%` }}>
            <UpperPane refreshKey={refreshKey} />
          </div>

          {/* Draggable divider handle */}
          <div
            className="split-handle"
            onMouseDown={handleMouseDown}
          >
            <div className="split-handle-bar" />
          </div>

          {/* Lower pane — takes remaining height */}
          <div style={{ height: `${100 - splitRatio}%` }}>
            <LowerPane periods={periods} />
          </div>
        </div>

        {/* AI assistant side panel (collapsible) */}
        <ChatPanel isOpen={chatOpen} onClose={() => setChatOpen(false)} />
      </div>

      {/* Floating toggle — shown when the assistant panel is collapsed */}
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
  );
}
