# Phase 1: The Multi-Currency Cash Ledger

**Goal**: Give the app a cash account that is a *ledger* rather than a number,
holds **a separate balance per currency**, and derives every balance by replaying
its own rows.

**Depends on**: `trading_coach_plan/` phase 1 (`services/db.py`).

## Why a ledger, and why per-currency

A single mutable balance cannot distinguish **money the user added** from **money
the portfolio made**. Deposit ₩5M into a ₩10M account and a balance-only model
reports +50% return. That distinction has to exist at write time or it never
exists at all.

A single *currency* is equally wrong here: this user holds won and dollars at the
same time and converts between them. Collapsing both into one number destroys the
FX position, which is one of the things this plan exists to measure. So the ledger
stores each flow in its own currency and the balance is per-currency; the
base-currency total is computed on read, in phase 2.

## Tasks

1. **Extend the schema in `backend/services/db.py`**

   Add `_SCHEMA_CASH_FLOWS` plus its index to `_SCHEMA_STATEMENTS`. `init_db()` is
   already idempotent `CREATE TABLE IF NOT EXISTS`, so an existing `portfolio.db`
   picks the table up on next startup with no migration script.

   ```sql
   CREATE TABLE IF NOT EXISTS cash_flows (
       id            INTEGER PRIMARY KEY AUTOINCREMENT,
       flow_type     TEXT    NOT NULL CHECK (flow_type IN
                       ('deposit','withdrawal','buy','sell','dividend',
                        'fee','tax','interest','fx_out','fx_in','adjustment')),
       currency      TEXT    NOT NULL,
       amount        REAL    NOT NULL,
       fx_to_krw     REAL    NOT NULL,
       occurred_at   TEXT    NOT NULL,
       trade_id      INTEGER,
       conversion_id TEXT,
       note          TEXT,
       created_at    TEXT    NOT NULL,
       FOREIGN KEY (trade_id) REFERENCES trades (id) ON DELETE CASCADE
   );
   CREATE INDEX IF NOT EXISTS idx_cash_flows_ccy_time
       ON cash_flows (currency, occurred_at);
   ```

   Every field is load-bearing:

   - **`amount` is denominated in `currency`, and it is signed** — positive into
     the account, negative out. A balance is then `SUM(amount) WHERE currency = ?`,
     with no per-type sign table to get wrong. Enforce the sign against
     `flow_type` in the repository rather than in SQL, so the error message can
     be useful.
   - **`fx_to_krw` is NOT NULL on every row, including KRW rows** (where it is
     1.0). For a USD row it is the USDKRW rate at `occurred_at`. This is the most
     important column in the table: it is the only record of what the money was
     worth in base currency *at the moment it moved*, and it cannot be
     reconstructed afterwards. Without it there is no cost basis in won and no
     realized FX gain.
   - **`fx_out` / `fx_in`** are the two legs of a 환전. They share a
     `conversion_id` (a UUID) so the pair renders as one event and so phase 3 can
     attribute a realized FX gain to it. They are *internal* flows: no money
     entered or left the portfolio.
   - **`trade_id` cascades** — removing a holding already cascades to its trades
     (`db.py:93`); this carries it one level further so a deleted position cannot
     orphan its cash movements.
   - **`adjustment`** exists for reconciliation against a real broker statement:
     a visible, dated, note-carrying row, never a silent correction.

2. **New `backend/services/cash_service.py`**

   The repository for cash, mirroring `portfolio_service`'s posture — the only
   module that writes SQL against `cash_flows`, so phases 3-7 never see a cursor.

   ```python
   BASE_CURRENCY  = "KRW"       # README decision 1 — a setting, not a constant
   SUPPORTED      = ("KRW", "USD")

   EXTERNAL_FLOWS = frozenset({"deposit", "withdrawal"})
   INFLOW_TYPES   = frozenset({"deposit","sell","dividend","interest","fx_in"})
   OUTFLOW_TYPES  = frozenset({"withdrawal","buy","fee","tax","fx_out"})

   OPENING_NOTE      = "Opening cash balance recorded at portfolio setup."
   SEED_FUNDING_NOTE = "Synthetic funding for a position seeded at setup."

   class CashError(Exception)
   class InvalidFlow(CashError)
   class LedgerNotInitialized(CashError)

   def record_flow(flow_type, currency, amount, fx_to_krw, occurred_at,
                   trade_id=None, conversion_id=None, note=None, conn=None) -> dict
   def balances(as_of: str | None = None) -> dict[str, float]   # {"KRW": …, "USD": …}
   def balance(currency, as_of=None) -> float
   def list_flows(currency=None, flow_type=None, since=None, limit=None) -> list[dict]
   def external_flows(since=None) -> list[dict]
   def is_initialized() -> bool
   ```

   - `record_flow` takes an optional `conn` so phase 3 can write a trade and its
     cash leg inside **one** `db.transaction()`. When `conn is None` it opens its
     own. A trade whose cash leg is missing is worse than no trade at all.
   - `record_flow` derives the sign from `flow_type` and rejects a mismatch,
     rather than trusting a caller to pass `-1234.56`.
   - `balances(as_of)` filters `occurred_at <= as_of`, which phase 4 needs to
     rebuild a net-worth series. ISO-8601 sorts lexicographically, the same
     property `list_trades` already relies on (`db.py:200`).
   - **No currency conversion happens in this module.** It returns per-currency
     numbers; turning them into one base-currency figure is phase 2's job,
     because that requires a rate and therefore network I/O.

