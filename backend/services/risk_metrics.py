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
    #
    # `+ 0.0` normalizes negative zero, which a zero-variance book (all cash in
    # the base currency) produces: `max(-0.0, 0.0)` returns -0.0 in Python, and
    # "-0.0%" on screen is indistinguishable from a bug.
    return round(max(loss, 0.0), 6) + 0.0


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
    return round(max(-float(tail.mean()), 0.0), 6) + 0.0   # normalize -0.0


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
    asset_currency: str | None = None,
    base_currency: str = "KRW",
) -> dict:
    """
    What happens to total portfolio risk if this position grows or shrinks?

    This is blueprint §2's question stated directly: "quantifies exactly how
    adding a new stock (or increasing position size) alters the total portfolio
    risk". ``delta_weight`` is an absolute change in portfolio fraction — 0.05
    means "take this position 5 percentage points higher".

    **Funding order**: cash in the asset's own currency, then any other cash,
    then pro-rata from the remaining positions. That is the order the money would
    actually move, and it matters — funding a dollar purchase out of dollar cash
    leaves FX exposure unchanged, while funding it out of won cash raises it.
    Draining other positions to buy one, which is what the previous version
    always did, is the *last* resort a real investor reaches for.

    ``funded_from`` reports which pocket was used, so the scenario describes a
    trade the user could actually place.
    """
    empty = {
        "ticker": ticker, "delta_weight": delta_weight,
        "volatility_before": None, "volatility_after": None,
        "volatility_change": None, "funded_from": None, "note": None,
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
    if abs(total - 1.0) > 1e-6:
        w = w / total          # legacy callers pass equity-only weights

    cov = covariance_matrix(returns)
    before = portfolio_volatility_from_cov(cov, w)

    i = cols.index(t)
    target = w[i] + delta_weight
    if target < 0:
        return {**empty, "volatility_before": round(before, 6),
                "note": f"Cannot reduce {t} by {abs(delta_weight):.0%} — "
                        f"it is only {w[i]:.1%} of the portfolio."}
    if target > 1.0 + 1e-9:
        return {**empty, "volatility_before": round(before, 6),
                "note": f"Cannot take {t} to {target:.0%} of net worth — that is "
                        f"more than the whole portfolio."}

    w_new = w.copy()
    w_new[i] = target
    need = delta_weight                     # weight that must come from somewhere
    sources: list[str] = []

    own = f"{CASH_PREFIX}{(asset_currency or '').strip().upper()}"
    other_cash = [
        j for j, c in enumerate(cols)
        if c.startswith(CASH_PREFIX) and c != own and j != i
    ]
    pockets: list[tuple[str, list[int]]] = []
    if own in cols and cols.index(own) != i:
        pockets.append((f"{asset_currency} cash", [cols.index(own)]))
    if other_cash:
        pockets.append(("other cash", other_cash))

    for label, indices in pockets:
        if need <= 1e-12:
            break
        available = float(w_new[indices].sum())
        drawn = min(available, need)
        if drawn <= 1e-12:
            continue
        # Proportionally within the pocket.
        for j in indices:
            share = float(w_new[j]) / available if available > 0 else 0.0
            w_new[j] -= drawn * share
        need -= drawn
        sources.append(label)

    if need > 1e-12:
        equity_idx = [
            j for j in range(len(w)) if j != i and not cols[j].startswith(CASH_PREFIX)
        ]
        pool = float(w_new[equity_idx].sum()) if equity_idx else 0.0
        if pool <= 1e-12:
            return {**empty, "volatility_before": round(before, 6),
                    "funded_from": ", ".join(sources) or None,
                    "note": "Not enough cash or other positions to fund the change."}
        for j in equity_idx:
            w_new[j] *= (pool - need) / pool
        sources.append("other positions, pro-rata")

    after = portfolio_volatility_from_cov(cov, w_new)
    return {
        "ticker": t,
        "delta_weight": delta_weight,
        "weight_before": round(float(w[i]), 6),
        "weight_after": round(float(target), 6),
        "volatility_before": round(before, 6),
        "volatility_after": round(after, 6),
        "volatility_change": round(after - before, 6),
        "funded_from": " then ".join(sources) or None,
        "note": None,
    }


def simulate_conversion(
    returns: pd.DataFrame,
    weights: np.ndarray,
    from_currency: str,
    share: float,
    base_currency: str = "KRW",
) -> dict:
    """
    What converting part of a foreign cash balance back to base currency does.

    A lever that touches **no equity position at all** and is currently invisible
    in the app: for someone who converts won to dollars in order to invest, how
    much of the portfolio's risk comes from simply holding those dollars is a
    real question with a real answer.

    ``share`` is the fraction of that currency's cash to convert (0.5 = half).
    """
    src = f"{CASH_PREFIX}{(from_currency or '').strip().upper()}"
    dst = f"{CASH_PREFIX}{base_currency}"
    out = {
        "scenario": "convert_cash",
        "from_currency": from_currency,
        "to_currency": base_currency,
        "share": share,
        "volatility_before": None, "volatility_after": None,
        "volatility_change": None, "note": None,
    }
    if returns is None or returns.empty:
        return {**out, "note": "No return data available."}

    cols = list(returns.columns)
    if src not in cols:
        return {**out, "note": f"No {from_currency} cash is held."}

    w = np.asarray(weights, dtype=float).copy()
    cov = covariance_matrix(returns)
    before = portfolio_volatility_from_cov(cov, w)

    i = cols.index(src)
    moved = float(w[i]) * max(0.0, min(1.0, share))
    w[i] -= moved
    if dst in cols:
        w[cols.index(dst)] += moved
    # If there is no base-currency cash column, the converted money simply stops
    # carrying exchange-rate risk — which is the point of the scenario.

    after = portfolio_volatility_from_cov(cov, w)
    return {
        **out,
        "converted_weight": round(moved, 6),
        "volatility_before": round(before, 6),
        "volatility_after": round(after, 6),
        "volatility_change": round(after - before, 6),
        "note": (
            f"Converting {share:.0%} of {from_currency} cash moves "
            f"{moved:.2%} of net worth out of exchange-rate exposure."
        ),
    }


# =============================================================================
# Aggregate
# =============================================================================

# Cash enters the covariance matrix as an ordinary column, named like this so a
# reader of the correlation matrix can tell it from a ticker.
CASH_PREFIX = "CASH:"

# The opportunity cost of holding won. `price_provider.fetch_treasury_yield`
# supplies ^TNX, the US 10-year — correct for the dollar leg and simply wrong for
# the won leg. Rather than silently apply a US yield to won and call the result
# cash drag, this is left unset unless the user configures it.
_KRW_RISK_FREE_ENV = "KRW_RISK_FREE_RATE"


def krw_risk_free_rate() -> float | None:
    """The configured KRW risk-free rate, or ``None`` if the user has not set one."""
    import os

    raw = (os.environ.get(_KRW_RISK_FREE_ENV) or "").strip()
    if not raw:
        return None
    try:
        rate = float(raw)
    except ValueError:
        logger.warning(f"[risk] {_KRW_RISK_FREE_ENV}={raw!r} is not a number.")
        return None
    return rate if 0 <= rate < 1 else None


def cash_return_columns(
    cash: dict[str, float],
    fx_returns: pd.Series | None,
    index: pd.Index,
    base_currency: str = "KRW",
) -> pd.DataFrame:
    """
    One return series per cash currency held, aligned to ``index``.

    **Cash is risk-free only in its own currency.** Won cash, for a won-based
    investor, is a genuine zero-variance column. Dollar cash is not: measured in
    won it carries the full USDKRW volatility and moves with the rest of the
    book. Giving it the exchange rate's own return series is the whole treatment
    — no special case, no separate factor model, and Euler's identity keeps
    holding across the extended matrix.

    A currency with no rate history is omitted rather than assumed risk-free,
    which would understate the book.
    """
    columns: dict[str, pd.Series] = {}
    for currency, amount in sorted((cash or {}).items()):
        if abs(float(amount or 0.0)) < 1e-9:
            continue
        name = f"{CASH_PREFIX}{currency}"
        if currency == base_currency:
            columns[name] = pd.Series(0.0, index=index)
        elif fx_returns is not None and not fx_returns.empty:
            columns[name] = fx_returns.reindex(index).fillna(0.0)
        else:
            logger.warning(
                f"[risk] no exchange-rate history for {currency} cash; it is "
                f"excluded rather than treated as risk-free."
            )
    return pd.DataFrame(columns, index=index) if columns else pd.DataFrame(index=index)


def compute_portfolio_risk(
    holdings: list[dict],
    prices: pd.DataFrame,
    confidence: float = 0.95,
    cash: dict[str, float] | None = None,
    fx_returns: pd.Series | None = None,
    base_currency: str = "KRW",
    local_prices: pd.DataFrame | None = None,
) -> dict:
    """
    Every metric in blueprint §2, in one JSON-friendly dict for the agent.

    ``holdings`` needs ``ticker`` plus a value: ``market_value_krw`` (preferred —
    base currency, so nothing is added across denominations), else
    ``market_value``, else ``quantity`` × ``avg_price``.

    ``prices`` should be **base-currency** series
    (``price_provider.fetch_price_history_base``). Converting before computing
    returns is what puts the stock/exchange-rate correlation inside the return
    series, so the existing covariance path finds it with no new machinery. That
    correlation is not a detail: USDKRW tends to rise when risk assets fall, so
    for a won-based investor dollar exposure is a partial hedge. A model blind to
    it gets the sign wrong, not merely the magnitude.

    ``cash`` adds one column per currency held (see :func:`cash_return_columns`),
    so **weights are over net worth and sum to 1.0** — the old behaviour divided
    by equity value alone and therefore modelled every portfolio as fully
    invested, roughly doubling the reported risk of a book holding half cash.

    ``local_prices`` (native-currency series) enables the hedged-volatility
    comparison in ``fx_risk``; without it that block reports what it can.

    Passing no ``cash`` reproduces the previous behaviour exactly.

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

    cash = {c: float(v or 0.0) for c, v in (cash or {}).items()
            if abs(float(v or 0.0)) > 1e-9}

    if not holdings and not cash:
        result["data_quality"]["note"] = (
            "No positions in the portfolio — there is no risk to measure yet."
        )
        return result

    if (prices is None or prices.empty) and not cash:
        result["data_quality"]["note"] = (
            "No aligned price history was available for these holdings, so no "
            "risk metrics could be computed."
        )
        return result

    # Weight by base-currency market value where known; fall back to cost basis.
    by_ticker = {}
    for h in holdings or []:
        t = (h.get("ticker") or "").strip().upper()
        mv = h.get("market_value_krw")
        if mv is None:
            mv = h.get("market_value")
        if mv is None:
            qty, avg = h.get("quantity") or 0.0, h.get("avg_price") or 0.0
            mv = float(qty) * float(avg)
        by_ticker[t] = float(mv or 0.0)

    price_cols = list(prices.columns) if prices is not None and not prices.empty else []
    cols = [c for c in price_cols if c in by_ticker and by_ticker[c] > 0]
    if not cols and not cash:
        result["data_quality"]["note"] = (
            "None of the holdings with price history carry a positive value."
        )
        return result

    equity_rets = daily_returns(prices[cols]) if cols else pd.DataFrame()
    equity_values = np.array([by_ticker[c] for c in cols], dtype=float)
    equity_value = float(equity_values.sum())

    # Cash joins the same matrix. A portfolio of only cash still has a measurable
    # risk — near zero in won, real in dollars — so it is no longer a "nothing to
    # measure" case.
    index = equity_rets.index if not equity_rets.empty else (
        fx_returns.index if fx_returns is not None and not fx_returns.empty
        else pd.Index([])
    )
    cash_rets = cash_return_columns(cash, fx_returns, index, base_currency)
    cash_cols = list(cash_rets.columns)
    cash_values = np.array(
        [cash.get(c[len(CASH_PREFIX):], 0.0) for c in cash_cols], dtype=float
    )
    # CONTRACT: `cash` amounts are already in BASE CURRENCY, keyed by the
    # currency they are actually held in. The key drives which return series the
    # column gets (won → zero variance, dollars → the exchange rate's own
    # returns); the value is what that cash is worth in won. Converting here
    # would mean a second conversion site, and phase 4 established that there is
    # exactly one.
    cash_value = float(cash_values.sum())

    net_worth = equity_value + cash_value
    if net_worth <= 0:
        # A negative or zero denominator produces sign-flipped weights that look
        # plausible and are not.
        result["data_quality"]["note"] = (
            f"Net worth is {net_worth:,.2f}, so no meaningful weights can be "
            f"formed. Reconcile the cash ledger before reading risk figures."
        )
        return result

    rets = (
        pd.concat([equity_rets, cash_rets], axis=1).dropna(how="any")
        if cash_cols else equity_rets
    )
    all_cols = cols + cash_cols
    weights = np.concatenate([equity_values, cash_values]) / net_worth

    n = int(len(rets))
    result["observations"] = n
    if n:
        result["period"] = f"{rets.index[0].date()}..{rets.index[-1].date()}"

    if n < 2:
        result["data_quality"]["note"] = (
            f"Only {n} usable observation(s) — not enough to measure dispersion."
        )
        return result

    # Weights already sum to 1.0 across equities and cash, so the normalization
    # inside `marginal_risk_contribution` is a no-op and the "rest is in cash"
    # meaning survives.
    port_rets = (rets[all_cols] * weights).sum(axis=1)
    cov = covariance_matrix(rets[all_cols])
    marginal, component, sigma_p = marginal_risk_contribution(cov, weights)
    # Correlation over the equity leg only: a zero-variance cash column has no
    # correlation to report, and NaNs in a displayed matrix are just noise.
    corr = correlation_matrix(equity_rets) if not equity_rets.empty else pd.DataFrame()

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
            "market_value": round(float(equity_values[i]), 2),
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

    # Cash rows, so the extended matrix is auditable and Euler's identity can be
    # checked across the whole book rather than the equity leg alone.
    cash_rows = []
    for j, name in enumerate(cash_cols):
        i = len(cols) + j
        comp = float(component[i]) if len(component) else 0.0
        cash_rows.append({
            "currency": name[len(CASH_PREFIX):],
            "weight": round(float(weights[i]), 6),
            "value": round(float(cash_values[j]), 2),
            "volatility": volatility(rets[name]),
            "risk_contribution": round(comp, 6),
            "risk_contribution_pct": round(comp / sigma_p, 6) if sigma_p > 0 else None,
        })
    result["cash_positions"] = cash_rows

    equity_weight = float(equity_value / net_worth)
    cash_weight = float(cash_value / net_worth)
    foreign_value = sum(
        v for c, v in cash.items() if c != base_currency
    ) + sum(
        by_ticker[c] for c in cols
        if (h_ccy := _holding_currency(holdings, c)) and h_ccy != base_currency
    )

    top = positions[0] if positions else None
    result["concentration"] = {
        # Over NET WORTH now, so a book that is mostly cash no longer reads as
        # concentrated in whatever equity it does hold.
        "largest_weight": round(float(weights.max()), 6),
        "largest_position": all_cols[int(weights.argmax())],
        "top_risk_position": top["ticker"] if top else None,
        "top_risk_share": top["risk_contribution_pct"] if top else None,
        "position_count": len(cols),
        # Herfindahl index over equities AND cash: 1.0 is everything in one
        # place, 1/n perfectly even.
        "herfindahl": round(float(np.sum(weights ** 2)), 6),
        "equity_weight": round(equity_weight, 6),
        "cash_weight": round(cash_weight, 6),
        "fx_exposure": round(float(foreign_value / net_worth), 6),
    }

    result["cash"] = _cash_block(cash, cash_weight, net_worth, base_currency)
    result["fx_risk"] = _fx_risk_block(
        holdings=holdings, cols=cols, by_ticker=by_ticker,
        equity_rets=equity_rets, local_prices=local_prices,
        fx_returns=fx_returns, weights=weights, all_cols=all_cols,
        sigma_p=sigma_p, net_worth=net_worth, confidence=confidence,
        base_currency=base_currency,
        fx_exposure=float(foreign_value / net_worth),
    )

    sufficient = n >= MIN_OBSERVATIONS
    result["data_quality"] = {
        "sufficient": sufficient,
        "note": (
            f"{n} aligned trading days across {len(cols)} position(s)"
            + (f" and {len(cash_cols)} cash currency(ies)." if cash_cols else ".")
            if sufficient else
            f"Only {n} aligned trading days (fewer than {MIN_OBSERVATIONS}) — "
            f"these estimates are noisy and should be read as indicative only."
        ),
    }
    return result


def _holding_currency(holdings: list[dict] | None, ticker: str) -> str | None:
    for h in holdings or []:
        if (h.get("ticker") or "").strip().upper() == ticker:
            return (h.get("currency") or "").strip().upper() or None
    return None


def _cash_block(cash: dict[str, float], cash_weight: float,
                net_worth: float, base_currency: str) -> dict:
    """
    Cash as a position: how much, what share, and what it costs to hold.

    ``cash_drag`` is an opportunity cost, so it needs a risk-free rate **per
    currency**. The app's only yield source is ^TNX, the US 10-year — right for
    the dollar leg and simply wrong for the won leg. Rather than apply a US yield
    to won and call the result cash drag, the won rate is reported as unset until
    the user configures ``KRW_RISK_FREE_RATE``.
    """
    krw_rate = krw_risk_free_rate()
    block: dict = {
        "balances_base": {c: round(float(v), 2) for c, v in cash.items()},
        "total_base": round(float(sum(cash.values())), 2),
        "weight": round(cash_weight, 6),
        "cash_drag": None,
        "cash_drag_note": None,
    }
    if not cash:
        block["cash_drag_note"] = "No cash held."
        return block
    if krw_rate is None:
        block["cash_drag_note"] = (
            "Not computed: no KRW risk-free rate is configured "
            f"({_KRW_RISK_FREE_ENV}). The US 10-year is not a substitute for a "
            "won-denominated opportunity cost."
        )
        return block
    # Every balance is already in base currency, so one base-currency rate is
    # the right opportunity cost for all of it.
    block["cash_drag"] = round(cash_weight * krw_rate, 6)
    block["cash_drag_note"] = (
        f"{cash_weight:.1%} of net worth earning nothing against a "
        f"{krw_rate:.2%} {base_currency} risk-free rate."
    )
    return block


def _fx_risk_block(*, holdings, cols, by_ticker, equity_rets, local_prices,
                   fx_returns, weights, all_cols, sigma_p, net_worth,
                   confidence, base_currency, fx_exposure) -> dict:
    """
    What the currency exposure actually does to **this** book.

    ``hedged_volatility`` rebuilds the same equity portfolio from
    **local-currency** returns and runs the identical covariance path. The
    difference from the base-currency figure is ``fx_contribution``, and it can
    legitimately be **negative**: USDKRW tends to rise when risk assets fall, so
    dollar exposure often dampens a won-based portfolio rather than adding to it.

    A model that ignored FX would report that diversification as absent; one that
    added FX as an independent risk would report it as pure added risk. Both are
    wrong, in opposite directions — which is why this is measured against the
    real historical window rather than assumed.
    """
    block: dict = {
        "exposure": round(fx_exposure, 6),
        "fx_volatility": None,
        "fx_var": None,
        "equity_fx_correlation": None,
        "hedged_volatility": None,
        "fx_contribution": None,
        "note": None,
    }
    if fx_returns is None or fx_returns.empty:
        block["note"] = "No exchange-rate history was available."
        return block

    block["fx_volatility"] = volatility(fx_returns)
    # The loss the rate alone could inflict on the exposed share of net worth.
    block["fx_var"] = value_at_risk(fx_returns * fx_exposure, confidence)

    if local_prices is None or local_prices.empty or not cols:
        block["note"] = (
            "Local-currency price history was not supplied, so the hedged "
            "comparison could not be made."
        )
        return block

    local_cols = [c for c in cols if c in local_prices.columns]
    if not local_cols:
        block["note"] = "No local-currency series matched the analyzed holdings."
        return block

    local_rets = daily_returns(local_prices[local_cols])
    equity_values = np.array([by_ticker[c] for c in local_cols], dtype=float)
    equity_total = float(equity_values.sum())
    if equity_total <= 0:
        block["note"] = "No positive equity value to compare."
        return block

    # The equity leg at its real share of NET WORTH, not renormalized to 1.0, so
    # the hedged and unhedged figures describe the same portfolio — same cash
    # cushion, same position sizes — and differ only by the currency.
    # `portfolio_volatility_from_cov` uses the weights exactly as given, so no
    # rescaling is needed (and applying one would double-count the cash drag).
    equity_w = equity_values / net_worth
    hedged = portfolio_volatility_from_cov(covariance_matrix(local_rets), equity_w)

    block["hedged_volatility"] = round(hedged, 6)
    block["fx_contribution"] = round(sigma_p - hedged, 6)

    local_port = (local_rets[local_cols] * (equity_values / equity_total)).sum(axis=1)
    joined = pd.concat([local_port, fx_returns], axis=1).dropna(how="any")
    if len(joined) >= 2:
        block["equity_fx_correlation"] = round(
            float(joined.iloc[:, 0].corr(joined.iloc[:, 1])), 4
        )
    block["note"] = (
        "fx_contribution is portfolio volatility minus the same book with the "
        "currency hedged away. Negative means the exchange rate is dampening "
        "risk, not adding it."
    )
    return block
