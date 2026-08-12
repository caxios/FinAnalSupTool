"""
agents/macro_market_agent.py
────────────────────────────
Macro & Market News Analyzer Agent.

Fetches broad market/macroeconomic news across the analysis period (month by
month, so the regime can be tracked as it shifts) and optionally the current
market-sentiment snapshot, then asks the LLM to translate that environment into
concrete consequences for the target company's sector.

The macro feed is company-agnostic; the company name/ticker is passed only so
the agent can infer the sector and make the read specific rather than generic.
"""

from __future__ import annotations

import asyncio
import logging

from providers import news_provider, price_provider
from market_sentiment import compute_market_sentiment

from .base_agent import BaseAgent
from .date_windows import month_windows
from .schemas.macro_market import MacroMarketReport

logger = logging.getLogger(__name__)

_PER_WINDOW_RESULTS = 12
_MAX_ARTICLES = 100
_SNIPPET_CHARS = 500


_SYSTEM_PROMPT = """\
You are the Macro & Market Analyzer, a specialist agent in a multi-agent
financial analysis system. You are given THREE aligned inputs:
  1. Macroeconomic and broad-market news collected across the analysis period
     and grouped by month.
  2. The US 10-Year Treasury yield (^TNX) as a month-by-month time-series over
     the SAME period (the single most important cross-asset gauge of the rate
     regime, discount rates, and risk appetite).
  3. (When available) a current market-sentiment snapshot.

Your job is to characterize the macro environment — reading the NEWS and the
YIELD PATH together — AND translate it into what it means for one specific
company's sector.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of the macro backdrop and its relevance, INCLUDING the current yield-vs-news diagnosis>",
  "yield_news_correlation": "<REQUIRED. First, the historical relationship you observe in the data: when ^TNX spiked or dropped across the period, what was the news tone and market reaction? Then the current diagnosis read THROUGH that pattern: does today's yield level + latest news rhyme with an earlier episode or break from it? Cite specific months/levels from the ^TNX series only.>",
  "analysis_period": "<YYYY-MM-DD..YYYY-MM-DD>",
  "articles_analyzed": <int>,
  "current_market_regime": "risk_on|neutral|risk_off",
  "macro_score": <int 0-100>,
  "macro_environment_over_period": [
    {"period": "YYYY-MM", "regime": "risk_on|neutral|risk_off", "note": "<what defined the backdrop then>"}
  ],
  "sector_impact": {
    "sector": "<the company's sector, inferred>",
    "direction": "tailwind|neutral|headwind",
    "rate_sensitivity": "high|medium|low",
    "detail": "<the transmission channel: financing costs, demand, input prices, FX>"
  },
  "key_themes": [
    {"theme": "<e.g. Fed rate path, tariffs, AI capex cycle>",
     "direction": "tailwind|neutral|headwind",
     "impact_on_company": "<concrete effect on THIS company>",
     "evidence": "<headlines/facts supporting the theme>"}
  ]
}

RUBRIC for the NUMERIC `macro_score` (0-100) — how supportive the macro backdrop
is for equities:
- 80-100 = strongly supportive: easing policy, cooling inflation, resilient
           growth, broad risk appetite.
- 60-79  = moderately supportive, with identifiable risks.
- 40-59  = directionless: conflicting signals, data-dependent market.
- 20-39  = hostile: tightening policy, sticky inflation, slowing growth.
- 0-19   = severely hostile: recession signals, credit stress, systemic risk.

ENUM FIELDS — copy one of the listed values EXACTLY, never prose:
- `current_market_regime` and `regime`: risk_on | neutral | risk_off
- `direction`: tailwind | neutral | headwind
- `rate_sensitivity`: high | medium | low
The rubric text above describes the SCORE; never put a rubric phrase in an enum.

REGIME TRACKING:
- `macro_environment_over_period` must have one entry per month present in the
  data, in chronological order. The macro regime SHIFTS — capture the shifts
  rather than averaging them away. If the regime changed mid-period, say when
  and why in the notes.

CROSS-ASSET YIELD ↔ NEWS CORRELATION (^TNX 10-Year Treasury) — REQUIRED WORK:
Do this analysis explicitly; it drives `yield_news_correlation`, `reasoning`,
`sector_impact`, and every `impact_on_company`.
1. HISTORICAL CORRELATION — walk the period month by month. Ask yourself: in the
   provided data, when the 10-Y yield SPIKED or DROPPED, what was the tone of
   that month's news and the market's reaction? State the relationship you
   observe (e.g. "the +48 bps April move coincided with hot-inflation, 'higher
   for longer' headlines and a risk-off tone").
2. CURRENT DIAGNOSIS VIA PAST PATTERNS — read the CURRENT (latest) yield level
   and the latest news flow THROUGH the relationship you just identified. Say
   whether today rhymes with an earlier episode or breaks from it. Example
   shape: "yields are rising again, but unlike the earlier spike where the news
   signalled inflation panic and risk-off, the latest flow points to resilient
   growth — a less hostile setup for equities."
3. DATA DISCIPLINE — every statement about yields MUST come from the provided
   ^TNX series (levels, directions, months, bps). NEVER invent a yield number,
   a move, or a date. If the yield block says data was unavailable, say so in
   `yield_news_correlation` and lower confidence; do not guess.

SECTOR TRANSLATION (the part that makes this useful):
- Infer the company's sector from its name/ticker and the coverage.
- State the TRANSMISSION CHANNEL explicitly: do not write "rates affect the
  company" — write how, e.g. "higher long rates raise financing costs for the
  capex cycle this company sells into".
- Every `impact_on_company` must translate the SPECIFIC yield-and-news dynamic
  into what it means for THIS company across the relevant channels:
    • sector position,
    • cost of capital / financing conditions,
    • valuation multiple (the discount-rate effect long rates have on the
      multiple — critical for long-duration / high-growth names),
    • consumer / end demand.
  Generic market commentary that would apply to any stock is not acceptable.

RULES:
- Ground every claim in the provided articles, yield series, and snapshot; never
  invent headlines, yield levels, or economic data points.
- Keep the output STRICTLY the single JSON object above (no prose outside it),
  as the downstream Manager parses it directly.
- If the coverage does not support a confident sector read, say so in the detail
  and lower your confidence rather than guessing.
- CONFIDENCE reflects coverage: broad macro coverage plus a usable yield series
  spread across the period → 0.75+; thin/one-sided coverage or missing yield
  data → 0.4-0.6. The sector translation is inferential, so rarely exceed 0.85.
"""

