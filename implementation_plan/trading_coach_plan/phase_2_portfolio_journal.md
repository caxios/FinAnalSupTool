# Phase 2: Portfolio & Trading Journal (Backend)

**Goal**: Implement blueprint §1 — holdings, trade logging, and the dedicated
"Entry Rationale" field — plus §4's auto-trigger of the 8-quarter fundamental
baseline when a new ticker joins the portfolio.

**Depends on**: Phase 1.

## Tasks:

1. **Create `backend/services/portfolio_service.py`** (the repository layer —
   the only module that touches SQL, so phases 3-6 never see the driver)
   - `list_holdings() -> list[dict]`
   - `get_holding(ticker) -> dict | None`
   - `add_holding(ticker, quantity, avg_price, initial_fx_rate=None) -> dict`
     — blueprint §1 "Initial Portfolio Setup". Normalize the ticker with
     `.strip().upper()`, matching `DocumentStore._normalize`.
   - `remove_holding(ticker) -> None` (cascades trades).
   - `record_trade(ticker, side, quantity, executed_at, entry_rationale, ...) -> dict`
   - `list_trades(ticker=None, limit=None) -> list[dict]` — newest first.
   - `recompute_average(ticker) -> dict`: the read-modify-write. On a **buy**,
     `new_avg = (old_qty*old_avg + qty*price) / (old_qty + qty)`; on a **sell**,
     quantity decreases and `avg_price` is unchanged (realized P/L is a separate
     concern, not a change to cost basis). Wrap in a single transaction.
   - Reject a sell exceeding held quantity with a domain error the router maps to 400.

2. **Add schemas to `backend/schemas/api_schemas.py`**
   - `HoldingCreate`, `Holding`, `TradeCreate`, `Trade`, `PortfolioResponse`.
   - `TradeCreate` carries **only** what blueprint §1 says the user types:
     `ticker`, `side`, `quantity`, `executed_at`, `entry_rationale`.
     `execution_price`, `total_value`, and `avg_price_after` are **server-derived**
     (phase 3) and must not be accepted from the client.
   - `entry_rationale: str | None` — a first-class column, never folded into a
     generic notes field. Phase 6 reads exactly this field.

3. **Create `backend/routers/portfolio.py`**
   - `GET  /portfolio`            → holdings + current aggregate
   - `POST /portfolio/holdings`   → add/seed a holding
   - `DELETE /portfolio/holdings/{ticker}`
   - `GET  /portfolio/trades`     → optional `?ticker=` filter
   - `POST /portfolio/trades`     → log a trade (calls `recompute_average`)
   - Register with `app.include_router(portfolio.router)` in `main.py`.

4. **8-quarter baseline auto-trigger** (blueprint §4)
   - In `services/portfolio_service.py`, on `add_holding` for a ticker not already
     in `DocumentStore`, kick off the existing SEC fetch path — do not write a new
     fetcher. `services/sec_fetch.plan_filings(ticker, form_type, start_year,
     end_year, start_quarter, end_quarter)` already resolves a fiscal range, and
     `routers/sec.py` already renders + calls `ingest_pdf` + rebuilds merged tables.
   - Compute the 2-year window from today: request `10-K` for the last 2 fiscal
     years and `10-Q` across the same span. Note `plan_filings` enforces
     `MAX_YEAR_SPAN` and yields Q1-Q3 only — there is no Q4 10-Q, so an 8-quarter
     ask resolves to ~6 10-Qs plus 2 10-Ks. Document that in the response.
   - Run it **in the background** (`BackgroundTasks` or `asyncio.create_task`);
     SEC rendering is sequential and rate-limited, so it must not block the
     "add holding" response. Return a status the UI can poll.
   - The MAS "Baseline Debate" is just `analyze_pipeline` over the ingested range —
     do not duplicate pipeline logic. Deferring the debate trigger to a manual
     Deep Analysis run is acceptable for this phase; state which you chose.

## Definition of done
- Add a holding, log two buys and a sell; `avg_price` and `quantity` are correct
  and survive a server restart.
- `entry_rationale` round-trips verbatim.
- Adding a new ticker begins ingesting filings and it appears in
  `GET /companies` (the phase-3 endpoint from `mas_analy_sys_plan`).
