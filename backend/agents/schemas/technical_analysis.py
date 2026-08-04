"""
agents/schemas/technical_analysis.py
────────────────────────────────────
Structured output schema for the Technical Analysis (Price Trend) Agent.

The LLM interprets PRE-COMPUTED indicators (from price_provider.py); it never
recomputes them. The scoring rubric lives in the agent's system prompt.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class PrimaryTrend(str, Enum):
    uptrend = "uptrend"
    downtrend = "downtrend"
    sideways = "sideways"


class Strength(str, Enum):
    weak = "weak"
    moderate = "moderate"
    strong = "strong"


class Reliability(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class TrendAssessment(BaseModel):
    primary_trend: PrimaryTrend
    strength: Strength
    trend_score: int = Field(..., ge=0, le=100, description="Technical outlook 0-100")
    detail: str = Field(..., description="What the moving averages/price imply")


class MomentumIndicators(BaseModel):
    """Plain-language interpretations of the momentum indicator values."""
    rsi: str = Field(..., description="Interpretation of RSI(14), incl. the value")
    macd: str = Field(..., description="Interpretation of MACD vs signal/histogram")
    volume: str = Field(..., description="Interpretation of current vs avg volume")
    divergence: str | None = Field(
        None, description="Any notable divergence (e.g. price up on falling volume)"
    )


class KeyLevels(BaseModel):
    support: list[float] = Field(
        default_factory=list, description="Nearby support price levels"
    )
    resistance: list[float] = Field(
        default_factory=list, description="Nearby resistance price levels"
    )
    note: str = Field(..., description="How price sits relative to these levels")


class PatternRecognition(BaseModel):
    pattern: str = Field(..., description="Current chart pattern, or 'none'")
    implication: str = Field(..., description="Bullish/bearish/neutral implication")
    reliability: Reliability = Field(
        ..., description="Patterns are probabilistic — how reliable this read is"
    )


class PriceVsFundamentals(BaseModel):
    """
    The technical agent does not receive fundamental data; it characterizes the
    price move so the aggregator can later reconcile it with fundamentals.
    """
    period_return: str = Field(..., description="Return over the period, e.g. '+16.3%'")
    assessment: str = Field(
        ..., description="Is the move momentum-driven / stretched / orderly?"
    )
    note: str = Field(
        ..., description="Flag that fundamental reconciliation happens elsewhere"
    )


class TechnicalAnalysisReport(AgentReport):
    """The Technical Analysis agent's full structured report."""
    agent: str = "technical_analysis"
    ticker: str
    analysis_period: str = Field(..., description="Date range analyzed, YYYY-MM-DD..YYYY-MM-DD")
    current_price: float
    trend_assessment: TrendAssessment
    momentum_indicators: MomentumIndicators
    key_levels: KeyLevels
    pattern_recognition: PatternRecognition
    price_vs_fundamentals: PriceVsFundamentals
    data_warning: str | None = Field(
        None, description="Set when history is insufficient (e.g. SMA200 unreliable)"
    )
