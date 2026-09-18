"""
services.doc_indexer
─────────────────────
Gets a ticker's fetched documents INTO the searchable DB tier — the vector
store (Chroma) and the BM25 index (services.search_index) — under stable,
reusable ids, so retrieval can find them from any later question.

Two entry points:
  - index_filing_sections() — one filing's extracted text sections. Called at
    ingestion time (services.ingestion) so a filing is searchable as soon as
    it lands.
  - ensure_indexed()        — backfill for a ticker whose documents were
    fetched BEFORE this indexing existed (or by a path that doesn't index),
    checked cheaply against chunk_registry so it's a no-op once done.

Deliberately distinct from rag/sec_rag.py, which indexes the same filing text
per ANALYSIS RUN (``{ticker}:{run_id}``) for one pipeline's token budget.
These chunks are scoped ``{TICKER}_{period}_{section}`` — written once, reused
by every future question, which is what lets the chat assistant answer about a
company that never went through Deep Analysis.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Chroma collection backing the "sec_filing_text" doc_type (rag/hybrid_search.py
# maps between the two spellings).
_SEC_COLLECTION = "sec_filings_text"


def _prefix(ticker: str, period_key: str, section_key: str) -> str:
    """Stable id prefix. vector_store.index_chunks appends its own `-{i}`, and
    the BM25 side reuses the SAME id — that shared doc_id is the join key RRF
    fusion needs (a mismatch here silently halves every fused result)."""
    return f"{ticker}_{period_key}_{section_key}_sec".replace(" ", "_")


async def index_filing_sections(
    ticker: str, period_key: str, sections: dict[str, str | None]
) -> int:
    """
    Index one filing's text sections (MD&A, Risk Factors, footnotes, …) into
    both stores. Returns the number of chunks written.

    Best-effort: logs and returns 0 on any failure rather than raising —
    indexing must never fail the ingestion that produced the text.
    """
    from rag import chunking, vector_store
    from services import search_index

    ticker = (ticker or "").strip().upper()
    if not ticker or not sections:
        return 0

    written = 0
    for section_key, text in sections.items():
        if not text or not str(text).strip():
            continue
        try:
            chunks = chunking.chunk_sec_text(str(text), period_key, section_key)
            if not chunks:
                continue
            for c in chunks:
                c["metadata"]["ticker"] = ticker
                c["metadata"]["doc_type"] = "sec_filing_text"
            prefix = _prefix(ticker, period_key, section_key)
            await vector_store.index_chunks(_SEC_COLLECTION, chunks, id_prefix=prefix)
            for i, c in enumerate(chunks):
                search_index.index_chunk(
                    f"{prefix}-{i}", c["text"], ticker, "sec_filing_text", c["metadata"]
                )
            written += len(chunks)
        except Exception as e:  # noqa: BLE001 — indexing is best-effort
            logger.warning(
                f"[doc_indexer] filing-text indexing failed for "
                f"{ticker} {period_key}/{section_key}: {e}"
            )
    if written:
        logger.info(f"[doc_indexer] indexed {written} filing-text chunk(s) for {ticker} {period_key}")
    return written


async def ensure_indexed(ticker: str, store=None) -> int:
    """
    Backfill anything this ticker has on disk but not yet in the search index:
    filing text (from the live CompanyStore, or rehydrated from filing_cache)
    and cached earnings-call transcripts.

    Cheap to call on every question: each period/quarter is checked against
    chunk_registry first, so a fully-indexed ticker does no work at all.
    Returns the number of chunks newly written.
    """
    from services import filing_cache, research_copilot, search_index, transcript_cache

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return 0
    written = 0

    # ── Filing text ──
    company = None
    if store is not None:
        if not store.has_company(ticker):
            filing_cache.rehydrate_company_store(ticker, store)
        if store.has_company(ticker):
            company = store.get_company_store(ticker)
    if company is not None:
        for period_key, sections in (company.text_store or {}).items():
            if not sections:
                continue
            # One probe per period: any section already indexed means this
            # period was done (they're written together).
            if any(
                search_index.has_chunks(_prefix(ticker, period_key, sk))
                for sk in sections
            ):
                continue
            written += await index_filing_sections(ticker, period_key, sections)

    # ── Earnings-call transcripts cached by the Data tab ──
    for qk in transcript_cache.list_cached_quarters(ticker):
        try:
            year, quarter = int(qk[:4]), int(qk[5])
        except (ValueError, IndexError):
            continue
        if search_index.has_chunks(f"{ticker}_{qk}_earnings"):
            continue
        doc = transcript_cache.get_transcript(ticker, year, quarter)
        if doc and doc.found and doc.text:
            await research_copilot.index_earnings_transcript(ticker, year, quarter, doc.text)
            written += 1

    return written
