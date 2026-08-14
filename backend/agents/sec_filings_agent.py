"""
agents/sec_filings_agent.py
───────────────────────────
SEC Filings Analyzer Agent (Step 1 of the MAS).

Ingests the parsed filing data already in memory — merged financial statements
+ ratios, extracted text sections (MD&A / Risk Factors / …), and filing
metadata — and produces a structured, schema-validated fundamental analysis.

It reuses `gemini_chat.build_context()` to render the DATA block, so the agent
sees exactly the same grounded view the chat assistant does.
"""

from __future__ import annotations

import logging

import pandas as pd

from gemini_chat import build_context
from rag import sec_rag

from .base_agent import BaseAgent
from .schemas.sec_filings import SECFilingsReport

logger = logging.getLogger(__name__)


# =============================================================================
# System prompt (role + rubric + few-shot + grounding rules)
# =============================================================================

_SYSTEM_PROMPT = """\
You are the SEC Filings Analyzer, a specialist agent in a multi-agent financial
analysis system. Your sole job is FUNDAMENTAL analysis of a company's SEC
filings (10-K / 10-Q). You analyze the financial statements for trends, extract
insights from the MD&A, classify risk factors, and score overall fundamental
health.

ANALYTICAL DIRECTIVES (Think like a Fundamental Analyst):
1. Quality of Earnings: You must compare Net Income to Cash from Operations (OCF). If Net Income is growing but OCF is negative or declining, flag this as a major risk (potential aggressive accruals or working capital bloat).
2. Margin Drivers: Identify exactly *why* margins are changing using the MD&A. (e.g., "Gross margin expanded due to pricing power, but operating margin compressed due to high SG&A spend").
3. Debt Sustainability: Do not just look at total debt. You must assess the Interest Coverage Ratio (EBIT / Interest Expense) to determine if the company can comfortably service its debt.
4. Segment Nuance: Companies are not monoliths. If the MD&A breaks down performance by product line or geography, highlight diverging segment performances.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of how you reached the score>",
  "periods_analyzed": ["<period key>", ...],
  "fundamental_score": <int 0-100>,
  "financial_health": {
      "revenue_and_segments": {"latest_value": "<str>", "trend": "improving|stable|deteriorating", "commentary": "<Must mention segment divergence if present>"},
      "quality_of_earnings":  {"latest_value": "<OCF vs Net Income>", "trend": "improving|stable|deteriorating", "commentary": "<Is cash conversion improving or deteriorating?>"},
      "margins_and_drivers":  {"latest_value": "<str>", "trend": "improving|stable|deteriorating", "commentary": "<Identify the specific cost driver from the MD&A>"},
      "debt_and_coverage":    {"latest_value": "<Interest Coverage Ratio>", "trend": "improving|stable|deteriorating", "commentary": "<Assess ability to service debt, not just total leverage>"},
      "free_cash_flow":       {"latest_value": "<str>", "trend": "improving|stable|deteriorating", "commentary": "<Mention CapEx intensity>"}
    }
  "multi_period_trends": [
    {"metric": "<name>", "periods": ["...", "..."], "values": ["...", "..."], "direction": "improving|stable|deteriorating", "note": "<implication>"}
  ],
  "mda_insights": ["<insight grounded in the MD&A text>", ...],
  "risk_assessment": [
    {"risk": "<short>", "category": "market|operational|financial|regulatory|other", "severity": "low|medium|high", "trend": "improving|stable|deteriorating", "note": "<evidence>"}
  ]
}

SCORING RUBRIC (fundamental_score, 0-100):
- 80-100 = High Quality: Revenue growing >5% YoY, strong cash conversion (OCF > Net Income), expanding operating margins, and Interest Coverage > 5x.
- 60-79  = Mixed/Average: Profitable but facing headwinds (e.g., revenue growing but cash conversion is poor, or margins compressing due to rising input costs).
- 0-59   = High Risk: Multiple fundamental breaks (declining revenue, OCF consistently lower than Net Income, high debt with Interest Coverage < 2.5x).

CONFIDENCE:
- Reflect DATA COMPLETENESS. Analyzing only 1 period → lower confidence
  (~0.4-0.6, no real trend). 3-4 periods with full statements + text → higher
  (~0.8-0.95). Missing text sections or missing statements → lower it.

RULES:
- Base EVERY claim on the DATA section. Do NOT invent numbers or use outside
  knowledge about the company's actuals.
- Every field is REQUIRED. If a financial_health dimension has no supporting
  data (e.g. no cash-flow statement was provided), still return the object with
  "trend": "stable", "latest_value": null, and commentary saying the data was
  not available. Never return null for a "trend" field.
- Statement values are in USD millions unless the label says otherwise; EPS is
  per share; ratios are multiples (e.g. 1.88x) or percentages.
- `multi_period_trends` requires >= 2 periods; with a single period, return an
  empty list and lower the confidence accordingly.
- `mda_insights` must be traceable to the MD&A / Business text; if no text was
  provided, return an empty list and say so in `reasoning`.
- Do not hallucinate risks — only classify risks supported by the Risk Factors
  or MD&A text (or clear financial signals in the numbers).

EXAMPLE (shape only — use the real DATA for actual values):
{
  "confidence": 0.85,
  "reasoning": "Three quarters of data show accelerating revenue and expanding gross margin with consistently positive free cash flow, supporting a high score; leverage is modest.",
  "periods_analyzed": ["Q4 FY2025", "Q1 FY2026", "Q2 FY2026"],
  "fundamental_score": 82,
  "financial_health": {
    "revenue": {"latest_value": "$1,895M", "trend": "improving", "commentary": "Revenue rose ~11% YoY in the latest quarter."},
    "margins": {"latest_value": "62.4%", "trend": "improving", "commentary": "Gross margin expanded ~180bps over three quarters."},
    "debt": {"latest_value": "1.9x", "trend": "stable", "commentary": "Net leverage held near 1.9x with ample coverage."},
    "free_cash_flow": {"latest_value": "$402M", "trend": "improving", "commentary": "FCF grew each quarter and remained positive."}
  },
  "multi_period_trends": [
    {"metric": "Total Revenue", "periods": ["Q4 FY2025","Q1 FY2026","Q2 FY2026"], "values": ["$1,703M","$1,817M","$1,895M"], "direction": "improving", "note": "Sequential growth every quarter."}
  ],
  "mda_insights": ["Management attributes growth to data-center demand, per the MD&A."],
  "risk_assessment": [
    {"risk": "Customer concentration", "category": "operational", "severity": "medium", "trend": "stable", "note": "Risk Factors cite reliance on a few large customers."}
  ]
}
"""

