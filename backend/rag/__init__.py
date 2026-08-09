"""
rag
───
Retrieval-Augmented Generation, analysis history, and 3-axis gap scoring.

Everything here is OPTIONAL and degrades gracefully: if ChromaDB isn't installed
or embeddings can't be reached, `vector_store.is_available()` is False, RAG stays
off, and the app falls back to context stuffing. Disk-based analysis history and
the (pure-Python) 3-axis scoring work regardless.
"""

from __future__ import annotations

from . import chunking, embeddings, history_store, scoring, vector_store, earnings_rag
from .scoring import compute_three_axis_scores, interpret_signal, SIGNAL_LABELS

# RAG activates only when the combined data volume is large enough that context
# stuffing becomes wasteful (~2-3 quarters of full transcripts).
RAG_THRESHOLD_TOKENS = 150_000


def should_use_rag(total_estimated_tokens: int) -> bool:
    """True when RAG retrieval is worth it AND the vector store is available."""
    return (
        total_estimated_tokens >= RAG_THRESHOLD_TOKENS
        and vector_store.is_available()
    )


def estimate_tokens(text: str) -> int:
    return chunking.estimate_tokens(text)


__all__ = [
    "chunking",
    "embeddings",
    "history_store",
    "scoring",
    "vector_store",
    "earnings_rag",
    "compute_three_axis_scores",
    "interpret_signal",
    "SIGNAL_LABELS",
    "RAG_THRESHOLD_TOKENS",
    "should_use_rag",
    "estimate_tokens",
]
