"""
agents/macro_history_agent.py
─────────────────────────────
Macro History Teller Agent.

"History doesn't repeat, but it rhymes." — Mark Twain

Finds historical periods whose macroeconomic conditions most closely mirror the
present, with a sharp focus on what happened to the TARGET COMPANY's sector
during those periods.  Uses a two-step approach:

  Step 1 — CURRENT DIAGNOSIS + ANALOGUE DISCOVERY
    Feed the LLM the current macro indicators (real FRED/yfinance data) and the
    target company's sector.  Ask it to identify 1-3 historical periods that
    rhyme with today's conditions, especially for this sector.

  Step 2 — HISTORICAL VALIDATION + FINAL REPORT
    For each analogue the LLM proposed in Step 1, pull the ACTUAL historical
    macro data from FRED/yfinance for that period.  Optionally search for
    historical news context via Tavily.  Re-prompt the LLM with this grounded
    evidence to produce the final, data-backed report.

This agent does NOT participate in the round-table debate — it produces an
independent advisory report that the Manager sees alongside the debate.
"""

from __future__ import annotations

import asyncio
import logging
import re

from providers import news_provider
from providers.macro_data_provider import (
    fetch_macro_indicators,
    fetch_historical_indicators,
    format_indicators_for_llm,
)

from .base_agent import BaseAgent
from .schemas.macro_history import MacroHistoryReport

logger = logging.getLogger(__name__)


# Maximum number of analogues the LLM should propose in Step 1.
_MAX_ANALOGUES = 3

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 prompt: discover candidate analogues
# ─────────────────────────────────────────────────────────────────────────────

_STEP1_SYSTEM = """\
You are a Financial Historian specializing in macroeconomic regime analysis and
cross-cycle sector comparison. You are given REAL, current macroeconomic
indicator data and a target company's sector.

YOUR TASK (Step 1 of 2):
Analyze the current macro regime, then identify 1-3 historical periods that
most closely mirror today's conditions — with SPECIAL EMPHASIS on what happened
to the TARGET SECTOR during those periods.

CRITICAL RULES:
- DO NOT just cite broad, well-known events (e.g. "2008 Financial Crisis") unless
  the current data pattern genuinely resembles that period's MACRO INDICATORS.
- Focus on the SPECIFIC combination of indicators: rate levels, inflation
  trajectory, employment, yield curve shape, and volatility.
- For EACH analogue, you MUST specify the sector-specific angle: what happened to
  companies in the same industry as the target during that period?
- Be precise with date ranges (YYYY-MM format). The date ranges must be real
  historical periods that you are confident about.

Output ONLY a single JSON object:
{
  "current_regime_summary": "<2-4 sentences on today's macro environment>",
  "target_sector": "<the inferred sector>",
  "current_snapshot": {"CPI_YoY": <num>, "Unemployment": <num>, ...},
  "candidate_analogues": [
    {
      "period_start": "YYYY-MM",
      "period_end": "YYYY-MM",
      "title": "<descriptive title>",
      "rationale": "<why this period's indicators match today>",
      "sector_angle": "<what happened to the target sector then>"
    }
  ]
}

`candidate_analogues` must have 1-3 entries, ordered by relevance.
`current_snapshot` should include the latest value for each indicator you were given.
"""

_STEP1_USER = """\
Target company: {company}
Target sector (inferred from the company): please identify the sector yourself.
Analysis period: {start_date} to {end_date}

=== CURRENT MACROECONOMIC INDICATORS (real data) ===
{indicators}
=== END INDICATORS ===

Identify the historical analogues now.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 prompt: validate with real historical data + produce final report
# ─────────────────────────────────────────────────────────────────────────────

_STEP2_SYSTEM = """\
You are the Macro History Teller, a specialist agent in a multi-agent financial
analysis system. In Step 1, you identified candidate historical analogues. Now
you have been given the ACTUAL historical macroeconomic data for those periods,
plus any additional news context that was found.

YOUR TASK (Step 2 of 2 — FINAL REPORT):
Validate your initial analogues against the real data and produce your final
structured report. You must:

1. CONFIRM or ADJUST each analogue based on the real historical data. If the
   actual numbers don't support an analogue, downgrade its similarity_score or
   drop it entirely.
