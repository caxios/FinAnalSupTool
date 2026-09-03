"""
services.portfolio_service
──────────────────────────
The repository layer for the trading portfolio and journal — the **only** module
that writes SQL against the tables in :mod:`services.db`.

Everything downstream (the routers now, the price automation in phase 3, the
quant-risk and coach agents in phases 5-6) calls these functions and never sees
a cursor. Moving off SQLite later therefore touches this file and ``db.py``, and
nothing else.

Two rules the whole portfolio depends on
────────────────────────────────────────
1. **Tickers are normalized** to ``.strip().upper()`` on every entry point, the
   same way ``DocumentStore._normalize`` does it — so the journal and the filing
   stores agree on what "aapl" means.
2. **Average price is recomputed inside a single transaction** with the trade
   insert. It is a read-modify-write (read old qty/avg → compute → write back),
   and two concurrent trades that interleave would silently lose one update.

Cost-basis convention
─────────────────────
A **buy** moves the weighted average: ``(old_qty*old_avg + qty*price) /
(old_qty + qty)``. A **sell** reduces quantity and leaves ``avg_price``
untouched — realized P/L is a separate reporting concern, not a change to what
the remaining shares cost. This is standard average-cost accounting, and it is
what makes "unrealized ROI vs. avg_price" meaningful after a partial exit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from services import db

logger = logging.getLogger(__name__)


# =============================================================================
# Domain errors
# =============================================================================
# Raised by this layer, mapped to HTTP status codes by `routers/portfolio.py`.
# Keeping them non-HTTP means the agents in phases 5-6 can call these functions
# without importing FastAPI.

class PortfolioError(Exception):
    """Base class for portfolio domain errors."""


class HoldingNotFound(PortfolioError):
    """The requested ticker isn't in the portfolio."""


class DuplicateHolding(PortfolioError):
    """A holding for this ticker already exists."""


class InvalidTrade(PortfolioError):
    """The trade is not valid against the current position."""


# =============================================================================
# Helpers
# =============================================================================

# Marks the synthetic journal entry written when a pre-existing position is
# seeded, so the UI and the Coach agent can tell it apart from a real decision
# the user made and reasoned about.
OPENING_RATIONALE = "Opening position recorded at portfolio setup."


def is_opening_entry(trade: dict) -> bool:
    """Whether a journal row is a seeded opening entry rather than a real trade."""
    return trade.get("entry_rationale") == OPENING_RATIONALE


def normalize_ticker(ticker: str) -> str:
    """Canonical ticker form, matching ``DocumentStore._normalize``."""
    return (ticker or "").strip().upper()


# Exchange suffixes that fix an asset's trading currency. yfinance prices a
# KOSPI listing in won and a US listing in dollars, and `holdings.currency`
# defaults to 'USD' — which is exactly wrong for `005930.KS` and is what let the
# app add won into a dollar total.
_SUFFIX_CURRENCY = {
    ".KS": "KRW",   # KOSPI
    ".KQ": "KRW",   # KOSDAQ
}


def resolve_asset_currency(ticker: str) -> str:
    """
    The currency a ticker actually trades in.

    The suffix rule is primary because it is deterministic and needs no network.
    ``yfinance.Ticker.info`` would be more general but is slow and unreliable
    enough that it must not sit in the write path for a position — a position
    that cannot be saved because a metadata lookup timed out is a worse failure
    than one saved with a currency the user can correct.

    A bare symbol is USD, which is the app's existing assumption everywhere else.
    """
    t = normalize_ticker(ticker)
    for suffix, currency in _SUFFIX_CURRENCY.items():
        if t.endswith(suffix):
            return currency
    return "USD"


def is_us_listed(ticker: str) -> bool:
    """
    Whether SEC EDGAR could plausibly hold filings for this ticker.

    Used to gate the fundamental baseline: EDGAR covers US-listed issuers, so a
    KOSPI ticker cannot succeed there no matter how long the fetch runs.
    """
    return resolve_asset_currency(ticker) == "USD"


def _require_ticker(ticker: str) -> str:
    t = normalize_ticker(ticker)
    if not t:
        raise InvalidTrade("A ticker symbol is required.")
    return t


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


# =============================================================================
# Holdings
# =============================================================================

def list_holdings() -> list[dict]:
    """Every holding, alphabetically by ticker."""
    rows = db.get_connection().execute(
        "SELECT * FROM holdings ORDER BY ticker"
    ).fetchall()
    return [dict(r) for r in rows]


def get_holding(ticker: str) -> dict | None:
    """One holding, or ``None`` if the ticker isn't held."""
    row = db.get_connection().execute(
        "SELECT * FROM holdings WHERE ticker = ?", (_require_ticker(ticker),)
    ).fetchone()
    return _row_to_dict(row)


def add_holding(
    ticker: str,
    quantity: float,
    avg_price: float,
    initial_fx_rate: float | None = None,
    currency: str | None = None,
) -> dict:
    """
    Seed an existing position — blueprint §1 "Initial Portfolio Setup".

    Writes the holding **and an opening trade** in one transaction. The opening
    trade matters: :func:`recompute_average` reconstructs a position by replaying
    its journal, so a position that exists only in ``holdings`` would vanish from
    that replay and the average would be silently wrong. The journal has to be a
    complete account of how the position was built.

    The opening entry is marked as such in its rationale and timestamped now —
    the real acquisition date is unknown, which is exactly why it is a *seed*
    rather than a logged trade.

    ``currency`` is resolved from the ticker when not given. Defaulting it to
    'USD' — as the schema does — mislabels every KOSPI position, and a mislabelled
    currency is not a cosmetic error: it is what lets ₩71,000 be added to a
    dollar total.
    """
    from services import cash_service as cs

    t = _require_ticker(ticker)
    currency = (currency or "").strip().upper() or resolve_asset_currency(t)
    if quantity <= 0:
        raise InvalidTrade("quantity must be greater than zero.")
    if avg_price <= 0:
        raise InvalidTrade("avg_price must be greater than zero.")
    if get_holding(t) is not None:
        raise DuplicateHolding(
            f"{t} is already in the portfolio. Log a trade to change the position."
        )

    now = db.utc_now_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO holdings (ticker, quantity, avg_price, initial_fx_rate,"
            " currency, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (t, float(quantity), float(avg_price), initial_fx_rate,
             currency, now, now),
        )
        rate = 1.0 if currency == "KRW" else initial_fx_rate
        cur = conn.execute(
            "INSERT INTO trades (ticker, side, quantity, executed_at,"
            " execution_price, total_value, fx_rate, entry_rationale,"
            " avg_price_after, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, "buy", float(quantity), now, float(avg_price),
             float(avg_price) * float(quantity) * float(rate or 1.0),
             rate, OPENING_RATIONALE, float(avg_price), now),
        )
        opening_trade_id = cur.lastrowid

        # Fund the seed, then spend that funding on it — so a seeded position
        # never drives a balance negative and net worth at setup is opening cash
        # plus cost basis. Skipped when no opening balance has been recorded yet:
        # `cash_service.initialize_ledger` funds every unfunded seed when the
        # user finally states their cash, and doing it twice would double-count.
        if cs.is_initialized():
            if rate is None:
                raise InvalidTrade(
                    f"An exchange rate is required to fund a seeded {currency} "
                    f"position in {t}."
                )
            cost = float(avg_price) * float(quantity)
            cs.record_flow("deposit", currency, cost, now, fx_to_krw=rate,
                           note=cs.SEED_FUNDING_NOTE, conn=conn)
            cs.record_flow("buy", currency, cost, now, fx_to_krw=rate,
                           trade_id=opening_trade_id,
                           note=cs.SEED_FUNDING_NOTE, conn=conn)
    logger.info(f"[portfolio] added holding {t}: {quantity} @ {avg_price}")
    return get_holding(t)


