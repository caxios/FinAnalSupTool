/**
 * Header.tsx
 * ──────────
 * Top bar of the application: app title, the active-company switcher, a
 * filing count for the active company, and the Investment Rules button.
 *
 * Data fetching (SEC auto-fetch, manual PDF upload) lives in the Data tab's
 * Fetch Control Panel, not here — the header only switches WHICH company
 * every other tab is scoped to.
 */

import { useEffect, useState } from "react";
import type { PeriodInfo } from "../types";
import { useDashboard } from "../context/DashboardContext";
import { getRules, getRuleProposals } from "../api";

interface HeaderProps {
  /** The active company's loaded filing periods (for showing count) */
  periods: PeriodInfo[];
  /** Opens the Investment Rules modal (App.tsx owns modal/docked state so it
   * can lay the docked drawer out alongside the main view). */
  onOpenRules: () => void;
  /** Bumped whenever a rule/proposal changes elsewhere, so this button's
   * badge counts stay in sync without polling. */
  rulesRefreshKey: number;
}

export default function Header({
  periods, onOpenRules, rulesRefreshKey,
}: HeaderProps) {
  const { activeTicker, setActiveTicker, availableTickers } = useDashboard();

  const [activeRuleCount, setActiveRuleCount] = useState(0);
  const [pendingProposalCount, setPendingProposalCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [rulesRes, proposalsRes] = await Promise.all([
          getRules({ activeOnly: true }),
          getRuleProposals("pending"),
        ]);
        if (cancelled) return;
        setActiveRuleCount(rulesRes.count);
        setPendingProposalCount(proposalsRes.count);
      } catch {
        // The badge is a convenience; the header must still render without it.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [rulesRefreshKey]);

  const hasCompanies = availableTickers.length > 0;

  return (
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

          {/* Investment Rules — accessible from any route */}
          <button
            className="btn-rules"
            onClick={onOpenRules}
            title="Your Golden Setup / Toxic Pattern rules and pending evolutions"
          >
            📜 Rules ({activeRuleCount})
            {pendingProposalCount > 0 && (
              <span className="btn-rules-badge">🟡 {pendingProposalCount}</span>
            )}
          </button>
      </div>
    </header>
  );
}
