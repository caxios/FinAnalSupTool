"""
agents/debate.py
────────────────
True Sequential ("relay") debate among the field agents (Phase 2 of the MAS).

Phase 1 produces each agent's independent JSON report in parallel. This module
runs the SECOND phase: the agents speak ONE AFTER ANOTHER, each bringing its own
RAW DATA to the table and reacting to everything said before it. Because every
agent argues from primary evidence (filing numbers, transcript quotes, price
indicators, headlines) it can genuinely refute or reinforce another agent's
claim with specifics — not just restate its own summary.

Design notes
────────────
- SEQUENTIAL by construction: each turn's prompt embeds the transcript built by
  all prior turns, so turn N+1 truly reacts to turn N. This is the whole point,
  and it is why the debate takes 1-2 minutes.
- Each turn is a structured `AgentArgument` (validated + repaired via
  `llm_utils.generate_structured`, which also handles 429 backoff).
- An agent's raw data is CAPPED per turn (`_RAW_CAP`) so a long earnings
  transcript can't blow the request size; the agent's own initial report is
  included in full because it is already compact.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from .llm_utils import generate_structured

logger = logging.getLogger(__name__)


# =============================================================================
# Debate data structures
# =============================================================================

class AgentArgument(BaseModel):
    """One agent's contribution to one debate turn."""
    agent_id: str = Field(..., description="Speaking agent's id")
    stance: Literal["bullish", "bearish", "neutral"] = Field(
        ..., description="The agent's current stance on the investment case"
    )
    argument: str = Field(
        ..., description="The agent's contribution — reacts to prior turns"
    )
    cited_evidence: list[str] = Field(
        default_factory=list,
        description="Specific numbers/quotes from THIS agent's raw data",
    )


class DebateTranscript(BaseModel):
    """The full ordered record of the relay debate."""
    rounds: int = 0
    history: list[AgentArgument] = Field(default_factory=list)
    consensus_reached: bool = False


# =============================================================================
# Roster
# =============================================================================

# Speaking order for every debate round. Fundamentals lead, momentum closes.
DEBATE_ORDER: list[str] = [
    "sec_filings",
    "earnings_call",
    "company_news",
    "youtube_analysis",
    "macro_market",
    "technical_analysis",
]

# The six specialist "field" agents (everyone in the debate is a field agent;
# the Manager is a separate synthesizer and never debates).
FIELD_AGENT_IDS: frozenset[str] = frozenset(DEBATE_ORDER)

AGENT_DISPLAY_NAMES: dict[str, str] = {
    "sec_filings": "SEC Filings Analyst",
    "earnings_call": "Earnings Call Analyst",
    "company_news": "Company News Analyst",
    "youtube_analysis": "Analyst-Commentary (YouTube) Analyst",
    "macro_market": "Macro & Market Analyst",
    "technical_analysis": "Technical (Price) Analyst",
    "macro_history": "Macro History Analyst",
    "manager": "Lead Analyst (Manager)",
}


def display_name(agent_id: str) -> str:
    return AGENT_DISPLAY_NAMES.get(agent_id, agent_id)


# Per-turn cap on an agent's raw data. The debate is expensive by design
# (~1M tokens/run is acceptable on Gemini Flash), but a single uncapped earnings
# transcript (~80K chars) sent on every one of its turns is pure waste — the
# agent has already distilled its report, and this slice is only to let it quote
# specifics. 30K chars keeps prepared remarks + Q&A within reach.
_RAW_CAP = 30_000
_DEFAULT_ROUNDS = 2
_TURN_MAX_TOKENS = 2048


# =============================================================================
# Transcript rendering
# =============================================================================

def render_transcript(
    transcript: DebateTranscript | None, *, include_evidence: bool = True
) -> str:
    """Render the debate so far as readable text for a prompt (or chat context)."""
    if transcript is None or not transcript.history:
        return "(The debate has not started — you are opening it.)"

    lines: list[str] = []
    for i, arg in enumerate(transcript.history, 1):
        lines.append(
            f"[{i}] {display_name(arg.agent_id)} — stance: {arg.stance}"
        )
        lines.append(arg.argument.strip())
        if include_evidence and arg.cited_evidence:
            for ev in arg.cited_evidence:
                lines.append(f"    • {ev}")
        lines.append("")
    return "\n".join(lines).strip()


# =============================================================================
# Prompting
# =============================================================================

