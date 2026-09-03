"""
providers.fx_provider
─────────────────────
USD/KRW exchange rates, for both **display** and **recording**.

Structured to mirror ``price_provider``: blocking yfinance work lives in a
``_compute_*`` function, an ``async`` wrapper hands it to a worker thread, and a
small TTL cache keeps a page render from becoming N network calls.

The rule this module exists to serve
────────────────────────────────────
    Market values convert at TODAY's rate.
    Cost bases convert at the rate stored on the flow that created them.

Convert both at today's rate and a won-denominated ROI collapses to the
dollar-denominated one, which silently asserts that exchange-rate moves produced
no gain or loss. That is not a display bug; it is a wrong number. Hence two
functions: :func:`fetch_spot` for what things are worth now, and
:func:`fetch_rate_at` for what they were worth when the money moved.

Reading and writing fail differently
────────────────────────────────────
Rendering a portfolio must survive an unavailable rate — the caller shows native
figures and a null rate. Recording a cash flow must not: ``cash_flows.fx_to_krw``
is ``NOT NULL`` precisely so a guessed rate cannot be written, because a wrong
rate corrupts a cost basis permanently and irrecoverably. That policy is enforced
by the callers; this module simply raises rather than inventing a fallback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


# USD -> KRW. Both of these resolve on yfinance and return the same series;
# `KRW=X` is the documented form and `USDKRW=X` is kept as a fallback in case one
# of them stops being served.
_FX_TICKER = "KRW=X"
_FX_FALLBACK = "USDKRW=X"

PAIR = "USDKRW"

# Longer than the 60s `price_provider` uses for equities. FX moves far less over
# a minute, and unlike a stock price this one rate is applied to every row on the
# page, so a slightly older quote costs nothing and saves a call per render.
_SPOT_TTL_SECONDS = 300.0

# How far back to look for a daily close when resolving a historical rate. Long
# enough to clear a Korean holiday cluster (설날/추석 can close several sessions)
# plus a weekend.
_HISTORY_LOOKBACK_DAYS = 14

# A spot quote whose bar is older than this is served but flagged: on a Monday
# morning the last close can legitimately be three days old, and the UI should
# say so rather than present it as live.
_STALE_AFTER = timedelta(days=4)


@dataclass
class FxQuote:
    """One exchange-rate observation, with enough provenance to be checked."""

    pair: str            # "USDKRW"
    rate: float          # KRW per 1 USD
    as_of: datetime      # the timestamp of the bar actually used, in UTC
    is_stale: bool       # the bar is older than expected, or served from cache
    source: str          # "spot" | "daily_close" | "manual"


# =============================================================================
# Internals
# =============================================================================

_spot_cache: tuple[FxQuote, float] | None = None   # (quote, fetched_at)


def _to_utc(dt: datetime) -> datetime:
    """Normalize to an aware UTC datetime; naive input is assumed to be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _closes(ticker: str, start: str | None, end: str | None,
            period: str | None = None) -> pd.Series:
    """
    Daily closes for one FX pair, tz-aware, or empty.

    The index is left in the tz yfinance returns (Europe/London for this pair)
    rather than converted to UTC. That matters: a bar labelled Friday sits at
    00:00 London, which is **Thursday 23:00 UTC** — converting first and taking
    the date afterwards reports every bar one day early, and joining such a
    series to a daily equity frame would pair each trading day with the previous
    day's exchange rate.

    Comparisons against a UTC target still work, because both sides are absolute
    instants.
    """
    try:
        if period:
            raw = yf.Ticker(ticker).history(period=period, interval="1d")
        else:
            raw = yf.Ticker(ticker).history(start=start, end=end, interval="1d")
    except Exception as e:  # noqa: BLE001 — caller decides whether this is fatal
        logger.warning(f"[fx] {ticker} history failed: {e}")
        return pd.Series(dtype=float)

    if raw is None or raw.empty or "Close" not in raw.columns:
        return pd.Series(dtype=float)

    close = raw["Close"].dropna()
    if close.empty:
        return pd.Series(dtype=float)

    if close.index.tz is None:
        close.index = close.index.tz_localize("UTC")
    return close


def _closes_with_fallback(start: str | None, end: str | None,
                          period: str | None = None) -> pd.Series:
    """Try the primary pair, then the fallback, before giving up."""
    series = _closes(_FX_TICKER, start, end, period)
    if series.empty:
        logger.warning(f"[fx] {_FX_TICKER} returned nothing; trying {_FX_FALLBACK}")
        series = _closes(_FX_FALLBACK, start, end, period)
    return series


def _compute_spot() -> FxQuote:
    series = _closes_with_fallback(None, None, period="5d")
    if series.empty:
        raise ValueError(
            "No USDKRW data is available from the market data provider."
        )
    as_of = series.index[-1].to_pydatetime()
    age = datetime.now(timezone.utc) - _to_utc(as_of)
    return FxQuote(
        pair=PAIR,
        rate=float(series.iloc[-1]),
        # Reported in the bar's own timezone so its date reads as the trading
        # day it belongs to.
        as_of=as_of,
        is_stale=age > _STALE_AFTER,
        source="spot",
    )


