"""
services.cash_service
─────────────────────
The repository for cash — the **only** module that writes SQL against
``cash_flows``, so phases 3-7 call these functions and never see a cursor.

Why a ledger and not a balance
──────────────────────────────
A single mutable balance cannot distinguish **money the user added** from **money
the portfolio made**. Deposit ₩5M into a ₩10M account and a balance-only model
reports +50% return. That distinction has to exist at write time or it never
exists at all — which is why deposits and withdrawals are their own flow types
(:data:`EXTERNAL_FLOWS`) and every balance is derived by replaying rows.

Why per-currency
────────────────
This user holds won and dollars at once and converts between them. Collapsing
both into one number destroys the FX position, which is one of the things the
plan exists to measure. So every flow carries its own ``currency`` and
:func:`balances` returns one figure per currency.

**No conversion happens in this module.** Turning per-currency balances into one
base-currency total needs a rate, and therefore network I/O; that belongs to
``providers.fx_provider`` and its callers. What this module does insist on is
that every row records the rate that applied *when it was written* — see
``fx_to_krw`` in :mod:`services.db`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from services import db

logger = logging.getLogger(__name__)


# =============================================================================
# Vocabulary
# =============================================================================

# The currency risk and return are measured in — a property of the investor, not
# of the app. See `cash_ledger_plan/README.md` decision 1.
BASE_CURRENCY = "KRW"
SUPPORTED_CURRENCIES = ("KRW", "USD")

# Only these two move money in or out of the portfolio. Everything else — trades,
# fees, dividends, conversions — rearranges money already inside it. The
# distinction is what makes a time-weighted return possible in phase 4.
EXTERNAL_FLOWS = frozenset({"deposit", "withdrawal"})

INFLOW_TYPES = frozenset({"deposit", "sell", "dividend", "interest", "fx_in"})
OUTFLOW_TYPES = frozenset({"withdrawal", "buy", "fee", "tax", "fx_out"})
# `adjustment` is in neither set: a reconciliation can go either way, so its sign
# comes from the caller and is the one case where that is correct.
FLOW_TYPES = INFLOW_TYPES | OUTFLOW_TYPES | {"adjustment"}

OPENING_NOTE = "Opening cash balance recorded at portfolio setup."
SEED_FUNDING_NOTE = "Synthetic funding for a position seeded at setup."


# =============================================================================
# Domain errors
# =============================================================================
# Non-HTTP on purpose, mirroring `portfolio_service`: the phase 7 agents import
# this module and must not drag in FastAPI. The router maps these to codes.

class CashError(Exception):
    """Base class for cash-ledger domain errors."""


class InvalidFlow(CashError):
    """The flow is not valid — bad type, currency, amount, or sign."""


class LedgerNotInitialized(CashError):
    """An opening balance has not been recorded yet."""


class LedgerAlreadyInitialized(CashError):
    """An opening balance has already been recorded."""


# =============================================================================
# Helpers
# =============================================================================

def normalize_currency(currency: str) -> str:
    c = (currency or "").strip().upper()
    if c not in SUPPORTED_CURRENCIES:
        raise InvalidFlow(
            f"Unsupported currency {currency!r}. "
            f"Supported: {', '.join(SUPPORTED_CURRENCIES)}."
        )
    return c


def _signed_amount(flow_type: str, amount: float) -> float:
    """
    Apply the sign that ``flow_type`` implies.

    Callers pass a positive magnitude and the direction is derived here, rather
    than trusting each call site to remember that a fee is negative. A caller who
    passes a sign that contradicts the type is refused instead of silently
    corrected — that mismatch means the two sides disagree about what happened.
    """
    if amount == 0:
        raise InvalidFlow("A cash flow of zero has nothing to record.")

    if flow_type == "adjustment":
        return float(amount)   # direction is the caller's to state

    if flow_type in INFLOW_TYPES:
        if amount < 0:
            raise InvalidFlow(
                f"{flow_type!r} moves money IN, but a negative amount "
                f"({amount}) was given."
            )
        return float(amount)

    if flow_type in OUTFLOW_TYPES:
        if amount < 0:
            raise InvalidFlow(
                f"{flow_type!r} moves money OUT; pass a positive magnitude "
                f"({abs(amount)}) and the sign is applied here."
            )
        return -float(amount)

    raise InvalidFlow(
        f"Unknown flow_type {flow_type!r}. "
        f"Known: {', '.join(sorted(FLOW_TYPES))}."
    )


def _validate_fx(currency: str, fx_to_krw: float | None) -> float:
    """
    A KRW row is 1.0 by definition; a USD row needs a real rate.

    ``fx_to_krw`` is ``NOT NULL`` in the schema precisely so a missing rate
    cannot be papered over with a default. ``1.0`` on a USD row would silently
    assert that one dollar is one won, which is worse than refusing to write.
    """
    if currency == BASE_CURRENCY:
        return 1.0
    if fx_to_krw is None:
        raise InvalidFlow(
            f"An exchange rate is required to record a {currency} flow — it is "
            f"the only record of what this money was worth in {BASE_CURRENCY} "
            f"at the time, and it cannot be recovered later."
        )
    rate = float(fx_to_krw)
    if rate <= 0:
        raise InvalidFlow(f"fx_to_krw must be greater than zero (got {rate}).")
    return rate


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


# =============================================================================
# Writing
# =============================================================================

def record_flow(
    flow_type: str,
    currency: str,
    amount: float,
    occurred_at: str,
    *,
    fx_to_krw: float | None = None,
    trade_id: int | None = None,
    conversion_id: str | None = None,
    note: str | None = None,
    conn=None,
) -> dict:
    """
    Record one movement of money.

    ``amount`` is a **positive magnitude**; the sign comes from ``flow_type``.

    ``conn`` lets a caller enlist this write in a transaction it already holds —
    phase 3 writes a trade and its cash leg together that way. A trade whose cash
    leg is missing is worse than no trade at all, so the two must commit or fail
    as one. When ``conn`` is ``None`` this opens its own transaction.
    """
    ft = (flow_type or "").strip().lower()
    ccy = normalize_currency(currency)
    signed = _signed_amount(ft, amount)
    rate = _validate_fx(ccy, fx_to_krw)

    if not (occurred_at or "").strip():
        raise InvalidFlow("occurred_at is required — a flow with no date cannot "
                          "be replayed in order.")

    now = db.utc_now_iso()
    params = (ft, ccy, signed, rate, occurred_at, trade_id, conversion_id,
              note, now)
    sql = (
        "INSERT INTO cash_flows (flow_type, currency, amount, fx_to_krw,"
        " occurred_at, trade_id, conversion_id, note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
    )

    if conn is not None:
        flow_id = conn.execute(sql, params).lastrowid
    else:
        with db.transaction() as c:
            flow_id = c.execute(sql, params).lastrowid

    logger.info(
        f"[cash] {ft} {signed:+.4f} {ccy} @ {rate} on {occurred_at}"
        + (f" (trade {trade_id})" if trade_id else "")
    )
    # Read back through the same connection when enlisted, so the row is visible
    # before the caller's transaction commits.
    reader = conn if conn is not None else db.get_connection()
    return _row(reader.execute(
        "SELECT * FROM cash_flows WHERE id = ?", (flow_id,)
    ).fetchone())


def delete_flow(flow_id: int) -> None:
    """Remove one flow — for a mistyped entry, not for rewriting history."""
    if get_flow(flow_id) is None:
        raise CashError(f"No cash flow with id {flow_id}.")
    with db.transaction() as conn:
        conn.execute("DELETE FROM cash_flows WHERE id = ?", (int(flow_id),))
    logger.info(f"[cash] deleted flow {flow_id}")


def effective_acquisition_rate(
    currency: str, as_of: str | None = None
) -> float | None:
    """
    The capital-weighted rate at which the currency currently held was acquired.

    Computed by **average cost**, replayed over the ledger: an inflow moves the
    weighted average, an outflow reduces the balance and leaves the average
    alone. That is the same convention ``_apply_trade`` uses for share prices
    (``portfolio_service.py:23-27``), and matching it is not cosmetic — mixing
    average-cost equities with FIFO currency would make the totals fail to
    reconcile against each other.

    Returns ``None`` when nothing of that currency is held, or for the base
    currency, which by definition was not acquired at a rate.
    """
    ccy = normalize_currency(currency)
    if ccy == BASE_CURRENCY:
        return None

    sql = "SELECT amount, fx_to_krw FROM cash_flows WHERE currency = ?"
    params: list = [ccy]
    if as_of:
        sql += " AND occurred_at <= ?"
        params.append(as_of)
    sql += " ORDER BY occurred_at ASC, id ASC"

    qty, avg = 0.0, 0.0
    for r in db.get_connection().execute(sql, params).fetchall():
        amount, rate = float(r["amount"]), float(r["fx_to_krw"])
        if amount > 0:
            if qty <= 1e-9:
                # Nothing was held, so there is no prior average to blend with.
                # Without this guard a replay that has gone negative divides the
                # incoming value by the NET balance and produces a rate that was
                # never paid.
                qty, avg = amount, rate
            else:
                new_qty = qty + amount
                avg = ((qty * avg) + (amount * rate)) / new_qty
                qty = new_qty
        else:
            qty += amount   # an outflow spends currency at the average it holds
    return avg if qty > 1e-9 and avg > 0 else None


def convert(
    from_currency: str,
    from_amount: float,
    to_currency: str,
    to_amount: float,
    occurred_at: str,
    *,
    market_rate: float | None = None,
    note: str | None = None,
    conn=None,
) -> dict:
    """
    Record a 환전 as two linked legs in one transaction.

    The effective rate is **derived from the two amounts**, not fetched: the
    user's bank or broker charged a spread over the mid-market rate, and that
    spread is a real cost that only their own numbers contain.

    Both legs share a ``conversion_id`` so the pair renders as one event, and
    both are internal — no money entered or left the portfolio, so neither is in
    :data:`EXTERNAL_FLOWS` and neither disturbs the phase 4 return calculation.
    """
    src = normalize_currency(from_currency)
    dst = normalize_currency(to_currency)
    if src == dst:
        raise InvalidFlow(f"A conversion needs two different currencies (got {src} twice).")
    if from_amount <= 0 or to_amount <= 0:
        raise InvalidFlow("Both sides of a conversion must be positive amounts.")

    # KRW per 1 USD, whichever direction the conversion ran.
    if src == "USD":
        rate = float(to_amount) / float(from_amount)
    else:
        rate = float(from_amount) / float(to_amount)

    cid = uuid.uuid4().hex

    # The spread the user actually paid, against the mid-market rate that day.
    # Both sides valued in won at the market rate: what you handed over, minus
    # what you got back. Positive means the conversion cost you. Over a year of
    # conversions this is not small, and it exists nowhere else — a rate fetched
    # from the market instead of taken from the statement would erase it.
    spread = None
    if market_rate:
        def in_krw(amount: float, ccy: str) -> float:
            return amount * float(market_rate) if ccy == "USD" else amount

        spread = round(in_krw(from_amount, src) - in_krw(to_amount, dst), 4)

    # Converting BACK to base currency realizes the exchange-rate gain or loss on
    # the money being converted, against the average rate it was acquired at.
    # Measured BEFORE the legs are written, or the outflow would move the very
    # average it is being measured against.
    realized_fx = None
    if dst == BASE_CURRENCY:
        acquired_at = effective_acquisition_rate(src, as_of=occurred_at)
        if acquired_at:
            realized_fx = round(from_amount * (rate - acquired_at), 4)

    def _write(c):
        out = record_flow("fx_out", src, from_amount, occurred_at,
                          fx_to_krw=rate, conversion_id=cid, note=note, conn=c)
        inn = record_flow("fx_in", dst, to_amount, occurred_at,
                          fx_to_krw=rate, conversion_id=cid, note=note, conn=c)
        if market_rate is not None or realized_fx is not None:
            c.execute(
                "UPDATE cash_flows SET market_rate = ?, realized_fx_pnl_krw = ?"
                " WHERE id = ?",
                (market_rate, realized_fx, inn["id"]),
            )
            inn["market_rate"] = market_rate
            inn["realized_fx_pnl_krw"] = realized_fx
        return {
            "conversion_id": cid,
            "rate": rate,
            "market_rate": market_rate,
            "spread_krw": spread,
            # Decision support, not a tax figure: Korean tax treatment of
            # overseas gains is FIFO-based and this is average-cost.
            "realized_fx_pnl_krw": realized_fx,
            "out": out,
            "in": inn,
        }

    if conn is not None:
        result = _write(conn)
    else:
        with db.transaction() as c:
            result = _write(c)

    logger.info(
        f"[cash] converted {from_amount} {src} -> {to_amount} {dst} "
        f"at {rate:.4f} (market {market_rate}, realized fx {realized_fx}) "
        f"(conversion {cid})"
    )
    return result


async def convert_auto(
    from_currency: str,
    from_amount: float,
    to_currency: str,
    to_amount: float,
    occurred_at: str,
    *,
    note: str | None = None,
) -> dict:
    """:func:`convert`, looking up the mid-market rate so the spread is recorded."""
    market_rate = None
    try:
        market_rate = await resolve_rate("USD", occurred_at)
    except CashError as e:
        # The conversion itself is fully specified by the two amounts, so a
        # missing market rate costs only the spread figure. Unlike a flow's own
        # rate, this one is not load-bearing.
        logger.warning(f"[cash] no market rate for conversion spread: {e}")
    return convert(from_currency, from_amount, to_currency, to_amount,
                   occurred_at, market_rate=market_rate, note=note)


# =============================================================================
# Reading
# =============================================================================

def balances(as_of: str | None = None) -> dict[str, float]:
    """
    One balance per supported currency, derived by replaying the ledger.

    ``as_of`` filters to flows that had occurred by that instant, which phase 4
    needs to rebuild a net-worth series day by day. ISO-8601 sorts
    lexicographically in the same order it sorts chronologically, the property
    ``list_trades`` already relies on.

    Every supported currency is present in the result, at 0.0 when untouched, so
    callers never have to distinguish "no balance" from "no key".
    """
    sql = "SELECT currency, SUM(amount) AS total FROM cash_flows"
    params: list = []
    if as_of:
        sql += " WHERE occurred_at <= ?"
        params.append(as_of)
    sql += " GROUP BY currency"

    out = {c: 0.0 for c in SUPPORTED_CURRENCIES}
    for r in db.get_connection().execute(sql, params).fetchall():
        out[r["currency"]] = round(float(r["total"] or 0.0), 6)
    return out


def balance(currency: str, as_of: str | None = None) -> float:
    """One currency's balance. See :func:`balances`."""
    return balances(as_of).get(normalize_currency(currency), 0.0)


