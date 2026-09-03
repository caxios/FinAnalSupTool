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


# =============================================================================
# Institutional Equity Research Paper — chapters 2-6
# =============================================================================
# Chapter 1 (executive summary + conviction) reuses `executive_summary`,
# `conviction`, `bull_case`, and `bear_case` below, plus `thesis_pillars` here.
# These are all synthesis-only — no new raw data reaches the Manager, so every
# figure here MUST already appear somewhere in the field agents' reports.

class SegmentBreakdown(BaseModel):
    """One product line / geography's contribution, when the reports break it out."""
    segment: str
    revenue_contribution: str | None = Field(
        None, description="e.g. '42% of revenue', from the SEC or earnings-call reports"
    )
    operating_profit_contribution: str | None = None
    commentary: str = ""


class BusinessModelChapter(BaseModel):
    """Chapter 2: segment economics and unit economics, from the reports only."""
    overview: str = Field(
        "", description="Product architecture / how the company makes money"
    )
    segments: list[SegmentBreakdown] = Field(
        default_factory=list,
        description="Empty when no agent broke out segment-level figures — do "
                    "not invent a segment split that isn't in the reports",
    )
    unit_economics_note: str = Field(
        "", description="e.g. gross margin by segment, take rate, ARPU — only "
                        "if present in the reports"
    )


class IndustryPositioningChapter(BaseModel):
    """Chapter 3: market structure and competitive positioning."""
    market_structure: str = Field(
        "", description="Market structure / TAM-SAM framing, from the reports"
    )
    competitive_moat: str = Field(
        "", description="Pricing power and defensibility — draw on the Peer "
                        "Comparison agent's `competitive_moat` when available"
    )
    peer_multiple_benchmark: str = Field(
        "", description="Where the company trades vs. peer medians — cite the "
                        "Peer Comparison agent's `valuation_assessment` and "
                        "specific multiples; empty if that agent did not report"
    )


class QoESynthesisChapter(BaseModel):
    """
    Chapter 4: earnings-quality synthesis, ONE LEVEL UP from the SEC Filings
    agent's own ``quality_of_earnings_forensic`` (the raw forensic report,
    with the Python-computed accrual table). This chapter restates that
    agent's findings in synthesis terms — it must not disagree with them, and
    it introduces no new figures.
    """
    summary: str = Field(
        "", description="2-4 sentences synthesizing the SEC agent's QoE "
                        "forensic findings for a decision-maker"
    )
    depreciation_cliff_flagged: bool = Field(
        False, description="Copied from the SEC agent's "
                            "quality_of_earnings_forensic.depreciation_cliff_detected"
    )
    structural_vs_transitory_verdict: str = Field(
        "", description="Net read: is the earnings trajectory structurally or "
                        "transitorily driven, per the SEC agent's driver lists?"
    )
    qoe_score: int = Field(
        50, ge=0, le=100,
        description="Copied from the SEC agent's "
                    "quality_of_earnings_forensic.qoe_score when that agent "
                    "reported; 50 (neutral) if it did not",
    )


class DCFScenario(BaseModel):
    """One valuation scenario, grounded in a stated multiple/figure — never a bare number."""
    scenario: Literal["bull", "base", "bear"]
    assumption: str = Field(..., description="The key assumption driving this scenario")
    implied_value: str | None = Field(
        None, description="Implied price/valuation, ONLY if derivable from a "
                          "figure in the reports; null otherwise"
    )
    basis: str | None = Field(
        None, description="The multiple/figure `implied_value` was derived "
                          "from, e.g. 'peer EV/EBITDA median 18x x company "
                          "EBITDA'; required whenever implied_value is set"
    )


class ValuationChapter(BaseModel):
    """Chapter 5: peer-relative valuation + scenario range."""
    peer_relative_read: str = Field(
        "", description="Where the company trades vs. peers and whether that "
                        "gap is justified by growth/margins — from the Peer "
                        "Comparison agent; empty if it did not report"
    )
    scenarios: list[DCFScenario] = Field(
        default_factory=list,
        description="Bull/base/bear — empty when the reports don't support "
                    "even a rough derivation; never fabricated to fill three "
                    "slots",
    )
    target_valuation_band: str | None = Field(
        None, description="e.g. '$180-$210, based on peer EV/EBITDA range "
                          "applied to trailing EBITDA'; null if ungrounded"
    )


class SensitivityFactor(BaseModel):
    factor: str
    sensitivity: str = Field(
        "", description="How the thesis responds to a move in this factor, "
                        "grounded in a figure from the reports where possible"
    )


class RiskSensitivityChapter(BaseModel):
    """Chapter 6: macro/supply-chain/regulatory sensitivities, beyond `key_risks`."""
    macro_sensitivity: str = Field(
        "", description="From the Macro Market / Macro History agents' reports"
    )
    supply_chain_risk: str = ""
    regulatory_headwinds: str = ""
    sensitivities: list[SensitivityFactor] = Field(default_factory=list)


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

    # ── Institutional Equity Research Paper — chapters 2-6 ─────────────────
    # Chapter 1 is `executive_summary` + `conviction` + `bull_case`/`bear_case`
    # above, plus `thesis_pillars` here.
    thesis_pillars: list[str] = Field(
        default_factory=list,
        description="Up to 3 core catalyst pillars behind the thesis, each "
                    "grounded in a specific figure from the reports",
    )
    business_model_and_segments: BusinessModelChapter = Field(
        default_factory=BusinessModelChapter
    )
    industry_and_peer_positioning: IndustryPositioningChapter = Field(
        default_factory=IndustryPositioningChapter
    )
    quality_of_earnings_forensic: QoESynthesisChapter = Field(
        default_factory=QoESynthesisChapter
    )
    valuation_thesis: ValuationChapter = Field(default_factory=ValuationChapter)
    key_risks_and_sensitivities: RiskSensitivityChapter = Field(
        default_factory=RiskSensitivityChapter
    )
