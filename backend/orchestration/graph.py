"""
orchestration.graph
──────────────────────
Wires the LangGraph state graph: plan -> {sql, footnote, search} (parallel
fan-out) -> assemble -> synthesize. This is the "Tool Router" the original
architecture diagram names as its own layer rather than one big if/elif
function — returning a LIST of node names from a conditional-edge routing
function is what makes LangGraph actually invoke all matched tool nodes
concurrently before converging on "assemble", which is the whole point of
naming it a router instead of a sequential chain.

assemble/synthesize (Phase 6) intentionally do NOT import anything from
services.research_copilot, even though research_copilot.query_data() is the
caller of run_graph() below — importing QueryDataResponse from there would
create research_copilot -> orchestration.graph -> research_copilot, a cycle.
Instead _SynthesisOutput is a LOCAL Pydantic model with the exact same shape;
_synthesize_node stores its .model_dump() (a plain dict) into state["answer"],
and research_copilot reconstructs QueryDataResponse(**state["answer"]) on the
other side with no import needed (duck-typed on the dict, not the class).
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from agents import llm_utils
from services import context_builder

from . import planner, tools
from .state import ResearchState

logger = logging.getLogger(__name__)

# Every tool node this graph can route to. Kept as a tuple (not derived from
# SubTask's Literal at runtime) so _route_after_plan's iteration order is
# stable and explicit.
_TOOL_KINDS = ("sql", "footnote", "search", "price", "insider")


# =============================================================================
# Synthesis — local mirror of services.research_copilot.QueryDataResponse
# (see module docstring for why this is a duplicate, not an import).
# =============================================================================

class _CitationModel(BaseModel):
    period: str = Field(
        ..., description="Filing period or data source, e.g. 'FY2025' or "
                          "'Q2 FY2025 earnings call'"
    )
    section: str = Field(
        ..., description="Section/table name, e.g. 'MD&A', 'Income Statement', "
                          "'Footnote: Debt'"
    )
    excerpt: str = Field(
        ..., description="The exact quoted or closely-extracted snippet "
                          "supporting the answer — not a paraphrase from memory"
    )


class _SynthesisOutput(BaseModel):
    table_markdown: str | None = Field(
        None, description="A Markdown table when the question calls for "
                          "tabular data; null otherwise"
    )
    citations: list[_CitationModel] = Field(default_factory=list)
    analytical_note: str = Field(
        "", description="2-3 sentences: the direct answer plus brief context "
                        "for incorporation into a research note"
    )


_SYNTHESIS_SYSTEM = """\
You are an equity-research data extraction assistant. You are given a
CONTEXT block assembled from a company's structured financial database
(verified numbers, never LLM-touched) and/or grounded excerpts from SEC
filings, earnings calls, and news (each tagged with a Doc_ID) — and a
specific question from an analyst.

ABSOLUTE RULES:
- Extract and organize ONLY what is present in the CONTEXT below. Never
  invent a number, quote, period, or company fact that is not there.
- Every citation's `excerpt` MUST be an actual quoted or closely-extracted
  snippet FROM the CONTEXT — never a summary invented from memory or general
  knowledge about the company.
- If the CONTEXT does not contain what the question asks for, say so plainly
  in `analytical_note`, leave `table_markdown` null, and leave `citations`
  empty — do NOT fabricate a plausible-looking table to fill the silence.
- `table_markdown` is a real Markdown table (header row + `---` separator
  row) ONLY when the question calls for tabular/multi-period data; otherwise
  null. Values under "## 1. Verified Financial Facts" are already correctly
  formatted (USD millions unless the Unit column says otherwise; EPS per
  share) — copy them as given, do not recompute or reformat.
- `analytical_note` is at most 2-3 sentences: the direct answer plus brief
  context — not a restatement of the whole table.

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

_SYNTHESIS_USER_TEMPLATE = """\
=== ANALYST QUESTION ===
{question}
=== END QUESTION ===

{context}

Answer the question using ONLY the context above.
"""

_NO_CONTEXT_NOTE = (
    "No verified financial facts, footnotes, or grounded excerpts were found "
    "for this question. Try a different phrasing, or confirm this ticker has "
    "been ingested via the Data tab."
)


