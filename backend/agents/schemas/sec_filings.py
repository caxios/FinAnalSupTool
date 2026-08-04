"""
agents/schemas/sec_filings.py
─────────────────────────────
Structured output schema for the SEC Filings Analyzer Agent.

The scoring rubric lives in the agent's system prompt, not here — this module
only defines the SHAPE the model must return.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class TrendDirection(str, Enum):
    improving = "improving"
    stable = "stable"
    deteriorating = "deteriorating"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class MetricHealth(BaseModel):
    """A single fundamental dimension (revenue / margins / debt / FCF)."""
    latest_value: str | None = Field(
        None, description="Most recent value as shown in the data, e.g. '$1,895M'"
    )
    trend: TrendDirection
    commentary: str = Field(..., description="One sentence grounded in the numbers")


class FinancialHealth(BaseModel):
    """Snapshot of the company's core fundamental dimensions."""
    revenue: MetricHealth
    margins: MetricHealth
    debt: MetricHealth
    free_cash_flow: MetricHealth


class TrendItem(BaseModel):
    """A key metric's trajectory across the analyzed periods."""
    metric: str = Field(..., description="e.g. 'Gross Margin', 'Total Revenue'")
    periods: list[str] = Field(
        default_factory=list, description="Period labels, oldest → newest"
    )
    values: list[str] = Field(
        default_factory=list, description="Values aligned with `periods`"
    )
    direction: TrendDirection
    note: str = Field(..., description="What the trajectory implies")


class RiskItem(BaseModel):
    """A classified risk drawn from the Risk Factors / MD&A text."""
    risk: str = Field(..., description="Short description of the risk")
    category: str = Field(
        ..., description="e.g. 'market', 'operational', 'financial', 'regulatory'"
    )
    severity: Severity
    trend: TrendDirection = Field(
        ..., description="Whether this risk appears to be growing or easing"
    )
    note: str = Field(..., description="Evidence for the classification")


class SECFilingsReport(AgentReport):
    """The SEC Filings Analyzer's full structured report."""
    agent: str = "sec_filings"
    periods_analyzed: list[str] = Field(
        default_factory=list,
        description="Period keys analyzed, e.g. ['Q2 FY2026']",
    )
    fundamental_score: int = Field(
        ..., ge=0, le=100, description="Overall fundamental health score (0-100)"
    )
    financial_health: FinancialHealth
    multi_period_trends: list[TrendItem] = Field(default_factory=list)
    mda_insights: list[str] = Field(
        default_factory=list, description="Key insights extracted from MD&A"
    )
    risk_assessment: list[RiskItem] = Field(default_factory=list)
