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

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentences on HOW you weighed the agents to reach the call>",
  "recommendation": "bullish|neutral|bearish",
  "conviction": "high|medium|low",
  "overall_score": <int 0-100>,
  "executive_summary": "<2-4 sentence bottom line for a decision-maker>",
  "bull_case": ["<strongest point FOR, attributed to the evidence>", ...],
  "bear_case": ["<strongest point AGAINST, attributed to the evidence>", ...],
  "key_debates": [
    {"topic": "<contested point>",
     "positions_summary": "<the opposing views, briefly>",
     "winning_side": "<which analyst had stronger evidence, and why>",
     "resolution": "<your call on this point>"}
  ],
  "consensus_points": ["<where analysts independently agreed>", ...],
  "key_risks": ["<what would most threaten the thesis>", ...],
  "recommended_actions": ["<concrete next step or metric to monitor>", ...],
  "agents_considered": ["<agent ids whose reports you used>", ...]
}

ENUM FIELDS — copy one value EXACTLY, never prose:
- `recommendation`: bullish | neutral | bearish
- `conviction`: high | medium | low

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

RULES:
- Base every statement on the reports and the transcript. Never introduce facts
  or numbers that do not appear there.
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

        report = await self._generate_report(
            ManagerReport, _SYSTEM_PROMPT, user_prompt, max_output_tokens=8192,
        )

        # Ground bookkeeping in what actually fed the synthesis.
        if not report.agents_considered:
            report.agents_considered = agent_ids
        return report
