# Phase 3: Trades Move Cash, 환전 Is an Event, and Sells Realize Two Kinds of Gain

**Goal**: Make every trade a two-sided entry, model currency conversion as a
first-class event, and capture both the equity gain and the **exchange-rate gain**
a sale realizes.

**Depends on**: Phases 1, 2.

## Tasks

1. **Couple `record_trade` to the ledger** (`services/portfolio_service.py`)

   `record_trade` already performs its position read-modify-write inside one
   `db.transaction()` (`portfolio_service.py:235`). Extend **that same block** —
   do not open a second transaction — passing the open `conn` into
   `cash_service.record_flow`:

   ```
   buy   -> flow_type="buy",  amount = -(execution_price * quantity) - fees
   sell  -> flow_type="sell", amount = +(execution_price * quantity) - fees
   ```

   Two rules that are easy to get wrong:

   - **The flow's `currency` is the asset's currency**, from
     `resolve_asset_currency`. A KOSPI buy debits the won balance; an `AAPL` buy
     debits the dollar balance. A trade never crosses currencies — crossing is
     what task 3 is for, and conflating the two is what makes brokerage
     statements unreadable.
   - **`occurred_at` is the trade's `executed_at`, not now.** A back-dated trade
     must move cash on the day it happened, or the net-worth series phase 4
     reconstructs will be wrong on every day in between.

   When `execution_price is None` — the journal-only case `record_trade` already
   supports — write **no** cash flow and return a warning. A cash movement of
   unknown size is not something to guess at.

2. **Insufficient cash warns; it does not reject**

   Deliberate. The app supports back-filling history, and a user entering three
   months of trades out of order will transiently go negative through no fault of
   their own. Blocking would make correct data unenterable.

   - Check `balance(asset_currency, as_of=executed_at)` before a buy.
   - If the buy exceeds it, still record, and return
     `cash_warning: {currency, shortfall, balance_before, note}`.
   - Surface it in the UI (phase 6) and expose it to the coach (phase 7) —
     spending money you do not have is behaviourally interesting.

   A **negative final balance** is a different thing from a transient one, and
   `GET /portfolio/cash` should report it as a reconciliation prompt.

3. **환전 as a first-class event** — `cash_service.convert(...)`

   ```python
   def convert(from_currency, from_amount, to_currency, to_amount,
               occurred_at, note=None) -> dict
       """One conversion, written as two linked legs in one transaction."""
   ```

   Writes `fx_out` (negative, `from_currency`) and `fx_in` (positive,
   `to_currency`) sharing a `conversion_id`. The **effective rate is derived from
   the two amounts** (`from_amount / to_amount`), not fetched — the user's bank
   or broker charged a spread over the mid-market rate, and that spread is a real
   cost that only their own numbers contain. Store the market rate alongside it
   so the spread is visible; over a year of conversions it is not small.

   Conversions are **internal flows**: excluded from `EXTERNAL_FLOWS`, so they do
   not distort the TWR calculation in phase 4. No money entered or left.

4. **Realized FX gain on conversion back to base currency**

   When `to_currency == BASE_CURRENCY`, the conversion realizes an exchange-rate
   gain or loss against the average rate at which those dollars were acquired:

   ```
   effective_acquisition_rate = <weighted average fx_to_krw over the USD balance>
   realized_fx_pnl = from_amount * (conversion_rate - effective_acquisition_rate)
   ```

   Compute the acquisition rate as an average-cost figure over the USD balance,
   for the same reason positions use average cost: the codebase is built on it
   (`portfolio_service.py:23-27`), and mixing conventions between the two would
   make the totals fail to reconcile.

   Store on the `fx_in` leg as `realized_fx_pnl_krw`. **Korean tax treatment of
   overseas gains is FIFO-based, so label this figure as decision-support and not
   a tax number** — the same caveat the README records for equity lots.

5. **Optional fees and taxes on a trade**

   Add optional `fee` and `tax` to `TradeCreate` (default 0), each written as its
   own typed row linked by `trade_id` rather than folded into the trade amount.
   Keeping them separate lets phase 4 report gross versus net and lets the user
   see what friction actually cost over a year — which for cross-border trading
   includes the conversion spread from task 3.

6. **Realized P/L on sells — in both currencies**

   Add two columns to `trades`. `CREATE TABLE IF NOT EXISTS` will not alter an
   existing table and SQLite has no `ADD COLUMN IF NOT EXISTS`, so guard an
   `ALTER TABLE` with a `PRAGMA table_info` check inside `db.init_db`:

   ```sql
   realized_pnl       REAL,   -- in the asset's own currency
   realized_pnl_base  REAL    -- in KRW, at the rates that actually applied
   ```

   ```
   realized_pnl      = (execution_price - avg_price_before) * quantity - fees
   realized_pnl_base = execution_price   * quantity * fx_at_sale
                     - avg_price_before  * quantity * effective_entry_fx
   ```

   `avg_price_before` is the average *before* this sell, which `_apply_trade`
   already holds as `old_avg` and currently discards. This preserves the module's
   average-cost convention: the sale realizes the spread against the running
   average and leaves the average itself alone.

   For a **Korean** holding the two figures differ only by a constant, since
   `fx_to_krw` is 1.0 throughout. For a **US** holding they genuinely diverge, and
   the difference is the exchange-rate component of the result — the number a
   USD-only report hides completely. Both are stored; neither is derived from the
   other at render time.

7. **Extend the replay** — `recompute_position(ticker)`

   Generalize `recompute_average` to rebuild quantity, average price **and** the
   ticker's cash flows from the journal. Preserve its existing ordering rule —
   opening entries first, then `executed_at`, then `id`
   (`portfolio_service.py:347`) — which exists because a seeded position predates
   everything in the journal despite carrying a "now" timestamp.

   Keep `recompute_average` as a thin wrapper so existing callers do not break.

8. **`add_holding` funds itself**

   Once the ledger exists, seeding a position must write the matching synthetic
   funding deposit and buy from phase 1, **in that holding's own currency**, in
   the same transaction that writes the holding and its opening trade. Otherwise
   every seeded position silently drives a balance negative.

## Definition of done
- A buy of 10 `AAPL` at $150 reduces the **USD** balance by exactly $1,500 and
  leaves the KRW balance untouched; a `005930.KS` buy does the reverse.
- Forcing an exception mid-transaction leaves *neither* the trade nor its flow.
- `convert(KRW 13,500,000 -> USD 10,000)` writes two linked legs, nets to zero
  externally, and records an effective rate of 1350 plus the spread against the
  market rate that day.
- Converting dollars back to won after the rate has moved produces a
  `realized_fx_pnl_krw` matching a hand calculation against the average
  acquisition rate.
- Selling a US position at a profit while USDKRW fell can produce a **positive
  `realized_pnl` and a smaller or negative `realized_pnl_base`** — verify this
  case explicitly, because it is the one the whole plan exists to make visible.
- A buy exceeding the balance is recorded and returns `cash_warning`; the journal
  stays complete.
- `recompute_position` on a ticker with a seed plus three back-dated trades
  reproduces the same quantity, average and balances as the incremental path.
