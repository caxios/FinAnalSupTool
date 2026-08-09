"""
agents/company_news_agent.py
────────────────────────────
Company News Analyzer Agent.

Fetches company news across the user's analysis period and asks the LLM for
BUSINESS IMPACT analysis — which segment is hit, through what mechanism, how
hard, and over what horizon — rather than positive/negative labelling.

Coverage note: Tavily caps results per call, so one search over a long range
returns articles clustered on recent weeks. We instead search month by month
(concurrently) and merge, which spreads coverage across the whole period and
makes the month-by-month sentiment trend meaningful.
"""

from __future__ import annotations

import asyncio
import logging

from providers import news_provider

from .base_agent import BaseAgent
from .date_windows import month_windows
from .schemas.company_news import CompanyNewsReport

logger = logging.getLogger(__name__)

_PER_WINDOW_RESULTS = 15     # articles per month window (Tavily caps ~20-30)
_MAX_ARTICLES = 120          # overall ceiling fed to the LLM
_SNIPPET_CHARS = 600


_SYSTEM_PROMPT = """\
You are the Company News Analyzer, a specialist agent in a multi-agent financial
analysis system. You are given news articles about one company, collected across
a specific analysis period and grouped by month.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of what the news flow means for the business>",
  "company": "<company name>",
  "articles_analyzed": <int>,
  "analysis_period": "<YYYY-MM-DD..YYYY-MM-DD>",
  "overall_sentiment": {
    "label": "positive|neutral|negative|mixed",
    "score": <int 0-100>,
    "note": "<one sentence on what drives the score>"
  },
  "business_impact_analysis": [
    {
      "headline": "<article headline>",
      "source": "<publisher domain>",
      "published": "<date or null>",
      "sentiment": "positive|neutral|negative|mixed",
      "affected_segment": "<business segment/unit, or 'company-wide'>",
      "impact_type": "<competitive_advantage|regulatory_headwind|market_expansion|product_launch|demand_shift|cost_pressure|litigation|management_change|capital_allocation|other>",
      "magnitude": "high|medium|low",
      "time_horizon": "short_term|medium_term|long_term",
      "analysis": "<HOW this changes the business operationally or financially>"
    }
  ],
  "sentiment_trend_over_period": [
    {"month": "YYYY-MM", "score": <int 0-100>, "article_count": <int>, "note": "<what dominated coverage>"}
  ],
  "catalysts": [
    {"catalyst": "<positive business driver>", "evidence": "<supporting articles/facts>", "time_horizon": "short_term|medium_term|long_term"}
  ],
  "headwinds": [
    {"headwind": "<negative business pressure>", "evidence": "<supporting articles/facts>", "time_horizon": "short_term|medium_term|long_term"}
  ]
}

RUBRIC for the NUMERIC `overall_sentiment.score` (0-100):
- 80-100 = overwhelmingly favourable flow: major wins, upgrades, expanding demand.
- 60-79  = favourable on balance, with some concerns.
- 40-59  = balanced, or genuinely two-sided coverage.
- 20-39  = unfavourable on balance: mounting pressures, downgrades, weak demand.
- 0-19   = severe: crisis, litigation, regulatory action, collapsing demand.

ENUM FIELDS — copy one of the listed values EXACTLY, never prose:
- `label` and `sentiment`: positive | neutral | negative | mixed
- `magnitude`: high | medium | low
- `time_horizon`: short_term | medium_term | long_term
The rubric text above describes the SCORE; never put a rubric phrase in `label`.

CORE DIRECTIVE — BUSINESS IMPACT, NOT MOOD:
- Do NOT simply classify articles as positive or negative. For each SIGNIFICANT
  article, analyze HOW it affects the company's actual business operations:
  revenue drivers, cost structure, competitive position, or regulatory exposure.
- Always identify WHICH business segment is affected and on WHAT time horizon.
  If an article is company-wide, say so explicitly.
- Cover only articles that matter. Skip routine price-movement recaps, generic
  "analysts weigh in" filler, and duplicate coverage of the same event — but
  fold repeated coverage into the magnitude of the underlying event.
- Distinguish the EVENT from the COVERAGE: three articles about one product
  launch is one development, not three.

TREND ANALYSIS:
- `sentiment_trend_over_period` must have one entry per month present in the
  data, in chronological order, with the article count for that month.
- Note when sentiment inflects — a shift mid-period matters more than the
  average level.

RULES:
- Ground every claim in the provided articles; never use outside knowledge about
  the company and never invent headlines, numbers, or sources.
- `source` and `headline` must be copied from the data, not paraphrased.
- CONFIDENCE reflects coverage quality: many substantive articles spread across
  the period → 0.8+; sparse, thin, or heavily clustered coverage → 0.4-0.6.
"""

_USER_TEMPLATE = """\
Analyze the following news coverage of {company}{ticker} and produce the JSON
report.

Analysis period: {period}
Total articles collected: {count}

=== ARTICLES (grouped by month, oldest → newest) ===
{articles}
=== END ARTICLES ===
"""


class CompanyNewsAgent(BaseAgent):
    """Business-impact analysis of company news across the analysis period."""

    @property
    def agent_id(self) -> str:
        return "company_news"

    async def analyze(self, context: dict, capture: dict | None = None) -> CompanyNewsReport:
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
        if not (company or ticker):
            raise RuntimeError("Company news analysis requires a company name or ticker.")

        label = company or ticker or ""
        windows = month_windows(start_date, end_date)

        results = await asyncio.gather(
            *(
                news_provider.search_company_news(
                    label, ticker,
                    max_results=_PER_WINDOW_RESULTS,
                    days=None, start_date=w_start, end_date=w_end,
                )
                for w_start, w_end in windows
            ),
            return_exceptions=True,
        )

        # Merge windows, keeping month grouping and dropping duplicate URLs.
        grouped: list[tuple[str, list]] = []
        seen: set[str] = set()
        total = 0
        for (w_start, w_end), res in zip(windows, results):
            if isinstance(res, Exception):
                logger.warning(f"Company news fetch failed for {w_start}..{w_end}: {res}")
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
                f"No news articles were found for {label} in {start_date}..{end_date}."
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
            articles="\n".join(lines),
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # A per-article impact breakdown across many months is a long report,
        # and Gemini's thinking tokens draw from the same budget — too small a
        # ceiling truncates the JSON mid-string and burns retries.
        report = await self._generate_report(
            CompanyNewsReport, _SYSTEM_PROMPT, user_prompt,
            max_output_tokens=32768,
        )

        # Ground the bookkeeping fields in what was actually fetched.
        report.company = label
        report.articles_analyzed = total
        report.analysis_period = f"{start_date}..{end_date}"
        return report
