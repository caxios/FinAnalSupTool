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


async def trade_outcomes(ticker: str | None = None, limit: int | None = 50) -> list[dict]:
    """
    Every trade joined to what the price did afterwards.

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
    from providers import price_provider   # local import keeps startup light

    trades = portfolio_service.list_trades(ticker=ticker, limit=limit)
    if not trades:
        return []

    now = datetime.now(timezone.utc)
    out: list[dict] = []

    for t in trades:
        executed = _parse_dt(t.get("executed_at"))
        entry = t.get("execution_price")
        row = {
            "id": t.get("id"),
            "ticker": t.get("ticker"),
            "side": t.get("side"),
            "quantity": t.get("quantity"),
            "executed_at": t.get("executed_at"),
            "execution_price": entry,
            "entry_rationale": t.get("entry_rationale"),
            "rationale_type": classify_rationale(t.get("entry_rationale")),
            "is_opening_entry": portfolio_service.is_opening_entry(t),
            "outcomes": {},
        }

        if executed and entry:
            for days in OUTCOME_HORIZONS_DAYS:
                target = executed + timedelta(days=days)
                key = f"{days}d"
                if target > now:
                    row["outcomes"][key] = {
                        "price": None, "return": None,
                        "note": "horizon has not elapsed yet",
                    }
                    continue
                try:
                    res = await price_provider.fetch_execution_price(
                        row["ticker"], target
                    )
                    later = res.price
                    raw = (later - float(entry)) / float(entry)
                    # Sign by intent: a sell is "right" when the price falls.
                    signed = raw if row["side"] == "buy" else -raw
                    row["outcomes"][key] = {
                        "price": later,
                        "return": round(signed, 6),
                        "note": None,
                    }
                except Exception as e:  # noqa: BLE001 — one horizon failing is fine
                    logger.warning(
                        f"[journal] outcome lookup failed for "
                        f"{row['ticker']} +{days}d: {e}"
                    )
                    row["outcomes"][key] = {
                        "price": None, "return": None,
                        "note": "price lookup failed",
                    }
        out.append(row)

    return out


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
