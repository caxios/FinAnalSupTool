"""
providers/peer_provider.py
───────────────────────────
Peer discovery + peer valuation/profitability/growth/health metrics via
yfinance. Mirrors ``price_provider``'s split: everything here is a blocking
fetch run in a worker thread, returning plain data for an agent to interpret —
no LLM, no invented numbers.

Peer discovery
──────────────
yfinance's `.info` has no reliable "similar companies" field, so discovery is
two-tier and entirely deterministic:
  1. Direct membership — the ticker already sits in one of the curated
     industry clusters below; its clustermates ARE the peer set.
  2. Sector/industry match — for a ticker outside every cluster, its `.info`
     sector/industry is matched against the clusters' own sector labels, so a
     new large-cap semiconductor name still lands among semiconductor peers
     rather than an empty list.
Neither tier fabricates a peer that isn't a real, named company; a ticker that
matches nothing returns an empty peer list rather than a guess.
"""

from __future__ import annotations

import asyncio
import logging
from statistics import median

import yfinance as yf

logger = logging.getLogger(__name__)

# Curated industry clusters — the plan's "major sectors", each a real,
# well-known peer group. Keyed by cluster id; `sector_keywords` are matched
# case-insensitively against yfinance's own `sector`/`industry` strings for
# tier-2 discovery.
_CLUSTERS: dict[str, dict] = {
    "semiconductors": {
        "members": ["NVDA", "AMD", "INTC", "TSM", "AVGO", "QCOM"],
        "sector_keywords": ["semiconductor"],
    },
    "big_tech": {
        "members": ["AAPL", "MSFT", "GOOGL", "AMZN", "META"],
        "sector_keywords": ["consumer electronics", "internet content", "software - infrastructure"],
    },
    "ev_auto": {
        # BYD's China listing/ADR is BYDDY — the plain "BYD" ticker on NYSE is
        # Boyd Gaming, an unrelated casino operator, so it is deliberately not
        # used here even though the plan's prose names it "BYD".
        "members": ["TSLA", "RIVN", "LCID", "GM", "F", "BYDDY"],
        "sector_keywords": ["auto manufacturers", "auto — manufacturers"],
    },
    "cloud_saas": {
        "members": ["CRM", "NOW", "WDAY", "SNOW", "DDOG"],
        "sector_keywords": ["software - application", "software—application"],
    },
}

_MEMBER_TO_CLUSTER: dict[str, str] = {
    m: cid for cid, c in _CLUSTERS.items() for m in c["members"]
}


def _f(x) -> float | None:
    """Coerce a scalar to a rounded float, or None if missing/NaN/non-numeric."""
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:  # NaN
            return None
        return round(v, 6)
    except (TypeError, ValueError):
        return None


def _fetch_info(ticker: str) -> dict:
    """Blocking `.info` fetch. Never raises — an unreachable ticker yields {}."""
    try:
        info = yf.Ticker(ticker).info
        return info if isinstance(info, dict) else {}
    except Exception as e:  # noqa: BLE001 — one bad peer must not fail the set
        logger.warning(f"[peer_provider] .info fetch failed for {ticker}: {e}")
        return {}


def _discover_blocking(ticker: str, limit: int) -> tuple[list[str], str, dict]:
    """Returns ``(peer_tickers, discovery_method, target_info)``."""
    t = (ticker or "").strip().upper()
    # Fetched once regardless of path — real sector/industry for DISPLAY should
    # never be a stand-in for the internal cluster id.
    info = _fetch_info(t)

    cluster_id = _MEMBER_TO_CLUSTER.get(t)
    if cluster_id:
        members = _CLUSTERS[cluster_id]["members"]
        peers = [m for m in members if m != t][:limit]
        return peers, "direct_cluster_membership", info

    sector = info.get("sector")
    industry = info.get("industry")
    haystack = f"{sector or ''} {industry or ''}".lower()

    for cid, c in _CLUSTERS.items():
        if any(kw in haystack for kw in c["sector_keywords"]):
            peers = [m for m in c["members"] if m != t][:limit]
            return peers, "sector_industry_match", info

    return [], "no_match", info


async def discover_peers(ticker: str, limit: int = 5) -> dict:
    """
    ``{"peers": [...], "method": ..., "sector": ..., "industry": ...}``

    ``method`` is always surfaced to the agent (and from there to the UI) so a
    "no real peers could be identified" state is visible rather than silently
    rendered as an empty comparison.
    """
    peers, method, info = await asyncio.to_thread(_discover_blocking, ticker, limit)
    return {
        "peers": peers, "method": method,
        "sector": info.get("sector"), "industry": info.get("industry"),
    }


# =============================================================================
# Metrics
# =============================================================================
# (id, human label, extractor, higher_is_better). `higher_is_better` drives the
# UI's green/red badge — a cheap valuation multiple is "good" (lower value),
# while a wide margin or fast growth is "good" (higher value).

def _fcf_margin(info: dict) -> float | None:
    fcf = _f(info.get("freeCashflow"))
    revenue = _f(info.get("totalRevenue"))
    if fcf is None or not revenue:
        return None
    return round(fcf / revenue, 6)


