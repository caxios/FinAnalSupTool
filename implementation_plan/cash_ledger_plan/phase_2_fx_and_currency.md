# Phase 2: Asset Currency, FX, and Base-Currency Price Series

**Goal**: Stop the app from adding won to dollars, fetch USDKRW for both display
and recording, and produce **price series already denominated in the base
currency** — the input every later phase depends on.

**Depends on**: Phase 1.

## Task 1 is a bug fix and should land first

`value_holdings` (`portfolio_service.py:376-426`) fetches a price per ticker and
accumulates `quantity × price` into `total_market_value` **without ever reading
`holdings.currency`**. yfinance returns `005930.KS` in won, so ten Samsung shares
add `710,000` to a dollar total. Nothing raises, and `risk_metrics` inherits the
corrupted weights through `market_value`.

Ship the guard before anything else in this plan:

```python
# Refuse to produce a total that mixes denominations.
if len({h["currency"] for h in rows}) > 1 and fx is None:
    totals["total_market_value"] = None
    totals["note"] = ("Holdings span more than one currency and no exchange "
                      "rate is available, so no single total can be stated.")
```

Per-row figures stay correct and are labelled with their own currency; only the
*total* is withheld. This is the same posture `value_holdings` already takes for
an unpriced ticker (`portfolio_service.py:403`) — report less rather than report
wrong. Tasks 2-5 then make the total possible instead of merely honest.

## The rule the rest of the phase enforces

> **Convert each price series to the base currency first. Compute returns second.**

Not the other way around, and not "convert the final value". Converting first is
what lets the *existing* covariance machinery in `services/risk_metrics.py`
discover the correlation between a US stock and the exchange rate, because that
correlation is then already inside the return series. Phase 5 depends entirely on
this ordering.

## Tasks

2. **New `backend/providers/fx_provider.py`**

   Mirror `price_provider`'s structure exactly — blocking yfinance work in a
   `_compute_*` function, an `async` wrapper over `asyncio.to_thread`, a small TTL
   cache. Reuse its `_flatten_columns` idiom for MultiIndex columns and its
   `_bar_at_or_before` approach (`price_provider.py:418`) for "the rate at or
   before this timestamp"; do not re-solve either.

   ```python
   _FX_TICKER   = "KRW=X"        # USD -> KRW. Verify at implementation time;
   _FX_FALLBACK = "USDKRW=X"     # fall back if the primary returns no history.
   _SPOT_TTL_SECONDS = 300.0

   @dataclass
   class FxQuote:
       pair: str          # "USDKRW"
       rate: float        # KRW per 1 USD
       as_of: datetime
       is_stale: bool
       source: str        # "spot" | "daily_close" | "manual"

   async def fetch_spot() -> FxQuote
   async def fetch_rate_at(when: datetime) -> FxQuote        # daily close at/before
   async def fetch_fx_history(start, end) -> pd.Series       # daily USDKRW, for task 4
   ```

   - Spot TTL is 300s rather than the 60s `price_provider` uses for equities: FX
     moves less over a minute and the rate is applied to every row on the page.
   - `fetch_rate_at` uses the **daily close**, not intraday. yfinance intraday FX
     coverage is thin, and pretending to minute-level FX precision on a trade
     whose price is already a bar approximation would be false precision.
   - A weekend or holiday timestamp resolves to the **nearest prior** close,
     the rule `_bar_at_or_before` already applies to equities.

3. **Failure policy — reading and writing must not share a path**

   | Situation | Behaviour |
   |---|---|
   | **Display** (rendering a portfolio) | Non-fatal. Return `fx: null`, render base-currency totals as `—`, keep every native-currency figure. One unavailable rate must not blank a working portfolio. |
   | **Recording** (writing a cash flow) | Fatal. Reject with 400 and ask for the rate. `fx_to_krw` is `NOT NULL` precisely so a guess cannot be papered over — a wrong rate corrupts the cost basis permanently. |

   `FxQuote.source == "manual"` marks a user-supplied rate so it stays auditable.

4. **Base-currency price series** — `providers/price_provider.py`

   ```python
   async def fetch_price_history_base(
       tickers: list[str],
       start, end,
       currencies: dict[str, str],      # ticker -> native currency
       base: str = "KRW",
   ) -> tuple[pd.DataFrame, list[str]]
   ```

   For each ticker: fetch its native series, then multiply the USD-denominated
   ones by the aligned daily USDKRW series. KRW-denominated ones pass through
   untouched.

   Build on the existing `_fetch_price_history` (`price_provider.py:630`), which
   already drops short-history tickers **before** the inner join so one new
   listing cannot truncate everyone else's history. Two additions:

   - The USDKRW series joins that same inner join, so a day without an FX quote
     is dropped rather than forward-filled into a fake observation.
   - **Korean and US market holidays do not coincide** (추석, 설날, Thanksgiving,
     Independence Day). An inner join across both calendars drops every date
     either market was shut, which for a mixed portfolio removes roughly 15-20
     sessions a year. That is the correct behaviour — a return computed across a
     day one leg did not trade is not a return — but it lowers the observation
     count, so report `observations` and let `MIN_OBSERVATIONS = 30`
     (`risk_metrics.py`) do its job rather than silently proceeding on thin data.

5. **Wire the rate into `cash_service.record_flow`**

   When the caller passes no `fx_to_krw`, resolve it from `occurred_at` via
   `fetch_rate_at`. Keep the parameter: a user reconciling against a broker
   statement has a more authoritative rate than a daily close.

6. **Backfill the dead columns**

   `trades.fx_rate` and `holdings.initial_fx_rate` are null or 1.0 on every
   existing row. Add `cash_service.backfill_fx()` to walk null rows, resolve
   `fetch_rate_at(executed_at)`, and fill them. Run it once from the phase 1
   initialization endpoint, and **report unresolved rows by count rather than
   defaulting them to 1.0** — `fx_rate = 1.0` silently asserts that one dollar is
   one won.

7. **Expose the rate once per response**

   ```python
   class FxInfo(BaseModel):
       pair: str = "USDKRW"
       rate: float | None
       as_of: datetime | None
       is_stale: bool = False
       source: str | None = None
   ```

   One `fx` object per response, not one per row: every converted figure on a
   page uses the same rate, and repeating it invites drift.

## Definition of done
- A portfolio holding both `AAPL` and `005930.KS` no longer reports a nonsense
  total — before task 4 it withholds the total; after it, the total is correct
  and matches a hand calculation in won.
- `fetch_spot()` returns a plausible USDKRW rate; a second call within 300s does
  not touch the network.
- `fetch_rate_at` for a Saturday returns Friday's close, stamped with its real
  `as_of` rather than the requested date.
- `fetch_price_history_base` returns a frame where the `AAPL` column is
  won-denominated and moves with both the stock and the rate, while `005930.KS`
  is unchanged from its native series.
- Days on which either market was closed are absent from the joined frame, and
  `observations` reflects it.
- Recording a deposit with no rate stamps the resolved historical rate; doing so
  while FX is unavailable returns 400 and writes nothing.
- With the network down, `GET /portfolio` still returns every native-currency
  figure and reports `fx: null`.
