# Phase 4: Net Worth, Weights, and Return Split Into Its Real Components

**Goal**: Answer the three questions the portfolio cannot currently answer — *what
am I worth*, *what is each thing's share of it*, and *how much of my return came
from the stocks versus the exchange rate*.

**Depends on**: Phase 3.

## The attribution identity

For any position, in base currency:

```
1 + R_base = (1 + R_local) x (1 + R_fx)

R_local = local_price_now / effective_entry_price - 1     # the stock's own return
R_fx    = fx_now / effective_entry_fx - 1                 # the currency's return
R_fx    = 0 for a KRW-denominated asset
```

Exact and multiplicative, with a **cross term** — a 10% stock gain on a 10%
currency gain is +21%, not +20%. Report `R_local`, `R_fx` and `R_base`
separately; never present `R_base` alone, because the user's two decisions
(what to buy, and when to convert) are separate decisions and deserve separate
scorecards.

`effective_entry_fx = cost_base / cost_local`, taken from the ledger's buy flows
for that ticker — the capital-weighted rate at which the position was actually
funded. This is why phase 1 makes `fx_to_krw` `NOT NULL`.

## Tasks

1. **Net worth and weights** — extend `portfolio_service.value_holdings`

   It already returns `(valued, totals)` and already excludes unpriced rows from
   the totals rather than counting them as zero
   (`portfolio_service.py:418-425`). Keep that rule and add:

   **Every amount ships in both currencies** (README decision 0). No field is
   base-currency-only, and no field is native-currency-only.

   ```python
   # per holding
   "currency":            "USD" | "KRW",       # what it actually trades in
   "market_value",                              # native, the traded figure
   "market_value_krw",   "market_value_usd",
   "cost_basis_krw",     "cost_basis_usd",      # from the ledger's stored rates
   "unrealized_pnl_krw", "unrealized_pnl_usd",
   "roi_local", "roi_fx", "roi_krw", "roi_usd", # ratios: currency-labelled
   "weight",                                    # share of NET WORTH, not equity

   # totals
   "cash_balances":     {"KRW": …, "USD": …},   # the actual balances held
   "cash_total_krw",    "cash_total_usd",       # all cash, expressed in each
   "equity_total_krw",  "equity_total_usd",
   "net_worth_krw",     "net_worth_usd",
   "cash_weight", "equity_weight",
   "fx_exposure",                               # foreign-denominated share
   "roi_local_total", "roi_fx_total", "roi_krw_total", "roi_usd_total",
   ```

   `market_value_krw` and `market_value_usd` are the *same wealth* stated twice,
   so exactly one of them equals `market_value` for any given holding. That
   redundancy is deliberate: it means no consumer of this API ever has to know a
   conversion rule, and the frontend never multiplies by a rate.

   `roi_usd` for a Korean holding is a real and different number from
   `roi_local` — a Samsung position flat in won *lost* value in dollars if the
   won weakened. Compute it; do not alias it.

   `weight` divides by **net worth**, so every weight plus `cash_weight` sums to
   1.0. Dividing by equity value — what `risk_metrics` does today — describes a
   portfolio the user does not have.

   Edge case: `net_worth <= 0` (fully withdrawn, or an unreconciled negative
   balance). Return `weight: null` throughout rather than dividing — a negative
   denominator produces sign-flipped weights that look plausible and are not.

2. **`fx_exposure` is a first-class number**

   ```
   fx_exposure = (USD equity at spot + USD cash at spot) / net_worth_base
   ```

   The share of this user's wealth whose base-currency value moves with USDKRW.
   It is the input to phase 5's risk decomposition and the single figure that
   summarises "how exposed am I to the dollar" — currently unavailable anywhere
   in the app.

