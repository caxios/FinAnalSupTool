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


class SubTask(TypedDict):
    kind: Literal["sql", "footnote", "search"]
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
    answer: dict                     # final synthesis (Phase 6) — QueryDataResponse-shaped dict
    citations: list[str]             # doc_ids actually used, for the UI to link back
