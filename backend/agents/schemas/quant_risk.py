"""
agents/schemas/quant_risk.py
────────────────────────────
Structured output schema for the Quantitative Risk Agent.

Note the split between computed and interpreted fields. Everything numeric here
is filled in by ``services.risk_metrics`` BEFORE the LLM is called and is copied
onto the report afterwards; the LLM only ever produces the prose fields. That is
enforced in the agent, not just documented — see ``quant_risk_agent.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class PositionRisk(BaseModel):
    """Per-position risk attribution. All values are computed, not generated."""

    ticker: str
    weight: float = Field(..., description="Share of portfolio market value, 0-1")
    market_value: float | None = None
    volatility: float | None = Field(
        None, description="Annualized standard deviation of this position's returns"
    )
    marginal_risk_contribution: float | None = Field(
        None,
        description="d(portfolio sigma)/d(weight) — the rate at which total risk "
                    "changes as this position grows",
    )
    risk_contribution: float | None = Field(
        None, description="weight x marginal; these sum to portfolio volatility"
    )
    risk_contribution_pct: float | None = Field(
        None, description="This position's share of total portfolio risk, 0-1"
    )


class ConcentrationInfo(BaseModel):
    """Where the portfolio's exposure is bunched up."""

    position_count: int = 0
    largest_position: str | None = None
    largest_weight: float | None = None
    top_risk_position: str | None = Field(
        None, description="Position carrying the largest share of risk"
    )
    top_risk_share: float | None = None
    herfindahl: float | None = Field(
        None, description="Sum of squared weights; 1.0 = one position, 1/n = even"
    )


class ScenarioResult(BaseModel):
    """A what-if on one position's size."""

    ticker: str
    delta_weight: float = Field(..., description="Absolute change in portfolio share")
    volatility_before: float | None = None
    volatility_after: float | None = None
    volatility_change: float | None = None
    note: str | None = None


class QuantRiskReport(AgentReport):
    """The Quantitative Risk Agent's full structured report."""

    agent: str = "quant_risk"

    # ── Computed by services.risk_metrics (never by the LLM) ──────────────
    analysis_period: str | None = Field(
        None, description="Date range of the return series actually used"
    )
    observations: int = Field(0, description="Aligned trading days in the sample")
    confidence_level: float = Field(0.95, description="Confidence level for VaR/CVaR")
    portfolio_volatility: float | None = Field(
        None, description="Annualized portfolio standard deviation"
    )
    value_at_risk: float | None = Field(
        None, description="Daily VaR as a POSITIVE loss fraction (0.031 = -3.1%)"
    )
    conditional_var: float | None = Field(
        None, description="Expected Shortfall: mean loss beyond VaR, positive"
    )
    max_drawdown: float | None = Field(
        None, description="Largest peak-to-trough decline, positive fraction"
    )
    average_correlation: float | None = Field(
        None, description="Mean off-diagonal correlation across holdings"
    )
    correlation_matrix: dict = Field(
        default_factory=dict,
        description="Pairwise correlations; empty for a single-position portfolio",
    )
    positions: list[PositionRisk] = Field(default_factory=list)
    concentration: ConcentrationInfo = Field(default_factory=ConcentrationInfo)
    scenarios: list[ScenarioResult] = Field(
        default_factory=list,
        description="Computed what-ifs on the largest risk contributors",
    )
    excluded_tickers: list[str] = Field(
        default_factory=list,
        description="Holdings left out for missing or too-short price history",
    )

    # ── Cash and currency (phase 5) ──────────────────────────────────────
    # Cash is a position, not an absence of one. Won cash is genuinely
    # risk-free for a won-based investor; dollar cash carries the exchange
    # rate's full volatility and is correlated with the rest of the book.
    cash_positions: list[dict] = Field(
        default_factory=list,
        description="Per-currency cash, with its own risk contribution",
    )
    cash: dict = Field(
        default_factory=dict,
        description="Balances, weight, and the opportunity cost of holding them",
    )
    fx_risk: dict = Field(
        default_factory=dict,
        description="Currency exposure and what it does to this book. "
                    "`fx_contribution` is portfolio volatility minus the same "
                    "portfolio hedged; NEGATIVE means the exchange rate is "
                    "reducing total risk, which is common for a won-based "
                    "investor holding dollars.",
    )
    data_sufficient: bool = Field(
        True, description="False when the sample is too small for reliable estimates"
    )

    # ── Interpreted by the LLM ────────────────────────────────────────────
    risk_assessment: str = Field(
        "",
        description="2-5 sentences reading the computed numbers: how much risk "
                    "this portfolio carries and where it comes from",
    )
    concentration_warning: str | None = Field(
        None,
        description="Explicit warning when risk is bunched in one position or "
                    "correlations are high; null when genuinely diversified",
    )
    macro_conditioned_view: str | None = Field(
        None,
        description="How the current macro regime changes the reading of these "
                    "numbers (blueprint §2's synthesis requirement)",
    )
    key_risks: list[str] = Field(
        default_factory=list, description="Specific, numbers-backed risk points"
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description="Concrete steps, each justified by a computed figure",
    )
    risk_score: int = Field(
        50, ge=0, le=100,
        description="0=very low risk, 100=extreme risk, for the dashboard",
    )
