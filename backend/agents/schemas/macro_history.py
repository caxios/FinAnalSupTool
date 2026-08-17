"""
agents/schemas/macro_history.py
───────────────────────────────
Structured output schema for the Macro History Teller Agent.

This agent finds historical periods whose macroeconomic conditions most closely
mirror the present — with a focus on what happened to the TARGET COMPANY's
sector during those periods — and presents the lessons.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class HistoricalAnalogue(BaseModel):
    """One historical period identified as an analogue to today."""
    period: str = Field(
        ..., description="Date range, e.g. '1994-02 ~ 1995-01'"
    )
    title: str = Field(
        ..., description="Short descriptive title, e.g. '1994 Fed Tightening Cycle'"
    )
    similarity_score: int = Field(
        ..., ge=0, le=100,
        description="0-100 score of how closely this period mirrors today's conditions",
    )
    similarity_factors: list[str] = Field(
        default_factory=list,
        description="Specific macro conditions that match today (cite data)",
    )
    differences: list[str] = Field(
        default_factory=list,
        description="Key differences from today (prevents false-equivalence)",
    )
    market_outcome: str = Field(
        ..., description="What happened to the broad market during/after this period",
    )
    sector_specific_outcome: str = Field(
        ...,
        description=(
            "What happened to the TARGET COMPANY's sector/industry specifically "
            "during this period — the most important field"
        ),
    )
    key_events: list[str] = Field(
        default_factory=list,
        description="Major events during this period that drove market outcomes",
    )
    lesson_for_today: str = Field(
        ..., description="The concrete takeaway for today's investment decision",
    )


class ProbabilityScenario(BaseModel):
    """A forward-looking scenario based on the historical analogues."""
    scenario: str = Field(..., description="Short title, e.g. 'Soft Landing'")
    probability: str = Field(
        ..., description="Qualitative: 'high', 'medium', or 'low'"
    )
    description: str = Field(
        ..., description="What this scenario looks like and which analogue supports it",
    )
    sector_implication: str = Field(
        ..., description="What this means for the target company's sector",
    )


class MacroHistoryReport(AgentReport):
    """The Macro History Teller Agent's full structured report."""
    agent: str = "macro_history"
    analysis_period: str = Field(
        ..., description="Current analysis period, YYYY-MM-DD..YYYY-MM-DD"
    )
    current_regime_summary: str = Field(
        ...,
        description=(
            "2-4 sentence summary of today's macro regime: rate environment, "
            "inflation trajectory, growth picture, and market sentiment"
        ),
    )
    target_sector: str = Field(
        ..., description="The inferred sector of the target company"
    )
    current_indicators_snapshot: dict = Field(
        default_factory=dict,
        description=(
            "Key current indicator values as a flat dict, "
            "e.g. {'CPI_YoY': 3.1, 'Unemployment': 3.8, ...}"
        ),
    )
    analogues: list[HistoricalAnalogue] = Field(
        default_factory=list,
        description="1-3 historical analogues, ordered by similarity_score descending",
    )
    primary_analogue: str = Field(
        ..., description="Title of the single most relevant analogue"
    )
    sector_historical_context: str = Field(
        ...,
        description=(
            "A paragraph on how the target sector has historically behaved "
            "across rate cycles, recessions, and recoveries"
        ),
    )
    probability_scenarios: list[ProbabilityScenario] = Field(
        default_factory=list,
        description="2-4 forward-looking scenarios based on the analogues",
    )
    data_limitations: list[str] = Field(
        default_factory=list,
        description="Honest caveats about data gaps or analogue imperfections",
    )
