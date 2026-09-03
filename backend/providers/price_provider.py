"""
price_provider.py
─────────────────
Historical price data + technical indicator computation via yfinance.

The LLM does NOT compute these indicators — they are calculated here in pandas
and passed to the Technical Analysis Agent as pre-computed facts for
interpretation. yfinance data is ~15-minute delayed, which is sufficient for
daily-level technical analysis.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# A reliable SMA200 needs ~200 trading days; below this we flag limited history.
_FULL_HISTORY_DAYS = 200


@dataclass
class TechnicalData:
    """Pre-computed technical indicators for the LLM to interpret."""
    ticker: str
    period_start: str
    period_end: str
    current_price: float
    period_high: float
    period_low: float
    period_return: float           # e.g., 0.12 = +12%

    # Moving averages
    sma_50: float | None
    sma_200: float | None
    ema_20: float | None
    golden_cross: bool             # SMA50 > SMA200
    price_vs_sma50: str            # "above" | "below" | "n/a"
    price_vs_sma200: str           # "above" | "below" | "n/a"

    # Momentum
    rsi_14: float | None           # 0-100
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None

    # Bollinger Bands
    bb_upper: float | None
    bb_lower: float | None
    bb_position: float | None      # 0-1 (where price sits within the band)

    # Volume
    current_volume: int | None
    avg_volume_20d: float | None
    volume_ratio: float | None     # current / avg

    # Support / Resistance (from recent price action)
    recent_highs: list[float] = field(default_factory=list)
    recent_lows: list[float] = field(default_factory=list)

    # Monthly price summary for trend visualization
    monthly_closes: list[dict] = field(default_factory=list)

    # Data quality
    data_points: int = 0                 # number of trading days fetched
    has_full_history: bool = False       # enough data for a reliable SMA200


# =============================================================================
# Indicator helpers (pure pandas/numpy)
# =============================================================================

def _f(x) -> float | None:
    """Coerce a scalar to a rounded float, or None if missing/NaN."""
    try:
        if x is None or pd.isna(x):
            return None
        return round(float(x), 4)
    except (TypeError, ValueError):
        return None


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (0-100) via exponential smoothing of gains/losses."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD(12, 26, 9): returns (macd_line, signal_line, histogram)."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal, macd_line - signal


def _swing_levels(series: pd.Series, kind: str, window: int = 5, take: int = 3) -> list[float]:
    """
    Identify recent swing highs/lows: local extrema where the point is the
    max/min within +/- `window` bars. Returns up to `take` most-recent distinct
    levels (rounded), for use as support/resistance.
    """
    arr = series.to_numpy(dtype=float)
    n = len(arr)
    out: list[float] = []
    for i in range(window, n - window):
        w = arr[i - window: i + window + 1]
        if kind == "high" and arr[i] == w.max():
            out.append(arr[i])
        elif kind == "low" and arr[i] == w.min():
            out.append(arr[i])
    # De-duplicate near-equal levels, keep the most recent ones.
    deduped: list[float] = []
    for v in out:
        r = round(float(v), 2)
        if not deduped or abs(deduped[-1] - r) / max(r, 1e-9) > 0.005:
            deduped.append(r)
    return deduped[-take:]


def _monthly_closes(close: pd.Series) -> list[dict]:
    """Aggregate to month-end closes: [{'month': '2025-01', 'close': 180.5}, ...]."""
    monthly = close.resample("ME").last().dropna()
    return [
        {"month": ts.strftime("%Y-%m"), "close": round(float(v), 2)}
        for ts, v in monthly.items()
    ]


def _flatten_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance can return MultiIndex columns; reduce to plain OHLCV names."""
    if isinstance(df.columns, pd.MultiIndex):
        # Prefer selecting the ticker level; fall back to dropping it.
        try:
            df = df.xs(ticker, axis=1, level=1)
        except (KeyError, ValueError):
            df.columns = df.columns.get_level_values(0)
    return df


