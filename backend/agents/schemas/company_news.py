"""
agents/schemas/company_news.py
──────────────────────────────
Structured output schema for the Company News Analyzer Agent.

The point of this agent is BUSINESS IMPACT, not sentiment labelling: for each
significant article it records which segment is affected, through what mechanism,
how big the effect is, and over what horizon.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class SentimentLabel(str, Enum):
    positive = "positive"
    neutral = "neutral"
    negative = "negative"
    mixed = "mixed"


class Magnitude(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class TimeHorizon(str, Enum):
    short_term = "short_term"
    medium_term = "medium_term"
    long_term = "long_term"


class SentimentScore(BaseModel):
    label: SentimentLabel
    score: int = Field(..., ge=0, le=100, description="0 = maximally negative, 100 = maximally positive")
    note: str = Field(..., description="One sentence on what drives the score")


class BusinessImpact(BaseModel):
    """How one article's news actually affects the company's operations."""
    headline: str
    source: str = Field(..., description="Publisher domain, e.g. 'cnbc.com'")
    published: str | None = None
    sentiment: SentimentLabel
    affected_segment: str = Field(
        ..., description="Business segment/unit affected, or 'company-wide'"
    )
    impact_type: str = Field(
        ...,
        description=(
            "Mechanism of impact, e.g. competitive_advantage, regulatory_headwind, "
            "market_expansion, product_launch, demand_shift, cost_pressure, "
            "litigation, management_change, capital_allocation"
        ),
    )
    magnitude: Magnitude
    time_horizon: TimeHorizon
    analysis: str = Field(..., description="How this changes the business, not just the mood")


class MonthlySentiment(BaseModel):
    month: str = Field(..., description="YYYY-MM")
    score: int = Field(..., ge=0, le=100)
    article_count: int = Field(..., ge=0)
    note: str = Field(..., description="What dominated coverage that month")


class CatalystItem(BaseModel):
    catalyst: str
    evidence: str = Field(..., description="The article(s)/facts supporting it")
    time_horizon: TimeHorizon


class HeadwindItem(BaseModel):
    headwind: str
    evidence: str = Field(..., description="The article(s)/facts supporting it")
    time_horizon: TimeHorizon


class CompanyNewsReport(AgentReport):
    """The Company News Analyzer's full structured report."""
    agent: str = "company_news"
    company: str
    articles_analyzed: int = Field(..., ge=0)
    analysis_period: str = Field(..., description="YYYY-MM-DD..YYYY-MM-DD")
    overall_sentiment: SentimentScore
    business_impact_analysis: list[BusinessImpact] = Field(default_factory=list)
    sentiment_trend_over_period: list[MonthlySentiment] = Field(default_factory=list)
    catalysts: list[CatalystItem] = Field(default_factory=list)
    headwinds: list[HeadwindItem] = Field(default_factory=list)
