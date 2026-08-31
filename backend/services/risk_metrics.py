"""
services.risk_metrics
─────────────────────
Objective portfolio risk math — blueprint §2's "Mandatory Calculations".

Everything here is a **pure function** over price/return data: no LLM, no
network, no database. That is deliberate and it is the whole design of the
quant-risk agent, which mirrors ``agents/technical_analysis_agent.py``: NumPy
and pandas produce the numbers, and the LLM is only ever asked to interpret
values it was handed. An LLM that computes a Value at Risk will produce a
confident, wrong number, and a risk figure that is quietly wrong is worse than
no risk figure at all.

Conventions
───────────
* **Returns are simple daily returns**, not log returns — they aggregate across
  a portfolio linearly (a portfolio's return is the weighted mean of its
  holdings'), which log returns do not.
* **Losses are reported as positive numbers.** ``value_at_risk`` returning 0.031
  means "a 3.1% loss", because a risk report that prints negative numbers for
  bad outcomes invites sign errors at the point of reading.
* **Annualization uses 252 trading days**, the standard convention.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Trading days per year — the standard annualization factor.
TRADING_DAYS = 252

# Below this many aligned observations, dispersion estimates are noise. We still
# compute them, but the caller must label the result low-confidence.
MIN_OBSERVATIONS = 30


# =============================================================================
# Returns
# =============================================================================

def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Simple daily returns per column, with the unusable first row dropped."""
    if prices is None or prices.empty:
        return pd.DataFrame()
    return prices.pct_change().dropna(how="all")


def portfolio_returns(prices: pd.DataFrame, weights: np.ndarray) -> pd.Series:
    """
    The portfolio's daily return series for fixed weights.

    Weights are matched to ``prices.columns`` positionally, so the caller must
    build them in that order. They are normalized here so a caller passing raw
    market values rather than fractions still gets the right answer.
    """
    rets = daily_returns(prices)
    if rets.empty or len(weights) == 0:
        return pd.Series(dtype=float)

    w = np.asarray(weights, dtype=float)
    total = w.sum()
    if total <= 0:
        return pd.Series(dtype=float)
    w = w / total
    return rets.mul(w, axis=1).sum(axis=1)


# =============================================================================
# Tail risk
# =============================================================================

def value_at_risk(
    returns: pd.Series, confidence: float = 0.95, method: str = "historical"
) -> float | None:
    """
    Value at Risk as a POSITIVE loss fraction (0.031 = a 3.1% loss).

    Historical VaR is the empirical quantile of realized returns: with 95%
    confidence, losses exceeded this on 5% of days in the sample. It makes no
    normality assumption, which matters because equity returns have fat tails
    that a parametric VaR systematically understates.

    ``method="parametric"`` is accepted and computes the Gaussian equivalent
    using a small table of z-values, so no SciPy dependency is added for it.
    """
    if returns is None or returns.empty:
        return None
    r = returns.dropna()
    if r.empty:
        return None

    if method == "parametric":
        # z-values for the common confidence levels; avoids pulling in SciPy
        # for a single inverse-normal lookup.
        z_table = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263}
        z = z_table.get(round(confidence, 3), 1.6449)
        loss = -(float(r.mean()) - z * float(r.std(ddof=1)))
    else:
        loss = -float(np.quantile(r, 1.0 - confidence))

    # A portfolio that only gained over the sample has no historical loss at
    # this quantile; report 0 rather than a negative "loss".
    return round(max(loss, 0.0), 6)


def conditional_var(returns: pd.Series, confidence: float = 0.95) -> float | None:
    """
    Conditional VaR / Expected Shortfall: the AVERAGE loss on the days that
    breached VaR, as a positive fraction.

    This is the number that actually matters for position sizing. VaR says how
    bad a bad day is at the threshold; CVaR says how bad the days beyond it are
    on average — the difference between "how often" and "how much".
    """
    if returns is None or returns.empty:
        return None
    r = returns.dropna()
    if r.empty:
        return None

    cutoff = float(np.quantile(r, 1.0 - confidence))
    tail = r[r <= cutoff]
    if tail.empty:
        return None
    return round(max(-float(tail.mean()), 0.0), 6)