# =============================================================================
# Public API
# =============================================================================

def _compute(ticker: str, start_date: str, end_date: str) -> TechnicalData:
    """Blocking fetch + compute (run in a thread by the async wrapper)."""
    raw = yf.download(
        ticker, start=start_date, end=end_date,
        auto_adjust=True, progress=False,
    )
    if raw is None or raw.empty:
        raise ValueError(
            f"No price data returned for '{ticker}'. Check the ticker symbol "
            f"and the date range ({start_date} → {end_date})."
        )

    df = _flatten_columns(raw, ticker)
    close = df["Close"].dropna()
    volume = df["Volume"] if "Volume" in df else pd.Series(dtype=float)
    if close.empty:
        raise ValueError(f"Price data for '{ticker}' had no usable close prices.")

    n = len(close)
    current_price = float(close.iloc[-1])
    period_high = float(close.max())
    period_low = float(close.min())
    first_price = float(close.iloc[0])
    period_return = round((current_price / first_price) - 1.0, 4) if first_price else 0.0

    # Moving averages (only when enough data exists).
    sma_50 = close.rolling(50).mean().iloc[-1] if n >= 50 else np.nan
    sma_200 = close.rolling(200).mean().iloc[-1] if n >= 200 else np.nan
    ema_20 = close.ewm(span=20, adjust=False).mean().iloc[-1] if n >= 20 else np.nan

    sma_50_f, sma_200_f, ema_20_f = _f(sma_50), _f(sma_200), _f(ema_20)
    golden_cross = bool(
        sma_50_f is not None and sma_200_f is not None and sma_50_f > sma_200_f
    )
    price_vs_sma50 = (
        "above" if sma_50_f is not None and current_price >= sma_50_f
        else "below" if sma_50_f is not None else "n/a"
    )
    price_vs_sma200 = (
        "above" if sma_200_f is not None and current_price >= sma_200_f
        else "below" if sma_200_f is not None else "n/a"
    )

    # Momentum
    rsi_14 = _f(_rsi(close).iloc[-1]) if n >= 15 else None
    macd_line, signal, hist = _macd(close)
    macd_v = _f(macd_line.iloc[-1]) if n >= 26 else None
    signal_v = _f(signal.iloc[-1]) if n >= 26 else None
    hist_v = _f(hist.iloc[-1]) if n >= 26 else None

    # Bollinger Bands (20, 2)
    bb_upper = bb_lower = bb_position = None
    if n >= 20:
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        upper = (mid + 2 * std).iloc[-1]
        lower = (mid - 2 * std).iloc[-1]
        bb_upper, bb_lower = _f(upper), _f(lower)
        if bb_upper is not None and bb_lower is not None and bb_upper != bb_lower:
            bb_position = round((current_price - bb_lower) / (bb_upper - bb_lower), 4)

    # Volume
    current_volume = avg_volume_20d = volume_ratio = None
    if not volume.empty and volume.notna().any():
        cv = volume.iloc[-1]
        current_volume = int(cv) if not pd.isna(cv) else None
        if len(volume) >= 20:
            avg = volume.rolling(20).mean().iloc[-1]
            avg_volume_20d = _f(avg)
            if avg_volume_20d and current_volume:
                volume_ratio = round(current_volume / avg_volume_20d, 4)

    # Support / resistance from the recent window (last ~90 bars).
    recent = close.iloc[-90:] if n > 90 else close
    recent_highs = _swing_levels(recent, "high")
    recent_lows = _swing_levels(recent, "low")

    return TechnicalData(
        ticker=ticker.upper(),
        period_start=start_date,
        period_end=end_date,
        current_price=round(current_price, 2),
        period_high=round(period_high, 2),
        period_low=round(period_low, 2),
        period_return=period_return,
        sma_50=sma_50_f, sma_200=sma_200_f, ema_20=ema_20_f,
        golden_cross=golden_cross,
        price_vs_sma50=price_vs_sma50, price_vs_sma200=price_vs_sma200,
        rsi_14=rsi_14, macd=macd_v, macd_signal=signal_v, macd_histogram=hist_v,
        bb_upper=bb_upper, bb_lower=bb_lower, bb_position=bb_position,
        current_volume=current_volume, avg_volume_20d=avg_volume_20d,
        volume_ratio=volume_ratio,
        recent_highs=recent_highs, recent_lows=recent_lows,
        monthly_closes=_monthly_closes(close),
        data_points=n,
        has_full_history=n >= _FULL_HISTORY_DAYS,
    )


