"""
routers.portfolio
─────────────────
The trading portfolio and journal — blueprint §1.

  GET    /portfolio                    — holdings + aggregates
  POST   /portfolio/holdings           — seed an existing position
  DELETE /portfolio/holdings/{ticker}  — drop a position (and its trades)
  GET    /portfolio/trades             — the journal, optionally per-ticker
  POST   /portfolio/trades             — log a trade
  GET    /portfolio/baseline/{ticker}  — poll the 8-quarter baseline fetch
  GET    /portfolio/risk               — VaR/CVaR/volatility/correlation/FX risk

  GET    /portfolio/cash               — balances per currency + the rate used
  POST   /portfolio/cash/initialize    — record the opening anchor
  GET    /portfolio/cash/flows         — the ledger, newest first
  POST   /portfolio/cash/flows         — deposit / withdrawal / dividend / fee / tax
  POST   /portfolio/cash/convert       — a 환전, as two linked legs
  DELETE /portfolio/cash/flows/{id}    — remove a mistyped entry

Unlike the filing endpoints, these are **portfolio-scoped, not company-scoped**:
the portfolio spans every ticker at once, so there is no ``activeTicker`` to
thread through. ``/portfolio/trades`` takes an optional ticker *filter* only.

This router stays thin — all logic lives in ``services.portfolio_service``, and
this file's job is mapping domain errors onto HTTP status codes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from schemas import (
    CashFlow,
    CashFlowCreate,
    CashFlowsResponse,
    CashPosition,
    ConversionCreate,
    FxInfo,
    Holding,
    LedgerInitRequest,
    LedgerInitResponse,
    PriceResolution,
    HoldingCreate,
    HoldingCreatedResponse,
    PortfolioResponse,
    Trade,
    TradeCreate,
    TradeResponse,
    TradesResponse,
)
from services import db
from services import cash_service as cs
from services import portfolio_service as ps
from services.storage import (
    DebateStore,
    DocumentStore,
    get_debate_store,
    get_document_store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def _holding_model(row: dict | None) -> Holding | None:
    """
    Map a DB row to the API model.

    Rows that have been through ``value_holdings`` already carry
    ``current_price`` / ``market_value`` / ``unrealized_roi``; rows that have not
    (or whose price lookup failed) leave them null, which lets the UI tell
    "unpriced" apart from "worthless".
    """
    return Holding(**row) if row else None


@router.get("", response_model=PortfolioResponse)
@router.get("/", response_model=PortfolioResponse, include_in_schema=False)
async def get_portfolio():
    """
    Every holding plus whole-portfolio aggregates.

    Live prices are fetched concurrently for every held ticker. A ticker whose
    lookup fails comes back with ``current_price: null`` and is left out of the
    totals — one delisted symbol must not blank the whole page.

    When the holdings span more than one currency the aggregate totals are
    withheld and ``note`` says why. Per-currency subtotals and every per-row
    figure remain, so nothing on the page is lost — only the one number that
    would have been meaningless.
    """
    valued, totals = await ps.value_holdings()
    return PortfolioResponse(
        holdings=[_holding_model(r) for r in valued],
        baseline_status=ps.all_baseline_statuses(),
        fx=await _fx_info(),
        cash=cs.balances(),
        cash_initialized=cs.is_initialized(),
        **totals,
    )


@router.post("/holdings", response_model=HoldingCreatedResponse, status_code=201)
async def create_holding(
    body: HoldingCreate,
    store: DocumentStore = Depends(get_document_store),
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Seed a position the user already owns.

    Side effect (blueprint §4): if this ticker has no filings ingested yet, an
    8-quarter SEC baseline fetch starts **in the background**, and then one full
    Deep Analysis per completed quarter over that same span, so the Coach agent
    has 2 years of fundamentals to anchor on — including analyses whose data
    window ends *before* a trade the user has already logged, which is what a
    retrospective review is allowed to cite.

    Both steps are detached: SEC rendering is sequential and rate-limited, and
    eight multi-agent runs take far longer than an HTTP request should. Poll
    ``GET /portfolio/baseline/{ticker}`` for progress on both.
    """
    try:
        holding = await ps.add_holding_auto(
            body.ticker, body.quantity, body.avg_price,
            initial_fx_rate=body.initial_fx_rate,
            currency=body.currency,
        )
    except ps.DuplicateHolding as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ps.PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))

    started = ps.trigger_baseline_if_new(body.ticker, store, debate_store)
    return HoldingCreatedResponse(
        holding=_holding_model(holding),
        baseline_started=started,
        baseline_status=ps.baseline_status(body.ticker),
    )


@router.delete("/holdings/{ticker}", status_code=204)
async def delete_holding(ticker: str):
    """Remove a position. Its journal entries cascade away with it."""
    try:
        ps.remove_holding(ticker)
    except ps.HoldingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ps.PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return None