def remove_holding(ticker: str) -> None:
    """
    Delete a holding. Its trades go with it via ``ON DELETE CASCADE`` — which is
    only enforced because ``db._configure`` turns on ``PRAGMA foreign_keys``.
    """
    t = _require_ticker(ticker)
    if get_holding(t) is None:
        raise HoldingNotFound(f"{t} is not in the portfolio.")
    with db.transaction() as conn:
        conn.execute("DELETE FROM holdings WHERE ticker = ?", (t,))
    logger.info(f"[portfolio] removed holding {t} (and its trades)")


# =============================================================================
# Trades
# =============================================================================

def list_trades(ticker: str | None = None, limit: int | None = None) -> list[dict]:
    """
    The journal, newest first. Optionally filtered to one ticker.

    Ordering is by ``executed_at`` (when the trade happened) rather than by
    ``created_at`` (when it was logged), because a user may back-fill an older
    trade and would expect it to land in the right place in the history.
    """
    sql = "SELECT * FROM trades"
    params: list = []
    if ticker:
        sql += " WHERE ticker = ?"
        params.append(_require_ticker(ticker))
    sql += " ORDER BY executed_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def get_trade(trade_id: int) -> dict | None:
    """One journal entry by id, or ``None``. Used by the retrospective coach."""
    row = db.get_connection().execute(
        "SELECT * FROM trades WHERE id = ?", (int(trade_id),)
    ).fetchone()
    return _row_to_dict(row)


