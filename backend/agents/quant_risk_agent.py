"""
agents/quant_risk_agent.py
──────────────────────────
Tool-Augmented Quantitative Risk Agent — blueprint §2.

Computes objective portfolio risk with NumPy/pandas (``services.risk_metrics``)
and asks the LLM ONLY to interpret the resulting numbers. This mirrors
``technical_analysis_agent``: the model is explicitly told the figures are
pre-computed and that it must not recalculate them. The distinction matters more
here than anywhere else in the system — an LLM asked to compute a Value at Risk
will return a fluent, confident, wrong number, and a wrong risk figure is worse
than none because it invites larger positions.

The separation is *enforced*, not merely requested: every numeric field on the
report is overwritten from the computed metrics after the LLM returns, so even a
model that ignores the instruction cannot alter a number.

Debate participation
────────────────────
This agent does NOT join the round-table debate. Every debating agent
(``FIELD_AGENT_IDS``) analyzes a single company, while this one analyzes the
whole portfolio — a portfolio-level argument cannot rebut a company-level one,
and letting it try would produce cross-talk rather than disagreement. It runs
like ``MacroHistoryAgent``: an independent advisory report handed to the
Manager alongside the debate.

Macro synthesis
───────────────
Blueprint §2 requires merging the hard numbers with the Macro Market agent's
qualitative regime read, so the same volatility is judged differently in a
high-rate regime than in a calm one. The agent therefore runs AFTER phase 1, and
the pipeline passes it ``macro_context`` from the Macro agent's report.
"""

from __future__ import annotations

import json
import logging

import pandas as pd

from providers import price_provider
from services import risk_metrics

from .base_agent import BaseAgent
from .schemas.quant_risk import QuantRiskReport

logger = logging.getLogger(__name__)


# How far back to pull returns. ~1 year of trading days is enough for a stable
# covariance estimate without letting a regime from three years ago dominate.
_HISTORY_DAYS = 400

# What-if scenarios are computed for the top risk contributors only — a scenario
# per position would bloat the prompt without adding insight.
_SCENARIO_COUNT = 2
_SCENARIO_DELTA = 0.05     # +5 percentage points of portfolio weight


_SYSTEM_PROMPT = """\
You are a quantitative risk analyst in a multi-agent financial analysis system.
You are given PRE-COMPUTED portfolio risk metrics. Do NOT attempt to recalculate
them, and do NOT invent any number that is not in the data you were given —
interpret the values exactly as provided.

HOW TO READ THE INPUT:
- All loss figures (value_at_risk, conditional_var, max_drawdown) are POSITIVE
  fractions. 0.031 means a 3.1% loss.
- portfolio_volatility and each position's volatility are ANNUALIZED standard
  deviations. 0.28 means 28% annualized.
- risk_contribution_pct is a position's share of TOTAL portfolio risk. Compare it
  against that position's `weight` (its share of capital): a position at 10% of
  capital carrying 35% of risk is the single most important thing to report.
- average_correlation near 1.0 means the holdings move together, so
  diversification is an illusion; near 0 means they genuinely offset.
- Scenarios show what happens to total volatility if a position grows, and
  `funded_from` says which pocket pays for it.

CURRENCY — read this before interpreting any figure:
- Every amount is in KRW, and every weight is a share of NET WORTH (positions
  PLUS cash). Weights over the positions alone therefore sum to LESS than 1;
  the remainder is `concentration.cash_weight`. Do NOT describe the portfolio as
  fully invested.
- The user is won-based. Cash is risk-free only in its own currency: KRW cash
  has zero volatility, while USD cash carries the exchange rate's volatility and
  appears in `cash_positions` with a real risk contribution.
- `fx_risk.fx_contribution` is portfolio volatility minus the same portfolio
  with the currency hedged away. A NEGATIVE value means dollar exposure is
  REDUCING total risk, which is common for a won-based investor because USDKRW
  tends to rise when risk assets fall. Report that as diversification, not as a
  problem — and never describe FX as pure added risk when the number says
  otherwise.
- `fx_risk.exposure` is the share of net worth denominated in a foreign currency.
- If `cash.cash_drag` is null, say the opportunity cost of holding cash was not
  computed because no KRW risk-free rate is configured. Do not substitute a US
  yield for it.

YOUR TASK:
1. State how much risk this portfolio carries and WHERE it comes from, citing
   the specific numbers.
2. Flag concentration explicitly when risk share far exceeds capital share, or
   when average_correlation is high. If the portfolio is genuinely diversified,
   say so and set concentration_warning to null — do not manufacture a warning.
3. Condition your reading on the MACRO REGIME you are given. The same volatility
   means different things in a high-rate, tightening environment than in a calm
   one. If no macro context was provided, say so rather than inventing one.
4. Give concrete recommended_actions, each justified by a figure from the data.
5. Set risk_score 0-100 (0 = very low risk, 100 = extreme).

If data_sufficient is false, say plainly that the sample is too small for these
estimates to be relied on, and lower your confidence accordingly.

Output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of your risk read>",
  "risk_assessment": "<2-5 sentences: how much risk, and from where>",
  "concentration_warning": "<explicit warning, or null if well diversified>",
  "macro_conditioned_view": "<how the macro regime changes this reading>",
  "key_risks": ["<specific, numbers-backed risk>", ...],
  "recommended_actions": ["<concrete step justified by a figure>", ...],
  "risk_score": <int 0-100>
}
"""


