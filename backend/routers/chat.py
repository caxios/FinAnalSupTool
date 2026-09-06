"""
routers.chat
─────────────
The conversational assistant:

  POST /chat  — Answer a question about the uploaded filings + fetched media.

Two modes, selected by the request's ``agent_id``:
  - general (default) → the cross-view assistant, grounded in the merged
    financials, filing text, and media fetched for the requested company (plus
    the shared macro data).
  - a field agent id or 'manager' → an ISOLATED persona built from that
    company's last /analyze run (rules enforced in ``_agent_chat_persona``).
  - 'trading_coach' → the coach, grounded in the user's trading journal
    rather than in a debate record (see ``_coach_chat_persona``).

Both modes are scoped by the request's ``ticker``, so a session holding several
companies never mixes one company's evidence into another's answer.

Grounding a COLD ticker (general assistant only)
─────────────────────────────────────────────────
``DocumentStore`` is in-memory and process-local, so a server restart — or
simply opening an archived Deep Analysis run without re-fetching — leaves it
empty for a ticker that was fully analyzed in an earlier session. Rather than
telling the user to re-upload filings the app already has evidence for, the
general-assistant path tries, in order:
  1. The live in-memory ``CompanyStore`` (the common case, nothing changes).
  2. Disk-cache rehydration (``services.filing_cache``) — the raw text/tables
     from the last time this ticker was ingested, restored into memory.
  3. A cross-run RAG search (``sec_rag.query_by_ticker``) — chunks indexed
     from an earlier run/session, found by ticker rather than a specific
     run_id (only populated for large multi-period runs; see its docstring).
  4. The persisted analysis summary (``rag.history_store``) — the SEC Filings
     agent's own MD&A insights / financial health / QoE findings from the
     last stored Deep Analysis run, when none of the above yield anything.
Only when all four are empty does the assistant say there's nothing to answer
from.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from schemas import ChatRequest, ChatResponse
from gemini_chat import build_context, ask_gemini, ask_persona, gemini_api_key
from rag import history_store, sec_rag
from agents import render_transcript, display_name, DebateTranscript, FIELD_AGENT_IDS
from services.storage import (
    DocumentStore,
    MediaCache,
    DebateStore,
    get_document_store,
    get_media_cache,
    get_debate_store,
)
from services import filing_cache, media_service, journal_analysis, review_store

router = APIRouter(tags=["chat"])


# Cap on a single agent's raw data injected into an isolated chat. Bounds a long
# earnings transcript (~80K chars) while leaving room for the debate + question.
_CHAT_RAW_CAP = 60_000

_FIELD_CHAT_TEMPLATE = """\
You are the {name}, one of six specialist analysts on a financial research team.
You have completed your own analysis and taken part in a round-table debate with
the other analysts. A user now wants to talk to YOU specifically.

Ground rules:
- Answer ONLY from YOUR OWN data and findings below, plus the shared debate
  transcript. You do NOT have the other analysts' raw data. If the user asks
  about something outside your domain, say it is outside your remit and point
  them to the relevant analyst or the Manager.
- Cite specifics from your data (numbers, quotes, dates). Never invent figures or
  use outside knowledge about the company's actuals.
- You may reference what other analysts argued in the debate transcript, but you
  can only speak authoritatively about your own evidence.
- Be concise and use Markdown.

=== YOUR INITIAL FINDINGS (your Phase-1 JSON report) ===
{report}
=== END FINDINGS ===

=== YOUR RAW DATA ===
{raw_data}
=== END RAW DATA ===

=== ROUND-TABLE DEBATE TRANSCRIPT (all analysts) ===
{transcript}
=== END TRANSCRIPT ==="""

_MANAGER_CHAT_TEMPLATE = """\
You are the Lead Analyst (Manager) of a financial research team. Six specialist
analysts each produced a report and then debated each other. A user wants to
discuss the overall investment picture with you.

Ground rules:
- You see every analyst's INITIAL REPORT (JSON) and the full debate transcript —
  but NOT their raw source data (no filings text, earnings transcripts, or
  headlines). Reason from the reports and the debate only; never introduce facts
  or numbers that are not present in them.
- Weigh evidence quality across domains, resolve disagreements, and give a clear
  synthesized view. Attribute claims to the analyst/domain they came from.