# =============================================================================
# Dispersion
# =============================================================================

def volatility(returns: pd.Series, annualize: bool = True) -> float | None:
    """Standard deviation of returns, annualized by sqrt(252) by default."""
    if returns is None or returns.empty:
        return None
    r = returns.dropna()
    if len(r) < 2:      # ddof=1 is undefined for a single observation
        return None
    sd = float(r.std(ddof=1))
    if annualize:
        sd *= np.sqrt(TRADING_DAYS)
    return round(sd, 6)


def max_drawdown(cumulative: pd.Series) -> float | None:
    """
    Largest peak-to-trough decline, as a positive fraction (0.24 = -24%).

    Accepts either a price/equity series or a cumulative-growth series — both
    are monotonic transforms of the same path, so the drawdown is identical.
    """
    if cumulative is None or cumulative.empty:
        return None
    s = cumulative.dropna()
    if len(s) < 2:
        return None
    running_peak = s.cummax()
    drawdowns = (s - running_peak) / running_peak
    return round(abs(float(drawdowns.min())), 6)


def equity_curve(returns: pd.Series) -> pd.Series:
    """Cumulative growth of 1 unit, for feeding :func:`max_drawdown`."""
    if returns is None or returns.empty:
        return pd.Series(dtype=float)
    return (1.0 + returns.dropna()).cumprod()


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    """
    Pairwise return correlations.

    A single-holding portfolio has no pairwise correlation to report, so this
    returns an EMPTY frame rather than the degenerate 1x1 [[1.0]] — which would
    otherwise render as "your portfolio is perfectly correlated with itself".
    """
    if returns is None or returns.empty or returns.shape[1] < 2:
        return pd.DataFrame()
    return returns.corr()


# =============================================================================
# Risk attribution — blueprint §2's headline metric
# =============================================================================

def covariance_matrix(returns: pd.DataFrame, annualize: bool = True) -> np.ndarray:
    """Return covariance as a plain ndarray, annualized by default."""
    if returns is None or returns.empty:
        return np.zeros((0, 0))
    cov = returns.cov().to_numpy()
    return cov * TRADING_DAYS if annualize else cov


def portfolio_volatility_from_cov(cov: np.ndarray, weights: np.ndarray) -> float:
    """Portfolio sigma from the covariance matrix: sqrt(wT @ cov @ w)."""
    w = np.asarray(weights, dtype=float)
    if cov.size == 0 or w.size == 0:
        return 0.0
    variance = float(w @ cov @ w)
    return float(np.sqrt(max(variance, 0.0)))


