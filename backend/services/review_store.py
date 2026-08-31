"""
services.review_store
─────────────────────
The repository for coaching reviews — the **only** module that writes SQL against
``coach_reviews``.

Why reviews are persisted at all
────────────────────────────────
Before this module every review was generated, rendered once, and thrown away.
Three things were impossible as a result:

  1. **Coaching a trade after it was logged.** `POST /coach/review` was pre-trade
     only, so a rationale written without first asking for a review got no
     feedback ever — which is every trade logged in a hurry, i.e. the ones most
     worth reviewing.
  2. **The coach remembering what it had already said.** It repeated itself, and
     could never observe that a warning it gave was ignored.
  3. **The user reading back what they were told.** The single most useful thing
     a journal can show is the advice you got and what you then did.

Storage shape
─────────────
The full report is stored as JSON in one column rather than exploded into
columns. The report schema is still growing (this phase adds five fields to it),
and an old review has to stay readable after it grows again. Reads go back
through the Pydantic model, whose defaults absorb the difference.

``rationale_snapshot`` freezes the text that was actually judged. If the user
later edits a trade's rationale, the stored review must not appear to have
assessed words it never saw.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services import db

logger = logging.getLogger(__name__)


REVIEW_TYPES = ("pre_trade", "retrospective", "journal")


def _row_to_dict(row) -> dict | None:
    """Inflate a stored row, parsing the report JSON back into a dict."""
    if row is None:
        return None
    out = dict(row)
    raw = out.pop("report_json", None)
    try:
        out["report"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as e:
        # A corrupt row must not take down the journal view that lists it.
        logger.warning(f"[review_store] unreadable report_json on id={out.get('id')}: {e}")
        out["report"] = {}
    return out


def save_review(
    report: Any,
    review_type: str,
    *,
    trade_id: int | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    rationale_snapshot: str | None = None,
    data_as_of: str | None = None,
) -> dict:
    """
    Persist one review. ``report`` is a Pydantic report model.

    Records the model id alongside it: ``_DEFAULT_MODEL`` has already changed
    once in this project (``gemini-2.5-flash`` was retired for new API keys), and
    reviews written by different models should be distinguishable rather than
    silently comparable.

    Never raises — a storage failure must not fail the review that just
    succeeded. Returns the stored row, or a dict with ``id: None`` if the write
    could not be made.
    """
    if review_type not in REVIEW_TYPES:
        raise ValueError(f"review_type must be one of {REVIEW_TYPES}, got {review_type!r}")

    try:
        from gemini_chat import _model_name
        model = _model_name()
    except Exception:  # noqa: BLE001 — the model id is metadata, not the point
        model = None

    payload = (
        report.model_dump(mode="json")
        if hasattr(report, "model_dump")
        else dict(report)
    )

    try:
        now = db.utc_now_iso()
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO coach_reviews (review_type, trade_id, ticker, scope,"
                " rationale_snapshot, report_json, model, data_as_of, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    review_type,
                    trade_id,
                    (ticker or "").strip().upper() or None,
                    scope,
                    rationale_snapshot,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    model,
                    data_as_of,
                    now,
                ),
            )
            review_id = cur.lastrowid
        logger.info(
            f"[review_store] saved {review_type} review id={review_id} "
            f"trade_id={trade_id} ticker={ticker}"
        )
        return get_review(review_id) or {"id": review_id}
    except Exception as e:  # noqa: BLE001
        logger.error(f"[review_store] failed to save {review_type} review: {e}")
        return {"id": None, "review_type": review_type, "report": payload}


def get_review(review_id: int) -> dict | None:
    row = db.get_connection().execute(
        "SELECT * FROM coach_reviews WHERE id = ?", (review_id,)
    ).fetchone()
    return _row_to_dict(row)


def reviews_for_trade(trade_id: int) -> list[dict]:
    """
    Every review of one trade, newest first.

    A trade can legitimately carry several: reviewing it at 7 days and again at
    90 days are different judgements, and where they diverge is informative. A
    new review never replaces an old one.
    """
    rows = db.get_connection().execute(
        "SELECT * FROM coach_reviews WHERE trade_id = ?"
        " ORDER BY created_at DESC, id DESC",
        (int(trade_id),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_reviews(
    review_type: str | None = None,
    ticker: str | None = None,
    limit: int | None = 50,
) -> list[dict]:
    """Reviews, newest first, optionally filtered by type and/or company."""
    sql = "SELECT * FROM coach_reviews"
    where, params = [], []
    if review_type:
        where.append("review_type = ?")
        params.append(review_type)
    if ticker:
        where.append("ticker = ?")
        params.append(ticker.strip().upper())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def latest_review_per_trade(trade_ids: list[int] | None = None) -> dict[int, dict]:
    """
    Map ``trade_id -> most recent review``, for badging a journal listing in one
    query rather than one per row.
    """
    sql = (
        "SELECT * FROM coach_reviews WHERE trade_id IS NOT NULL"
        " ORDER BY created_at ASC, id ASC"
    )
    rows = db.get_connection().execute(sql).fetchall()
    wanted = set(trade_ids) if trade_ids is not None else None
    out: dict[int, dict] = {}
    for r in rows:
        tid = r["trade_id"]
        if wanted is not None and tid not in wanted:
            continue
        out[tid] = _row_to_dict(r)   # ascending order leaves the newest last
    return out


def unreviewed_trades(limit: int | None = None) -> list[dict]:
    """
    Logged trades that have a rationale and no review — the backlog the user
    cannot currently see.

    Seeded opening entries are excluded: they carry a synthetic rationale
    (``OPENING_RATIONALE``) that records a setup anchor rather than a decision,
    so there is nothing in them to coach.
    """
    from services.portfolio_service import OPENING_RATIONALE

    sql = (
        "SELECT t.* FROM trades t"
        " LEFT JOIN coach_reviews r ON r.trade_id = t.id"
        " WHERE r.id IS NULL"
        "   AND t.entry_rationale IS NOT NULL"
        "   AND TRIM(t.entry_rationale) != ''"
        "   AND t.entry_rationale != ?"
        " ORDER BY t.executed_at DESC, t.id DESC"
    )
    params: list = [OPENING_RATIONALE]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def pending_count() -> int:
    """How many logged trades are still waiting for feedback."""
    return len(unreviewed_trades())