- Be concise and use Markdown.

=== ALL INITIAL AGENT REPORTS (JSON) ===
{reports}
=== END REPORTS ===

=== ROUND-TABLE DEBATE TRANSCRIPT ===
{transcript}
=== END TRANSCRIPT ==="""



_COACH_CHAT_TEMPLATE = """You are the user's Trading Coach. You do not pick stocks — you help this user
see their own decision-making clearly, using their real trading journal.

Ground rules:
- NEVER invent a past trade. Every date you mention must appear in the journal
  below. If the journal is empty, say so.
- NEVER invent a number. Cite only what is in the data below.
- If the behavioural summary says the history is insufficient, say the history is
  too short to establish a pattern rather than generalizing from a few trades.
- Be direct but not moralizing. A good decision deserves to be told it is good.
- NEVER state a directional view on the exchange rate. You may say what the
  user's dollar exposure IS and what it costs or hedges; you may not say where
  USDKRW is going.
- Weights are shares of NET WORTH (positions plus cash), so position weights sum
  to less than 1 and the remainder is cash. Do not describe the portfolio as
  fully invested.
- PORTFOLIO RISK below is computed the same way the Portfolio Risk dashboard
  computes it — VaR, CVaR, per-position risk contribution vs. capital weight,
  and pairwise correlation. Cite only numbers that appear in it; if it says a
  position's risk share far exceeds its capital weight, or a `risk_warnings`
  entry is present, say so plainly.
- Be concise and use Markdown.

=== THE USER'S TRADING JOURNAL (real logged trades + outcomes) ===
{journal}
=== END JOURNAL ===

=== BEHAVIOURAL SUMMARY (computed) ===
{patterns}
=== END SUMMARY ===

=== CASH, SIZING AND CURRENCY (computed, in KRW; weights are of NET WORTH) ===
{position}
=== END POSITION ===

=== PORTFOLIO RISK (computed — VaR/CVaR/correlation over the WHOLE book) ===
{risk}
=== END PORTFOLIO RISK ===

=== WHAT YOU HAVE ALREADY TOLD THIS USER (past reviews) ===
{reviews}
=== END REVIEWS ===

=== TRADES STILL AWAITING A REVIEW ===
{pending}
=== END PENDING ===

=== FUNDAMENTAL ANALYST REPORT (if a Deep Analysis was run) ===
{fundamental}
=== END FUNDAMENTAL ===

