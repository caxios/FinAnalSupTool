"""
agents/schemas/coach.py
───────────────────────
Structured output schema for the Adaptive Trading Coach Agent.

``DetectedBias.past_occurrences`` is the field that keeps the coach honest: a
bias claim must point at real trades by date. The agent's prompt forbids listing
a date that does not appear in the journal it was given, and the router checks
the returned dates against the real journal before the report goes out.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class DetectedBias(BaseModel):
    """One psychological bias the coach believes it can evidence."""

    bias: str = Field(
        ..., description="Named bias, e.g. 'FOMO', 'panic selling', 'loss aversion'"
    )
    evidence: str = Field(
        ...,
        description="What in THIS rationale or the journal supports the claim — "
                    "quote the user's own words",
    )
    past_occurrences: list[str] = Field(
        default_factory=list,
        description="Dates (YYYY-MM-DD) of past trades showing the same pattern. "
                    "Must correspond to real journal entries; empty if none.",
    )
    severity: str = Field(
        "moderate", description="'mild', 'moderate', or 'strong'"
    )


class CoachReport(AgentReport):
    """The Trading Coach Agent's full structured report."""

    agent: str = "trading_coach"

    ticker: str | None = None
    proposed_action: str | None = Field(
        None, description="The trade being reviewed, e.g. 'sell 15 AAPL'"
    )

    rationale_evaluation: str = Field(
        "",
        description="The user's stated logic held against the objective data — "
                    "where they agree and, more importantly, where they conflict",
    )
    detected_biases: list[DetectedBias] = Field(default_factory=list)
    historical_pattern: str | None = Field(
        None,
        description="What the user's own trade history shows about this kind of "
                    "decision; null when the journal is too short to say",
    )
    coaching_feedback: str = Field(
        "", description="Direct, actionable guidance — a coach, not a scold"
    )
    alignment_score: int = Field(
        50, ge=0, le=100,
        description="0 = rationale contradicts the data entirely, "
                    "100 = fully consistent with it",
    )
    supporting_data_points: list[str] = Field(
        default_factory=list,
        description="Specific figures from the agent reports that informed this",
    )
    data_limitations: list[str] = Field(
        default_factory=list,
        description="What the coach could NOT see — short journal, missing "
                    "fundamental or technical report, etc.",
    )
    history_sufficient: bool = Field(
        True,
        description="False when there are too few logged trades for any "
                    "behavioural pattern to be meaningful",
    )
