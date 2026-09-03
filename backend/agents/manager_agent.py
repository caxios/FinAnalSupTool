"""
agents/manager_agent.py
───────────────────────
Manager (Synthesizer) Agent — Phase 3 of the MAS.

Consumes ONLY the field agents' initial JSON reports and the final debate
transcript, and produces one reconciled investment view. It deliberately never
receives raw source data (PDFs, earnings transcripts, headlines, price series):
the field agents already distilled those into their reports and defended them in
the debate, so re-feeding the raw text to the Manager would multiply token cost
for no gain and blur the separation of concerns.
"""

from __future__ import annotations

import json
import logging

from . import debate as debate_mod
from .base_agent import BaseAgent
from .schemas.manager import ManagerReport

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are the Lead Analyst (Manager) of a multi-agent investment research team.
Six specialist analysts each studied one evidence domain (SEC filings, earnings
calls, company news, analyst commentary, macro/market, technical/price), wrote an
initial report, then debated each other in a round-table where they cited their
raw data to refute or reinforce one another.

You are given ONLY their initial JSON reports and the full debate transcript. You
do NOT have the underlying raw data, and you must not invent any. Your job is to
SYNTHESIZE — weigh the evidence, resolve the disagreements, and issue one clear,
defensible investment view.

You now produce a full INSTITUTIONAL EQUITY RESEARCH PAPER (the format a
sell-side research desk would publish), not just a verdict. Chapter 1 is the
existing verdict fields (`executive_summary`, `conviction`, `bull_case`,
`bear_case`, plus `thesis_pillars` below); chapters 2-6 are new sections below.
EVERY chapter is synthesis-only: you have no raw data, so every figure in every
chapter must already appear in the reports or transcript you were given.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentences on HOW you weighed the agents to reach the call>",
  "recommendation": "bullish|neutral|bearish",
  "conviction": "high|medium|low",
  "overall_score": <int 0-100>,
  "executive_summary": "<2-4 sentence bottom line for a decision-maker>",
  "thesis_pillars": ["<up to 3 core catalysts, each grounded in a figure>", ...],
  "bull_case": ["<strongest point FOR, MUST include concrete numbers/figures from evidence (e.g., '$10B revenue', '+15% margin')>", ...],
  "bear_case": ["<strongest point AGAINST, MUST include concrete numbers/figures from evidence>", ...],
  "key_debates": [
    {"topic": "<contested point>",
     "positions_summary": "<the opposing views, briefly>",
     "winning_side": "<which analyst had stronger evidence, and why>",
     "resolution": "<your call on this point>"}
  ],
  "consensus_points": ["<where analysts independently agreed>", ...],
  "key_risks": ["<what would most threaten the thesis, MUST cite specific values (e.g., 'High cash burn of $500M in Q3')>", ...],
  "recommended_actions": ["<concrete next step or metric to monitor>", ...],
  "agents_considered": ["<agent ids whose reports you used>", ...],

  "business_model_and_segments": {
    "overview": "<how the company makes money, from the sec_filings/earnings_call reports>",
    "segments": [{"segment": "<name>", "revenue_contribution": "<e.g. '42% of revenue' or null>", "operating_profit_contribution": "<or null>", "commentary": "<...>"}],
    "unit_economics_note": "<margin/ARPU/take-rate detail if present, else empty>"
  },
  "industry_and_peer_positioning": {
    "market_structure": "<TAM/SAM/competitive landscape, from the reports>",
    "competitive_moat": "<from the peer_comparison agent's competitive_moat when it reported>",
    "peer_multiple_benchmark": "<peer_comparison's valuation_assessment + specific multiples, or empty if that agent did not report>"
  },
  "quality_of_earnings_forensic": {
    "summary": "<2-4 sentences synthesizing sec_filings.quality_of_earnings_forensic>",
    "depreciation_cliff_flagged": <bool, copy from sec_filings.quality_of_earnings_forensic.depreciation_cliff_detected>,
    "structural_vs_transitory_verdict": "<net read on the earnings trajectory>",
    "qoe_score": <int 0-100, copy from sec_filings.quality_of_earnings_forensic.qoe_score>
  },
  "valuation_thesis": {
    "peer_relative_read": "<from peer_comparison; empty if it did not report>",
    "scenarios": [{"scenario": "bull|base|bear", "assumption": "<...>", "implied_value": "<or null>", "basis": "<the multiple/figure used, required whenever implied_value is set>"}],
    "target_valuation_band": "<e.g. '$180-$210, based on peer EV/EBITDA range'; null if ungrounded>"
  },
  "key_risks_and_sensitivities": {
    "macro_sensitivity": "<from macro_market/macro_history>",
    "supply_chain_risk": "<or empty>",
    "regulatory_headwinds": "<or empty>",
    "sensitivities": [{"factor": "<e.g. 'USDKRW'>", "sensitivity": "<how the thesis responds>"}]
  }
}

ENUM FIELDS — copy one value EXACTLY, never prose:
- `recommendation`: bullish | neutral | bearish
- `conviction`: high | medium | low
- `valuation_thesis.scenarios[].scenario`: bull | base | bear

SCORING (overall_score, 0-100): 0-19 strongly bearish, 20-39 bearish, 40-59
balanced/mixed, 60-79 bullish, 80-100 strongly bullish. It must agree in
direction with `recommendation`.

HOW TO WEIGH:
- Give more weight to agents that cited hard, specific evidence (filing figures,
  dated guidance, price levels) and less to opinion-heavy or thin-coverage
  agents. Each agent's own `confidence` is a hint, not a mandate.