def _compute_rate_at(when: datetime) -> FxQuote:
    """
    The daily close at or before ``when``.

    Daily rather than intraday on purpose: yfinance's intraday FX coverage is
    thin, and a trade's execution price is already a bar approximation, so
    claiming minute-level FX precision on top of it would be false precision.

    A weekend or holiday resolves to the nearest **prior** close — the last rate
    the market actually printed before that moment.
    """
    target = _to_utc(when)
    start = (target - timedelta(days=_HISTORY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    # yfinance's `end` is exclusive, so reach a day past the target.
    end = (target + timedelta(days=2)).strftime("%Y-%m-%d")

    series = _closes_with_fallback(start, end)
    if series.empty:
        raise ValueError(
            f"No USDKRW data available around {target.date()}."
        )

    prior = series[series.index <= pd.Timestamp(target)]
    if prior.empty:
        # The request predates the window fetched — use the earliest bar we have
        # and mark it, rather than failing outright.
        bar_time = series.index[0].to_pydatetime()
        return FxQuote(PAIR, float(series.iloc[0]), bar_time,
                       is_stale=True, source="daily_close")

    bar_time = prior.index[-1].to_pydatetime()
    # Stale relative to what was ASKED for, not to now: a Friday close standing
    # in for a Saturday is expected, a two-week-old close is not.
    is_stale = (target - _to_utc(bar_time)) > _STALE_AFTER
    return FxQuote(PAIR, float(prior.iloc[-1]), bar_time,
                   is_stale=is_stale, source="daily_close")


def _compute_history(start: str, end: str) -> pd.Series:
    series = _closes_with_fallback(start, end)
    if series.empty:
        return series
    # Match the naive, date-labelled index `_fetch_price_history` produces for
    # equities, so the two join on the same trading day.
    #
    # `tz_localize(None)` keeps the LOCAL wall time and drops the zone, so a bar
    # at 00:00 London stays 2026-08-28. Going via UTC first would land it on
    # 2026-08-27 23:00 and normalize to the wrong day — pairing every equity
    # close with the previous day's rate.
    series = series.copy()
    series.index = series.index.tz_localize(None).normalize()
    series.name = PAIR
    return series


# =============================================================================
# Public API
# =============================================================================

async def fetch_spot(use_cache: bool = True) -> FxQuote:
    """
    The current USD/KRW rate, for converting things that are worth something now.

    Cached for 300 seconds. Raises ``ValueError`` when no rate can be obtained —
    a display caller should catch that and render a null rate rather than
    substituting one.
    """
    global _spot_cache
    now = time.monotonic()
    if use_cache and _spot_cache is not None:
        quote, fetched_at = _spot_cache
        if now - fetched_at < _SPOT_TTL_SECONDS:
            return quote

    quote = await asyncio.to_thread(_compute_spot)
    _spot_cache = (quote, now)
    return quote


async def fetch_rate_at(when: datetime) -> FxQuote:
    """
    The USD/KRW rate that applied at a past instant — the daily close at or
    before it.

    This is the rate that must be stamped on a cash flow. It is the only record
    of what that money was worth in won at the time, and it cannot be
    reconstructed from a later rate.
    """
    return await asyncio.to_thread(_compute_rate_at, when)


async def fetch_fx_history(start: str, end: str) -> pd.Series:
    """
    Daily USD/KRW closes over a window, UTC-normalized and tz-naive.

    Used by :func:`price_provider.fetch_price_history_base` to convert a whole
    price series before returns are computed — the ordering the risk model
    depends on.
    """
    return await asyncio.to_thread(_compute_history, start, end)


def to_krw(usd: float | None, rate: float | None) -> float | None:
    """Convert, or return ``None`` if either side is missing. Never guesses."""
    if usd is None or rate is None:
        return None
    return float(usd) * float(rate)


def to_usd(krw: float | None, rate: float | None) -> float | None:
    if krw is None or rate is None or not rate:
        return None
    return float(krw) / float(rate)


def convert(amount: float | None, from_currency: str, to_currency: str,
            rate: float | None) -> float | None:
    """
    Convert between the two supported currencies at ``rate`` (KRW per USD).

    Returns ``amount`` unchanged when the currencies match, and ``None`` when a
    conversion is needed but no rate is available — so a caller that forgets to
    check renders a dash rather than a wrong number.
    """
    src = (from_currency or "").strip().upper()
    dst = (to_currency or "").strip().upper()
    if amount is None:
        return None
    if src == dst:
        return float(amount)
    if src == "USD" and dst == "KRW":
        return to_krw(amount, rate)
    if src == "KRW" and dst == "USD":
        return to_usd(amount, rate)
    raise ValueError(f"Unsupported conversion {src} -> {dst}.")


def clear_cache() -> None:
    """Drop the cached spot quote. Used by tests."""
    global _spot_cache
    _spot_cache = None