async def fetch_technical_data(
    ticker: str, start_date: str, end_date: str
) -> TechnicalData:
    """
    Fetch OHLCV data from yfinance and compute all technical indicators.

    The blocking yfinance/pandas work runs in a worker thread so it doesn't
    stall the event loop (important when this agent runs in parallel with
    others). Raises ValueError for an invalid ticker / empty data.
    """
    return await asyncio.to_thread(_compute, ticker, start_date, end_date)


# =============================================================================
# US 10-Year Treasury Yield (^TNX) — macro cross-asset context
# =============================================================================
# The Macro & Market agent correlates market news/sentiment against the path of
# long rates. Like the technical indicators above, the LLM does NOT invent these
# numbers — they are fetched here and injected as pre-computed facts.

# ^TNX is the CBOE 10-Year Treasury Note Yield Index. Yahoo currently quotes it
# as the yield in percent (e.g. 4.34 = 4.34%), but the index historically used a
# ×10 convention (43.4). If the level looks like the ×10 form, normalize it.
_TNX_TICKER = "^TNX"
_TNX_X10_THRESHOLD = 25.0


@dataclass
class YieldData:
    """Pre-computed 10-Year Treasury yield facts for the LLM to interpret."""
    ticker: str
    period_start: str
    period_end: str
    current_yield: float           # latest close, in percent
    start_yield: float             # first close in the period, in percent
    period_high: float
    period_low: float
    change_bps: float              # end − start, in basis points (1% = 100bps)
    # Monthly series aligned to the news grouping:
    # [{"month": "2025-01", "yield": 4.25, "change_bps": +12.0 | None}, ...]
    monthly: list[dict] = field(default_factory=list)
    data_points: int = 0           # number of trading days fetched


def _compute_yield(start_date: str, end_date: str, ticker: str = _TNX_TICKER) -> YieldData:
    """Blocking fetch + summarize of the 10-Year yield (run in a thread)."""
    raw = yf.download(
        ticker, start=start_date, end=end_date,
        auto_adjust=True, progress=False,
    )
    if raw is None or raw.empty:
        raise ValueError(
            f"No yield data returned for '{ticker}' over "
            f"{start_date} → {end_date}."
        )

    df = _flatten_columns(raw, ticker)
    close = df["Close"].dropna()
    if close.empty:
        raise ValueError(f"Yield data for '{ticker}' had no usable closes.")

    # Normalize the legacy ×10 convention (e.g. 43.4 → 4.34) if present.
    if float(close.median()) > _TNX_X10_THRESHOLD:
        close = close / 10.0

    current_yield = round(float(close.iloc[-1]), 3)
    start_yield = round(float(close.iloc[0]), 3)
    change_bps = round((current_yield - start_yield) * 100.0, 1)

    monthly: list[dict] = []
    prev: float | None = None
    for ts, v in close.resample("ME").last().dropna().items():
        y = round(float(v), 3)
        monthly.append({
            "month": ts.strftime("%Y-%m"),
            "yield": y,
            "change_bps": round((y - prev) * 100.0, 1) if prev is not None else None,
        })
        prev = y

    return YieldData(
        ticker=ticker,
        period_start=start_date,
        period_end=end_date,
        current_yield=current_yield,
        start_yield=start_yield,
        period_high=round(float(close.max()), 3),
        period_low=round(float(close.min()), 3),
        change_bps=change_bps,
        monthly=monthly,
        data_points=len(close),
    )


