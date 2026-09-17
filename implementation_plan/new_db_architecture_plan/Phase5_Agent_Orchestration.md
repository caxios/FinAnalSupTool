# Phase 5 — Agent Orchestration Layer (LangGraph)

Everything up to here is infrastructure (stores, ingestion, fused retrieval,
reranking) that nothing calls yet. This phase adds the piece that actually
decides, per question, which of those stores to query — the "Query Analyzer /
Planner" + "Tool Router" boxes from the original diagram.

This is a **new graph**, not a rewrite of the MAS pipeline
(`services/pipeline.py`/`agents/*_agent.py` are untouched — see Phase 0). It
becomes the engine behind the interactive "ask a question about data already
fetched" surface (Phase 6 wires it into `POST /analysis/query-data`).

New dependencies: `langgraph`, `langchain-core` (LangGraph's own state-graph
primitives depend on it, even though we do NOT use LangChain's LLM
integrations — every LLM call still goes through the existing
`gemini_chat.gemini_generate`, so auth/retry logic stays in one place).

---

## [NEW] `backend/orchestration/state.py`

```python
from typing import TypedDict
from rag.hybrid_search import SearchHit

class SubTask(TypedDict):
    kind: str            # "sql" | "footnote" | "search"
    ticker: str
    metric_or_topic: str
    periods: list[str]   # e.g. ["FY2025 Q1", ..., "FY2025 Q4"] for sql; [] for search

class ResearchState(TypedDict, total=False):
    question: str
    ticker: str | None
    sub_tasks: list[SubTask]
    sql_results: list[dict]          # rows from structured_db.query_facts
    footnote_results: list[dict]     # rows from structured_db.query_footnotes
    search_results: list[SearchHit]  # fused+reranked hits from hybrid_search.search
    context: str                     # built in Phase 6's assemble node
    answer: str                      # final synthesis
    citations: list[str]             # doc_ids actually used, for the UI to link back
```

---

## [NEW] `backend/orchestration/planner.py`

```python
_PLANNER_SYSTEM = """\
You decompose a financial research question into sub-tasks. Output ONLY JSON:
{
  "sub_tasks": [
    {"kind": "sql", "ticker": "MRVL", "metric_or_topic": "Data Center segment revenue",
     "periods": ["FY2025 Q1","FY2025 Q2","FY2025 Q3","FY2025 Q4"]},
    {"kind": "search", "ticker": "MRVL", "metric_or_topic": "custom ASIC design wins commentary", "periods": []}
  ]
}
Rules:
- "sql" tasks are for NUMBERS that live in a financial statement (revenue,
  margins, cash flow, balance sheet items) — never for qualitative commentary.
- "search" tasks are for anything that requires reading prose (earnings-call
  commentary, news, MD&A) — never invent a metric name for these; describe the
  topic in plain language, it will be used as a search query.
- "footnote" tasks are for "why did X change" questions about a specific
  statement line item — only emit these when the question explicitly asks
  about a footnote/note or an unusual accounting treatment.
- Infer the ticker from the question; if genuinely ambiguous, use the ticker
  the caller supplied as ambient context.
- Never invent periods not implied by the question; default to the last 4
  reported quarters if the question doesn't name a period.
"""

async def plan(question: str, ambient_ticker: str | None) -> list[SubTask]:
    """
    One gemini_chat.gemini_generate call with _PLANNER_SYSTEM, low temperature
    (0.1 — this is a structured decomposition, not creative writing). Parses
    the JSON response; on parse failure, falls back to a single "search"
    sub_task covering the whole question verbatim (degrade to plain hybrid
    search rather than fail the whole query).
    """
```

---

## [NEW] `backend/orchestration/tools.py`

Thin adapter nodes — each one just calls infrastructure already built in
Phases 1/3/4. No new logic here beyond mapping a `SubTask` to a call.

```python
async def sql_tool(task: SubTask) -> list[dict]:
    """structured_db.query_facts(task['ticker'], concepts=[...resolve metric_or_topic
    to a concept/label via a small keyword map...], fiscal_years=[...from periods...])"""

async def footnote_tool(task: SubTask) -> list[dict]:
    """structured_db.query_footnotes(task['ticker'], period_key=task['periods'][0],
    statement_item=task['metric_or_topic'])"""

async def search_tool(task: SubTask) -> list[SearchHit]:
    """hybrid_search.search(task['metric_or_topic'], ticker=task['ticker'],
    rerank=True, rerank_top_n=5)"""
```