@router.get("/trades", response_model=TradesResponse)
async def get_trades(
    ticker: str | None = Query(None, description="Filter to one ticker"),
    limit: int | None = Query(None, ge=1, le=1000, description="Max rows"),
):
    """The trading journal, newest first — including each entry's rationale."""
    rows = ps.list_trades(ticker=ticker, limit=limit)
    return TradesResponse(
        trades=[Trade(**r) for r in rows],
        total=len(rows),
        ticker=ps.normalize_ticker(ticker) if ticker else None,
    )


@router.post("/trades", response_model=TradeResponse, status_code=201)
async def create_trade(body: TradeCreate):
    """
    Log a trade and update the position in one transaction.

    The user supplies the transaction time, quantity, and their **entry
    rationale** — nothing else. The fill price is looked up from intraday market
    data at that timestamp, the total value follows from it, and the position's
    new average price is computed server-side.

    ``execution_price`` in the body is a manual override for a fill the lookup
    gets wrong; when it is absent (the normal case) the automation runs.
    """
    resolution: PriceResolution | None = None
    price = body.execution_price

    if price is None:
        try:
            resolved = await ps.resolve_execution_price(body.ticker, body.executed_at)
        except ps.InvalidTrade as e:
            # A future timestamp or an unknown ticker — the submitted trade is
            # the problem, so 400 rather than a 5xx.
            raise HTTPException(status_code=400, detail=str(e))
        price = resolved.price
        resolution = PriceResolution(
            resolution=resolved.resolution,
            bar_time=resolved.bar_time,
            is_approximate=resolved.is_approximate,
            message=resolved.message,
        )

    try:
        trade = await ps.record_trade_auto(
            body.ticker, body.side, body.quantity, body.executed_at,
            entry_rationale=body.entry_rationale,
            execution_price=price,
            fx_rate=body.fx_rate,
            fee=body.fee,
            tax=body.tax,
            emotion_tag=body.emotion_tag,
        )
    except ps.InvalidTrade as e:
        # Selling more than is held, or opening a position with no price.
        raise HTTPException(status_code=400, detail=str(e))
    except ps.PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))

    warning = trade.pop("cash_warning", None)
    return TradeResponse(
        trade=Trade(**trade),
        holding=_holding_model(ps.get_holding(body.ticker)),
        price_resolution=resolution,
        cash_warning=warning,
    )


@router.get("/baseline/{ticker}")
async def get_baseline_status(ticker: str):
    """
    Progress of the 8-quarter baseline fetch for a ticker.

    States: ``none`` (never run), ``queued``, ``running``, ``complete``,
    ``partial`` (some forms failed), ``failed``.
    """
    return {"ticker": ps.normalize_ticker(ticker), **ps.baseline_status(ticker)}


@router.get("/risk")
async def get_portfolio_risk(
    confidence: float = Query(0.95, gt=0, lt=1, description="VaR/CVaR confidence level"),
    refresh: bool = Query(False, description="Bypass the 5-minute cache and refetch"),
):
    """
    Whole-portfolio quantitative risk: annualized volatility, historical VaR and
    CVaR (Expected Shortfall), max drawdown, per-position risk contribution vs.
    capital weight, the pairwise correlation matrix, cash-as-a-position, and FX
    risk (unhedged exposure and the hedged-volatility comparison).

    This was formerly computed inside Deep Analysis (the retired `quant_risk`
    agent) — a single-company research run had no business touching the whole
    book. The math is unchanged (``services.risk_metrics``); only where it runs
    moved. Results are cached for 5 minutes since they require a price-history
    download; pass ``refresh=true`` to force a refetch (e.g. right after logging
    a trade).
    """
    from services import portfolio_risk

    try:
        return await portfolio_risk.build_snapshot(
            confidence=confidence, use_cache=not refresh
        )
    except Exception as e:  # noqa: BLE001 — a risk-snapshot failure is not a 500
        logger.error(f"Portfolio risk snapshot failed: {e}")
        raise HTTPException(status_code=502, detail=f"Portfolio risk snapshot failed: {e}")


# =============================================================================
# Cash ledger
# =============================================================================
# Cash is a LEDGER, not a number. Every movement is its own row and every balance
# is a replay of them, so there is no balance field to POST — you record what
# happened and the balance follows. See `services/cash_service.py`.


async def _fx_info() -> FxInfo:
    """
    The current rate, for DISPLAY.

    Failure here is non-fatal by design: one unavailable quote must not blank a
    working portfolio, so this returns a null rate and the client renders a dash.
    The recording path takes the opposite line — see
    ``cash_service.resolve_rate``, which raises rather than guess.
    """
    from providers import fx_provider

    try:
        q = await fx_provider.fetch_spot()
        return FxInfo(pair=q.pair, rate=q.rate, as_of=q.as_of,
                      is_stale=q.is_stale, source=q.source)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[portfolio] FX unavailable for display: {e}")
        return FxInfo(rate=None, as_of=None, is_stale=True, source=None)


