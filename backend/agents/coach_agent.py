"""
agents/coach_agent.py
─────────────────────
Adaptive Trading Coach Agent — blueprint §3.

A meta-cognitive coach: it holds the user's stated **Entry Rationale** against
what the objective data actually said, names the psychological bias when those
two disagree, and cites the user's OWN past trades as evidence.

Style-respecting rule incubator, not a lecturer
────────────────────────────────────────────────
The coach's mandate is NOT to push every user toward the same conservative
disposition. It identifies the user's own trading archetype (from their real
closed trades — see :func:`journal_analysis.trader_archetype`) and helps them
execute THAT style with a better payoff ratio and more discipline — never
talks an aggressive momentum trader into becoming a passive value investor,
or the reverse. See the "STYLE" directive in ``_SYSTEM_PROMPT`` below.

Four pillars
────────────
  1. Fundamental/Peer/Technical — a condensed institutional digest (Manager
     verdict, forensic QoE, peer valuation, technical levels), retrieved
     ON DEMAND by :func:`fetch_fundamental_analysis` rather than stuffed into
     every prompt regardless of whether the review names a ticker.
  2. Behavioural — ``services.journal_analysis``, which joins every logged trade
                   to what the price did afterwards.
  3. Archetype   — the user's own demonstrated trading style (descriptive,
                   never a target — see above).

This agent adds no new RAW data source; :func:`fetch_fundamental_analysis`
reads the same Manager/SEC/Peer/Technical reports Deep Analysis already
produced, just condensed and fetched only when a ticker is actually named.
The synthesis is still the risk: a coaching claim about "your last three
trades" is worthless unless those trades exist. So the journal statistics are
computed in Python and handed over as structure, the prompt forbids inventing
a date or a figure, and :func:`verify_citations` checks every date the model
returns against the real journal before the report is served.

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
from datetime import datetime, timedelta, timezone

from pydantic import Field

from services import journal_analysis, portfolio_service

from .base_agent import AgentReport, BaseAgent
from .schemas.coach import CoachReport, JournalReport

logger = logging.getLogger(__name__)


# How many recent journal entries to show the model. Enough to establish a
# pattern; bounded so a long history doesn't crowd out the reports.
_MAX_JOURNAL_ROWS = 25

# A journal review may return at most this many priorities. Enforced in code, not
# only asked for: a list of twelve things to fix is a list of zero things that
# will be fixed.
_MAX_PRIORITIES = 3

# The decision-relevant slices of the two analyst reports, named once so the
# pre-trade and retrospective paths cannot drift apart in what they show.
_SEC_FIELDS = (
    "confidence", "reasoning", "financial_health", "key_findings",
    "revenue_trend", "profitability", "risks", "fundamental_score",
    "overall_assessment",
)
_TECH_FIELDS = (
    "confidence", "reasoning", "current_price", "trend_assessment",
    "momentum_indicators", "key_levels", "pattern_recognition",
    "technical_score", "price_vs_fundamentals",
)


# =============================================================================
# Tool-augmented fundamental grounding
# =============================================================================
# The coach must ground a reflection in the SAME institutional research Deep
# Analysis already produced for that ticker — Manager verdict, forensic QoE,
# peer valuation, technical levels — but stuffing full reports into EVERY
# coaching prompt regardless of whether the review even names a ticker would
# exhaust the context budget and dilute the prompt for the reviews that don't.
#
# So this is retrieved ON DEMAND, exactly like a tool call the model would
# issue for itself: `fetch_fundamental_analysis(ticker)` is called only when a
# review names a ticker, and only for that ticker. It is implemented as a
# direct Python call rather than a real Gemini function-calling round trip —
# this app's LLM plumbing (`agents.llm_utils`) has no tool-calling loop, and
# this is the same "compute/fetch in Python, hand the model a demarcated
# section to interpret" pattern every other agent in this codebase already
# uses (`sec_rag.prepare_context`, `portfolio_risk_context`, etc.) — a
# pre-invocation resolution step rather than a mid-generation tool call.
#
# This is ALWAYS the latest available research — never time-scoped — so it is
# only safe to call for a review that is inherently "as of now": the pre-trade
# review and the whole-journal review. A retrospective review of a trade
# already made must instead use `rag.history_store.analysis_as_of`, which
# excludes anything the user could not have known at the time; see
# `analyze_retrospective`, which never calls this function.

_PEER_METRIC_HIGHLIGHTS = ("trailing_pe", "ev_ebitda", "gross_margin", "revenue_growth_yoy")


def _condense_manager(manager) -> dict | None:
    """
    Normalize a Manager verdict to a plain dict, or ``None`` if there isn't a
    usable one.

    ``services.storage.DebateStore`` holds the LIVE run's manager result as
    whatever ``ManagerAgent().analyze()`` returned — a ``ManagerReport``
    Pydantic object on success, or a bare ``{"error": ...}`` dict on failure
    (see ``services.pipeline``) — while a PERSISTED history record already
    holds it as a plain dict. Both shapes reach this function.
    """
    if manager is None:
        return None
    if hasattr(manager, "model_dump"):
        manager = manager.model_dump()
    if not isinstance(manager, dict) or manager.get("error"):
        return None
    return {
        "recommendation": manager.get("recommendation"),
        "conviction": manager.get("conviction"),
        "overall_score": manager.get("overall_score"),
        "executive_summary": manager.get("executive_summary"),
        "bull_case": manager.get("bull_case"),
        "bear_case": manager.get("bear_case"),
        "thesis_pillars": manager.get("thesis_pillars"),
    }


def _condense_qoe(sec: dict | None) -> dict | None:
    """The forensic Quality-of-Earnings read, without the raw accrual table."""
    qoe = (sec or {}).get("quality_of_earnings_forensic") or {}
    if not qoe:
        return None
    return {
        "qoe_score": qoe.get("qoe_score"),
        "accrual_summary": qoe.get("accrual_summary"),
        "capex_da_reconciliation": qoe.get("capex_da_reconciliation"),
        "depreciation_cliff_detected": qoe.get("depreciation_cliff_detected"),
        "depreciation_cliff_note": qoe.get("depreciation_cliff_note"),
        "structural_drivers": qoe.get("structural_drivers"),
        "transitory_drivers": qoe.get("transitory_drivers"),
    }


def _condense_peer(peer: dict | None) -> dict | None:
    """Valuation stance + moat, without the full peer metrics table."""
    if not peer:
        return None
    rows = {r.get("metric"): r for r in (peer.get("metrics_table") or [])}
    highlights = [
        {
            "metric": rows[m].get("label"), "target": rows[m].get("target_value"),
            "peer_median": rows[m].get("peer_median"),
            "premium_discount_pct": rows[m].get("premium_discount_pct"),
        }
        for m in _PEER_METRIC_HIGHLIGHTS if m in rows
    ]
    return {
        "valuation_assessment": peer.get("valuation_assessment"),
        "competitive_moat": peer.get("competitive_moat"),
        "key_differentiators": peer.get("key_differentiators"),
        "valuation_highlights": highlights,
    }


def _condense_fundamental_digest(
    *, status: str, manager=None, sec: dict | None = None,
    peer: dict | None = None, technical: dict | None = None,
    period: str | None = None, message: str | None = None,
) -> dict:
    """Assemble the compact institutional digest from whatever pieces exist."""
    return {
        "status": status,
        "period": period,
        "message": message,
        "manager_verdict": _condense_manager(manager),
        "quality_of_earnings": _condense_qoe(sec),
        "peer_valuation": _condense_peer(peer),
        "technical": (
            {k: technical.get(k) for k in _TECH_FIELDS if technical.get(k) is not None}
            if technical else None
        ),
        "downside_risks": (sec or {}).get("risk_assessment"),
    }


def fetch_fundamental_analysis(ticker: str) -> dict:
    """
    On-demand tool: the institutional research digest for ``ticker`` — Manager
    verdict, forensic QoE, peer valuation, technical levels, and downside
    risks — from the most recent /analyze run this session, falling back to
    the latest persisted Deep Analysis. Never the raw filing text/tables;
    those already have their own retrieval path (the AI Chat's RAG fallback
    chain) and would drown a coaching prompt in detail it doesn't need.

    Returns a well-formed digest with ``status`` in ``'active_run'``,
    ``'persisted_history'``, or ``'no_prior_analysis'``/``'no_ticker'`` — never
    raises. Missing pieces (e.g. no Peer Comparison agent ran) come back as
    ``None`` fields rather than an error.
    """
    from rag import history_store
    from services.storage import get_debate_store

    t = (ticker or "").strip().upper()
    if not t:
        return _condense_fundamental_digest(status="no_ticker")

    live = get_debate_store().get(t)
    if live and live.get("reports"):
        reports = live.get("reports") or {}
        return _condense_fundamental_digest(
            status="active_run", period=live.get("period"),
            manager=live.get("manager"), sec=reports.get("sec_filings"),
            peer=reports.get("peer_comparison"),
            technical=reports.get("technical_analysis"),
        )

    record = history_store.get_latest_analysis(t)
    if record:
        reports = record.get("reports") or {}
        return _condense_fundamental_digest(
            status="persisted_history", period=record.get("analysis_period"),
            manager=record.get("manager"), sec=reports.get("sec_filings"),
            peer=reports.get("peer_comparison"),
            technical=reports.get("technical_analysis"),
        )

    return _condense_fundamental_digest(
        status="no_prior_analysis",
        message=f"No Deep Analysis has been run for {t} yet.",
    )


_SYSTEM_PROMPT = """\
You are a trading coach in a financial analysis system. Your job is NOT to pick
stocks — it is to help this user see their own decision-making clearly.