_SYSTEM_TEMPLATE = """\
You are the {name}, one of six specialist analysts in a round-table investment
debate. Each analyst has their OWN raw data and initial findings; the others
have data you cannot see and you cannot see theirs. The only shared record is
the running debate transcript.

Your task on THIS turn: contribute exactly one argument that MOVES the debate
forward. Specifically:
- React to what earlier speakers claimed. If another analyst's claim CONTRADICTS
  your raw data, refute it explicitly by quoting the specific number or line from
  YOUR data that disproves it. Name the analyst you are rebutting.
- If you AGREE with a prior point, reinforce it with fresh evidence from your own
  data rather than merely repeating it.
- Stay strictly within your domain and your data. Do NOT invent figures, and do
  NOT speak to evidence you were not given. If your data cannot address a point,
  say so plainly instead of guessing.
- Take a clear `stance` (bullish / bearish / neutral) that reflects what YOUR
  evidence supports — do not soften it just to seem agreeable.

Output ONLY a single JSON object:
{{
  "agent_id": "{agent_id}",
  "stance": "bullish|bearish|neutral",
  "argument": "<3-6 sentences: your reaction + your point, citing specifics>",
  "cited_evidence": ["<exact number/quote from YOUR raw data>", "..."]
}}

`cited_evidence` must contain 1-4 concrete items copied or computed from your raw
data (e.g. \"Gross margin fell 62.4% → 59.1% over three quarters\"), not vague
paraphrases. Return ONLY the JSON, no prose, no code fences.
"""

_USER_TEMPLATE = """\
This is debate round {round_num} of {total_rounds}. You are the {name}.

=== YOUR INITIAL FINDINGS (your own Phase-1 JSON report) ===
{report}
=== END YOUR FINDINGS ===

=== YOUR RAW DATA (primary evidence — quote from this) ===
{raw_data}
=== END YOUR RAW DATA ===

=== DEBATE TRANSCRIPT SO FAR ===
{transcript}
=== END TRANSCRIPT ===

Formulate your argument for this turn now.
"""


def build_debate_prompt(
    agent_id: str,
    agent_ctx: dict,
    transcript: DebateTranscript,
    round_num: int,
    total_rounds: int,
) -> tuple[str, str]:
    """
    Build (system_prompt, user_prompt) for one agent's debate turn.

    `agent_ctx` is `{"raw_data": str, "report": dict}` captured in Phase 1.
    """
    import json

    name = display_name(agent_id)
    raw = (agent_ctx.get("raw_data") or "").strip()
    if len(raw) > _RAW_CAP:
        raw = raw[:_RAW_CAP] + "\n…[raw data truncated for the debate]…"
    if not raw:
        raw = "(No raw data was captured; argue from your initial findings only.)"

    report = agent_ctx.get("report") or {}
    report_json = json.dumps(report, ensure_ascii=False, indent=2)

    system = _SYSTEM_TEMPLATE.format(name=name, agent_id=agent_id)
    user = _USER_TEMPLATE.format(
        round_num=round_num,
        total_rounds=total_rounds,
        name=name,
        report=report_json,
        raw_data=raw,
        transcript=render_transcript(transcript),
    )
    return system, user


# =============================================================================
# Consensus detection
# =============================================================================

def _detect_consensus(transcript: DebateTranscript, participants: int) -> bool:
    """
    Consensus = the LAST round's stances broadly agree.

    We look only at the final round (the debate's settled state) and call it a
    consensus when a single stance holds at least two-thirds of that round's
    speakers. A two-sided final round is explicitly NOT consensus.
    """
    if participants <= 0 or not transcript.history:
        return False
    final_round = transcript.history[-participants:]
    if not final_round:
        return False
    counts: dict[str, int] = {}
    for arg in final_round:
        counts[arg.stance] = counts.get(arg.stance, 0) + 1
    top = max(counts.values())
    return top / len(final_round) >= 2 / 3


# =============================================================================
# Sequential debate runner
# =============================================================================

async def run_sequential_debate(
    agent_contexts: dict[str, dict], *, rounds: int = _DEFAULT_ROUNDS
) -> DebateTranscript:
    """
    Run the relay debate over the agents that produced a report in Phase 1.

    `agent_contexts` maps agent_id → {"raw_data": str, "report": dict}. Only
    agents present here participate; the speaking order follows `DEBATE_ORDER`.

    Turns are SEQUENTIAL (awaited one at a time) so each speaker sees the growing
    transcript. A single turn that fails validation/rate-limits is logged and
    skipped — the debate continues rather than aborting.
    """
    transcript = DebateTranscript(rounds=0, history=[], consensus_reached=False)
    order = [aid for aid in DEBATE_ORDER if aid in agent_contexts]
    participants = len(order)
    if participants < 2:
        logger.info("Debate skipped: fewer than 2 agents produced a report.")
        return transcript

    for r in range(1, rounds + 1):
        for agent_id in order:
            system, user = build_debate_prompt(
                agent_id, agent_contexts[agent_id], transcript, r, rounds
            )
            try:
                arg = await generate_structured(
                    system, user, AgentArgument,
                    temperature=0.4, max_output_tokens=_TURN_MAX_TOKENS,
                )
            except Exception as e:  # noqa: BLE001 — one bad turn must not kill the debate
                logger.warning(f"Debate turn failed ({agent_id}, round {r}): {e}")
                continue
            arg.agent_id = agent_id  # trust the roster, not the model's echo
            transcript.history.append(arg)
        transcript.rounds = r

    transcript.consensus_reached = _detect_consensus(transcript, participants)
    logger.info(
        f"Debate complete: {len(transcript.history)} arguments over "
        f"{transcript.rounds} round(s), consensus={transcript.consensus_reached}."
    )
    return transcript
