# Phase 3: Smart Trade Price Automation

**Goal**: Implement blueprint §1's "Smart Trading Journal Automation" — the user
enters only **transaction time** and **quantity**; the system derives execution
price, total value, updated average, and real-time ROI.

**Depends on**: Phase 2.

## Tasks:

1. **Extend `backend/providers/price_provider.py`**
   - Add `async def fetch_execution_price(ticker: str, executed_at: datetime) -> float`.
   - Reuse the module's existing yfinance access and its `_flatten_columns` helper
     (yfinance returns MultiIndex columns for some queries — the existing code
     already handles this; do not re-solve it).
   - Intraday granularity: `yf.Ticker(t).history(interval="1m", start=..., end=...)`
     and pick the bar containing `executed_at`. **yfinance only serves 1-minute
     data for roughly the last 30 days** — this is the phase's main constraint.
     Fall back in order: 1m → 1h → daily close, and return which resolution was
     used so the UI can label an approximate fill.
   - Return the bar's close (document the choice; open/VWAP are defensible too).
   - Handle: market closed at that timestamp (use the nearest prior bar), a
     future timestamp (400), and a ticker with no price history (400).
   - Wrap blocking yfinance calls in `run_in_threadpool`, as
     `fetch_technical_data` already does.

2. **Add `async def fetch_current_price(ticker) -> float`** for ROI, with a short
   in-process TTL cache (~60s) so rendering a 20-row portfolio is not 20 network calls.

3. **Wire into `POST /portfolio/trades`**
   - On trade creation: resolve `execution_price` via `fetch_execution_price`,
     compute `total_value = execution_price * quantity` (times `fx_rate` when set),
     persist both plus `avg_price_after` from `recompute_average`.
   - Allow an explicit `execution_price` override in the request for manual
     correction, but leave it absent by default — the blueprint's point is that the
     user does not type it.

4. **ROI on `GET /portfolio`**
   - Per holding: `unrealized_roi = (current_price - avg_price) / avg_price`,
     plus `market_value` and `unrealized_pnl`.
   - Portfolio totals: cost basis, market value, total unrealized P/L and ROI.
   - Fetch all current prices concurrently with `asyncio.gather`.
   - If a price lookup fails, return the holding with `current_price: null` rather
     than failing the whole request — one delisted ticker must not blank the page.

## Definition of done
- Logging a trade with only time + quantity yields a plausible execution price and
  a correctly updated average.
- A timestamp older than the 1-minute window degrades to daily close and says so.
- `GET /portfolio` returns ROI for every holding; one bad ticker degrades alone.