You are given five things:
  1. The trade the user is considering, and THEIR OWN stated reason for it. This
     may instead be a NON-TRADE REFLECTION — a dilemma, a decision to pass or
     wait, or a market note with no proposed execution at all (look for
     "Observing/passing on", "Contemplating opening a position in", "Watching
     an existing position in — without selling —", "Contemplating whether to
     sell", or similar in THE TRADE UNDER REVIEW). These labels are computed
     from whether the user already HOLDS the ticker — a reflection on a held
     position is about EXITING; on one they don't hold, it is about ENTERING.
     Match your coaching to whichever it is: entry hesitation (FOMO, fear of
     missing the move) and exit hesitation (hope-holding a loser, reluctance
     to lock in a gain, anchoring to the purchase price) are different
     failure modes and deserve different feedback. Do NOT assume an execution
     took place. Give meta-cognitive feedback on the psychological conflict or
     the reasoning itself — exactly as you would for a real trade, just
     without talking about a fill, a position size change, or a cost basis.
  2. FUNDAMENTAL MANAGER SYNTHESIS: an institutional digest retrieved ON DEMAND
     for this ticker — the Lead Analyst's verdict/conviction, forensic Quality
     of Earnings, peer valuation, technical levels, and downside risks. It may
     say no Deep Analysis has been run yet — say so plainly rather than
     inventing a fundamental view.
  3. YOUR TRADING ARCHETYPE: a computed, descriptive label for the style the
     user's own closed trades already show (e.g. "Aggressive Momentum Trader").
     This is not a target — see the STYLE directive below.
  4. YOUR OWN PLAYBOOK: the user's empirically-derived Golden Setup / Toxic
     Pattern rules, matched against this proposed trade.
  5. The user's real trading journal: past trades, what they wrote at the time,
     and what the price actually did 7/30/90 days later.

STYLE — the single most important directive in this prompt:
- Identify the user's trading archetype from ARCHETYPE below (or from the
  journal itself if ARCHETYPE says the history is too short). NEVER attempt to
  change their inherent disposition — do not tell an aggressive momentum
  trader to become a passive value investor, and do not tell a patient value
  investor to chase momentum. Your sole mandate is helping them execute their
  OWN chosen style with a better payoff ratio and more discipline.
- Formulate every actionable piece of `coaching_feedback` as a testable rule
  where the data supports one: condition (what set this trade apart) →
  execution criteria (what to require before entering) → stop/exit boundary
  (when to cut it) → expected payoff (cite the real win rate/expectancy from
  PLAYBOOK or the journal). A vague "be more careful" is not coaching; "your
  aggressive breakouts bought on volume confirmation with sub-2% initial risk
  have a 3.4:1 payoff ratio — the ones chased after already running 15%+ do
  not" is.
- Ground every claim in BOTH the user's own words (quote them) AND a specific
  figure from FUNDAMENTAL MANAGER SYNTHESIS, PLAYBOOK, or the journal. Never
  one without the other when both are available.

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
- NEVER state a directional view on the exchange rate. You may say what the
  user's exposure IS, what rate they entered at versus rates they have seen, and
  that their rationale did not mention the currency. You may NOT say where USDKRW
  is heading. This is the same failure as inventing a trade — a confident claim
  the data does not support — and it belongs under the same rule.

WHAT TO DO:
- Compare the user's stated rationale against FUNDAMENTAL MANAGER SYNTHESIS.
  Name the conflict EXPLICITLY when they disagree. For example: "You're
  selling because the technicals broke, but the Manager's verdict is bullish
  on revenue up 20% and margins expanding — those are different time
  horizons, and your reason only addresses one of them."
- When the journal supports it, connect this decision to the user's own history:
  "The last two times you wrote something like this (2026-03-14, 2026-05-02),
  the position was higher 30 days later."
- Detect biases only when you can evidence them: FOMO, panic selling, loss
  aversion, anchoring, revenge trading, recency bias, over-concentration
  (repeatedly adding to an already-largest position), cash-drag anxiety
  (deploying immediately after every deposit), panic de-risking (a withdrawal or
  sell cluster after a drawdown), and currency chasing (converting to dollars in
  bulk right after a sharp won move).
- For a reflection on a HELD position (exit hesitation), also watch for: the
  disposition effect (hoping a loser recovers instead of cutting it, while
  taking winners early), anchoring to the original purchase price rather than
  the current thesis, sunk-cost reasoning ("I've already held this long, might
  as well keep holding"), and greed-driven profit-taking reluctance (refusing
  to trim a large unrealized gain with no new evidence for the extra upside).

SIZING AND CURRENCY — use POSITION & SIZING below when it is present:
- State concentration as a fact when it is material: "this buy takes AAPL from
  22% to 38% of your net worth". Often that restatement IS the coaching. Say
  nothing about size when the trade is small — a 2% position needs no comment.
- Call out dry-powder exhaustion with the actual number: "this deploys 94% of
  your remaining dollars, leaving nothing to average down with if the thesis
  takes longer than you expect". This lands hardest when the rationale sounds
  urgent.
