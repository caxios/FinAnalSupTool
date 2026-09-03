# Phase 4: Institutional Equity Research Paper, Forensic QoE & Data Copilot

**Goal**: Transform Deep Analysis from a simple buy/sell score generator into a professional **Institutional Equity Research Platform** featuring **Forensic Quality of Earnings (QoE)** analysis (3-Statement reconciliation, depreciation cliff detection, footnotes & MD&A cross-matching) and an **Interactive On-Demand Research Data Copilot**.

---

## Background & Problem

Currently:
1. Deep Analysis outputs an overall verdict with three numerical gauge scores (Fundamental, Sentiment, Technical) and short bullet points, which falls short of institutional equity research papers published by major investment banks.
2. Financial statement analysis often takes operating profit at face value. A profit jump caused by a **Depreciation Cliff** (heavy past PP&E CapEx reaching the end of its useful life, causing D&A expense to abruptly drop) can be falsely mistaken for operational excellence or expanding market demand.
3. Analysts need to drill down into specific questions while drafting reports (e.g. *"What did MD&A say about cloud margin compression?"*, *"Extract segment revenue for the past 3 years"*) without leaving the analysis workspace.

---

## Tasks

### 1. Forensic 3-Statement Reconciliation Engine (`backend/agents/sec_filings_agent.py`)
- Expand SEC Filings Agent directives and analytical logic:
  - **3-Statement Cross-Triangulation**:
    - Reconcile Net Income (IS) $\leftrightarrow$ Cash from Operations (CFS) $\leftrightarrow$ Working Capital changes (BS).
    - Compute the **Sloan Accrual Ratio**: $\frac{\text{Net Income} - \text{CFO}}{\text{Total Assets}}$. Flag aggressive earnings quality when accruals surge.
    - Reconcile CapEx & PP&E changes (BS) $\leftrightarrow$ Depreciation & Amortization (CFS/IS).
  - **Depreciation Cliff & CapEx Cycle Tracking**:
    - Track historical PP&E balances and D&A trends across the past 8 quarters.
    - Specifically analyze whether operating margin expansions or EBIT jumps are driven by **D&A cost roll-offs (depreciation cliff)** vs genuine top-line growth or pricing power.
  - **Footnotes (주석) & MD&A Cross-Matching**:
    - Cross-examine Notes to Consolidated Financial Statements (PP&E useful lives, inventory write-down reserves, capitalized R&D/software, revenue recognition rules, litigation provisions).
    - Cross-reference management explanations in MD&A against real cash flow and balance sheet movements to classify earnings drivers into:
      - **Structural Factors**: Sustainable price increases, volume growth, favorable product mix, structural cost reductions.
      - **Transitory / Accounting Artifacts**: Depreciation roll-offs, inventory liquidation gains, tax valuation allowance releases, non-operating asset sales.

### 2. Institutional Equity Research Report Generator (`backend/agents/manager_agent.py`)
- Expand `ManagerReport` schema (`backend/agents/schemas/manager.py`) into institutional publication chapters:
  1. `executive_summary`: Investment thesis, 3 core catalyst pillars, target valuation band, conviction level.
  2. `business_model_and_segments`: Segment revenue/operating profit contribution, product architecture, unit economics.
  3. `industry_and_peer_positioning`: Market structure, TAM/SAM, competitive moat, peer multiple benchmarks.
  4. `quality_of_earnings_forensic`: Detailed 3-statement reconciliation, depreciation cycle analysis, structural vs transitory earnings breakdown.
  5. `valuation_thesis`: Peer multiple relative valuation + DCF scenario range (Base, Bull, Bear).
  6. `key_risks_and_sensitivities`: Macro sensitivity, supply chain risks, regulatory headwinds.

### 3. Interactive Research Data Copilot Endpoint (`backend/routers/analysis.py`)
- Add endpoint `POST /analysis/query-data`:
  - Request: `{ ticker: str, query: str, data_scope?: "financials" | "sec_text" | "earnings" | "peers" | "all" }`.
  - Resolution:
    - Queries merged financial tables, SEC filing text chunks (MD&A, Notes), earnings call transcripts, and yfinance peer tables.
    - Calls Gemini with a strict grounded extraction prompt.
  - Response:
    - `table_markdown`: structured Markdown table of requested numbers (if applicable).
    - `citations`: exact filing period, section name, and excerpt.
    - `analytical_note`: concise 2-sentence context for incorporation into an analyst note.

### 4. Frontend Research Paper View (`frontend/src/components/analysis/ResearchPaperView.tsx`)
- Build a dedicated, publication-style document view in `DeepAnalysis.tsx`:
  - Clean typography, institutional-grade layout, collapsible chapters.
  - Clear **"Quality of Earnings & Forensic Analysis"** highlight box detailing depreciation trends, cash conversion, and structural vs one-off profit drivers.
  - Actions:
    - **"Copy as Markdown"**: copies formatted report directly to clipboard for external editing.
    - **"Print / Export PDF"**: clean printable stylesheet.

### 5. Frontend Research Copilot Panel (`frontend/src/components/analysis/ResearchCopilot.tsx`)
- Embed an on-demand data extraction drawer inside Deep Analysis:
  - Preset quick queries: *"3-year Segment Revenue Table"*, *"CapEx vs D&A Trend"*, *"MD&A on Operating Margin Drivers"*, *"Peer Multiple Comparison"*.
  - Custom user prompt input with instant copyable Markdown data tables and quote snippets.

---

## Verification & Acceptance Criteria

1. **Forensic QoE Detection**:
   - Run SEC Filings Agent on a company with rolling-off D&A $\rightarrow$ confirm the report explicitly flags the depreciation cliff and categorizes it as a transitory accounting factor rather than structural revenue expansion.
2. **Institutional Paper Generation**:
   - Confirm completed Deep Analysis generates all 6 structured institutional chapters.
   - Verify `ResearchPaperView` renders with professional layout, tables, and one-click clipboard copy.
3. **Data Copilot Verification**:
   - Query `POST /analysis/query-data` with *"Compare operating margin across the last 4 quarters"* $\rightarrow$ confirm response returns a structured markdown table with exact SEC period citations.
