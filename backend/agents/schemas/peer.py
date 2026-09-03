"""
agents/schemas/peer.py
───────────────────────
Structured output schema for the Peer Comparison Agent.

Same split as ``quant_risk``: every number in ``metrics_table`` (and
``valuation_assessment``, which is derived mechanically from it) is computed by
``providers.peer_provider`` BEFORE the LLM is called and copied onto the report
afterwards — see ``peer_agent.py``. The LLM only ever produces the qualitative
fields (``competitive_moat``, ``key_differentiators``, ``reasoning``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base_agent import AgentReport


class PeerMetricRow(BaseModel):
    """One valuation/profitability/growth/health metric, target vs. peer set."""

    metric: str = Field(..., description="Snake_case id, e.g. 'trailing_pe'")
    label: str = Field(..., description="Human label, e.g. 'Trailing P/E'")
    target_value: float | None = None
    peer_median: float | None = None
    peer_min: float | None = None
    peer_max: float | None = None
    premium_discount_pct: float | None = Field(
        None, description="(target - peer_median) / |peer_median|; positive = "
                           "target is higher than the peer group"
    )
    percentile: float | None = Field(
        None, description="Target's percentile rank within peers + itself, 0-100"
    )
    higher_is_better: bool = Field(
        ..., description="True for margins/growth/returns; False for valuation "
                          "multiples and leverage, where lower is cheaper/safer"
    )


class PeerComparisonReport(AgentReport):
    """The Peer Comparison Agent's full structured report."""

    agent: str = "peer_comparison"

    # ── Computed by providers.peer_provider (never by the LLM) ────────────
    target_ticker: str = ""
    peer_tickers: list[str] = Field(default_factory=list)
    sector: str | None = None
    industry: str | None = None
    discovery_method: str | None = Field(
        None,
        description="'direct_cluster_membership' | 'sector_industry_match' | "
                    "'no_match' — how the peer set was identified",
    )
    metrics_table: list[PeerMetricRow] = Field(default_factory=list)
    valuation_assessment: str = Field(
        "in_line",
        description="'premium' | 'discount' | 'in_line', derived mechanically "
                    "from the valuation multiples' premium_discount_pct — not "
                    "an LLM judgement call",
    )
    excluded_peers: list[str] = Field(
        default_factory=list,
        description="Requested peers with no usable metrics at all",
    )

    # ── Interpreted by the LLM ──────────────────────────────────────────────
    competitive_moat: str = Field(
        "",
        description="2-4 sentences: pricing power and market-share "
                    "defensibility, grounded in the metrics given",
    )
    key_differentiators: list[str] = Field(
        default_factory=list,
        description="Where the target concretely outperforms or lags peers, "
                    "each citing a specific figure from metrics_table",
    )
    data_limitations: list[str] = Field(
        default_factory=list,
        description="Peers excluded for missing data, or a peer set that "
                    "could not be identified at all",
    )
