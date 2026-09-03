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
    quality_of_earnings: MetricHealth = Field(
        ..., description="At-a-glance OCF vs. Net Income read — see "
                          "`quality_of_earnings_forensic` for the full "
                          "3-statement reconciliation"
    )
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


class QoEAccrualRow(BaseModel):
    """
    One period's accrual/cash-conversion figures — computed in Python from the
    SAME raw XBRL metrics that already back the Ratios tab
    (``providers.edgar_xbrl.extract_ratio_metrics``), never recomputed or
    invented by the LLM. See ``sec_filings_agent._qoe_accrual_table``.
    """
    period: str
    net_income: float | None = None
    operating_cash_flow: float | None = None
    total_assets: float | None = None
    sloan_accrual_ratio: float | None = Field(
        None, description="(Net Income - Operating Cash Flow) / Total Assets. "
                           "Sloan (1996): |ratio| > ~0.10 flags earnings "
                           "leaning heavily on accruals rather than cash."
    )
    accrual_flag: str | None = Field(
        None, description="'low_risk' | 'moderate' | 'aggressive', from "
                           "|sloan_accrual_ratio| vs. 0.05 / 0.10"
    )
    cash_conversion_ratio: float | None = Field(
        None, description="Operating Cash Flow / Net Income — near or above "
                           "1.0 means profit is backed by cash"
    )


class QoEForensic(BaseModel):
    """
    3-statement reconciliation and depreciation-cycle forensic read —
    institutional-grade Quality of Earnings, beyond the quick snapshot in
    ``financial_health.quality_of_earnings``.
    """
    accrual_table: list[QoEAccrualRow] = Field(
        default_factory=list,
        description="Computed by Python; the LLM interprets this table via "
                    "`accrual_summary` but does not alter its figures",
    )
    accrual_summary: str = Field(
        "", description="What the accrual/cash-conversion trend across "
                        "periods implies, citing the actual ratio values"
    )
    capex_da_reconciliation: str = Field(
        "", description="How PP&E/CapEx trends in the data relate to "
                        "Depreciation & Amortization trends — the basis for "
                        "the depreciation-cliff call below"
    )
    depreciation_cliff_detected: bool = Field(
        False, description="True when D&A dropped materially while prior "
                            "PP&E/CapEx suggests the assets are simply "
                            "reaching the end of their useful life"
    )
    depreciation_cliff_note: str | None = Field(
        None, description="Evidence and magnitude if detected; null otherwise "
                          "— never a hedge like 'possibly' when the data "
                          "does not support a call either way"
    )
    footnote_crossmatch: list[str] = Field(
        default_factory=list,
        description="Each entry ties a Notes/footnote disclosure (PP&E "
                    "useful lives, inventory write-downs, capitalized R&D, "
                    "revenue recognition, litigation reserves) to a specific "
                    "movement seen in the statements or MD&A",
    )
    structural_drivers: list[str] = Field(
        default_factory=list,
        description="Sustainable earnings drivers (pricing power, volume "
                    "growth, favorable mix, structural cost reduction), each "
                    "grounded in a specific figure",
    )
    transitory_drivers: list[str] = Field(
        default_factory=list,
        description="One-off / accounting-artifact drivers (depreciation "
                    "roll-off, inventory liquidation gains, tax valuation "
                    "allowance releases, asset-sale gains), each grounded in "
                    "a specific figure",
    )
    qoe_score: int = Field(
        50, ge=0, le=100,
        description="0 = low quality (accrual-heavy, transitory-driven), "
                    "100 = high quality (cash-backed, structurally driven)",
    )


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
    quality_of_earnings_forensic: QoEForensic = Field(default_factory=QoEForensic)