async def fetch_treasury_yield(
    start_date: str, end_date: str, ticker: str = _TNX_TICKER
) -> YieldData:
    """
    Fetch the US 10-Year Treasury yield (^TNX) and summarize it for the macro
    agent: period scalars plus a month-by-month series (aligned to the news
    grouping) with month-over-month change in basis points.

    The blocking yfinance/pandas work runs in a worker thread. Raises ValueError
    on empty/invalid data so the caller can degrade gracefully.
    """
    return await asyncio.to_thread(_compute_yield, start_date, end_date, ticker)


# =============================================================================
# Execution & Current Prices — the trading journal's price automation
# =============================================================================
# Blueprint §1: the user logs only a transaction *time* and *quantity*; the fill
# price is derived here. Everything above serves analysis (daily bars are plenty);
# these two functions serve bookkeeping, where the bar containing the trade
# matters, so they need intraday resolution and their own fallbacks.

# How far back each interval is actually available from Yahoo. The 1-minute
# limit is the real constraint on this feature: a trade logged more than ~30 days
# late can only ever get an approximate fill.
_INTRADAY_LIMITS: tuple[tuple[str, int], ...] = (
    ("1m", 29),      # ~30 days of 1-minute bars
    ("1h", 700),     # ~730 days of hourly bars
    ("1d", 36500),   # daily bars go back effectively forever
)

# How much history to pull around the target timestamp for each interval. Wide
# enough to survive a weekend or a market holiday, narrow enough to stay cheap.
_WINDOW_DAYS: dict[str, tuple[int, int]] = {
    "1m": (4, 2),
    "1h": (10, 2),
    "1d": (21, 5),
}

_RESOLUTION_LABELS = {
    "1m": "1-minute bar",
    "1h": "1-hour bar",
    "1d": "daily close",
}


@dataclass
class ExecutionPrice:
    """A resolved fill price, plus how precisely it could be resolved."""

    ticker: str
    price: float
    resolution: str            # "1m" | "1h" | "1d"
    bar_time: str              # ISO-8601 UTC timestamp of the bar actually used
    requested_at: str          # the timestamp the caller asked about
    is_approximate: bool       # True unless an exact 1-minute bar was found
    message: str               # human-readable note for the UI


