"""
providers/macro_data_provider.py
────────────────────────────────
Macroeconomic indicator data via FRED (Federal Reserve Economic Data) and
yfinance, designed for the Macro History Teller Agent.

Provides two levels of data:
  1. CURRENT-PERIOD indicators — the same window the other agents analyse.
  2. HISTORICAL indicators — past decades of data so the History Agent can
     ground its "analogue" claims with real numbers rather than LLM guesses.

Data sources
────────────
  - FRED REST API (free key from https://fred.stlouisfed.org/docs/api/api_key.html)
    → CPI, Core CPI, Unemployment, Non-farm Payrolls, GDP, Fed Funds Rate
  - yfinance (no key needed)
    → Treasury yields (2Y, 10Y, 30Y), VIX, yield-curve spread (10Y-2Y)

Configuration
─────────────
  FRED_API_KEY  (optional) — enables CPI/unemployment/GDP/Fed-Funds.
  If missing, only yfinance-based indicators are returned (graceful degradation).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

import httpx
import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_HTTP_TIMEOUT = 30.0

# FRED series we care about.  Each entry: (human_label, series_id, transform).
# Transform is applied AFTER fetching:
#   "level"  → use as-is (e.g. unemployment rate already in %)
#   "yoy"    → year-over-year % change (e.g. CPI index → inflation rate)
#   "mom"    → month-over-month change
_FRED_SERIES: list[tuple[str, str, str]] = [
    ("CPI (YoY %)",              "CPIAUCSL",  "yoy"),
    ("Core CPI (YoY %)",         "CPILFESL",  "yoy"),
    ("Unemployment Rate (%)",    "UNRATE",    "level"),
    ("Non-farm Payrolls (MoM)",  "PAYEMS",    "mom"),
    ("Real GDP (QoQ %)",         "GDP",       "yoy"),
    ("Fed Funds Rate (%)",       "FEDFUNDS",  "level"),
]

# yfinance tickers for market-based indicators.
_YF_YIELD_TICKERS: dict[str, str] = {
    "UST 2Y Yield (%)":  "2YY=F",   # 2-Year Treasury futures yield
    "UST 10Y Yield (%)": "^TNX",     # CBOE 10-Year
    "UST 30Y Yield (%)": "^TYX",     # CBOE 30-Year
}
_VIX_TICKER = "^VIX"

# ^TNX historically used a ×10 convention; normalize if detected.
_TNX_X10_THRESHOLD = 25.0


def fred_api_key() -> str | None:
    """Return the configured FRED API key, or None if unset."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    return key or None


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MonthlyIndicator:
    """One indicator's monthly time-series."""
    label: str
    source: str                       # "FRED" or "yfinance"
    # [{month: "2024-01", value: 3.1}, ...]
    monthly: list[dict] = field(default_factory=list)
    latest_value: float | None = None
    unit: str = ""                    # e.g. "%" or "index"


@dataclass
class MacroIndicatorData:
    """All macro indicators for a given period."""
    period_start: str
    period_end: str
    indicators: list[MonthlyIndicator] = field(default_factory=list)
    yield_spread_10y2y: list[dict] = field(default_factory=list)  # [{month, spread_bps}]
    fred_available: bool = False


@dataclass
class YieldCurveSnapshot:
    """Multi-tenor yield curve at a point in time."""
    date: str
    ust_2y: float | None = None
    ust_10y: float | None = None
    ust_30y: float | None = None
    spread_10y2y_bps: float | None = None


