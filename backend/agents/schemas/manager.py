"""
agents/schemas/manager.py
─────────────────────────
Schema for the Manager (Synthesizer) agent's final report.

The Manager is the ONLY agent that sees every field agent's initial JSON report
plus the full debate transcript — but never any raw source data (no PDFs, no
transcripts, no headlines). It resolves the debate into one investment view.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class DebateResolution(BaseModel):
    """How the Manager adjudicates one contested point from the debate."""
    topic: str = Field(..., description="The point of contention")
    positions_summary: str = Field(
        ..., description="The opposing views in one sentence each"
    )
    winning_side: str = Field(
        ..., description="Which agent/side had the stronger evidence, and why"
    )
    resolution: str = Field(..., description="The Manager's call on this point")


class ManagerReport(AgentReport):
    """Final synthesized investment view produced from reports + debate."""
    agent: str = "manager"

    recommendation: Literal["bullish", "neutral", "bearish"] = Field(
        ..., description="The synthesized investment stance"
    )
    conviction: Literal["high", "medium", "low"] = Field(
        ..., description="How strongly the evidence supports the recommendation"
    )
    overall_score: int = Field(
        ..., ge=0, le=100,
        description="0=strongly bearish, 50=balanced, 100=strongly bullish",
    )
    executive_summary: str = Field(
        ..., description="2-4 sentence bottom line for a decision-maker"
    )
    bull_case: list[str] = Field(
        default_factory=list, description="Strongest points FOR the investment"
    )
    bear_case: list[str] = Field(
        default_factory=list, description="Strongest points AGAINST the investment"
    )
    key_debates: list[DebateResolution] = Field(
        default_factory=list,
        description="The important disagreements and how they were resolved",
    )
    consensus_points: list[str] = Field(
        default_factory=list, description="Where the analysts independently agreed"
    )
    key_risks: list[str] = Field(
        default_factory=list, description="What would most threaten the thesis"
    )
    recommended_actions: list[str] = Field(
        default_factory=list, description="Concrete next steps / what to monitor"
    )
    agents_considered: list[str] = Field(
        default_factory=list, description="Which agents' reports fed the synthesis"
    )