`metric_or_topic` → XBRL concept resolution for `sql_tool`: reuse the
existing label strings from `edgar_xbrl.py`'s `INCOME_STATEMENT_CONCEPTS` /
`BALANCE_SHEET_CONCEPTS` / `CASH_FLOW_CONCEPTS` constants as the matchable
vocabulary (fuzzy/substring match the planner's free-text metric name against
these labels) rather than asking the LLM to know exact XBRL tag names.

---

## [NEW] `backend/orchestration/graph.py`

```python
from langgraph.graph import StateGraph, END

def build_graph() -> "CompiledGraph":
    g = StateGraph(ResearchState)

    g.add_node("plan", _plan_node)
    g.add_node("sql", _sql_node)
    g.add_node("footnote", _footnote_node)
    g.add_node("search", _search_node)
    g.add_node("assemble", _assemble_node)     # Phase 6
    g.add_node("synthesize", _synthesize_node) # Phase 6

    g.set_entry_point("plan")
    # Fan-out: plan decides WHICH of sql/footnote/search actually have work;
    # LangGraph runs any node whose predecessor emitted a matching edge
    # condition concurrently — this is the "결정론적 & 확률적 도구 병렬 실행" step.
    g.add_conditional_edges("plan", _route_after_plan, {
        "sql": "sql", "footnote": "footnote", "search": "search", "skip": "assemble",
    })
    for tool_node in ("sql", "footnote", "search"):
        g.add_edge(tool_node, "assemble")
    g.add_edge("assemble", "synthesize")
    g.add_edge("synthesize", END)
    return g.compile()


async def run_graph(question: str, ticker: str | None) -> ResearchState:
    """Public entry point — Phase 6's router integration calls this."""
    graph = build_graph()  # or module-level singleton, compiled once
    return await graph.ainvoke({"question": question, "ticker": ticker})
```

`_route_after_plan` inspects `state["sub_tasks"]` and returns the set of tool
node names that have at least one sub_task of that kind — LangGraph invokes
all matched nodes before converging on `assemble` (true parallel fan-out, not
a sequential if/elif chain), which is the actual point of naming a "Tool
Router" as its own layer instead of one big function.

Each tool node wraps its adapter in a per-node try/except (mirroring
`services/pipeline.py`'s `_run` pattern for the MAS agents): one tool failing
(e.g. Cohere down) must not take down the other two or the whole query.

---

## Execution Order

| Step | Description |
|---|---|
| 1 | `pip install langgraph langchain-core` |
| 2 | `orchestration/state.py` |
| 3 | `orchestration/planner.py` — test in isolation against 5-10 real questions, eyeball the JSON |
| 4 | `orchestration/tools.py` |
| 5 | `orchestration/graph.py`, with `assemble`/`synthesize` as STUBS (pass-through) until Phase 6 |
| 6 | Manual `run_graph()` call from a script, confirm the right tool nodes fire for a sql-only, a search-only, and a mixed question |

## Verification Plan

- **Planner accuracy**: hand-write 10 realistic questions ("What was MRVL's
  Data Center revenue last 4 quarters" → sql-only; "What did management say
  about AI demand" → search-only; "How has MRVL's revenue grown and what's
  driving it" → both). Confirm the planner's sub_task `kind`s match
  expectations for at least 8/10 — this is the highest-risk, most
  LLM-judgment-dependent part of the whole architecture, so verify it
  directly rather than only end-to-end.
- **Parallel execution**: for a mixed question, log wall-clock time and
  confirm sql/search run concurrently (total time ≈ max(sql, search), not
  sql + search).
- **Partial failure**: manually break one tool (e.g. point `sql_tool` at a
  ticker with no `financial_facts` rows) and confirm the graph still reaches
  `synthesize` with the other tools' results rather than crashing.
- **Malformed planner output**: feed a deliberately weird question and
  confirm the JSON-parse-failure fallback (single search sub_task) engages
  instead of raising.