def record_trade(
    ticker: str,
    side: str,
    quantity: float,
    executed_at: str,
    entry_rationale: str | None = None,
    execution_price: float | None = None,
    fx_rate: float | None = None,
    fee: float = 0.0,
    tax: float = 0.0,
) -> dict:
    """
    Log a trade, update the position, **and move the cash**, atomically.

    ``execution_price`` is optional: when it is ``None`` the trade is still
    recorded (the journal entry and its rationale are the point), the average is
    left unchanged — a buy at an unknown price cannot move a cost basis — and
    **no cash flow is written**. A cash movement of unknown size is not something
    to guess at.

    Two rules the cash leg depends on:

    * **The flow's currency is the asset's currency.** A KOSPI buy debits won; an
      AAPL buy debits dollars. A trade never crosses currencies — that is what
      :func:`cash_service.convert` is for, and conflating the two is what makes a
      brokerage statement unreadable.
    * **The flow occurs at ``executed_at``, not now.** A back-dated trade has to
      move cash on the day it happened, or every day in between is wrong in the
      net-worth series.

    Fees and taxes are written as their own typed rows rather than folded into
    the trade's amount, so a year's friction can be totalled on its own.

    Returns the inserted trade row plus a ``cash_warning`` key when the buy
    exceeded the balance available at that moment.
    """
    from services import cash_service as cs

    t = _require_ticker(ticker)
    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise InvalidTrade(f"side must be 'buy' or 'sell' (got {side!r}).")
    if quantity is None or quantity <= 0:
        raise InvalidTrade("quantity must be greater than zero.")
    fee, tax = float(fee or 0.0), float(tax or 0.0)
    if fee < 0 or tax < 0:
        raise InvalidTrade("fee and tax must not be negative.")

    price = float(execution_price) if execution_price is not None else None
    currency = resolve_asset_currency(t)
    # `fx_rate` is the asset currency's rate at execution. It is 1.0 for a
    # base-currency asset by definition, and required for anything else: without
    # it the cash leg has no recoverable value in won.
    rate = 1.0 if currency == cs.BASE_CURRENCY else fx_rate

    # A trade dated at or before the opening anchor moves no cash: the anchor is
    # the whole state at that instant, so this trade's effect is already inside
    # the balance the user reported. Moving cash again would double-count it.
    anchor = cs.opening_timestamp()
    predates_anchor = bool(anchor and executed_at <= anchor)
    writes_cash = price is not None and cs.is_initialized() and not predates_anchor
    if writes_cash and rate is None:
        raise InvalidTrade(
            f"An exchange rate is required to record a {currency} trade — it is "
            f"the only record of what this money was worth in "
            f"{cs.BASE_CURRENCY} at the time."
        )

    total_value = None if price is None else price * float(quantity) * float(rate or 1.0)

    # A buy that outruns the balance is RECORDED, not rejected. Back-filling
    # history out of order transiently goes negative through no fault of the
    # user's, and refusing would make correct data unenterable. The warning is
    # returned instead — and it is behaviourally interesting in its own right.
    cash_warning = None
    if writes_cash and side == "buy":
        gross = price * float(quantity) + fee + tax
        available = cs.balance(currency, as_of=executed_at)
        if gross > available + 1e-9:
            cash_warning = {
                "currency": currency,
                "shortfall": round(gross - available, 4),
                "balance_before": round(available, 4),
                "note": (
                    f"This buy costs {gross:,.2f} {currency} but only "
                    f"{available:,.2f} was available on that date. Recorded "
                    f"anyway — check for a missing deposit or conversion."
                ),
            }

    # Everything below runs in ONE transaction so the read-modify-write of the
    # position cannot interleave with a concurrent trade on the same ticker.
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT quantity, avg_price FROM holdings WHERE ticker = ?", (t,)
        ).fetchone()

        if row is None:
            # A buy for an unheld ticker opens the position; a sell cannot.
            if side == "sell":
                raise InvalidTrade(
                    f"Cannot sell {t}: it is not in the portfolio."
                )
            if price is None:
                raise InvalidTrade(
                    f"Cannot open a position in {t} without an execution price."
                )
            old_qty, old_avg = 0.0, 0.0
            is_new_position = True
        else:
            old_qty, old_avg = float(row["quantity"]), float(row["avg_price"])
            is_new_position = False

        new_qty, new_avg = _apply_trade(
            old_qty=old_qty, old_avg=old_avg,
            side=side, quantity=float(quantity), price=price, ticker=t,
        )

        now = db.utc_now_iso()
        if is_new_position:
            conn.execute(
                "INSERT INTO holdings (ticker, quantity, avg_price, currency,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                # Resolved from the ticker, not defaulted: a buy that opens a
                # KOSPI position must not be recorded as dollar-denominated.
                (t, new_qty, new_avg, resolve_asset_currency(t), now, now),
            )
        else:
            conn.execute(
                "UPDATE holdings SET quantity = ?, avg_price = ?, updated_at = ?"
                " WHERE ticker = ?",
                (new_qty, new_avg, now, t),
            )

        # A sale realizes the spread against the average the position carried
        # BEFORE it — `old_avg`, which `_apply_trade` already has and used to
        # discard. The average itself is untouched, per the module's
        # average-cost convention.
        realized_pnl = realized_pnl_base = None
        if side == "sell" and price is not None:
            realized_pnl = (price - old_avg) * float(quantity) - fee - tax
            entry_fx = _effective_entry_fx(conn, t, currency, default=rate)
            if rate is not None and entry_fx is not None:
                # Proceeds at the rate on the day of sale, minus cost at the rate
                # the position was actually funded at. The two diverge for a US
                # holding, and that difference IS the exchange-rate component —
                # the number a dollar-only report hides completely.
                realized_pnl_base = (
                    price * float(quantity) * float(rate)
                    - old_avg * float(quantity) * float(entry_fx)
                    - (fee + tax) * float(rate)
                )

        cur = conn.execute(
            "INSERT INTO trades (ticker, side, quantity, executed_at,"
            " execution_price, total_value, fx_rate, entry_rationale,"
            " avg_price_after, realized_pnl, realized_pnl_base, fee, tax,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, side, float(quantity), executed_at, price, total_value,
             rate, entry_rationale, new_avg, realized_pnl, realized_pnl_base,
             fee or None, tax or None, now),
        )
        trade_id = cur.lastrowid

        # The cash leg, in the SAME transaction. A trade whose cash movement is
        # missing is worse than no trade at all, so the two commit or fail as one.
        if writes_cash:
            cs.record_flow(
                "buy" if side == "buy" else "sell",
                currency, price * float(quantity), executed_at,
                fx_to_krw=rate, trade_id=trade_id, conn=conn,
            )
            for kind, amount in (("fee", fee), ("tax", tax)):
                if amount > 0:
                    cs.record_flow(
                        kind, currency, amount, executed_at,
                        fx_to_krw=rate, trade_id=trade_id, conn=conn,
                    )

    logger.info(
        f"[portfolio] {side} {quantity} {t} @ {price} → "
        f"qty {new_qty}, avg {new_avg}"
        + (" (no cash leg)" if not writes_cash else "")
    )
    if predates_anchor and price is not None:
        cash_warning = {
            "currency": currency,
            "shortfall": 0.0,
            "balance_before": round(cs.balance(currency), 4),
            "note": (
                f"This trade is dated before your opening balance "
                f"({anchor[:10]}), which already reflects it, so no cash was "
                f"moved. The journal entry is complete."
            ),
        }
    row = db.get_connection().execute(
        "SELECT * FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    result = dict(row)
    if cash_warning:
        result["cash_warning"] = cash_warning
    return result


def _effective_entry_fx(conn, ticker: str, currency: str,
                        default: float | None = None) -> float | None:
    """
    The capital-weighted rate at which a position was actually funded.

    ``cost_base / cost_local`` over the ledger's buy flows for this ticker — the
    identity phase 4's attribution rests on, and the reason ``fx_to_krw`` is
    ``NOT NULL``. Falls back to ``default`` when the position predates the
    ledger, so an old holding still yields a figure rather than a null that
    silently drops the whole base-currency P/L.
    """
    if currency == "KRW":
        return 1.0
    row = conn.execute(
        "SELECT SUM(-amount) AS local, SUM(-amount * fx_to_krw) AS base"
        " FROM cash_flows WHERE flow_type = 'buy' AND trade_id IN"
        " (SELECT id FROM trades WHERE ticker = ?)",
        (ticker,),
    ).fetchone()
    local = float(row["local"] or 0.0)
    base = float(row["base"] or 0.0)
    return (base / local) if local > 1e-9 else default


def _apply_trade(
    *, old_qty: float, old_avg: float, side: str,
    quantity: float, price: float | None, ticker: str,
) -> tuple[float, float]:
    """
    Pure position math: given the current position and a trade, return the new
    ``(quantity, avg_price)``. No I/O, so it is directly unit-testable.
    """
    if side == "buy":
        new_qty = old_qty + quantity
        if price is None:
            # Unknown fill price can't move a cost basis; keep the old average.
            return new_qty, old_avg
        new_avg = ((old_qty * old_avg) + (quantity * price)) / new_qty
        return new_qty, new_avg

    # Sell: reduce the position, leave the cost basis alone.
    if quantity > old_qty + 1e-9:   # tolerance for float accumulation
        raise InvalidTrade(
            f"Cannot sell {quantity} of {ticker}: only {old_qty} held."
        )
    new_qty = old_qty - quantity
    # A fully closed position keeps its last average for the journal's record;
    # quantity 0 is what marks it closed.
    return new_qty, old_avg


def recompute_average(ticker: str) -> dict:
    """
    Rebuild a holding's quantity and average price by replaying its trades in
    chronological order.

    ``record_trade`` already maintains both incrementally; this is the repair
    path — after a back-dated trade is inserted, or a correction is made, the
    incremental value is out of order and must be recomputed from the journal.

    Trades with no ``execution_price`` are skipped for the average (they carry
    no cost information) but still move quantity.

    Replay order is **opening entries first, then chronological**. A seeded
    position is stamped with the time it was entered, but by definition it was
    acquired before anything in the journal — so a purely chronological replay
    would apply it after back-dated trades and produce the wrong average.
    """
    t = _require_ticker(ticker)
    if get_holding(t) is None:
        raise HoldingNotFound(f"{t} is not in the portfolio.")

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT side, quantity, execution_price, entry_rationale, executed_at,"
            " id FROM trades WHERE ticker = ?", (t,)
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda r: (0 if is_opening_entry(dict(r)) else 1,
                           r["executed_at"], r["id"]),
        )

        qty, avg = 0.0, 0.0
        for r in rows:
            qty, avg = _apply_trade(
                old_qty=qty, old_avg=avg,
                side=r["side"], quantity=float(r["quantity"]),
                price=(float(r["execution_price"])
                       if r["execution_price"] is not None else None),
                ticker=t,
            )
        conn.execute(
            "UPDATE holdings SET quantity = ?, avg_price = ?, updated_at = ?"
            " WHERE ticker = ?",
            (qty, avg, db.utc_now_iso(), t),
        )
    logger.info(f"[portfolio] recomputed {t} from journal: qty {qty}, avg {avg}")
    return get_holding(t)


