"""
routers.coach
─────────────
The trading coach — blueprint §3.

  POST /coach/review                    review a trade the user is CONSIDERING
  POST /coach/review/trade/{trade_id}   review a trade already LOGGED
  POST /coach/review/journal            review the whole record at once
  GET  /coach/reviews                   past reviews, newest first
  GET  /coach/reviews/pending           logged trades still awaiting feedback
  GET  /coach/reviews/trade/{trade_id}  every review of one trade
  GET  /coach/edge-analytics            Expectancy/Payoff/MAE-MFE/rule candidates
  GET  /coach/rules                     the user's Golden/Toxic rules
  POST /coach/rules                     adopt a candidate or write a custom rule
  PATCH  /coach/rules/{id}              toggle a rule active/inactive
  DELETE /coach/rules/{id}              remove a rule

Why there are three review endpoints
────────────────────────────────────
The pre-trade review is the one that can still change a decision, so it stays the
primary path. But it used to be the *only* path: the moment the user pressed
"Log trade", that entry became permanently un-coachable, and any rationale
written without first asking for a review — every trade logged in a hurry, which
is to say the ones most worth reviewing — got no feedback ever.

The retrospective and journal endpoints close that gap, and every review from all
three is persisted, so the coach can see what it already said and the user can
read back what they were told.

The reviews draw on the last /analyze run (or, for a retrospective, the run that
existed at the time) plus the real trading journal, so they need no new fetching.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from gemini_chat import gemini_api_key
from agents import CoachAgent
from agents.schemas.coach import CoachReport, JournalReport
from schemas import (
    CoachReviewRequest,
    JournalReviewRequest,
    PendingReviewsResponse,
    RuleActiveUpdate,
    StoredReview,
    StoredReviewsResponse,
    Trade,
    TradingRule,
    TradingRuleCreate,
    TradingRulesResponse,
)
from services import journal_analysis as ja
from services import portfolio_service as ps
from services import review_store
from services import trading_rules as tr
from services.storage import DebateStore, get_debate_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coach", tags=["coach"])


def _require_key() -> None:
    """Every review needs the LLM; fail the same way on all three paths."""
    if not gemini_api_key():
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured on the server, so the "
                   "coach cannot run.",
        )


def _stored(row: dict) -> StoredReview:
    return StoredReview(**row)


# =============================================================================
# Reviewing
# =============================================================================

@router.post("/review", response_model=CoachReport)
async def review_trade(
    req: CoachReviewRequest,
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Hold the user's stated rationale against the objective data and their history,
    **before** they commit to the trade.

    The fundamental and technical reports come from that company's most recent
    ``POST /analyze`` run. If none has been run, the review still works — it
    falls back to the journal alone and says explicitly in ``data_limitations``
    what it could not see, rather than pretending to a fundamental view it does
    not have.
    """
    _require_key()

    ticker = (req.ticker or "").strip().upper() or None

    # Pull the two analytical pillars from the last analysis of THIS company.
    # `debate_store` is keyed by ticker, so no other company's view leaks in.
    sec_report = technical_report = None
    if ticker:
        record = debate_store.get(ticker) or {}
        reports = record.get("reports") or {}
        sec_report = reports.get("sec_filings")
        technical_report = reports.get("technical_analysis")

    try:
        report = await CoachAgent().analyze({
            "ticker": ticker,
            "entry_rationale": req.entry_rationale,
            "proposed_side": req.proposed_side,
            "proposed_quantity": req.proposed_quantity,
            "emotion_tag": req.emotion_tag,
            "sec_report": sec_report,
            "technical_report": technical_report,
        })
    except Exception as e:  # noqa: BLE001 — surface a clean message, not a 500
        logger.error(f"Coach review failed: {e}")
        raise HTTPException(status_code=502, detail=f"Coach review failed: {e}")

    # Be explicit about a missing pillar: silence here would read as "the coach
    # considered the fundamentals and had no concerns".
    if ticker and sec_report is None and technical_report is None:
        report.data_limitations.append(
            f"No analysis has been run for {ticker}, so this review is based on "
            f"your trading journal alone — not on the company's fundamentals or "
            f"price action. Run a Deep Analysis for a fuller picture."
        )

    # Persisted even though this trade may never be logged: a warning that was
    # given and then ignored is one of the most informative records the journal
    # can hold, and the journal review reads exactly that.
    review_store.save_review(
        report, "pre_trade",
        ticker=ticker,
        rationale_snapshot=req.entry_rationale,
    )
    return report