_METRICS: list[tuple[str, str, str, bool]] = [
    # id, label, info key ("|" joins fallbacks tried in order), higher_is_better
    ("trailing_pe", "Trailing P/E", "trailingPE", False),
    ("forward_pe", "Forward P/E", "forwardPE", False),
    ("ev_ebitda", "EV/EBITDA", "enterpriseToEbitda", False),
    ("price_to_sales", "P/S", "priceToSalesTrailing12Months", False),
    ("price_to_book", "P/B", "priceToBook", False),
    ("peg_ratio", "PEG", "trailingPegRatio|pegRatio", False),
    ("gross_margin", "Gross Margin", "grossMargins", True),
    ("operating_margin", "Operating Margin", "operatingMargins", True),
    ("net_margin", "Net Margin", "profitMargins", True),
    ("roe", "ROE", "returnOnEquity", True),
    # yfinance has no true ROIC field; ROA is the closest available proxy and
    # is labelled as such rather than presented as a real ROIC.
    ("roic_proxy", "ROIC (ROA proxy)", "returnOnAssets", True),
    ("revenue_growth", "Revenue Growth YoY", "revenueGrowth", True),
    ("earnings_growth", "Quarterly Earnings Growth YoY", "earningsQuarterlyGrowth", True),
    ("debt_to_equity", "Debt/Equity", "debtToEquity", False),
    ("current_ratio", "Current Ratio", "currentRatio", True),
]


def _extract(info: dict, key_spec: str) -> float | None:
    for key in key_spec.split("|"):
        v = _f(info.get(key))
        if v is not None:
            return v
    return None


def _percentile(target: float, all_values: list[float]) -> float:
    """Share of the peer set (INCLUDING the target) at or below the target."""
    if not all_values:
        return 0.0
    at_or_below = sum(1 for v in all_values if v <= target)
    return round(100.0 * at_or_below / len(all_values), 1)


def _compute_table(target_ticker: str, infos: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """
    ``infos`` maps ticker -> its `.info` dict (target included). Returns
    ``(metrics_table, excluded_peers)`` — a peer with NO usable metric at all
    is reported separately so the agent doesn't silently drop it.
    """
    target_info = infos.get(target_ticker, {})
    peer_tickers = [t for t in infos if t != target_ticker]

    table: list[dict] = []
    any_metric_by_peer = {t: False for t in peer_tickers}

    for mid, label, key_spec, higher_is_better in _METRICS:
        target_value = _extract(target_info, key_spec)
        peer_values: list[float] = []
        for t in peer_tickers:
            v = _extract(infos[t], key_spec)
            if v is not None:
                peer_values.append(v)
                any_metric_by_peer[t] = True

        peer_median = round(median(peer_values), 6) if peer_values else None
        peer_min = round(min(peer_values), 6) if peer_values else None
        peer_max = round(max(peer_values), 6) if peer_values else None
        premium_discount_pct = (
            round((target_value - peer_median) / abs(peer_median), 6)
            if target_value is not None and peer_median else None
        )
        percentile = (
            _percentile(target_value, peer_values + [target_value])
            if target_value is not None else None
        )

        table.append({
            "metric": mid, "label": label,
            "target_value": target_value,
            "peer_median": peer_median, "peer_min": peer_min, "peer_max": peer_max,
            "premium_discount_pct": premium_discount_pct,
            "percentile": percentile,
            "higher_is_better": higher_is_better,
        })

    # Add FCF margin, computed from two raw fields rather than one `.info` key.
    target_fcf = _fcf_margin(target_info)
    peer_fcf_by_ticker = {t: _fcf_margin(infos[t]) for t in peer_tickers}
    for t, v in peer_fcf_by_ticker.items():
        if v is not None:
            any_metric_by_peer[t] = True
    peer_fcf = [v for v in peer_fcf_by_ticker.values() if v is not None]
    peer_median = round(median(peer_fcf), 6) if peer_fcf else None
    table.append({
        "metric": "fcf_margin", "label": "FCF Margin",
        "target_value": target_fcf,
        "peer_median": peer_median,
        "peer_min": round(min(peer_fcf), 6) if peer_fcf else None,
        "peer_max": round(max(peer_fcf), 6) if peer_fcf else None,
        "premium_discount_pct": (
            round((target_fcf - peer_median) / abs(peer_median), 6)
            if target_fcf is not None and peer_median else None
        ),
        "percentile": (
            _percentile(target_fcf, peer_fcf + [target_fcf])
            if target_fcf is not None else None
        ),
        "higher_is_better": True,
    })

    excluded = [t for t, has_any in any_metric_by_peer.items() if not has_any]
    return table, excluded


async def fetch_peer_metrics(target: str, peers: list[str]) -> dict:
    """
    ``{"target_ticker", "metrics_table", "excluded_peers"}``.

    Fetches `.info` for the target and every peer CONCURRENTLY but capped —
    yfinance's `.info` endpoint is undocumented and rate-limits more
    aggressively than the OHLCV download used elsewhere in this app.
    """
    t = (target or "").strip().upper()
    tickers = [t] + [p.strip().upper() for p in peers if p.strip()]

    sem = asyncio.Semaphore(3)

    async def _one(sym: str) -> tuple[str, dict]:
        async with sem:
            return sym, await asyncio.to_thread(_fetch_info, sym)

    results = await asyncio.gather(*(_one(s) for s in tickers))
    infos = {sym: info for sym, info in results}

    table, excluded = _compute_table(t, infos)
    return {
        "target_ticker": t,
        "metrics_table": table,
        "excluded_peers": excluded,
    }
