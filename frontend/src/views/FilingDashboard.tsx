/**
 * views/FilingDashboard.tsx
 * ─────────────────────────
 * View 1 — the original filing dashboard.
 *
 * A resizable vertical split: UpperPane (quantitative financials/ratios) over
 * LowerPane (qualitative filing text). This is the existing behavior, extracted
 * from App.tsx unchanged so the app can host multiple routed views.
 */

import { useState, useEffect, useCallback, useRef } from "react";
import UpperPane from "../components/UpperPane";
import LowerPane from "../components/LowerPane";
import { useDashboard } from "../context/DashboardContext";

export default function FilingDashboard() {
  const { periods, refreshKey } = useDashboard();

  // Percentage of vertical space given to the upper pane (drag to resize).
  const [splitRatio, setSplitRatio] = useState(50);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleMouseDown = useCallback(() => setIsDragging(true), []);

  const handleMouseMove = useCallback(
    (e: MouseEvent) => {
      if (!isDragging || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const ratio = ((e.clientY - rect.top) / rect.height) * 100;
      setSplitRatio(Math.min(80, Math.max(20, ratio)));
    },
    [isDragging]
  );

  const handleMouseUp = useCallback(() => setIsDragging(false), []);

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
    <div
      ref={containerRef}
      className={`split-container ${isDragging ? "split-dragging" : ""}`}
    >
      <div style={{ height: `${splitRatio}%` }}>
        <UpperPane refreshKey={refreshKey} />
      </div>

      <div className="split-handle" onMouseDown={handleMouseDown}>
        <div className="split-handle-bar" />
      </div>

      <div style={{ height: `${100 - splitRatio}%` }}>
        <LowerPane periods={periods} />
      </div>
    </div>
  );
}