def marginal_risk_contribution(
    cov: np.ndarray, weights: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    """
    How much each position contributes to total portfolio risk.

    Returns ``(marginal, component, sigma_p)`` where:

    * ``marginal[i]`` = d(sigma_p)/d(w_i) = ``(cov @ w)_i / sigma_p`` — the rate
      at which total risk changes as this position grows. This is the number
      that answers "should I add more of this?".
    * ``component[i]`` = ``w_i * marginal[i]`` — this position's *share* of
      current risk. **These sum exactly to sigma_p**, which is what makes them
      interpretable as an attribution: a position at 10% of the portfolio
      carrying 40% of its risk is visible immediately.
    * ``sigma_p`` = total portfolio volatility.

    The summing property is Euler's theorem for homogeneous functions, and it is
    asserted in the tests — if it ever fails, the attribution is wrong.
    """
    w = np.asarray(weights, dtype=float)
    if cov.size == 0 or w.size == 0:
        return np.array([]), np.array([]), 0.0

    total = w.sum()
    if total > 0:
        w = w / total

    sigma_p = portfolio_volatility_from_cov(cov, w)
    if sigma_p <= 0:
        # A zero-variance portfolio (e.g. one constant series) has no risk to
        # attribute; return zeros rather than dividing by zero.
        return np.zeros(len(w)), np.zeros(len(w)), 0.0

    marginal = (cov @ w) / sigma_p
    component = w * marginal
    return marginal, component, sigma_p


def simulate_position_change(
    returns: pd.DataFrame,
    weights: np.ndarray,
    ticker: str,
    delta_weight: float,
) -> dict:
    """
    What happens to total portfolio risk if this position grows or shrinks?

    This is blueprint §2's question stated directly: "quantifies exactly how
    adding a new stock (or increasing position size) alters the total portfolio
    risk". ``delta_weight`` is an absolute change in portfolio fraction — 0.05
    means "take this position 5 percentage points higher", funded pro-rata from
    the others so the weights still sum to 1.

    Returns before/after volatility and the difference. ``None`` values mean the
    scenario could not be evaluated (unknown ticker, or no usable data).
    """
    empty = {
        "ticker": ticker, "delta_weight": delta_weight,
        "volatility_before": None, "volatility_after": None,
        "volatility_change": None, "note": None,
    }
    if returns is None or returns.empty:
        return {**empty, "note": "No return data available."}

    cols = list(returns.columns)
    t = (ticker or "").strip().upper()
    if t not in cols:
        return {**empty, "note": f"{t} is not in the analyzed holdings."}

    w = np.asarray(weights, dtype=float)
    total = w.sum()
    if total <= 0:
        return {**empty, "note": "Portfolio has no positive weights."}
    w = w / total

    cov = covariance_matrix(returns)
    before = portfolio_volatility_from_cov(cov, w)

    i = cols.index(t)
    w_new = w.copy()
    target = w[i] + delta_weight
    if target < 0:
        return {**empty, "volatility_before": round(before, 6),
                "note": f"Cannot reduce {t} by {abs(delta_weight):.0%} — "
                        f"it is only {w[i]:.1%} of the portfolio."}

    w_new[i] = target
    others = [j for j in range(len(w)) if j != i]
    other_sum = w[others].sum()
    if other_sum > 0:
        # Fund the change pro-rata from every other position.
        w_new[others] = w[others] * (1.0 - target) / other_sum
    elif target != 1.0:
        return {**empty, "volatility_before": round(before, 6),
                "note": "Single-position portfolio; nothing to fund the change from."}

    after = portfolio_volatility_from_cov(cov, w_new)
    return {
        "ticker": t,
        "delta_weight": delta_weight,
        "weight_before": round(float(w[i]), 6),
        "weight_after": round(float(target), 6),
        "volatility_before": round(before, 6),
        "volatility_after": round(after, 6),
        "volatility_change": round(after - before, 6),
        "note": None,
    }


# =============================================================================
# Aggregate
# =============================================================================

def compute_portfolio_risk(
    holdings: list[dict],
    prices: pd.DataFrame,
    confidence: float = 0.95,
) -> dict:
    """
    Every metric in blueprint §2, in one JSON-friendly dict for the agent.

    ``holdings`` needs ``ticker`` plus either ``market_value`` (preferred — the
    weights that actually matter) or ``quantity`` × ``avg_price`` as a fallback
    for an unpriced position.

    Degenerate inputs return a well-formed dict with nulls and an explanation in
    ``data_quality``, never an exception: an empty portfolio is a normal state
    of the app, not an error.
    """
    result: dict = {
        "positions": [],
        "confidence_level": confidence,
        "portfolio_volatility": None,
        "value_at_risk": None,
        "conditional_var": None,
        "max_drawdown": None,
        "correlation_matrix": {},
        "average_correlation": None,
        "concentration": {},
        "observations": 0,
        "period": None,
        "data_quality": {"sufficient": False, "note": ""},
    }

    if not holdings:
        result["data_quality"]["note"] = (
            "No positions in the portfolio — there is no risk to measure yet."
        )
        return result

    if prices is None or prices.empty:
        result["data_quality"]["note"] = (
            "No aligned price history was available for these holdings, so no "
            "risk metrics could be computed."
        )
        return result

    # Weight by market value where known; fall back to cost basis.
    by_ticker = {}
    for h in holdings:
        t = (h.get("ticker") or "").strip().upper()
        mv = h.get("market_value")
        if mv is None:
            qty, avg = h.get("quantity") or 0.0, h.get("avg_price") or 0.0
            mv = float(qty) * float(avg)
        by_ticker[t] = float(mv or 0.0)

    cols = [c for c in prices.columns if c in by_ticker and by_ticker[c] > 0]
    if not cols:
        result["data_quality"]["note"] = (
            "None of the holdings with price history carry a positive value."
        )
        return result

    prices = prices[cols]
    values = np.array([by_ticker[c] for c in cols], dtype=float)
    total_value = float(values.sum())
    weights = values / total_value

    rets = daily_returns(prices)
    n = int(len(rets))
    result["observations"] = n
    if n:
        result["period"] = f"{rets.index[0].date()}..{rets.index[-1].date()}"

    if n < 2:
        result["data_quality"]["note"] = (
            f"Only {n} usable observation(s) — not enough to measure dispersion."
        )
        return result

    port_rets = portfolio_returns(prices, weights)
    cov = covariance_matrix(rets)
    marginal, component, sigma_p = marginal_risk_contribution(cov, weights)
    corr = correlation_matrix(rets)

    result["portfolio_volatility"] = round(sigma_p, 6)
    result["value_at_risk"] = value_at_risk(port_rets, confidence)
    result["conditional_var"] = conditional_var(port_rets, confidence)
    result["max_drawdown"] = max_drawdown(equity_curve(port_rets))

    # Per-position detail, ordered by how much risk each one actually carries.
    positions = []
    for i, t in enumerate(cols):
        vol_i = volatility(rets[t])
        comp = float(component[i]) if len(component) else 0.0
        positions.append({
            "ticker": t,
            "weight": round(float(weights[i]), 6),
            "market_value": round(float(values[i]), 2),
            "volatility": vol_i,
            "marginal_risk_contribution": (
                round(float(marginal[i]), 6) if len(marginal) else None
            ),
            "risk_contribution": round(comp, 6),
            # The headline comparison: risk share vs. capital share. A position
            # far above its weight is the concentration the user cannot see by
            # looking at position sizes alone.
            "risk_contribution_pct": (
                round(comp / sigma_p, 6) if sigma_p > 0 else None
            ),
        })
    positions.sort(key=lambda p: p["risk_contribution"] or 0.0, reverse=True)
    result["positions"] = positions

    if not corr.empty:
        result["correlation_matrix"] = {
            c: {k: round(float(v), 4) for k, v in corr[c].items()}
            for c in corr.columns
        }
        # Mean of the off-diagonal entries: the portfolio's overall co-movement.
        vals = corr.to_numpy()
        off = vals[~np.eye(len(vals), dtype=bool)]
        result["average_correlation"] = round(float(off.mean()), 4) if off.size else None

    top = positions[0] if positions else None
    result["concentration"] = {
        "largest_weight": round(float(weights.max()), 6),
        "largest_position": cols[int(weights.argmax())],
        "top_risk_position": top["ticker"] if top else None,
        "top_risk_share": top["risk_contribution_pct"] if top else None,
        "position_count": len(cols),
        # Herfindahl index: 1.0 is a single position, 1/n is perfectly even.
        "herfindahl": round(float(np.sum(weights ** 2)), 6),
    }

    sufficient = n >= MIN_OBSERVATIONS
    result["data_quality"] = {
        "sufficient": sufficient,
        "note": (
            f"{n} aligned trading days across {len(cols)} position(s)."
            if sufficient else
            f"Only {n} aligned trading days (fewer than {MIN_OBSERVATIONS}) — "
            f"these estimates are noisy and should be read as indicative only."
        ),
    }
    return result
