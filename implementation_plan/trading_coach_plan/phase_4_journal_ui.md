# Phase 4: Portfolio & Journal UI

**Goal**: Give blueprint §1 a front end — a new "Portfolio" view with a holdings
table, a trade-entry form, and the prominent **Entry Rationale** field.

**Depends on**: Phases 2, 3.

## Tasks:

1. **Types & API client**
   - `frontend/src/types.ts`: add `Holding`, `Trade`, `PortfolioResponse`,
     mirroring the phase-2 Pydantic schemas.
   - `frontend/src/api.ts`: add `getPortfolio()`, `addHolding(body)`,
     `removeHolding(ticker)`, `getTrades(ticker?)`, `logTrade(body)`.
   - These are portfolio-scoped, **not** company-scoped: the portfolio spans every
     ticker, so do **not** thread `activeTicker` through them the way phase 5 of
     `mas_analy_sys_plan` did. `getTrades` takes an *optional* ticker filter only.

2. **New view `frontend/src/views/Portfolio.tsx`**
   - Holdings table: ticker, quantity, avg price, current price, market value,
     unrealized P/L, ROI %. Color P/L via the existing tone convention — reuse the
     `positive` / `negative` / `neutral` class suffixes used by
     `components/agentMeta.ts` rather than inventing new color classes.
   - Portfolio summary row: total cost basis, market value, total ROI.
   - Clicking a row sets `activeTicker` via `useDashboard()` — this is the join
     between the portfolio and the existing MAS views.
   - Use the existing `useAsync` hook (`hooks/useAsync.ts`) for fetching; it
     already cancels stale updates on dependency change.

3. **`frontend/src/components/portfolio/TradeForm.tsx`**
   - Inputs, in blueprint order: ticker, side (buy/sell), **transaction time**,
     **quantity** — and nothing else numeric. Price fields are absent by design.
   - **Entry Rationale**: a `<textarea>` with the visible label
     `Entry Rationale (진입 이유)`, given real vertical space and placeholder text
     that prompts psychological honesty (e.g. "Why now? What are you feeling —
     conviction, FOMO, fear?"). The blueprint calls this field out specifically; it
     must not render as a cramped afterthought.
   - On submit, show the server-derived execution price and new average as
     confirmation, so the automation is visible to the user.

4. **`frontend/src/components/portfolio/TradeHistory.tsx`**
   - Chronological journal: time, ticker, side, quantity, execution price, and the
     rationale text. Reuse `hooks/usePagination.ts` +
     `components/media/Pagination.tsx`.

5. **Navigation**
   - Add to `NAV_ITEMS` in `components/Sidebar.tsx`:
     `{ to: "/portfolio", label: "Portfolio", icon: "💼", hint: "Holdings & journal" }`.
   - Add the `<Route>` in `App.tsx` alongside the existing four.
   - Styles go in `frontend/src/index.css` following its existing class
     conventions (the project uses no CSS framework).

## Definition of done
- Add a holding, log a trade with only time + quantity, see the derived price.
- Rationale text appears verbatim in the history.
- Clicking a holding switches the header company and the other views follow.
- `npx tsc --noEmit` and `npx vite build` both clean.