async def _plan_node(state: ResearchState) -> dict:
    sub_tasks = await planner.plan(state.get("question", ""), state.get("ticker"))

    # Caller-side narrowing (orchestration.state.ResearchState): an agent
    # persona may only look at its own domain, so drop sub_tasks of other
    # kinds and pin every search to its allowed doc_types. Done here rather
    # than in the planner prompt so the restriction is enforced, not merely
    # requested of the model.
    allowed = state.get("allowed_kinds") or []
    if allowed:
        sub_tasks = [t for t in sub_tasks if t["kind"] in allowed]
    doc_types = state.get("search_doc_types") or []
    if doc_types:
        for t in sub_tasks:
            if t["kind"] == "search":
                t["doc_types"] = list(doc_types)
    return {"sub_tasks": sub_tasks}


def _route_after_plan(state: ResearchState) -> list[str]:
    """Fan out to every tool kind that has at least one sub_task — LangGraph
    runs all returned node names concurrently. "skip" (routing straight to
    "assemble") only fires if sub_tasks is somehow empty; planner.plan()
    itself never returns an empty list, so this is a defensive fallback, not
    the normal path."""
    kinds = {t["kind"] for t in state.get("sub_tasks", [])}
    routes = [k for k in _TOOL_KINDS if k in kinds]
    return routes or ["skip"]


async def _sql_node(state: ResearchState) -> dict:
    results: list[dict] = []
    for t in state.get("sub_tasks", []):
        if t["kind"] != "sql":
            continue
        try:
            results.extend(await tools.sql_tool(t))
        except Exception as e:  # noqa: BLE001 — isolate one tool's failure from the rest
            logger.warning(f"[orchestration.graph] sql_tool failed for {t}: {e}")
    return {"sql_results": results}


async def _footnote_node(state: ResearchState) -> dict:
    results: list[dict] = []
    for t in state.get("sub_tasks", []):
        if t["kind"] != "footnote":
            continue
        try:
            results.extend(await tools.footnote_tool(t))
        except Exception as e:  # noqa: BLE001 — isolate one tool's failure from the rest
            logger.warning(f"[orchestration.graph] footnote_tool failed for {t}: {e}")
    return {"footnote_results": results}


async def _search_node(state: ResearchState) -> dict:
    results = []
    for t in state.get("sub_tasks", []):
        if t["kind"] != "search":
            continue
        try:
            results.extend(await tools.search_tool(t))
        except Exception as e:  # noqa: BLE001 — isolate one tool's failure from the rest
            logger.warning(f"[orchestration.graph] search_tool failed for {t}: {e}")
    return {"search_results": results}


async def _price_node(state: ResearchState) -> dict:
    results: list[dict] = []
    for t in state.get("sub_tasks", []):
        if t["kind"] != "price":
            continue
        try:
            results.extend(await tools.price_tool(t))
        except Exception as e:  # noqa: BLE001 — isolate one tool's failure from the rest
            logger.warning(f"[orchestration.graph] price_tool failed for {t}: {e}")
    return {"price_results": results}


async def _insider_node(state: ResearchState) -> dict:
    results: list[dict] = []
    for t in state.get("sub_tasks", []):
        if t["kind"] != "insider":
            continue
        try:
            results.extend(await tools.insider_tool(t))
        except Exception as e:  # noqa: BLE001 — isolate one tool's failure from the rest
            logger.warning(f"[orchestration.graph] insider_tool failed for {t}: {e}")
    return {"insider_results": results}


async def _assemble_node(state: ResearchState) -> dict:
    context = context_builder.build_context(
        state.get("sql_results", []),
        state.get("footnote_results", []),
        state.get("search_results", []),
        price_results=state.get("price_results", []),
        insider_results=state.get("insider_results", []),
    )
    return {"context": context}


async def _synthesize_node(state: ResearchState) -> dict:
    context = state.get("context", "")
    if not context:
        # Nothing was retrieved — never call the LLM over an empty context;
        # answer honestly instead of risking a fabricated response.
        return {
            "answer": _SynthesisOutput(analytical_note=_NO_CONTEXT_NOTE).model_dump(),
            "citations": [],
        }

    user_prompt = _SYNTHESIS_USER_TEMPLATE.format(
        question=state.get("question", ""), context=context,
    )
    try:
        parsed = await llm_utils.generate_structured(
            _SYNTHESIS_SYSTEM, user_prompt, _SynthesisOutput, max_output_tokens=4096,
        )
    except Exception as e:  # noqa: BLE001 — never let a synthesis failure raise past run_graph
        logger.warning(f"[orchestration.graph] synthesis failed: {e}")
        parsed = _SynthesisOutput(
            analytical_note="Synthesis failed — the underlying data was retrieved "
                             "but could not be summarized. Please retry."
        )

    return {
        "answer": parsed.model_dump(),
        "citations": [h.doc_id for h in state.get("search_results", [])],
    }