def recompute_position(ticker: str) -> dict:
    """
    Rebuild a holding's quantity, average price **and cash flows** from its
    journal — the repair path once trades move money.

    The ordering rule from :func:`recompute_average` is preserved exactly:
    opening entries first, then ``executed_at``, then ``id``. A seeded position
    carries a "now" timestamp but by definition predates everything logged, so a
    purely chronological replay would apply it after back-dated trades and
    produce the wrong average.

    Trade-linked flows are rewritten from the journal, which is the source of
    truth. Deposits, withdrawals, conversions and manual adjustments are **not**
    touched: they are facts about the user's cash that no trade implies, and
    regenerating them from the journal would delete them.
    """
    from services import cash_service as cs

    t = _require_ticker(ticker)
    if get_holding(t) is None:
        raise HoldingNotFound(f"{t} is not in the portfolio.")
    currency = resolve_asset_currency(t)

    holding = recompute_average(t)
    if not cs.is_initialized():
        return holding

    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE ticker = ?", (t,)
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda r: (0 if is_opening_entry(dict(r)) else 1,
                           r["executed_at"], r["id"]),
        )
        trade_ids = [r["id"] for r in rows]
        if trade_ids:
            placeholders = ",".join("?" for _ in trade_ids)
            conn.execute(
                f"DELETE FROM cash_flows WHERE trade_id IN ({placeholders})",
                trade_ids,
            )

        for r in rows:
            price = r["execution_price"]
            if price is None:
                continue          # nothing to move; matches `record_trade`
            rate = r["fx_rate"] if currency != cs.BASE_CURRENCY else 1.0
            if rate is None:
                logger.warning(
                    f"[portfolio] trade {r['id']} has no fx_rate; its cash leg "
                    f"cannot be rebuilt."
                )
                continue
            if is_opening_entry(dict(r)):
                # The seed's funding deposit goes back with it, or replaying
                # would leave the position unfunded and the balance negative.
                cs.record_flow("deposit", currency,
                               float(price) * float(r["quantity"]),
                               r["executed_at"], fx_to_krw=rate,
                               note=cs.SEED_FUNDING_NOTE, conn=conn)
            cs.record_flow(
                "buy" if r["side"] == "buy" else "sell",
                currency, float(price) * float(r["quantity"]),
                r["executed_at"], fx_to_krw=rate, trade_id=r["id"], conn=conn,
            )
            for kind in ("fee", "tax"):
                amount = r[kind]
                if amount:
                    cs.record_flow(kind, currency, float(amount),
                                   r["executed_at"], fx_to_krw=rate,
                                   trade_id=r["id"], conn=conn)

    logger.info(f"[portfolio] rebuilt cash flows for {t} from its journal")
    return get_holding(t)


async def record_trade_auto(
    ticker: str,
    side: str,
    quantity: float,
    executed_at: str,
    **kwargs,
) -> dict:
    """
    :func:`record_trade`, resolving the exchange rate the cash leg needs.

    The network hop lives out here so ``record_trade`` can stay synchronous and
    run inside a held write transaction — an ``await`` in the middle of one is
    not something to introduce.
    """
    from services import cash_service as cs

    if kwargs.get("fx_rate") is None:
        currency = resolve_asset_currency(ticker)
        if currency != cs.BASE_CURRENCY and cs.is_initialized():
            try:
                kwargs["fx_rate"] = await cs.resolve_rate(currency, executed_at)
            except cs.CashError as e:
                raise InvalidTrade(str(e))
    return record_trade(ticker, side, quantity, executed_at, **kwargs)


async def add_holding_auto(ticker: str, quantity: float, avg_price: float,
                           **kwargs) -> dict:
    """:func:`add_holding`, resolving the rate a seeded position's funding needs."""
    from services import cash_service as cs

    if kwargs.get("initial_fx_rate") is None:
        currency = (kwargs.get("currency") or "").strip().upper() \
            or resolve_asset_currency(ticker)
        if currency != cs.BASE_CURRENCY and cs.is_initialized():
            try:
                kwargs["initial_fx_rate"] = await cs.resolve_rate(
                    currency, db.utc_now_iso()
                )
            except cs.CashError as e:
                raise InvalidTrade(str(e))
    return add_holding(ticker, quantity, avg_price, **kwargs)



# =============================================================================
# Valuation (phase 3)
# =============================================================================

