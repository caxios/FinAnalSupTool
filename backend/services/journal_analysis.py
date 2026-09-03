"""
services.journal_analysis
─────────────────────────
Deterministic pre-processing of the trading journal for the Coach agent.

Why this module exists
──────────────────────
The coach's whole value is claims like "the last three times you sold on a
technical break, you missed a rebound". A claim like that is either grounded in
real rows or it is a fabrication — and a fabricated pattern is worse than
silence, because the user may size a position on it. So the joining, counting,
and outcome measurement happen HERE, in Python, against real trades and real
prices. The LLM is handed the resulting structure and asked to interpret it; it
is never asked to remember or reconstruct the history itself.

This is the same division of labour as ``services.risk_metrics``: numbers from
pandas, narrative from the model.

Cold start
──────────
Below :data:`MIN_TRADES_FOR_PATTERN` logged trades there is no pattern to find.
:func:`pattern_summary` says so explicitly rather than reporting a "100% win
rate" off two trades, and the agent's prompt requires it to pass that admission
through to the user.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from services import portfolio_service

logger = logging.getLogger(__name__)


# Fewer trades than this and any "pattern" is noise. Two winning trades is not
# a strategy, and telling a user it is would be actively harmful.
MIN_TRADES_FOR_PATTERN = 5

# Quartile labels for `outcome_by_size_quartile`, smallest first.
_SIZE_QUARTILES = ("smallest", "small", "large", "largest")

# Horizons at which we measure what happened after each decision.
OUTCOME_HORIZONS_DAYS = (7, 30, 90)


# =============================================================================
# Rationale classification
# =============================================================================
# Kept as named constants so the classification is auditable and editable rather
# than buried in a regex. This is intentionally a crude lexical signal, NOT a
# psychological diagnosis — it exists to let the coach compare outcomes between
# trades the user described emotionally and ones they described analytically.
# The LLM is told this is a keyword heuristic so it doesn't over-trust it.

EMOTIONAL_MARKERS: tuple[str, ...] = (
    "fomo", "panic", "scared", "fear", "afraid", "greed", "greedy",
    "hype", "everyone", "revenge", "regret", "impulse", "gut", "feeling",
    "excited", "anxious", "worried", "yolo", "can't miss", "cant miss",
    "missing out", "bounce back", "hope", "hoping", "desperate",
    "쫄", "불안", "공포", "조급",           # common Korean equivalents
)

ANALYTICAL_MARKERS: tuple[str, ...] = (
    "valuation", "p/e", "pe ratio", "dcf", "margin", "cash flow", "revenue",
    "earnings", "guidance", "balance sheet", "debt", "moat", "thesis",
    "support", "resistance", "moving average", "rsi", "macd", "breakout",
    "target", "rebalance", "hedge", "allocation", "fundamental", "technical",
    "밸류", "실적", "펀더멘털",              # common Korean equivalents
)


def classify_rationale(text: str | None) -> str:
    """
    Label a rationale ``"emotional"``, ``"analytical"``, ``"mixed"``, or
    ``"none"`` from its wording.

    Deliberately simple and transparent. A rationale mentioning both a valuation
    and FOMO is "mixed" — which is the honest label, and often the most
    interesting one for coaching.
    """
    if not text or not text.strip():
        return "none"
    low = text.lower()
    has_emotion = any(m in low for m in EMOTIONAL_MARKERS)
    has_analysis = any(m in low for m in ANALYTICAL_MARKERS)
    if has_emotion and has_analysis:
        return "mixed"
    if has_emotion:
        return "emotional"
    if has_analysis:
        return "analytical"
    return "unclassified"


# A second, independent axis over the same rationale text: not WHETHER the
# wording is emotional, but WHAT KIND OF SETUP the user describes. Same crude
# keyword-heuristic status as `classify_rationale` above — this is what lets
# `edge_analytics` segment expectancy by "dip-buy" vs "momentum" etc., not a
# claim that the classifier understands trading strategy.
STRATEGY_MARKERS: dict[str, tuple[str, ...]] = {
    "valuation": (
        "undervalued", "cheap", "p/e", "pe ratio", "dcf", "fair value",
        "discount to", "book value", "intrinsic value", "value play",
        "밸류", "저평가",
    ),
    "technical_breakout": (
        "breakout", "broke resistance", "broke out", "breaking out",
        "52-week high", "52 week high", "new high", "volume surge",
        "resistance level", "돌파",
    ),
    "dip_buy": (
        "dip", "pullback", "oversold", "correction", "buy the dip",
        "bought the dip", "sell-off", "selloff", "저점", "눌림목",
    ),
    "momentum": (
        "momentum", "trend", "riding the trend", "strong move",
        "continuation", "uptrend", "상승세", "모멘텀",
    ),
}


def classify_strategy(text: str | None) -> str:
    """
    Label a rationale by SETUP TYPE: ``"valuation"``, ``"technical_breakout"``,
    ``"dip_buy"``, ``"momentum"``, ``"mixed"`` (more than one matched),
    ``"unclassified"`` (text present, none matched), or ``"none"`` (no text).

    A weak keyword hint, exactly like :func:`classify_rationale` — the coach's
    prompt is told this and must not over-trust it.
    """
    if not text or not text.strip():
        return "none"
    low = text.lower()
    hits = [name for name, markers in STRATEGY_MARKERS.items() if any(m in low for m in markers)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "mixed"
    return "unclassified"


# =============================================================================
# Outcomes
# =============================================================================

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def trade_outcomes(
    ticker: str | None = None,
    limit: int | None = 50,
    before: datetime | None = None,
) -> list[dict]:
    """
    Every trade joined to what the price did afterwards.

    ``before`` restricts the result to what was knowable at a past instant: only
    trades executed at or before it, and only outcome horizons that had already
    resolved by then. The retrospective coach depends on this — judging a
    decision's *process* is only honest if the judge cannot see what came after
    it, and that includes later trades and later horizons of earlier ones.

    For each trade we look up the price 7, 30, and 90 days later and compute the
    return **from the user's own fill**, signed by direction: a buy profits when
    the price rises, a sell "profits" (i.e. was correct) when the price falls,
    because avoiding a decline is what a good sell achieves.

    Horizons still in the future are reported as ``None`` rather than omitted —
    a trade from yesterday has no 90-day outcome yet, and saying so is more
    useful than silently dropping it.

    Network failures degrade to ``None`` outcomes; the journal entry itself is
    still returned so the coach can at least read the rationale.
    """
    if before is None:
        trades = portfolio_service.list_trades(ticker=ticker, limit=limit)
    else:
        # The SQL LIMIT would cut from the newest end, which is the end being
        # filtered away — so fetch the lot, then slice after the cutoff applies.
        rows = portfolio_service.list_trades(ticker=ticker)
        trades = [r for r in rows
                  if (d := _parse_dt(r.get("executed_at"))) and d <= before]
        if limit is not None:
            trades = trades[:limit]

    if not trades:
        return []

    # A horizon must have resolved by `before` to be knowable at that moment;
    # absent a cutoff, "knowable" just means it has resolved by now.
    horizon_cutoff = before or datetime.now(timezone.utc)
    out: list[dict] = []

    for t in trades:
        row = _journal_row(t)
        row["outcomes"] = await compute_outcomes(t, as_of=horizon_cutoff)
        out.append(row)

    return out


def _journal_row(trade: dict) -> dict:
    """The journal fields the coach reads, without the outcome lookup."""
    return {
        "id": trade.get("id"),
        "ticker": trade.get("ticker"),
        "side": trade.get("side"),
        "quantity": trade.get("quantity"),
        "executed_at": trade.get("executed_at"),
        "execution_price": trade.get("execution_price"),
        "entry_rationale": trade.get("entry_rationale"),
        "rationale_type": classify_rationale(trade.get("entry_rationale")),
        "is_opening_entry": portfolio_service.is_opening_entry(trade),
    }


async def compute_outcomes(trade: dict, as_of: datetime | None = None) -> dict:
    """
    What the price did 7/30/90 days after one trade, signed by direction.

    ``as_of`` is the moment the question is being asked from: a horizon landing
    after it is reported as unelapsed rather than looked up. Passing a past
    instant yields the outcomes that were knowable then — which is what keeps a
    retrospective process review free of hindsight.
    """
    from providers import price_provider   # local import keeps startup light

    cutoff = as_of or datetime.now(timezone.utc)
    executed = _parse_dt(trade.get("executed_at"))
    entry = trade.get("execution_price")
    ticker, side = trade.get("ticker"), trade.get("side")
    outcomes: dict = {}

    if not (executed and entry):
        return outcomes

    for days in OUTCOME_HORIZONS_DAYS:
        target = executed + timedelta(days=days)
        key = f"{days}d"
        if target > cutoff:
            outcomes[key] = {
                "price": None, "return": None,
                "note": "horizon has not elapsed yet",
            }
            continue
        try:
            res = await price_provider.fetch_execution_price(ticker, target)
            later = res.price
            raw = (later - float(entry)) / float(entry)
            # Sign by intent: a sell is "right" when the price falls.
            signed = raw if side == "buy" else -raw
            outcomes[key] = {
                "price": later, "return": round(signed, 6), "note": None,
            }
        except Exception as e:  # noqa: BLE001 — one horizon failing is fine
            logger.warning(
                f"[journal] outcome lookup failed for {ticker} +{days}d: {e}"
            )
            outcomes[key] = {
                "price": None, "return": None, "note": "price lookup failed",
            }
    return outcomes


async def outcomes_for_trade(trade_id: int) -> dict | None:
    """
    One journal entry joined to its outcomes, as of now.

    The retrospective coach's second pass needs exactly this and nothing else —
    fetching the whole ticker's journal to find one row would spend a price
    lookup per horizon per trade for a single answer.
    """
    trade = portfolio_service.get_trade(trade_id)
    if trade is None:
        return None
    row = _journal_row(trade)
    row["outcomes"] = await compute_outcomes(trade)
    return row


async def rationale_corpus(ticker: str | None = None, limit: int | None = 50) -> list[dict]:
    """
    ``(executed_at, side, rationale, outcome)`` tuples, newest first — the
    narrow view the coach reads when looking for repeated wording.

    Seeded opening entries are excluded: their "rationale" is boilerplate the
    app wrote, not something the user thought, and counting it would pollute
    every behavioural statistic.
    """
    rows = await trade_outcomes(ticker=ticker, limit=limit)
    return [
        {
            "executed_at": r["executed_at"],
            "ticker": r["ticker"],
            "side": r["side"],
            "rationale": r["entry_rationale"],
            "rationale_type": r["rationale_type"],
            "return_30d": (r["outcomes"].get("30d") or {}).get("return"),
        }
        for r in rows
        if not r["is_opening_entry"] and r["entry_rationale"]
    ]


# =============================================================================
# Patterns
# =============================================================================

def _win_rate(returns: list[float]) -> float | None:
    """Share of positive outcomes, or None when there is nothing to average."""
    usable = [r for r in returns if r is not None]
    if not usable:
        return None
    return round(sum(1 for r in usable if r > 0) / len(usable), 4)


async def pattern_summary(ticker: str | None = None) -> dict:
    """
    Behavioural statistics across the journal.

    The headline comparison is **win rate on emotionally-worded trades vs.
    analytically-worded ones** — the closest thing to an objective read on
    whether the user's stated reasoning tracks their results.

    Always returns a well-formed dict. When there is too little history,
    ``sufficient`` is False and ``note`` explains why, and the caller MUST
    surface that rather than reporting the (meaningless) numbers.
    """
    rows = await trade_outcomes(ticker=ticker)
    real = [r for r in rows if not r["is_opening_entry"]]

    summary: dict = {
        "total_trades": len(real),
        "seeded_positions": len(rows) - len(real),
        "buys": sum(1 for r in real if r["side"] == "buy"),
        "sells": sum(1 for r in real if r["side"] == "sell"),
        "with_rationale": sum(1 for r in real if r["entry_rationale"]),
        "rationale_types": {},
        "win_rate_30d": None,
        "win_rate_by_rationale_type": {},
        "average_holding_days": None,
        "sufficient": False,
        "note": "",
    }

    if not real:
        summary["note"] = (
            "No trades have been logged yet, so there is no behavioural history "
            "to analyze."
        )
        return summary

    # Rationale mix.
    for r in real:
        k = r["rationale_type"]
        summary["rationale_types"][k] = summary["rationale_types"].get(k, 0) + 1

    # Win rates overall and split by how the trade was described.
    returns_30 = [(r["outcomes"].get("30d") or {}).get("return") for r in real]
    summary["win_rate_30d"] = _win_rate(returns_30)

    by_type: dict[str, list] = {}
    for r in real:
        by_type.setdefault(r["rationale_type"], []).append(
            (r["outcomes"].get("30d") or {}).get("return")
        )
    summary["win_rate_by_rationale_type"] = {
        k: {"count": len(v), "win_rate_30d": _win_rate(v)}
        for k, v in by_type.items()
    }

    # Average holding period, from each sell back to the preceding buy on the
    # same ticker. A rough measure, but it is the one the user can act on.
    holds: list[float] = []
    by_ticker: dict[str, list] = {}
    for r in sorted(real, key=lambda x: x["executed_at"] or ""):
        by_ticker.setdefault(r["ticker"], []).append(r)
    for _tk, rs in by_ticker.items():
        open_buys: list[datetime] = []
        for r in rs:
            dt = _parse_dt(r["executed_at"])
            if not dt:
                continue
            if r["side"] == "buy":
                open_buys.append(dt)
            elif open_buys:
                holds.append((dt - open_buys.pop(0)).total_seconds() / 86400.0)
    if holds:
        summary["average_holding_days"] = round(sum(holds) / len(holds), 1)

    # ── Sizing and currency (phase 7) ──
    # All of it sits behind the same cold-start gate below. These statistics are
    # only as good as `classify_rationale`, which is a keyword match — they must
    # not acquire more authority than the classifier beneath them deserves.
    summary.update(await _sizing_statistics(real))

    # ── The cold-start gate. ──
    n = len(real)
    if n < MIN_TRADES_FOR_PATTERN:
        summary["sufficient"] = False
        summary["note"] = (
            f"Only {n} trade(s) logged — fewer than the {MIN_TRADES_FOR_PATTERN} "
            f"needed before any behavioural pattern is meaningful. Do NOT infer "
            f"a pattern from this; say the history is too short."
        )
    else:
        summary["sufficient"] = True
        summary["note"] = (
            f"{n} trades logged; behavioural statistics are based on realized "
            f"30-day outcomes where available."
        )
    return summary


# =============================================================================
# Sizing and currency behaviour (phase 7)
# =============================================================================

def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


async def _sizing_statistics(trades: list[dict]) -> dict:
    """
    How big the user's trades are, and whether size has been rewarded.

    ``outcome_by_size_quartile`` is the most useful figure here: it can tell a
    user, from their own record, whether their high-conviction sizing has
    historically worked. ``outcome_local_vs_base`` is the second — someone whose
    stock picks work but whose won-denominated results do not has a **currency**
    problem, not a selection problem, and no other view in the app can say so.

    Never raises: this decorates a summary that must always be well-formed.
    """
    from services import cash_service as cs
    from services import portfolio_service as ps

    out: dict = {
        "avg_trade_size_pct": None,
        "largest_trade_size_pct": None,
        "emotional_vs_analytical_sizing": {},
        "cash_deployment_pattern": None,
        "outcome_by_size_quartile": [],
        "conversion_timing": None,
        "outcome_local_vs_base": None,
    }
    if not trades:
        return out

    try:
        # Trade value as a share of the cash that existed just before it — the
        # only sizing denominator available for a PAST trade without replaying
        # market prices for every day in the journal.
        sized: list[dict] = []
        for t in trades:
            price, qty = t.get("execution_price"), t.get("quantity")
            if not price or not qty:
                continue
            currency = ps.resolve_asset_currency(t["ticker"])
            value = float(price) * float(qty)
            before = cs.balance(currency, as_of=t.get("executed_at"))
            # A buy is measured against the cash it consumed; below 1.0 means it
            # fitted inside the balance, 1.0 means it took everything.
            share = (value / before) if before > 1e-9 else None
            sized.append({
                "id": t.get("id"),
                "ticker": t["ticker"],
                "executed_at": t.get("executed_at"),
                "side": t.get("side"),
                "currency": currency,
                "value": round(value, 4),
                "pct_of_cash": round(min(share, 5.0), 6) if share is not None else None,
                "rationale_type": t.get("rationale_type"),
                "return_30d": (t.get("outcomes", {}).get("30d") or {}).get("return"),
            })

        shares = [s["pct_of_cash"] for s in sized if s["pct_of_cash"] is not None]
        out["avg_trade_size_pct"] = _mean(shares)
        out["largest_trade_size_pct"] = round(max(shares), 6) if shares else None

        # Does the way a trade was described track how much was staked on it?
        by_type: dict[str, list[float]] = {}
        for s in sized:
            if s["pct_of_cash"] is not None:
                by_type.setdefault(s["rationale_type"] or "unclassified", []).append(
                    s["pct_of_cash"]
                )
        out["emotional_vs_analytical_sizing"] = {
            k: {"count": len(v), "avg_size_pct": _mean(v)} for k, v in by_type.items()
        }

        # Do buys cluster when the balance is already low? Deploying the last of
        # the cash every time is a habit worth naming.
        buys = [s for s in sized if s["side"] == "buy" and s["pct_of_cash"] is not None]
        if buys:
            near_full = sum(1 for s in buys if s["pct_of_cash"] >= 0.8)
            out["cash_deployment_pattern"] = {
                "buys_measured": len(buys),
                "buys_using_80pct_or_more_of_cash": near_full,
                "share": round(near_full / len(buys), 4),
            }

        # Have the big ones actually worked out?
        with_outcome = [s for s in sized
                        if s["pct_of_cash"] is not None and s["return_30d"] is not None]
        if len(with_outcome) >= 4:
            with_outcome.sort(key=lambda s: s["pct_of_cash"])
            size = len(with_outcome)
            for i, label in enumerate(_SIZE_QUARTILES):
                lo, hi = (i * size) // 4, ((i + 1) * size) // 4
                bucket = with_outcome[lo:hi]
                if not bucket:
                    continue
                out["outcome_by_size_quartile"].append({
                    "quartile": label,
                    "count": len(bucket),
                    "avg_size_pct": _mean([s["pct_of_cash"] for s in bucket]),
                    "avg_return_30d": _mean([s["return_30d"] for s in bucket]),
                    "win_rate_30d": _win_rate([s["return_30d"] for s in bucket]),
                })

        # Did the exchange rate ever flip the sign of a realized result?
        realized = ps.db.get_connection().execute(
            "SELECT ticker, executed_at, realized_pnl, realized_pnl_base"
            " FROM trades WHERE side = 'sell' AND realized_pnl IS NOT NULL"
            "   AND realized_pnl_base IS NOT NULL"
        ).fetchall()
        if realized:
            flipped = [
                {"ticker": r["ticker"], "date": (r["executed_at"] or "")[:10],
                 "local": round(float(r["realized_pnl"]), 4),
                 "base": round(float(r["realized_pnl_base"]), 4)}
                for r in realized
                if (float(r["realized_pnl"]) > 0) != (float(r["realized_pnl_base"]) > 0)
            ]
            out["outcome_local_vs_base"] = {
                "sells_measured": len(realized),
                "sign_flipped_by_currency": len(flipped),
                "examples": flipped[:5],
                "note": (
                    "A sign flip means the trade made money in the stock's own "
                    "currency and lost it in won, or the reverse — a currency "
                    "outcome, not a selection one."
                ),
            }

        # Conversions: where the rate stood when the user converted, against the
        # range of rates they had actually seen.
        conversions = cs.list_flows(flow_type="fx_in")
        if conversions:
            rates = [float(f["fx_to_krw"]) for f in conversions if f["fx_to_krw"]]
            if rates:
                lo, hi = min(rates), max(rates)
                out["conversion_timing"] = {
                    "conversions": len(conversions),
                    "avg_rate": round(sum(rates) / len(rates), 4),
                    "range": [round(lo, 4), round(hi, 4)],
                    "dates": [(f["occurred_at"] or "")[:10] for f in conversions[:10]],
                }
    except Exception as e:  # noqa: BLE001 — decoration must not break the summary
        logger.warning(f"[journal] sizing statistics unavailable: {e}")
    return out


# =============================================================================
# Edge analytics (phase 5) — expectancy, disposition effect, MAE/MFE, rules
# =============================================================================
# Everything below operates on CLOSED round trips (a buy matched to the sell
# that closed it) rather than the 30-day-price-outcome proxy `pattern_summary`
# uses. A round trip's `realized_pnl_base` is what the user actually made or
# lost — the textbook inputs to Expectancy and Payoff Ratio are REALIZED
# results, not "what the price later did".
#
# BASE CURRENCY ONLY: `realized_pnl` is in the asset's own currency, which
# cannot be averaged across a KRW trade and a USD trade without silently
# mixing denominations. `realized_pnl_base` (KRW) is the only figure used here
# for exactly that reason — see `_sizing_statistics.outcome_local_vs_base`
# above, which exists because this distinction matters.

# Fewer closed trades than this in one segment (rationale/strategy/emotion
# bucket) and its expectancy is noise, not a pattern — distinct from
# MIN_TRADES_FOR_PATTERN, which gates the whole-journal statistics.
MIN_TRADES_PER_SEGMENT = 3

# How many golden/toxic candidates `synthesize_rules` proposes, strongest first.
_MAX_SYNTHESIZED_RULES = 5


def _expectancy_stats(pnls: list[float]) -> dict:
    """
    Win Rate, average gain/loss, Payoff Ratio, and Expectancy from a list of
    REALIZED P/L figures (all in the same currency — callers must ensure that).

        W = wins / total
        Payoff Ratio R = avg(gains) / avg(|losses|)
        Expectancy E = W * avg(gains) - (1 - W) * avg(|losses|)

    A trade with P/L exactly 0 counts as a loss (it did not profit) — the same
    convention `_win_rate` above uses (`r > 0` for a win).
    """
    n = len(pnls)
    if n == 0:
        return {
            "count": 0, "win_rate": None, "avg_gain": None, "avg_loss": None,
            "payoff_ratio": None, "expectancy": None,
        }
    gains = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]  # stored as positive magnitudes
    win_rate = len(gains) / n
    avg_gain = round(sum(gains) / len(gains), 4) if gains else None
    avg_loss = round(sum(losses) / len(losses), 4) if losses else None
    payoff_ratio = (
        round(avg_gain / avg_loss, 4)
        if avg_gain is not None and avg_loss is not None and avg_loss > 1e-9
        else None
    )
    expectancy = round(
        win_rate * (avg_gain or 0.0) - (1 - win_rate) * (avg_loss or 0.0), 4
    )
    return {
        "count": n, "win_rate": round(win_rate, 4),
        "avg_gain": avg_gain, "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio, "expectancy": expectancy,
    }


def _closed_round_trips(real_trades: list[dict]) -> list[dict]:
    """
    Match each sell to the buy it closed, FIFO per ticker — the same matching
    `pattern_summary`'s holding-days figure uses, kept here with the realized
    P/L and entry/exit prices attached so MAE/MFE and disposition effect can
    be computed from the SAME matched pairs (one matching pass, several
    statistics), rather than three separately-drifting reimplementations.

    Only sells with a resolvable `realized_pnl_base` are matched (a sell can
    still consume a FIFO buy slot even without one — the position DID close —
    but the pair is dropped from this list since none of the statistics below
    can use it without a base-currency figure).
    """
    by_ticker: dict[str, list[dict]] = {}
    for t in sorted(real_trades, key=lambda x: x.get("executed_at") or ""):
        by_ticker.setdefault(t["ticker"], []).append(t)

    trips: list[dict] = []
    for ticker, rows in by_ticker.items():
        open_buys: list[dict] = []
        for r in rows:
            if r.get("side") == "buy":
                open_buys.append(r)
            elif open_buys:
                buy = open_buys.pop(0)
                if r.get("realized_pnl_base") is None:
                    continue
                buy_dt, sell_dt = _parse_dt(buy.get("executed_at")), _parse_dt(r.get("executed_at"))
                if not (buy_dt and sell_dt):
                    continue
                pnl = float(r["realized_pnl_base"])
                trips.append({
                    "ticker": ticker,
                    "buy_id": buy.get("id"), "sell_id": r.get("id"),
                    "buy_at": buy.get("executed_at"), "sell_at": r.get("executed_at"),
                    "entry_price": buy.get("execution_price"),
                    "exit_price": r.get("execution_price"),
                    "holding_days": round((sell_dt - buy_dt).total_seconds() / 86400.0, 2),
                    "realized_pnl_base": pnl,
                    "is_win": pnl > 0,
                    "rationale_type": classify_rationale(buy.get("entry_rationale")),
                    "strategy_type": classify_strategy(buy.get("entry_rationale")),
                    "emotion_tag": buy.get("emotion_tag") or "untagged",
                })
    return trips


def disposition_effect(trips: list[dict]) -> dict:
    """
    Whether losers are held longer than winners — the classic loss-aversion
    trap: hoping a loser recovers while taking a winner's profit early.

    ``disposition_ratio`` = avg holding days (losers) / avg holding days
    (winners); ``flag`` is true above 2.0 (losers held twice as long).
    """
    winners = [t["holding_days"] for t in trips if t["is_win"]]
    losers = [t["holding_days"] for t in trips if not t["is_win"]]
    avg_w = _mean(winners)
    avg_l = _mean(losers)
    ratio = round(avg_l / avg_w, 3) if avg_w and avg_w > 1e-9 and avg_l is not None else None
    return {
        "avg_holding_days_winners": avg_w,
        "avg_holding_days_losers": avg_l,
        "winners_count": len(winners),
        "losers_count": len(losers),
        "disposition_ratio": ratio,
        "flag": bool(ratio is not None and ratio > 2.0),
        "note": (
            f"Losing trades are held {ratio:.1f}x as long as winning ones — "
            f"classic loss-aversion (hoping losers recover, taking winners' "
            f"profit early)." if ratio is not None and ratio > 2.0 else
            "No meaningful disposition-effect signal in the closed trades so far."
        ),
    }


async def mae_mfe_analysis(trips: list[dict]) -> dict:
    """
    Maximum Adverse/Favorable Excursion for each closed LONG round trip (this
    app's trading model has no shorting): the deepest drawdown and highest
    run-up the position actually experienced between entry and exit, as a
    return from the entry price.

    Derives two things a lecture cannot give the user:
      - **Empirical optimal stop-loss**: the 10th percentile of MAE among
        WINNING trades — the level 90% of eventual winners never crossed on
        the way to a profitable exit.
      - **Exit efficiency**: realized return / MFE — how much of the peak
        favorable move was actually captured before selling.

    A price-history fetch failure for one trade is skipped, not fatal — same
    degrade-gracefully convention as :func:`compute_outcomes`.
    """
    from providers import price_provider

    per_trade: list[dict] = []
    for trip in trips:
        entry, exitp = trip.get("entry_price"), trip.get("exit_price")
        if not entry or not trip.get("buy_at") or not trip.get("sell_at"):
            continue
        try:
            prices, _dropped = await price_provider.fetch_price_history(
                [trip["ticker"]], trip["buy_at"][:10], trip["sell_at"][:10],
            )
        except Exception as e:  # noqa: BLE001 — one trade's price gap is fine
            logger.warning(f"[journal] MAE/MFE fetch failed for {trip['ticker']}: {e}")
            continue
        if prices is None or prices.empty or trip["ticker"] not in prices.columns:
            continue
        series = prices[trip["ticker"]].dropna()
        if series.empty:
            continue

        returns = (series - float(entry)) / float(entry)
        mae, mfe = round(float(returns.min()), 6), round(float(returns.max()), 6)
        realized_return = (
            round((float(exitp) - float(entry)) / float(entry), 6) if exitp else None
        )
        exit_efficiency = (
            round(realized_return / mfe, 4)
            if realized_return is not None and mfe > 1e-9 else None
        )
        per_trade.append({
            "ticker": trip["ticker"], "buy_id": trip["buy_id"], "sell_id": trip["sell_id"],
            "buy_at": trip["buy_at"], "sell_at": trip["sell_at"], "is_win": trip["is_win"],
            "mae": mae, "mfe": mfe,
            "realized_return": realized_return, "exit_efficiency": exit_efficiency,
        })

    winners_mae = [t["mae"] for t in per_trade if t["is_win"]]
    optimal_stop = None
    if len(winners_mae) >= MIN_TRADES_PER_SEGMENT:
        import numpy as np
        optimal_stop = round(float(np.quantile(winners_mae, 0.10)), 4)

    efficiencies = [t["exit_efficiency"] for t in per_trade if t["exit_efficiency"] is not None]
    avg_efficiency = _mean(efficiencies)

    return {
        "trades": per_trade,
        "optimal_stop_loss": optimal_stop,
        "optimal_stop_loss_note": (
            f"90% of your winning trades never dipped below {optimal_stop:.1%} "
            f"from entry on the way to a profitable exit."
            if optimal_stop is not None else
            f"Not enough winning closed trades with price history yet "
            f"(need {MIN_TRADES_PER_SEGMENT}+) to derive an empirical stop-loss."
        ),
        "avg_exit_efficiency": avg_efficiency,
        "avg_exit_efficiency_note": (
            f"On average, you captured {avg_efficiency:.0%} of the peak "
            f"favorable move (MFE) before exiting."
            if avg_efficiency is not None else None
        ),
    }


def synthesize_rules(trips: list[dict], overall: dict) -> dict:
    """
    Groups closed round trips by (rationale_type, strategy_type, emotion_tag)
    and proposes GOLDEN (positive expectancy, win rate at/above the overall
    average) and TOXIC (negative expectancy) candidate rules.

    These are CANDIDATES, never auto-adopted — `POST /coach/rules` is the only
    way one becomes an active rule the pre-trade review checks against. A
    pattern from 3 trades is a hypothesis, not a law.
    """
    groups: dict[tuple[str, str, str], list[float]] = {}
    for t in trips:
        key = (t["rationale_type"], t["strategy_type"], t["emotion_tag"])
        groups.setdefault(key, []).append(t["realized_pnl_base"])

    overall_win_rate = overall.get("win_rate") or 0.0
    candidates = []
    for (rtype, stype, etag), pnls in groups.items():
        if len(pnls) < MIN_TRADES_PER_SEGMENT:
            continue
        stats = _expectancy_stats(pnls)
        if stats["expectancy"] is None:
            continue
        candidates.append({
            "conditions": {
                "rationale_type": rtype, "strategy_type": stype, "emotion_tag": etag,
            },
            **stats,
        })

    golden = sorted(
        (c for c in candidates
         if c["expectancy"] > 0 and (c["win_rate"] or 0) >= overall_win_rate),
        key=lambda c: c["expectancy"], reverse=True,
    )[:_MAX_SYNTHESIZED_RULES]
    toxic = sorted(
        (c for c in candidates if c["expectancy"] < 0),
        key=lambda c: c["expectancy"],
    )[:_MAX_SYNTHESIZED_RULES]
    return {"golden_candidates": golden, "toxic_candidates": toxic}


async def edge_analytics(ticker: str | None = None) -> dict:
    """
    The full quantitative edge picture: Expectancy/Payoff Ratio (overall and
    segmented by rationale/strategy/emotion), the Disposition Effect, the
    MAE/MFE empirical stop-loss, and Golden/Toxic rule candidates.

    Gated by :data:`MIN_TRADES_FOR_PATTERN` like every other behavioural
    statistic in this module — below it, this returns a well-formed but empty
    result with `sufficient: False` rather than a noisy figure from 2 trades.
    """
    trades = portfolio_service.list_trades(ticker=ticker)
    real = [t for t in trades if not portfolio_service.is_opening_entry(t)]

    result: dict = {
        "ticker": ticker,
        "total_trades": len(real),
        "closed_round_trips": 0,
        "excluded_missing_base_pnl": 0,
        "sufficient": False,
        "note": "",
        "overall": _expectancy_stats([]),
        "by_rationale_type": {},
        "by_strategy_type": {},
        "by_emotion_tag": {},
        "disposition_effect": {},
        "mae_mfe": {},
        "rule_candidates": {"golden_candidates": [], "toxic_candidates": []},
    }
    if len(real) < MIN_TRADES_FOR_PATTERN:
        result["note"] = (
            f"Only {len(real)} trade(s) logged — fewer than the "
            f"{MIN_TRADES_FOR_PATTERN} needed before edge analytics are "
            f"meaningful."
        )
        return result

    trips = _closed_round_trips(real)
    result["closed_round_trips"] = len(trips)
    if len(trips) < MIN_TRADES_FOR_PATTERN:
        result["note"] = (
            f"{len(real)} trade(s) logged but only {len(trips)} closed round "
            f"trip(s) with a resolvable base-currency P/L — fewer than the "
            f"{MIN_TRADES_FOR_PATTERN} needed. Close more positions to unlock "
            f"edge analytics."
        )
        return result

    pnls = [t["realized_pnl_base"] for t in trips]
    result["overall"] = _expectancy_stats(pnls)

    def _segment(field: str) -> dict:
        groups: dict[str, list[float]] = {}
        for t in trips:
            groups.setdefault(t[field], []).append(t["realized_pnl_base"])
        return {
            k: _expectancy_stats(v) for k, v in groups.items()
            if len(v) >= MIN_TRADES_PER_SEGMENT
        }

    result["by_rationale_type"] = _segment("rationale_type")
    result["by_strategy_type"] = _segment("strategy_type")
    result["by_emotion_tag"] = _segment("emotion_tag")
    result["disposition_effect"] = disposition_effect(trips)
    result["mae_mfe"] = await mae_mfe_analysis(trips)
    result["rule_candidates"] = synthesize_rules(trips, result["overall"])

    result["sufficient"] = True
    result["note"] = (
        f"{len(real)} trade(s) logged; {len(trips)} closed round trip(s) with "
        f"resolvable base-currency P/L back the statistics below."
    )
    return result