def get_flow(flow_id: int) -> dict | None:
    return _row(db.get_connection().execute(
        "SELECT * FROM cash_flows WHERE id = ?", (int(flow_id),)
    ).fetchone())


def list_flows(
    currency: str | None = None,
    flow_type: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """
    The ledger, newest first.

    Ordered by ``occurred_at`` (when the money moved) rather than ``created_at``
    (when it was typed in), so a back-filled flow lands where it belongs — the
    same choice ``list_trades`` makes.
    """
    sql = "SELECT * FROM cash_flows"
    where, params = [], []
    if currency:
        where.append("currency = ?")
        params.append(normalize_currency(currency))
    if flow_type:
        where.append("flow_type = ?")
        params.append(flow_type.strip().lower())
    if since:
        where.append("occurred_at >= ?")
        params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def external_flows(since: str | None = None) -> list[dict]:
    """
    Deposits and withdrawals only — money crossing the portfolio boundary.

    Phase 4's time-weighted return breaks its series at exactly these, and at
    nothing else. A conversion or a trade appearing here would show up as
    performance the user never earned.
    """
    placeholders = ",".join("?" for _ in EXTERNAL_FLOWS)
    sql = f"SELECT * FROM cash_flows WHERE flow_type IN ({placeholders})"
    params: list = sorted(EXTERNAL_FLOWS)
    if since:
        sql += " AND occurred_at >= ?"
        params.append(since)
    sql += " ORDER BY occurred_at ASC, id ASC"
    return [dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def flows_for_trade(trade_id: int) -> list[dict]:
    """Every cash movement belonging to one trade — its fill, plus fees and tax."""
    return [dict(r) for r in db.get_connection().execute(
        "SELECT * FROM cash_flows WHERE trade_id = ? ORDER BY id",
        (int(trade_id),),
    ).fetchall()]


def is_initialized() -> bool:
    """
    Whether the user has recorded an **opening balance**.

    Deliberately narrower than "the ledger has rows in it". Once trades write
    their own cash legs, a portfolio can hold flows without the user ever having
    said how much cash they started with — and that is exactly when the setup
    prompt still needs to appear.
    """
    row = db.get_connection().execute(
        "SELECT 1 FROM cash_flows WHERE flow_type = 'deposit' AND note = ? LIMIT 1",
        (OPENING_NOTE,),
    ).fetchone()
    return row is not None


def funded_trade_ids() -> set[int]:
    """Trades that already have a cash leg, so they are not funded twice."""
    rows = db.get_connection().execute(
        "SELECT DISTINCT trade_id FROM cash_flows WHERE trade_id IS NOT NULL"
    ).fetchall()
    return {r["trade_id"] for r in rows}


# =============================================================================
# Rate resolution (phase 2)
# =============================================================================
# `record_flow` stays synchronous so it can run inside a caller's open
# transaction — phase 3 writes a trade and its cash leg together that way, and an
# `await` in the middle of a held write lock is not something to introduce. So
# the network hop lives here, in async wrappers that resolve a rate *before* the
# transaction opens.

async def resolve_rate(currency: str, occurred_at: str) -> float:
    """
    The USDKRW rate that applied when a flow occurred.

    Raises on failure rather than substituting a default. This is the
    **recording** path, where an unavailable rate must be fatal: ``fx_to_krw`` is
    ``NOT NULL`` so that a guess cannot be written, and a wrong rate corrupts a
    cost basis permanently — unlike a display, which can simply show a dash and
    recover on the next render.
    """
    ccy = normalize_currency(currency)
    if ccy == BASE_CURRENCY:
        return 1.0

    from providers import fx_provider   # local import: keeps startup light

    try:
        when = datetime.fromisoformat((occurred_at or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise InvalidFlow(
            f"occurred_at must be an ISO-8601 datetime (got {occurred_at!r})."
        )
    try:
        quote = await fx_provider.fetch_rate_at(when)
    except Exception as e:  # noqa: BLE001 — surface as a domain error
        raise InvalidFlow(
            f"Could not resolve a {ccy}/{BASE_CURRENCY} rate for "
            f"{occurred_at}: {e}. Supply the rate from your statement to record "
            f"this flow."
        )
    return quote.rate


async def record_flow_auto(
    flow_type: str,
    currency: str,
    amount: float,
    occurred_at: str,
    *,
    fx_to_krw: float | None = None,
    trade_id: int | None = None,
    conversion_id: str | None = None,
    note: str | None = None,
) -> dict:
    """
    :func:`record_flow`, resolving the exchange rate when the caller has none.

    The parameter is kept rather than always resolving: a user reconciling
    against a broker statement has a more authoritative rate than a daily close,
    including the spread they actually paid.
    """
    rate = fx_to_krw
    if rate is None:
        rate = await resolve_rate(currency, occurred_at)
    return record_flow(
        flow_type, currency, amount, occurred_at,
        fx_to_krw=rate, trade_id=trade_id,
        conversion_id=conversion_id, note=note,
    )


async def backfill_fx() -> dict:
    """
    Fill the exchange-rate columns that were written but never populated.

    ``trades.fx_rate`` and ``holdings.initial_fx_rate`` have been null (or 1.0)
    on every row since they were added — the app stored them and read them
    nowhere. This resolves each null from the row's own timestamp.

    **Unresolved rows are reported by count, never defaulted to 1.0.** A stored
    ``fx_rate`` of 1.0 on a USD row silently asserts that one dollar is one won,
    and every figure derived from it would be wrong by a factor of ~1,370 with
    nothing to indicate it.
    """
    from services import portfolio_service as ps

    conn = db.get_connection()
    filled = unresolved = skipped = 0
    problems: list[str] = []

    trades = conn.execute(
        "SELECT id, ticker, executed_at FROM trades WHERE fx_rate IS NULL"
    ).fetchall()
    for t in trades:
        ccy = ps.resolve_asset_currency(t["ticker"])
        if ccy == BASE_CURRENCY:
            # A won-denominated trade needs no conversion; 1.0 is the true rate.
            with db.transaction() as c:
                c.execute("UPDATE trades SET fx_rate = 1.0 WHERE id = ?", (t["id"],))
            filled += 1
            continue
        try:
            rate = await resolve_rate(ccy, t["executed_at"])
        except CashError as e:
            unresolved += 1
            problems.append(f"trade {t['id']} ({t['ticker']}): {e}")
            continue
        with db.transaction() as c:
            c.execute("UPDATE trades SET fx_rate = ? WHERE id = ?", (rate, t["id"]))
        filled += 1

    holdings = conn.execute(
        "SELECT id, ticker, currency, created_at FROM holdings"
        " WHERE initial_fx_rate IS NULL"
    ).fetchall()
    for h in holdings:
        ccy = (h["currency"] or "").strip().upper() or ps.resolve_asset_currency(h["ticker"])
        if ccy == BASE_CURRENCY:
            with db.transaction() as c:
                c.execute("UPDATE holdings SET initial_fx_rate = 1.0 WHERE id = ?",
                          (h["id"],))
            filled += 1
            continue
        try:
            rate = await resolve_rate(ccy, h["created_at"])
        except CashError as e:
            unresolved += 1
            problems.append(f"holding {h['ticker']}: {e}")
            continue
        with db.transaction() as c:
            c.execute("UPDATE holdings SET initial_fx_rate = ? WHERE id = ?",
                      (rate, h["id"]))
        filled += 1

    result = {
        "filled": filled,
        "unresolved": unresolved,
        "skipped": skipped,
        "problems": problems[:20],
    }
    logger.info(f"[cash] fx backfill: {filled} filled, {unresolved} unresolved")
    return result


# =============================================================================
# Opening anchor
# =============================================================================

def opening_timestamp() -> str | None:
    """
    When the opening anchor was taken, or ``None`` if none has been recorded.

    **The anchor describes the whole state at that instant** — the cash held
    *and* the positions held. That is what makes it usable at all: the user knows
    what they have today, not the cash-flow history that produced it.

    The consequence matters and is enforced in ``portfolio_service.record_trade``:
    a trade dated **before** the anchor moves no cash, because its effect is
    already inside the anchor's balance. Letting it move cash as well would
    double-count it — the balance would drop by money the anchor had already
    netted out, and the average acquisition rate would be computed against a
    balance the user never held.
    """
    row = db.get_connection().execute(
        "SELECT MIN(occurred_at) AS t FROM cash_flows"
        " WHERE flow_type = 'deposit' AND note = ?",
        (OPENING_NOTE,),
    ).fetchone()
    return row["t"] if row and row["t"] else None


def initialize_ledger(
    opening: dict[str, float],
    fx_to_krw: float | None = None,
    occurred_at: str | None = None,
) -> dict:
    """
    Record the starting point of the ledger.

    Existing users already hold seeded positions whose ``OPENING_RATIONALE``
    trades were never funded; replaying that ledger would yield a large negative
    balance. So initialization writes **both sides** in one transaction:

      * one ``deposit`` per currency for the cash the user says they hold now;
      * one ``deposit`` per seeded holding for its cost basis, in that holding's
        own currency, marked :data:`SEED_FUNDING_NOTE`;
      * one ``buy`` per seeded holding, linked to its opening trade.

    Net effect: each currency's balance equals exactly what the user entered,
    every position is funded, and net worth at setup is opening cash plus seeded
    cost basis.

    Like ``OPENING_RATIONALE``, this is explicitly an **anchor** and not a claim
    about real history — the acquisition dates are unknown, which is why those
    positions were seeded rather than logged.
    """
    from services import portfolio_service as ps   # local: avoids an import cycle

    if is_initialized():
        raise LedgerAlreadyInitialized(
            "An opening balance has already been recorded. Add a deposit or an "
            "adjustment instead of re-initializing."
        )

    when = occurred_at or db.utc_now_iso()
    amounts = {normalize_currency(c): float(a) for c, a in (opening or {}).items()}
    for c, a in amounts.items():
        if a < 0:
            raise InvalidFlow(f"Opening {c} balance cannot be negative ({a}).")
    if any(c != BASE_CURRENCY for c in amounts) and fx_to_krw is None:
        raise InvalidFlow(
            "An exchange rate is required to record a non-KRW opening balance."
        )

    holdings = ps.list_holdings()
    written: list[dict] = []

    with db.transaction() as conn:
        # 1. The cash the user actually holds.
        for ccy, amount in sorted(amounts.items()):
            if amount <= 0:
                continue
            written.append(record_flow(
                "deposit", ccy, amount, when,
                fx_to_krw=fx_to_krw, note=OPENING_NOTE, conn=conn,
            ))

        # 2. Fund each seeded position, then spend that funding on it, so the
        #    books balance and the opening cash is untouched by the seeding.
        #
        #    Skipped for positions whose opening trade already has a cash leg:
        #    once `add_holding` funds its own seed, a position added after the
        #    ledger was opened must not be funded a second time here.
        already_funded = funded_trade_ids()
        opening_trades = {
            t["ticker"]: t
            for t in ps.list_trades()
            if ps.is_opening_entry(t)
        }
        for h in holdings:
            trade = opening_trades.get(h["ticker"])
            if trade and trade["id"] in already_funded:
                continue
            cost = float(h["quantity"]) * float(h["avg_price"])
            if cost <= 0:
                continue
            ccy = normalize_currency(h.get("currency") or "USD")
            rate = 1.0 if ccy == BASE_CURRENCY else (
                h.get("initial_fx_rate") or fx_to_krw
            )
            if ccy != BASE_CURRENCY and rate is None:
                raise InvalidFlow(
                    f"An exchange rate is required to fund the seeded "
                    f"{ccy} position in {h['ticker']}."
                )
            written.append(record_flow(
                "deposit", ccy, cost, when,
                fx_to_krw=rate, note=SEED_FUNDING_NOTE, conn=conn,
            ))
            written.append(record_flow(
                "buy", ccy, cost, when,
                fx_to_krw=rate, trade_id=(trade or {}).get("id"),
                note=SEED_FUNDING_NOTE, conn=conn,
            ))

    result = {
        "balances": balances(),
        "flows_written": len(written),
        "holdings_funded": len(holdings),
    }
    logger.info(
        f"[cash] ledger initialized: {result['balances']}, "
        f"{len(holdings)} holding(s) funded"
    )
    return result
