"""
agents/schemas/youtube_analysis.py
──────────────────────────────────
Structured output schema for the YouTube Video Analyzer Agent.

This agent does NOT reduce a video to "bullish" or "bearish". It deconstructs
each thesis: what is claimed, what evidence backs the claim, how strong that
evidence is, and what the analyst failed to consider.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class Thesis(str, Enum):
    bullish = "bullish"
    neutral = "neutral"
    bearish = "bearish"


class ArgumentStrength(str, Enum):
    strong = "strong"
    moderate = "moderate"
    weak = "weak"


class KeyArgument(BaseModel):
    """One argument made in a video, with its evidential backing."""
    argument: str
    supporting_data: str = Field(
        ..., description="Specific data cited, or 'none — asserted without evidence'"
    )
    strength: ArgumentStrength = Field(
        ..., description="Strong = specific verifiable data; weak = bare assertion"
    )


class VideoAnalysis(BaseModel):
    channel: str
    title: str
    published: str | None = None
    video_id: str | None = None
    thesis: Thesis
    key_arguments: list[KeyArgument] = Field(default_factory=list)
    blind_spots: list[str] = Field(
        default_factory=list, description="Important factors the analyst ignored"
    )
    actionable_insight: str = Field(..., description="One-line takeaway")


class Disagreement(BaseModel):
    topic: str
    positions: list[str] = Field(
        default_factory=list, description="The opposing views, attributed to channels"
    )
    stronger_side: str = Field(..., description="Which side has better evidence, and why")


class CrossChannelSynthesis(BaseModel):
    agreements: list[str] = Field(
        default_factory=list, description="Points multiple channels independently make"
    )
    disagreements: list[Disagreement] = Field(default_factory=list)
    consensus_view: str = Field(..., description="Where the commentary nets out overall")


class YouTubeAnalysisReport(AgentReport):
    """The YouTube Video Analyzer's full structured report."""
    agent: str = "youtube_analysis"
    company: str
    channels_analyzed: int = Field(0, ge=0)
    videos_analyzed: int = Field(0, ge=0)
    analysis_period: str = Field(..., description="YYYY-MM-DD..YYYY-MM-DD")
    per_video_analysis: list[VideoAnalysis] = Field(default_factory=list)
    cross_channel_synthesis: CrossChannelSynthesis
    overall_consensus_score: int = Field(
        ..., ge=0, le=100, description="0 = uniformly bearish commentary, 100 = uniformly bullish"
    )
