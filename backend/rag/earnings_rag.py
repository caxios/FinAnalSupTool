"""
rag/earnings_rag.py
───────────────────
RAG path for the Earnings Call Agent, used only for LONG analysis periods.

Short periods stuff every transcript in full (simpler, equally good). For long
periods that path balloons the prompt, so instead we:

  1. Feed the CURRENT (most recent) quarter's transcript in full — it carries the
     most signal and is what everything else is compared against.
  2. Index the OLDER quarters' transcripts as chunked turns, then retrieve only
     the chunks semantically relevant to a fixed set of high-signal topics.

The retrieval is SCOPED to this run (ticker + run_id) so a shared collection
never leaks one company's or one run's chunks into another's context.
"""

from __future__ import annotations

import logging

from . import chunking, vector_store

logger = logging.getLogger(__name__)

# Topics we always want tracked across prior quarters. Retrieval pulls the most
# relevant chunk(s) per topic from each older quarter.
_QUERY_TOPICS = [
    "revenue growth and forward guidance",
    "gross margin and profitability",
    "AI, new products, and strategic investments",
    "capital allocation, buybacks, and dividends",
    "demand trends and end markets",
    "risks, headwinds, and competitive pressure",
]

_PER_TOPIC = 3            # chunks retrieved per topic
_MAX_RETRIEVED = 36       # overall cap on retrieved historical chunks

# The current quarter is passed IN FULL whenever it fits the window (it carries
# the most signal and is the baseline everything else is compared against). Only
# a pathologically large current transcript is itself RAG-retrieved instead of
# being truncated — so the crucial late-call Q&A is never dropped by a static
# character cap. ~40k tokens ≈ 160k chars, well above a normal transcript.
_CURRENT_FULL_TOKENS = 40_000
_CURRENT_PER_TOPIC = 4    # current-quarter chunks retrieved per topic (RAG path)
_CURRENT_MAX_RETRIEVED = 28


async def prepare_context(
    fetched: list[tuple[str, str, str]], *, ticker: str | None, run_id: str
) -> str | None:
    """
    Build a RAG earnings context.

    Args:
        fetched: chronological list of (quarter_label, source, transcript_text),
                 oldest → newest.
        ticker:  used (with run_id) to scope retrieval to this run.
        run_id:  unique id for this analysis, so indexing/query don't collide.

    Returns the assembled context string, or None if RAG couldn't be used (store
    unavailable, indexing/embeddings failed) so the caller can fall back to full
    context stuffing.
    """
    if not vector_store.is_available() or len(fetched) < 2:
        return None

    scope = f"{ticker or 'NA'}:{run_id}"
    *older, current = fetched
    cur_q, cur_src, cur_text = current

    # Index EVERY quarter's chunks (scoped to this run), current included, so the
    # current transcript can also be retrieved from if it's too large to stuff.
    indexed_any = False
    for qlabel, _src, text in fetched:
        chunks = chunking.chunk_earnings_transcript(text, qlabel)
        for c in chunks:
            c["metadata"]["scope"] = scope
        written = await vector_store.index_chunks(
            "earnings_transcripts", chunks,
            id_prefix=f"{scope}:{qlabel}".replace(" ", "_"),
        )
        indexed_any = indexed_any or written > 0
    if not indexed_any:
        return None

    # ── Current quarter: full transcript when it fits, else its own topic-based
    # retrieval (NEVER a truncation, so the end of the call is never lost). ──
    if chunking.estimate_tokens(cur_text) <= _CURRENT_FULL_TOKENS:
        parts: list[str] = [
            f"=== CURRENT QUARTER: {cur_q} (source: {cur_src or 'unknown'}) — FULL TRANSCRIPT ===\n"
            f"{cur_text}\n=== END {cur_q} ==="
        ]
    else:
        cur_seen: set[str] = set()
        cur_hits: list[dict] = []
        for topic in _QUERY_TOPICS:
            hits = await vector_store.query(
                "earnings_transcripts", topic, n_results=_CURRENT_PER_TOPIC,
                where={"$and": [{"scope": scope}, {"quarter": cur_q}]},
            )
            for h in hits:
                key = h["text"][:120]
                if key in cur_seen or len(cur_hits) >= _CURRENT_MAX_RETRIEVED:
                    continue
                cur_seen.add(key)
                h["_topic"] = topic
                cur_hits.append(h)
        cur_body = "\n".join(
            f"[topic: {h['_topic']}] {h['text']}" for h in cur_hits
        ) or cur_text[:_CURRENT_FULL_TOKENS * 4]  # last-resort head slice if retrieval empty
        parts = [
            f"=== CURRENT QUARTER: {cur_q} (source: {cur_src or 'unknown'}) — "
            f"KEY EXCERPTS (full transcript too large to include verbatim; "
            f"chunked and retrieved by topic) ===\n"
            f"{cur_body}\n=== END {cur_q} ==="
        ]
        logger.info(
            f"Earnings RAG: current quarter {cur_q} too large "
            f"(~{chunking.estimate_tokens(cur_text):,} est. tokens) — "
            f"retrieved {len(cur_hits)} current-quarter chunk(s)."
        )

    # Retrieve topic-relevant chunks from the older quarters only.
    seen: set[str] = set()
    retrieved: list[dict] = []
    for topic in _QUERY_TOPICS:
        hits = await vector_store.query(
            "earnings_transcripts", topic, n_results=_PER_TOPIC,
            where={"$and": [{"scope": scope}, {"quarter": {"$ne": cur_q}}]},
        )
        for h in hits:
            key = h["text"][:120]
            if key in seen or len(retrieved) >= _MAX_RETRIEVED:
                continue
            seen.add(key)
            h["_topic"] = topic
            retrieved.append(h)

    if retrieved:
        # Group retrieved chunks by quarter for a readable historical section.
        by_quarter: dict[str, list[dict]] = {}
        for h in retrieved:
            q = h["metadata"].get("quarter", "prior quarter")
            by_quarter.setdefault(q, []).append(h)
        parts.append(
            "\n=== RELEVANT EXCERPTS FROM PRIOR QUARTERS "
            "(retrieved by topic via semantic search) ==="
        )
        for q, hits in by_quarter.items():
            parts.append(f"\n--- {q} ---")
            for h in hits:
                parts.append(f"[topic: {h['_topic']}] {h['text']}")
        parts.append("=== END PRIOR-QUARTER EXCERPTS ===")

    logger.info(
        f"Earnings RAG: current={cur_q}, older={len(older)} quarter(s), "
        f"retrieved {len(retrieved)} historical chunk(s)."
    )
    return "\n".join(parts)