_USER_TEMPLATE = """\
=== PRE-COMPUTED PORTFOLIO RISK METRICS ===
{metrics}
=== END METRICS ===

=== MACRO REGIME CONTEXT ===
{macro}
=== END MACRO CONTEXT ===

Interpret these metrics. Do not recalculate them and do not introduce numbers
that do not appear above.
"""


def _macro_summary(macro_report: dict | None) -> str:
    """
    Condense the Macro Market agent's report into the regime read this agent
    needs. Passing the whole report would bury the signal in company-specific
    detail that is irrelevant to portfolio risk.
    """
    if not macro_report:
        return (
            "(No macro context available for this run — judge the numbers on "
            "their own and say that the macro regime was not supplied.)"
        )
    keep = {
        k: macro_report.get(k)
        for k in ("market_regime", "macro_score", "regime_summary",
                  "yield_news_correlation", "analysis_period")
        if macro_report.get(k) is not None
    }
    themes = macro_report.get("key_themes") or []
    if isinstance(themes, list) and themes:
        keep["key_themes"] = [
            t.get("theme") for t in themes[:5] if isinstance(t, dict) and t.get("theme")
        ]
    if not keep:
        return "(Macro report contained no usable regime fields.)"
    return json.dumps(keep, ensure_ascii=False, indent=2, default=str)


