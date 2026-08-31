"""
agents/coach_agent.py
─────────────────────
Adaptive Trading Coach Agent — blueprint §3.

A meta-cognitive coach: it holds the user's stated **Entry Rationale** against
what the objective data actually said, names the psychological bias when those
two disagree, and cites the user's OWN past trades as evidence.

Three pillars, all of which already existed before this agent
────────────────────────────────────────────────────────────
  1. Fundamental — the SEC Filings agent's report from the last /analyze run.
  2. Technical   — the Technical Analysis agent's report from that same run.
  3. Behavioural — ``services.journal_analysis``, which joins every logged trade
                   to what the price did afterwards.

This agent adds no new data source. It is pure synthesis, which is why its one
real risk is fabrication: a coaching claim about "your last three trades" is
worthless unless those trades exist. So the journal statistics are computed in
Python and handed over as structure, the prompt forbids inventing a date, and
:func:`verify_citations` checks every date the model returns against the real
journal before the report is served.

Debate participation
────────────────────
Like ``QuantRiskAgent``, this agent does NOT join the round-table debate. It
does not analyze a security at all — it analyzes the *user*, and it runs on
demand from the Portfolio view rather than as part of the /analyze pipeline.
"""

from __future__ import annotations

import json
import logging
import re

from services import journal_analysis

from .base_agent import BaseAgent
from .schemas.coach import CoachReport

logger = logging.getLogger(__name__)


# How many recent journal entries to show the model. Enough to establish a
# pattern; bounded so a long history doesn't crowd out the reports.
_MAX_JOURNAL_ROWS = 25


_SYSTEM_PROMPT = """\
You are a trading coach in a financial analysis system. Your job is NOT to pick
stocks — it is to help this user see their own decision-making clearly.

You are given four things:
  1. The trade the user is considering, and THEIR OWN stated reason for it.
  2. The fundamental analyst's report on the company (if available).
  3. The technical analyst's report on the company (if available).
  4. The user's real trading journal: past trades, what they wrote at the time,
     and what the price actually did 7/30/90 days later.

ABSOLUTE RULES — violating these makes your advice harmful:
- NEVER invent a past trade. Every date you cite in `past_occurrences` MUST
  appear in the journal you were given. If the journal is empty or too short,
  say so plainly and leave `past_occurrences` empty.
- NEVER invent a number. Cite only figures present in the reports or the journal.
- If `history_sufficient` is false in the data, you MUST say the history is too
  short to establish a pattern, and set `historical_pattern` to null. Do not
  generalize from two or three trades — the user may act on what you say.
- The rationale-type labels ("emotional"/"analytical") come from a crude keyword
  match, not a psychological assessment. Treat them as a weak hint, not a fact
  about the user.

WHAT TO DO:
- Compare the user's stated rationale against the objective reports. Name the
  conflict EXPLICITLY when they disagree. For example: "You're selling because
  the technicals broke, but the fundamental report shows revenue up 20% and
  margins expanding — those are different time horizons, and your reason only
  addresses one of them."
- When the journal supports it, connect this decision to the user's own history:
  "The last two times you wrote something like this (2026-03-14, 2026-05-02),
  the position was higher 30 days later."
- Detect biases only when you can evidence them: FOMO, panic selling, loss
  aversion, anchoring, revenge trading, recency bias.
- Set `alignment_score`: 100 = the rationale is fully consistent with the
  objective data, 0 = it directly contradicts it.
- Be DIRECT but not moralizing. You are a coach, not a scold. Do not lecture
  about discipline in the abstract; point at the specific decision in front of
  you. A good rationale deserves to be told it is good.

Output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentences on how you reached this assessment>",
  "rationale_evaluation": "<the user's logic vs. the objective data>",
  "detected_biases": [
    {"bias": "<name>", "evidence": "<quote their words>",
     "past_occurrences": ["YYYY-MM-DD", ...], "severity": "mild|moderate|strong"}
  ],
  "historical_pattern": "<what their own history shows, or null>",
  "coaching_feedback": "<direct, actionable guidance>",
  "alignment_score": <int 0-100>,
  "supporting_data_points": ["<specific figure you used>", ...],
  "data_limitations": ["<what you could not see>", ...]
}
"""


