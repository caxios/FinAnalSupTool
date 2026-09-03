"""
services.research_copilot
──────────────────────────
The Interactive Research Data Copilot behind ``POST /analysis/query-data``: an
on-demand, grounded extraction assistant for the Deep Analysis workspace. An
analyst drafting a research note asks a specific question ("3-year segment
revenue table", "what did MD&A say about margin compression?") and gets back a
Markdown table (when applicable), citations, and a short analytical note —
extracted, never computed or opinionated.

This is deliberately NOT another MAS agent: it doesn't join the debate, and it
answers ONE ad-hoc question rather than producing a full report. It reuses
existing data paths rather than fetching anything new:

  - "financials" / "sec_text" -> ``gemini_chat.build_context`` over the
    already-in-memory ``CompanyStore`` (the SAME renderer the chat assistant
    and the SEC Filings agent use — scoped by passing an empty dict for the
    half not requested).
  - "earnings"  -> the LAST ``/analyze`` run's CAPTURED earnings-call raw data
    from ``DebateStore`` (no new Tavily fetch — that would be one live network
    call per copilot question, which is both slow and wasteful when the same
    transcripts were already pulled for the last analysis run).
  - "peers"     -> ``providers.peer_provider`` (the same module the Peer
    Comparison agent uses).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from agents import llm_utils
from gemini_chat import build_context
from providers import peer_provider
from services.storage import CompanyStore, DebateStore

logger = logging.getLogger(__name__)

VALID_SCOPES = ("financials", "sec_text", "earnings", "peers", "all")

# Keeps a single copilot call bounded even at scope="all" on a company with a
# long filing history — this is one ad-hoc question, not a full agent run.
_MAX_DATA_CHARS = 150_000


class QueryDataCitation(BaseModel):
    """One grounded citation: exactly where an extracted fact came from."""
    period: str = Field(
        ..., description="Filing period or data source, e.g. 'FY2025' or "
                          "'Peer set (direct_cluster_membership)'"
    )
    section: str = Field(
        ..., description="Section/table name, e.g. 'MD&A', 'Income Statement', "
                          "'Peer Metrics'"
    )
    excerpt: str = Field(
        ..., description="The exact quoted or closely-extracted snippet "
                          "supporting the answer — not a paraphrase from memory"
    )


class QueryDataResponse(BaseModel):
    """The copilot's answer to one ad-hoc data question."""
    table_markdown: str | None = Field(
        None, description="A Markdown table when the question calls for "
                          "tabular data; null otherwise"
    )
    citations: list[QueryDataCitation] = Field(default_factory=list)
    analytical_note: str = Field(
        "", description="2-3 sentences: the direct answer plus brief context "
                        "for incorporation into a research note"
    )


_SYSTEM_PROMPT = """\
You are an equity-research data extraction assistant embedded in a financial
analysis tool. You are given a SLICE of one company's data — financial
statement tables, filing text (MD&A / footnotes / risk factors), captured
earnings-call material, and/or peer metrics — and a specific question from an
analyst who is drafting a research note.

ABSOLUTE RULES:
- Extract and organize ONLY what is present in the DATA below. Never invent a
  number, quote, period, or company fact that is not there.
- Every citation's `excerpt` MUST be an actual quoted or closely-extracted
  snippet FROM the DATA — never a summary invented from memory or general
  knowledge about the company.
- If the DATA does not contain what the question asks for, say so plainly in
  `analytical_note`, leave `table_markdown` null, and leave `citations` empty
  — do NOT fabricate a plausible-looking table to fill the silence.
- `table_markdown` is a real Markdown table (header row + `---` separator row)
  ONLY when the question calls for tabular/multi-period data; otherwise null.
- `analytical_note` is at most 2-3 sentences: the direct answer plus brief
  context — not a restatement of the whole table.
- Values are as labelled in the DATA (USD millions unless noted; EPS per
  share; ratios as multiples/percent). Do not convert units yourself.

Output ONLY a single JSON object:
{
  "table_markdown": "<Markdown table, or null>",
  "citations": [
    {"period": "<filing period / data source>",
     "section": "<section/table name>",
     "excerpt": "<exact quoted or extracted snippet>"}
  ],
  "analytical_note": "<2-3 sentences>"
}
"""

