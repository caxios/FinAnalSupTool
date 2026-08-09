"""
services.media_service
───────────────────────
Turn the cached media/macro data (fetched by the Company Media & Macro views)
into a Markdown context block for the AI assistant, so it can answer questions
across all views. Only includes what the user has actually fetched this session.
"""

from __future__ import annotations

from schemas import NewsResponse, VideoResponse, SentimentResponse
from services.storage import MediaCache


def build_media_context(cache: MediaCache) -> str:
    """Render the cached media/macro data as a Markdown block for the assistant."""
    data = cache.data
    parts: list[str] = []

    cn: NewsResponse | None = data.get("company_news")
    if cn and cn.articles:
        parts.append("# Company News (recent, from web search)")
        for a in cn.articles[:10]:
            parts.append(f"- [{a.source}] {a.title} — {a.snippet[:200]}")
        parts.append("")

    mn: NewsResponse | None = data.get("macro_news")
    if mn and mn.articles:
        parts.append("# Macro / Market News (recent)")
        for a in mn.articles[:10]:
            parts.append(f"- [{a.source}] {a.title} — {a.snippet[:200]}")
        parts.append("")

    sent: SentimentResponse | None = data.get("sentiment")
    if sent and sent.configured and sent.summary:
        parts.append(
            f"# Market Sentiment: {sent.label} "
            f"({sent.score if sent.score is not None else 'n/a'}/100)"
        )
        parts.append(sent.summary)
        for ind in sent.indicators:
            parts.append(f"- {ind.theme} ({ind.direction}): {ind.note}")
        parts.append("")

    for scope_key in ("company_videos", "macro_videos"):
        vr: VideoResponse | None = data.get(scope_key)
        if vr and vr.videos:
            label = "Company" if scope_key == "company_videos" else "Macro"
            parts.append(f"# {label} Analysis Videos")
            for v in vr.videos[:8]:
                parts.append(f"- {v.title} — {v.channel}")
            parts.append("")

    transcripts: dict = data.get("transcripts", {})
    if transcripts:
        parts.append("# Video Transcript Excerpts")
        for vid, info in list(transcripts.items())[:5]:
            excerpt = info.get("text", "")[:400]
            if excerpt:
                parts.append(f"- ({vid}) {excerpt}")
        parts.append("")

    return "\n".join(parts)
