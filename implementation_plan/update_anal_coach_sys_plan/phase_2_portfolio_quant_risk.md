# Phase 2: Portfolio Quantitative Risk & Trading Coach Integration

**Goal**: Reallocate whole-portfolio quantitative risk calculations (VaR, CVaR, portfolio volatility, risk contribution, correlation matrix, FX VaR) out of Deep Analysis and into a dedicated Portfolio Risk dashboard and Trading Coach reviews.

---

## Background & Problem

Currently:
1. `QuantRiskAgent` is executed inside `backend/services/pipeline.py` (Phase 1 of Deep Analysis).
2. Deep Analysis is intended for single-company fundamental, technical, and industry evaluation, yet it is burdened with computing math over the user's entire portfolio holdings.
3. The computed risk metrics (VaR, CVaR, correlations) are hidden inside an accordion panel in the Deep Analysis report and are completely inaccessible on the actual Portfolio view (`views/Portfolio.tsx`).
4. Trading Coach (`CoachAgent`) does not actively leverage these quantitative risk metrics to guide position sizing or warn against correlated asset accumulation.

---

## Tasks

### 1. Remove Quant Risk from Deep Analysis Pipeline (`backend/services/pipeline.py`)
- Remove `QuantRiskAgent` execution and the `quant_risk` slot from `analyze_pipeline`.
- Remove `quant_risk` status yields and payload captures from the pipeline generator.
- Remove `quant_risk` from `FIELD_AGENT_IDS` in `backend/agents/debate.py` and `backend/routers/chat.py`.
- Keep `services/risk_metrics.py` intact as the core pure-Python quantitative calculation engine.

### 2. Implement Portfolio Risk API Endpoint (`backend/routers/portfolio.py`)
- Define response schema `PortfolioRiskResponse`:
  - `analysis_period`: start date, end date, trading day observations.
  - `confidence_level`: float (default 0.95).
  - `portfolio_volatility`: annualized standard deviation.
  - `value_at_risk`: 95% 1-day Historical VaR (and Parametric VaR).
  - `conditional_var`: Expected Shortfall (average loss beyond VaR).
  - `max_drawdown`: peak-to-trough historical drawdown.
  - `positions`: list of `{ ticker, weight, volatility, marginal_risk_contribution, beta_to_portfolio }`.
  - `correlation_matrix`: pairwise asset correlation matrix `dict[str, dict[str, float]]`.
  - `fx_risk`: `{ fx_var, unhedged_exposure_pct, currency_breakdown }`.
  - `stress_scenarios`: simulated impact of position shifts (+5% weight) or market shocks.
- Implement endpoint `GET /portfolio/risk`:
  - Fetches current holdings and cash balances from SQLite via `portfolio_service` and `cash_service`.
  - Calls `risk_metrics.compute_portfolio_risk(...)`.
  - Implements an in-memory TTL cache (e.g. 5 minutes) to avoid repeated yfinance historical downloads when holdings have not changed.

### 3. Integrate Quant Risk into Trading Coach (`backend/agents/coach_agent.py`)
- Update `CoachAgent.analyze` (Pre-trade Review) and `analyze_journal`:
  - Fetch real-time portfolio risk snapshot before evaluating a prospective trade.
  - Evaluate how adding/increasing a position alters total portfolio VaR and concentration:
    - If adding a ticker pushes sector correlation $> 0.75$ or marginal risk share significantly exceeds capital weight (e.g., a 10% position contributing 35% of risk), inject a concrete quantitative risk warning into the coach's evaluation.
  - Update `_COACH_CHAT_TEMPLATE` and system prompts to ground risk assessments in real computed metrics rather than generic aphorisms.

### 4. Create Frontend Portfolio Risk Dashboard (`frontend/src/components/portfolio/PortfolioRiskPanel.tsx`)
- Build a dedicated, modern visual panel for the Portfolio view:
  - **KPI Metric Cards**:
    - **95% Daily VaR**: highlighted in red/amber with plain-language tooltip (*"On 95% of trading days, expected loss will not exceed X%"*).
    - **Expected Shortfall (CVaR)**: expected severity when a tail-risk event occurs.
    - **Annualized Volatility & Max Drawdown**.
  - **Risk vs Capital Allocation Chart/Table**:
    - Dual comparison: Capital Weight vs Marginal Risk Contribution % per ticker.
    - Instantly flags positions that punch above their weight in risk.
  - **Asset Correlation Matrix Heatmap**:
    - Color-coded grid showing pairwise correlation between holdings (green for low/negative correlation $\rightarrow$ red for correlation $> 0.7$).
  - **Currency & FX Risk Breakdown**:
    - Domestic (KRW) vs Foreign (USD) asset exposure and FX VaR.

### 5. Mount Panel in Portfolio View (`frontend/src/views/Portfolio.tsx`)
- Add `PortfolioRiskPanel` above or alongside the Holdings and Performance sections.
- Ensure risk metrics refresh automatically when trades are logged or holdings are added/removed.

---

## Verification & Acceptance Criteria

1. **Pipeline Cleanliness**:
   - Run Deep Analysis on any ticker $\rightarrow$ confirm `quant_risk` is absent from agent logs and does not slow down company analysis.
2. **API Correctness**:
   - Query `GET /portfolio/risk` with 2 or more portfolio holdings $\rightarrow$ confirm valid numerical figures for VaR, CVaR, volatility, and symmetric correlation matrix.
3. **UI Rendering**:
   - Open Portfolio tab $\rightarrow$ confirm `PortfolioRiskPanel` displays KPI cards, correlation heatmap, and risk contribution bars without layout bugs.
4. **Coach Pre-trade Review**:
   - Request pre-trade review on a high-volatility stock $\rightarrow$ confirm coach commentary references exact changes to portfolio risk and correlation.