_USER_TEMPLATE = """\
Analyze the macro/market environment for the period below and translate it for
the target company.

Target company: {company}{ticker}
Analysis period: {period}
Total articles collected: {count}
{yields}{snapshot}
=== MACRO / MARKET NEWS (grouped by month, oldest → newest) ===
{articles}
=== END NEWS ===
"""


async def _sentiment_snapshot() -> str:
    """
    Current market-sentiment read, rendered for the prompt. Best-effort: this is
    supplementary context, so any failure degrades to an empty block rather than
    failing the agent.
    """
    try:
        s = await compute_market_sentiment()
    except Exception as e:
        logger.warning(f"Market sentiment snapshot unavailable: {e}")
        return ""
    if not s.configured or not s.summary:
        return ""

    lines = [
        "",
        "=== CURRENT MARKET SENTIMENT SNAPSHOT (latest, not period-wide) ===",
        f"label: {s.label} | score: {s.score if s.score is not None else 'n/a'}/100",
        s.summary,
    ]
    lines += [f"- {i.theme} ({i.direction}): {i.note}" for i in s.indicators]
    lines.append("=== END SNAPSHOT ===")
    return "\n".join(lines) + "\n"


async def _yield_block(start_date: str, end_date: str) -> str:
    """
    US 10-Year Treasury yield (^TNX) as a monthly time-series, rendered for the
    prompt so the agent can correlate rate moves against the month-grouped news.

    Best-effort: if the fetch fails the block explicitly says the data was
    unavailable, so the agent degrades honestly (never inventing yield levels)
    rather than the whole agent failing.
    """
    try:
        yd = await price_provider.fetch_treasury_yield(start_date, end_date)
    except Exception as e:
        logger.warning(f"10Y Treasury yield (^TNX) unavailable: {e}")
        return (
            "\n=== US 10-YEAR TREASURY YIELD (^TNX) ===\n"
            "Yield data was UNAVAILABLE for this period. Do NOT infer or invent "
            "yield levels or moves; set `yield_news_correlation` to note the data "
            "gap and lower your confidence accordingly.\n"
            "=== END YIELD ===\n"
        )

    trend = (
        "rising" if yd.change_bps > 5
        else "falling" if yd.change_bps < -5
        else "roughly flat"
    )
    lines = [
        "",
        "=== US 10-YEAR TREASURY YIELD (^TNX) — monthly, oldest → newest ===",
        f"period: {yd.start_yield:.2f}% → {yd.current_yield:.2f}% "
        f"(net {yd.change_bps:+.0f} bps, {trend}); "
        f"range {yd.period_low:.2f}%–{yd.period_high:.2f}% "
        f"across {yd.data_points} trading days.",
        "month-end (month | yield | MoM change):",
    ]
    for m in yd.monthly:
        chg = f"{m['change_bps']:+.0f} bps" if m["change_bps"] is not None else "—"
        lines.append(f"- {m['month']} | {m['yield']:.2f}% | {chg}")
    lines.append(
        "Align these monthly yield moves with the same months' news below."
    )
    lines.append("=== END YIELD ===")
    return "\n".join(lines) + "\n"