def _to_utc(dt: datetime) -> datetime:
    """Normalize to an aware UTC datetime; naive input is assumed to be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bar_at_or_before(df: pd.DataFrame, target: datetime):
    """
    The last bar at or before ``target``, or ``None``.

    Markets close. A trade stamped 02:00 on a Sunday has no bar of its own, so
    the nearest *prior* bar is the honest answer — the last price the market
    actually printed before that moment.
    """
    if df is None or df.empty:
        return None
    idx = df.index
    # yfinance returns tz-aware timestamps for intraday and (usually) naive for
    # daily. Normalize both to UTC so the comparison below is meaningful.
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    df = df.copy()
    df.index = idx

    prior = df[df.index <= pd.Timestamp(target)]
    if not prior.empty:
        return prior.iloc[-1], prior.index[-1]
    # Target predates every bar we fetched (e.g. a trade on the ticker's first
    # trading day). Use the earliest bar available rather than failing.
    return df.iloc[0], df.index[0]


def _compute_execution_price(ticker: str, executed_at: datetime) -> ExecutionPrice:
    """Blocking fetch + resolve (run in a thread by the async wrapper)."""
    t = (ticker or "").strip().upper()
    if not t:
        raise ValueError("A ticker symbol is required.")

    target = _to_utc(executed_at)
    now = datetime.now(timezone.utc)
    if target > now + timedelta(minutes=5):
        # Small tolerance for clock skew between the user's machine and ours.
        raise ValueError(
            f"Trade time {target.isoformat()} is in the future - a fill price "
            f"cannot be looked up for a trade that has not happened yet."
        )

    age_days = (now - target).days
    attempted: list[str] = []

    for interval, max_age in _INTRADAY_LIMITS:
        if age_days > max_age:
            continue   # Yahoo will not serve this interval that far back
        back, fwd = _WINDOW_DAYS[interval]
        start = (target - timedelta(days=back)).date()
        end = min(target + timedelta(days=fwd), now).date() + timedelta(days=1)
        attempted.append(interval)

        try:
            raw = yf.Ticker(t).history(
                interval=interval, start=start, end=end, auto_adjust=True,
            )
        except Exception as e:  # noqa: BLE001 - try the next, coarser interval
            logger.warning(f"[price] {t} {interval} fetch failed: {e}")
            continue

        if raw is None or raw.empty:
            continue
        df = _flatten_columns(raw, t)
        if "Close" not in df:
            continue
        df = df[df["Close"].notna()]

        found = _bar_at_or_before(df, target)
        if found is None:
            continue
        bar, bar_time = found
        price = float(bar["Close"])
        if not price > 0:
            continue

        # The bar's CLOSE is the fill. For a 1-minute bar it is within a minute
        # of the stated time, which is closer than a retail fill is knowable;
        # open or VWAP would be equally defensible and no more accurate.
        gap = abs((bar_time.to_pydatetime() - target).total_seconds())
        exact = interval == "1m" and gap <= 120
        label = _RESOLUTION_LABELS[interval]

        if exact:
            message = f"Filled from the {label} at {bar_time.isoformat()}."
        elif interval == "1m":
            message = (
                f"Nearest {label} was {int(gap // 60)} minute(s) from the stated "
                f"time - the market was likely closed at that exact moment."
            )
        else:
            reason = (
                "1-minute data is only available for about the last 30 days"
                if age_days > 29 else "finer intervals returned no data"
            )
            message = (
                f"Approximate fill from the {label} at {bar_time.isoformat()} "
                f"({reason})."
            )

        return ExecutionPrice(
            ticker=t,
            price=round(price, 4),
            resolution=interval,
            bar_time=bar_time.to_pydatetime().isoformat(),
            requested_at=target.isoformat(),
            is_approximate=not exact,
            message=message,
        )

    raise ValueError(
        f"No price data found for '{t}' around {target.isoformat()} "
        f"(tried: {', '.join(attempted) or 'no usable interval'}). Check the "
        f"ticker symbol and the trade time."
    )


async def fetch_execution_price(ticker: str, executed_at: datetime) -> ExecutionPrice:
    """
    Resolve what a trade actually filled at, given only when it happened.

    Tries 1-minute bars first and degrades to hourly, then to the daily close,
    reporting which resolution it landed on so the UI can label an approximate
    fill honestly. Raises ValueError for a future timestamp or an unknown ticker
    (the router maps that to a 400).

    Blocking yfinance work runs in a worker thread, matching
    :func:`fetch_technical_data`.
    """
    return await asyncio.to_thread(_compute_execution_price, ticker, executed_at)


# -- Current price, with a short TTL cache -----------------------------------
# Rendering a 20-row portfolio must not become 20 network round-trips on every
# refresh. 60s is well inside yfinance's own ~15-minute delay, so the cache
# costs no meaningful freshness.

_CURRENT_TTL_SECONDS = 60.0
_current_cache: dict[str, tuple[float, float]] = {}   # ticker -> (price, fetched_at)


def _compute_current_price(ticker: str) -> float:
    """Blocking latest-price fetch."""
    t = (ticker or "").strip().upper()
    raw = yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=True)
    if raw is None or raw.empty:
        raise ValueError(f"No recent price data for '{t}'.")
    df = _flatten_columns(raw, t)
    close = df["Close"].dropna() if "Close" in df else pd.Series(dtype=float)
    if close.empty:
        raise ValueError(f"No usable close price for '{t}'.")
    return round(float(close.iloc[-1]), 4)


async def fetch_current_price(ticker: str, use_cache: bool = True) -> float:
    """
    Latest close for a ticker, cached for ~60 seconds.

    Raises ValueError if the ticker has no price data - callers valuing a
    portfolio should catch it per-ticker so one delisted symbol does not blank
    the whole page.
    """
    t = (ticker or "").strip().upper()
    now = time.monotonic()
    if use_cache:
        hit = _current_cache.get(t)
        if hit and (now - hit[1]) < _CURRENT_TTL_SECONDS:
            return hit[0]

    price = await asyncio.to_thread(_compute_current_price, t)
    _current_cache[t] = (price, now)
    return price


async def fetch_current_prices(tickers: list[str]) -> dict[str, float | None]:
    """
    Current prices for many tickers at once, fetched concurrently.

    A ticker whose lookup fails maps to ``None`` instead of raising: valuing a
    portfolio is best-effort per row, and one bad symbol must not fail the rest.
    """
    unique = sorted({(t or "").strip().upper() for t in tickers if t})
    if not unique:
        return {}

    results = await asyncio.gather(
        *(fetch_current_price(t) for t in unique), return_exceptions=True
    )
    out: dict[str, float | None] = {}
    for t, r in zip(unique, results):
        if isinstance(r, Exception):
            logger.warning(f"[price] current price unavailable for {t}: {r}")
            out[t] = None
        else:
            out[t] = r
    return out


# =============================================================================
# Multi-Ticker Price History — portfolio risk math
# =============================================================================
# The quant-risk agent needs every holding's returns on a COMMON set of dates:
# a covariance matrix built from misaligned series is meaningless. Everything
# above fetches one ticker at a time; this fetches many and aligns them.

# A ticker with far less history than the rest would, on an inner join, truncate
# the whole matrix to its own short life. Dropping it costs one position's
# detail; keeping it costs every position's history.
_MIN_COVERAGE_RATIO = 0.6


def _fetch_price_history(
    tickers: list[str], start: str, end: str
) -> tuple[pd.DataFrame, list[str]]:
    """Blocking multi-ticker fetch. Returns (aligned closes, dropped tickers)."""
    unique = sorted({(t or "").strip().upper() for t in tickers if t and t.strip()})
    if not unique:
        return pd.DataFrame(), []

    raw = yf.download(
        unique, start=start, end=end,
        auto_adjust=True, progress=False, group_by="column",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(), unique

    # With several tickers yfinance returns MultiIndex columns (field, ticker);
    # with one it returns plain OHLCV names.
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            return pd.DataFrame(), unique
        closes = raw["Close"]
    else:
        if "Close" not in raw.columns:
            return pd.DataFrame(), unique
        closes = raw[["Close"]].rename(columns={"Close": unique[0]})

    closes = closes.dropna(axis=1, how="all")
    dropped = [t for t in unique if t not in closes.columns]

    if closes.empty:
        return pd.DataFrame(), unique

    # Drop short-history tickers BEFORE aligning, so one late IPO cannot shorten
    # everyone else's window.
    best = int(closes.notna().sum().max())
    if best > 0:
        thin = [
            c for c in closes.columns
            if int(closes[c].notna().sum()) < best * _MIN_COVERAGE_RATIO
        ]
        for c in thin:
            logger.warning(
                f"[risk] dropping {c}: only {int(closes[c].notna().sum())} of "
                f"{best} observations — too short to align."
            )
        if thin and len(thin) < len(closes.columns):
            closes = closes.drop(columns=thin)
            dropped.extend(thin)

    # Inner join: keep only dates every remaining ticker traded on.
    aligned = closes.dropna(how="any")
    return aligned, sorted(set(dropped))


async def fetch_price_history(
    tickers: list[str], start: str, end: str
) -> tuple[pd.DataFrame, list[str]]:
    """
    Daily closes for several tickers, aligned to their common trading dates.

    Returns ``(DataFrame indexed by date with one column per ticker, dropped)``.
    ``dropped`` lists tickers excluded for having no data or too little history
    to align — the caller should surface them rather than pretend the portfolio
    was fully covered.

    Blocking work runs in a worker thread, matching the other fetchers here.
    """
    return await asyncio.to_thread(_fetch_price_history, tickers, start, end)


# =============================================================================
# Base-currency price series
# =============================================================================
# The ordering rule the whole risk model rests on:
#
#     Convert each price series to the base currency FIRST.
#     Compute returns SECOND.
#
# Done in that order, the correlation between a US stock and the exchange rate is
# already inside the return series, so the existing covariance machinery in
# `services/risk_metrics.py` discovers it with no new factor model. Convert after
# computing returns — or convert only the final value — and that correlation is
# thrown away. For a KRW-based investor it is not a rounding detail: the won is a
# risk-on currency and the dollar a haven, so USD exposure typically *dampens*
# portfolio volatility, and a model blind to it gets the sign wrong, not just the
# magnitude.


def _convert_series(
    series: pd.Series, native: str, base: str, fx: pd.Series
) -> pd.Series:
    """Restate one price series in ``base``, given a USDKRW rate series."""
    if native == base:
        return series
    if native == "USD" and base == "KRW":
        return series * fx
    if native == "KRW" and base == "USD":
        return series / fx
    raise ValueError(f"Unsupported conversion {native} -> {base}.")


async def fetch_price_history_base(
    tickers: list[str],
    start: str,
    end: str,
    currencies: dict[str, str],
    base: str = "KRW",
) -> tuple[pd.DataFrame, list[str]]:
    """
    Daily closes for several tickers, **restated in one currency** and aligned to
    the dates every series shares.

    ``currencies`` maps ticker -> the currency it trades in (from
    ``portfolio_service.resolve_asset_currency``). Tickers already denominated in
    ``base`` pass through untouched.

    Returns ``(DataFrame, dropped)`` exactly like :func:`fetch_price_history`, so
    a caller can swap one for the other. ``dropped`` lists tickers excluded for
    having no data or too little history to align — surface them rather than
    pretend the portfolio was fully covered.

    **Korean and US market holidays do not coincide** (설날, 추석, Thanksgiving,
    Independence Day). The inner join therefore drops every date on which either
    market was shut — roughly 15-20 sessions a year for a mixed book. That is the
    correct behaviour: a "return" measured across a day one leg did not trade is
    not a return. It does lower the observation count, so callers should report
    it and let ``risk_metrics.MIN_OBSERVATIONS`` refuse thin data rather than
    proceeding quietly.
    """
    from providers import fx_provider   # local import: keeps startup light

    aligned, dropped = await fetch_price_history(tickers, start, end)
    if aligned.empty:
        return aligned, dropped

    resolved = {
        c: (currencies.get(c) or "USD").strip().upper() for c in aligned.columns
    }
    if all(v == base for v in resolved.values()):
        return aligned, dropped          # nothing to convert, nothing to join

    fx = await fx_provider.fetch_fx_history(start, end)
    if fx.empty:
        raise ValueError(
            "No USDKRW history is available, so a mixed-currency price series "
            "cannot be stated in one currency."
        )

    # The FX series joins the SAME inner join as the price columns, so a day
    # without a quote is dropped rather than forward-filled into an observation
    # that never happened.
    common = aligned.index.intersection(fx.index)
    if common.empty:
        raise ValueError(
            "Price history and exchange-rate history share no dates."
        )
    aligned = aligned.loc[common]
    fx = fx.loc[common]

    out = pd.DataFrame(index=common)
    for col in sorted(aligned.columns):
        out[col] = _convert_series(aligned[col], resolved[col], base, fx)

    out = out.dropna(how="any")
    return out, dropped
