"""
rag/sec_rag.py
──────────────
RAG path for SEC filing TEXT sections (MD&A, Risk Factors, Business, …).

Filing text is enormous — MD&A and Risk Factors alone run tens of thousands of
words per filing, and a multi-period analysis stacks several. The old pipeline
hard-capped each section at 12k characters, silently dropping whatever came
after (segment detail late in the MD&A, the tail of Risk Factors). Instead we:

  - Pass every section IN FULL when the combined text still fits the model's
    optimal window (simpler, and best for quality).
  - Otherwise chunk EVERY section and retrieve only the chunks semantically
    relevant to the caller's queries (the SEC agent's fixed analysis topics, or
    the chat user's actual question). Nothing is lost by POSITION — the whole
    document is indexed — only by RELEVANCE to what's being asked.

Retrieval is SCOPED (ticker + run_id) so one company's or one run's chunks never
leak into another's context. Any failure (store unavailable, embedding/indexing
error) returns None, so the caller cleanly falls back to full-text stuffing.
"""

from __future__ import annotations

import logging

from . import chunking, vector_store

logger = logging.getLogger(__name__)

# Analysis topics the SEC agent always wants covered when RAG is engaged. These
# mirror the fundamental lenses in the agent's rubric, so retrieval surfaces the
# passages it actually reasons over (segments, margins, cash flow, leverage,
# outlook, risks).
SEC_ANALYSIS_TOPICS = [
    "revenue, segment performance, and geographic breakdown",
    "gross margin, operating margin, and specific cost drivers",
    "cash flow from operations, free cash flow, and capital expenditures",
    "debt, liquidity, interest expense, and coverage",
    "management outlook, guidance, and demand trends",
    "risk factors, litigation, and competitive pressure",
]

# Above this combined size (across all sections and periods), stuffing every
# section in full wastes the window; retrieve the relevant chunks instead. Below
# it, full text is both cheaper for quality and simpler. ~50k tokens ≈ 200k chars
# — comfortably within Gemini's window while covering typical 1-3 filing runs in
# full and only engaging RAG for large multi-period text.
RAG_THRESHOLD_TOKENS = 50_000

_PER_QUERY = 4          # chunks retrieved per query topic
_MAX_RETRIEVED = 48     # overall cap on retrieved chunks


def _iter_sections(text_store: dict, ordered: list[str]):
    """Yield (period_key, section_key, non-empty text) in period order."""
    for pk in ordered:
        sections = text_store.get(pk, {}) or {}
        for section_key, content in sections.items():
            text = (content or "").strip()
            if text:
                yield pk, section_key, text


def estimate_total_tokens(text_store: dict, ordered: list[str]) -> int:
    """Estimated token size of ALL filing text sections combined."""
    return sum(
        chunking.estimate_tokens(text) for _pk, _sk, text in _iter_sections(text_store, ordered)
    )


async def prepare_context(
    text_store: dict,
    ordered: list[str],
    *,
    queries: list[str],
    ticker: str | None,
    run_id: str,
) -> str | None:
    """
    Build a RAG filing-text block from the section store.

    Args:
        text_store: {period_key: {section_key: text}} — the parsed sections.
        ordered:    period keys in chronological order (for stable grouping).
        queries:    what to retrieve for — the agent's analysis topics, or the
                    chat user's question(s).
        ticker/run_id: scope the index + retrieval to this run.

    Returns the assembled excerpts block, or None when RAG was NOT used (store
    unavailable, text small enough to pass in full, or indexing/retrieval
    yielded nothing) — the caller then renders the sections in full.
    """
    if not vector_store.is_available():
        return None
    if estimate_total_tokens(text_store, ordered) < RAG_THRESHOLD_TOKENS:
        return None

    scope = f"{ticker or 'NA'}:{run_id}"

    # Index every section's chunks (scoped to this run). The whole document is
    # indexed, so retrieval can reach any passage regardless of position.
    indexed_any = False
    for pk, section_key, text in _iter_sections(text_store, ordered):
        chunks = chunking.chunk_sec_text(text, pk, section_key)
        for c in chunks:
            c["metadata"]["scope"] = scope
        written = await vector_store.index_chunks(
            "sec_filings_text", chunks,
            id_prefix=f"{scope}:{pk}:{section_key}".replace(" ", "_"),
        )
        indexed_any = indexed_any or written > 0
    if not indexed_any:
        return None

    # Retrieve the chunks most relevant to each query, de-duplicated and capped.
    seen: set[str] = set()
    retrieved: list[dict] = []
    for topic in queries:
        if not (topic or "").strip():
            continue
        hits = await vector_store.query(
            "sec_filings_text", topic, n_results=_PER_QUERY,
            where={"scope": scope},
        )
        for h in hits:
            key = h["text"][:120]
            if key in seen or len(retrieved) >= _MAX_RETRIEVED:
                continue
            seen.add(key)
            h["_topic"] = topic
            retrieved.append(h)

    if not retrieved:
        return None

    # Group retrieved chunks by period → section for a readable, attributed block.
    from parsers.pdf_utils import SECTION_LABELS  # local import: avoids parser deps at import time

    order_index = {pk: i for i, pk in enumerate(ordered)}
    by_period: dict[str, list[dict]] = {}
    for h in retrieved:
        pk = h["metadata"].get("period", "unknown period")
        by_period.setdefault(pk, []).append(h)

    parts: list[str] = [
        "# Filing Text — RELEVANT EXCERPTS",
        "(The full filing text exceeded the context budget, so the sections were "
        "chunked and only the passages semantically relevant to the analysis were "
        "retrieved. Treat absence of a detail here as 'not retrieved', not "
        "'not disclosed'.)",
        "",
    ]
    for pk in sorted(by_period, key=lambda p: order_index.get(p, 10_000)):
        parts.append(f"## Filing Text — {pk}")
        for h in by_period[pk]:
            label = SECTION_LABELS.get(h["metadata"].get("section_key", ""), "Filing Text")
            parts.append(f"### {label}  [retrieved for: {h['_topic']}]")
            parts.append(h["text"])
            parts.append("")

    logger.info(
        f"SEC filings RAG: indexed sections for {scope}, retrieved "
        f"{len(retrieved)} chunk(s) across {len(by_period)} period(s)."
    )
    return "\n".join(parts)