class MacroMarketAgent(BaseAgent):
    """Macro regime analysis, translated into sector impact for the company."""

    @property
    def agent_id(self) -> str:
        return "macro_market"

    async def analyze(self, context: dict, capture: dict | None = None) -> MacroMarketReport:
        """
        Args:
            context: {"company": str, "ticker": str | None,
                      "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}
            capture: optional side-channel for the assembled raw-data prompt.
        """
        company = (context.get("company") or "").strip()
        ticker = (context.get("ticker") or "").strip() or None
        start_date = context["start_date"]
        end_date = context["end_date"]
        label = company or ticker or "(unknown company)"

        windows = month_windows(start_date, end_date)

        news_results, snapshot, yields = await asyncio.gather(
            asyncio.gather(
                *(
                    news_provider.search_macro_news(
                        max_results=_PER_WINDOW_RESULTS,
                        days=None, start_date=w_start, end_date=w_end,
                    )
                    for w_start, w_end in windows
                ),
                return_exceptions=True,
            ),
            _sentiment_snapshot(),
            _yield_block(start_date, end_date),
        )

        grouped: list[tuple[str, list]] = []
        seen: set[str] = set()
        total = 0
        for (w_start, w_end), res in zip(windows, news_results):
            if isinstance(res, Exception):
                logger.warning(f"Macro news fetch failed for {w_start}..{w_end}: {res}")
                continue
            if not res.configured:
                raise RuntimeError(
                    res.message
                    or "News is not configured: set TAVILY_API_KEY on the backend."
                )
            fresh = []
            for a in res.articles:
                if a.url in seen or total >= _MAX_ARTICLES:
                    continue
                seen.add(a.url)
                fresh.append(a)
                total += 1
            if fresh:
                grouped.append((w_start[:7], fresh))

        if not total:
            raise RuntimeError(
                f"No macro/market news was found for {start_date}..{end_date}."
            )

        lines: list[str] = []
        for month, articles in grouped:
            lines.append(f"\n--- {month} ({len(articles)} articles) ---")
            for a in articles:
                published = f" | published: {a.published}" if a.published else ""
                lines.append(
                    f"- [{a.source}] {a.title}{published}\n"
                    f"  {a.snippet[:_SNIPPET_CHARS]}"
                )

        user_prompt = _USER_TEMPLATE.format(
            company=label,
            ticker=f" ({ticker})" if ticker else "",
            period=f"{start_date}..{end_date}",
            count=total,
            yields=yields,
            snapshot=snapshot,
            articles="\n".join(lines),
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # Gemini's thinking tokens draw from the same budget as the answer, so
        # keep headroom above the report's own size.
        report = await self._generate_report(
            MacroMarketReport, _SYSTEM_PROMPT, user_prompt,
            max_output_tokens=16384,
        )

        report.analysis_period = f"{start_date}..{end_date}"
        report.articles_analyzed = total
        return report
