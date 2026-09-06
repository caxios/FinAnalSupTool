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

# S&P 500 — the universal market benchmark for `beta`, fetched in base
# currency like every other series here so beta is measured against the same
# won-denominated risk this KRW-based portfolio actually carries. Best-effort:
# a fetch failure simply leaves `beta` null, the same degrade-gracefully
# convention as the FX history above it.
_BENCHMARK_TICKER = "^GSPC"

# The ~3.5% default `risk_metrics.sharpe_ratio` already uses; named here too
# so `simulate_any_trade` and `build_snapshot` agree without importing a
# private default from another module.
_RISK_FREE_ANNUAL = 0.035

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

    benchmark_returns = None
    try:
        bench_prices, _ = await price_provider.fetch_price_history_base(
            [_BENCHMARK_TICKER], start, end, {_BENCHMARK_TICKER: "USD"}, base=base
        )
        if bench_prices is not None and not bench_prices.empty:
            benchmark_returns = bench_prices[_BENCHMARK_TICKER].pct_change().dropna()
    except Exception as e:  # noqa: BLE001 — beta degrades to null, never fails the snapshot
        logger.warning(f"[portfolio_risk] no benchmark history for beta: {e}")

    metrics = risk_metrics.compute_portfolio_risk(
        holdings, prices, confidence=confidence, cash=cash,
        fx_returns=fx_returns, base_currency=base, local_prices=local_prices,
        benchmark_returns=benchmark_returns, risk_free_annual=_RISK_FREE_ANNUAL,
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


# =============================================================================
# What-If Position Simulator — Sharpe/Vol/MDD/VaR, holding or brand-new
# =============================================================================

# Below this many aligned trading days, a diversifying/concentrating verdict
# would be reading noise as signal — matches `risk_metrics.MIN_OBSERVATIONS`.
_VERDICT_VOL_THRESHOLD = 0.02          # 2 percentage points of annualized vol
_VERDICT_CORRELATION_THRESHOLD = 0.75


def _verdict(result: dict) -> str:
    """
    A one-word, Python-computed read of the simulation — never an LLM guess.

    Priority order matches the task's own definition: an unambiguous
    improvement on BOTH Sharpe and volatility wins outright; a volatility
    increase paired with high correlation to the existing book is flagged as
    concentrating even if Sharpe also happens to rise; anything else with a
    materially large volatility swing is "high_impact" without a directional
    verdict; small moves are "neutral".
    """
    before, after = result.get("before") or {}, result.get("after") or {}
    delta = result.get("delta") or {}
    sharpe_delta = delta.get("sharpe_ratio_delta")
    vol_delta = delta.get("volatility_delta")
    corr = result.get("correlation_to_book")

    if sharpe_delta is not None and vol_delta is not None:
        if sharpe_delta > 0 and vol_delta < 0:
            return "diversifying"
        if vol_delta > 0 and corr is not None and corr > _VERDICT_CORRELATION_THRESHOLD:
            return "concentrating"
    if vol_delta is not None and abs(vol_delta) >= _VERDICT_VOL_THRESHOLD:
        return "high_impact"
    return "neutral"


def _commentary(result: dict, ticker: str) -> str:
    """Plain-language 2-sentence summary, built entirely from the result's own numbers."""
    before, after = result.get("before") or {}, result.get("after") or {}
    delta = result.get("delta") or {}
    weight_after = result.get("weight_after")
    corr = result.get("correlation_to_book")

    def pct(v: float | None) -> str:
        return "—" if v is None else f"{v * 100:.1f}%"

    def signed_pp(v: float | None) -> str:
        return "—" if v is None else f"{v * 100:+.1f}pp"

    sentence1 = (
        f"Taking {ticker} to {pct(weight_after)} of net worth moves annualized "
        f"volatility from {pct(before.get('volatility'))} to "
        f"{pct(after.get('volatility'))} ({signed_pp(delta.get('volatility_delta'))}) "
        f"and the Sharpe ratio from "
        f"{before.get('sharpe_ratio') if before.get('sharpe_ratio') is not None else '—'} "
        f"to {after.get('sharpe_ratio') if after.get('sharpe_ratio') is not None else '—'}."
    )
    verdict = result.get("verdict")
    if verdict == "diversifying":
        reason = (
            f"a {corr:.2f} correlation to the rest of the book" if corr is not None
            else "its return pattern differing from the rest of the book"
        )
        sentence2 = f"This is diversifying — {reason} is pulling total risk down while raising risk-adjusted return."
    elif verdict == "concentrating":
        sentence2 = (
            f"This concentrates the book: {ticker} moves closely with what you "
            f"already hold (correlation {corr:.2f}), so this size adds risk "
            f"without adding an independent return source."
            if corr is not None else
            "This raises volatility with no offsetting diversification benefit at this size."
        )
    elif verdict == "high_impact":
        sentence2 = "The size alone is enough to materially shift the whole portfolio's risk profile — resize with that in mind."
    else:
        sentence2 = "At this size the impact on the whole portfolio is modest either way."
    return f"{sentence1} {sentence2}"


async def simulate_any_trade(
    ticker: str,
    target_weight: float | None = None,
    dollar_amount: float | None = None,
) -> dict:
    """
    Full Before/After portfolio-KPI comparison (Sharpe, annualized volatility,
    max drawdown, 95% VaR, correlation-to-book) for taking ``ticker`` — HELD
    or BRAND NEW — to a target size, expressed as either a weight fraction of
    net worth or a base-currency dollar amount (converted to a weight using
    the current net worth).

    Unlike :func:`simulate_trade` (volatility-only, held tickers only — kept
    for the coach's lightweight risk-warning check and the dashboard's
    top-risk-contributor scenarios), this dynamically fetches ~1 year of price
    history for a ticker that is not already in the snapshot, so ANY valid
    ticker can be tested before committing real capital. Never raises: a
    resolvable problem (bad ticker, no context yet, no price data) comes back
    as ``{"ticker": ..., "error": "..."}`` rather than an exception.

    Net worth (for the ``dollar_amount`` path) comes from
    ``portfolio_service.value_holdings`` — the same LIVE, base-currency-
    converted figure ``coach_agent.position_context`` uses — never from this
    snapshot's own position values. Those are keyed to whatever
    ``holdings.market_value_krw``/``market_value`` supplies, which the raw
    ``holdings`` table does not carry; ``build_snapshot`` falls back to
    ``quantity * avg_price`` in the asset's NATIVE currency for weighting, and
    reusing that as a base-currency net worth would silently mix units for a
    dollar_amount request.
    """
    await build_snapshot()
    if _ctx is None:
        return {"ticker": ticker, "error": (
            "No portfolio risk context is available yet — the portfolio may "
            "be empty or its price history could not be fetched."
        )}

    t = (ticker or "").strip().upper()
    if not t:
        return {"ticker": ticker, "error": "A ticker is required."}

    net_worth = None
    if dollar_amount is not None and target_weight is None:
        _valued, totals = await ps.value_holdings()
        net_worth = totals.get("net_worth_krw")
        if not net_worth or net_worth <= 0:
            return {"ticker": t, "error": (
                "Net worth could not be determined, so a dollar amount cannot "
                "be converted to a weight — pass target_weight directly instead."
            )}
        target_weight = dollar_amount / net_worth
    elif target_weight is None:
        return {"ticker": t, "error": "target_weight or dollar_amount is required."}

    target_weight = max(0.0, min(1.0, target_weight))

    new_ticker_returns = None
    currency = _ctx["currencies"].get(t)
    if t not in _ctx["rets"].columns:
        currency = ps.resolve_asset_currency(t)
        today = date.today()
        end, start = today.isoformat(), (today - timedelta(days=_HISTORY_DAYS)).isoformat()
        try:
            new_prices, dropped = await price_provider.fetch_price_history_base(
                [t], start, end, {t: currency}, base=_ctx["base"]
            )
        except Exception as e:  # noqa: BLE001 — surface as a clean error, not a 500
            return {"ticker": t, "error": f"Could not fetch price history for {t}: {e}"}
        if new_prices is None or new_prices.empty or t not in new_prices.columns:
            return {"ticker": t, "error": (
                f"No usable price history was found for {t} — check the "
                f"ticker symbol, or it may be too newly listed to have a "
                f"full year of history."
            )}
        new_ticker_returns = risk_metrics.daily_returns(new_prices[[t]])[t]

    result = risk_metrics.simulate_new_position_impact(
        _ctx["rets"], _ctx["w"], t, target_weight,
        new_ticker_returns=new_ticker_returns,
        asset_currency=currency, base_currency=_ctx["base"],
        risk_free_annual=_RISK_FREE_ANNUAL,
    )
    if result.get("note") and result.get("before") is None:
        # A resolvable simulation failure (not enough aligned history, no
        # funding source, bad weight) — surface it the same way as the
        # earlier fetch-side errors rather than as a partial success.
        return {"ticker": t, "error": result["note"]}

    result["net_worth_base"] = round(net_worth, 2) if net_worth else None
    result["verdict"] = _verdict(result)
    result["commentary"] = _commentary(result, t)
    return result
