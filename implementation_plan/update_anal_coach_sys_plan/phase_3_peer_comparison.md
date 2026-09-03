# Phase 3: Peer Comparison Agent in Deep Analysis

**Goal**: Equip Deep Analysis with an institutional-grade **Peer Comparison Agent**, benchmarking the target company against industry rivals across valuation multiples, profitability margins, growth trajectories, and financial health.

---

## Background & Problem

Currently:
1. Deep Analysis evaluates a company in an isolated vacuum (SEC text, earnings calls, technical charts, news).
2. Institutional equity research never analyzes a company in isolation: an analyst cannot evaluate whether a 25x P/E is cheap or expensive without knowing if industry peers trade at 15x or 40x, or whether 20% operating margin represents a leader or a laggard.
3. With `quant_risk` moved to Portfolio, Deep Analysis needs a direct peer comparison dimension to complete the fundamental picture.

---

## Tasks

### 1. Peer Data Provider (`backend/providers/peer_provider.py`)
- Implement `discover_peers(ticker: str, limit: int = 5) -> list[str]`:
  - Primary: Query `yfinance.Ticker(ticker).info` to identify sector, industry, and recommended peer tickers.
  - Fallback: Curated industry clusters for major sectors (Semiconductors: NVDA, AMD, INTC, TSM, AVGO, QCOM; Big Tech: AAPL, MSFT, GOOGL, AMZN, META; EV/Auto: TSLA, RIVN, LCID, GM, F, BYD; Cloud/SaaS: CRM, NOW, WDAY, SNOW, DDOG).
- Implement `fetch_peer_metrics(target: str, peers: list[str]) -> dict`:
  - Fetch key institutional metrics via `yfinance` for both target and peers:
    - **Valuation**: Trailing P/E, Forward P/E, EV/EBITDA, P/S, P/B, PEG.
    - **Profitability**: Gross Margin, Operating Margin, Net Margin, ROE, ROIC.
    - **Growth**: Revenue Growth YoY, Quarterly Earnings Growth YoY.
    - **Financial Health**: Total Debt / Equity, Current Ratio, Free Cash Flow Margin.
  - Compute peer benchmarks: Peer Median, Peer Mean, and Target Percentile Rank.
  - Compute relative valuation metrics: Target Premium/Discount % vs Peer Median.

### 2. Peer Comparison Agent (`backend/agents/peer_agent.py` & `backend/agents/schemas/peer.py`)
- Schema `PeerComparisonReport`:
  - `target_ticker`: string.
  - `peer_tickers`: list[str].
  - `metrics_table`: list of `{ metric: str, target_value: float, peer_median: float, peer_min: float, peer_max: float, premium_discount_pct: float }`.
  - `valuation_assessment`: `"premium" | "discount" | "in_line"`.
  - `competitive_moat`: qualitative assessment of pricing power and market share defensibility.
  - `key_differentiators`: list of bullet points detailing where the target outperforms or lags rivals.
  - `confidence`: float (0.0 to 1.0).
  - `reasoning`: 2-3 sentence summary.
- Agent Logic:
  - Pre-computes deterministic tables in Python so the LLM cannot hallucinate numerical peer ratios.
  - Prompts LLM with the deterministic data table to interpret competitive moat, pricing power, and why the valuation gap exists.

### 3. Integrate into MAS Debate & Pipeline (`backend/agents/debate.py`, `backend/services/pipeline.py`)
- Add `"peer_comparison"` to `DEBATE_ORDER` in `backend/agents/debate.py`:
  - Position: right after `sec_filings` and `earnings_call` (Fundamentals lead, Peers contextualize, News/Media add sentiment, Macro/Technical close).
- Update `display_name("peer_comparison") -> "Peer & Industry Analyst"`.
- Update `backend/services/pipeline.py`:
  - Run `PeerComparisonAgent().analyze(...)` during Phase 1.
  - Include peer report in debate context and manager synthesis.

### 4. Frontend UI Integration (`frontend/src/components/AnalysisReport.tsx` & `agentMeta.ts`)
- In `frontend/src/components/agentMeta.ts`:
  - Add `peer_comparison: "Peer & Industry"`, icon `🏢`, order position.
- In `frontend/src/components/AnalysisReport.tsx`:
  - Render an interactive **Peer Comparison Matrix**:
    - Highlight target vs peer medians.
    - Visual indicators (green/red badges for relative premium or superior margins).
    - Clear summary of competitive moat and industry positioning.

---

## Verification & Acceptance Criteria

1. **Automated Peer Fetching**:
   - Run `fetch_peer_metrics("NVDA", ["AMD", "INTC", "QCOM"])` $\rightarrow$ verify all key multiples (P/E, EV/EBITDA, Operating Margin) return valid non-null numerical values.
2. **Debate Inclusion**:
   - Run a Deep Analysis run on a stock $\rightarrow$ confirm `Peer & Industry Analyst` generates a report and participates in the sequential debate rounds.
3. **UI Rendering**:
   - Verify the Peer Comparison card in the Deep Analysis report renders clean comparison tables with percentile rankings and valuation premium/discount badges.
