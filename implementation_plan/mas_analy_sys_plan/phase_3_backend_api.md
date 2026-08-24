# Phase 3: Backend API & Agents Updates

**Goal**: Update all routers, pipelines, and agents to require a `ticker` parameter and use the correct `CompanyStore`.

## Tasks:

1. **Update Document Routers (`backend/routers/document.py`)**
   - Update `GET /financials`, `GET /filing-text`, `GET /periods`, and `GET /filing-pdf` to require a `ticker: str` query parameter.
   - Use `company_store = store.get_company_store(ticker)` to fetch the correct data for the response.

2. **Update Analysis & Chat Routers (`backend/routers/analysis.py`, `backend/routers/chat.py`)**
   - Update the `AnalyzeRequest` schema to include `ticker: str`.
   - Update `POST /analyze` and `POST /analyze/stream` to pass `ticker` to `analyze_pipeline`.
   - Inside `services/pipeline.py`, route the analysis pipeline to use `store.get_company_store(ticker)` and save the result using `debate_store.replace(ticker, payload)`.
   - Update `POST /chat` to accept `ticker: str` and provide only that ticker's context to the chat agent.

3. **Update SEC/Media Routers**
   - Update `GET /company` (or create `GET /companies`) to return a list of all available tickers currently stored in `DocumentStore`.