_USER_TEMPLATE = """\
=== ANALYST QUESTION ===
{query}
=== END QUESTION ===

=== AVAILABLE DATA (scope: {scope}) ===
{data}
=== END DATA ===

Answer the question using ONLY the data above.
"""


def _financials_text(store: CompanyStore) -> str:
    """Financial statements + ratios only — reuses build_context with no text_store."""
    return build_context(store.merged_tables, {}, store.filing_meta)


def _sec_text_text(store: CompanyStore) -> str:
    """Filing text only — reuses build_context with no merged_tables."""
    return build_context({}, store.text_store, store.filing_meta)


def _earnings_text(debate_store: DebateStore, ticker: str) -> str:
    """
    The last /analyze run's captured earnings-call raw data — not a new fetch.
    A copilot question is ad-hoc and can arrive many times per session; a live
    Tavily call per question would be both slow and needlessly repeat a fetch
    the last analysis run already paid for.
    """
    record = debate_store.get(ticker) or {}
    ctx = (record.get("agent_contexts") or {}).get("earnings_call")
    raw = (ctx or {}).get("raw_data")
    if raw:
        return raw
    return (
        "(No earnings-call data available. This scope reuses the transcripts "
        "captured by the last Deep Analysis run for this ticker rather than "
        "fetching new ones — run a Deep Analysis first to populate it.)"
    )


async def _peers_text(ticker: str) -> str:
    """Live peer discovery + metrics — the same module the Peer Comparison agent uses."""
    import json

    discovery = await peer_provider.discover_peers(ticker)
    peers = discovery["peers"]
    if not peers:
        return f"(No peer set could be identified for {ticker}.)"
    metrics = await peer_provider.fetch_peer_metrics(ticker, peers)
    return (
        f"Peers ({discovery['method']}, sector: {discovery.get('sector')}, "
        f"industry: {discovery.get('industry')}): {', '.join(peers)}\n\n"
        + json.dumps(metrics["metrics_table"], ensure_ascii=False, indent=2, default=str)
    )


async def query_data(
    *,
    company_store: CompanyStore,
    debate_store: DebateStore,
    ticker: str,
    query: str,
    data_scope: str = "all",
) -> QueryDataResponse:
    """
    Answer one ad-hoc data question, scoped to the requested data source(s).

    Raises ``ValueError`` for an unrecognized scope (the router maps this to a
    400) or an empty query.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("A query is required.")
    scope = (data_scope or "all").strip().lower()
    if scope not in VALID_SCOPES:
        raise ValueError(f"Unknown data_scope {scope!r}. Use one of {VALID_SCOPES}.")

    blocks: list[str] = []
    if scope in ("financials", "all"):
        blocks.append(f"--- FINANCIAL STATEMENTS & RATIOS ---\n{_financials_text(company_store)}")
    if scope in ("sec_text", "all"):
        blocks.append(f"--- FILING TEXT (MD&A / FOOTNOTES / RISK FACTORS) ---\n{_sec_text_text(company_store)}")
    if scope in ("earnings", "all"):
        blocks.append(f"--- EARNINGS CALL MATERIAL ---\n{_earnings_text(debate_store, ticker)}")
    if scope in ("peers", "all"):
        blocks.append(f"--- PEER METRICS ---\n{await _peers_text(ticker)}")

    data_text = "\n\n".join(blocks)
    if len(data_text) > _MAX_DATA_CHARS:
        data_text = (
            data_text[:_MAX_DATA_CHARS]
            + "\n…[truncated — narrow data_scope to a single source for full coverage]…"
        )

    user_prompt = _USER_TEMPLATE.format(query=query, scope=scope, data=data_text)
    return await llm_utils.generate_structured(
        _SYSTEM_PROMPT, user_prompt, QueryDataResponse, max_output_tokens=4096,
    )