# ─────────────────────────────────────────────────────────────────────────────
# FRED helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_fred_series(
    series_id: str,
    start_date: str,
    end_date: str,
    api_key: str,
) -> pd.Series:
    """
    Fetch a single FRED series and return it as a pandas Series indexed by date.
    Raises ValueError on empty / error responses.
    """
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
        "sort_order": "asc",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(_FRED_BASE, params=params)
    if resp.status_code != 200:
        raise ValueError(f"FRED API error {resp.status_code} for {series_id}: {resp.text[:300]}")

    data = resp.json()
    observations = data.get("observations", [])
    if not observations:
        raise ValueError(f"No FRED observations for {series_id} in {start_date}..{end_date}")

    dates, values = [], []
    for obs in observations:
        val = obs.get("value", ".")
        if val == ".":
            continue  # FRED uses "." for missing
        try:
            dates.append(pd.Timestamp(obs["date"]))
            values.append(float(val))
        except (ValueError, KeyError):
            continue

    if not dates:
        raise ValueError(f"All FRED observations for {series_id} were missing ('.')")

    return pd.Series(values, index=pd.DatetimeIndex(dates), name=series_id)


def _transform(series: pd.Series, transform: str) -> pd.Series:
    """Apply a transform to a raw FRED series."""
    if transform == "yoy":
        # Year-over-year percentage change.
        return series.pct_change(periods=12) * 100.0
    if transform == "mom":
        return series.diff()
    return series  # "level" — use as-is


