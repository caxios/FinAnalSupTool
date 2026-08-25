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

Both modes are scoped by the request's ``ticker``, so a session holding several
companies never mixes one company's evidence into another's answer.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from schemas import ChatRequest, ChatResponse
from gemini_chat import build_context, ask_gemini, ask_persona, gemini_api_key
from rag import sec_rag
from agents import render_transcript, display_name, FIELD_AGENT_IDS
from services.storage import (
    DocumentStore,
    MediaCache,
    DebateStore,
    get_document_store,
    get_media_cache,
    get_debate_store,
)
from services import media_service

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
    """
    debate = debate_store.get(ticker)
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
               f"{sorted(FIELD_AGENT_IDS | {'macro_history'})}, 'manager', or omit it for the "
               f"general assistant.",
    )


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
        if not ticker:
            raise HTTPException(
                status_code=400,
                detail="A ticker is required to chat with an agent persona: it "
                       "selects which company's analysis run to talk about.",
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
    # store, it just means there's no filing data to ground the answer in.
    company = (
        store.get_company_store(ticker)
        if ticker and store.has_company(ticker) else None
    )
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

    context = build_context(
        merged_tables, text_store, filing_meta,
        extra_context=media_context,
        filing_text_override=filing_text_override,
    )

    # Short-circuit only when there's truly nothing to talk about — no filings
    # AND no media/macro data has been fetched (the Macro view needs no upload).
    if not filing_meta and not media_context.strip():
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
