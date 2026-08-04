"""
agents/schemas/earnings_call.py
───────────────────────────────
Structured output schema for the Earnings Call Analyzer Agent.

The agent reads every earnings-call transcript in the user's analysis period, so
the schema is built for LONGITUDINAL analysis: a per-quarter breakdown plus an
explicit cross-quarter section that tracks what management promised against what
was actually delivered.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class ConfidenceLevel(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class Significance(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ResponseQuality(str, Enum):
    """How management actually answered an analyst's question."""
    direct = "direct"
    partial = "partial"
    evasive = "evasive"


class GuidanceDirection(str, Enum):
    raised = "raised"
    maintained = "maintained"
    lowered = "lowered"
    withdrawn = "withdrawn"
    not_provided = "not_provided"


class DeliveryVerdict(str, Enum):
    delivered = "delivered"
    partially_delivered = "partially_delivered"
    missed = "missed"
    too_early = "too_early"


# =============================================================================
# Per-quarter analysis
# =============================================================================

class ManagementTone(BaseModel):
    overall: str = Field(..., description="e.g. 'confident', 'cautious', 'defensive'")
    confidence_level: ConfidenceLevel
    tone_score: int = Field(..., ge=0, le=100, description="0 = very negative, 100 = very positive")
    detail: str = Field(..., description="Language/hedging evidence behind the read")


class QATopic(BaseModel):
    """One topic analysts pressed on during Q&A."""
    topic: str
    question_count: int = Field(..., ge=0, description="How many questions touched this topic")
    response_quality: ResponseQuality
    note: str = Field(..., description="What management said, and what they avoided")


class KeyDevelopment(BaseModel):
    area: str = Field(..., description="Business area, e.g. 'Data center segment'")
    detail: str = Field(..., description="The substantive development, with any numbers cited")
    significance: Significance


class BusinessSubstance(BaseModel):
    """The substance of the call — not tone, but what actually changed."""
    key_developments: list[KeyDevelopment] = Field(default_factory=list)
    strategic_shifts: list[str] = Field(
        default_factory=list, description="Changes in strategy/priorities stated on the call"
    )


class ForwardGuidance(BaseModel):
    direction: GuidanceDirection
    detail: str = Field(..., description="What was guided, and to what numbers if given")


class QuarterAnalysis(BaseModel):
    quarter: str = Field(..., description="e.g. 'Q2 2025'")
    source: str | None = Field(None, description="Transcript source domain")
    management_tone: ManagementTone
    qa_key_topics: list[QATopic] = Field(default_factory=list)
    business_substance: BusinessSubstance
    forward_guidance: ForwardGuidance


# =============================================================================
# Cross-quarter (longitudinal) tracking
# =============================================================================

class PromiseVsDelivery(BaseModel):
    """What management committed to in one quarter vs. what happened later."""
    promise_quarter: str = Field(..., description="Quarter the commitment was made")
    promise: str = Field(..., description="What management said would happen")
    outcome_quarter: str = Field(..., description="Quarter where the outcome was reported")
    outcome: str = Field(..., description="What management actually reported")
    verdict: DeliveryVerdict


class EvolvingTheme(BaseModel):
    theme: str
    trajectory: str = Field(..., description="How the theme changed across quarters")
    assessment: str = Field(..., description="What the trajectory implies for the business")


class ToneTrendPoint(BaseModel):
    quarter: str
    tone_score: int = Field(..., ge=0, le=100)


class GuidanceTrendPoint(BaseModel):
    quarter: str
    direction: GuidanceDirection


class LongitudinalTracking(BaseModel):
    """
    The cross-quarter comparison. Empty lists are valid when only one quarter's
    transcript was available — the agent lowers confidence in that case.
    """
    promise_vs_delivery: list[PromiseVsDelivery] = Field(default_factory=list)
    evolving_themes: list[EvolvingTheme] = Field(default_factory=list)
    tone_trend_across_quarters: list[ToneTrendPoint] = Field(default_factory=list)
    guidance_trend: list[GuidanceTrendPoint] = Field(default_factory=list)
    new_topics_not_in_previous: list[str] = Field(
        default_factory=list, description="Topics that appeared for the first time"
    )
    dropped_topics: list[str] = Field(
        default_factory=list, description="Topics previously discussed, now absent"
    )


class EarningsCallReport(AgentReport):
    """The Earnings Call Analyzer's full structured report."""
    agent: str = "earnings_call"
    company: str
    quarters_analyzed: list[str] = Field(
        default_factory=list, description="Quarters with a transcript, oldest → newest"
    )
    quarters_missing: list[str] = Field(
        default_factory=list, description="Quarters in range with no transcript found"
    )
    per_quarter_analysis: list[QuarterAnalysis] = Field(default_factory=list)
    longitudinal_tracking: LongitudinalTracking