=== TECHNICAL ANALYST REPORT (if a Deep Analysis was run) ===
{technical}
=== END TECHNICAL ==="""


async def _coach_chat_persona(debate_store: DebateStore, ticker: str | None) -> str:
    """
    Build the coach persona.

    Unlike every other persona this one is NOT built from a debate record — the
    coach's subject is the user's journal, which exists whether or not any
    analysis has been run. The company reports are folded in when available, so
    "why did I sell?" can be answered against the fundamentals too.
    """
    journal = await journal_analysis.trade_outcomes(limit=25)
    patterns = await journal_analysis.pattern_summary()

    # Past reviews let the user ask "what have you been telling me?" and "what
    # did I ignore?" — questions the coach could not answer while every review
    # was discarded the moment it was rendered.
    reviews = [
        {
            "reviewed_at": r.get("created_at"),
            "review_type": r.get("review_type"),
            "ticker": r.get("ticker"),
            "trade_id": r.get("trade_id"),
            "rationale_at_the_time": r.get("rationale_snapshot"),
            "coaching_feedback": (r.get("report") or {}).get("coaching_feedback"),
            "alignment_score": (r.get("report") or {}).get("alignment_score"),
            "process_quality": (r.get("report") or {}).get("process_quality"),
            "luck_vs_skill": (r.get("report") or {}).get("luck_vs_skill"),
        }
        for r in review_store.list_reviews(limit=15)
    ]
    pending = review_store.unreviewed_trades(limit=15)

    # Cash, sizing and currency, so "how much cash do I have?", "what is my
    # biggest position?" and "how exposed am I to the dollar?" are answerable
    # without a ticker and without any prior analysis.
    from agents.coach_agent import position_context, portfolio_risk_context
    position = await position_context(None, None, None)
    risk = await portfolio_risk_context(ticker, None, None)

    sec_report = technical_report = None
    if ticker:
        record = debate_store.get(ticker) or {}
        if not record:
            # COLD ticker: no live debate this session — fall back to the
            # persisted analysis record rather than leaving the coach with
            # no fundamental/technical pillar for a company that HAS been
            # analyzed, just not in this process's lifetime.
            archived = history_store.get_latest_analysis(ticker)
            if archived:
                record = {"reports": archived.get("reports") or {}}
        reports = record.get("reports") or {}
        sec_report = reports.get("sec_filings")
        technical_report = reports.get("technical_analysis")

    def dump(x):
        return (json.dumps(x, ensure_ascii=False, indent=2, default=str)
                if x else "(Not available — no Deep Analysis has been run.)")

    return _COACH_CHAT_TEMPLATE.format(
        journal=dump(journal) if journal else
                "(The journal is EMPTY. The user has logged no trades; do not "
                "cite any past trade.)",
        patterns=dump(patterns),
        position=dump(position) if position else
                 "(The portfolio could not be valued, so no cash or sizing "
                 "figures are available.)",
        risk=dump(risk) if risk else
             "(Portfolio risk could not be computed — do not cite VaR, "
             "correlation, or risk contribution figures.)",
        reviews=dump(reviews) if reviews else
                "(You have not reviewed anything for this user yet.)",
        pending=(
            json.dumps(
                [{"trade_id": t["id"], "ticker": t["ticker"], "side": t["side"],
                  "executed_at": t["executed_at"],
                  "entry_rationale": t.get("entry_rationale")}
                 for t in pending],
                ensure_ascii=False, indent=2, default=str,
            )
            if pending else "(None — every logged trade has been reviewed.)"
        ),
        fundamental=dump(sec_report),
        technical=dump(technical_report),
    )


def _agent_chat_persona(
    agent_id: str, debate_store: DebateStore, ticker: str
) -> str:
    """
    Build the ISOLATED system prompt for a single-agent chat from that COMPANY's
    last /analyze run. Enforces the data-isolation rules: a field agent sees only
    its own raw data + report + the debate transcript; the Manager sees all
    reports + the transcript but no raw data.

    Raises HTTPException with a helpful message when the persona can't be served
    (no analysis yet for this company, agent didn't report, or unknown id).

    A COLD ticker (server restart, or opening an archived Deep Analysis run
    without re-fetching) falls back to the persisted record on disk
    (``rag.history_store``) before giving up — the same "disk survives a
    restart, RAM doesn't" principle as the general assistant's fallback chain
    above. The reconstructed record is cached back into ``debate_store`` so a
    second question this session is served from memory. Runs saved before
    ``agent_contexts`` was persisted have no raw_data to offer a field agent
    (only the Manager persona, which needs reports only, is unaffected).
    """
    debate = debate_store.get(ticker)
    if not debate:
        record = history_store.get_latest_analysis(ticker)
        if record:
            # The persisted `debate` field is a plain dict (JSON round-trip),
            # while `render_transcript` below requires the actual
            # `DebateTranscript` Pydantic model (it reads `.history`, not
            # `["history"]`) — the live `DebateStore` only ever holds the
            # real object because `pipeline.py` writes it there directly.
            raw_transcript = record.get("debate")
            transcript = DebateTranscript(**raw_transcript) if raw_transcript else None
            debate = {
                "reports": record.get("reports") or {},
                "agent_contexts": record.get("agent_contexts") or {},
                "transcript": transcript,
                "manager": record.get("manager"),
                "period": record.get("analysis_period"),
                "company": record.get("company"),
            }
            debate_store.replace(ticker, debate)
    if not debate:
        raise HTTPException(
            status_code=409,
            detail=f"No analysis has been run for '{ticker}' yet. Run POST "
                   f"/analyze for it first, then you can chat with an "
                   f"individual agent.",
        )

    transcript = debate.get("transcript")
    rendered = render_transcript(transcript)

    if agent_id == "manager":
        reports = debate.get("reports") or {}
        if not reports:
            raise HTTPException(
                status_code=409,
                detail="The last analysis produced no agent reports to synthesize.",
            )
        return _MANAGER_CHAT_TEMPLATE.format(
            reports=json.dumps(reports, ensure_ascii=False, indent=2),
            transcript=rendered,
        )

    if agent_id in FIELD_AGENT_IDS or agent_id == "macro_history":
        ctx = (debate.get("agent_contexts") or {}).get(agent_id)
        if not ctx:
            raise HTTPException(
                status_code=409,
                detail=f"The '{agent_id}' agent did not produce a report in the "
                       f"last analysis (it may have been skipped or failed), so "
                       f"there is nothing to discuss with it.",
            )
        raw = (ctx.get("raw_data") or "")[:_CHAT_RAW_CAP] or "(no raw data captured)"
        return _FIELD_CHAT_TEMPLATE.format(
            name=display_name(agent_id),
            report=json.dumps(ctx.get("report") or {}, ensure_ascii=False, indent=2),
            raw_data=raw,
            transcript=rendered,
        )

    raise HTTPException(
        status_code=400,
        detail=f"Unknown agent_id '{agent_id}'. Use one of "
               f"{sorted(FIELD_AGENT_IDS | {'macro_history'})}, "
               f"'manager', 'trading_coach', or omit it for the general assistant.",
    )


def _archived_findings_block(record: dict) -> str:
    """
    Condense a stored analysis record's own computed findings into a labelled
    Markdown block — the general assistant's LAST-resort grounding when no
    live filing text and no RAG excerpt are available for this ticker (see the
    module docstring's fallback chain).

    Deliberately narrow: only the SEC Filings agent's structured findings (the
    closest thing this app has to "what the filing said") plus the Manager's
    executive summary, never the full report set — this is meant to read as a
    condensed brief, not a data dump the assistant might over-interpret.
    """
    reports = record.get("reports") or {}
    sec = reports.get("sec_filings") or {}
    manager = record.get("manager") or {}

    parts = [
        "# Archived Analysis Findings",
        f"(From a prior Deep Analysis run — period {record.get('analysis_period', 'unknown')}, "
        f"run {str(record.get('timestamp', ''))[:10] or 'unknown date'}. Full "
        f"extracted filing text is NOT currently loaded; these are the SEC "
        f"Filings agent's own computed findings from when the filing WAS "
        f"loaded, not the primary source.)",
        "",
    ]
    if sec.get("financial_health"):
        parts += ["## Financial Health",
                   json.dumps(sec["financial_health"], ensure_ascii=False, indent=2), ""]
    if sec.get("mda_insights"):
        parts += ["## MD&A Insights", *[f"- {i}" for i in sec["mda_insights"]], ""]
    if sec.get("multi_period_trends"):
        parts += ["## Multi-Period Trends",
                   json.dumps(sec["multi_period_trends"], ensure_ascii=False, indent=2), ""]
    if sec.get("quality_of_earnings_forensic"):
        parts += ["## Quality of Earnings — Forensic Findings",
                   json.dumps(sec["quality_of_earnings_forensic"], ensure_ascii=False, indent=2), ""]
    if sec.get("risk_assessment"):
        parts += ["## Risk Assessment",
                   json.dumps(sec["risk_assessment"], ensure_ascii=False, indent=2), ""]
    if manager.get("executive_summary"):
        parts += ["## Manager's Executive Summary (from the same run)",
                   manager["executive_summary"], ""]

    return "\n".join(parts)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    store: DocumentStore = Depends(get_document_store),
    cache: MediaCache = Depends(get_media_cache),
    debate_store: DebateStore = Depends(get_debate_store),
):
    """
    Answer a natural-language question about ONE company's filings using Gemini.

    The general assistant is grounded strictly in the app's own data for
    ``request.ticker`` — that company's merged financial statements + ratios,
    its extracted filing text, and its media — plus the shared macro data. The
    context is re-assembled on each call, so freshly uploaded filings are always
    in scope. Omitting the ticker gives a macro-only conversation.
    """
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    ticker = (request.ticker or "").strip().upper() or None

    # Fail fast with a clear message if the key isn't configured.
    if not gemini_api_key():
        raise HTTPException(
            status_code=503,
            detail="The AI assistant is not configured: GEMINI_API_KEY is not "
                   "set on the backend. Set it in the server environment and "
                   "restart to enable chat.",
        )

    # ── Role-based chat: talk to ONE agent (or the Manager) in isolation ──
    # When an agent_id is supplied, we scope the system prompt to just that
    # agent's data + the debate transcript (data isolation → far fewer tokens
    # than the omniscient assistant). Omit it (or 'general') for the cross-view
    # assistant below.
    agent_id = (request.agent_id or "").strip().lower()
    if agent_id and agent_id != "general":
        # The coach is the one persona that does NOT need a ticker or a prior
        # analysis: its subject is the user's own journal, which exists from the
        # first logged trade. A ticker just adds the company reports on top.
        if agent_id == "trading_coach":
            system_prompt = await _coach_chat_persona(debate_store, ticker)
        else:
            if not ticker:
                raise HTTPException(
                    status_code=400,
                    detail="A ticker is required to chat with an agent persona: "
                           "it selects which company's analysis run to talk about.",
                )
            # raises if unavailable
            system_prompt = _agent_chat_persona(agent_id, debate_store, ticker)
        history = [{"role": m.role, "content": m.content} for m in request.history]
        try:
            answer = await ask_persona(question, history, system_prompt)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return ChatResponse(answer=answer)

    # Assemble the grounding context from this company's in-memory data, plus the
    # media/macro data fetched for it (so the AI sees all views for ONE company).
    media_context = media_service.build_media_context(cache, ticker)

    # Only the named company's filings are in scope. Without a ticker the
    # assistant is macro-only (no filing data at all).
    # Look up without creating: an unknown ticker must not register an empty
    # store, it just means there's no filing data to ground the answer in yet
    # — `filing_cache.rehydrate_company_store` below still gets a chance to
    # populate it (it operates on the ticker directly, not on this lookup).
    company = (
        store.get_company_store(ticker)
        if ticker and store.has_company(ticker) else None
    )
    has_live_data = bool(company and (company.text_store or company.table_store))

    # COLD ticker: nothing in memory for it (fresh process, or an archived
    # analysis opened without re-fetching). Try to rehydrate from the disk
    # cache written at ingestion time — see the module docstring's fallback
    # chain. This never overwrites live data; it only fills an empty store.
    if ticker and not has_live_data:
        if filing_cache.rehydrate_company_store(ticker, store):
            company = store.get_company_store(ticker)
            has_live_data = bool(company.text_store or company.table_store)

    merged_tables = company.merged_tables if company else {}
    text_store = company.text_store if company else {}
    filing_meta = company.filing_meta if company else {}

    # Filing text is included in full — UNLESS it's too large for the window, in
    # which case we chunk every section and retrieve only the passages relevant
    # to THIS question (so no MD&A / Risk Factors detail is lost to a static cap).
    filing_text_override = None
    if text_store:
        try:
            filing_text_override = await sec_rag.prepare_context(
                text_store, list(filing_meta.keys()),
                queries=[question],
                ticker=ticker, run_id="chat",
            )
        except Exception:  # noqa: BLE001 — best-effort; fall back to full text
            filing_text_override = None

    # STILL cold (nothing ingested this session, and the disk cache was empty
    # or missing too) — fall further down the chain rather than answering from
    # nothing: a cross-run RAG search, then the persisted analysis summary.
    archived_findings = None
    if ticker and not text_store:
        try:
            filing_text_override = await sec_rag.query_by_ticker(ticker, question)
        except Exception:  # noqa: BLE001 — best-effort
            filing_text_override = None
        if not filing_text_override:
            record = history_store.get_latest_analysis(ticker)
            if record:
                archived_findings = _archived_findings_block(record)

    context = build_context(
        merged_tables, text_store, filing_meta,
        extra_context=media_context,
        filing_text_override=filing_text_override,
        archived_findings=archived_findings,
    )

    # Short-circuit only when there's truly nothing to talk about anywhere in
    # the fallback chain, AND no media/macro data has been fetched (the Macro
    # view needs no upload).
    if not filing_meta and not archived_findings and not media_context.strip():
        return ChatResponse(
            answer="No data yet. Upload SEC 10-K / 10-Q PDFs on the Dashboard, "
                   "or open the Company Media / Macro Sentiment views to pull in "
                   "news and market data — then ask me about any of it.",
        )

    history = [{"role": m.role, "content": m.content} for m in request.history]

    try:
        answer = await ask_gemini(question, history, context)
    except RuntimeError as e:
        # Configuration / API errors from the Gemini layer.
        raise HTTPException(status_code=502, detail=str(e))

    return ChatResponse(answer=answer)