3. **Resolve and store each holding's currency**

   `holdings.currency` exists but is user-supplied and defaults to `'USD'`
   (`db.py:67`) — exactly wrong for a KOSPI ticker. Add to `portfolio_service`:

   ```python
   _SUFFIX_CURRENCY = {".KS": "KRW", ".KQ": "KRW"}   # KOSPI, KOSDAQ

   def resolve_asset_currency(ticker: str) -> str:
       """Suffix rule first (deterministic, offline); yfinance info['currency']
       only as a cross-check. A bare symbol is USD."""
   ```

   Called by `add_holding`, and used to backfill existing rows on startup. The
   suffix rule is primary because it is deterministic and needs no network;
   `yfinance.Ticker.info` is slow and flaky enough that it must not sit in the
   write path for a position.

4. **Gate the SEC baseline to US-listed tickers**

   `trigger_baseline_if_new` (`portfolio_service.py:561`) currently fires for any
   new ticker. For `005930.KS` that starts a two-year EDGAR fetch which cannot
   succeed and ends in `state: "failed"`. Skip it when
   `resolve_asset_currency(ticker) != "USD"`, with an explicit status:

   ```python
   {"state": "unsupported",
    "message": "SEC EDGAR covers US-listed issuers only; no fundamental "
               "baseline is available for {ticker}."}
   ```

   Honest and visible beats a background failure the user has to interpret.

5. **Opening anchor — `initialize_ledger(opening: dict[str, float], fx_to_krw: float)`**

   Existing users already hold seeded positions whose `OPENING_RATIONALE` trades
   were never funded; replaying an unfunded ledger yields a large negative
   balance. Initialization writes both sides in one transaction:

   - one `deposit` per currency for the cash the user says they hold now;
   - one `deposit` per seeded holding for its cost basis, in **that holding's own
     currency**, noted `SEED_FUNDING_NOTE`;
   - one `buy` per seeded holding, linked by `trade_id` to its opening trade.

   Net effect: each currency's balance equals what the user entered, every
   position is funded, and net worth at setup equals opening cash plus seeded
   cost basis. Like `OPENING_RATIONALE`, this is explicitly an anchor rather than
   a claim about real history — the acquisition dates are unknown, which is why
   those positions were seeded in the first place.

   Idempotent: raises if `is_initialized()`.

6. **Router** (`backend/routers/portfolio.py`) **and schemas**

   ```
   GET    /portfolio/cash                balances per currency, init state, recent flows
   POST   /portfolio/cash/initialize     201, or 409 if already initialized
   POST   /portfolio/cash/flows          201 — deposit/withdrawal/dividend/fee/tax
   GET    /portfolio/cash/flows          paginated ledger, newest first
   DELETE /portfolio/cash/flows/{id}     204, for a mistyped entry
   ```

   Add `CashFlow`, `CashFlowCreate`, `CashPosition`, `LedgerInitRequest` to
   `schemas/api_schemas.py` and export them from `schemas/__init__.py`.
   `CashFlowCreate` takes `currency` plus a positive `amount`; the sign is
   server-derived, the same way `TradeCreate` excludes `total_value`. Map
   `CashError` subclasses to status codes in the router only, so `cash_service`
   stays importable by the phase 7 agents without FastAPI.

## Definition of done
- `init_db()` on an existing `portfolio.db` creates `cash_flows` without touching
  `holdings` or `trades`.
- A KRW deposit, a USD deposit and a USD fee replay to two correct, independent
  balances; no stored balance column exists anywhere.
- `resolve_asset_currency("005930.KS") == "KRW"` and `("AAPL") == "USD"`.
- Adding a `.KS` ticker starts **no** SEC baseline and reports `unsupported`.
- `initialize_ledger` on a portfolio holding one US and one Korean seeded
  position leaves both balances exactly as entered, every opening trade funded in
  its own currency.
- Deleting a holding removes its trades *and* their flows (cascade verified with
  `PRAGMA foreign_keys` on).
- `record_flow(..., conn=outer)` joins the caller's transaction: forcing an
  exception afterwards rolls the flow back with the rest.