async def _entry_usdkrw(ticker: str, currency: str,
                        rate_cache: dict[str, float | None] | None = None) -> float | None:
    """
    The capital-weighted USDKRW rate at which a position was funded.

    For a **USD** asset this is exact and needs no network: ``fx_to_krw`` on the
    buy flows already *is* the USDKRW rate that applied.

    For a **KRW** asset ``fx_to_krw`` is 1.0 by definition, so the dollar view of
    a Korean holding needs the USDKRW rate on each buy date, looked up (and
    cached per date). Best-effort: a failed lookup returns ``None`` and the
    caller reports the dollar figures as unknown rather than converting at
    today's rate, which would erase the currency component entirely.
    """
    from providers import fx_provider

    conn = db.get_connection()
    rows = conn.execute(
        "SELECT amount, fx_to_krw, occurred_at FROM cash_flows"
        " WHERE flow_type = 'buy' AND trade_id IN"
        " (SELECT id FROM trades WHERE ticker = ?)",
        (ticker,),
    ).fetchall()
    if not rows:
        return None

    if currency == "USD":
        total = sum(-float(r["amount"]) for r in rows)
        if total <= 1e-9:
            return None
        return sum(-float(r["amount"]) * float(r["fx_to_krw"]) for r in rows) / total

    cache = rate_cache if rate_cache is not None else {}
    weighted, total = 0.0, 0.0
    for r in rows:
        amount = -float(r["amount"])
        if amount <= 0:
            continue
        day = (r["occurred_at"] or "")[:10]
        if day not in cache:
            try:
                when = datetime.fromisoformat(
                    r["occurred_at"].strip().replace("Z", "+00:00")
                )
                cache[day] = (await fx_provider.fetch_rate_at(when)).rate
            except Exception:  # noqa: BLE001 — display path; degrade, never fail
                cache[day] = None
        rate = cache[day]
        if rate is None:
            return None
        weighted += amount * rate
        total += amount
    return (weighted / total) if total > 1e-9 else None


async def value_holdings(rows: list[dict] | None = None) -> tuple[list[dict], dict]:
    """
    Attach live market value and unrealized ROI to each holding.

    Returns ``(holdings, totals)``. Prices are fetched concurrently and
    best-effort: a ticker whose lookup fails keeps ``current_price = None``
    rather than failing the request, so one delisted symbol cannot blank the
    whole portfolio. Such a row is excluded from the totals — averaging it in as
    zero would understate the portfolio, which is worse than reporting less.

    **Currency**: every per-row figure is in that holding's own currency, and a
    total is only stated when one can honestly be stated. This function used to
    sum ``quantity × price`` across every row without reading
    ``holdings.currency``, so ten Samsung shares at ₩71,000 added 710,000 to a
    dollar total with nothing raised. The guard below refuses that total instead;
    :func:`value_holdings_converted` is the version that can produce a real one.
    """
    from providers import price_provider   # local import: keeps startup light

    rows = list_holdings() if rows is None else rows
    prices = await price_provider.fetch_current_prices([r["ticker"] for r in rows])

    valued: list[dict] = []
    # Cost basis and market value per currency, so nothing is added across
    # denominations by accident.
    cost_by_ccy: dict[str, float] = {}
    value_by_ccy: dict[str, float] = {}
    priced_cost_by_ccy: dict[str, float] = {}

    for r in rows:
        row = dict(r)
        qty, avg = float(row["quantity"]), float(row["avg_price"])
        # An older row may predate `resolve_asset_currency`; trust the ticker
        # over a stored default that was never chosen deliberately.
        ccy = (row.get("currency") or "").strip().upper() or resolve_asset_currency(row["ticker"])
        row["currency"] = ccy
        cost = qty * avg
        cost_by_ccy[ccy] = cost_by_ccy.get(ccy, 0.0) + cost

        price = prices.get(row["ticker"])
        if price is None:
            row.update(current_price=None, market_value=None,
                       unrealized_pnl=None, unrealized_roi=None)
        else:
            market_value = qty * price
            row.update(
                current_price=price,
                market_value=round(market_value, 4),
                unrealized_pnl=round(market_value - cost, 4),
                # Guard the zero-cost case: a fully closed position has qty 0,
                # and a seeded avg_price of 0 would divide by zero here.
                unrealized_roi=(round((price - avg) / avg, 6) if avg else None),
            )
            value_by_ccy[ccy] = value_by_ccy.get(ccy, 0.0) + market_value
            priced_cost_by_ccy[ccy] = priced_cost_by_ccy.get(ccy, 0.0) + cost
        valued.append(row)

    currencies = sorted(set(cost_by_ccy) | set(value_by_ccy))
    total_cost = sum(cost_by_ccy.values())
    total_value = sum(value_by_ccy.values())
    priced_cost = sum(priced_cost_by_ccy.values())
    any_priced = priced_cost > 0

    totals = {
        "currencies": currencies,
        "cost_basis_by_currency": {c: round(v, 4) for c, v in cost_by_ccy.items()},
        "market_value_by_currency": {c: round(v, 4) for c, v in value_by_ccy.items()},
        "total_cost_basis": round(total_cost, 4),
        "total_market_value": round(total_value, 4) if any_priced else None,
        "total_unrealized_pnl": round(total_value - priced_cost, 4) if any_priced else None,
        "total_roi": round((total_value - priced_cost) / priced_cost, 6) if any_priced else None,
        "note": None,
    }

    # Refuse to state a NATIVE-currency total that mixes denominations. The
    # dual-currency figures below are the answer to that; these stay withheld so
    # an old consumer reading `total_market_value` alone cannot be misled.
    if len(currencies) > 1:
        totals.update(
            total_cost_basis=None,
            total_market_value=None,
            total_unrealized_pnl=None,
            total_roi=None,
            note=(
                "Holdings span more than one currency "
                f"({', '.join(currencies)}); per-position figures are in each "
                "holding's own currency, and the KRW/USD totals below state the "
                "whole portfolio in each."
            ),
        )

    await _attach_dual_currency(valued, totals)
    return valued, totals