2. For each confirmed analogue, provide the SECTOR-SPECIFIC outcome: what
   actually happened to companies in the target sector during that period? This
   is the most valuable part of your analysis.
3. Derive 2-4 forward-looking probability scenarios based on the analogues.
4. Be honest about limitations — if historical data was unavailable for a
   period, say so.

Output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentences: how you reached your conclusions>",
  "analysis_period": "<YYYY-MM-DD..YYYY-MM-DD>",
  "current_regime_summary": "<2-4 sentence macro diagnosis>",
  "target_sector": "<sector>",
  "current_indicators_snapshot": {"CPI_YoY": <num>, ...},
  "analogues": [
    {
      "period": "<YYYY-MM ~ YYYY-MM>",
      "title": "<title>",
      "similarity_score": <int 0-100>,
      "similarity_factors": ["<specific indicator match, citing real numbers>", ...],
      "differences": ["<key difference from today>", ...],
      "market_outcome": "<what happened to the broad market>",
      "sector_specific_outcome": "<what happened to the TARGET SECTOR — MOST IMPORTANT>",
      "key_events": ["<event>", ...],
      "lesson_for_today": "<concrete takeaway>"
    }
  ],
  "primary_analogue": "<title of the most relevant analogue>",
  "sector_historical_context": "<paragraph: how this sector behaves across cycles>",
  "probability_scenarios": [
    {
      "scenario": "<title>",
      "probability": "high|medium|low",
      "description": "<what this looks like>",
      "sector_implication": "<what it means for the target sector>"
    }
  ],
  "data_limitations": ["<caveat>", ...]
}

CONFIDENCE:
- 0.8-1.0: Multiple analogues confirmed by real data with clear sector parallels.
- 0.5-0.7: Analogues partially confirmed; some data gaps or weak sector match.
- 0.2-0.4: Limited historical data; analogues are speculative.

RULES:
- EVERY number you cite must come from the DATA sections. Do NOT invent
  historical indicator values.
- `sector_specific_outcome` in each analogue is REQUIRED and must reference the
  target company's industry, not just "the stock market."
- `analogues` must be sorted by `similarity_score` descending.
"""

_STEP2_USER = """\
Target company: {company}
Target sector: {sector}
Analysis period: {start_date} to {end_date}

=== YOUR STEP-1 ANALYSIS (candidate analogues) ===
{step1_result}
=== END STEP-1 ===

=== CURRENT MACRO INDICATORS (real data) ===
{current_indicators}
=== END CURRENT ===

{historical_sections}

{news_context}

