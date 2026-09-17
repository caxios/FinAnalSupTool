"""
rag.hybrid_search
───────────────────
The one thing neither store from Phase 1/2 does alone: a single ranked
result combining keyword precision (BM25 via services.search_index — exact
tickers, dollar figures, proper nouns) with semantic recall (vectors via
rag.vector_store — "AI demand" matching "custom silicon ramp"), fused by
Reciprocal Rank Fusion (RRF).

Both underlying calls already degrade gracefully on their own (search_bm25
catches its own SQLite errors and returns []; vector_store.query catches its
own embedding/Chroma errors and returns []), so this module doesn't need its
own exception handling around them — a store being unavailable just means
that half of the fusion contributes nothing, not a crash.

Nothing calls search() yet outside this module's own manual verification —
Phase 5 (the LangGraph tool router) is the first real caller. See
implementation_plan/new_db_architecture_plan/Phase3_Hybrid_Retrieval.md.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from providers import rerank_provider
from rag import vector_store
from services import search_index

logger = logging.getLogger(__name__)

# Phase 2's fixed doc_type vocabulary -> the Chroma collection it lives in
# (rag/vector_store.py's own _COLLECTIONS dict, spelled slightly differently
# — "sec_filing_text" the doc_type vs. "sec_filings_text" the collection).
# analysis_history is deliberately excluded: it's zero-vector upserted and
# metadata-filtered only (see vector_store.upsert_record), not a document
# type this search surfaces.
_DOC_TYPE_TO_COLLECTION = {
    "sec_filing_text": "sec_filings_text",
    "earnings_transcript": "earnings_transcripts",
    "news_article": "news_articles",
    "youtube_transcript": "youtube_transcripts",
}

# Standard RRF constant — higher flattens the weighting across ranks (rank 1
# vs rank 20 matters less), lower sharpens it toward top ranks mattering a
# lot more. 60 is the conventional default from the original RRF paper.
_RRF_K = 60


@dataclass
class SearchHit:
    doc_id: str
    text: str
    metadata: dict
    bm25_rank: int | None      # 1-based rank in the BM25 list, or None if absent from it
    vector_rank: int | None    # 1-based rank in the vector list, or None if absent from it
    rrf_score: float


def _build_where(ticker: str | None, period: str | None) -> dict | None:
    """
    Chroma `where` filter for the vector leg. KNOWN LIMITATION: `period` only
    reliably filters sec_filing_text and news_article chunks — earnings
    transcript chunks (rag/chunking.chunk_earnings_transcript, unchanged
    from before this architecture) tag their period under a `quarter` key,
    not `period`, in the Chroma metadata (services.search_index.index_chunk
    already normalizes this into ONE `period` column on the BM25 side, but
    fixing the Chroma metadata itself is a Phase 2 chunking concern, not
    this phase's). A period filter combined with doc_types=["earnings_transcript"]
    will therefore silently return nothing from the vector leg — pass
    doc_types without period for earnings until that's addressed.
    """
    clauses = []
    if ticker:
        clauses.append({"ticker": ticker.strip().upper()})
    if period:
        clauses.append({"period": period})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


async def search(
    query: str,
    *,
    ticker: str | None = None,
    doc_types: list[str] | None = None,
    period: str | None = None,
    k: int = 20,
    rerank: bool = False,
    rerank_top_n: int = 5,
) -> list[SearchHit]:
    """
    rag.hybrid_search's public entry point: RRF-fused results (_fused_search),
    optionally narrowed by a cross-encoder rerank pass.

    rerank=True asks providers.rerank_provider to re-score the fused
    candidates against the query directly (something neither BM25 nor vector
    similarity does — each scores a document independently of the others).
    Off by default so this phase's own verification and Phase 3's already
    stay unaffected; Phase 5's tool router is expected to pass rerank=True.

    Degrades exactly like every other provider in this codebase: no
    COHERE_API_KEY, an empty candidate list, or a live API failure all fall
    back to the plain RRF order (top rerank_top_n) rather than raising or
    returning nothing — see providers.rerank_provider.rerank's own
    never-raises contract.
    """
    hits = await _fused_search(query, ticker=ticker, doc_types=doc_types, period=period, k=k)
    if not rerank or not hits:
        return hits

    result = await rerank_provider.rerank(query, [h.text for h in hits], top_n=rerank_top_n)
    if not result.configured or not result.ranked:
        logger.info(
            f"[hybrid_search] rerank skipped ({result.message or 'not configured'}) "
            f"— using RRF order."
        )
        return hits[:rerank_top_n]
    return [hits[i] for i, _score in result.ranked]


async def _fused_search(
    query: str,
    *,
    ticker: str | None = None,
    doc_types: list[str] | None = None,
    period: str | None = None,
    k: int = 20,
) -> list[SearchHit]:
    """
    Run BM25 and vector search IN PARALLEL, then fuse by Reciprocal Rank
    Fusion:

        rrf_score(doc) = sum over each ranked list L containing doc of
                         1 / (_RRF_K + rank_in_L(doc))

    A doc present in both lists scores higher than one in only one list, but
    a doc need only appear in ONE list to surface at all — BM25 misses
    paraphrases, vectors miss exact numbers/tickers, so requiring both would
    throw away real hits either side alone would have found.

    doc_types narrows which Chroma collections + which FTS5 doc_type values
    are searched (Phase 2's fixed vocabulary — see _DOC_TYPE_TO_COLLECTION).
    Omit (or pass an unrecognized value) to search everything recognized.

    Returns the top-k hits sorted by rrf_score descending, deduplicated by
    doc_id — the shared key search_index.search_bm25 and vector_store.query
    both return for the identical underlying chunk (see Phase 0's "stable
    chunk IDs" convention).
    """
    types = [t for t in (doc_types or _DOC_TYPE_TO_COLLECTION) if t in _DOC_TYPE_TO_COLLECTION]
    if not types:
        return []

    where = _build_where(ticker, period)
    bm25_task = asyncio.to_thread(
        search_index.search_bm25, query, ticker=ticker, doc_type=None, k=k * 2
    )
    vector_tasks = [
        vector_store.query(_DOC_TYPE_TO_COLLECTION[t], query, n_results=k * 2, where=where)
        for t in types
    ]
    bm25_hits, *vector_results = await asyncio.gather(bm25_task, *vector_tasks)
    bm25_hits = [h for h in bm25_hits if h["metadata"].get("doc_type") in types]

    bm25_rank = {h["doc_id"]: i + 1 for i, h in enumerate(bm25_hits)}
    vector_rank: dict[str, int] = {}
    all_docs: dict[str, dict] = {h["doc_id"]: h for h in bm25_hits}
    for vres in vector_results:
        for i, h in enumerate(vres):
            doc_id = h.get("doc_id")
            if not doc_id:
                continue  # defensive — vector_store.query() always sets this, but never trust silently
            vector_rank.setdefault(doc_id, i + 1)
            all_docs.setdefault(doc_id, {"doc_id": doc_id, "text": h["text"], "metadata": h["metadata"]})

    scored: list[SearchHit] = []
    for doc_id, doc in all_docs.items():
        score = 0.0
        if doc_id in bm25_rank:
            score += 1.0 / (_RRF_K + bm25_rank[doc_id])
        if doc_id in vector_rank:
            score += 1.0 / (_RRF_K + vector_rank[doc_id])
        scored.append(SearchHit(
            doc_id=doc_id, text=doc["text"], metadata=doc["metadata"],
            bm25_rank=bm25_rank.get(doc_id), vector_rank=vector_rank.get(doc_id),
            rrf_score=score,
        ))
    scored.sort(key=lambda h: h.rrf_score, reverse=True)
    return scored[:k]
