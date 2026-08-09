"""
rag/embeddings.py
─────────────────
Text embeddings via Gemini's `gemini-embedding-001` model, reached over the same
REST surface + API key the chat/agents already use — no extra SDK.

Notes on this model:
  - It exposes only single-item `embedContent` (no synchronous batch endpoint),
    so `embed_texts` fans out concurrent single calls under a small semaphore.
  - Native dimensionality is 3072; we request `outputDimensionality=768` (a
    Matryoshka truncation) to keep vectors compact, and UNIT-NORMALIZE them so
    L2 distance in Chroma ranks the same as cosine similarity (Google recommends
    normalizing whenever the output dim is < 3072).

`task_type` matters for retrieval: index documents as "RETRIEVAL_DOCUMENT" and
search queries as "RETRIEVAL_QUERY" so they land in compatible sub-spaces.
"""

from __future__ import annotations

import asyncio
import logging
import math

import httpx

from gemini_chat import gemini_api_key

logger = logging.getLogger(__name__)

_EMBED_MODEL = "gemini-embedding-001"
_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_EMBED_MODEL}:embedContent"
)
_HTTP_TIMEOUT = 60.0

EMBED_DIM = 768

# Concurrency for embed_texts, and a light per-request retry for transient blips.
_CONCURRENCY = 8
_MAX_CHARS = 8_000        # the model truncates long inputs anyway; keep bodies sane
_RETRIES = 2


class EmbeddingError(RuntimeError):
    """Raised when the embedding API is unavailable or returns an error."""


def _require_key() -> str:
    key = gemini_api_key()
    if not key:
        raise EmbeddingError(
            "GEMINI_API_KEY is not set; embeddings (RAG) are unavailable."
        )
    return key


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


async def _embed_one(
    client: httpx.AsyncClient, key: str, text: str, task_type: str
) -> list[float]:
    body = {
        "model": f"models/{_EMBED_MODEL}",
        "content": {"parts": [{"text": (text or "")[:_MAX_CHARS]}]},
        "taskType": task_type,
        "outputDimensionality": EMBED_DIM,
    }
    last: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = await client.post(
                _EMBED_URL,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            last = EmbeddingError(f"Could not reach the embedding API: {e}")
        else:
            if resp.status_code == 200:
                values = (resp.json().get("embedding") or {}).get("values")
                if not values:
                    raise EmbeddingError("Embedding API returned no vector.")
                return _normalize(values)
            # 429 / 5xx are transient; retry with a short backoff.
            if resp.status_code == 429 or resp.status_code >= 500:
                last = EmbeddingError(
                    f"Embedding API transient error ({resp.status_code})."
                )
            else:
                raise EmbeddingError(
                    f"Embedding API error ({resp.status_code}): {resp.text[:300]}"
                )
        if attempt < _RETRIES:
            await asyncio.sleep(0.5 * (2 ** attempt))
    raise last or EmbeddingError("Embedding failed.")


async def embed_text(text: str, *, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    """Embed a single string into a unit-normalized 768-float vector."""
    key = _require_key()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        return await _embed_one(client, key, text, task_type)


async def embed_texts(
    texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
) -> list[list[float]]:
    """
    Embed many strings, one request each but with bounded concurrency. Returns
    one vector per input, in order. Raises EmbeddingError if any item fails.
    """
    if not texts:
        return []
    key = _require_key()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        async def one(t: str) -> list[float]:
            async with sem:
                return await _embed_one(client, key, t, task_type)

        return await asyncio.gather(*(one(t) for t in texts))