Produce the final validated report now.
"""


def _parse_period(text: str) -> tuple[str, str] | None:
    """
    Extract a YYYY-MM start and end from the LLM's candidate analogue.
    Handles formats like "1994-02" to "1995-06", "1994-02 ~ 1995-06", etc.
    Returns (start_iso, end_iso) as YYYY-MM-DD strings, or None.
    """
    matches = re.findall(r"(\d{4})-(\d{2})", text)
    if len(matches) >= 2:
        s = f"{matches[0][0]}-{matches[0][1]}-01"
        e = f"{matches[-1][0]}-{matches[-1][1]}-28"  # safe end-of-month
        return s, e
    if len(matches) == 1:
        s = f"{matches[0][0]}-{matches[0][1]}-01"
        e = f"{matches[0][0]}-{matches[0][1]}-28"
        return s, e
    return None


class MacroHistoryAgent(BaseAgent):
    """
    Macro History Teller — finds historical analogues for the current macro
    environment, with sector-specific analysis grounded in real data.

    Does NOT participate in the debate; produces an independent advisory report.
    """

    @property
    def agent_id(self) -> str:
        return "macro_history"

    async def analyze(self, context: dict, capture: dict | None = None) -> MacroHistoryReport:
        """
        Two-step analysis:
          1. Feed current indicators → LLM identifies candidate analogues.
          2. Fetch real historical data for those periods → LLM validates and
             produces the final grounded report.

        Args:
            context: {"company": str, "ticker": str | None,
                      "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}
            capture: optional side-channel for the assembled raw-data prompt.
        """
        company = (context.get("company") or "").strip()
        ticker = (context.get("ticker") or "").strip() or None
        start_date = context["start_date"]
        end_date = context["end_date"]
        label = company or ticker or "unknown"

        # ── Fetch current macro indicators ──
        current_data = await fetch_macro_indicators(start_date, end_date)
        current_text = format_indicators_for_llm(current_data)

        # ── Step 1: Discover candidate analogues ──
        step1_user = _STEP1_USER.format(
            company=label,
            start_date=start_date,
            end_date=end_date,
            indicators=current_text,
        )

        step1_raw = await self._call_llm(_STEP1_SYSTEM, step1_user)

        # Parse the Step 1 output to extract candidate periods.
        import json
        try:
            # Strip markdown code fences if present.
            cleaned = step1_raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
            start_idx = cleaned.find("{")
            end_idx = cleaned.rfind("}")
            if start_idx >= 0 and end_idx > start_idx:
                cleaned = cleaned[start_idx:end_idx + 1]
            step1 = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Step 1 JSON parse failed, using raw output as context")
            step1 = {"raw": step1_raw}

        candidates = step1.get("candidate_analogues", [])[:_MAX_ANALOGUES]
        sector = step1.get("target_sector", "unknown")

        # ── Step 2: Fetch historical data for each candidate ──
        historical_sections: list[str] = []

        async def _fetch_historical(candidate: dict) -> str | None:
            """Fetch real FRED/yfinance data for one candidate period."""
            period_str = (
                f"{candidate.get('period_start', '')} ~ "
                f"{candidate.get('period_end', '')}"
            )
            parsed = _parse_period(period_str)
            if not parsed:
                return None
            hist_start, hist_end = parsed
            try:
                hist_data = await fetch_historical_indicators(hist_start, hist_end)
                text = format_indicators_for_llm(hist_data)
                title = candidate.get("title", period_str)
                return (
                    f"=== HISTORICAL DATA: {title} ({hist_start} → {hist_end}) ===\n"
                    f"{text}\n"
                    f"=== END {title} ==="
                )
            except Exception as e:
                logger.warning(f"Historical data fetch failed for {period_str}: {e}")
                return f"(Historical data for {period_str} was not available: {e})"

        # Fetch all historical periods concurrently.
        if candidates:
            results = await asyncio.gather(
                *(_fetch_historical(c) for c in candidates),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, str):
                    historical_sections.append(r)
                elif isinstance(r, Exception):
                    historical_sections.append(f"(Historical fetch error: {r})")

        # ── Optional: search for historical news context ──
        news_parts: list[str] = []
        for candidate in candidates[:2]:  # Limit searches to top 2
            title = candidate.get("title", "")
            if title:
                try:
                    news_result = await news_provider.search_company_news(
                        f"{title} {sector} market impact historical",
                        max_results=5,
                    )
                    if news_result.configured and news_result.articles:
                        news_parts.append(f"--- Historical context: {title} ---")
                        for a in news_result.articles[:3]:
                            news_parts.append(f"[{a.source}] {a.title}: {a.snippet[:200]}")
                except Exception as e:
                    logger.warning(f"Historical news search failed for '{title}': {e}")

        news_context = (
            "=== HISTORICAL NEWS CONTEXT (web search) ===\n"
            + "\n".join(news_parts)
            + "\n=== END HISTORICAL NEWS ==="
        ) if news_parts else "(No historical news context was retrieved.)"

        # ── Step 2: Final validated report ──
        step2_user = _STEP2_USER.format(
            company=label,
            sector=sector,
            start_date=start_date,
            end_date=end_date,
            step1_result=json.dumps(step1, ensure_ascii=False, indent=2),
            current_indicators=current_text,
            historical_sections="\n\n".join(historical_sections) or "(No historical data fetched.)",
            news_context=news_context,
        )

        if capture is not None:
            capture["raw_data"] = (
                f"=== STEP 1 RESULT ===\n{step1_raw}\n=== END STEP 1 ===\n\n"
                f"=== CURRENT INDICATORS ===\n{current_text}\n"
                f"=== END CURRENT ===\n\n"
                + "\n\n".join(historical_sections)
            )

        # The final report can be lengthy (multiple analogues + scenarios),
        # so we give it a generous token budget.
        report = await self._generate_report(
            MacroHistoryReport, _STEP2_SYSTEM, step2_user,
            max_output_tokens=16384,
        )

        # Ensure the analysis_period is populated.
        if not report.analysis_period:
            report.analysis_period = f"{start_date}..{end_date}"

        return report
