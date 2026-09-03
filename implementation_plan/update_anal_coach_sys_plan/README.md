# Deep Analysis & Trading Coach Upgrade Plan Index

This directory contains the phase-by-phase detailed technical specifications for upgrading **FinAnalSupTool**:

1. **[Phase 1: History Persistence & Ticker Independence](file:///c:/Users/mrsim/FinAnalSupTool/implementation_plan/update_anal_coach_sys_plan/phase_1_history_persistence.md)**
   - Decouple analysis history and ticker discovery from the volatile in-memory `DocumentStore`.
   - Enable instant loading of all past analysis runs across all companies upon entering Deep Analysis without manual SEC filing re-fetch.

2. **[Phase 2: Portfolio Quantitative Risk & Trading Coach Integration](file:///c:/Users/mrsim/FinAnalSupTool/implementation_plan/update_anal_coach_sys_plan/phase_2_portfolio_quant_risk.md)**
   - Reallocate portfolio-level quantitative risk metrics (VaR, CVaR, portfolio volatility, risk contribution, correlation matrix, FX VaR) out of Deep Analysis.
   - Build a dedicated `PortfolioRiskPanel` in the Portfolio view and feed real-time risk metrics into `CoachAgent` pre-trade reviews.

3. **[Phase 3: Peer Comparison Agent in Deep Analysis](file:///c:/Users/mrsim/FinAnalSupTool/implementation_plan/update_anal_coach_sys_plan/phase_3_peer_comparison.md)**
   - Equip Deep Analysis with a dedicated `PeerComparisonAgent` benchmarking target companies against industry rivals across valuation multiples (P/E, EV/EBITDA), margins, growth, and ROIC.
   - Integrate peer comparison directly into the multi-agent debate sequence.

4. **[Phase 4: Institutional Equity Research Paper, Forensic QoE & Data Copilot](file:///c:/Users/mrsim/FinAnalSupTool/implementation_plan/update_anal_coach_sys_plan/phase_4_institutional_research_and_qoe.md)**
   - Elevate Deep Analysis to generate comprehensive Wall Street-grade Equity Research Papers.
   - **Forensic Quality of Earnings (QoE)**: 3-Statement reconciliation (BS $\leftrightarrow$ IS $\leftrightarrow$ CFS) + Footnotes & MD&A matching to expose depreciation cliffs, working capital distortions, and structural vs transitory profits.
   - **Research Data Copilot**: On-demand interactive extraction assistant for financial tables, SEC citations, and transcript quotes.

5. **[Phase 5: Trading Coach Evolution (Personal Edge & Behavioral Mirror)](file:///c:/Users/mrsim/FinAnalSupTool/implementation_plan/update_anal_coach_sys_plan/phase_5_trading_coach_personal_edge.md)**
   - Transform Trading Coach into an empirical data mirror helping the user discover their own trading edge.
   - Expectancy ($E$) & Payoff Ratio Matrix, Disposition Effect tracker, 1-Click Emotion Tags & PnL correlation.
   - MAE/MFE Quant Engine to derive user-optimal stop-loss and take-profit cutoffs.
   - Personal Rulebook Synthesizer for Golden Setups and Toxic Pattern pre-trade guardrails.