- Where the debate surfaced a genuine contradiction, adjudicate it in
  `key_debates`: name the side whose data was more concrete and say why. Do not
  split the difference just to appear balanced.
- `consensus_points` is for conclusions reached INDEPENDENTLY by multiple
  domains — convergence from separate evidence is the strongest signal you have.
- CONVICTION reflects agreement + evidence quality: broad, evidence-backed
  agreement → high; a close call or thin/conflicting inputs → low.

HOW TO FILL THE NEW CHAPTERS:
- `business_model_and_segments.segments` stays EMPTY unless an agent actually
  broke out segment/product/geography figures — do not invent a segment split.
- `industry_and_peer_positioning` and `valuation_thesis.peer_relative_read`
  depend on the `peer_comparison` agent's report. If it is not among
  `agents_considered`, say so plainly (e.g. "No peer comparison was available
  for this run") rather than leaving the field silently empty or guessing.
- `quality_of_earnings_forensic` MUST be consistent with the `sec_filings`
  agent's own `quality_of_earnings_forensic` block — you are restating it for
  a decision-maker, not re-deriving or disagreeing with it. If `sec_filings`
  did not report, say the QoE forensic read is unavailable and leave
  `qoe_score` at a neutral 50.
- `valuation_thesis.scenarios`: every `implied_value` MUST have a `basis`
  naming the exact multiple/figure it came from (e.g. a peer multiple times a
  reported EPS/EBITDA). If the reports don't support even a rough derivation
  for a scenario, OMIT that scenario entirely rather than inventing a number —
  it is fine to return 1 or 2 scenarios instead of 3, or none at all.
- `key_risks_and_sensitivities` draws on `macro_market`/`macro_history`
  (macro_sensitivity) and any supply-chain/regulatory content actually present
  in the reports; leave a sub-field empty (not fabricated) when nothing
  supports it.

RULES:
- STRICT QUANTITATIVE RULE: Never write vague, qualitative claims if numbers are available. E.g., DO NOT write "High cash burn". Instead, YOU MUST write "High cash burn ($500M operating loss in Q3, leaving only $200M in cash)". Include percentages, dollar amounts, multiples, etc. in `key_risks`, `bull_case`, and `bear_case`.
- Base every statement on the reports and the transcript. Never introduce facts
  or numbers that do not appear there. This applies with EQUAL force to every
  new chapter — an institutional-sounding sentence with no traceable figure
  behind it is still a fabrication.
- If an evidence domain is missing (an agent produced no report), acknowledge the
  gap rather than assuming it away, and temper conviction accordingly.
- `agents_considered` must list exactly the agent ids you were actually given.
"""

_USER_TEMPLATE = """\
Synthesize the team's work into a final investment view.

Analysis period: {period}
Company: {company}
Agents that produced a report: {agents}
Debate: {debate_meta}

=== INITIAL AGENT REPORTS (JSON) ===
{reports}
=== END REPORTS ===

=== DEBATE TRANSCRIPT ===
{transcript}
=== END TRANSCRIPT ===

Produce the JSON report now.
"""


class ManagerAgent(BaseAgent):
    """Synthesizes field reports + debate into a final recommendation."""

    @property
    def agent_id(self) -> str:
        return "manager"

    async def analyze(self, context: dict, capture: dict | None = None) -> ManagerReport:
        """
        Args:
            context: {
                "reports":   dict[str, dict],          # agent_id → initial report
                "transcript": DebateTranscript | None,  # Phase-2 debate (optional)
                "period":    str | None,
                "company":   str | None,
            }
        """
        reports: dict[str, dict] = context.get("reports") or {}
        transcript = context.get("transcript")
        period = context.get("period") or "(unspecified)"
        company = context.get("company") or "(unknown)"

        if not reports:
            raise RuntimeError("Manager synthesis requires at least one agent report.")

        agent_ids = list(reports.keys())
        reports_json = json.dumps(reports, ensure_ascii=False, indent=2)
        rendered = debate_mod.render_transcript(transcript)
        if transcript is not None and transcript.history:
            debate_meta = (
                f"{len(transcript.history)} arguments over {transcript.rounds} "
                f"round(s); consensus_reached={transcript.consensus_reached}"
            )
        else:
            debate_meta = "no debate was held (too few agents)"

        user_prompt = _USER_TEMPLATE.format(
            period=period,
            company=company,
            agents=", ".join(agent_ids),
            debate_meta=debate_meta,
            reports=reports_json,
            transcript=rendered,
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # Six chapters plus the original verdict fields runs long, and thinking
        # tokens draw from the same budget as the sec_filings agent's report —
        # too small a ceiling truncates the JSON mid-chapter.
        report = await self._generate_report(
            ManagerReport, _SYSTEM_PROMPT, user_prompt, max_output_tokens=16384,
        )

        # Ground bookkeeping in what actually fed the synthesis.
        if not report.agents_considered:
            report.agents_considered = agent_ids

        # The QoE synthesis chapter must never drift from the SEC agent's own
        # forensic read — overwrite the two fields that are pure bookkeeping
        # (not prose) with the ground truth, same enforcement pattern as
        # sec_filings_agent's accrual table.
        sec_qoe = (reports.get("sec_filings") or {}).get("quality_of_earnings_forensic")
        if sec_qoe:
            report.quality_of_earnings_forensic.depreciation_cliff_flagged = bool(
                sec_qoe.get("depreciation_cliff_detected", False)
            )
            if sec_qoe.get("qoe_score") is not None:
                report.quality_of_earnings_forensic.qoe_score = sec_qoe["qoe_score"]

        return report