- When `requires_conversion` is true, the user is making TWO decisions: the
  stock and the dollar. Name the exposure change ("this raises your dollar
  exposure from 51% to 64% of net worth") and note it if the rationale addresses
  only the company. Buying after the won has already weakened is a fact about
  their entry rate — state it as that, never as a forecast.
- Compare what they SAY against what they STAKE. A hedged, uncertain rationale
  behind an unusually large position — or a strongly argued one behind a token
  position — is the most legible behavioural signal the journal contains.

PORTFOLIO RISK — use PORTFOLIO RISK below when it is present. These are
WHOLE-BOOK numbers (VaR, correlation, risk contribution vs. capital weight),
computed the same way the Portfolio Risk dashboard computes them — not this
one position's size, which SIZING above already covers:
- `risk_warnings` is a list of concrete, Python-computed flags (concentration,
  high correlation to existing holdings, a large simulated volatility jump).
  When it is non-empty, you MUST work every warning into `coaching_feedback` in
  your own words, grounded in the specific numbers given — do not soften or
  omit one. When it is empty, do not manufacture a risk concern.
- `current_position_risk.risk_contribution_pct` vs. `.weight`: a position
  carrying much more risk share than capital share is the single most
  important quantitative fact to surface if it applies here.
- `correlation_to_holdings`: correlations above ~0.7 mean this ticker moves
  with what is already held — say so plainly rather than treating it as a new,
  independent bet.
- `simulated_impact` (when present) shows the ACTUAL covariance-based effect of
  this trade on total portfolio volatility — cite the before/after numbers
  rather than describing the change qualitatively.
- Never invent a VaR, correlation, or volatility figure. Every number you cite
  from this section must appear in it verbatim.

FUNDAMENTAL MANAGER SYNTHESIS — use it when `status` is `active_run` or
`persisted_history`; when it is `no_prior_analysis` or `no_ticker`, say so and
do not invent a fundamental view:
- `manager_verdict` (recommendation/conviction/thesis) is the Lead Analyst's
  synthesized stance — cite it by name ("the Manager's bullish, high-conviction
  call") rather than treating it as your own independent opinion.
- `quality_of_earnings` (`qoe_score`, `accrual_summary`, a detected
  depreciation cliff, structural vs. transitory drivers) is forensic, not a
  vibe — if the user's rationale claims strong earnings growth, check it
  against this before agreeing.
- `peer_valuation` (`valuation_assessment`, the P/E or EV/EBITDA premium or
  discount, `competitive_moat`) tells you whether the price the user is
  paying is cheap or expensive RELATIVE to comparable companies — a fact the
  user's own rationale usually never mentions.
- Every figure you cite from this section must appear in it verbatim. A null
  field (e.g. no Peer Comparison agent ran) means say nothing about that
  dimension — never fill the gap with a guess.

YOUR OWN PLAYBOOK — use PLAYBOOK below when it is present. `toxic_pattern_matches`
and `golden_setup_matches` are the user's OWN empirically-derived rules
(win rate, payoff ratio, expectancy — computed from their real closed trades in
`services.journal_analysis`), matched against this proposed trade's own
rationale/strategy/emotion classification — NOT a generic warning:
- A NON-EMPTY `toxic_pattern_matches` is the single most important thing in
  this review. Open `coaching_feedback` with it, name the matched rule's
  `title`, and cite its actual `win_rate`/`expectancy` (e.g. "This matches your
  own 'FOMO momentum chases' pattern — 0% win rate, -₩53,333 average expectancy
  across your last 3 trades like this."). This is empirical, not a lecture.
- A NON-EMPTY `golden_setup_matches` is validating evidence — say the trade
  fits a setup that has actually worked for this user, citing the figures.
- Never invent a rule, a win rate, or a match — use only what PLAYBOOK gives
  you. An empty list on either side means say nothing about it.
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

=== FUNDAMENTAL MANAGER SYNTHESIS (tool-retrieved on demand for this ticker) ===
{fundamental_synthesis}
=== END SYNTHESIS ===

=== YOUR TRADING ARCHETYPE (computed from your own closed trades — descriptive, not a target) ===
{archetype}
=== END ARCHETYPE ===

=== THE USER'S TRADING JOURNAL (real logged trades) ===
{journal}
=== END JOURNAL ===

=== BEHAVIOURAL SUMMARY (computed, not estimated) ===
{patterns}
=== END SUMMARY ===

=== POSITION & SIZING (computed, in KRW; weights are of NET WORTH) ===
{sizing}
=== END SIZING ===

=== PORTFOLIO RISK (computed — VaR/CVaR/correlation over the WHOLE book) ===
{risk}
=== END PORTFOLIO RISK ===

=== YOUR OWN PLAYBOOK (computed — this trade checked against YOUR adopted rules) ===
{playbook}
=== END PLAYBOOK ===

Review this decision.
"""


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


async def position_context(
    ticker: str | None, side: str | None, quantity: float | None,
    price: float | None = None,
) -> dict:
    """
    The fourth pillar: **how much** this trade commits, and **in which currency**.

    A trade is a different decision at 2% of net worth than at 40%, and a US
    purchase funded by conversion is two bets — the stock and the dollar — that
    the app could not previously separate. These are numbers, not judgements:
    Python computes them, the LLM interprets them, exactly as with the journal
    statistics and the risk metrics.

    Never raises. A portfolio that cannot be valued yields an empty dict and the
    prompt simply has no sizing to comment on.
    """
    from providers import fx_provider
    from services import cash_service as cs
    from services import portfolio_service as ps

    try:
        valued, totals = await ps.value_holdings()
        net_worth = totals.get("net_worth_krw")
        if not net_worth or net_worth <= 0:
            return {}

        currency = ps.resolve_asset_currency(ticker) if ticker else None
        balances = cs.balances()
        spot = None
        try:
            spot = (await fx_provider.fetch_spot()).rate
        except Exception:  # noqa: BLE001 — sizing degrades, it does not fail
            pass

        ctx: dict = {
            "net_worth_krw": round(net_worth, 2),
            "net_worth_usd": totals.get("net_worth_usd"),
            "cash_balances": balances,
            "cash_weight": totals.get("cash_weight"),
            "fx_exposure_before": totals.get("fx_exposure"),
            "largest_position_weight": max(
                (v.get("weight") or 0.0 for v in valued), default=None
            ),
            "trade_currency": currency,
        }

        current = next(
            (v for v in valued if v["ticker"] == (ticker or "").strip().upper()), None
        )
        ctx["position_current_weight"] = (current or {}).get("weight")

        # Value the proposed trade at the position's current price when the
        # caller has no explicit one — a pre-trade review has no fill yet.
        unit = price or (current or {}).get("current_price")
        if not (unit and quantity and currency):
            return ctx

        value_native = float(unit) * float(quantity)
        value_krw = fx_provider.convert(value_native, currency, "KRW", spot)
        if value_krw is None:
            return ctx

        signed = value_krw if (side or "buy") == "buy" else -value_krw
        ctx["trade_value_krw"] = round(value_krw, 2)
        ctx["trade_size_pct_of_net_worth"] = round(value_krw / net_worth, 6)

        own_cash = balances.get(currency, 0.0)
        ctx["trade_size_pct_of_cash"] = (
            round(value_native / own_cash, 6) if own_cash > 1e-9 else None
        )
        # True when this currency's cash cannot cover the purchase, so won would
        # have to be converted — the hidden second decision.
        ctx["requires_conversion"] = bool(
            (side or "buy") == "buy" and value_native > own_cash + 1e-9
        )

        after = ((current or {}).get("market_value_krw") or 0.0) + signed
        ctx["position_weight_after"] = round(max(after, 0.0) / net_worth, 6)

        # FX exposure after: a foreign purchase raises it, a foreign sale lowers
        # it, and a domestic trade leaves it alone.
        before_exposure = totals.get("fx_exposure")
        if before_exposure is not None:
            delta = signed if currency != cs.BASE_CURRENCY else 0.0
            ctx["fx_exposure_after"] = round(
                min(max((before_exposure * net_worth + delta) / net_worth, 0.0), 1.0), 6
            )
        return ctx
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[coach] position sizing unavailable: {e}")
        return {}


async def portfolio_risk_context(
    ticker: str | None, side: str | None, trade_size_pct: float | None,
) -> dict:
    """
    The fifth pillar: how this decision interacts with **portfolio-level** risk
    — VaR, concentration, and correlation to what is already held — not just
    this position's own size (that is ``position_context``'s job).

    Reads the exact same snapshot ``GET /portfolio/risk`` shows the user
    (``services.portfolio_risk``, TTL-cached), so a number the coach cites is
    never a different computation than the dashboard's. ``risk_warnings`` are
    rule-based, computed here in Python rather than left to the LLM — the same
    "compute in Python, interpret in the prompt" split as every other pillar —
    so a concrete concentration/correlation flag cannot be silently dropped by
    a model that decides not to mention it.

    Never raises: an unavailable snapshot yields an empty dict and the prompt
    simply has no portfolio-risk section to comment on.
    """
    from services import portfolio_risk as pr

    try:
        snap = await pr.build_snapshot()
    except Exception as e:  # noqa: BLE001 — risk context degrades, never fails
        logger.warning(f"[coach] portfolio risk snapshot unavailable: {e}")
        return {}

    if snap.get("portfolio_volatility") is None:
        return {"note": (snap.get("data_quality") or {}).get("note")}

    ctx: dict = {
        "portfolio_volatility": snap.get("portfolio_volatility"),
        "value_at_risk_95": snap.get("value_at_risk"),
        "conditional_var_95": snap.get("conditional_var"),
        "average_correlation": snap.get("average_correlation"),
        "concentration": snap.get("concentration"),
        "data_sufficient": (snap.get("data_quality") or {}).get("sufficient"),
    }

    t = (ticker or "").strip().upper() or None
    warnings: list[str] = []

    position = (
        next((p for p in snap.get("positions", []) if p["ticker"] == t), None)
        if t else None
    )
    if position:
        ctx["current_position_risk"] = position
        share = position.get("risk_contribution_pct")
        weight = position.get("weight")
        # A position punching well above its capital weight in risk terms —
        # blueprint §2's headline concentration signal.
        if share is not None and weight is not None and weight > 1e-6:
            if share > weight * 1.5 and share > 0.20:
                warnings.append(
                    f"{t} is already {weight:.1%} of net worth but carries "
                    f"{share:.1%} of total portfolio risk — a concentration the "
                    f"position size alone does not show."
                )

    corr_row = (snap.get("correlation_matrix") or {}).get(t) if t else None
    if corr_row:
        others = {k: v for k, v in corr_row.items() if k != t}
        ctx["correlation_to_holdings"] = others
        high = {k: v for k, v in others.items() if v is not None and v > 0.75}
        if high:
            pairs = ", ".join(f"{k} ({v:.2f})" for k, v in high.items())
            warnings.append(
                f"{t} is highly correlated (>0.75) with {pairs} — adding it "
                f"does not diversify the book, it concentrates it."
            )

    if t and side and trade_size_pct:
        delta = trade_size_pct if side == "buy" else -trade_size_pct
        try:
            sim = await pr.simulate_trade(t, delta)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[coach] risk scenario simulation failed: {e}")
            sim = None
        if sim and sim.get("volatility_after") is not None:
            ctx["simulated_impact"] = sim
            before, after = sim["volatility_before"], sim["volatility_after"]
            if before > 1e-9 and (after - before) / before > 0.10:
                warnings.append(
                    f"This trade would raise annualized portfolio volatility "
                    f"from {before:.1%} to {after:.1%} "
                    f"({(after - before) / before:+.0%})."
                )

    if (ctx.get("average_correlation") or 0) > 0.75:
        warnings.append(
            f"The existing book's average pairwise correlation is "
            f"{ctx['average_correlation']:.2f} — diversification across current "
            f"holdings is already limited."
        )

    ctx["risk_warnings"] = warnings
    return ctx


def _cited_date_lists(report) -> list[tuple[object, str]]:
    """
    Every ``(owner, attribute)`` on a report that holds a list of cited dates.

    Collected in one place on purpose. Each new report field that can carry a
    date is a new place for the model to fabricate one, and a verifier that names
    its fields inline silently stops covering the report as the report grows.
    Adding a field here is the only step needed to bring it under verification.
    """
    refs: list[tuple[object, str]] = []
    for bias in getattr(report, "detected_biases", None) or []:
        refs.append((bias, "past_occurrences"))
    for pattern in getattr(report, "recurring_patterns", None) or []:
        refs.append((pattern, "occurrences"))
    return refs


def real_dates(journal: list[dict], flows: list[dict] | None = None) -> set[str]:
    """
    Every date the coach is allowed to cite.

    Trades **and** cash flows: once the coach can talk about deposits and
    conversions, those are a second place to invent a date, and a claim about a
    환전 must be held to exactly the same standard as one about a trade.
    """
    dates = {
        (t.get("executed_at") or "")[:10] for t in journal if t.get("executed_at")
    }
    dates |= {
        (f.get("occurred_at") or "")[:10] for f in (flows or []) if f.get("occurred_at")
    }
    return {d for d in dates if d}


def verify_citations(report, journal: list[dict],
                     flows: list[dict] | None = None) -> list[str]:
    """
    Strip any cited date that does not correspond to a real journal entry.

    The prompt forbids inventing dates, but "the prompt says not to" is not a
    guarantee — and a fabricated "you did this on 2026-03-14" is exactly the
    failure that would destroy the user's trust in the coach. Returns the list
    of dates that were removed, so the caller can log or surface them.

    Works on any report shape: it verifies whatever :func:`_cited_date_lists`
    reports, so ``CoachReport`` and ``JournalReport`` are covered by the same
    pass.
    """
    allowed = real_dates(journal, flows)
    removed: list[str] = []

    for owner, attr in _cited_date_lists(report):
        kept = []
        for d in getattr(owner, attr) or []:
            day = (d or "").strip()[:10]
            if _DATE_RE.fullmatch(day) and day in allowed:
                kept.append(day)
            else:
                removed.append(d)
        setattr(owner, attr, kept)

    if removed:
        logger.warning(
            f"[coach] dropped {len(removed)} fabricated trade date(s): {removed}"
        )
        report.data_limitations.append(
            "Some cited dates did not match the journal or the cash ledger and "
            "were removed."
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


# =============================================================================
# Retrospective review — blueprint §3, phase 8
# =============================================================================
# Reviewing a trade the user already made means knowing what happened next. That
# is the value and also the danger: a coach that says "you were wrong, the price
# fell" teaches outcome-chasing, the exact habit this system exists to fight.
#
#                 | good outcome                  | bad outcome
#   good process  | repeat it                     | BAD LUCK — change nothing
#   bad process   | DANGEROUS — a bad habit paid  | fix it
#
# The two off-diagonal cells are where the coaching value is, and they are what a
# naive review destroys. So the judgement is split across TWO passes: pass 1 sees
# only what existed at the trade's timestamp and scores the process; pass 2 sees
# the outcome and may not revise pass 1. Asking one call to "ignore the outcome"
# does not work — the outcome is in its context and it rationalizes backwards.


class _OutcomeVerdict(AgentReport):
    """Pass 2's narrow output. Deliberately cannot express a process score."""

    # `AgentReport.agent` is required with no default, and pass 2's prompt does
    # not ask for it — the field is stamped by `_generate_report` anyway.
    agent: str = "trading_coach"

    outcome_summary: str = Field(
        "", description="What the price actually did, stated plainly"
    )
    luck_vs_skill: str = Field(
        "", description="Which of the four quadrants this trade fell in"
    )
    hindsight_note: str = Field(
        "", description="Why process and outcome are judged separately here"
    )


_RETRO_PROCESS_PROMPT = """\
You are a trading coach reviewing a decision the user ALREADY MADE. Your job in
this pass is to judge the QUALITY OF THEIR REASONING — nothing else.

THE TRADE UNDER REVIEW may instead be a NON-TRADE REFLECTION (look for a
"[note/...]" or "[pass/...]" tag ahead of the ticker) — a dilemma, a decision
to pass, or a market note, with no execution and no fill. Judge the reasoning
the same way; just do not refer to a position size, a fill, or a cost basis
that does not exist.

CRITICAL: You have deliberately NOT been told what happened after this trade.
You cannot know, and you must not guess. Judge the decision only against the
information that existed at the moment it was made, which is all you have been
given. A decision can be excellent and still lose money.

ABSOLUTE RULES:
- NEVER invent a past trade. Every date in `past_occurrences` MUST appear in the
  journal below. The journal has been truncated to trades made BEFORE this one;
  that is intentional, and it is all the history that existed at the time.
- NEVER invent a number. Cite only figures present in the data below.
- Do NOT speculate about what happened next. If you find yourself writing "this
  probably worked out" or "the stock likely fell", delete it.
- If the history is too short to establish a pattern, say so and set
  `historical_pattern` to null.
- The rationale-type labels come from a crude keyword match, not a psychological
  assessment. Treat them as a weak hint.

WHAT TO DO:
- Set `process_quality` (0-100): was this reasoning sound GIVEN WHAT WAS
  KNOWABLE? 100 = the rationale engaged with the actual evidence available;
  0 = it contradicted or ignored it.
- Fill `what_was_knowable`: state what the data available at that timestamp
  actually said. This is the standard the decision is being held to, so make it
  concrete and checkable.
- Fill `rationale_evaluation`: the user's stated logic against that evidence.
- Detect biases only where you can evidence them, quoting the user's own words.
- Be direct but not moralizing. A well-reasoned trade deserves to be told it was
  well reasoned, whatever became of it.
- Judge the EXECUTION of this decision, not the trading style itself. A fast,
  high-conviction breakout entry is not automatically "unsound reasoning" just
  because it is aggressive — judge whether it was well-executed FOR that style
  (confirmation used, risk sized, a plan for being wrong), not against a more
  conservative style the user was never attempting.

Output ONLY a single JSON object:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentences on how you reached this assessment>",
  "process_quality": <int 0-100>,
  "what_was_knowable": "<what the data said at the time>",
  "rationale_evaluation": "<their logic vs. that evidence>",
  "detected_biases": [
    {"bias": "<name>", "evidence": "<quote their words>",
     "past_occurrences": ["YYYY-MM-DD", ...], "severity": "mild|moderate|strong"}
  ],
  "historical_pattern": "<what their prior history shows, or null>",
  "coaching_feedback": "<direct, actionable guidance>",
  "alignment_score": <int 0-100, same meaning as process_quality here>,
  "supporting_data_points": ["<specific figure you used>", ...],
  "data_limitations": ["<what you could not see>", ...]
}
"""


_RETRO_PROCESS_TEMPLATE = """\
=== THE TRADE UNDER REVIEW (already executed) ===
{subject}
=== END TRADE ===

=== THE USER'S STATED RATIONALE, AS WRITTEN AT THE TIME ===
{rationale}
=== END RATIONALE ===

=== FUNDAMENTAL ANALYST REPORT AS IT STOOD AT THAT TIME ===
{fundamental}
=== END FUNDAMENTAL ===

=== TECHNICAL ANALYST REPORT AS IT STOOD AT THAT TIME ===
{technical}
=== END TECHNICAL ===

=== THE USER'S JOURNAL UP TO THAT MOMENT ({prior_count} earlier trades) ===
{journal}
=== END JOURNAL ===

=== POSITION & SIZING AT THAT TIME (computed, in KRW) ===
{sizing}
=== END SIZING ===

Judge the reasoning. You do not know what happened next.
"""


_RETRO_OUTCOME_PROMPT = """\
You are a trading coach. A judgement of this decision's REASONING has already
been made, WITHOUT knowledge of what happened afterwards. It is given to you
below and it is FINAL.

You now see what the price actually did. Your only job is to describe that
outcome and place the trade in one of four quadrants.

ABSOLUTE RULES:
- You MUST NOT revise, soften, or contradict the process judgement. A sound
  decision that lost money is still a sound decision. An unsound decision that
  made money is still unsound — and more dangerous, because it just got
  rewarded.
- NEVER invent a number. Use only the outcome figures given.
- If no horizon has elapsed yet, say plainly that it is too early to tell and
  leave the quadrant unresolved. Do not fill the silence.

`luck_vs_skill` MUST be exactly one of:
  "good process, good outcome"  — repeat it
  "good process, bad outcome"   — bad luck; change nothing about the process
  "bad process, good outcome"   — the most dangerous cell; a bad habit was paid
  "bad process, bad outcome"    — fix the process
  "too early to tell"           — no horizon has elapsed

`hindsight_note` explains, in terms specific to THIS trade, why the process and
the outcome are scored separately.

Output ONLY a single JSON object:
{
  "confidence": <float 0-1>,
  "reasoning": "<1-3 sentences>",
  "outcome_summary": "<what the price did over 7/30/90 days>",
  "luck_vs_skill": "<one of the five strings above>",
  "hindsight_note": "<why process and outcome are judged apart, for this trade>"
}
"""


_RETRO_OUTCOME_TEMPLATE = """\
=== THE TRADE ===
{subject}

=== THE PROCESS JUDGEMENT (final — do not revise) ===
process_quality: {process_quality}
what_was_knowable: {what_was_knowable}
rationale_evaluation: {rationale_evaluation}

=== WHAT ACTUALLY HAPPENED ===
{outcomes}
=== END ===

Describe the outcome and name the quadrant.
"""


# =============================================================================
# Whole-journal review — phase 9
# =============================================================================

_JOURNAL_PROMPT = """\
You are a trading coach reviewing this user's ENTIRE trading record at once.

This is not a series of individual trade reviews. Answer only the questions that
exist at the level of the whole record:
  - Which behaviours actually RECUR, and are they getting better or worse?
  - Does good reasoning actually pay off for this user, or not?
  - What advice was given in earlier reviews, and what did the user then do?
  - When DIARY / REFLECTION ENTRIES are present: did PASSING or HESITATING pay
    off, or cost them? A "pass" followed by a big rally is a real, coachable
    outcome — so is a "pass" followed by a further decline that validated it.
    Cross-reference each diary entry's ticker against THE USER'S TRADING
    JOURNAL to tell whether it was ENTRY hesitation (ticker not held at the
    time) or EXIT hesitation (already held — did NOT selling pay off, or did
    hope-holding a loser cost them?). These are diary entries recorded via
    "Observe"/"Contemplating"/"Note" on the SAME ticker they hold or don't —
    do not assume every reflection is about buying.

STYLE — the single most important directive in this prompt:
- ARCHETYPE below describes the trading style this user's own closed trades
  already show. NEVER use `priorities` to push them toward a different
  disposition — do not tell an aggressive momentum trader to become a passive
  value investor, or a patient value investor to chase momentum. Every
  priority must help them execute THEIR OWN style better, not a different one.
- Where the data supports it, phrase a `priority` as a testable rule: condition
  → execution criteria → stop/exit boundary → expected payoff (cite the real
  win rate/expectancy from BEHAVIOURAL SUMMARY). This is what "formalize a
  Golden Rule" means in practice — a vague "manage risk better" is not one.
- When FUNDAMENTAL MANAGER SYNTHESIS is present (a single-ticker scope), ground
  at least one observation in it — the Manager's verdict, the peer valuation,
  or the forensic QoE read — not only in the user's own trading behaviour.

ABSOLUTE RULES:
- NEVER invent a trade or a diary entry. Every date in `occurrences` MUST appear
  in the journal OR the diary entries below.
- NEVER invent a number.
- A pattern needs at least two dated occurrences. One event is an anecdote; do
  not call it a pattern.
- If the behavioural summary says the history is insufficient, say the record is
  too short to establish tendencies and return NO recurring patterns.
- The rationale-type labels come from a crude keyword match. Weak hint only.
- Do not moralize or lecture in the abstract. Point at specific decisions.

WHAT TO DO:
- `strengths` is REQUIRED whenever anything in the record was done well. A review
  that lists only faults gets read once and then avoided, which costs the user
  more than any single missed correction.
- `priorities` is AT MOST 3, most important first. A list of twelve things to fix
  is a list of zero things that will be fixed.
- `process_vs_outcome`: across reviewed trades, did the well-reasoned ones
  actually do better? If the record cannot yet say, say that.
- `advice_followed`: compare earlier reviews against what the user subsequently
  logged. Name specifics. Null if there are no earlier reviews.
- When CURRENT PORTFOLIO RISK is present, ground any concentration or
  diversification observation in its actual numbers (VaR, correlation,
  risk_contribution_pct vs. weight) — not a general impression. If
  `risk_warnings` is non-empty, treat at least the most important one as
  priority material. Never invent a number this section does not contain.

Output ONLY a single JSON object:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentences>",
  "scope_description": "<exactly what you reviewed>",
  "trades_reviewed": <int>,
  "period": "<YYYY-MM-DD..YYYY-MM-DD or null>",
  "recurring_patterns": [
    {"pattern": "<name>", "occurrences": ["YYYY-MM-DD", ...],
     "trend": "worsening|stable|improving", "evidence": "<quote the user>"}
  ],
  "process_vs_outcome": "<does good reasoning pay off here?>",
  "advice_followed": "<what was warned about, and what happened, or null>",
  "strengths": ["<what this user does well>", ...],
  "priorities": ["<at most three, most important first>", ...],
  "data_limitations": ["<what you could not see>", ...]
}
"""


_JOURNAL_TEMPLATE = """\
=== SCOPE ===
{scope}
=== END SCOPE ===

=== THE USER'S TRADING JOURNAL (real logged trades + what followed) ===
{journal}
=== END JOURNAL ===

=== DIARY / REFLECTION ENTRIES (non-trade: passes, dilemmas, notes — with the RAW price move afterwards, not signed by a direction that never executed) ===
{diary}
=== END DIARY ===

=== YOUR TRADING ARCHETYPE (computed from your own closed trades — descriptive, not a target) ===
{archetype}
=== END ARCHETYPE ===

=== FUNDAMENTAL MANAGER SYNTHESIS (tool-retrieved on demand — only present when scoped to one ticker) ===
{fundamental_synthesis}
=== END SYNTHESIS ===

=== BEHAVIOURAL SUMMARY (computed, not estimated) ===
{patterns}
=== END SUMMARY ===

=== EARLIER COACHING REVIEWS (what this user was already told) ===
{prior_reviews}
=== END REVIEWS ===

=== CURRENT PORTFOLIO RISK (computed — VaR/CVaR/correlation over the WHOLE book, as of TODAY) ===
{risk}
=== END PORTFOLIO RISK ===

Review this record.
"""


class CoachAgent(BaseAgent):
    """Evaluates the user's reasoning against objective data and their history."""

    @property
    def agent_id(self) -> str:
        return "trading_coach"

    async def analyze(self, context: dict, capture: dict | None = None) -> CoachReport:
        """
        Args:
            context: ``ticker``, ``entry_rationale``, optional ``proposed_side`` /
                     ``proposed_quantity`` / ``emotion_tag``.

                     ``proposed_side`` is the single control: 'buy'/'sell' is a
                     trade being considered; 'observe'/'contemplating'/'note'
                     is a non-executed reflection — a dilemma or a decision to
                     pass — and ``proposed_quantity`` is then ignored.

                     The fundamental/peer/technical digest is retrieved by this
                     method itself, via :func:`fetch_fundamental_analysis` — a
                     caller no longer pre-fetches ``sec_report``/
                     ``technical_report`` from the debate store.
        """
        ticker = (context.get("ticker") or "").strip().upper() or None
        rationale = (context.get("entry_rationale") or "").strip()
        side = context.get("proposed_side")
        qty = context.get("proposed_quantity")
        emotion_tag = context.get("emotion_tag")

        # `observe`/`contemplating` are direction-agnostic in the DB — the
        # same two values cover "should I buy this" and "should I sell this".
        # Disambiguate from data, not from a separate field the client would
        # have to manage: a reflection on a ticker the user ALREADY HOLDS is
        # almost always about EXITING (take profit, cut a loser, hold through
        # noise); on one they don't hold, it's about ENTERING. This is exactly
        # the "매도 관망/매도 고민" case — recorded with the same `side` value,
        # framed correctly because the holding already tells us which it is.
        is_held = bool(ticker and portfolio_service.get_holding(ticker))

        if side in ("buy", "sell") and qty:
            proposed = " ".join(
                str(x) for x in [side, qty, ticker] if x not in (None, "")
            )
        elif side in ("observe", "contemplating", "note") and is_held:
            label = {
                "observe": "Watching an existing position in — without selling —",
                "contemplating": "Contemplating whether to sell",
                "note": "A market note about",
            }.get(side, "Reflecting on")
            proposed = f"{label} {ticker}" if ticker else f"{label} the market"
        elif side in ("observe", "contemplating", "note"):
            label = {
                "observe": "Observing/passing on",
                "contemplating": "Contemplating opening a position in",
                "note": "A market note about",
            }.get(side, "Reflecting on")
            proposed = f"{label} {ticker}" if ticker else f"{label} the market"
        else:
            proposed = "(no specific trade — general review)"

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

        # ── Tool-augmented fundamental grounding: retrieved on demand, only
        # when a ticker is actually named — never stuffed in regardless. ──
        fundamental_digest = (
            fetch_fundamental_analysis(ticker) if ticker
            else _condense_fundamental_digest(status="no_ticker")
        )
        archetype = journal_analysis.archetype_for()

        sizing = await position_context(ticker, side, qty)
        risk_ctx = await portfolio_risk_context(
            ticker, side, sizing.get("trade_size_pct_of_net_worth")
        )

        # ── The user's own empirical playbook, computed in Python. ──
        from services import trading_rules

        proposed_segment = {
            "rationale_type": journal_analysis.classify_rationale(rationale),
            "strategy_type": journal_analysis.classify_strategy(rationale),
            "emotion_tag": (emotion_tag or "untagged"),
        }
        toxic_matches = trading_rules.match_active_rules("toxic", proposed_segment)
        golden_matches = trading_rules.match_active_rules("golden", proposed_segment)

        user_prompt = _USER_TEMPLATE.format(
            proposed=proposed,
            rationale=rationale or "(The user gave no rationale for this trade.)",
            fundamental_synthesis=json.dumps(
                fundamental_digest, ensure_ascii=False, indent=2, default=str
            ),
            archetype=json.dumps(archetype, ensure_ascii=False, indent=2, default=str),
            journal=journal_text,
            patterns=json.dumps(patterns, ensure_ascii=False, indent=2, default=str),
            sizing=(
                json.dumps(sizing, ensure_ascii=False, indent=2, default=str)
                if sizing else
                "(Not available — the portfolio could not be valued, so do not "
                "comment on position size.)"
            ),
            risk=(
                json.dumps(risk_ctx, ensure_ascii=False, indent=2, default=str)
                if risk_ctx else
                "(Not available — portfolio risk could not be computed, so do "
                "not comment on VaR, correlation, or risk contribution.)"
            ),
            playbook=json.dumps(
                {"proposed_segment": proposed_segment,
                 "toxic_pattern_matches": toxic_matches,
                 "golden_setup_matches": golden_matches},
                ensure_ascii=False, indent=2, default=str,
            ),
        )

        if capture is not None:
            capture["raw_data"] = user_prompt

        report = await self._generate_report(CoachReport, _SYSTEM_PROMPT, user_prompt)

        # ── Enforce what the prompt only asked for. ──
        from services import cash_service as cs
        verify_citations(report, real_entries, flows=cs.list_flows(limit=200))

        # Guarantee every computed risk flag / rule match survives regardless of
        # whether the model chose to mention it — the same enforcement pattern
        # as history_sufficient below.
        report.risk_warnings = risk_ctx.get("risk_warnings", [])
        report.toxic_pattern_matches = toxic_matches
        report.golden_setup_matches = golden_matches

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

        # Be explicit about a missing fundamental pillar: silence here would
        # read as "the coach checked the fundamentals and had no concerns".
        if ticker and fundamental_digest.get("status") == "no_prior_analysis":
            report.data_limitations.append(
                f"No Deep Analysis has been run for {ticker}, so this review "
                f"is based on your trading journal alone — not the company's "
                f"fundamentals, peer valuation, or price action. Run a Deep "
                f"Analysis for a fuller picture."
            )
        return report

    # =====================================================================
    # Retrospective review of a trade already logged
    # =====================================================================

    async def analyze_retrospective(
        self, trade_id: int, capture: dict | None = None
    ) -> CoachReport:
        """
        Review a decision the user has already made.

        Two passes, for the reason set out above this class: pass 1 judges the
        reasoning against only what existed at ``executed_at``; pass 2 sees the
        outcome and may not revise pass 1. The separation is enforced by what is
        put in each prompt, not by asking the model to be disciplined.

        Raises ``ValueError`` if the trade does not exist.
        """
        from rag import history_store

        trade = portfolio_service.get_trade(trade_id)
        if trade is None:
            raise ValueError(f"No journal entry with id {trade_id}.")

        ticker = (trade.get("ticker") or "").strip().upper() or None
        rationale = (trade.get("entry_rationale") or "").strip()
        executed_at = trade.get("executed_at")
        executed = journal_analysis._parse_dt(executed_at)
        if executed is None:
            raise ValueError(
                f"Trade {trade_id} has an unreadable executed_at ({executed_at!r})."
            )

        if portfolio_service.is_trade_entry(trade):
            subject = (
                f"{trade.get('side')} {trade.get('quantity')} {ticker} "
                f"@ {trade.get('execution_price')} on {executed_at}"
            )
        else:
            subject = (
                f"[{trade.get('entry_type')}/{trade.get('side') or 'reflection'}] "
                f"{ticker or 'general market note'} "
                f"(benchmark price {trade.get('execution_price')}) on {executed_at}"
            )

        # ── Pass 1 input: nothing that postdates the trade. ──────────────
        prior = await journal_analysis.trade_outcomes(
            limit=_MAX_JOURNAL_ROWS, before=executed
        )
        prior = [r for r in prior
                 if r.get("id") != trade_id and not r.get("is_opening_entry")
                 and r.get("entry_type") == "trade"]

        # The reports as they stood then, not as they stand now. A current
        # technical report already knows which way the price went.
        as_of_record = history_store.analysis_as_of(ticker, executed) if ticker else None
        as_of_reports = (as_of_record or {}).get("reports") or {}
        data_as_of = (as_of_record or {}).get("run_id")

        fundamental = _compact_report(as_of_reports.get("sec_filings"), _SEC_FIELDS)
        technical = _compact_report(
            as_of_reports.get("technical_analysis"), _TECH_FIELDS
        )

        sizing = await position_context(
            ticker, trade.get("side"), trade.get("quantity"),
            price=trade.get("execution_price"),
        )
        pass1_prompt = _RETRO_PROCESS_TEMPLATE.format(
            subject=subject,
            sizing=(
                json.dumps(sizing, ensure_ascii=False, indent=2, default=str)
                if sizing else "(Not available.)"
            ),
            rationale=rationale or "(No rationale was recorded for this trade.)",
            fundamental=fundamental,
            technical=technical,
            prior_count=len(prior),
            journal=(
                json.dumps(prior, ensure_ascii=False, indent=2, default=str)
                if prior else
                "(No trades had been logged before this one. You must not cite "
                "any past trade.)"
            ),
        )
        if capture is not None:
            capture["raw_data"] = pass1_prompt

        report = await self._generate_report(
            CoachReport, _RETRO_PROCESS_PROMPT, pass1_prompt
        )

        # ── Pass 2 input: the outcome, plus pass 1 as a fixed premise. ───
        subject_row = await journal_analysis.outcomes_for_trade(trade_id)
        outcomes = (subject_row or {}).get("outcomes") or {}
        elapsed = any(o.get("return") is not None for o in outcomes.values())

        if elapsed:
            pass2_prompt = _RETRO_OUTCOME_TEMPLATE.format(
                subject=subject,
                process_quality=report.process_quality,
                what_was_knowable=report.what_was_knowable or "(not stated)",
                rationale_evaluation=report.rationale_evaluation,
                outcomes=json.dumps(outcomes, ensure_ascii=False, indent=2,
                                    default=str),
            )
            try:
                verdict = await self._generate_report(
                    _OutcomeVerdict, _RETRO_OUTCOME_PROMPT, pass2_prompt,
                    max_output_tokens=2048,
                )
                report.outcome_summary = verdict.outcome_summary or None
                report.luck_vs_skill = verdict.luck_vs_skill or None
                report.hindsight_note = verdict.hindsight_note or None
            except Exception as e:  # noqa: BLE001 — pass 1 still stands alone
                logger.error(f"[coach] outcome pass failed for trade {trade_id}: {e}")
                report.data_limitations.append(
                    "The outcome could not be summarized; only the process "
                    "review below is available."
                )
        else:
            # Do not let the model fill this silence with a guess.
            report.outcome_summary = (
                "No outcome horizon has elapsed yet — it is too early to say what "
                "this trade achieved."
            )
            report.luck_vs_skill = "too early to tell"

        # ── Enforce what the prompts only asked for. ─────────────────────
        from services import cash_service as cs
        verify_citations(report, prior, flows=cs.list_flows(limit=200))

        report.review_type = "retrospective"
        report.trade_id = trade_id
        report.ticker = ticker
        report.proposed_action = subject
        report.data_as_of = data_as_of

        # Sufficiency is measured over the history that existed AT THE TIME, not
        # over the journal as it stands now — otherwise an old trade inherits
        # confidence from trades made after it.
        report.history_sufficient = len(prior) >= journal_analysis.MIN_TRADES_FOR_PATTERN
        if not report.history_sufficient:
            report.historical_pattern = None
            note = (
                f"Only {len(prior)} trade(s) had been logged before this one — "
                f"too few to establish a behavioural pattern."
            )
            if note not in report.data_limitations:
                report.data_limitations.append(note)

        if ticker and as_of_record is None:
            report.data_limitations.append(
                f"No analysis of {ticker} had been run before this trade, so this "
                f"review rests on the rationale and the journal alone. The current "
                f"reports were deliberately NOT used — they already know what the "
                f"price did next."
            )
        return report

    # =====================================================================
    # Whole-journal review
    # =====================================================================

    async def analyze_journal(
        self, scope: dict | None = None, capture: dict | None = None
    ) -> JournalReport:
        """
        Review the user's entire record rather than one decision.

        ``scope`` accepts ``ticker``, ``since`` (ISO date) and ``limit``. This is
        not a loop over single-trade reviews: it looks for what recurs, whether
        good reasoning has actually paid, and what earlier advice was ignored —
        none of which a per-trade review can see.
        """
        from services import review_store

        scope = scope or {}
        ticker = (scope.get("ticker") or "").strip().upper() or None
        since = (scope.get("since") or "").strip() or None
        limit = scope.get("limit") or _MAX_JOURNAL_ROWS

        rows = await journal_analysis.trade_outcomes(ticker=ticker, limit=limit)
        journal = [
            r for r in rows
            if not r.get("is_opening_entry") and r.get("entry_type") == "trade"
        ]
        diary = [r for r in rows if r.get("entry_type") != "trade"]
        if since:
            journal = [r for r in journal if (r.get("executed_at") or "") >= since]
            diary = [r for r in diary if (r.get("executed_at") or "") >= since]

        patterns = await journal_analysis.pattern_summary(ticker=ticker)
        archetype = journal_analysis.archetype_for(ticker=ticker)

        # Tool-augmented fundamental grounding — only when this review is
        # scoped to one company; a portfolio-wide review has no single ticker
        # to fetch a digest for. Always "as of now", which is correct here:
        # unlike a retrospective review of a PAST trade, a whole-journal
        # review is inherently a review as of today.
        fundamental_digest = (
            fetch_fundamental_analysis(ticker) if ticker
            else _condense_fundamental_digest(status="no_ticker")
        )

        # What the coach has already said. This is the only source for
        # `advice_followed`, and it is why every review is persisted.
        prior_reviews = [
            {
                "reviewed_at": r.get("created_at"),
                "review_type": r.get("review_type"),
                "ticker": r.get("ticker"),
                "trade_id": r.get("trade_id"),
                "rationale_at_the_time": r.get("rationale_snapshot"),
                "what_the_coach_said": {
                    k: (r.get("report") or {}).get(k)
                    for k in ("coaching_feedback", "detected_biases",
                              "alignment_score", "process_quality",
                              "luck_vs_skill")
                    if (r.get("report") or {}).get(k) is not None
                },
            }
            for r in review_store.list_reviews(ticker=ticker, limit=20)
            if r.get("review_type") != "journal"
        ]

        dates = sorted((r.get("executed_at") or "")[:10] for r in journal
                       if r.get("executed_at"))
        period = f"{dates[0]}..{dates[-1]}" if dates else None
        scope_text = (
            f"{'All companies' if not ticker else ticker}"
            f"{f', trades from {since} onward' if since else ''}"
            f" — {len(journal)} logged trade(s)"
            f"{f' spanning {period}' if period else ''}."
        )

        risk_ctx = await portfolio_risk_context(ticker, None, None)
        user_prompt = _JOURNAL_TEMPLATE.format(
            scope=scope_text,
            journal=(
                json.dumps(journal, ensure_ascii=False, indent=2, default=str)
                if journal else
                "(The journal is EMPTY for this scope. You must not cite any "
                "past trade.)"
            ),
            diary=(
                json.dumps(diary, ensure_ascii=False, indent=2, default=str)
                if diary else
                "(No diary/reflection entries for this scope.)"
            ),
            archetype=json.dumps(archetype, ensure_ascii=False, indent=2, default=str),
            fundamental_synthesis=json.dumps(
                fundamental_digest, ensure_ascii=False, indent=2, default=str
            ),
            patterns=json.dumps(patterns, ensure_ascii=False, indent=2, default=str),
            prior_reviews=(
                json.dumps(prior_reviews, ensure_ascii=False, indent=2, default=str)
                if prior_reviews else
                "(No earlier reviews exist. Set `advice_followed` to null.)"
            ),
            risk=(
                json.dumps(risk_ctx, ensure_ascii=False, indent=2, default=str)
                if risk_ctx else
                "(Not available — portfolio risk could not be computed.)"
            ),
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        report = await self._generate_report(
            JournalReport, _JOURNAL_PROMPT, user_prompt, max_output_tokens=6144
        )

        # ── Enforce what the prompt only asked for. ──────────────────────
        from services import cash_service as cs
        verify_citations(report, journal + diary, flows=cs.list_flows(limit=200))

        report.risk_warnings = risk_ctx.get("risk_warnings", [])
        report.scope_description = scope_text
        report.trades_reviewed = len(journal)
        report.period = period

        # A cap the model is asked for and not trusted with: a long list of fixes
        # is a list nobody acts on, so it is enforced here.
        if len(report.priorities) > _MAX_PRIORITIES:
            report.priorities = report.priorities[:_MAX_PRIORITIES]

        # A "pattern" needs more than one dated occurrence. After verification
        # stripped unverifiable dates, some may no longer clear that bar.
        report.recurring_patterns = [
            p for p in report.recurring_patterns if len(p.occurrences) >= 2
        ]

        report.history_sufficient = bool(patterns.get("sufficient"))
        if not report.history_sufficient:
            report.recurring_patterns = []
            note = patterns.get("note") or "Too few logged trades to establish a pattern."
            if note not in report.data_limitations:
                report.data_limitations.append(note)
        if not prior_reviews:
            report.advice_followed = None
        return report
