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
