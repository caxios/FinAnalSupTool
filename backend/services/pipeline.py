"""
services.pipeline
──────────────────
The three-phase Multi-Agent System (MAS) analysis orchestration, extracted from
the HTTP layer so the routers stay thin and the logic lives in exactly one place.

  Phase 1 — Independent analysis: the field agents run concurrently, capped at
            two Gemini calls at a time (avoids 429s). The portfolio-wide Quant
            Risk agent then runs, since it needs Phase 1's macro regime read.
  Phase 2 — True sequential debate over the agents that reported.
  Phase 3 — Manager synthesis + programmatic 3-axis gap scoring, then the run is
            persisted to history.

``analyze_pipeline`` is an async generator of progress events; both POST /analyze
(drains to the final result) and POST /analyze/stream (relays every event as SSE)
build on it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from schemas import AnalyzeRequest
from gemini_chat import gemini_api_key
from rag import compute_three_axis_scores, history_store
from agents import (
    SECFilingsAgent,
    TechnicalAnalysisAgent,
    EarningsCallAgent,
    CompanyNewsAgent,
    MacroMarketAgent,
    YouTubeAgent,
    MacroHistoryAgent,
    QuantRiskAgent,
    ManagerAgent,
    DebateTranscript,
    run_sequential_debate,
    FIELD_AGENT_IDS,
)
from services.storage import DocumentStore, DebateStore
from services import company_service, portfolio_service

logger = logging.getLogger(__name__)


# Default window when the caller doesn't specify one: a trailing ~18 months.
# ~550 calendar days yields ~380 trading days (enough for a reliable SMA200) and
# spans 6 quarters of earnings calls.
_DEFAULT_WINDOW_DAYS = 550


def analysis_window(request: AnalyzeRequest | None = None) -> tuple[str, str]:
    """
    Resolve the analysis period that drives EVERY agent's data fetching.

    Uses the caller's start/end when given, else falls back to the trailing
    default window. Raises HTTPException(400) on a malformed or inverted range.
    """
    def parse(value: str, field: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be a YYYY-MM-DD date (got '{value}').",
            )

    today = datetime.now(timezone.utc).date()
    end = parse(request.end_date, "end_date") if (request and request.end_date) else today
    start = (
        parse(request.start_date, "start_date")
        if (request and request.start_date)
        else end - timedelta(days=_DEFAULT_WINDOW_DAYS)
    )

    if start > end:
        raise HTTPException(
            status_code=400,
            detail=f"start_date ({start}) must not be after end_date ({end}).",
        )
    return start.isoformat(), end.isoformat()


def analyze_preconditions(doc_store: DocumentStore, ticker: str) -> None:
    """
    Guardrails shared by the sync and streaming analyze endpoints.

    Verifies the requested company actually has filings ingested — analyzing a
    ticker the user never uploaded would otherwise run six agents over an empty
    store and produce a confidently empty report.
    """
    if not ticker or not ticker.strip():
        raise HTTPException(
            status_code=400,
            detail="A ticker is required: it selects which company to analyze.",
        )
    if not doc_store.has_company(ticker):
        available = doc_store.list_tickers()
        raise HTTPException(
            status_code=404,
            detail=f"No filings uploaded for '{ticker}'. "
                   f"Available: {available or '(none)'}.",
        )
    if not doc_store.get_company_store(ticker).filing_meta:
        raise HTTPException(
            status_code=404, detail=f"No filings uploaded yet for '{ticker}'."
        )
    if not gemini_api_key():
        raise HTTPException(
            status_code=503,
            detail="Analysis is not configured: GEMINI_API_KEY is not set on "
                   "the backend. Set it and restart to enable the agents.",
        )


async def analyze_pipeline(
    request: AnalyzeRequest,
    doc_store: DocumentStore,
    debate_store: DebateStore,
):
    """
    The full three-phase MAS pipeline, as an async GENERATOR of progress events.

    Reads ONLY ``request.ticker``'s company store, so a session holding several
    companies analyzes exactly the one asked for.

    The last event is ``{"status": "complete", "result": <full payload>}``.
    """
    start_date, end_date = analysis_window(request)
    # Unique id for this run — scopes the earnings RAG index so runs don't mix.
    analysis_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]

    # The request names WHICH company to analyze; everything below reads from
    # that company's isolated store, so no other company's filings are in scope.
    requested_ticker = request.ticker.strip().upper()
    company_store = doc_store.get_company_store(requested_ticker)

    primary = company_service.primary_company(company_store)
    # Prefer the ticker resolved from the filings themselves; fall back to the
    # requested one (e.g. filings ingested without a CIK match).
    ticker = (primary.ticker if primary and primary.ticker else requested_ticker)
    company_name = primary.name if primary else None
    # Every agent except the SEC one works from the company identity; the shared
    # payload keeps their contexts identical.
    company_ctx = {
        "company": company_name or "",
        "ticker": ticker,
        "start_date": start_date,
        "end_date": end_date,
        "run_id": analysis_id,
    }

    # Each runnable agent gets a fresh `capture` dict; the agent writes its
    # assembled RAW-DATA prompt there, so Phase 2 (debate) and the isolated
    # per-agent chat can reuse the primary evidence without re-fetching it.
    captures: dict[str, dict] = {}

    def _cap(agent_id: str) -> dict:
        c: dict = {}
        captures[agent_id] = c
        return c

    # (agent_id, coroutine | skip-reason). A skip reason means the agent's
    # prerequisites are missing, so we report it without spending an LLM call.
    planned: list[tuple[str, object]] = [
        ("sec_filings", SECFilingsAgent().analyze(
            {
                "merged_tables": company_store.merged_tables,
                "text_store": company_store.text_store,
                "filing_meta": company_store.filing_meta,
                # For scoping the filing-text RAG index/retrieval to this run.
                "ticker": ticker,
                "run_id": analysis_id,
            },
            capture=_cap("sec_filings"),
        )),
        ("technical_analysis",
         TechnicalAnalysisAgent().analyze(
             {"ticker": ticker, "start_date": start_date, "end_date": end_date},
             capture=_cap("technical_analysis"),
         )
         if ticker else
         "No ticker could be resolved from the uploaded filings; "
         "technical analysis was skipped."),
    ]

    identified = bool(company_name or ticker)
    no_company = (
        "No company could be identified from the uploaded filings; "
        "this agent was skipped."
    )
    for agent_id, agent_cls in (
        ("earnings_call", EarningsCallAgent),
        ("company_news", CompanyNewsAgent),
        ("macro_market", MacroMarketAgent),
        ("youtube_analysis", YouTubeAgent),
    ):
        planned.append((
            agent_id,
            agent_cls().analyze(dict(company_ctx), capture=_cap(agent_id))
            if identified else no_company,
        ))

    # The Macro History agent does NOT join the debate — it produces an
    # independent advisory report. It still needs a company/ticker to infer
    # the sector, so it follows the same guard as the other company agents.
    planned.append((
        "macro_history",
        MacroHistoryAgent().analyze(dict(company_ctx), capture=_cap("macro_history"))
        if identified else no_company,
    ))

    # The Quant Risk agent runs after the others (it needs the macro regime),
    # but it counts toward the total so the progress bar doesn't jump at the end.
    total = len(planned) + 1
    yield {"phase": 1, "status": "running", "agents_total": total, "agents_completed": 0}

    reports: dict[str, dict] = {}          # successful reports only (feed 2 & 3)
    slots: dict[str, dict] = {}            # every agent's slot (incl. errors)
    agent_contexts: dict[str, dict] = {}   # raw_data + report, for debate & chat
    done = 0

    # Agents skipped before launch: report immediately so progress stays honest.
    runnable: list[tuple[str, object]] = []
    for agent_id, coro in planned:
        if isinstance(coro, str):
            slots[agent_id] = {"error": coro}
            done += 1
            yield {"phase": 1, "status": "agent_done", "agent": agent_id, "ok": False,
                   "skipped": True, "agents_completed": done, "agents_total": total}
        else:
            runnable.append((agent_id, coro))

    # ── Phase 1: run the rest concurrently but capped at 2 Gemini calls. ─────
    # Firing all six at once reliably trips the per-minute quota on Flash; two at
    # a time stays under it while overlapping the slow, network-bound fetching.
    sem = asyncio.Semaphore(2)

    async def _run(aid: str, coro):
        async with sem:
            try:
                return aid, await coro
            except BaseException as e:      # noqa: BLE001 — isolate every agent
                return aid, e

    tasks = [asyncio.create_task(_run(aid, coro)) for aid, coro in runnable]
    for fut in asyncio.as_completed(tasks):
        aid, res = await fut
        done += 1
        if isinstance(res, BaseException):
            logger.error(f"{aid} agent failed: {res}")
            slots[aid] = {"error": str(res)}
            ok = False
        else:
            rd = res.model_dump()
            reports[aid] = rd
            slots[aid] = rd
            agent_contexts[aid] = {
                "raw_data": captures.get(aid, {}).get("raw_data", ""), "report": rd,
            }
            ok = True
        yield {"phase": 1, "status": "agent_done", "agent": aid, "ok": ok,
               "agents_completed": done, "agents_total": total}

    # ── Quant Risk: portfolio-wide, and it needs Phase 1's macro read. ──────
    # It runs here rather than alongside the others because blueprint §2 requires
    # conditioning the risk numbers on the macro regime, which only exists once
    # the Macro agent has reported. Like MacroHistoryAgent it does NOT debate:
    # every debating agent argues about one company, and a portfolio-level claim
    # cannot rebut a company-level one.
    try:
        holdings = portfolio_service.list_holdings()
    except Exception as e:  # noqa: BLE001 — a portfolio read must not fail a run
        logger.warning(f"Could not read the portfolio for risk analysis: {e}")
        holdings = []

    # Cash in BASE CURRENCY, keyed by the currency it is actually held in — the
    # key selects the return series (won: none, dollars: the exchange rate's),
    # the value is what it is worth in won. A book of only cash is no longer
    # "nothing to measure": it has near-zero risk in won and real risk in dollars.
    cash_base: dict[str, float] = {}
    try:
        from providers import fx_provider
        from services import cash_service

        spot = None
        balances = cash_service.balances()
        if any(c != cash_service.BASE_CURRENCY and abs(v) > 1e-9
               for c, v in balances.items()):
            spot = (await fx_provider.fetch_spot()).rate
        for currency, amount in balances.items():
            if abs(amount) < 1e-9:
                continue
            converted = fx_provider.convert(
                amount, currency, cash_service.BASE_CURRENCY, spot
            )
            if converted is not None:
                cash_base[currency] = converted
    except Exception as e:  # noqa: BLE001 — cash is additive; never fail the run
        logger.warning(f"Could not read cash balances for risk analysis: {e}")

    if holdings or cash_base:
        try:
            risk_report = await QuantRiskAgent().analyze(
                {
                    "holdings": holdings,
                    "cash": cash_base,
                    "macro_report": reports.get("macro_market"),
                    "start_date": start_date,
                    "end_date": end_date,
                },
                capture=_cap("quant_risk"),
            )
            payload = risk_report.model_dump()
            reports["quant_risk"] = payload
            slots["quant_risk"] = {"report": payload}
            agent_contexts["quant_risk"] = {
                "raw_data": captures.get("quant_risk", {}).get("raw_data", ""),
                "report": payload,
            }
            done += 1
            yield {"phase": 1, "status": "agent_done", "agent": "quant_risk",
                   "ok": True, "agents_completed": done, "agents_total": total}
        except Exception as e:  # noqa: BLE001
            logger.error(f"Quant risk agent failed: {e}")
            slots["quant_risk"] = {"error": str(e)}
            done += 1
            yield {"phase": 1, "status": "agent_done", "agent": "quant_risk",
                   "ok": False, "agents_completed": done, "agents_total": total}
    else:
        slots["quant_risk"] = {
            "error": "No positions in the portfolio; portfolio risk analysis was "
                     "skipped. Add holdings on the Portfolio view to enable it."
        }
        done += 1
        yield {"phase": 1, "status": "agent_done", "agent": "quant_risk",
               "ok": False, "skipped": True,
               "agents_completed": done, "agents_total": total}

    # ── Phase 2: TRUE sequential debate over the agents that reported. ───────
    # The debate roster is limited to FIELD agents — the Macro History and
    # Quant Risk agents do NOT participate (each produces an independent
    # advisory report that the Manager receives alongside the debate).
    debate_contexts = {
        aid: ctx for aid, ctx in agent_contexts.items()
        if aid in FIELD_AGENT_IDS
    }
    transcript: DebateTranscript | None = None
    if len(debate_contexts) >= 2:
        yield {"phase": 2, "status": "debating",
               "participants": sorted(debate_contexts.keys())}
        try:
            transcript = await run_sequential_debate(debate_contexts)
        except Exception as e:             # noqa: BLE001
            logger.error(f"Sequential debate failed: {e}")
            transcript = None

    # ── Phase 3: manager synthesis (reports + transcript, NEVER raw data). ───
    yield {"phase": 3, "status": "synthesizing"}
    manager_result: object = None
    if reports:
        try:
            manager_result = await ManagerAgent().analyze({
                "reports": reports,
                "transcript": transcript,
                "period": f"{start_date}..{end_date}",
                "company": company_name or ticker,
            })
        except Exception as e:             # noqa: BLE001
            logger.error(f"Manager synthesis failed: {e}")
            manager_result = {"error": str(e)}

    # Programmatic 3-axis gap scores (deterministic, from the agent reports).
    three_axis = compute_three_axis_scores(reports)
    manager_payload = (
        manager_result.model_dump()
        if hasattr(manager_result, "model_dump") else manager_result
    )

    # Persist for the role-based chat (overwrites this company's previous run
    # only — other companies' runs stay intact).
    debate_store.replace(ticker, {
        "reports": reports,
        "agent_contexts": agent_contexts,
        "transcript": transcript,
        "manager": manager_result,
        "period": f"{start_date}..{end_date}",
        "company": company_name or ticker,
    })

    # Persist to history: disk (authoritative) + best-effort vector store.
    run_id = history_store.save_analysis(
        company=company_name, ticker=ticker,
        analysis_period=f"{start_date}..{end_date}",
        three_axis_scores=three_axis,
        manager=manager_payload if isinstance(manager_payload, dict) else None,
        reports=slots,
        debate=transcript.model_dump() if transcript else None,
    )

    result = {
        "run_id": run_id,
        "ticker": ticker,
        "analysis_period": f"{start_date}..{end_date}",
        "company": primary.model_dump() if primary else None,
        "agents_total": total,
        "agents_completed": len(reports),
        "three_axis_scores": three_axis,
        "reports": slots,
        "debate": transcript.model_dump() if transcript else None,
        "manager": manager_payload,
    }
    yield {"phase": 3, "status": "complete", "result": result}