class QuantRiskAgent(BaseAgent):
    """Computes portfolio risk in Python; the LLM only interprets it."""

    @property
    def agent_id(self) -> str:
        return "quant_risk"

    async def analyze(self, context: dict, capture: dict | None = None) -> QuantRiskReport:
        """
        Args:
            context: ``holdings`` (list of dicts from ``portfolio_service``),
                     optional ``macro_report`` (the Macro agent's dict),
                     optional ``start_date`` / ``end_date``.
        """
        holdings = context.get("holdings") or []
        cash = context.get("cash") or {}

        # An empty portfolio is a normal state of the app, not an error — and
        # there is nothing for an LLM to interpret, so return a well-formed
        # report directly rather than spending a call on it.
        #
        # Cash alone is no longer this case: a book of only won has near-zero
        # risk and a book of only dollars carries the exchange rate's own
        # volatility. Both are real answers.
        if not holdings and not cash:
            return QuantRiskReport(
                agent=self.agent_id,
                confidence=1.0,
                reasoning="No positions are held, so there is no portfolio risk "
                          "to measure.",
                risk_assessment="The portfolio is empty. Add positions to get a "
                                "risk assessment.",
                data_sufficient=False,
                risk_score=0,
            )

        tickers = [h.get("ticker") for h in holdings if h.get("ticker")]
        end = context.get("end_date")
        start = context.get("start_date")
        if not start or not end:
            from datetime import date, timedelta
            today = date.today()
            end = end or today.isoformat()
            start = start or (today - timedelta(days=_HISTORY_DAYS)).isoformat()

        from providers import fx_provider
        from services import cash_service as cs
        from services import portfolio_service as ps

        currencies = {t: ps.resolve_asset_currency(t) for t in tickers}
        base = cs.BASE_CURRENCY

        # BOTH series: base-currency prices drive the risk model (so the
        # stock/exchange-rate correlation is inside the returns), and the native
        # ones drive the hedged comparison in `fx_risk`.
        local_prices, dropped = await price_provider.fetch_price_history(
            tickers, start, end
        ) if tickers else (None, [])
        try:
            prices, dropped = await price_provider.fetch_price_history_base(
                tickers, start, end, currencies, base=base
            ) if tickers else (None, [])
        except Exception as e:  # noqa: BLE001 — fall back rather than fail the run
            logger.warning(
                f"[quant_risk] base-currency series unavailable ({e}); "
                f"falling back to native prices."
            )
            prices = local_prices

        fx_returns = None
        try:
            fx = await fx_provider.fetch_fx_history(start, end)
            if not fx.empty:
                fx_returns = fx.pct_change().dropna()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[quant_risk] no FX history for risk model: {e}")

        metrics = risk_metrics.compute_portfolio_risk(
            holdings, prices, cash=cash, fx_returns=fx_returns,
            base_currency=base, local_prices=local_prices,
        )

        # What-if scenarios on the biggest risk contributors (blueprint §2's
        # "how does changing this position alter total risk"), now funded from
        # the pocket the money would really come from.
        scenarios: list[dict] = []
        if metrics.get("positions") and prices is not None and not prices.empty:
            import numpy as np
            rets = risk_metrics.daily_returns(prices)
            cash_rets = risk_metrics.cash_return_columns(
                cash, fx_returns, rets.index, base
            )
            if not cash_rets.empty:
                rets = pd.concat([rets, cash_rets], axis=1).dropna(how="any")
            cols = list(rets.columns)
            weight_by = {p["ticker"]: p["weight"] for p in metrics["positions"]}
            weight_by.update({
                f"{risk_metrics.CASH_PREFIX}{c['currency']}": c["weight"]
                for c in metrics.get("cash_positions", [])
            })
            w = np.array([weight_by.get(c, 0.0) for c in cols], dtype=float)
            for p in metrics["positions"][:_SCENARIO_COUNT]:
                scenarios.append(
                    risk_metrics.simulate_position_change(
                        rets, w, p["ticker"], _SCENARIO_DELTA,
                        asset_currency=currencies.get(p["ticker"]),
                        base_currency=base,
                    )
                )
            # A lever that touches no position at all, and that this user pulls
            # every time they convert won to dollars to invest.
            for c in metrics.get("cash_positions", []):
                if c["currency"] != base and c["weight"] > 0:
                    scenarios.append(
                        risk_metrics.simulate_conversion(
                            rets, w, c["currency"], 0.5, base_currency=base
                        )
                    )
                    break

        payload = {**metrics, "scenarios": scenarios, "excluded_tickers": dropped}
        metrics_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        macro_text = _macro_summary(context.get("macro_report"))

        if capture is not None:
            capture["raw_data"] = (
                f"=== PORTFOLIO RISK METRICS ===\n{metrics_text}\n"
                f"=== END METRICS ===\n\n"
                f"=== MACRO REGIME ===\n{macro_text}\n=== END MACRO ==="
            )

        # No usable price history: there is nothing to interpret, so skip the
        # LLM and report the gap honestly instead of narrating null values.
        if metrics.get("portfolio_volatility") is None:
            note = metrics.get("data_quality", {}).get("note", "")
            return QuantRiskReport(
                agent=self.agent_id,
                confidence=0.2,
                reasoning=f"Risk metrics could not be computed. {note}",
                risk_assessment=note or "No risk metrics could be computed.",
                excluded_tickers=dropped,
                data_sufficient=False,
                risk_score=50,
            )

        report = await self._generate_report(
            QuantRiskReport,
            _SYSTEM_PROMPT,
            _USER_TEMPLATE.format(metrics=metrics_text, macro=macro_text),
        )

        # ── Overwrite every computed field from the Python-side metrics. ──
        # The prompt tells the model not to invent numbers; this makes it
        # impossible for it to matter either way. Only the prose fields and the
        # risk_score survive from the LLM.
        report.analysis_period = metrics.get("period")
        report.observations = metrics.get("observations", 0)
        report.confidence_level = metrics.get("confidence_level", 0.95)
        report.portfolio_volatility = metrics.get("portfolio_volatility")
        report.value_at_risk = metrics.get("value_at_risk")
        report.conditional_var = metrics.get("conditional_var")
        report.max_drawdown = metrics.get("max_drawdown")
        report.average_correlation = metrics.get("average_correlation")
        report.correlation_matrix = metrics.get("correlation_matrix", {})
        report.positions = metrics.get("positions", [])
        report.concentration = metrics.get("concentration", {})
        report.scenarios = scenarios
        report.excluded_tickers = dropped
        report.cash_positions = metrics.get("cash_positions", [])
        report.cash = metrics.get("cash", {})
        report.fx_risk = metrics.get("fx_risk", {})
        report.data_sufficient = bool(
            metrics.get("data_quality", {}).get("sufficient", False)
        )
        return report
