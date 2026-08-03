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
import re
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
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
    language: str | None = None
    message: str | None = None


@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    handle: str | None = None


@dataclass
class ChannelResolveResult:
    ok: bool
    channel: ChannelInfo | None = None
    message: str | None = None


# =============================================================================
# Video search (YouTube Data API v3)
# =============================================================================

async def search_videos(
    query: str = "",
    *,
    channel_id: str | None = None,
    max_results: int = 9,
    published_after: str | None = None,
    published_before: str | None = None,
) -> VideoResult:
    """
    Search YouTube for videos matching `query` (optionally within a channel).

    `query` may be empty when `channel_id` is given — then we simply browse the
    channel's newest uploads. `published_after` / `published_before` are RFC-3339
    timestamps (e.g. "2024-01-01T00:00:00Z") that restrict results to a window.
    """
    api_key = youtube_api_key()
    if not api_key:
        return VideoResult(
            configured=False,
            message="Videos are not configured: set YOUTUBE_API_KEY on the "
                    "backend to enable YouTube video search.",
        )

    query = (query or "").strip()
    # Sort by newest when browsing a channel or filtering by date; else by relevance.
    order = (
        "date"
        if (published_after or published_before or (channel_id and not query))
        else "relevance"
    )
    base_params = {
        "key": api_key,
        "part": "snippet",
        "type": "video",
        "order": order,
        "safeSearch": "none",
    }
    # Only bias toward English for keyword searches; channel browses (e.g. a
    # Korean channel) must not be filtered by language or they come back empty.
    if query:
        base_params["q"] = query
        base_params["relevanceLanguage"] = "en"
    if channel_id:
        base_params["channelId"] = channel_id
    if published_after:
        base_params["publishedAfter"] = published_after
    if published_before:
        base_params["publishedBefore"] = published_before

    # The Data API returns at most 50 items per page; paginate to reach
    # `max_results` (so a channel with lots of uploads isn't capped at one page).
    videos: list[Video] = []
    page_token: str | None = None
    remaining = max_results
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            while remaining > 0:
                params = dict(base_params)
                params["maxResults"] = min(remaining, 50)
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(_SEARCH_URL, params=params)

                if resp.status_code != 200:
                    detail = resp.text
                    try:
                        detail = resp.json().get("error", {}).get("message", detail)
                    except Exception:
                        pass
                    logger.error(f"YouTube API error {resp.status_code}: {detail}")
                    # If we already gathered some videos, return those.
                    if videos:
                        break
                    return VideoResult(
                        configured=True,
                        message=f"YouTube API error ({resp.status_code}): {detail}",
                    )

                data = resp.json()
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

                remaining = max_results - len(videos)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
    except httpx.HTTPError as e:
        logger.error(f"YouTube request failed: {e}")
        if not videos:
            return VideoResult(configured=True, message=f"Could not reach YouTube: {e}")

    return VideoResult(configured=True, videos=videos[:max_results])


async def search_company_videos(
    company: str,
    ticker: str | None = None,
    *,
    max_results: int = 9,
    published_after: str | None = None,
    published_before: str | None = None,
) -> VideoResult:
    label = f"{company} {ticker}".strip() if ticker else company
    return await search_videos(
        f"{label} stock analysis earnings",
        max_results=max_results,
        published_after=published_after,
        published_before=published_before,
    )


async def search_macro_videos(
    *,
    max_results: int = 9,
    published_after: str | None = None,
    published_before: str | None = None,
) -> VideoResult:
    """Macro/economic videos — from curated channels if set, else a broad query."""
    channels = macro_channel_ids()
    if channels:
        # Pull a few recent videos from each curated channel.
        combined = VideoResult(configured=True, videos=[])
        for cid in channels[:4]:
            r = await search_videos(
                "market outlook economy",
                channel_id=cid,
                max_results=3,
                published_after=published_after,
                published_before=published_before,
            )
            if not r.configured:
                return r  # not configured — surface immediately
            combined.videos.extend(r.videos)
        return combined
    return await search_videos(
        "stock market analysis macroeconomic outlook",
        max_results=max_results,
        published_after=published_after,
        published_before=published_before,
    )


# =============================================================================
# Channel resolution (URL / @handle / ID → channelId + title)
# =============================================================================

def _parse_channel_input(raw: str) -> dict:
    """
    Parse a user-provided channel reference into resolution hints.

    Accepts a channel URL, an @handle, a bare UC… id, or a plain name.
    Returns one of: {"id"|"handle"|"username"|"query": value}.
    """
    s = raw.strip()

    # Full/partial YouTube URL
    m = re.search(
        r"youtube\.com/(?:channel/(UC[\w-]{22})|@([\w.\-]+)|user/([\w.\-]+)|c/([\w.\-]+))",
        s,
    )
    if m:
        if m.group(1):
            return {"id": m.group(1)}
        if m.group(2):
            return {"handle": m.group(2)}
        if m.group(3):
            return {"username": m.group(3)}
        if m.group(4):
            return {"query": m.group(4)}

    if s.startswith("@"):
        return {"handle": s[1:]}
    if re.match(r"^UC[\w-]{22}$", s):
        return {"id": s}
    # Bare token with no spaces → likely a handle; otherwise a search query.
    return {"handle": s} if (" " not in s and s) else {"query": s}


