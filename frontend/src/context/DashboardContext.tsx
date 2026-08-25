/**
 * DashboardContext.tsx
 * ────────────────────
 * Shared app state for the multi-view shell.
 *
 * The `App` shell owns the state and provides it here so both the persistent
 * ChatPanel and the routed views (`<Outlet/>`) can read it without prop-drilling
 * through the router.
 *
 * Multi-company: filings are stored per company on the backend, so the shell
 * tracks WHICH company is in view (`activeTicker`). Everything else here —
 * `periods`, `company` — describes that company only, and re-loads whenever the
 * active ticker changes. Views should pass `activeTicker` to every API call so
 * the client honors the same isolation the backend enforces.
 */

import { createContext, useContext } from "react";
import type { PeriodInfo, CompanyInfo } from "../types";

export interface DashboardContextValue {
  /** Ticker currently in view; null when no company has been ingested yet. */
  activeTicker: string | null;
  /** Switch the whole app to another company. */
  setActiveTicker: (ticker: string) => void;
  /** Every company with ingested filings (drives the header switcher). */
  availableTickers: string[];
  /** The ACTIVE company's filing periods (drives dropdowns + "loaded" UI). */
  periods: PeriodInfo[];
  /** The ACTIVE company's derived identity (for View 2). */
  company: CompanyInfo | null;
  /** Bumped after every successful upload to trigger data refreshes. */
  refreshKey: number;
}

export const DashboardContext = createContext<DashboardContextValue | null>(null);

/** Read shared dashboard state. Must be used within the App shell provider. */
export function useDashboard(): DashboardContextValue {
  const ctx = useContext(DashboardContext);
  if (!ctx) {
    throw new Error("useDashboard must be used within DashboardContext.Provider");
  }
  return ctx;
}
