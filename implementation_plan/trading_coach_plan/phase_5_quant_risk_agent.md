# Phase 5: Tool-Augmented Quantitative Risk Agent

**Goal**: Implement blueprint §2 — a MAS agent that computes objective portfolio
risk in Python and asks the LLM only to *interpret* it.

**Depends on**: Phase 3 (needs real holdings and weights).

**Design constraint**: follow the precedent already set by
`agents/technical_analysis_agent.py`, whose prompt states the indicators are
PRE-COMPUTED and instructs the LLM not to recalculate them. Risk math must work
the same way: NumPy/pandas produce the numbers, the LLM never does arithmetic.

## Tasks:

1. **Create `backend/services/risk_metrics.py`** (pure functions, no LLM, no I/O
   beyond price fetching — so it is unit-testable without a network)
   - `portfolio_returns(prices: pd.DataFrame, weights: np.ndarray) -> pd.Series`
   - `value_at_risk(returns, confidence=0.95, method="historical") -> float`
     — implement historical VaR first; parametric is optional.
   - `conditional_var(returns, confidence=0.95) -> float` — mean of the losses
     beyond VaR (Expected Shortfall).
   - `volatility(returns, annualize=True) -> float` (scale by sqrt(252)).
   - `max_drawdown(cumulative: pd.Series) -> float`.
   - `correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame`.
   - `marginal_risk_contribution(cov, weights) -> np.ndarray` — blueprint §2's
     headline metric: `MRC_i = (cov @ w)_i / sigma_p`, with component contribution
     `w_i * MRC_i`. Those components sum to `sigma_p`, and that property is what
     makes the number interpretable — assert it in a test.
   - `simulate_position_change(ticker, delta_weight) -> dict` returning portfolio
     volatility before and after, which answers the blueprint's "how does adding
     this stock alter total risk" question directly.
   - `numpy` and `pandas` are already dependencies. `scipy` is **not** — add it
     only if parametric VaR is implemented, otherwise skip the new dependency.

2. **Price history helper**
   - Add `fetch_price_history(tickers: list[str], start, end) -> pd.DataFrame` to
     `providers/price_provider.py`: aligned daily closes, inner-joined on dates so
     a short-history ticker cannot silently truncate the whole matrix — drop it
     with a warning instead.
   - Guard the degenerate cases: a single-holding portfolio (correlation is
     undefined — return an empty matrix, not a crash) and fewer than ~30 usable
     observations (report low confidence rather than a meaningless VaR).

3. **Create `backend/agents/quant_risk_agent.py`**
   - Subclass `BaseAgent`; `agent_id = "quant_risk"`.
   - Schema `backend/agents/schemas/quant_risk.py`: `QuantRiskReport` carrying the
     computed metrics plus `risk_assessment`, `concentration_warning`,
     `confidence`, and `reasoning` — matching the `AgentReport` base shape.
   - The system prompt presents the metrics as given data and asks for
     interpretation, mirroring `technical_analysis_agent`'s wording.
   - **Synthesis** (blueprint §2): include the Macro Market agent's regime read in
     the context dict, so the risk warning is conditioned on the macro regime
     rather than issued in a vacuum.

4. **Register the agent**
   - Export from `agents/__init__.py`.
   - Add display names to `AGENT_DISPLAY_NAMES` in `agents/debate.py` and to
     `AGENT_NAMES` / `AGENT_ICONS` / `AGENT_ORDER` in
     `frontend/src/components/agentMeta.ts` (suggested icon: scales).
   - **Decide deliberately whether it joins the debate.** It is portfolio-scoped,
     while every current `FIELD_AGENT_IDS` / `DEBATE_ORDER` agent is
     single-company. The recommended default is to run it the way
     `MacroHistoryAgent` runs — an independent report handed to the Manager,
     *not* a debate participant — because a portfolio-level argument does not
     rebut a company-level one. Record the choice in the module docstring.

## Definition of done
- Component risk contributions sum to portfolio volatility (unit test asserts it).
- A one-holding and a zero-holding portfolio both return a clean report rather
  than raising.
- The agent's report renders in Deep Analysis with correct labels and icon.
