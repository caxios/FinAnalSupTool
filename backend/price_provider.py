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
from dataclasses import dataclass, field

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
