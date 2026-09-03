"""
services.performance
────────────────────
Net-worth history and the two returns that mean different things.

Why two returns
───────────────
**Time-weighted (TWR)** breaks the series at every external cash flow and chains
the sub-period returns. It measures return *per unit of capital* — the user's
selection, unaffected by when they happened to deposit.

**Money-weighted (MWR)** is the IRR of the dated deposits and withdrawals plus
the ending value. It measures what the user's *money* actually did.

They diverge exactly when the timing of deposits was good or bad, and that
divergence is itself worth showing. Reporting only one of them answers a
question the user did not ask: deposit ₩5M into a flat ₩10M account and a
naive "return" reads +50%.

**Conversions are internal and must not break the TWR series.** If a 환전 counted
as an external flow, every currency swap would appear as performance the user
never earned. That is why :data:`cash_service.EXTERNAL_FLOWS` holds only
deposits and withdrawals.

What this module cannot do
──────────────────────────
The series can only begin where the ledger begins. Seeded positions have no real
acquisition date — that is why they are seeds — so nothing before the opening
anchor is reconstructible. Every result carries ``coverage_start`` and callers
must label the chart with it rather than drawing a confident line through
history that was never recorded.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from services import cash_service as cs
from services import portfolio_service as ps

logger = logging.getLogger(__name__)


# IRR is not guaranteed to converge, and a sign-changing flow series can have
# several roots. These bound the search rather than letting it wander.
_IRR_MAX_ITER = 100
_IRR_TOLERANCE = 1e-7
_IRR_BOUNDS = (-0.9999, 10.0)   # -99.99% to +1000% annualized

_WINDOWS = {"1m": 30, "3m": 90, "6m": 180, "1y": 365, "all": None}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


# =============================================================================
# Net-worth series
# =============================================================================

async def net_worth_series(
    start: str | None = None, end: str | None = None
) -> tuple[pd.DataFrame, dict]:
    """
    Daily net worth, reconstructed by replaying the journal and the ledger.

    For each date: the quantity held that day times that day's close, plus the
    cash balance as of that day — **each converted at that day's rate, not
    today's**. Using the current rate for historical cash would rewrite the past
    every time the currency moves.

    Returns ``(frame, meta)``; the frame is empty when there is nothing to
    reconstruct, and ``meta`` always explains why.
    """
    from providers import fx_provider, price_provider

    anchor = cs.opening_timestamp()
    trades = ps.list_trades()
    holdings = ps.list_holdings()
    meta: dict = {
        "coverage_start": (anchor or "")[:10] or None,
        "note": None,
    }

    if not holdings and not cs.list_flows(limit=1):
        meta["note"] = "Nothing has been recorded yet."
        return pd.DataFrame(), meta

    first_activity = min(
        [t["executed_at"] for t in trades] + ([anchor] if anchor else [])
    ) if (trades or anchor) else None
    start_date = (start or (first_activity or "")[:10]) or None
    end_date = end or datetime.now(timezone.utc).date().isoformat()
    if not start_date:
        meta["note"] = "No dated activity to reconstruct from."
        return pd.DataFrame(), meta

    tickers = [h["ticker"] for h in holdings]
    currencies = {t: ps.resolve_asset_currency(t) for t in tickers}

    # Native closes plus the daily USDKRW series, so each day is valued at the
    # rate that applied on it.
    prices, dropped = await price_provider.fetch_price_history(
        tickers, start_date, end_date
    ) if tickers else (pd.DataFrame(), [])
    try:
        fx = await fx_provider.fetch_fx_history(start_date, end_date)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[performance] no FX history: {e}")
        fx = pd.Series(dtype=float)

    if prices.empty and fx.empty:
        meta["note"] = "No market data was available for this window."
        return pd.DataFrame(), meta

    index = prices.index if not prices.empty else fx.index
    if not fx.empty:
        index = index.intersection(fx.index)
    if len(index) == 0:
        meta["note"] = "Price history and exchange-rate history share no dates."
        return pd.DataFrame(), meta

    # Quantity held per ticker per day, from the journal.
    qty_by_day = _quantity_series(trades, index)

    records = []
    for day in index:
        day_iso = day.date().isoformat()
        as_of = f"{day_iso}T23:59:59+00:00"
        rate = float(fx.loc[day]) if (not fx.empty and day in fx.index) else None

        equity_krw = equity_usd = 0.0
        for ticker in tickers:
            qty = qty_by_day.get(ticker, {}).get(day, 0.0)
            if qty <= 0 or prices.empty or ticker not in prices.columns:
                continue
            price = float(prices.at[day, ticker])
            native = qty * price
            if currencies[ticker] == "KRW":
                equity_krw += native
                if rate:
                    equity_usd += native / rate
            else:
                equity_usd += native
                if rate:
                    equity_krw += native * rate

        balances = cs.balances(as_of=as_of)
        cash_krw = balances.get("KRW", 0.0) + (
            balances.get("USD", 0.0) * rate if rate else 0.0
        )
        cash_usd = balances.get("USD", 0.0) + (
            balances.get("KRW", 0.0) / rate if rate else 0.0
        )

        records.append({
            "date": day,
            "equity_krw": equity_krw, "equity_usd": equity_usd,
            "cash_krw": cash_krw, "cash_usd": cash_usd,
            "net_worth_krw": equity_krw + cash_krw,
            "net_worth_usd": equity_usd + cash_usd,
            "fx_rate": rate,
        })

    frame = pd.DataFrame(records).set_index("date")
    meta["observations"] = len(frame)
    if dropped:
        meta["dropped_tickers"] = dropped
    if anchor is None:
        meta["note"] = (
            "No opening cash balance has been recorded, so cash is counted from "
            "zero and net worth reflects positions only."
        )
    return frame, meta


def _quantity_series(trades: list[dict], index) -> dict[str, dict]:
    """Shares held per ticker on each date in ``index``, from the journal."""
    events: dict[str, list[tuple[datetime, float]]] = {}
    for t in trades:
        when = _parse(t["executed_at"])
        if when is None:
            continue
        delta = float(t["quantity"]) * (1 if t["side"] == "buy" else -1)
        events.setdefault(t["ticker"], []).append((when, delta))

    out: dict[str, dict] = {}
    for ticker, rows in events.items():
        rows.sort(key=lambda r: r[0])
        per_day, running, i = {}, 0.0, 0
        for day in index:
            edge = day.to_pydatetime().replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
            while i < len(rows) and rows[i][0] <= edge:
                running += rows[i][1]
                i += 1
            per_day[day] = running
        out[ticker] = per_day
    return out


# =============================================================================
# Returns
# =============================================================================

def time_weighted_return(series: pd.Series, flows: list[dict],
                         currency: str = "KRW") -> dict:
    """
    Chain-linked return, broken at each external cash flow.

    Each sub-period's return is measured on its **pre-flow** value, so money
    arriving on a given day is not credited with that day's gain. Only deposits
    and withdrawals break the chain — a conversion or a trade appearing here
    would show up as performance the user never earned.
    """
    if series is None or len(series) < 2:
        return {"cumulative": None, "annualized": None,
                "note": "Not enough history to measure a return."}

    by_day: dict[date, float] = {}
    for f in flows:
        when = _parse(f["occurred_at"])
        if when is None:
            continue
        amount = float(f["amount"])
        if currency == "KRW":
            amount *= float(f["fx_to_krw"])
        elif f["currency"] == "KRW":
            rate = float(f["fx_to_krw"]) or 1.0
            amount = amount / rate if rate else amount
        by_day[when.date()] = by_day.get(when.date(), 0.0) + amount

    factor, previous = 1.0, float(series.iloc[0])
    for day, value in list(series.items())[1:]:
        flow = by_day.get(day.date(), 0.0)
        # The flow lands during the day, so the period's gain is measured before
        # it: (end - flow) / start.
        if previous > 1e-9:
            factor *= (float(value) - flow) / previous
        previous = float(value)

    cumulative = factor - 1.0
    days = (series.index[-1] - series.index[0]).days or 1
    annualized = (factor ** (365.0 / days) - 1.0) if factor > 0 else None
    return {
        "cumulative": round(cumulative, 6),
        "annualized": round(annualized, 6) if annualized is not None else None,
        "days": days,
        "note": None,
    }


def money_weighted_return(flows: list[dict], ending_value: float,
                          ending_date: datetime, currency: str = "KRW") -> dict:
    """
    IRR over the dated external flows plus the ending value (XIRR).

    Newton-Raphson with a bisection fallback. IRR does not always converge and a
    sign-changing series can have several roots, so a failure returns ``None``
    with a note rather than a number that looks authoritative and is not.
    """
    points: list[tuple[datetime, float]] = []
    for f in flows:
        when = _parse(f["occurred_at"])
        if when is None:
            continue
        amount = float(f["amount"])
        if currency == "KRW":
            amount *= float(f["fx_to_krw"])
        elif f["currency"] == "KRW":
            rate = float(f["fx_to_krw"]) or 1.0
            amount = amount / rate if rate else amount
        # A deposit is money INTO the portfolio, i.e. out of the investor's
        # pocket — negative from the investor's point of view.
        points.append((when, -amount))
    if not points:
        return {"cumulative": None, "annualized": None,
                "note": "No deposits or withdrawals to measure against."}

    points.append((ending_date, float(ending_value)))
    points.sort(key=lambda p: p[0])
    t0 = points[0][0]
    years = [((w - t0).days / 365.0) for w, _ in points]
    amounts = [a for _, a in points]

    if not (min(amounts) < 0 < max(amounts)):
        return {"cumulative": None, "annualized": None,
                "note": "The flows never change sign, so no rate of return exists."}

    def npv(r: float) -> float:
        return sum(a / ((1.0 + r) ** y) for a, y in zip(amounts, years))

    rate = 0.1
    for _ in range(_IRR_MAX_ITER):
        value = npv(rate)
        if abs(value) < _IRR_TOLERANCE:
            break
        derivative = sum(
            -y * a / ((1.0 + rate) ** (y + 1)) for a, y in zip(amounts, years)
        )
        if abs(derivative) < 1e-12:
            rate = None
            break
        step = value / derivative
        rate -= step
        if rate <= _IRR_BOUNDS[0] or rate >= _IRR_BOUNDS[1]:
            rate = None
            break
    else:
        rate = None

    if rate is None:                       # bisection fallback
        low, high = _IRR_BOUNDS
        if npv(low) * npv(high) > 0:
            return {"cumulative": None, "annualized": None,
                    "note": "The internal rate of return did not converge."}
        for _ in range(_IRR_MAX_ITER):
            mid = (low + high) / 2
            if npv(low) * npv(mid) <= 0:
                high = mid
            else:
                low = mid
        rate = (low + high) / 2

    span = years[-1] or (1.0 / 365.0)
    cumulative = (1.0 + rate) ** span - 1.0
    return {
        "cumulative": round(cumulative, 6),
        "annualized": round(rate, 6),
        "days": int(span * 365),
        "note": None,
    }


# =============================================================================
# The whole picture
# =============================================================================

async def performance_report(window: str = "all") -> dict:
    """Everything ``GET /portfolio/performance`` returns, in both currencies."""
    days = _WINDOWS.get(window, None)
    start = None
    if days:
        start = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

    frame, meta = await net_worth_series(start=start)
    flows = cs.external_flows(since=start)

    result: dict = {
        "window": window,
        "coverage_start": meta.get("coverage_start"),
        "note": meta.get("note"),
        "observations": meta.get("observations", 0),
        "series": [],
        "twr": {}, "mwr": {},
        "realized": _realized_totals(start),
    }
    if frame.empty:
        return result

    result["series"] = [
        {"date": idx.date().isoformat(), **{k: (None if pd.isna(v) else round(float(v), 4))
                                            for k, v in row.items()}}
        for idx, row in frame.iterrows()
    ]
    ending = frame.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)

    for unit, column in (("krw", "net_worth_krw"), ("usd", "net_worth_usd")):
        series = frame[column].dropna()
        ccy = unit.upper()
        result["twr"][unit] = time_weighted_return(series, flows, currency=ccy)
        result["mwr"][unit] = money_weighted_return(
            flows, float(series.iloc[-1]) if len(series) else 0.0, ending, currency=ccy
        )
    return result


def _realized_totals(since: str | None = None) -> dict:
    """Realized P/L, FX gains, and the friction paid — in both currencies."""
    conn = ps.db.get_connection()

    sql = "SELECT SUM(realized_pnl) a, SUM(realized_pnl_base) b FROM trades WHERE side='sell'"
    params: list = []
    if since:
        sql += " AND executed_at >= ?"
        params.append(since)
    row = conn.execute(sql, params).fetchone()

    fsql = ("SELECT flow_type, SUM(-amount) native, SUM(-amount * fx_to_krw) krw"
            " FROM cash_flows WHERE flow_type IN ('fee','tax')")
    fparams: list = []
    if since:
        fsql += " AND occurred_at >= ?"
        fparams.append(since)
    fsql += " GROUP BY flow_type"
    friction = {
        r["flow_type"]: {"native": round(float(r["native"] or 0), 4),
                         "krw": round(float(r["krw"] or 0), 4)}
        for r in conn.execute(fsql, fparams).fetchall()
    }

    xsql = "SELECT SUM(realized_fx_pnl_krw) v FROM cash_flows WHERE realized_fx_pnl_krw IS NOT NULL"
    xparams: list = []
    if since:
        xsql += " AND occurred_at >= ?"
        xparams.append(since)
    fx_pnl = conn.execute(xsql, xparams).fetchone()["v"]

    return {
        "realized_pnl_native": round(float(row["a"]), 4) if row["a"] is not None else None,
        "realized_pnl_krw": round(float(row["b"]), 4) if row["b"] is not None else None,
        "realized_fx_pnl_krw": round(float(fx_pnl), 4) if fx_pnl is not None else None,
        "fees": friction.get("fee", {"native": 0.0, "krw": 0.0}),
        "taxes": friction.get("tax", {"native": 0.0, "krw": 0.0}),
        # Average cost, not FIFO. Korean tax treatment of overseas gains is
        # FIFO-based, so these are decision-support figures, not tax figures.
        "basis": "average_cost",
    }
