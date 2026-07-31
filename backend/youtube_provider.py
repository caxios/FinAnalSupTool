"""
youtube_provider.py
───────────────────
YouTube video discovery + transcript retrieval.

  - Video search:  YouTube Data API v3 (needs YOUTUBE_API_KEY)
  - Transcripts:   youtube-transcript-api (no key; scrapes public captions)

Used by View 2 (company analysis videos) and View 3 (macro/economic channels).

Configuration
─────────────
  YOUTUBE_API_KEY   (required for video search)
  YOUTUBE_CHANNELS  (optional) comma-separated channel IDs for the macro feed

Graceful degradation
─────────────────────
Missing key → `VideoResult(configured=False, message=...)`. Transcripts work
independently of the key (they don't use the Data API).
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_HTTP_TIMEOUT = 30.0


def youtube_api_key() -> str | None:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    return key or None


def macro_channel_ids() -> list[str]:
    """Optional curated channel IDs for the macro video feed (View 3)."""
    raw = os.environ.get("YOUTUBE_CHANNELS", "").strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


# =============================================================================
# Result types
# =============================================================================

@dataclass
class Video:
    video_id: str
    title: str
    channel: str
    published: str | None = None
    thumbnail: str | None = None
    description: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube.com/embed/{self.video_id}"


@dataclass
class VideoResult:
    configured: bool
    videos: list[Video] = field(default_factory=list)
    message: str | None = None


@dataclass
class TranscriptResult:
    available: bool
    text: str = ""
    message: str | None = None


# =============================================================================
# Video search (YouTube Data API v3)
# =============================================================================

async def search_videos(
    query: str,
    *,
    channel_id: str | None = None,
    max_results: int = 6,
) -> VideoResult:
    """Search YouTube for videos matching `query` (optionally within a channel)."""
    api_key = youtube_api_key()
    if not api_key:
        return VideoResult(
            configured=False,
            message="Videos are not configured: set YOUTUBE_API_KEY on the "
                    "backend to enable YouTube video search.",
        )

    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "type": "video",
        "maxResults": max_results,
        "order": "relevance",
        "relevanceLanguage": "en",
        "safeSearch": "none",
    }
    if channel_id:
        params["channelId"] = channel_id

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_SEARCH_URL, params=params)
    except httpx.HTTPError as e:
        logger.error(f"YouTube request failed: {e}")
        return VideoResult(configured=True, message=f"Could not reach YouTube: {e}")

    if resp.status_code != 200:
        detail = resp.text
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        logger.error(f"YouTube API error {resp.status_code}: {detail}")
        return VideoResult(
            configured=True,
            message=f"YouTube API error ({resp.status_code}): {detail}",
        )

    data = resp.json()
    videos: list[Video] = []
    for item in data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = item.get("snippet", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
        videos.append(
            Video(
                video_id=vid,
                title=sn.get("title", "").strip(),
                channel=sn.get("channelTitle", "").strip(),
                published=sn.get("publishedAt"),
                thumbnail=thumb,
                description=sn.get("description", "").strip(),
            )
        )

    return VideoResult(configured=True, videos=videos)


async def search_company_videos(company: str, ticker: str | None = None) -> VideoResult:
    label = f"{company} {ticker}".strip() if ticker else company
    return await search_videos(f"{label} stock analysis earnings", max_results=6)


async def search_macro_videos(max_results: int = 6) -> VideoResult:
    """Macro/economic videos — from curated channels if set, else a broad query."""
    channels = macro_channel_ids()
    if channels:
        # Pull a couple of recent videos from each curated channel.
        combined = VideoResult(configured=True, videos=[])
        for cid in channels[:4]:
            r = await search_videos(
                "market outlook economy", channel_id=cid, max_results=3
            )
            if not r.configured:
                return r  # not configured — surface immediately
            combined.videos.extend(r.videos)
        return combined
    return await search_videos(
        "stock market analysis macroeconomic outlook this week",
        max_results=max_results,
    )


# =============================================================================
# Transcripts (youtube-transcript-api — no key)
# =============================================================================

def get_transcript(video_id: str) -> TranscriptResult:
    """
    Fetch a video's caption transcript as plain text.

    Handles both the older (`YouTubeTranscriptApi.get_transcript`) and newer
    (instance `.fetch`) styles of youtube-transcript-api so we don't break on a
    version bump. Captions are often unavailable — that's a normal outcome, not
    an error.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return TranscriptResult(
            available=False,
            message="Transcript library not installed (youtube-transcript-api).",
        )

    segments = None
    try:
        # Newer (1.x) instance API
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        segments = [
            {"text": getattr(s, "text", "") if not isinstance(s, dict) else s.get("text", "")}
            for s in fetched
        ]
    except Exception:
        segments = None

    if segments is None:
        try:
            # Older (0.6.x) static API
            segments = YouTubeTranscriptApi.get_transcript(video_id)
        except Exception as e:
            return TranscriptResult(
                available=False,
                message=f"No transcript available for this video ({e.__class__.__name__}).",
            )

    text = " ".join(
        (seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")).strip()
        for seg in segments
    ).strip()

    if not text:
        return TranscriptResult(available=False, message="Transcript was empty.")
    return TranscriptResult(available=True, text=text)