_USER_TEMPLATE = """\
=== THE TRADE UNDER REVIEW ===
{proposed}
=== END TRADE ===

=== THE USER'S STATED ENTRY RATIONALE ===
{rationale}
=== END RATIONALE ===

=== FUNDAMENTAL ANALYST REPORT ===
{fundamental}
=== END FUNDAMENTAL ===

=== TECHNICAL ANALYST REPORT ===
{technical}
=== END TECHNICAL ===

=== THE USER'S TRADING JOURNAL (real logged trades) ===
{journal}
=== END JOURNAL ===

=== BEHAVIOURAL SUMMARY (computed, not estimated) ===
{patterns}
=== END SUMMARY ===

Review this decision.
"""


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def verify_citations(report: CoachReport, journal: list[dict]) -> list[str]:
    """
    Strip any cited date that does not correspond to a real journal entry.

    The prompt forbids inventing dates, but "the prompt says not to" is not a
    guarantee — and a fabricated "you did this on 2026-03-14" is exactly the
    failure that would destroy the user's trust in the coach. Returns the list
    of dates that were removed, so the caller can log or surface them.
    """
    real_dates = {
        (t.get("executed_at") or "")[:10]
        for t in journal
        if t.get("executed_at")
    }
    removed: list[str] = []

    for bias in report.detected_biases:
        kept = []
        for d in bias.past_occurrences:
            day = d.strip()[:10]
            if _DATE_RE.fullmatch(day) and day in real_dates:
                kept.append(day)
            else:
                removed.append(d)
        bias.past_occurrences = kept

    if removed:
        logger.warning(
            f"[coach] dropped {len(removed)} fabricated trade date(s): {removed}"
        )
        report.data_limitations.append(
            "Some cited past trades did not match the journal and were removed."
        )
    return removed


def _compact_report(report: dict | None, keys: tuple[str, ...]) -> str:
    """Pull just the decision-relevant fields out of a full agent report."""
    if not report:
        return "(Not available — no analysis has been run for this company yet.)"
    kept = {k: report.get(k) for k in keys if report.get(k) is not None}
    if not kept:
        return "(Report contained no usable fields.)"
    return json.dumps(kept, ensure_ascii=False, indent=2, default=str)


class CoachAgent(BaseAgent):
    """Evaluates the user's reasoning against objective data and their history."""

    @property
    def agent_id(self) -> str:
        return "trading_coach"

    async def analyze(self, context: dict, capture: dict | None = None) -> CoachReport:
        """
        Args:
            context: ``ticker``, ``entry_rationale``, optional ``proposed_side`` /
                     ``proposed_quantity``, and optional ``sec_report`` /
                     ``technical_report`` dicts from the last /analyze run.
        """
        ticker = (context.get("ticker") or "").strip().upper() or None
        rationale = (context.get("entry_rationale") or "").strip()
        side = context.get("proposed_side")
        qty = context.get("proposed_quantity")

        proposed = " ".join(
            str(x) for x in [side, qty, ticker] if x not in (None, "")
        ) or "(no specific trade — general review)"

        # ── The behavioural pillar, computed in Python. ──
        journal = await journal_analysis.trade_outcomes(limit=_MAX_JOURNAL_ROWS)
        patterns = await journal_analysis.pattern_summary()
        real_entries = [t for t in journal if not t.get("is_opening_entry")]

        journal_text = (
            json.dumps(journal[:_MAX_JOURNAL_ROWS], ensure_ascii=False,
                       indent=2, default=str)
            if journal else
            "(The journal is EMPTY — this user has not logged any trades yet. "
            "You must not cite any past trade.)"
        )

        fundamental = _compact_report(
            context.get("sec_report"),
            ("confidence", "reasoning", "financial_health", "key_findings",
             "revenue_trend", "profitability", "risks", "fundamental_score",
             "overall_assessment"),
        )
        technical = _compact_report(
            context.get("technical_report"),
            ("confidence", "reasoning", "current_price", "trend_assessment",
             "momentum_indicators", "key_levels", "pattern_recognition",
             "technical_score", "price_vs_fundamentals"),
        )

        user_prompt = _USER_TEMPLATE.format(
            proposed=proposed,
            rationale=rationale or "(The user gave no rationale for this trade.)",
            fundamental=fundamental,
            technical=technical,
            journal=journal_text,
            patterns=json.dumps(patterns, ensure_ascii=False, indent=2, default=str),
        )

        if capture is not None:
            capture["raw_data"] = user_prompt

        report = await self._generate_report(CoachReport, _SYSTEM_PROMPT, user_prompt)

        # ── Enforce what the prompt only asked for. ──
        verify_citations(report, real_entries)

        report.ticker = ticker
        report.proposed_action = proposed
        report.history_sufficient = bool(patterns.get("sufficient"))
        if not report.history_sufficient:
            # The model is told to do this, but the guarantee shouldn't depend on
            # it complying — an invented pattern is the failure that matters most.
            report.historical_pattern = None
            note = patterns.get("note") or "Too few logged trades to establish a pattern."
            if note not in report.data_limitations:
                report.data_limitations.append(note)
        return report
