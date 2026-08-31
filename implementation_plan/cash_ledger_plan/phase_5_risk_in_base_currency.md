# Phase 5: Risk Measured in the Currency the User Actually Spends

**Goal**: Correct two live errors in `services/risk_metrics.py` at once — it
models every portfolio as fully invested, and it has no concept of currency at
all, so for this user it currently overstates some risks and misses others
entirely.

**Depends on**: Phase 4.

## The two bugs

**One — cash does not exist.** `risk_metrics.py:386`:

```python
values      = np.array([by_ticker[c] for c in cols], dtype=float)
total_value = float(values.sum())     # equity only
weights     = values / total_value    # therefore always sums to 1.0
```

Every downstream metric inherits it. A user holding 50% cash sees roughly double
their true volatility and VaR.

**Two — currency does not exist.** `compute_portfolio_risk` receives
`market_value` figures that (before phase 2) mix won and dollars, and return
series computed from native prices. For a KRW-based investor that measures the
wrong quantity: a US position's risk in won includes the exchange rate, and the
**correlation between the stock and the rate** is a large part of the answer.

## Why the fix is small, exact, and does not need a factor model

Phase 2 already returns price series denominated in the base currency. Compute
returns from those, and the FX exposure is inside the return series — so the
existing covariance matrix, `portfolio_volatility_from_cov`,
`marginal_risk_contribution` and `value_at_risk` all become currency-correct with
**no new estimation machinery**. The stock/FX correlation is discovered
empirically rather than assumed.

This matters for the direction, not just the magnitude. USDKRW tends to rise when
global risk assets fall — the won is a risk-on currency and the dollar a haven —
so for a Korean investor **USD exposure is a partial hedge that lowers total
portfolio volatility**. A model that ignores FX would report the diversification
as absent; a model that adds FX as an independent risk would report it as pure
added risk. Both are wrong, and in opposite directions.

## Cash is risk-free only in its own currency

An earlier draft of this plan said "cash is a risk-free asset, σ = 0". That is
true of **KRW** cash for this user and false of **USD** cash, which carries the
full USDKRW volatility (historically on the order of 8-10% annualized) and is
correlated with the rest of the book.

The clean treatment needs no special case: **USD cash enters the covariance
matrix as an asset whose return series is the USDKRW return series.** Only
base-currency cash is a genuine zero-volatility column. Euler's identity then
holds across the whole book, cash included, and USD cash can correctly show a
*negative* marginal risk contribution — it is diversifying.

## Tasks

1. **Signature**

   ```python
   compute_portfolio_risk(holdings, prices, confidence=0.95,
                          cash: dict[str, float] | None = None,
                          fx_returns: pd.Series | None = None,
                          base_currency: str = "KRW")
   ```

   `cash=None` preserves today's behaviour exactly, so the change lands without a
   flag day for any caller not yet updated.

   ```python
   net_worth   = equity_value_base + cash_base
   weights     = values_base / net_worth        # equity weights; sum to < 1
   ```

   `portfolio_returns(prices, weights)` needs **no change** — it already accepts
   an arbitrary weight vector, and weights summing to less than one correctly
   express "the rest is in cash".

   Append one column per non-base cash currency, its returns being `fx_returns`,
   and one zero-variance column for base-currency cash. Guard `net_worth <= 0`
   the way the function already guards degenerate input
   (`risk_metrics.py:335-352`): a well-formed dict of nulls with an explanation,
   never an exception.

2. **FX risk decomposition** — new, and the reason the phase exists

   ```python
   "fx_risk": {
       "exposure":            <share of net worth in foreign currency>,
       "fx_volatility":       <annualized USDKRW vol>,
       "fx_var":              <VaR attributable to the rate alone>,
       "equity_fx_correlation": <corr(portfolio equity returns, fx returns)>,
       "hedged_volatility":   <portfolio vol if FX were fully hedged>,
       "fx_contribution":     <portfolio vol minus hedged vol; may be NEGATIVE>,
   }
   ```

   `hedged_volatility` is computed by rebuilding the same portfolio from
   **local-currency** return series and re-running the identical covariance path.
   The difference is what the currency exposure actually does to this specific
   book — a directly interpretable number, and negative when the dollar is
   hedging Korean equity risk.

3. **Concentration over net worth, including cash**

   Herfindahl over equity-only weights always reads more concentrated than the
   user's real position. Compute HHI over `weights + cash weights`, report
   `largest_weight` against net worth, and add `cash_weight`, `equity_weight` and
   `fx_exposure` to the `concentration` block so the figure can be interpreted.

4. **Euler's identity, extended — assert it**

   Component contributions `w_i × MRC_i` must still sum to `sigma_p`, now across
   equities *and* cash columns, with base-currency cash contributing exactly
   zero. The existing assertion from `trading_coach_plan` phase 5 stays and now
   also proves no currency term leaked into the attribution.

5. **`simulate_position_change` funds from the right pocket**

   It currently funds an increase pro-rata from the other holdings
   (`risk_metrics.py:249-310`). With real balances the realistic order is **cash
   in the asset's own currency first, then a conversion, then pro-rata from other
   positions.** Report which source was used, and refuse — with a note, not an
   exception — to simulate a purchase larger than net worth.

   Add a scenario the user can now actually evaluate: **"convert X% of USD cash
   back to KRW"**, which changes `fx_exposure` without touching a single equity
   position. For someone converting won to dollars to invest, that is a real
   lever and currently an invisible one.

6. **Cash metrics, with the right risk-free rate per currency**

   ```python
   "cash": {"balances", "weight", "dry_powder_days", "cash_drag"}
   ```

   `cash_drag` is the opportunity cost of holding cash, so it needs a **KRW**
   risk-free rate for won and a USD rate for dollars.
   `price_provider.fetch_treasury_yield` supplies `^TNX`, the US 10-year, which is
   correct for the dollar leg and **wrong for the won leg**. Either add a Korean
   base-rate source or make the KRW rate a user-entered setting — but do not
   silently apply the US yield to won and call it cash drag.

7. **Wire the caller and the prompt**

   `services/pipeline.py:276` already calls `list_holdings()` and skips with a
   clear reason when the portfolio is empty. Pass `cash` and `fx_returns`. A
   portfolio of **only** cash is no longer "nothing to measure": it has near-zero
   risk in won and real risk in dollars, and that is a reportable answer.

   Update `agents/quant_risk_agent.py`'s prompt to state that weights are over net
   worth, that figures are in KRW, and that USD holdings carry currency risk —
   otherwise the model will reasonably assume weights sum to one and describe the
   book as more invested and less currency-exposed than it is. Keep the existing
   enforcement: every numeric field is overwritten from `metrics` after
   generation, so only prose and `risk_score` come from the LLM.

## Definition of done
- A portfolio with 50% **KRW** cash reports exactly half the volatility and VaR of
  the same holdings with no cash.
- A portfolio of 100% **USD** cash reports **non-zero** volatility equal to the
  USDKRW volatility — not zero. This is the single assertion that proves the
  currency treatment is real.
- A portfolio of 100% KRW cash reports exactly zero.
- A US holding's volatility measured in KRW differs from its USD volatility, and
  the difference reconciles to `fx_contribution`.
- `fx_contribution` is **negative** on a book where USDKRW is negatively
  correlated with the equity leg — confirm against a real historical window
  rather than asserting a sign a priori.
- Weights plus cash weights sum to 1.0; Euler's identity still holds across the
  extended matrix.
- Existing callers passing no `cash` get byte-identical output to today.