3. **Net-worth history** — new `backend/services/performance.py`

   ```python
   async def net_worth_series(start=None, end=None) -> pd.DataFrame
       # index: date
       # columns: equity_krw, equity_usd, cash_krw, cash_usd,
       #          net_worth_krw, net_worth_usd, fx_rate
   ```

   Reconstruct by replay: for each date, the quantity held that day (from the
   journal) times that day's close in **base currency** (from
   `fetch_price_history_base`, phase 2), plus `cash_service.balances(as_of=date)`
   converted at that day's rate — **not** at today's rate. Using today's rate for
   historical cash would rewrite the past every time the currency moves.

   **State the limit honestly.** The series can only begin where the ledger
   begins; seeded positions have no real acquisition date, so nothing before
   initialization is reconstructible. Return `coverage_start` and have the UI
   label the chart rather than drawing a confident line through invented history.

4. **Time-weighted and money-weighted return** — same module

   ```python
   def time_weighted_return(series, external_flows) -> dict
   def money_weighted_return(flows, ending_value) -> dict   # XIRR
   ```

   - **TWR** breaks the series at each external flow (`EXTERNAL_FLOWS`, phase 1),
     computes each sub-period's return on its pre-flow value, and chains them:
     `prod(1 + r_i) - 1`. Return per unit of capital — it measures selection and
     is unaffected by deposit timing. **Conversions are internal and must not
     break the series**; if they do, every 환전 shows up as fake performance.
   - **MWR** is the IRR of the dated external flows plus the ending value, solved
     by Newton-Raphson with a bisection fallback. IRR does not always converge and
     a sign-changing series can have multiple roots — on failure return `null`
     with a note rather than a wrong number.
   - Report both, cumulative and annualized, **and in both a base-currency and a
     local-currency form**, so the user can see how much of their measured skill
     was actually the won moving.

5. **API** — extend `PortfolioResponse`; add `GET /portfolio/performance`

   Additive fields only — `_krw` and `_usd` siblings alongside the existing
   native-currency fields, plus phase 2's single `fx` object. This keeps every
   response shape from `trading_coach_plan` phases 2-6 valid and avoids a frontend
   rewrite; a nested `Money {krw, usd}` type is cleaner in the abstract but churns
   every consumer for no behavioural gain.

   The dual-currency requirement makes this the **only** place conversion is
   allowed to happen. Nothing downstream — not the frontend, not an agent — may
   multiply an amount by a rate; they read the pair. One conversion site means
   one place for a rounding or staleness bug to live.

   `GET /portfolio/performance?window=1m|3m|1y|all` returns the series, TWR, MWR,
   the three-way attribution, realized P/L, realized FX P/L, fees, taxes,
   conversion spread paid, and `coverage_start`.

## Definition of done
- Weights plus `cash_weight` sum to 1.0 (within 1e-9) on a mixed KRW/USD
  portfolio.
- `net_worth_krw == equity_total_krw + cash_total_krw` and the same identity in
  USD, both checked against hand-entered figures.
- On a portfolio holding a Korean stock, a US stock, won cash and dollar cash,
  **every** amount field comes back populated in both currencies, and
  `net_worth_krw / net_worth_usd` equals the reported spot rate.
- The identity `(1 + roi_base) == (1 + roi_local)(1 + roi_fx)` holds per
  position, asserted in tests the way `trading_coach_plan` phase 5 asserts
  Euler's identity — an exact algebraic relationship is the cheapest possible
  check that the whole conversion chain is right.
- A Korean holding reports `roi_fx == 0` and `roi_base == roi_local` exactly.
- A US position flat in dollars while USDKRW fell 7% reports `roi_local ≈ 0`,
  `roi_fx ≈ -7%`, `roi_base ≈ -7%`. **This is the case the phase exists for.**
- Deposit new money into a flat portfolio: **TWR stays ~0%, MWR changes, net
  worth rises.**
- A 환전 with no market movement changes **no** return figure — only the balances.
- A single-currency, zero-cash portfolio with no deposits reports
  `TWR == MWR == total_roi`, proving the new path agrees with the old one where
  they overlap.
