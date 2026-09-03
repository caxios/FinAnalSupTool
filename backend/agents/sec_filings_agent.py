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

import json
import logging

import pandas as pd

from gemini_chat import build_context
from rag import sec_rag

from .base_agent import BaseAgent
from .schemas.sec_filings import QoEAccrualRow, SECFilingsReport

logger = logging.getLogger(__name__)

# Sloan (1996) accrual-ratio thresholds: |ratio| above these flags earnings
# leaning increasingly on accruals rather than cash. Applied per period, not
# fitted — the same two numbers Sloan's original study used.
_SLOAN_MODERATE = 0.05
_SLOAN_AGGRESSIVE = 0.10


# =============================================================================
# System prompt (role + rubric + few-shot + grounding rules)
# =============================================================================

_SYSTEM_PROMPT = """\
You are a top-tier Wall Street Equity Research Analyst and a CFA Charterholder,
serving as the SEC Filings specialist in a multi-agent financial analysis system.
Your sole job is rigorous FUNDAMENTAL analysis of a company's SEC filings (10-K / 10-Q).
You analyze the financial statements for trends, extract deep insights from the MD&A,
classify risk factors, and score overall fundamental health with institutional-grade scrutiny.

ANALYTICAL DIRECTIVES (Think like a CFA Charterholder):
1. Quality of Earnings: You must compare Net Income to Cash from Operations (OCF). If Net Income is growing but OCF is negative or declining, flag this as a major risk (potential aggressive accruals or working capital bloat).
2. Margin Drivers: Identify exactly *why* margins are changing using the MD&A. (e.g., "Gross margin expanded due to pricing power, but operating margin compressed due to high SG&A spend").
3. Debt Sustainability: Do not just look at total debt. You must assess the Interest Coverage Ratio (EBIT / Interest Expense) to determine if the company can comfortably service its debt.
4. Segment Nuance: Companies are not monoliths. If the MD&A breaks down performance by product line or geography, highlight diverging segment performances.

FORENSIC QUALITY OF EARNINGS (a full 3-statement reconciliation, not just directive 1's quick read):
5. Accrual table: you are given a PRE-COMPUTED Sloan Accrual Ratio ((Net
   Income - OCF) / Total Assets) and cash-conversion ratio (OCF / Net Income)
   per period. Do NOT recompute these — interpret them in `accrual_summary`.
   `accrual_flag: "aggressive"` (|ratio| > 0.10) means profit is leaning
   heavily on accruals rather than cash; say so plainly. If the table is
   empty, say the XBRL data needed for it was not available — never estimate
   a Sloan ratio yourself from the statement text.
6. CapEx/PP&E vs. D&A reconciliation: from the DATA (balance sheet PP&E line
   items, cash-flow-statement CapEx and Depreciation & Amortization lines),
   assess whether the two move together as expected. Fill
   `capex_da_reconciliation` with what you find.
7. Depreciation cliff detection: a company that spent heavily on PP&E several
   periods ago and is now seeing D&A drop sharply while operating margin or
   EBIT jumps is very likely seeing that CapEx's useful life roll off — NOT
   organic margin expansion. Set `depreciation_cliff_detected` true ONLY when
   the DATA actually shows this pattern (declining D&A alongside an EBIT/
   margin jump, following a period of elevated PP&E/CapEx); otherwise false,
   with `depreciation_cliff_note` null.
8. Footnotes & MD&A cross-matching: tie disclosures in the Notes to Financial
   Statements (PP&E useful lives, inventory write-down reserves, capitalized
   R&D/software, revenue-recognition policy, litigation reserves) to an actual
   movement you can see in the statements or MD&A. Each `footnote_crossmatch`
   entry must name BOTH the disclosure and the movement it explains.
9. Structural vs. transitory classification: sort what is DRIVING earnings
   into `structural_drivers` (sustainable: pricing power, volume growth,
   favorable mix, structural cost cuts) vs. `transitory_drivers` (one-off:
   depreciation roll-off, inventory liquidation gains, tax valuation-allowance
   releases, gains on asset sales). A driver you cannot ground in the DATA
   belongs in neither list.
10. `qoe_score` (0-100): 0 = earnings leaning on accruals and one-off/
    transitory items with weak cash backing; 100 = earnings fully cash-backed
    (OCF >= Net Income) and driven by structural factors. This is a SEPARATE
    judgement from `fundamental_score` — a company can have strong revenue
    growth (high fundamental_score) built on weak earnings quality (low
    qoe_score), and that divergence is itself the most important thing to
    surface.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of how you reached the score>",
  "periods_analyzed": ["<period key>", ...],
  "fundamental_score": <int 0-100>,
  "financial_health": {
      "revenue":              {"latest_value": "<str>", "trend": "improving|stable|deteriorating", "commentary": "<Must mention segment divergence if present>"},
      "quality_of_earnings":  {"latest_value": "<OCF vs Net Income, at a glance>", "trend": "improving|stable|deteriorating", "commentary": "<Is cash conversion improving or deteriorating?>"},
      "margins":              {"latest_value": "<str>", "trend": "improving|stable|deteriorating", "commentary": "<Identify the specific cost driver from the MD&A>"},
      "debt":                 {"latest_value": "<Interest Coverage Ratio>", "trend": "improving|stable|deteriorating", "commentary": "<Assess ability to service debt, not just total leverage>"},
      "free_cash_flow":       {"latest_value": "<str>", "trend": "improving|stable|deteriorating", "commentary": "<Mention CapEx intensity>"}
    }
  "multi_period_trends": [
    {"metric": "<name>", "periods": ["...", "..."], "values": ["...", "..."], "direction": "improving|stable|deteriorating", "note": "<implication>"}
  ],
  "mda_insights": ["<insight grounded in the MD&A text, MUST include specific numbers/figures>", ...],
  "risk_assessment": [
    {"risk": "<short>", "category": "market|operational|financial|regulatory|other", "severity": "low|medium|high", "trend": "improving|stable|deteriorating", "note": "<evidence, MUST include concrete numbers (e.g., $500M loss)>"}
  ],
  "quality_of_earnings_forensic": {
    "accrual_table": [],
    "accrual_summary": "<what the given accrual/cash-conversion figures show across periods>",
    "capex_da_reconciliation": "<how PP&E/CapEx trends relate to D&A trends in the data>",
    "depreciation_cliff_detected": <bool>,
    "depreciation_cliff_note": "<evidence + magnitude, or null>",
    "footnote_crossmatch": ["<footnote disclosure tied to a specific statement/MD&A movement>", ...],
    "structural_drivers": ["<sustainable driver, grounded in a figure>", ...],
    "transitory_drivers": ["<one-off/accounting driver, grounded in a figure>", ...],
    "qoe_score": <int 0-100>
  }
}

NOTE on `quality_of_earnings_forensic.accrual_table`: leave it as an empty
array `[]` in your output — it is filled in from the pre-computed data after
you respond, so anything you write there is discarded. Spend your tokens on
the interpretive fields instead.

SCORING RUBRIC (fundamental_score, 0-100):
- 80-100 = High Quality: Revenue growing >5% YoY, strong cash conversion (OCF > Net Income), expanding operating margins, and Interest Coverage > 5x.
- 60-79  = Mixed/Average: Profitable but facing headwinds (e.g., revenue growing but cash conversion is poor, or margins compressing due to rising input costs).
- 0-59   = High Risk: Multiple fundamental breaks (declining revenue, OCF consistently lower than Net Income, high debt with Interest Coverage < 2.5x).

CONFIDENCE:
- Reflect DATA COMPLETENESS. Analyzing only 1 period → lower confidence
  (~0.4-0.6, no real trend). 3-4 periods with full statements + text → higher
  (~0.8-0.95). Missing text sections or missing statements → lower it.

RULES:
- STRICT QUANTITATIVE RULE: Do not use vague terms like "High debt" or "Declining revenue" alone. You MUST embed exact numbers, percentages, or dollar amounts into your `note`, `commentary`, and `mda_insights` strings (e.g. "High debt ($1.2B total, 4.5x leverage)").
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
- `quality_of_earnings_forensic.footnote_crossmatch`, `.structural_drivers`,
  and `.transitory_drivers` are held to the SAME quantitative-grounding rule as
  everything else — a driver with no traceable figure or footnote does not
  belong on either list. Empty lists are correct when the data doesn't support
  a classification; do not pad them to look thorough.
- `depreciation_cliff_detected` defaults to false. Only set it true when the
  DATA shows the actual pattern (prior elevated PP&E/CapEx, then declining
  D&A, then an EBIT/margin jump) — not merely because D&A declined for any
  reason, and not as a generic caveat.

EXAMPLE (shape only — use the real DATA for actual values):
{
  "confidence": 0.85,
  "reasoning": "Three quarters of data show accelerating revenue and expanding gross margin with consistently positive free cash flow, supporting a high score; leverage is modest, but earnings quality is mixed — see the QoE forensic section.",
  "periods_analyzed": ["Q4 FY2025", "Q1 FY2026", "Q2 FY2026"],
  "fundamental_score": 82,
  "financial_health": {
    "revenue": {"latest_value": "$1,895M", "trend": "improving", "commentary": "Revenue rose ~11% YoY in the latest quarter."},
    "quality_of_earnings": {"latest_value": "OCF $410M vs Net Income $455M", "trend": "stable", "commentary": "Cash conversion holds near 0.9x; see the forensic section for the accrual read."},
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
  ],
  "quality_of_earnings_forensic": {
    "accrual_table": [],
    "accrual_summary": "The Sloan Accrual Ratio held near -0.02 across the three periods (low_risk), and cash conversion stayed near 0.9x — earnings are largely cash-backed, not accrual-driven.",
    "capex_da_reconciliation": "PP&E, net rose from $2.1B to $2.6B over the window as CapEx of ~$180M/quarter exceeded D&A of ~$90M/quarter — the asset base is still expanding, not rolling off.",
    "depreciation_cliff_detected": false,
    "depreciation_cliff_note": null,
    "footnote_crossmatch": ["Notes disclose a 5-7 year useful life for data-center equipment, consistent with the steady D&A run-rate rather than a step-down."],
    "structural_drivers": ["Gross margin expansion of ~180bps attributed in the MD&A to a richer product mix, not a one-time pricing action."],
    "transitory_drivers": [],
    "qoe_score": 78
  }
}
"""

