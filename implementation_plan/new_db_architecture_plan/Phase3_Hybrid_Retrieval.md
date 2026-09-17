# Phase 3 — Hybrid Retrieval (BM25 + Vector, fused via RRF)

Phase 2 fills two parallel indexes (SQLite FTS5 + ChromaDB) with the same
`doc_id`-keyed chunks. This phase adds the one thing neither store does
alone: a single ranked result combining keyword precision (BM25 — exact
tickers, dollar figures, proper nouns) with semantic recall (vectors —
"AI demand" matching "custom silicon ramp").

---

## [NEW] `backend/rag/hybrid_search.py`

```python
@dataclass
class SearchHit:
    doc_id: str
    text: str
    metadata: dict
    bm25_rank: int | None      # 1-based rank in the BM25 list, or None if absent from it
    vector_rank: int | None    # 1-based rank in the vector list, or None if absent from it
    rrf_score: float


_RRF_K = 60  # standard RRF constant; higher = flatter weighting across ranks


async def search(
    query: str,
    *,
    ticker: str | None = None,
    doc_types: list[str] | None = None,   # e.g. ["earnings_transcript", "news_article"]
    period: str | None = None,
    k: int = 20,
) -> list[SearchHit]:
    """
    Run BM25 (search_index.search_bm25, sync — wrapped in asyncio.to_thread)
    and vector (vector_store.query, per doc_type collection) IN PARALLEL via
    asyncio.gather, then fuse by Reciprocal Rank Fusion:

        rrf_score(doc) = sum over each ranked list L containing doc of
                         1 / (_RRF_K + rank_in_L(doc))

    A doc present in both lists scores higher than one in only one list, but
    a doc need only appear in ONE list to surface at all — this is what makes
    hybrid search more robust than either alone (BM25 misses paraphrases,
    vectors miss exact numbers/tickers).

    doc_types maps to which Chroma collections + which FTS5 doc_type filter
    to query (Phase 2's fixed vocabulary: sec_filing_text, earnings_transcript,
    news_article, youtube_transcript). Omit for "search everything."

    Returns the top-k hits sorted by rrf_score descending, deduplicated by
    doc_id (the shared key between both indexes — see Phase 0's conventions).
    """
```

Implementation sketch:

```python
async def search(query, *, ticker=None, doc_types=None, period=None, k=20) -> list[SearchHit]:
    types = doc_types or list(_DOC_TYPE_TO_COLLECTION)
    bm25_task = asyncio.to_thread(
        search_index.search_bm25, query, ticker=ticker, doc_type=None, k=k * 2
    )
    vector_tasks = [
        vector_store.query(_DOC_TYPE_TO_COLLECTION[t], query, n_results=k * 2,
                            where=_build_where(ticker, period))
        for t in types
    ]
    bm25_hits, *vector_results = await asyncio.gather(bm25_task, *vector_tasks)
    bm25_hits = [h for h in bm25_hits if h["metadata"]["doc_type"] in types]

    bm25_rank = {h["doc_id"]: i + 1 for i, h in enumerate(bm25_hits)}
    vector_rank: dict[str, int] = {}
    all_docs: dict[str, dict] = {h["doc_id"]: h for h in bm25_hits}
    for vres in vector_results:
        for i, h in enumerate(vres):
            doc_id = h["metadata"]["doc_id"]
            vector_rank.setdefault(doc_id, i + 1)
            all_docs.setdefault(doc_id, {"doc_id": doc_id, "text": h["text"], "metadata": h["metadata"]})

    scored = []
    for doc_id, doc in all_docs.items():
        score = 0.0
        if doc_id in bm25_rank:
            score += 1.0 / (_RRF_K + bm25_rank[doc_id])
        if doc_id in vector_rank:
            score += 1.0 / (_RRF_K + vector_rank[doc_id])
        scored.append(SearchHit(doc_id, doc["text"], doc["metadata"],
                                 bm25_rank.get(doc_id), vector_rank.get(doc_id), score))
    scored.sort(key=lambda h: h.rrf_score, reverse=True)
    return scored[:k]
```

`_DOC_TYPE_TO_COLLECTION` maps the fixed doc_type vocabulary to Chroma
collection names (`sec_filing_text` → `sec_filings_text`, etc. — reuse
`rag/vector_store.py`'s existing `_COLLECTIONS`).

---

## [MODIFY] `backend/services/search_index.py`

`search_bm25` needs to return each hit's `doc_id` and enough metadata to
match `hybrid_search`'s expectations — join `chunk_fts` (for the BM25 rank via
SQLite's built-in `bm25()` ranking function) against `chunk_registry` (for
`metadata_json`) in one query:

```sql
SELECT f.doc_id, f.text, r.metadata_json, bm25(chunk_fts) AS score
FROM chunk_fts f JOIN chunk_registry r ON r.doc_id = f.doc_id
WHERE chunk_fts MATCH ? AND (?  IS NULL OR f.ticker = ?)
ORDER BY score LIMIT ?
```

(SQLite's `bm25()` returns *lower is better* — sort ascending, and note this
when computing `bm25_rank` above: rank 1 = lowest score, i.e. best match.)

---

## Where this plugs in

Nothing calls `hybrid_search.search()` yet — that's Phase 5 (the LangGraph
tool router) and, optionally, a very small manual test endpoint for this
phase's own verification (see below). This phase is retrieval infrastructure
only; it doesn't change any user-facing behavior on its own.

## [NEW, verification-only] `GET /debug/search`

A minimal, temporary router endpoint (or a standalone script — either is
fine, this does not need to ship) purely to manually exercise the fusion
before Phase 5 wires it into the agent graph:

```python
@router.get("/debug/search")
async def debug_search(q: str, ticker: str | None = None, k: int = 10):
    hits = await hybrid_search.search(q, ticker=ticker, k=k)
    return [{"doc_id": h.doc_id, "score": round(h.rrf_score, 4),
             "bm25_rank": h.bm25_rank, "vector_rank": h.vector_rank,
             "text": h.text[:200]} for h in hits]
```

## Execution Order

| Step | Description |
|---|---|
| 1 | Extend `search_index.search_bm25` to join against `chunk_registry` and return `doc_id` |
| 2 | Create `rag/hybrid_search.py` with the RRF fusion |
| 3 | Add the temporary `/debug/search` endpoint |
| 4 | Run Phase 2's ingestion for one real ticker, then exercise `/debug/search` manually |

## Verification Plan

- **Keyword-only case**: search for an exact dollar figure or a specific
  proper noun that appears in only one earnings chunk. Confirm it surfaces
  even though a paraphrase-based vector search alone might rank it low —
  proves BM25 is actually contributing, not just vectors doing all the work.
- **Semantic-only case**: search a paraphrase ("chip demand from cloud
  providers") that doesn't share exact words with the source text ("hyperscale
  AI accelerator orders"). Confirm it still surfaces — proves the vector leg
  is contributing.
- **Fusion case**: a query where the best chunk appears in BOTH lists at
  different ranks; confirm its `rrf_score` is higher than a chunk appearing
  in only one list at rank 1 (sanity-checks the RRF formula itself, not just
  that results come back).
- Confirm `ticker` filtering actually excludes another company's chunks
  (cross-ticker leakage would be a real bug, not just noise).