async def _attach_dual_currency(valued: list[dict], totals: dict) -> None:
    """
    Restate every amount in **both** KRW and USD, add weights over net worth, and
    split each position's return into its stock and currency components.

    The attribution identity, exact and multiplicative:

        1 + R_base = (1 + R_local) x (1 + R_fx)

    with a cross term — a 10% stock gain on a 10% currency gain is +21%, not
    +20%. All three are reported separately because *what to buy* and *when to
    convert* are two different decisions and deserve two scorecards.

    This is the **only** place a conversion happens. Nothing downstream may
    multiply an amount by a rate; they read the pair. One conversion site means
    one place for a rounding or staleness bug to live.
    """
    from providers import fx_provider
    from services import cash_service as cs

    spot: float | None = None
    try:
        spot = (await fx_provider.fetch_spot()).rate
    except Exception as e:  # noqa: BLE001 — display path
        logger.warning(f"[portfolio] no spot rate for dual-currency view: {e}")

    def to_krw(amount, ccy):
        return fx_provider.convert(amount, ccy, "KRW", spot)

    def to_usd(amount, ccy):
        return fx_provider.convert(amount, ccy, "USD", spot)

    conn = db.get_connection()
    rate_cache: dict[str, float | None] = {}

    equity_krw = equity_usd = 0.0
    cost_krw_total = cost_usd_total = 0.0
    foreign_krw = 0.0
    priced_any = False

    for row in valued:
        ccy = row["currency"]
        qty, avg = float(row["quantity"]), float(row["avg_price"])
        cost_local = qty * avg

        entry_fx = _effective_entry_fx(conn, row["ticker"], ccy)
        entry_usdkrw = await _entry_usdkrw(row["ticker"], ccy, rate_cache)

        # Cost basis: converted at the rates that applied WHEN THE MONEY MOVED,
        # never at today's. Using the current rate here would make the won-return
        # numerically identical to the dollar-return and delete the FX component.
        if ccy == "USD":
            row["cost_basis_krw"] = round(cost_local * entry_fx, 4) if entry_fx else None
            row["cost_basis_usd"] = round(cost_local, 4)
        else:
            row["cost_basis_krw"] = round(cost_local, 4)
            row["cost_basis_usd"] = (
                round(cost_local / entry_usdkrw, 4) if entry_usdkrw else None
            )

        mv = row.get("market_value")
        row["market_value_krw"] = round(to_krw(mv, ccy), 4) if to_krw(mv, ccy) is not None else None
        row["market_value_usd"] = round(to_usd(mv, ccy), 4) if to_usd(mv, ccy) is not None else None

        for unit in ("krw", "usd"):
            m, c = row[f"market_value_{unit}"], row[f"cost_basis_{unit}"]
            row[f"unrealized_pnl_{unit}"] = (
                round(m - c, 4) if (m is not None and c is not None) else None
            )
            row[f"roi_{unit}"] = (
                round(m / c - 1, 6) if (m is not None and c and c > 0) else None
            )

        # The stock's own return, and the currency's, as separate figures.
        row["roi_local"] = row.get("unrealized_roi")
        if ccy == "KRW":
            row["roi_fx"] = 0.0          # a won asset has no currency component
        elif entry_fx and spot:
            row["roi_fx"] = round(spot / entry_fx - 1, 6)
        else:
            row["roi_fx"] = None

        if row["market_value_krw"] is not None:
            equity_krw += row["market_value_krw"]
            equity_usd += row["market_value_usd"] or 0.0
            priced_any = True
            if ccy != cs.BASE_CURRENCY:
                foreign_krw += row["market_value_krw"]
        if row["cost_basis_krw"] is not None:
            cost_krw_total += row["cost_basis_krw"]
        if row["cost_basis_usd"] is not None:
            cost_usd_total += row["cost_basis_usd"]

    balances = cs.balances()
    cash_krw = sum((to_krw(v, c) or 0.0) for c, v in balances.items())
    cash_usd = sum((to_usd(v, c) or 0.0) for c, v in balances.items())
    foreign_krw += sum(
        (to_krw(v, c) or 0.0) for c, v in balances.items() if c != cs.BASE_CURRENCY
    )

    net_krw = equity_krw + cash_krw
    net_usd = equity_usd + cash_usd

    # Weights divide by NET WORTH, so every position plus cash sums to 1.0.
    # Dividing by equity value — what `risk_metrics` still does — describes a
    # portfolio the user does not have.
    positive_net = net_krw > 1e-9
    for row in valued:
        mv = row.get("market_value_krw")
        row["weight"] = (
            round(mv / net_krw, 6) if (positive_net and mv is not None) else None
        )

    totals.update({
        "cash_balances": balances,
        "cash_total_krw": round(cash_krw, 4) if spot else None,
        "cash_total_usd": round(cash_usd, 4) if spot else None,
        "equity_total_krw": round(equity_krw, 4) if priced_any else None,
        "equity_total_usd": round(equity_usd, 4) if priced_any else None,
        "net_worth_krw": round(net_krw, 4) if spot else None,
        "net_worth_usd": round(net_usd, 4) if spot else None,
        "cost_basis_krw": round(cost_krw_total, 4) or None,
        "cost_basis_usd": round(cost_usd_total, 4) or None,
        "cash_weight": round(cash_krw / net_krw, 6) if positive_net else None,
        "equity_weight": round(equity_krw / net_krw, 6) if positive_net else None,
        # The share of this user's wealth whose base-currency value moves with
        # the exchange rate — the single figure that answers "how exposed am I
        # to the dollar", and phase 5's input.
        "fx_exposure": round(foreign_krw / net_krw, 6) if positive_net else None,
        "roi_krw_total": (
            round(equity_krw / cost_krw_total - 1, 6)
            if (priced_any and cost_krw_total > 0) else None
        ),
        "roi_usd_total": (
            round(equity_usd / cost_usd_total - 1, 6)
            if (priced_any and cost_usd_total > 0) else None
        ),
    })