async def resolve_channel(raw: str) -> ChannelResolveResult:
    """
    Resolve a channel reference (URL / @handle / UC id / name) to a
    {channel_id, title} pair via the YouTube Data API.

    Without an API key we can still accept an explicit UC… channel id (title
    falls back to the id); names/handles need the key to resolve.
    """
    hint = _parse_channel_input(raw)
    api_key = youtube_api_key()

    if not api_key:
        if "id" in hint:
            return ChannelResolveResult(
                ok=True, channel=ChannelInfo(hint["id"], hint["id"], None)
            )
        return ChannelResolveResult(
            ok=False,
            message="Set YOUTUBE_API_KEY to add channels by name/handle/URL. "
                    "You can still add a raw channel ID (starts with 'UC').",
        )

    async def _channels_lookup(param: str, value: str) -> ChannelInfo | None:
        params = {"key": api_key, "part": "snippet", param: value}
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(_CHANNELS_URL, params=params)
            if resp.status_code != 200:
                return None
            items = resp.json().get("items", [])
            if items:
                it = items[0]
                return ChannelInfo(
                    it["id"], it["snippet"]["title"],
                    hint.get("handle"),
                )
        except httpx.HTTPError:
            return None
        return None

    # Direct lookups first
    if "id" in hint:
        ch = await _channels_lookup("id", hint["id"])
        if ch:
            return ChannelResolveResult(ok=True, channel=ch)
    if "handle" in hint:
        ch = await _channels_lookup("forHandle", hint["handle"])
        if ch:
            return ChannelResolveResult(ok=True, channel=ch)
    if "username" in hint:
        ch = await _channels_lookup("forUsername", hint["username"])
        if ch:
            return ChannelResolveResult(ok=True, channel=ch)

    # Fallback: search for a channel by name
    q = hint.get("query") or hint.get("handle") or raw
    try:
        params = {
            "key": api_key, "part": "snippet", "type": "channel",
            "q": q, "maxResults": 1,
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_SEARCH_URL, params=params)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if items:
                it = items[0]
                cid = it["id"]["channelId"]
                title = it["snippet"]["title"]
                return ChannelResolveResult(
                    ok=True, channel=ChannelInfo(cid, title, hint.get("handle"))
                )
    except httpx.HTTPError as e:
        return ChannelResolveResult(ok=False, message=f"Could not reach YouTube: {e}")

    return ChannelResolveResult(
        ok=False, message=f"No channel found for '{raw}'."
    )


# =============================================================================
# Transcripts (youtube-transcript-api — no key)
# =============================================================================

# Languages we try first; anything else the video offers is used as a fallback,
# so Korean / other non-English captions (e.g. 한경글로벌마켓) still come through.
_PREFERRED_LANGS = ("en", "en-US", "en-GB", "ko")


def _fetched_to_text(fetched) -> str:
    """Join a FetchedTranscript (or list of dict/segment) into plain text."""
    return " ".join(
        (seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")).strip()
        for seg in fetched
    ).strip()


def get_transcript(video_id: str) -> TranscriptResult:
    """
    Fetch a video's full caption transcript as plain text, in whatever language
    the video offers.

    youtube-transcript-api 1.x's `fetch()` defaults to English only, so for a
    Korean (or other non-English) video it raises NoTranscriptFound. We instead
    enumerate the available transcripts via `list()` and pick the best language
    (preferring English/Korean, then falling back to anything available —
    manually-created captions preferred over auto-generated). Captions being
    unavailable is a normal outcome, not an error.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return TranscriptResult(
            available=False,
            message="Transcript library not installed (youtube-transcript-api).",
        )

    api = YouTubeTranscriptApi()

    # Enumerate available caption tracks and choose one.
    try:
        transcript_list = api.list(video_id)
    except Exception as e:
        return TranscriptResult(
            available=False,
            message=f"No transcript available for this video ({e.__class__.__name__}).",
        )

    available = list(transcript_list)
    if not available:
        return TranscriptResult(
            available=False, message="This video has no captions."
        )

    codes = [t.language_code for t in available]
    # Preferred languages first, then every remaining track as a fallback.
    order = [c for c in _PREFERRED_LANGS if c in codes]
    order += [c for c in codes if c not in order]

    chosen = None
    try:
        chosen = transcript_list.find_transcript(order)
    except Exception:
        chosen = available[0]

    try:
        fetched = chosen.fetch()
    except Exception as e:
        return TranscriptResult(
            available=False,
            message=f"Could not fetch the transcript ({e.__class__.__name__}).",
        )

    text = _fetched_to_text(fetched)
    if not text:
        return TranscriptResult(available=False, message="Transcript was empty.")
    return TranscriptResult(
        available=True, text=text, language=getattr(chosen, "language_code", None)
    )
