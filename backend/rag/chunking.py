"""
rag/chunking.py
───────────────
Data-type-aware chunking for the vector store. Each source type has a natural
unit of meaning, so we split along it rather than blindly by character count:

  - Earnings calls → Q&A / speaker TURNS (so retrieval can target "what did
    management say about AI last quarter"), each turn capped so a long monologue
    still splits.
  - SEC filing text → SECTION units (MD&A, Risk Factors), sub-split when a
    section runs past the cap.
  - YouTube transcripts → semantic PARAGRAPHS (blank-line / size windows).

Every chunk is `{"text": str, "metadata": dict}`. Token counts are estimated as
~4 chars/token (good enough to size chunks and decide RAG activation).
"""

from __future__ import annotations

import re

# ~4 characters per token is a reliable rule of thumb for English prose.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (no tokenizer dependency)."""
    return len(text or "") // _CHARS_PER_TOKEN


def _cap_chars(tokens: int) -> int:
    return tokens * _CHARS_PER_TOKEN


# A speaker header in an earnings transcript, e.g.
#   "Tim Cook -- Chief Executive Officer"
#   "Operator"
#   "Amit Daryanani -- Evercore ISI -- Analyst"
_SPEAKER_RE = re.compile(
    r"^\s*(Operator|[A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3}\s+--\s+[^\n]+)\s*$",
    re.MULTILINE,
)


def _speaker_role(header: str) -> str:
    """Best-effort classification of a speaker header into a coarse role."""
    h = header.lower()
    if h.startswith("operator"):
        return "operator"
    if "analyst" in h:
        return "analyst"
    if any(t in h for t in ("chief", "ceo", "cfo", "president", "officer", "vp", "head of")):
        return "management"
    return "other"


def _split_oversized(text: str, cap_chars: int) -> list[str]:
    """Split a too-long block on paragraph, then sentence, then hard boundaries."""
    text = text.strip()
    if len(text) <= cap_chars:
        return [text] if text else []
    pieces: list[str] = []
    buf = ""
    # Prefer paragraph boundaries, fall back to sentences.
    units = re.split(r"\n\s*\n", text)
    if len(units) == 1:
        units = re.split(r"(?<=[.!?])\s+", text)
    for unit in units:
        if not unit.strip():
            continue
        if len(buf) + len(unit) + 1 > cap_chars and buf:
            pieces.append(buf.strip())
            buf = ""
        if len(unit) > cap_chars:
            # A single unit larger than the cap: hard-slice it.
            for i in range(0, len(unit), cap_chars):
                pieces.append(unit[i:i + cap_chars].strip())
        else:
            buf = f"{buf} {unit}".strip()
    if buf.strip():
        pieces.append(buf.strip())
    return [p for p in pieces if p]


def chunk_earnings_transcript(
    transcript: str, quarter: str, *, max_tokens: int = 1000, min_tokens: int = 40
) -> list[dict]:
    """
    Split an earnings-call transcript into speaker/Q&A turns.

    Falls back to size windows when the transcript has no recognizable speaker
    headers (some sources strip them). Tiny turns are merged forward so we don't
    emit dozens of one-line fragments.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return []
    cap = _cap_chars(max_tokens)
    min_chars = _cap_chars(min_tokens)

    matches = list(_SPEAKER_RE.finditer(transcript))
    turns: list[tuple[str, str]] = []  # (header, body)
    if matches:
        for i, m in enumerate(matches):
            header = m.group(1).strip()
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(transcript)
            body = transcript[body_start:body_end].strip()
            turns.append((header, body))
    else:
        # No speaker markup — window the whole thing.
        for piece in _split_oversized(transcript, cap):
            turns.append(("", piece))

    chunks: list[dict] = []
    idx = 0
    pending = ""  # merge-forward buffer for tiny turns
    pending_role = "other"
    for header, body in turns:
        role = _speaker_role(header) if header else "other"
        block = (f"{header}\n{body}" if header else body).strip()
        if not block:
            continue
        if len(block) < min_chars:
            pending = f"{pending}\n\n{block}".strip()
            pending_role = role
            continue
        if pending:
            block = f"{pending}\n\n{block}".strip()
            pending = ""
        for piece in _split_oversized(block, cap) or [block]:
            chunks.append({
                "text": piece,
                "metadata": {
                    "quarter": quarter,
                    "speaker_role": role or pending_role,
                    "chunk_index": idx,
                },
            })
            idx += 1
    if pending:
        chunks.append({
            "text": pending,
            "metadata": {"quarter": quarter, "speaker_role": pending_role, "chunk_index": idx},
        })
    return chunks


def chunk_sec_text(
    text: str, period: str, section_key: str, *, max_tokens: int = 2000
) -> list[dict]:
    """Section-level chunks; a section longer than the cap is sub-split."""
    cap = _cap_chars(max_tokens)
    pieces = _split_oversized((text or "").strip(), cap)
    return [
        {
            "text": piece,
            "metadata": {"period": period, "section_key": section_key, "chunk_index": i},
        }
        for i, piece in enumerate(pieces)
    ]


def chunk_youtube_transcript(
    text: str, channel: str, video_id: str, published: str | None,
    *, max_tokens: int = 500,
) -> list[dict]:
    """Semantic-ish paragraph chunks for a (often caption-noisy) transcript."""
    cap = _cap_chars(max_tokens)
    pieces = _split_oversized((text or "").strip(), cap)
    return [
        {
            "text": piece,
            "metadata": {
                "channel": channel,
                "video_id": video_id,
                "published": published or "",
                "chunk_index": i,
            },
        }
        for i, piece in enumerate(pieces)
    ]
