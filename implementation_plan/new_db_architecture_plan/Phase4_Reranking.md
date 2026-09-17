# Phase 4 — Cross-Encoder Reranking (Cohere Rerank)

RRF fusion (Phase 3) gives a decent top-k, but it's still rank-fusion over two
first-stage retrievers — neither actually reads the query and the document
together. A cross-encoder reranker does, and is what narrows ~20-40 candidates
down to the 3-5 that actually go in the LLM's context. Small phase — one new
provider module, one integration point.

---

## [NEW] `backend/providers/rerank_provider.py`

Modeled on this codebase's existing provider pattern (`news_provider.py`,
`youtube_provider.py`): a thin `httpx` wrapper, a `*_api_key()` getter reading
from `.env`, a typed result dataclass, graceful "not configured" degradation
(never raises for a missing key — callers fall back to the pre-rerank order).

```python
_API_URL = "https://api.cohere.com/v1/rerank"
_MODEL = "rerank-v3.5"

def cohere_api_key() -> str | None:
    return os.environ.get("COHERE_API_KEY")

@dataclass
class RerankResult:
    configured: bool
    ranked: list[tuple[int, float]] = field(default_factory=list)  # (original_index, relevance_score)
    message: str | None = None

async def rerank(query: str, documents: list[str], *, top_n: int = 5) -> RerankResult:
    """
    POST to Cohere's rerank endpoint: {model, query, documents, top_n}.
    Returns RerankResult(configured=False, message=...) if COHERE_API_KEY is
    unset — never raises for that case, matching news_provider's pattern.
    A live API failure (rate limit, timeout) DOES raise (or returns
    configured=True with an empty ranked list + message) — the caller decides
    whether to fall back to the pre-rerank RRF order; see integration below.
    """
```

## [MODIFY] `backend/.env.example` (or wherever the other keys are documented)

Add `COHERE_API_KEY=` alongside `GEMINI_API_KEY`, `TAVILY_API_KEY`,
`YOUTUBE_API_KEY`.

---

## [MODIFY] `backend/rag/hybrid_search.py`

Add an optional reranking pass, off by default at the fusion layer itself
(callers opt in) so Phase 3's own verification/debug endpoint keeps working
unchanged if Cohere isn't configured:

```python
async def search(
    query: str, *, ticker=None, doc_types=None, period=None, k=20,
    rerank: bool = False, rerank_top_n: int = 5,
) -> list[SearchHit]:
    hits = await _fused_search(query, ticker=ticker, doc_types=doc_types, period=period, k=k)
    if not rerank or not hits:
        return hits
    result = await rerank_provider.rerank(query, [h.text for h in hits], top_n=rerank_top_n)
    if not result.configured or not result.ranked:
        logger.info("Rerank skipped (not configured or empty result) — using RRF order.")
        return hits[:rerank_top_n]
    return [hits[i] for i, _score in result.ranked]
```

This keeps the "graceful degradation" property every other provider in this
codebase has: no Cohere key → hybrid search still works, just without the
final cross-encoder pass, exactly like news/YouTube already degrade when
their keys are missing.

---

## Execution Order

| Step | Description |
|---|---|
| 1 | Add `COHERE_API_KEY` to `.env` (get a key — Cohere has a free tier sufficient for this) |
| 2 | Create `providers/rerank_provider.py` |
| 3 | Add `rerank` param to `hybrid_search.search()` |
| 4 | Manually compare `/debug/search?q=...&rerank=false` vs `rerank=true` on the same query |

## Verification Plan

- With no `COHERE_API_KEY` set: `search(..., rerank=True)` returns the same
  order as `rerank=False` (graceful degradation, not an error).
- With a real key: construct a query where the RRF top-1 is a mediocre match
  and a lower-ranked RRF hit is actually the best answer (easy to engineer —
  e.g. a query using different vocabulary than the top RRF hit but exactly
  matching a hit ranked #4-5). Confirm reranking promotes the better one to
  the top — this is the actual point of the phase, so verify it does
  something, not just that it runs without erroring.
- Latency check: log and eyeball the added round-trip time (Cohere rerank is
  typically <500ms for ~20 documents) — this sits in the hot path of every
  query once Phase 5 wires it in, so it needs to stay fast enough for an
  interactive chat response.
