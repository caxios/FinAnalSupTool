"""
providers.rerank_provider
────────────────────────────
Cross-encoder reranking via the Cohere Rerank API — the pass that actually
reads the query and a candidate document TOGETHER (unlike BM25/vector
first-stage retrieval, which score each independently), used to narrow
rag.hybrid_search's ~20-40 RRF candidates down to the 3-5 that actually
belong in an LLM's context.

Configuration
─────────────
  COHERE_API_KEY  (required for live reranking)

Graceful degradation
─────────────────────
Mirrors every other provider in this codebase (news_provider, youtube_provider):
when the key is missing, `rerank()` returns a `RerankResult` with
`configured=False` and a helpful message instead of raising — callers fall
back to whatever order they already had. A live API failure (rate limit,
timeout, malformed response) is handled the SAME way — `configured=True` but
`ranked=[]` plus a message — so a caller never needs its own try/except
around this to stay safe; see rag/hybrid_search.py's integration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://api.cohere.com/v1/rerank"
_MODEL = "rerank-v3.5"
_HTTP_TIMEOUT = 15.0  # this sits in the hot path of an interactive query — fail fast


def cohere_api_key() -> str | None:
    key = os.environ.get("COHERE_API_KEY", "").strip()
    return key or None


@dataclass
class RerankResult:
    """A rerank outcome, including a not-configured / error state."""
    configured: bool
    ranked: list[tuple[int, float]] = field(default_factory=list)  # (original_index, relevance_score), best first
    message: str | None = None


async def rerank(query: str, documents: list[str], *, top_n: int = 5) -> RerankResult:
    """
    Cross-encoder rerank `documents` against `query` via Cohere's
    /v1/rerank endpoint, returning the top_n (original_index, relevance_score)
    pairs sorted best-first.

    Never raises: a missing key, an empty `documents`/`query`, a live API
    failure (rate limit, timeout, non-200, malformed response) all come back
    as a RerankResult the caller can safely treat uniformly — check
    `.configured` and `.ranked`, never a try/except.
    """
    if not documents or not (query or "").strip():
        return RerankResult(configured=bool(cohere_api_key()), message="Nothing to rerank.")

    key = cohere_api_key()
    if not key:
        return RerankResult(
            configured=False,
            message="Reranking is not configured: set COHERE_API_KEY on the backend.",
        )

    body = {
        "model": _MODEL,
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                _API_URL, json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001 — a rerank failure degrades, never propagates
        logger.warning(f"[rerank_provider] Cohere rerank failed: {e}")
        return RerankResult(configured=True, message=f"Rerank request failed: {e}")

    try:
        ranked = [
            (int(r["index"]), float(r["relevance_score"]))
            for r in data.get("results", [])
        ]
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"[rerank_provider] Cohere response shape unexpected: {e}")
        return RerankResult(configured=True, message=f"Unexpected rerank response shape: {e}")

    if not ranked:
        return RerankResult(configured=True, message="Cohere returned no ranked results.")
    return RerankResult(configured=True, ranked=ranked)
