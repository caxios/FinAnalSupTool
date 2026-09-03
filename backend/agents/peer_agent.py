"""
agents/peer_agent.py
─────────────────────
Peer Comparison Agent — benchmarks the analyzed company against real,
named industry rivals across valuation, profitability, growth, and financial
health, so a P/E or a margin is read against what the industry actually looks
like rather than in isolation.

Mirrors ``technical_analysis_agent`` / ``quant_risk_agent``: every number in
``metrics_table`` and ``valuation_assessment`` is computed in Python
(``providers.peer_provider``) BEFORE the LLM is called and is copied onto the
report afterwards, so the model can interpret the premium/discount but cannot
invent or recompute it.
"""

from __future__ import annotations

import json
import logging

from providers import peer_provider

from .base_agent import BaseAgent
from .schemas.peer import PeerComparisonReport

logger = logging.getLogger(__name__)

# How many peers to compare against — enough for a real median without
# diluting the comparison with distant, less-relevant names.
_PEER_LIMIT = 5

# A meaningful gap vs. the peer median, past which the target reads as a
# genuine premium/discount rather than statistical noise around "in line".
_VALUATION_THRESHOLD = 0.15
_VALUATION_METRICS = frozenset({
    "trailing_pe", "forward_pe", "ev_ebitda", "price_to_sales",
    "price_to_book", "peg_ratio",
})


def _classify_valuation(table: list[dict]) -> str:
    """
    'premium' | 'discount' | 'in_line', from the AVERAGE premium/discount
    across the valuation multiples that had a usable peer median. Deterministic
    on purpose — this is exactly the kind of categorical call an LLM tends to
    round to whatever sounds most narratively satisfying.
    """
    deltas = [
        row["premium_discount_pct"] for row in table
        if row["metric"] in _VALUATION_METRICS and row["premium_discount_pct"] is not None
    ]
    if not deltas:
        return "in_line"
    avg = sum(deltas) / len(deltas)
    if avg > _VALUATION_THRESHOLD:
        return "premium"
    if avg < -_VALUATION_THRESHOLD:
        return "discount"
    return "in_line"


_SYSTEM_PROMPT = """\
You are a peer & industry analyst in a multi-agent financial analysis system.
You are given a PRE-COMPUTED peer comparison table: the target company's
valuation, profitability, growth, and financial-health metrics against the
MEDIAN, MIN, and MAX of a set of real, named industry peers. Do NOT
recalculate any figure and do NOT introduce a number that is not in the table.

HOW TO READ THE TABLE:
- `higher_is_better` tells you which direction is favorable for that metric —
  a LOW P/E is cheap (good), a LOW debt/equity is safer (good), but a HIGH
  margin, ROE, or growth rate is good. Read `premium_discount_pct` through
  that lens, not just by its sign.
- `percentile` is the target's rank within the peer set plus itself, 0-100.
  A percentile near 100 on a `higher_is_better` metric is a real strength; the
  same percentile on a valuation multiple means the target is the MOST
  EXPENSIVE of the group, not the best.
- `valuation_assessment` (premium/discount/in_line) is already decided from the
  valuation multiples' average premium/discount — do not re-derive or
  contradict it; explain WHY it makes sense (or is puzzling) given the growth
  and margin figures.

YOUR TASK:
1. `competitive_moat`: 2-4 sentences on pricing power and market-share
   defensibility — inferred from the margin and growth figures relative to
   peers, not from outside knowledge about the company's brand.
2. `key_differentiators`: where the target concretely outperforms or lags,
   each bullet citing a specific number from the table (e.g. "operating margin
   of 34% vs a peer median of 21%").
3. Explain WHY any valuation gap exists in `reasoning` — a premium justified by
   superior margins/growth reads differently than a premium with no such
   support.
4. If `excluded_peers` is non-empty, or the peer set is small (2 or fewer
   peers), lower `confidence` and say so in `data_limitations` — a 2-name
   comparison is not a reliable median.

Output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary: is the valuation gap justified, and why>",
  "competitive_moat": "<2-4 sentences>",
  "key_differentiators": ["<specific, numbers-backed comparison>", ...],
  "data_limitations": ["<what limits this comparison>", ...]
}
"""

_USER_TEMPLATE = """\
=== TARGET ===
{target}  (sector: {sector}, industry: {industry})

=== PEER SET ({method}) ===
{peers}
{excluded_note}

=== PRE-COMPUTED METRICS TABLE ===
{table}
=== END TABLE ===

=== VALUATION ASSESSMENT (already computed, do not contradict) ===
{assessment}

Interpret this table. Do not recalculate any figure.
"""


class PeerComparisonAgent(BaseAgent):
    """Benchmarks the target against real industry peers; the LLM only interprets it."""

    @property
    def agent_id(self) -> str:
        return "peer_comparison"

    async def analyze(self, context: dict, capture: dict | None = None) -> PeerComparisonReport:
        """
        Args:
            context: ``ticker`` (required), optional ``company`` name for the
                     prompt only.
        """
        ticker = (context.get("ticker") or "").strip().upper()
        if not ticker:
            raise RuntimeError("Peer comparison requires a ticker symbol.")

        discovery = await peer_provider.discover_peers(ticker, limit=_PEER_LIMIT)
        peers = discovery["peers"]

        # No real peer could be identified — there is nothing for an LLM to
        # compare, so report the gap honestly rather than spend a call on it.
        if not peers:
            return PeerComparisonReport(
                agent=self.agent_id,
                confidence=0.2,
                reasoning="No peer set could be identified for this ticker, so "
                          "no relative comparison is available.",
                target_ticker=ticker,
                sector=discovery.get("sector"),
                industry=discovery.get("industry"),
                discovery_method=discovery.get("method"),
                data_limitations=[
                    "No curated or sector-matched peer group covers this "
                    "ticker; only an isolated read of this company is possible."
                ],
            )

        metrics = await peer_provider.fetch_peer_metrics(ticker, peers)
        table = metrics["metrics_table"]
        excluded = metrics["excluded_peers"]
        assessment = _classify_valuation(table)

        table_text = json.dumps(table, ensure_ascii=False, indent=2, default=str)
        user_prompt = _USER_TEMPLATE.format(
            target=ticker,
            sector=discovery.get("sector") or "unknown",
            industry=discovery.get("industry") or "unknown",
            method=discovery.get("method"),
            peers=", ".join(peers),
            excluded_note=(
                f"(Excluded — no usable data: {', '.join(excluded)})" if excluded else ""
            ),
            table=table_text,
            assessment=assessment,
        )
        if capture is not None:
            capture["raw_data"] = (
                f"=== TARGET: {ticker} ({discovery.get('sector')} / "
                f"{discovery.get('industry')}) ===\n"
                f"=== PEERS ({discovery.get('method')}): {', '.join(peers)} ===\n\n"
                f"{table_text}"
            )

        report = await self._generate_report(
            PeerComparisonReport, _SYSTEM_PROMPT, user_prompt,
        )

        # ── Overwrite every computed field from the Python-side data. ──
        report.target_ticker = ticker
        report.peer_tickers = peers
        report.sector = discovery.get("sector")
        report.industry = discovery.get("industry")
        report.discovery_method = discovery.get("method")
        report.metrics_table = table
        report.valuation_assessment = assessment
        report.excluded_peers = excluded
        return report
