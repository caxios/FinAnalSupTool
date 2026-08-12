"""
agents/schemas/macro_market.py
──────────────────────────────
Structured output schema for the Macro & Market News Analyzer Agent.

This agent looks outward — rates, inflation, policy, market regime — and then
translates that environment into what it means for the target company's sector.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class MarketRegime(str, Enum):
    risk_on = "risk_on"
    neutral = "neutral"
    risk_off = "risk_off"


class ImpactDirection(str, Enum):
    tailwind = "tailwind"
    neutral = "neutral"
    headwind = "headwind"


class Sensitivity(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class PeriodRegime(BaseModel):
    """The macro regime during one slice of the analysis period."""
    period: str = Field(..., description="YYYY-MM, or a range label")
    regime: MarketRegime
    note: str = Field(..., description="What defined the macro backdrop then")


class SectorImpact(BaseModel):
    """How the macro backdrop lands on the target company's sector."""
    sector: str = Field(..., description="Best inference of the company's sector")
    direction: ImpactDirection
    rate_sensitivity: Sensitivity = Field(
        ..., description="How exposed this sector is to the rate path"
    )
    detail: str = Field(..., description="The transmission channel, e.g. financing costs, demand")


class ThemeItem(BaseModel):
    theme: str = Field(..., description="e.g. 'Fed rate path', 'tariffs', 'AI capex cycle'")
    direction: ImpactDirection
    impact_on_company: str = Field(
        ..., description="Concrete effect on THIS company, not generic commentary"
    )
    evidence: str = Field(..., description="Headlines/facts supporting the theme")


class MacroMarketReport(AgentReport):
    """The Macro & Market analyzer's full structured report."""
    agent: str = "macro_market"
    analysis_period: str = Field(..., description="YYYY-MM-DD..YYYY-MM-DD")
    articles_analyzed: int = Field(0, ge=0)
    yield_news_correlation: str = Field(
        ...,
        description=(
            "Cross-asset finding on the US 10-Year Treasury yield (^TNX): the "
            "historical relationship between yield moves and news tone/market "
            "reaction across the period, followed by the current diagnosis read "
            "through that pattern. Grounded strictly in the injected ^TNX "
            "series — no invented yield levels."
        ),
    )
    current_market_regime: MarketRegime
    macro_score: int = Field(
        ..., ge=0, le=100, description="0 = maximally hostile macro, 100 = maximally supportive"
    )
    macro_environment_over_period: list[PeriodRegime] = Field(default_factory=list)
    sector_impact: SectorImpact
    key_themes: list[ThemeItem] = Field(default_factory=list)
