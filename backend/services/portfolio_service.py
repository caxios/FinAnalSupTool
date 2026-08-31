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
    currency: str = "USD",
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
    """
    t = _require_ticker(ticker)
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
             (currency or "USD").upper(), now, now),
        )
        conn.execute(
            "INSERT INTO trades (ticker, side, quantity, executed_at,"
            " execution_price, total_value, fx_rate, entry_rationale,"
            " avg_price_after, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, "buy", float(quantity), now, float(avg_price),
             float(avg_price) * float(quantity) * float(initial_fx_rate or 1.0),
             initial_fx_rate, OPENING_RATIONALE, float(avg_price), now),
        )
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


def record_trade(
    ticker: str,
    side: str,
    quantity: float,
    executed_at: str,
    entry_rationale: str | None = None,
    execution_price: float | None = None,
    fx_rate: float | None = None,
) -> dict:
    """
    Log a trade and update the position, atomically.

    ``execution_price`` is optional here because phase 3 derives it from
    intraday market data given ``executed_at``; until then a caller may pass it
    explicitly. When it is ``None`` the trade is still recorded (the journal
    entry and its rationale are the point), and the average is left unchanged —
    a buy at an unknown price cannot move a cost basis.

    Returns the inserted trade row, including the server-computed
    ``total_value`` and ``avg_price_after``.
    """
    t = _require_ticker(ticker)
    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise InvalidTrade(f"side must be 'buy' or 'sell' (got {side!r}).")
    if quantity is None or quantity <= 0:
        raise InvalidTrade("quantity must be greater than zero.")

    price = float(execution_price) if execution_price is not None else None
    total_value = None if price is None else price * float(quantity) * float(fx_rate or 1.0)

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
                (t, new_qty, new_avg, "USD", now, now),
            )
        else:
            conn.execute(
                "UPDATE holdings SET quantity = ?, avg_price = ?, updated_at = ?"
                " WHERE ticker = ?",
                (new_qty, new_avg, now, t),
            )

        cur = conn.execute(
            "INSERT INTO trades (ticker, side, quantity, executed_at,"
            " execution_price, total_value, fx_rate, entry_rationale,"
            " avg_price_after, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, side, float(quantity), executed_at, price, total_value,
             fx_rate, entry_rationale, new_avg, now),
        )
        trade_id = cur.lastrowid

    logger.info(
        f"[portfolio] {side} {quantity} {t} @ {price} → "
        f"qty {new_qty}, avg {new_avg}"
    )
    row = db.get_connection().execute(
        "SELECT * FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    return dict(row)


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



# =============================================================================
# Valuation (phase 3)
# =============================================================================

async def value_holdings(rows: list[dict] | None = None) -> tuple[list[dict], dict]:
    """
    Attach live market value and unrealized ROI to each holding.

    Returns ``(holdings, totals)``. Prices are fetched concurrently and
    best-effort: a ticker whose lookup fails keeps ``current_price = None``
    rather than failing the request, so one delisted symbol cannot blank the
    whole portfolio. Such a row is excluded from the totals — averaging it in as
    zero would understate the portfolio, which is worse than reporting less.
    """
    from providers import price_provider   # local import: keeps startup light

    rows = list_holdings() if rows is None else rows
    prices = await price_provider.fetch_current_prices([r["ticker"] for r in rows])

    valued: list[dict] = []
    total_cost = total_value = 0.0
    priced_cost = 0.0          # cost basis of the rows we could actually price

    for r in rows:
        row = dict(r)
        qty, avg = float(row["quantity"]), float(row["avg_price"])
        cost = qty * avg
        total_cost += cost

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
            total_value += market_value
            priced_cost += cost
        valued.append(row)

    any_priced = priced_cost > 0
    totals = {
        "total_cost_basis": round(total_cost, 4),
        "total_market_value": round(total_value, 4) if any_priced else None,
        "total_unrealized_pnl": round(total_value - priced_cost, 4) if any_priced else None,
        "total_roi": round((total_value - priced_cost) / priced_cost, 6) if any_priced else None,
    }
    return valued, totals


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


async def fetch_baseline(ticker: str, store) -> dict:
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
    }
    logger.info(f"[portfolio] baseline for {t}: {state} ({total_ingested} filings)")
    return _baseline_status[t]


def trigger_baseline_if_new(ticker: str, store) -> bool:
    """
    Kick off :func:`fetch_baseline` in the background when a ticker has no
    filings yet. Returns whether a fetch was started.

    Detached via ``asyncio.create_task`` rather than awaited: SEC rendering is
    sequential and rate-limited, so a 2-year fetch takes far longer than an HTTP
    request should. The caller returns immediately and the UI polls the status.
    """
    t = normalize_ticker(ticker)
    if store.has_company(t):
        logger.info(f"[portfolio] {t} already has filings — skipping baseline.")
        return False
    if _baseline_status.get(t, {}).get("state") == "running":
        return False

    _baseline_status[t] = {
        "state": "queued", "message": "Baseline fetch queued.", "ingested": 0,
    }
    asyncio.create_task(fetch_baseline(t, store))
    return True