@router.post("/review/trade/{trade_id}", response_model=CoachReport)
async def review_logged_trade(trade_id: int):
    """
    Review a decision the user has **already made**.

    Judged in two passes: the reasoning is scored first, against only the data
    that existed at the trade's timestamp, and the outcome is described second
    without being allowed to revise that score. A trade can be well reasoned and
    still lose money, and a coach that cannot say so teaches outcome-chasing.

    Reviewing an already-reviewed trade adds a **new** review rather than
    replacing the old one — a verdict at 7 days and another at 90 days are both
    legitimate, and where they differ is worth keeping.
    """
    _require_key()

    trade = ps.get_trade(trade_id)
    if trade is None:
        raise HTTPException(
            status_code=404, detail=f"No journal entry with id {trade_id}."
        )
    if ps.is_opening_entry(trade):
        raise HTTPException(
            status_code=400,
            detail="This entry is a position seeded at portfolio setup, not a "
                   "decision the user reasoned about — there is nothing to coach.",
        )

    try:
        report = await CoachAgent().analyze_retrospective(trade_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Retrospective review failed for trade {trade_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Coach review failed: {e}")

    review_store.save_review(
        report, "retrospective",
        trade_id=trade_id,
        ticker=trade.get("ticker"),
        rationale_snapshot=trade.get("entry_rationale"),
        # Null means no analysis existed at the trade's timestamp — never that
        # the current one was quietly substituted.
        data_as_of=report.data_as_of,
    )
    return report


@router.post("/review/journal", response_model=JournalReport)
async def review_journal(req: JournalReviewRequest):
    """
    Review the user's whole record rather than one decision.

    Answers what no single-trade review can: which behaviours actually recur,
    whether well-reasoned trades have actually done better, and what earlier
    coaching was given and then ignored.
    """
    _require_key()

    try:
        report = await CoachAgent().analyze_journal({
            "ticker": req.ticker,
            "since": req.since,
            "limit": req.limit,
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"Journal review failed: {e}")
        raise HTTPException(status_code=502, detail=f"Journal review failed: {e}")

    review_store.save_review(
        report, "journal",
        ticker=req.ticker,
        scope=report.scope_description,
    )
    return report


# =============================================================================
# Reading what the coach has already said
# =============================================================================

@router.get("/reviews", response_model=StoredReviewsResponse)
async def list_reviews(
    review_type: str | None = Query(
        None, description="'pre_trade', 'retrospective', or 'journal'"
    ),
    ticker: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """Past reviews, newest first — what the user was told, and when."""
    rows = review_store.list_reviews(
        review_type=review_type, ticker=ticker, limit=limit
    )
    return StoredReviewsResponse(
        reviews=[_stored(r) for r in rows], count=len(rows)
    )


@router.get("/reviews/pending", response_model=PendingReviewsResponse)
async def pending_reviews(limit: int | None = Query(None, ge=1, le=500)):
    """
    Logged trades that carry a rationale and have never been reviewed.

    This is the backlog the user could not previously see: entries they wrote a
    reason for, submitted, and got nothing back on. Seeded opening positions are
    excluded — their rationale is a setup marker, not a decision.
    """
    rows = review_store.unreviewed_trades(limit=limit)
    return PendingReviewsResponse(
        trades=[Trade(**r) for r in rows], count=len(rows)
    )


@router.get("/reviews/trade/{trade_id}", response_model=StoredReviewsResponse)
async def reviews_for_trade(trade_id: int):
    """
    Every review of one trade, newest first.

    More than one is normal and is not a duplicate: the same decision judged
    after 7 days and after 90 days can reasonably reach different conclusions.
    """
    rows = review_store.reviews_for_trade(trade_id)
    return StoredReviewsResponse(
        reviews=[_stored(r) for r in rows], count=len(rows)
    )


# =============================================================================
# Personal Trading Edge — quantitative analytics + the rules playbook
# =============================================================================

@router.get("/edge-analytics")
async def edge_analytics(ticker: str | None = Query(None, description="Scope to one ticker")):
    """
    Expectancy, Payoff Ratio, and win rate — overall and segmented by rationale
    type, strategy type, and emotion tag — plus the Disposition Effect, an
    empirical MAE/MFE-derived stop-loss, and Golden/Toxic rule candidates.

    Computed entirely from REALIZED P/L on closed round trips (never the
    30-day-price-outcome proxy the rest of the coach uses), in KRW, so a USD
    and a KRW trade are never silently averaged together. Below
    ``journal_analysis.MIN_TRADES_FOR_PATTERN`` closed trips this returns a
    well-formed but empty result with ``sufficient: False``.
    """
    try:
        return await ja.edge_analytics(ticker=ticker)
    except Exception as e:  # noqa: BLE001 — a stats failure is not a 500
        logger.error(f"Edge analytics failed: {e}")
        raise HTTPException(status_code=502, detail=f"Edge analytics failed: {e}")


@router.get("/rules", response_model=TradingRulesResponse)
async def list_rules(
    rule_type: str | None = Query(None, description="'golden', 'toxic', or 'custom'"),
    active_only: bool = Query(False),
):
    """The user's Golden Setup / Toxic Pattern / custom rules, newest first."""
    rows = tr.list_rules(rule_type=rule_type, active_only=active_only)
    return TradingRulesResponse(rules=[TradingRule(**r) for r in rows], count=len(rows))


@router.post("/rules", response_model=TradingRule, status_code=201)
async def create_rule(body: TradingRuleCreate):
    """
    Adopt a Golden/Toxic candidate from ``GET /coach/edge-analytics``, or write
    a custom rule. A candidate is only a hypothesis until this makes it active
    — the pre-trade review checks a proposed trade against active rules only.
    """
    try:
        row = tr.create_rule(
            body.rule_type, body.title, body.conditions, body.description,
            win_rate=body.win_rate, payoff_ratio=body.payoff_ratio,
            expectancy=body.expectancy,
        )
    except tr.RuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TradingRule(**row)


@router.patch("/rules/{rule_id}", response_model=TradingRule)
async def update_rule_active(rule_id: int, body: RuleActiveUpdate):
    """Toggle a rule active/inactive — the playbook's toggle switch."""
    try:
        row = tr.set_active(rule_id, body.is_active)
    except tr.RuleError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return TradingRule(**row)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int):
    """Remove a rule permanently."""
    try:
        tr.delete_rule(rule_id)
    except tr.RuleError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return None