def _series_to_monthly(series: pd.Series) -> list[dict]:
    """Resample a series to month-end and return [{month, value}, ...]."""
    monthly = series.resample("ME").last().dropna()
    return [
        {"month": ts.strftime("%Y-%m"), "value": round(float(v), 2)}
        for ts, v in monthly.items()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# yfinance helpers
# ─────────────────────────────────────────────────────────────────────────────

def _flatten_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance can return MultiIndex columns; reduce to plain names."""
    if isinstance(df.columns, pd.MultiIndex):
        try:
            df = df.xs(ticker, axis=1, level=1)
        except (KeyError, ValueError):
            df.columns = df.columns.get_level_values(0)
    return df


def _fetch_yf_close(ticker: str, start: str, end: str) -> pd.Series:
    """Blocking: download close prices for a single ticker."""
    raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise ValueError(f"No yfinance data for {ticker} in {start}..{end}")
    df = _flatten_columns(raw, ticker)
    close = df["Close"].dropna()
    if close.empty:
        raise ValueError(f"yfinance data for {ticker} had no usable closes.")
    return close


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_macro_indicators(
    start_date: str, end_date: str,
) -> MacroIndicatorData:
    """
    Fetch all macro indicators for the given period.

    Returns a MacroIndicatorData with monthly time-series for each indicator.
    FRED-based indicators are only available when FRED_API_KEY is set; yfinance
    indicators (yields, VIX) are always attempted.
    """
    result = MacroIndicatorData(period_start=start_date, period_end=end_date)
    api_key = fred_api_key()

    # ── FRED indicators ──
    if api_key:
        result.fred_available = True
        for label, series_id, transform in _FRED_SERIES:
            try:
                # FRED calls are async (httpx), so we can gather them.
                raw = await _fetch_fred_series(series_id, start_date, end_date, api_key)
                transformed = _transform(raw, transform)
                monthly = _series_to_monthly(transformed)
                latest = monthly[-1]["value"] if monthly else None
                result.indicators.append(MonthlyIndicator(
                    label=label, source="FRED", monthly=monthly,
                    latest_value=latest, unit="%",
                ))
            except (ValueError, Exception) as e:
                logger.warning(f"FRED fetch failed for {series_id}: {e}")
    else:
        logger.info(
            "FRED_API_KEY not set — CPI, unemployment, GDP, and Fed Funds "
            "indicators will not be available. Set it in .env for full macro data."
        )

    # ── yfinance yields ──
    yield_series: dict[str, pd.Series] = {}
    for label, ticker in _YF_YIELD_TICKERS.items():
        try:
            close = await asyncio.to_thread(_fetch_yf_close, ticker, start_date, end_date)
            # Normalize ^TNX ×10 convention if present.
            if ticker == "^TNX" and float(close.median()) > _TNX_X10_THRESHOLD:
                close = close / 10.0
            if ticker == "^TYX" and float(close.median()) > _TNX_X10_THRESHOLD:
                close = close / 10.0
            yield_series[label] = close
            monthly = _series_to_monthly(close)
            latest = monthly[-1]["value"] if monthly else None
            result.indicators.append(MonthlyIndicator(
                label=label, source="yfinance", monthly=monthly,
                latest_value=latest, unit="%",
            ))
        except (ValueError, Exception) as e:
            logger.warning(f"yfinance yield fetch failed for {ticker}: {e}")

    # ── Yield spread (10Y - 2Y) ──
    s10 = yield_series.get("UST 10Y Yield (%)")
    s2 = yield_series.get("UST 2Y Yield (%)")
    if s10 is not None and s2 is not None:
        try:
            # Align on common dates, compute spread in basis points.
            aligned = pd.DataFrame({"t10": s10, "t2": s2}).dropna()
            spread = (aligned["t10"] - aligned["t2"]) * 100.0  # bps
            spread_monthly = spread.resample("ME").last().dropna()
            result.yield_spread_10y2y = [
                {"month": ts.strftime("%Y-%m"), "spread_bps": round(float(v), 1)}
                for ts, v in spread_monthly.items()
            ]
            # Also add as a proper indicator.
            monthly_list = [
                {"month": ts.strftime("%Y-%m"), "value": round(float(v), 1)}
                for ts, v in spread_monthly.items()
            ]
            latest_spread = monthly_list[-1]["value"] if monthly_list else None
            result.indicators.append(MonthlyIndicator(
                label="10Y-2Y Spread (bps)", source="computed",
                monthly=monthly_list, latest_value=latest_spread, unit="bps",
            ))
        except Exception as e:
            logger.warning(f"Yield spread computation failed: {e}")

    # ── VIX ──
    try:
        vix = await asyncio.to_thread(_fetch_yf_close, _VIX_TICKER, start_date, end_date)
        monthly = _series_to_monthly(vix)
        latest = monthly[-1]["value"] if monthly else None
        result.indicators.append(MonthlyIndicator(
            label="VIX", source="yfinance", monthly=monthly,
            latest_value=latest, unit="index",
        ))
    except (ValueError, Exception) as e:
        logger.warning(f"VIX fetch failed: {e}")

    logger.info(
        f"Macro indicators fetched: {len(result.indicators)} series "
        f"for {start_date}..{end_date} (FRED={'yes' if result.fred_available else 'no'})"
    )
    return result


async def fetch_historical_indicators(
    start_date: str, end_date: str,
) -> MacroIndicatorData:
    """
    Fetch macro indicators for a HISTORICAL period (potentially decades back).

    Same logic as fetch_macro_indicators but intended for validating historical
    analogues — the History Agent calls this with dates like "1994-01-01" to
    "1995-06-30" to get the real numbers for a past period.
    """
    return await fetch_macro_indicators(start_date, end_date)


def format_indicators_for_llm(data: MacroIndicatorData) -> str:
    """
    Render MacroIndicatorData as a compact Markdown string suitable for
    injection into an LLM prompt.
    """
    if not data.indicators:
        return "(No macro indicator data available.)"

    parts: list[str] = [
        f"# Macroeconomic Indicators ({data.period_start} → {data.period_end})\n"
    ]

    for ind in data.indicators:
        if not ind.monthly:
            continue
        parts.append(f"## {ind.label}")
        parts.append(f"Latest: {ind.latest_value} {ind.unit}")
        # Show monthly table (compact).
        header = "| Month | Value |"
        sep = "| --- | --- |"
        rows = [f"| {m['month']} | {m['value']} |" for m in ind.monthly]
        parts.append(header)
        parts.append(sep)
        parts.extend(rows)
        parts.append("")

    if data.yield_spread_10y2y:
        parts.append("## 10Y-2Y Treasury Spread (bps)")
        parts.append("Negative = yield curve inversion (recession signal)")
        header = "| Month | Spread (bps) |"
        sep = "| --- | --- |"
        rows = [f"| {m['month']} | {m['spread_bps']} |" for m in data.yield_spread_10y2y]
        parts.append(header)
        parts.append(sep)
        parts.extend(rows)
        parts.append("")

    return "\n".join(parts)