async def resolve_execution_price(ticker: str, executed_at: str):
    """
    Derive what a trade filled at — blueprint §1's core automation.

    Wraps the provider so the router never imports yfinance directly, and maps a
    lookup failure onto :class:`InvalidTrade` (a 400): an unresolvable fill is a
    problem with the submitted trade, not a server fault.
    """
    from providers import price_provider

    try:
        when = datetime.fromisoformat((executed_at or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise InvalidTrade(
            f"executed_at must be an ISO-8601 datetime (got {executed_at!r})."
        )
    try:
        return await price_provider.fetch_execution_price(ticker, when)
    except ValueError as e:
        raise InvalidTrade(str(e))
    except Exception as e:  # noqa: BLE001 — network/provider trouble
        raise InvalidTrade(
            f"Could not resolve an execution price for {normalize_ticker(ticker)}: {e}"
        )

# =============================================================================
# 8-Quarter Fundamental Baseline (blueprint §4)
# =============================================================================
# When a ticker joins the portfolio, pull its last ~2 years of filings so the
# Coach agent has long-term corporate health to anchor on instead of one quarter.
#
# Scope note: this triggers the *ingestion* of the baseline, not the MAS debate.
# The "Baseline Debate" is exactly `analyze_pipeline`, which the user runs from
# Deep Analysis once the filings are in — auto-running it here would spend a full
# multi-agent LLM budget on every added ticker without the user asking. The plan
# permits deferring it; this is the deferral.

BASELINE_YEARS = 2

# How many completed calendar quarters to analyze when a ticker joins the
# portfolio. One full MAS run per quarter, each over a window ENDING at that
# quarter's close.
#
# Why per-quarter rather than one run over the whole span: a single run is
# stamped with today's window, and `history_store.analysis_as_of` will not hand a
# post-trade analysis to a retrospective review — the current technical report
# already knows which way the price went. A run whose window ends 2026-06-30
# contains nothing that postdates that quarter, so it IS legitimate evidence for
# an August trade. Quarterly runs are what give the coach anything to cite about
# a decision the user already made.
#
# This is deliberately expensive: eight full multi-agent runs per added ticker.
# It replaces the earlier deferral, at the user's request.
BASELINE_QUARTERS = 8

# Status of each ticker's baseline fetch, for the UI to poll. In-memory on
# purpose: it describes a *this-process* background task, so a restart correctly
# forgets it (the ingested filings are likewise in-memory, per `services.storage`).
_baseline_status: dict[str, dict] = {}


def baseline_status(ticker: str) -> dict:
    """Current baseline-fetch status for a ticker."""
    return _baseline_status.get(
        normalize_ticker(ticker), {"state": "none", "message": "No baseline fetch run."}
    )


def all_baseline_statuses() -> dict[str, dict]:
    return dict(_baseline_status)


def _baseline_window(today: date | None = None) -> tuple[int, int]:
    """
    Fiscal-year span covering roughly the last 8 quarters.

    Returns ``(start_year, end_year)``. The span is BASELINE_YEARS wide, which
    ``sec_fetch.plan_filings`` accepts (its MAX_YEAR_SPAN is 5).
    """
    year = (today or datetime.now(timezone.utc).date()).year
    # Filings for the current fiscal year may not exist yet; ending on the
    # current year is still correct — plan_filings simply returns what EDGAR has.
    return year - BASELINE_YEARS + 1, year


def quarter_ends(count: int = BASELINE_QUARTERS, today: date | None = None) -> list[date]:
    """
    The last ``count`` **completed** calendar quarter ends, oldest first.

    The current quarter is excluded: it has not closed, so an analysis of it
    would be a partial period that a later run over the same quarter would
    silently disagree with.
    """
    import calendar

    d = today or datetime.now(timezone.utc).date()
    year, quarter = d.year, (d.month - 1) // 3 + 1
    ends: list[date] = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
        month = quarter * 3
        ends.append(date(year, month, calendar.monthrange(year, month)[1]))
    return sorted(ends)


def missing_quarters(ticker: str, count: int = BASELINE_QUARTERS) -> list[date]:
    """
    The baseline quarters that have no stored analysis yet.

    Makes the whole step idempotent and, more to the point, cheap to re-enter:
    re-adding a ticker, or adding one whose filings are already ingested, spends
    a full multi-agent run only on the quarters actually missing. At eight runs
    apiece that difference is the difference between usable and not.
    """
    from rag import history_store

    wanted = quarter_ends(count)
    have: set[str] = set()
    for summary in history_store.get_analysis_history(ticker, limit=200):
        period = (summary.get("analysis_period") or "")
        if ".." in period:
            have.add(period.split("..", 1)[1].strip())
    return [q for q in wanted if q.isoformat() not in have]


async def run_baseline_analyses(
    ticker: str, doc_store, debate_store, ends: list[date] | None = None
) -> dict:
    """
    Run one full Deep Analysis per completed quarter for a newly added ticker.

    **Reuses ``services.pipeline.analyze_pipeline`` unchanged.** That generator
    already does the three phases, the rate limiting, the 3-axis scoring and the
    write to ``rag.history_store`` — so the runs show up in the Deep Analysis
    view's history sidebar with no new plumbing. All this function does is decide
    the eight windows and drive the existing pipeline over each.

    Only ``end_date`` is set per run. ``pipeline.analysis_window`` then derives
    the start from its own default trailing window, which is exactly what each
    agent needs (a full SMA200 lookback, several quarters of transcripts) ending
    at that quarter's close.

    Runs sequentially. The pipeline is already internally rate-limited across six
    agents, and firing eight of them concurrently would collide with that.

    Never raises: one failed quarter is recorded and the rest continue, matching
    how filing ingestion already behaves.
    """
    from schemas import AnalyzeRequest
    from services.pipeline import analyze_pipeline

    t = normalize_ticker(ticker)
    ends = missing_quarters(t) if ends is None else ends
    if not ends:
        _set_analysis_status(
            t, state="complete", completed=0, total=0,
            message=f"Every baseline quarter for {t} has already been analyzed.",
            failures=[], run_ids=[],
        )
        return _baseline_status[t]["analysis"]

    completed, failures, run_ids = 0, [], []

    for i, end in enumerate(ends, start=1):
        _set_analysis_status(
            t, state="running", completed=completed, total=len(ends),
            message=f"Analyzing quarter {i} of {len(ends)} (through {end})…",
            failures=failures, run_ids=run_ids,
        )
        try:
            request = AnalyzeRequest(ticker=t, end_date=end.isoformat())
            async for event in analyze_pipeline(request, doc_store, debate_store):
                if event.get("status") == "complete":
                    rid = (event.get("result") or {}).get("run_id")
                    if rid:
                        run_ids.append(rid)
            completed += 1
        except Exception as e:  # noqa: BLE001 — one quarter must not stop the rest
            logger.error(f"[portfolio] baseline analysis {t} through {end} failed: {e}")
            failures.append(f"{end}: {e}")

    if completed == 0:
        state = "failed"
        message = (
            f"No quarterly analysis completed for {t}. "
            + ("; ".join(failures) or "The pipeline returned nothing.")
        )
    else:
        state = "partial" if failures else "complete"
        message = (
            f"Analyzed {completed} of {len(ends)} quarters for {t} "
            f"({ends[0]} … {ends[-1]}). Open Deep Analysis to read them."
        )
        if failures:
            message += f" {len(failures)} quarter(s) failed."

    _set_analysis_status(t, state=state, completed=completed, total=len(ends),
                         message=message, failures=failures, run_ids=run_ids)
    logger.info(f"[portfolio] baseline analysis for {t}: {state} ({completed}/{len(ends)})")
    return _baseline_status[t]["analysis"]


def _set_analysis_status(ticker: str, **fields) -> None:
    """Merge analysis progress into the ticker's baseline status, for polling."""
    entry = _baseline_status.setdefault(ticker, {})
    entry["analysis"] = {**entry.get("analysis", {}), **fields}


async def fetch_baseline(ticker: str, store, debate_store=None) -> dict:
    """
    Fetch and ingest ~8 quarters of filings for ``ticker``.

    Runs both forms over the same window: the 10-Ks give the annual picture and
    the 10-Qs the quarterly cadence. Note that **an "8 quarter" request resolves
    to about 6 10-Qs plus 2 10-Ks**, not 8 10-Qs — the SEC does not file a Q4
    10-Q, and that quarter's figures live in the annual report instead.

    Never raises: this runs detached in the background, so a failure is recorded
    in the status map rather than surfacing as an unhandled task exception.
    """
    from services import sec_ingest   # local import: avoids a cycle at startup

    t = normalize_ticker(ticker)
    start_year, end_year = _baseline_window()
    _baseline_status[t] = {
        "state": "running",
        "message": f"Fetching {start_year}–{end_year} filings from SEC EDGAR…",
        "ingested": 0,
    }

    total_ingested, failures = 0, []
    for form_type in ("10-K", "10-Q"):
        try:
            result = await sec_ingest.fetch_and_ingest_range(
                ticker=t,
                form_type=form_type,
                start_year=start_year,
                end_year=end_year,
                start_quarter=None,
                end_quarter=None,
                store=store,
            )
            total_ingested += result.succeeded
            _baseline_status[t]["ingested"] = total_ingested
        except Exception as e:  # noqa: BLE001 — background task must not die
            logger.error(f"[portfolio] baseline {form_type} failed for {t}: {e}")
            failures.append(f"{form_type}: {e}")

    if total_ingested == 0:
        state, message = "failed", (
            f"No filings ingested for {t}. " + ("; ".join(failures) or
            "SEC EDGAR returned nothing for this range.")
        )
    else:
        state = "partial" if failures else "complete"
        message = (
            f"Ingested {total_ingested} filing(s) for {t} covering "
            f"{start_year}–{end_year} (~8 quarters: 10-Qs cover Q1–Q3, "
            f"Q4 figures come from each 10-K)."
        )
        if failures:
            message += " Some forms failed: " + "; ".join(failures)

    _baseline_status[t] = {
        "state": state, "message": message, "ingested": total_ingested,
        "start_year": start_year, "end_year": end_year,
        "analysis": _baseline_status.get(t, {}).get("analysis", {
            "state": "pending", "completed": 0, "total": BASELINE_QUARTERS,
            "message": "Waiting for filings before analysis can start.",
        }),
    }
    logger.info(f"[portfolio] baseline for {t}: {state} ({total_ingested} filings)")

    # Then the analysis, over the same span. Ingestion has to land first — the
    # agents read the filings this step just wrote, so running them concurrently
    # would analyze a company store that is still filling up.
    if total_ingested == 0:
        _set_analysis_status(
            t, state="skipped", completed=0, total=BASELINE_QUARTERS,
            message=("No filings were ingested, so there is nothing for the "
                     "analysts to read."),
        )
    elif debate_store is None:
        # Only reachable if a caller forgot to thread the store through; say so
        # rather than silently producing no analysis.
        _set_analysis_status(
            t, state="skipped", completed=0, total=BASELINE_QUARTERS,
            message="No debate store was supplied, so analysis was not run.",
        )
        logger.warning(f"[portfolio] no debate_store for {t}; skipping analyses.")
    else:
        await run_baseline_analyses(t, store, debate_store)

    return _baseline_status[t]


def trigger_baseline_if_new(ticker: str, store, debate_store=None) -> bool:
    """
    Kick off :func:`fetch_baseline` in the background when a ticker has no
    filings yet. Returns whether a fetch was started.

    Detached via ``asyncio.create_task`` rather than awaited: SEC rendering is
    sequential and rate-limited, so a 2-year fetch takes far longer than an HTTP
    request should. The caller returns immediately and the UI polls the status.
    """
    t = normalize_ticker(ticker)

    # EDGAR covers US-listed issuers only. Firing a two-year fetch at a KOSPI
    # ticker cannot succeed no matter how long it runs, and reporting "failed"
    # invites the user to retry something that will never work. Say what is
    # actually true instead.
    if not is_us_listed(t):
        _baseline_status[t] = {
            "state": "unsupported",
            "message": (
                f"SEC EDGAR covers US-listed issuers only, so no fundamental "
                f"baseline is available for {t}. Portfolio tracking, risk, and "
                f"the trading coach all still work for this holding."
            ),
            "ingested": 0,
        }
        logger.info(f"[portfolio] {t} is not US-listed — no SEC baseline.")
        return False

    if _baseline_status.get(t, {}).get("state") == "running":
        return False

    # Ingestion and analysis are separate needs. A ticker whose filings are
    # already in the store may still have no analysis the coach can cite — which
    # is precisely the case that produced "no fundamental or technical analyst
    # reports were provided" on a trade the user had already logged. Skipping
    # both because one was satisfied is what hid that.
    needs_filings = not store.has_company(t)
    missing = missing_quarters(t)

    if not needs_filings and not missing:
        logger.info(f"[portfolio] {t} already has filings and every analysis.")
        return False

    if needs_filings:
        _baseline_status[t] = {
            "state": "queued",
            "message": "Baseline fetch queued.",
            "ingested": 0,
            "analysis": {
                "state": "pending", "completed": 0, "total": len(missing),
                "message": "Waiting for filings before analysis can start.",
            },
        }
        asyncio.create_task(fetch_baseline(t, store, debate_store))
        return True

    # Filings are here; only the analyses are missing.
    _baseline_status[t] = {
        "state": "complete",
        "message": f"{t} already has filings ingested.",
        "ingested": 0,
        "analysis": {
            "state": "queued", "completed": 0, "total": len(missing),
            "message": f"{len(missing)} quarter(s) queued for analysis.",
        },
    }
    if debate_store is None:
        _set_analysis_status(
            t, state="skipped",
            message="No debate store was supplied, so analysis was not run.",
        )
        return False
    asyncio.create_task(run_baseline_analyses(t, store, debate_store, missing))
    return True
