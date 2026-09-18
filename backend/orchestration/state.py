"""
orchestration.state
─────────────────────
The shapes threaded through the LangGraph state graph (orchestration/graph.py).

SubTask is a plain TypedDict (not a Pydantic model) because it's internal
graph state, not something validated at a boundary — the planner DOES
validate its own LLM output against a Pydantic model (see
orchestration/planner.py) and converts to this shape before it ever enters
the graph.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from rag.hybrid_search import SearchHit


class SubTask(TypedDict, total=False):
    kind: Literal["sql", "footnote", "search", "price", "insider"]
    ticker: str
    metric_or_topic: str
    periods: list[str]   # e.g. ["FY2025 Q1", ..., "FY2025 Q4"] for sql; [] for search
    doc_types: list[str]  # search only: narrow to e.g. ["earnings_transcript"]


class ResearchState(TypedDict, total=False):
    question: str
    ticker: str | None
    sub_tasks: list[SubTask]
    sql_results: list[dict]          # rows from structured_db.query_facts
    footnote_results: list[dict]     # rows from structured_db.query_footnotes
    search_results: list[SearchHit]  # fused+reranked hits from hybrid_search.search
    price_results: list[dict]        # latest cached price/technicals (services.price_cache)
    insider_results: list[dict]      # Form 4 trades + 8-K rows (services.insider_cache)
    # Narrowing applied by a CALLER (e.g. an agent persona that may only see its
    # own domain): sub_tasks of other kinds / search hits of other doc_types are
    # dropped after planning rather than being planned around.
    allowed_kinds: list[str]
    search_doc_types: list[str]
    context: str                     # built in Phase 6's assemble node
    answer: dict                     # final synthesis (Phase 6) — QueryDataResponse-shaped dict
    citations: list[str]             # doc_ids actually used, for the UI to link back
