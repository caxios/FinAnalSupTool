# Phase 5: Frontend API & Views Updates

**Goal**: Propagate the `activeTicker` context to all API requests and Views to ensure data isolation is respected on the client.

## Tasks:

1. **Update API Client (`frontend/src/api.ts`)**
   - Modify API wrapper functions (`getFinancials`, `getFilingText`, `getFilingPdfUrl`, `runAnalysis`, `runAnalysisStream`, `askChat`, etc.) to accept a `ticker: string` parameter.
   - Append `?ticker=${ticker}` to GET request URLs or include `{ ticker }` in POST JSON payloads.

2. **Update Views (`frontend/src/views/*.tsx`)**
   - In `FilingDashboard.tsx`, `CompanyMedia.tsx`, `DeepAnalysis.tsx`, and `MacroSentiment.tsx`, read `activeTicker` from `useDashboard()`.
   - Pass `activeTicker` to all API calls. 
   - Add early returns or placeholder UI (e.g., "Please select a company to view analysis") if `activeTicker` is null.
   - Ensure that changing the `activeTicker` in the Header causes the Views to re-fetch and render the correct data cleanly.