@router.get("/cash", response_model=CashPosition)
async def get_cash(limit: int = Query(10, ge=0, le=100)):
    """Balances per currency, whether the ledger has been opened, recent flows."""
    return CashPosition(
        balances=cs.balances(),
        base_currency=cs.BASE_CURRENCY,
        is_initialized=cs.is_initialized(),
        fx=await _fx_info(),
        recent_flows=[CashFlow(**f) for f in cs.list_flows(limit=limit)],
    )


@router.post("/cash/initialize", response_model=LedgerInitResponse, status_code=201)
async def initialize_cash(body: LedgerInitRequest):
    """
    Record the opening balance and fund the positions seeded at setup.

    Both sides are written together: the cash the user says they hold, plus a
    synthetic deposit-and-buy per seeded holding. Without the second part,
    replaying a ledger whose positions were never funded produces a large
    negative balance.
    """
    if cs.is_initialized():
        raise HTTPException(
            status_code=409,
            detail="The cash ledger has already been initialized. Record a "
                   "deposit or an adjustment instead.",
        )

    rate = body.fx_to_krw
    if rate is None and any(
        c.strip().upper() != cs.BASE_CURRENCY for c in (body.opening or {})
    ):
        try:
            rate = await cs.resolve_rate("USD", db.utc_now_iso())
        except cs.CashError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        result = cs.initialize_ledger(body.opening, fx_to_krw=rate,
                                      occurred_at=body.occurred_at)
    except cs.LedgerAlreadyInitialized as e:
        raise HTTPException(status_code=409, detail=str(e))
    except cs.CashError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The rate columns on existing trades and holdings have been null since they
    # were added. Fill them now, while there is a rate in hand — and report what
    # could not be resolved rather than defaulting it to 1.0.
    backfill = await cs.backfill_fx() if body.backfill_fx else None
    return LedgerInitResponse(**result, fx_backfill=backfill)


@router.get("/cash/flows", response_model=CashFlowsResponse)
async def list_cash_flows(
    currency: str | None = Query(None),
    flow_type: str | None = Query(None),
    since: str | None = Query(None, description="ISO date; flows on or after it"),
    limit: int = Query(100, ge=1, le=500),
):
    """The ledger, newest first."""
    try:
        rows = cs.list_flows(currency=currency, flow_type=flow_type,
                             since=since, limit=limit)
    except cs.CashError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return CashFlowsResponse(flows=[CashFlow(**r) for r in rows], count=len(rows))


@router.post("/cash/flows", response_model=CashFlow, status_code=201)
async def create_cash_flow(body: CashFlowCreate):
    """
    Record a deposit, withdrawal, dividend, fee, tax, interest, or adjustment.

    Trades write their own cash legs (phase 3) and conversions have their own
    endpoint, so neither is accepted here — routing them through a generic form
    would let a trade be recorded with no position attached to it.
    """
    if body.flow_type in ("buy", "sell", "fx_in", "fx_out"):
        raise HTTPException(
            status_code=400,
            detail=f"{body.flow_type!r} is written by the trade or conversion "
                   f"that causes it, not recorded on its own.",
        )
    try:
        return CashFlow(**await cs.record_flow_auto(
            body.flow_type, body.currency, body.amount, body.occurred_at,
            fx_to_krw=body.fx_to_krw, note=body.note,
        ))
    except cs.CashError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cash/convert", status_code=201)
async def create_conversion(body: ConversionCreate):
    """
    Record a 환전 as two linked legs.

    Both amounts come from the user because that is what their statement shows;
    the effective rate is derived from them, spread included. A conversion is an
    internal movement — it does not touch the portfolio's external flows and so
    does not appear as performance.
    """
    try:
        return await cs.convert_auto(
            body.from_currency, body.from_amount,
            body.to_currency, body.to_amount,
            body.occurred_at, note=body.note,
        )
    except cs.CashError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/cash/flows/{flow_id}", status_code=204)
async def delete_cash_flow(flow_id: int):
    """Remove a mistyped entry. Use an `adjustment` to correct a real one."""
    try:
        cs.delete_flow(flow_id)
    except cs.CashError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/performance")
async def get_performance(
    window: str = Query("all", description="1m | 3m | 6m | 1y | all"),
):
    """
    Net worth over time, plus the two returns that answer different questions.

    **TWR** measures return per unit of capital — the user's selection, unaffected
    by when they deposited. **MWR** measures what their money actually did. They
    diverge exactly when deposit timing was good or bad, which is itself worth
    seeing; reporting one alone answers a question the user did not ask.

    Both are given in KRW and USD, because a portfolio can gain in one and lose
    in the other. `coverage_start` marks where the ledger begins — the chart must
    not imply history that was never recorded.
    """
    from services import performance

    try:
        return await performance.performance_report(window=window)
    except Exception as e:  # noqa: BLE001 — a report failure is not a 500
        logger.error(f"Performance report failed: {e}")
        raise HTTPException(status_code=502, detail=f"Performance report failed: {e}")