def build_graph():
    g = StateGraph(ResearchState)

    g.add_node("plan", _plan_node)
    g.add_node("sql", _sql_node)
    g.add_node("footnote", _footnote_node)
    g.add_node("search", _search_node)
    g.add_node("price", _price_node)
    g.add_node("insider", _insider_node)
    g.add_node("assemble", _assemble_node)
    g.add_node("synthesize", _synthesize_node)

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", _route_after_plan, {
        "sql": "sql", "footnote": "footnote", "search": "search",
        "price": "price", "insider": "insider", "skip": "assemble",
    })
    for tool_node in _TOOL_KINDS:
        g.add_edge(tool_node, "assemble")
    g.add_edge("assemble", "synthesize")
    g.add_edge("synthesize", END)
    return g.compile()


def build_retrieval_graph():
    """The same plan -> tools -> assemble pipeline, STOPPING at the assembled
    context (no synthesis node). The chat assistant uses this: it wants the
    retrieved evidence to answer conversationally in its own voice/persona,
    not a second model's structured verdict."""
    g = StateGraph(ResearchState)

    g.add_node("plan", _plan_node)
    g.add_node("sql", _sql_node)
    g.add_node("footnote", _footnote_node)
    g.add_node("search", _search_node)
    g.add_node("price", _price_node)
    g.add_node("insider", _insider_node)
    g.add_node("assemble", _assemble_node)

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", _route_after_plan, {
        "sql": "sql", "footnote": "footnote", "search": "search",
        "price": "price", "insider": "insider", "skip": "assemble",
    })
    for tool_node in _TOOL_KINDS:
        g.add_edge(tool_node, "assemble")
    g.add_edge("assemble", END)
    return g.compile()


# Compiled once at import time — a LangGraph CompiledStateGraph is stateless
# and safe to reuse across calls/requests (each .ainvoke() gets its own
# fresh state dict), so there's no reason to rebuild it per call.
_compiled = None
_compiled_retrieval = None


async def run_graph(question: str, ticker: str | None) -> ResearchState:
    """Public entry point — Phase 6's router integration calls this."""
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return await _compiled.ainvoke({"question": question, "ticker": ticker})


async def retrieve_with_sources(
    question: str,
    ticker: str | None,
    *,
    allowed_kinds: list[str] | None = None,
    search_doc_types: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """
    Like retrieve_context, but also returns the citation registry for what was
    retrieved (see services.context_builder.build_context_with_sources) — so a
    caller can require the model to cite ``[S#]`` tags and then resolve those
    tags to real records for the reader.

    Never raises: on failure the answer is simply ungrounded, which the caller
    handles, rather than the conversation breaking.
    """
    global _compiled_retrieval
    if _compiled_retrieval is None:
        _compiled_retrieval = build_retrieval_graph()
    try:
        state = await _compiled_retrieval.ainvoke({
            "question": question,
            "ticker": ticker,
            "allowed_kinds": allowed_kinds or [],
            "search_doc_types": search_doc_types or [],
        })
    except Exception as e:  # noqa: BLE001 — retrieval degrades, never propagates
        logger.warning(f"[orchestration.graph] retrieval failed for {ticker!r}: {e}")
        return "", []
    return context_builder.build_context_with_sources(
        state.get("sql_results", []),
        state.get("footnote_results", []),
        state.get("search_results", []),
        price_results=state.get("price_results", []),
        insider_results=state.get("insider_results", []),
    )


async def retrieve_context(
    question: str,
    ticker: str | None,
    *,
    allowed_kinds: list[str] | None = None,
    search_doc_types: list[str] | None = None,
) -> str:
    """
    Plan the question, query only the stores it actually needs, and return the
    assembled context — the retrieval half of the pipeline, for callers that
    write their own answer (routers/chat.py).

    `allowed_kinds` / `search_doc_types` scope retrieval to one agent's domain
    (see _plan_node). Returns "" when nothing was found; never raises — a
    retrieval failure must degrade the answer, not break the conversation.
    """
    global _compiled_retrieval
    if _compiled_retrieval is None:
        _compiled_retrieval = build_retrieval_graph()
    try:
        state = await _compiled_retrieval.ainvoke({
            "question": question,
            "ticker": ticker,
            "allowed_kinds": allowed_kinds or [],
            "search_doc_types": search_doc_types or [],
        })
    except Exception as e:  # noqa: BLE001 — retrieval degrades, never propagates
        logger.warning(f"[orchestration.graph] retrieval failed for {ticker!r}: {e}")
        return ""
    return state.get("context", "") or ""
