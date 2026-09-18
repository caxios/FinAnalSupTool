"""
orchestration.planner
────────────────────────
Decomposes a financial research question into sub-tasks: which numbers need
a SQL lookup, which line items need a footnote lookup, and what needs a
hybrid-search read — the "Query Analyzer / Planner" box from the original
architecture diagram.

Uses agents.llm_utils.generate_structured rather than a raw Gemini call +
manual json.loads (the plan's own sketch) — that helper already gives this
JSON-mode enforcement, Pydantic validation, AND a repair-retry loop that
feeds a validation failure back to the model to self-correct, all built and
tested for the MAS agents. Reusing it means the planner's fallback path only
has to handle a TOTAL failure (repairs exhausted), not the first parse
attempt — a strictly better position than the plan anticipated.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from agents import llm_utils

from .state import SubTask

logger = logging.getLogger(__name__)

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
- "price" tasks are for the stock itself: price level, return, moving
  averages, RSI, momentum/technicals. metric_or_topic describes what is
  asked; periods are ignored (the latest cached window is used).
- "insider" tasks are for insider buying/selling (Form 4) and 8-K events.
- Infer the ticker from the question; if genuinely ambiguous, use the ticker
  the caller supplied as ambient context.
- Never invent periods not implied by the question; default to the last 4
  reported quarters if the question doesn't name a period.
- Emit at least one sub_task. Never emit an empty list.
"""

_USER_TEMPLATE = """\
Question: {question}
Ambient ticker (use only if the question doesn't name one): {ambient_ticker}
"""


class _SubTaskModel(BaseModel):
    """Validates the LLM's own JSON — converted to the plain SubTask
    TypedDict (orchestration.state) before this module hands it to the graph."""
    kind: Literal["sql", "footnote", "search", "price", "insider"]
    ticker: str
    metric_or_topic: str
    periods: list[str] = Field(default_factory=list)


class _PlannerOutput(BaseModel):
    sub_tasks: list[_SubTaskModel]


def _fallback(question: str, ambient_ticker: str | None) -> list[SubTask]:
    """Degrade to a single plain hybrid-search sub_task covering the whole
    question verbatim — used when the model can't produce valid structured
    output even after llm_utils.generate_structured's own repair retries."""
    return [SubTask(
        kind="search", ticker=(ambient_ticker or "").strip().upper(),
        metric_or_topic=question, periods=[],
    )]


async def plan(question: str, ambient_ticker: str | None) -> list[SubTask]:
    """
    Decompose `question` into sub-tasks via one structured Gemini call (low
    temperature — this is a classification/extraction task, not creative
    writing). Falls back to a single "search" sub_task covering the whole
    question verbatim if the model can't produce valid JSON even after
    generate_structured's built-in repair attempts — degrading to plain
    hybrid search rather than failing the whole query.

    Never raises.
    """
    question = (question or "").strip()
    if not question:
        return _fallback(question, ambient_ticker)

    user_prompt = _USER_TEMPLATE.format(
        question=question, ambient_ticker=(ambient_ticker or "unknown"),
    )
    try:
        parsed = await llm_utils.generate_structured(
            _PLANNER_SYSTEM, user_prompt, _PlannerOutput,
            temperature=0.1, max_output_tokens=2048,
        )
    except Exception as e:  # noqa: BLE001 — planning failure degrades, never propagates
        logger.warning(f"[orchestration.planner] planning failed, falling back to search: {e}")
        return _fallback(question, ambient_ticker)

    if not parsed.sub_tasks:
        return _fallback(question, ambient_ticker)

    return [
        SubTask(
            kind=t.kind, ticker=(t.ticker or ambient_ticker or "").strip().upper(),
            metric_or_topic=t.metric_or_topic, periods=t.periods,
        )
        for t in parsed.sub_tasks
    ]
