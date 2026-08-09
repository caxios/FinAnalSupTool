"""
rag/vector_store.py
───────────────────
ChromaDB-backed vector store for long-period analysis and analysis history.

Collections:
  - earnings_transcripts : chunked earnings-call turns, keyed by quarter
  - sec_filings_text     : MD&A / Risk Factors section chunks
  - youtube_transcripts  : video transcript chunks
  - analysis_history     : past MAS run summaries (for semantic recall)

Two design choices keep this robust:
  1. ChromaDB is an OPTIONAL dependency. If it isn't installed (or fails to
     initialize), `is_available()` returns False and every operation degrades to
     a no-op / empty result — the app still runs, RAG just stays off.
  2. We ALWAYS supply our own Gemini embeddings on add/query, so Chroma never
     invokes (or downloads) its default local embedding model.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import embeddings as _emb

logger = logging.getLogger(__name__)

_CHROMA_PATH = str(Path(__file__).parent.parent / "chroma_data")

_COLLECTIONS = {
    "earnings_transcripts": "Earnings call transcript chunks by quarter",
    "sec_filings_text": "SEC filing MD&A / Risk Factors section chunks",
    "youtube_transcripts": "YouTube analyst video transcript chunks",
    "analysis_history": "Past MAS analysis run summaries",
}

# Lazily initialized so importing this module never fails or does I/O.
_client = None
_init_tried = False
_available = False


def _init() -> bool:
    """Initialize the Chroma client on first use. Returns availability."""
    global _client, _init_tried, _available
    if _init_tried:
        return _available
    _init_tried = True
    try:
        import chromadb
        from chromadb.config import Settings

        _client = chromadb.PersistentClient(
            path=_CHROMA_PATH,
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )
        _available = True
        logger.info(f"ChromaDB vector store ready at {_CHROMA_PATH}")
    except Exception as e:  # noqa: BLE001 — any failure just disables RAG
        logger.warning(f"ChromaDB unavailable — RAG/vector features disabled: {e}")
        _client = None
        _available = False
    return _available


def is_available() -> bool:
    """True when the vector store can be used (Chroma installed + initialized)."""
    return _init()


def _get_collection(name: str):
    if not _init() or _client is None:
        return None
    try:
        return _client.get_or_create_collection(
            name=name, metadata={"description": _COLLECTIONS.get(name, name)}
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not open collection '{name}': {e}")
        return None


def _clean_meta(meta: dict) -> dict:
    """Chroma metadata values must be str/int/float/bool — drop None, coerce rest."""
    out: dict = {}
    for k, v in (meta or {}).items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out or {"_": ""}


async def index_chunks(collection_name: str, chunks: list[dict], id_prefix: str) -> int:
    """
    Embed and upsert a list of `{"text", "metadata"}` chunks into a collection.

    Ids are deterministic (`{id_prefix}-{i}`) so re-indexing the same source
    upserts rather than duplicating. Returns the number of chunks written (0 if
    the store is unavailable or there's nothing to index).
    """
    if not chunks:
        return 0
    col = _get_collection(collection_name)
    if col is None:
        return 0
    texts = [c["text"] for c in chunks]
    try:
        vectors = await _emb.embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")
    except _emb.EmbeddingError as e:
        logger.warning(f"Embedding failed for '{collection_name}': {e}")
        return 0
    ids = [f"{id_prefix}-{i}" for i in range(len(chunks))]
    metadatas = [_clean_meta(c.get("metadata", {})) for c in chunks]
    try:
        col.upsert(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Upsert into '{collection_name}' failed: {e}")
        return 0
    return len(chunks)


async def query(
    collection_name: str, query_text: str, *, n_results: int = 5, where: dict | None = None
) -> list[dict]:
    """
    Semantic search. Returns `[{"text", "metadata", "distance"}]`, closest first.
    Empty list when the store is unavailable or the collection is empty.
    """
    col = _get_collection(collection_name)
    if col is None or not query_text.strip():
        return []
    try:
        qvec = await _emb.embed_text(query_text, task_type="RETRIEVAL_QUERY")
    except _emb.EmbeddingError as e:
        logger.warning(f"Query embedding failed: {e}")
        return []
    try:
        res = col.query(
            query_embeddings=[qvec], n_results=n_results,
            where=where or None, include=["documents", "metadatas", "distances"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Query on '{collection_name}' failed: {e}")
        return []

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    out: list[dict] = []
    for i, doc in enumerate(docs):
        out.append({
            "text": doc,
            "metadata": metas[i] if i < len(metas) else {},
            "distance": dists[i] if i < len(dists) else None,
        })
    return out


def upsert_record(collection_name: str, doc_id: str, text: str, metadata: dict) -> bool:
    """
    Store a single already-textual record WITHOUT embeddings (metadata-only
    recall / listing). Used by the history store, which primarily filters by
    ticker rather than semantic distance. Returns success.
    """
    col = _get_collection(collection_name)
    if col is None:
        return False
    try:
        # A zero vector keeps Chroma happy without invoking any embedding model;
        # history recall is metadata-filtered, not distance-ranked.
        col.upsert(
            ids=[doc_id], embeddings=[[0.0] * _emb.EMBED_DIM],
            documents=[text or ""], metadatas=[_clean_meta(metadata)],
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"History upsert failed: {e}")
        return False