_USER_TEMPLATE = """\
Analyze the following SEC filing data and produce the JSON report.

The period keys available for `periods_analyzed` (oldest → newest) are:
{periods}

=== PRE-COMPUTED QoE ACCRUAL TABLE (Python — do not recompute these figures) ===
{accrual_table}
=== END ACCRUAL TABLE ===

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


def _qoe_accrual_table(
    metrics_store: dict[str, dict], periods: list[str]
) -> list[QoEAccrualRow]:
    """
    Per-period Sloan Accrual Ratio + cash-conversion ratio, computed in Python
    from the SAME raw XBRL metrics that already back the Ratios tab
    (``providers.edgar_xbrl.extract_ratio_metrics`` → ``CompanyStore.metrics_store``)
    — no new SEC/XBRL fetching, just reading what has already been ingested.

    The LLM is asked to interpret this table, never to derive it: an accrual
    ratio is exactly the kind of "confident, fluent, wrong" number an LLM
    produces when asked to eyeball it off multi-period statement text.

    Periods without XBRL-derived metrics (pdfplumber-only fallback filings)
    are skipped rather than guessed at.
    """
    rows: list[QoEAccrualRow] = []
    for pk in periods:
        m = metrics_store.get(pk)
        if not m:
            continue
        ni, cfo, ta = m.get("net_income"), m.get("operating_cash_flow"), m.get("total_assets")

        sloan = None
        if ni is not None and cfo is not None and ta:
            sloan = round((ni - cfo) / ta, 4)
        flag = None
        if sloan is not None:
            mag = abs(sloan)
            flag = (
                "aggressive" if mag > _SLOAN_AGGRESSIVE
                else "moderate" if mag > _SLOAN_MODERATE
                else "low_risk"
            )
        cash_conversion = round(cfo / ni, 4) if ni and cfo is not None else None

        rows.append(QoEAccrualRow(
            period=pk, net_income=ni, operating_cash_flow=cfo, total_assets=ta,
            sloan_accrual_ratio=sloan, accrual_flag=flag,
            cash_conversion_ratio=cash_conversion,
        ))
    return rows


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
                "metrics_store": dict[str, dict],  # raw per-period XBRL metrics
            }
            capture: optional side-channel for the assembled raw-data prompt.
        """
        merged_tables: dict[str, pd.DataFrame] = context.get("merged_tables", {}) or {}
        text_store: dict[str, dict] = context.get("text_store", {}) or {}
        filing_meta: dict[str, dict] = context.get("filing_meta", {}) or {}
        metrics_store: dict[str, dict] = context.get("metrics_store", {}) or {}
        ticker = (context.get("ticker") or "").strip() or None
        run_id = str(context.get("run_id") or "adhoc")

        periods = _ordered_periods(filing_meta)
        accrual_table = _qoe_accrual_table(metrics_store, periods)

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
        accrual_text = (
            json.dumps([r.model_dump() for r in accrual_table],
                       ensure_ascii=False, indent=2, default=str)
            if accrual_table else
            "(No XBRL-derived metrics available for these periods — Net "
            "Income, Operating Cash Flow, and Total Assets could not all be "
            "resolved, so no Sloan Accrual Ratio could be computed. Say so "
            "rather than estimating one from the statement text.)"
        )
        user_prompt = _USER_TEMPLATE.format(
            periods=", ".join(periods) or "(none)",
            accrual_table=accrual_text,
            context=data_context,
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # Multi-period trends + risk assessment across several filings runs long,
        # and Gemini's thinking tokens draw from the same budget — too small a
        # ceiling truncates the JSON mid-string and burns retries.
        report = await self._generate_report(
            SECFilingsReport, _SYSTEM_PROMPT, user_prompt, max_output_tokens=32768,
        )

        # Backfill periods if the model left them empty, so the report always
        # reflects what was actually loaded.
        if not report.periods_analyzed:
            report.periods_analyzed = periods

        # The accrual table is ground truth — overwrite whatever the model
        # echoed (or dropped) with the Python-computed rows, same enforcement
        # pattern as `quant_risk_agent` / `peer_agent`.
        report.quality_of_earnings_forensic.accrual_table = accrual_table
        return report
