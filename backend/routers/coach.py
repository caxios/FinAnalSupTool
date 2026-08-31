"""
routers.coach
─────────────
The trading coach — blueprint §3.

  POST /coach/review — review a trade the user is CONSIDERING, before they
                       commit to it.

Pre-trade rather than post-trade by design. A coach that only tells you what you
did wrong afterwards is a scorekeeper; the moment coaching can actually change a
decision is while the user is still writing their rationale and has not yet
clicked "Log trade".

The review draws on the last /analyze run for that company (the fundamental and
technical reports) plus the real trading journal, so it needs no new fetching.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from gemini_chat import gemini_api_key
from agents import CoachAgent
from agents.schemas.coach import CoachReport
from schemas import CoachReviewRequest
from services.storage import DebateStore, get_debate_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coach", tags=["coach"])


@router.post("/review", response_model=CoachReport)
async def review_trade(
    req: CoachReviewRequest,
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Hold the user's stated rationale against the objective data and their history.

    The fundamental and technical reports come from that company's most recent
    ``POST /analyze`` run. If none has been run, the review still works — it
    falls back to the journal alone and says explicitly in ``data_limitations``
    what it could not see, rather than pretending to a fundamental view it does
    not have.
    """
    if not gemini_api_key():
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY is not configured on the server, so the "
                   "coach cannot run.",
        )

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
    return report