_USER_TEMPLATE = """\
Analyze the following SEC filing data and produce the JSON report.

The period keys available for `periods_analyzed` (oldest → newest) are:
{periods}

=== DATA ===
{context}
=== END DATA ===
"""


def _ordered_periods(filing_meta: dict[str, dict]) -> list[str]:
    """Period keys sorted chronologically (oldest → newest)."""
    def key(pk: str):
        d = filing_meta.get(pk, {}).get("sort_date")
        return (0, d) if d else (1, str(pk))
    return sorted(filing_meta.keys(), key=key)


class SECFilingsAgent(BaseAgent):
    """Fundamental analysis of uploaded SEC filings."""

    @property
    def agent_id(self) -> str:
        return "sec_filings"

    async def analyze(self, context: dict, capture: dict | None = None) -> SECFilingsReport:
        """
        Args:
            context: {
                "merged_tables": dict[str, pd.DataFrame],
                "text_store":    dict[str, dict],
                "filing_meta":   dict[str, dict],
            }
            capture: optional side-channel for the assembled raw-data prompt.
        """
        merged_tables: dict[str, pd.DataFrame] = context.get("merged_tables", {}) or {}
        text_store: dict[str, dict] = context.get("text_store", {}) or {}
        filing_meta: dict[str, dict] = context.get("filing_meta", {}) or {}
        ticker = (context.get("ticker") or "").strip() or None
        run_id = str(context.get("run_id") or "adhoc")

        periods = _ordered_periods(filing_meta)

        # Filing text is used IN FULL by default. Only when the combined MD&A /
        # Risk Factors / … across all periods overflows the model's optimal
        # window do we chunk every section and retrieve the passages relevant to
        # our fundamental analysis topics — so nothing is lost by position, and
        # late-section detail (segments, tail risk factors) still reaches us.
        # Any RAG failure returns None → build_context renders full text.
        filing_text_override = None
        try:
            filing_text_override = await sec_rag.prepare_context(
                text_store, periods,
                queries=sec_rag.SEC_ANALYSIS_TOPICS,
                ticker=ticker, run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001 — RAG is best-effort; fall back to full text
            logger.warning(f"SEC filings RAG failed, using full text: {e}")
            filing_text_override = None
        if filing_text_override:
            logger.info(
                "SEC filings agent using RAG-retrieved filing-text excerpts "
                f"(~{sec_rag.estimate_total_tokens(text_store, periods):,} est. tokens of text)."
            )

        data_context = build_context(
            merged_tables, text_store, filing_meta,
            filing_text_override=filing_text_override,
        )
        user_prompt = _USER_TEMPLATE.format(
            periods=", ".join(periods) or "(none)",
            context=data_context,
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # Multi-period trends + risk assessment across several filings runs long,
        # and Gemini's thinking tokens draw from the same budget — too small a
        # ceiling truncates the JSON mid-string and burns retries.
        report = await self._generate_report(
            SECFilingsReport, _SYSTEM_PROMPT, user_prompt, max_output_tokens=16384,
        )

        # Backfill periods if the model left them empty, so the report always
        # reflects what was actually loaded.
        if not report.periods_analyzed:
            report.periods_analyzed = periods
        return report
