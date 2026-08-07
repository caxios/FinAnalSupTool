"""
agents/technical_analysis_agent.py
──────────────────────────────────
Technical Analysis (Price Trend) Agent — the second MAS agent.

It fetches PRE-COMPUTED technical indicators from `price_provider` (all math is
done in pandas) and asks the LLM only to INTERPRET them: identify the trend and
its strength, read the momentum indicators, spot chart patterns and divergences,
and score the technical outlook. The agent never asks the LLM to recompute
numbers.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

import price_provider

from .base_agent import BaseAgent
from .schemas.technical_analysis import TechnicalAnalysisReport

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a technical analysis specialist in a multi-agent financial analysis
system. You are given PRE-COMPUTED technical indicators for a stock. Do NOT
attempt to recalculate them — interpret the values exactly as given.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of the technical read>",
  "ticker": "<symbol>",
  "analysis_period": "<start..end>",
  "current_price": <float>,
  "trend_assessment": {
    "primary_trend": "uptrend|downtrend|sideways",
    "strength": "weak|moderate|strong",
    "trend_score": <int 0-100>,
    "detail": "<what the moving averages and price imply>"
  },
  "momentum_indicators": {
    "rsi": "<interpretation incl. the value, e.g. 'Neutral (40): neither overbought nor oversold'>",
    "macd": "<interpretation, e.g. 'Bearish: MACD below signal, negative histogram'>",
    "volume": "<interpretation, e.g. 'Average: ~1.0x the 20-day average'>",
    "divergence": "<notable divergence or null>"
  },
  "key_levels": {
    "support": [<float>, ...],
    "resistance": [<float>, ...],
    "note": "<how price sits relative to these levels>"
  },
  "pattern_recognition": {
    "pattern": "<chart pattern or 'none'>",
    "implication": "<bullish|bearish|neutral implication>",
    "reliability": "low|medium|high"
  },
  "price_vs_fundamentals": {
    "period_return": "<e.g. '+16.3%'>",
    "assessment": "<is the move momentum-driven / stretched / orderly?>",
    "note": "<state that fundamental reconciliation is done by the aggregator>"
  },
  "data_warning": "<warning if history is insufficient, else null>"
}

TREND_SCORE RUBRIC (0-100):
- 80-100 = Strong uptrend: price above SMA50 which is above SMA200 (golden
           cross), RSI ~50-70, positive MACD, at/above average volume.
- 60-79  = Moderate uptrend, or consolidation with a bullish bias.
- 40-59  = Sideways / no clear trend.
- 20-39  = Moderate downtrend, or consolidation with a bearish bias.
- 0-19   = Strong downtrend: price below SMA50 and SMA200, weak RSI, negative
           MACD.

RULES:
- Interpret ONLY the provided values. Never invent indicator numbers.
- Pattern recognition is PROBABILISTIC — always set a realistic `reliability`
  and never state a pattern as certain.
- Note divergences when present (e.g. price rising while volume falls, or price
  making highs while RSI weakens).
- You do NOT have fundamental data. In `price_vs_fundamentals`, characterize the
  price move and explicitly note that reconciliation with fundamentals is done
  by the aggregator, not here.
- DATA QUALITY: if `has_full_history` is false or `data_points` is small, SMA200
  (and long-term trend reads) are unreliable — lower your `confidence` and set
  `data_warning` accordingly. Full history with all indicators present →
  higher confidence (~0.8+). Short window → lower (~0.4-0.6).
"""

_USER_TEMPLATE = """\
Interpret the following PRE-COMPUTED technical indicators and produce the JSON
report. All numbers are already calculated — do not recompute them.

=== TECHNICAL DATA ({ticker}) ===
{data}
=== END TECHNICAL DATA ===
"""


def _format_data(td: price_provider.TechnicalData) -> str:
    """Render the TechnicalData dataclass as readable key: value lines."""
    d = asdict(td)
    # Keep the long monthly series compact but present (for trend context).
    monthly = d.pop("monthly_closes", [])
    lines = [f"{k}: {v}" for k, v in d.items()]
    if monthly:
        series = ", ".join(f"{m['month']}={m['close']}" for m in monthly)
        lines.append(f"monthly_closes: {series}")
    return "\n".join(lines)


class TechnicalAnalysisAgent(BaseAgent):
    """Interprets pre-computed price/volume indicators into a technical report."""

    @property
    def agent_id(self) -> str:
        return "technical_analysis"

    async def analyze(self, context: dict, capture: dict | None = None) -> TechnicalAnalysisReport:
        """
        Args:
            context: {"ticker": str, "start_date": "YYYY-MM-DD",
                      "end_date": "YYYY-MM-DD"}
            capture: optional side-channel for the assembled raw-data prompt.
        """
        ticker = (context.get("ticker") or "").strip().upper()
        start_date = context["start_date"]
        end_date = context["end_date"]
        if not ticker:
            raise RuntimeError("Technical analysis requires a ticker symbol.")

        # Fetch + compute indicators (raises ValueError for bad ticker/empty data).
        td = await price_provider.fetch_technical_data(ticker, start_date, end_date)

        user_prompt = _USER_TEMPLATE.format(ticker=ticker, data=_format_data(td))
        if capture is not None:
            capture["raw_data"] = user_prompt
        # This report is compact, but Gemini's thinking tokens draw from the same
        # budget as the answer — keep headroom so the JSON can't be truncated.
        report = await self._generate_report(
            TechnicalAnalysisReport, _SYSTEM_PROMPT, user_prompt,
            max_output_tokens=16384,
        )

        # Overwrite the factual fields with the ground-truth computed values, so
        # they can never drift from what price_provider actually returned.
        report.ticker = td.ticker
        report.analysis_period = f"{td.period_start}..{td.period_end}"
        report.current_price = td.current_price
        if not td.has_full_history and not report.data_warning:
            report.data_warning = (
                f"Only {td.data_points} trading days available; SMA200 and "
                f"long-term trend signals are unreliable."
            )
        return report
