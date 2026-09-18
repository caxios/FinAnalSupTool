"""
orchestration.tools
──────────────────────
Thin adapter nodes: map one SubTask to a call into infrastructure already
built in Phases 1/3/4. No new retrieval logic here — just resolving the
planner's free-text `metric_or_topic` into the concrete arguments
services.structured_db and rag.hybrid_search actually need.
"""

from __future__ import annotations

import re

from providers import edgar_xbrl
from rag import hybrid_search
from rag.hybrid_search import SearchHit
from services import structured_db

from .state import SubTask

# Every (label, [concept aliases]) pair this app knows how to extract from
# XBRL, pooled across all three statements — the vocabulary sql_tool
# fuzzy-matches the planner's free-text metric_or_topic against, so the
# planner never needs to know exact XBRL tag names (see edgar_xbrl.py's own
# BALANCE_SHEET_CONCEPTS / INCOME_STATEMENT_CONCEPTS / CASH_FLOW_CONCEPTS).
_ALL_CONCEPT_DEFS: list[tuple[str, list[str]]] = [
    (label, [f"us-gaap:{alias}" for alias in aliases])
    for label, aliases, _kind in (
        edgar_xbrl.BALANCE_SHEET_CONCEPTS
        + edgar_xbrl.INCOME_STATEMENT_CONCEPTS
        + edgar_xbrl.CASH_FLOW_CONCEPTS
    )
]

_PERIOD_RE = re.compile(r"FY\s*(\d{4})(?:\s+(Q[1-4]))?", re.IGNORECASE)


def _resolve_concepts(metric_or_topic: str) -> list[str]:
    """
    Fuzzy/substring-match the planner's free-text metric description against
    the known statement-line-item labels, returning every us-gaap concept
    alias that label can resolve to — structured_db.financial_facts stores
    whichever ONE alias actually matched for a given period/company (see
    edgar_xbrl.facts_to_rows), so matching on the full alias set catches it
    regardless of which specific tag the company used.

    Exact label match wins first; otherwise substring match in either
    direction (e.g. "Data Center segment Revenue" contains the label
    "Revenue"). Returns [] when nothing matches — sql_tool treats that as
    "can't resolve this metric," not "return everything."
    """
    needle = (metric_or_topic or "").strip().lower()
    if not needle:
        return []
    for label, aliases in _ALL_CONCEPT_DEFS:
        if label.lower() == needle:
            return aliases
    matches: list[str] = []
    for label, aliases in _ALL_CONCEPT_DEFS:
        label_lower = label.lower()
        if label_lower in needle or needle in label_lower:
            matches.extend(aliases)
    return matches


def _parse_periods(periods: list[str]) -> tuple[list[int], list[str]]:
    """Extract distinct fiscal years and fiscal-period codes ('Q1'..'Q4'|'FY')
    from period strings like 'FY2025 Q1' or 'FY2025'. Entries that don't
    match the expected shape are silently skipped."""
    years: set[int] = set()
    fps: set[str] = set()
    for p in periods or []:
        m = _PERIOD_RE.search(p or "")
        if not m:
            continue
        years.add(int(m.group(1)))
        fps.add(m.group(2).upper() if m.group(2) else "FY")
    return sorted(years), sorted(fps)


def _to_period_key(period: str) -> str:
    """
    Convert the planner's 'FY2025 Q1' shape into structured_db's own
    period_key convention: 'Q1 FY2025' for a quarter, 'FY2025' for the
    annual — matching edgar_xbrl.get_period_label's fp-first format, which
    is what services.sec_ingest actually persists as period_key. Falls back
    to the input unchanged if it doesn't match the expected shape.
    """
    m = _PERIOD_RE.search(period or "")
    if not m:
        return period
    year, fp = m.group(1), m.group(2)
    return f"{fp.upper()} FY{year}" if fp else f"FY{year}"


async def sql_tool(task: SubTask) -> list[dict]:
    """
    structured_db.query_facts scoped to this sub_task's ticker, resolved
    concepts, and parsed fiscal years/periods. An unresolved ticker or metric
    returns [] rather than an unfiltered dump of every fact the company has
    ever reported — an honest "couldn't resolve this" is safer than flooding
    Phase 6's verified-facts table with noise.
    """
    ticker = (task.get("ticker") or "").strip().upper()
    if not ticker:
        return []
    concepts = _resolve_concepts(task.get("metric_or_topic", ""))
    if not concepts:
        return []
    fiscal_years, fiscal_periods = _parse_periods(task.get("periods") or [])
    return structured_db.query_facts(
        ticker, concepts=concepts,
        fiscal_years=fiscal_years or None,
        fiscal_periods=fiscal_periods or None,
    )


async def footnote_tool(task: SubTask) -> list[dict]:
    """
    structured_db.query_footnotes for this sub_task's ticker + first named
    period (a footnote lookup is inherently one-period-at-a-time — "why did
    Long-term Debt change" asks about one filing's notes, not a time series).
    Returns [] when the sub_task names no period at all.
    """
    ticker = (task.get("ticker") or "").strip().upper()
    periods = task.get("periods") or []
    if not ticker or not periods:
        return []
    period_key = _to_period_key(periods[0])
    return structured_db.query_footnotes(
        ticker, period_key, statement_item=task.get("metric_or_topic") or None,
    )


async def price_tool(task: SubTask) -> list[dict]:
    """
    The most recently cached price/technicals window for this ticker
    (services.price_cache — written by the Data tab's "Price & Technicals"
    fetch). Returns [] when nothing has been fetched for the ticker.
    """
    from services import price_cache

    ticker = (task.get("ticker") or "").strip().upper()
    if not ticker:
        return []
    ranges = price_cache.list_cached_ranges(ticker)
    if not ranges:
        return []
    start, end = ranges[-1]
    data = price_cache.get_price_data(ticker, start, end)
    if not data:
        return []
    return [{"period_start": start, "period_end": end, **data}]


async def insider_tool(task: SubTask) -> list[dict]:
    """
    Form 4 insider trades + 8-K filing rows for this ticker
    (services.insider_cache — written by the Data tab's "Other SEC" fetch),
    newest first and capped: this feeds a prompt, not a table view.
    """
    from services import insider_cache

    ticker = (task.get("ticker") or "").strip().upper()
    if not ticker:
        return []
    rows: list[dict] = []
    for tr in (insider_cache.get_insider_trades(ticker) or [])[:20]:
        rows.append({"row_type": "form4", **tr})
    for f in (insider_cache.get_8k_filings(ticker) or [])[:10]:
        rows.append({"row_type": "8k", **f})
    return rows


async def search_tool(task: SubTask) -> list[SearchHit]:
    """rag.hybrid_search.search scoped to this sub_task's ticker, with
    reranking on — this is the tool feeding the final top-5 excerpts into
    Phase 6's context builder, so it should be the best-ranked set available,
    not the raw RRF order."""
    query = (task.get("metric_or_topic") or "").strip()
    if not query:
        return []
    ticker = (task.get("ticker") or "").strip().upper() or None
    return await hybrid_search.search(
        query, ticker=ticker, doc_types=task.get("doc_types") or None,
        rerank=True, rerank_top_n=5,
    )
