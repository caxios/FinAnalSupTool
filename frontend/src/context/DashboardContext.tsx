/**
 * DashboardContext.tsx
 * ────────────────────
 * Shared app state for the multi-view shell.
 *
 * The `App` shell owns the state (uploaded periods, derived company, and a
 * refresh counter bumped after each upload) and provides it here so both the
 * persistent ChatPanel and the routed views (`<Outlet/>`) can read it without
 * prop-drilling through the router.
 */

import { createContext, useContext } from "react";
import type { PeriodInfo, CompanyInfo } from "../types";

export interface DashboardContextValue {
  /** Uploaded filing periods (drives dropdowns + "filings loaded" UI). */
  periods: PeriodInfo[];
  /** The company derived from the uploaded filings (for View 2). */
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
