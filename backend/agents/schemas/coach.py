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


class RuleMatch(BaseModel):
    """
    One of the user's own active Golden Setup / Toxic Pattern rules that the
    proposed trade matches (>=70% of the rule's specified conditions) —
    computed in Python (``services.trading_rules.match_active_rules``) and
    stamped onto the report after generation, never asserted by the LLM.
    """
    id: int
    title: str
    description: str
    conditions: dict = Field(default_factory=dict)
    win_rate: float | None = None
    payoff_ratio: float | None = None
    expectancy: float | None = None
    match_score: float = Field(..., description="0-1, share of specified conditions matched")


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

    review_type: str = Field(
        "pre_trade",
        description="'pre_trade' (a trade being considered) or 'retrospective' "
                    "(a trade already logged)",
    )
    trade_id: int | None = Field(
        None, description="The reviewed journal entry, on a retrospective review"
    )

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
    risk_warnings: list[str] = Field(
        default_factory=list,
        description="Rule-based, Python-computed portfolio-risk flags (over-"
                    "concentration, high correlation to existing holdings, a "
                    "large simulated volatility jump) — set by the agent after "
                    "generation, not asserted by the LLM.",
    )
    toxic_pattern_matches: list[RuleMatch] = Field(
        default_factory=list,
        description="The user's own active Toxic Pattern rules this proposed "
                    "trade matches — pre-trade only, computed in Python",
    )
    golden_setup_matches: list[RuleMatch] = Field(
        default_factory=list,
        description="The user's own active Golden Setup rules this proposed "
                    "trade matches — pre-trade only, computed in Python",
    )

    # ── Retrospective-only fields ────────────────────────────────────────────
    # All None on a pre-trade review, where there is no outcome to speak of.
    #
    # `process_quality` is deliberately NOT `alignment_score` under another name.
    # A retrospective review knows what happened next, and a single blended score
    # would let that outcome leak into the judgement of the decision. Scoring the
    # process separately — and generating it before the outcome is ever shown to
    # the model — is what keeps "good decision, bad luck" expressible.

    process_quality: int | None = Field(
        None, ge=0, le=100,
        description="Quality of the REASONING judged only on what was knowable "
                    "at the time; 100 = sound process, 0 = unsupported by the "
                    "data that existed then",
    )
    what_was_knowable: str | None = Field(
        None,
        description="What the data available at the trade's timestamp actually "
                    "said — the standard the decision is held to",
    )
    outcome_summary: str | None = Field(
        None,
        description="What the price then did over 7/30/90 days, stated plainly; "
                    "null when no horizon has elapsed yet",
    )
    luck_vs_skill: str | None = Field(
        None,
        description="Which quadrant this trade fell in: 'good process, good "
                    "outcome' | 'good process, bad outcome' | 'bad process, "
                    "good outcome' | 'bad process, bad outcome'",
    )
    hindsight_note: str | None = Field(
        None,
        description="Why the process and the outcome are scored separately, in "
                    "terms specific to this trade",
    )
    data_as_of: str | None = Field(
        None,
        description="run_id of the analysis that existed at the trade's "
                    "timestamp and backed the process judgement. Null means no "
                    "analysis had been run yet — never that the current one was "
                    "used instead.",
    )


class RecurringPattern(BaseModel):
    """A behaviour the journal shows more than once, with the dates to prove it."""

    pattern: str = Field(..., description="What recurs, named plainly")
    occurrences: list[str] = Field(
        default_factory=list,
        description="Dates (YYYY-MM-DD) of the trades showing it. Must be real "
                    "journal entries; unverifiable dates are stripped.",
    )
    trend: str = Field(
        "stable", description="'worsening', 'stable', or 'improving' over time"
    )
    evidence: str = Field(
        "", description="What in the journal supports the claim — quote the user"
    )


class JournalReport(AgentReport):
    """
    A review of the user's whole record rather than one decision.

    Not a loop over single-trade reviews: it answers questions that only exist at
    the level of the whole journal — which patterns actually recur, whether good
    process has actually paid, and which advice was given and then ignored.
    """

    agent: str = "trading_coach"
    review_type: str = "journal"

    scope_description: str = Field(
        "", description="Exactly what was reviewed, in words the user can check"
    )
    trades_reviewed: int = 0
    period: str | None = Field(
        None, description="Date range covered, e.g. '2026-02-10..2026-08-30'"
    )

    recurring_patterns: list[RecurringPattern] = Field(default_factory=list)
    process_vs_outcome: str = Field(
        "",
        description="Whether well-reasoned decisions have actually done better "
                    "here, or whether the record cannot yet say",
    )
    advice_followed: str | None = Field(
        None,
        description="What earlier reviews warned about and what the user then "
                    "did; null when there are no earlier reviews",
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="What this user does well. Required, not decorative — a "
                    "review that only lists faults stops being read.",
    )
    priorities: list[str] = Field(
        default_factory=list,
        description="At most 3, most important first. A list of twelve fixes is "
                    "a list of zero fixes.",
    )
    history_sufficient: bool = True
    data_limitations: list[str] = Field(default_factory=list)
    risk_warnings: list[str] = Field(
        default_factory=list,
        description="Rule-based, Python-computed CURRENT portfolio-risk flags "
                    "(concentration, high correlation, elevated VaR) — set by "
                    "the agent after generation, not asserted by the LLM.",
    )
