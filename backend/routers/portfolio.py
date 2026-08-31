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
    Holding,
    PriceResolution,
    HoldingCreate,
    HoldingCreatedResponse,
    PortfolioResponse,
    Trade,
    TradeCreate,
    TradeResponse,
    TradesResponse,
)
from services import portfolio_service as ps
from services.storage import DocumentStore, get_document_store

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
    """
    valued, totals = await ps.value_holdings()
    return PortfolioResponse(
        holdings=[_holding_model(r) for r in valued],
        baseline_status=ps.all_baseline_statuses(),
        **totals,
    )


@router.post("/holdings", response_model=HoldingCreatedResponse, status_code=201)
async def create_holding(
    body: HoldingCreate,
    store: DocumentStore = Depends(get_document_store),
):
    """
    Seed a position the user already owns.

    Side effect (blueprint §4): if this ticker has no filings ingested yet, an
    8-quarter SEC baseline fetch starts **in the background** so the Coach agent
    has 2 years of fundamentals to anchor on. The fetch is detached because SEC
    rendering is sequential and rate-limited — poll
    ``GET /portfolio/baseline/{ticker}`` for progress.
    """
    try:
        holding = ps.add_holding(
            ticker=body.ticker,
            quantity=body.quantity,
            avg_price=body.avg_price,
            initial_fx_rate=body.initial_fx_rate,
            currency=body.currency,
        )
    except ps.DuplicateHolding as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ps.PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))

    started = ps.trigger_baseline_if_new(body.ticker, store)
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
        trade = ps.record_trade(
            ticker=body.ticker,
            side=body.side,
            quantity=body.quantity,
            executed_at=body.executed_at,
            entry_rationale=body.entry_rationale,
            execution_price=price,
            fx_rate=body.fx_rate,
        )
    except ps.InvalidTrade as e:
        # Selling more than is held, or opening a position with no price.
        raise HTTPException(status_code=400, detail=str(e))
    except ps.PortfolioError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return TradeResponse(
        trade=Trade(**trade),
        holding=_holding_model(ps.get_holding(body.ticker)),
        price_resolution=resolution,
    )


@router.get("/baseline/{ticker}")
async def get_baseline_status(ticker: str):
    """
    Progress of the 8-quarter baseline fetch for a ticker.

    States: ``none`` (never run), ``queued``, ``running``, ``complete``,
    ``partial`` (some forms failed), ``failed``.
    """
    return {"ticker": ps.normalize_ticker(ticker), **ps.baseline_status(ticker)}
