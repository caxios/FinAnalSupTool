"""
services.portfolio_risk
────────────────────────
Assembles the objective, whole-portfolio risk snapshot — current holdings and
cash from SQLite, base- and local-currency price history, FX history — then
calls ``risk_metrics.compute_portfolio_risk``. Pure data orchestration, no LLM.

This is the ONE place the network fetch for portfolio risk happens. Both
``GET /portfolio/risk`` and the Trading Coach (``agents/coach_agent.py``) call
:func:`build_snapshot` directly, so the number a user sees on the dashboard and
the number the coach cites in a review are always the identical computation —
mirroring the "reuse the existing engine, don't reproduce it" pattern already
used for Deep Analysis. This module was extracted from
``agents/quant_risk_agent.py`` (which now builds its LLM interpretation on top
of the same snapshot) and from the quant-risk block that used to run inside
``services/pipeline.py``.

A short in-memory TTL cache avoids re-downloading yfinance/FX history on every
call: a coach review in the same session as a dashboard load should not double
the network cost, and holdings rarely change faster than a few minutes.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

from providers import fx_provider, price_provider
from services import cash_service as cs
from services import portfolio_service as ps
from services import risk_metrics

logger = logging.getLogger(__name__)

# ~1 year of trading days — enough for a stable covariance estimate without
# letting a regime from years ago dominate. Matches quant_risk_agent's window.
_HISTORY_DAYS = 400

# What-if scenarios are computed for the top risk contributors only, plus one
# currency-conversion lever — a scenario per position would add cost without
# adding insight.
_SCENARIO_COUNT = 2
_SCENARIO_DELTA = 0.05

_CACHE_TTL_SECONDS = 300.0

# Module-level cache: the computed snapshot dict, the raw return-series context
# `simulate_trade` needs to answer an ad-hoc "what if" without a re-fetch, the
# key they were computed for, and when.
_cache: dict | None = None
_ctx: dict | None = None
_cache_key: tuple | None = None
_cache_time: float = 0.0


async def _cash_in_base() -> dict[str, float]:
    """
    Cash balances converted to base currency, keyed by the currency actually
    held — the same conversion the removed pipeline.py quant-risk block did.
    The key selects which return series prices the column (won: none, dollars:
    the exchange rate's own), the value is what it is worth in won.
    """
    cash_base: dict[str, float] = {}
    try:
        balances = cs.balances()
        spot = None
        if any(c != cs.BASE_CURRENCY and abs(v) > 1e-9 for c, v in balances.items()):
            spot = (await fx_provider.fetch_spot()).rate
        for currency, amount in balances.items():
            if abs(amount) < 1e-9:
                continue
            converted = fx_provider.convert(amount, currency, cs.BASE_CURRENCY, spot)
            if converted is not None:
                cash_base[currency] = converted
    except Exception as e:  # noqa: BLE001 — cash is additive; never fail the snapshot
        logger.warning(f"[portfolio_risk] could not read cash balances: {e}")
    return cash_base


def _cache_key_for(holdings: list[dict], cash: dict[str, float],
                   confidence: float, start: str, end: str) -> tuple:
    return (
        tuple(sorted((h.get("ticker"), h.get("quantity")) for h in holdings)),
        tuple(sorted(cash.items())),
        confidence, start, end,
    )


async def build_snapshot(
    confidence: float = 0.95,
    start: str | None = None,
    end: str | None = None,
    use_cache: bool = True,
) -> dict:
    """
    Every field from ``risk_metrics.compute_portfolio_risk``, plus ``scenarios``
    (what-if position/conversion changes on the top risk contributors) and
    ``excluded_tickers`` (holdings with no usable price history).

    Never raises: a fetch failure degrades to whatever ``compute_portfolio_risk``
    reports for missing data, matching the rest of this app's "an empty/partial
    portfolio is a normal state" convention.
    """
    global _cache, _ctx, _cache_key, _cache_time

    holdings = ps.list_holdings()
    cash = await _cash_in_base()

    today = date.today()
    end = end or today.isoformat()
    start = start or (today - timedelta(days=_HISTORY_DAYS)).isoformat()

    key = _cache_key_for(holdings, cash, confidence, start, end)
    now = time.monotonic()
    if (use_cache and _cache is not None and _cache_key == key
            and now - _cache_time < _CACHE_TTL_SECONDS):
        return _cache

    tickers = [h.get("ticker") for h in holdings if h.get("ticker")]
    currencies = {t: ps.resolve_asset_currency(t) for t in tickers}
    base = cs.BASE_CURRENCY

    # BOTH series: base-currency prices drive the risk model (so the
    # stock/exchange-rate correlation sits inside the returns), and the native
    # ones drive the hedged comparison in `fx_risk`.
    local_prices, dropped = await price_provider.fetch_price_history(
        tickers, start, end
    ) if tickers else (None, [])
    try:
        prices, dropped = await price_provider.fetch_price_history_base(
            tickers, start, end, currencies, base=base
        ) if tickers else (None, [])
    except Exception as e:  # noqa: BLE001 — fall back rather than fail the snapshot
        logger.warning(
            f"[portfolio_risk] base-currency series unavailable ({e}); "
            f"falling back to native prices."
        )
        prices = local_prices

    fx_returns = None
    try:
        fx = await fx_provider.fetch_fx_history(start, end)
        if not fx.empty:
            fx_returns = fx.pct_change().dropna()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[portfolio_risk] no FX history for risk model: {e}")

    metrics = risk_metrics.compute_portfolio_risk(
        holdings, prices, confidence=confidence, cash=cash,
        fx_returns=fx_returns, base_currency=base, local_prices=local_prices,
    )

    # What-if scenarios on the biggest risk contributors (blueprint §2's "how
    # does changing this position alter total risk"), funded from the pocket
    # the money would really come from.
    scenarios: list[dict] = []
    ctx: dict | None = None
    if metrics.get("positions") and prices is not None and not prices.empty:
        rets = risk_metrics.daily_returns(prices)
        cash_rets = risk_metrics.cash_return_columns(cash, fx_returns, rets.index, base)
        if not cash_rets.empty:
            rets = pd.concat([rets, cash_rets], axis=1).dropna(how="any")
        cols = list(rets.columns)
        weight_by = {p["ticker"]: p["weight"] for p in metrics["positions"]}
        weight_by.update({
            f"{risk_metrics.CASH_PREFIX}{c['currency']}": c["weight"]
            for c in metrics.get("cash_positions", [])
        })
        w = np.array([weight_by.get(c, 0.0) for c in cols], dtype=float)
        ctx = {"rets": rets, "w": w, "currencies": currencies, "base": base}

        for p in metrics["positions"][:_SCENARIO_COUNT]:
            scenarios.append(risk_metrics.simulate_position_change(
                rets, w, p["ticker"], _SCENARIO_DELTA,
                asset_currency=currencies.get(p["ticker"]), base_currency=base,
            ))
        for c in metrics.get("cash_positions", []):
            if c["currency"] != base and c["weight"] > 0:
                scenarios.append(risk_metrics.simulate_conversion(
                    rets, w, c["currency"], 0.5, base_currency=base
                ))
                break

    snapshot = {**metrics, "scenarios": scenarios, "excluded_tickers": dropped}
    _cache, _ctx, _cache_key, _cache_time = snapshot, ctx, key, now
    return snapshot


async def simulate_trade(
    ticker: str, delta_weight: float, asset_currency: str | None = None,
) -> dict | None:
    """
    What a proposed change in ``ticker``'s portfolio weight would do to total
    volatility — the same covariance-based scenario ``build_snapshot`` runs for
    the top risk contributors, but for an arbitrary trade under review.

    Only meaningful for a ticker that is ALREADY held (it must be a column in
    the fetched return series) — a brand-new position has no price history in
    this snapshot to simulate against. Returns ``None`` in that case, or when
    no risk context is available at all (e.g. an empty portfolio); callers
    should fall back to the simpler net-worth-weight arithmetic in
    ``coach_agent.position_context`` for those cases.
    """
    await build_snapshot()  # ensure the cache (and _ctx) is fresh
    if _ctx is None:
        return None
    t = (ticker or "").strip().upper()
    if t not in _ctx["rets"].columns:
        return None
    return risk_metrics.simulate_position_change(
        _ctx["rets"], _ctx["w"], t, delta_weight,
        asset_currency=asset_currency or _ctx["currencies"].get(t),
        base_currency=_ctx["base"],
    )
