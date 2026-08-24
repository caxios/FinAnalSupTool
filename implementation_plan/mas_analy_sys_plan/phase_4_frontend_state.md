# Phase 4: Frontend State & Header UI

**Goal**: Manage multi-company state in the React frontend and allow users to switch contexts.

## Tasks:

1. **Update Context (`frontend/src/context/DashboardContext.tsx`)**
   - Add `activeTicker: string | null` and `setActiveTicker: (ticker: string) => void` to the context state.
   - Add `availableTickers: string[]` to list all fetched/uploaded companies.
   - Ensure `periods` and `company` reflect the data for the `activeTicker`.

2. **Update App Shell (`frontend/src/App.tsx`)**
   - On load, fetch the list of `availableTickers`. If tickers exist and `activeTicker` is null, set `activeTicker` to the first available ticker.
   - Pass `activeTicker` down to all views as necessary or ensure they consume it from `useDashboard()`.

3. **Update Header UI (`frontend/src/components/Header.tsx`)**
   - Add a `<select>` dropdown element in the Header that maps over `availableTickers`.
   - When the user selects a different ticker, call `setActiveTicker(newTicker)` to switch contexts.
   - Display a fallback UI ("No company selected") if no data is available.
