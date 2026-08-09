"""
routers.media
──────────────
Company identity, company/macro media (news + videos + transcripts), macro
sentiment, and saved-channel management:

  GET  /company
  GET  /media/news, /media/videos, /media/transcript, /media/earnings
  GET  /macro/news, /macro/videos, /macro/sentiment
  GET/POST/DELETE/PATCH /channels

Company derivation lives in ``services.company_service``; the fetched results are
cached in the injected ``MediaCache`` so the AI assistant can reference them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, HTTPException

from schemas import (
    CompanyResponse,
    NewsArticleModel,
    NewsResponse,
    VideoModel,
    VideoResponse,
    TranscriptResponse,
    EarningsResponse,
    SentimentIndicatorModel,
    SentimentResponse,
    ChannelModel,
    ChannelsResponse,
    AddChannelRequest,
)
from providers import news_provider, youtube_provider
import channel_store
from market_sentiment import compute_market_sentiment
from services.storage import (
    DocumentStore,
    MediaCache,
    get_document_store,
    get_media_cache,
)
from services import company_service

router = APIRouter(tags=["media"])


# =============================================================================
# Helpers
# =============================================================================

def _news_range_kwargs(days: int | None, start: str | None, end: str | None) -> dict:
    """
    Normalize a range selection into news-provider kwargs.

    A custom window (start/end, YYYY-MM-DD) takes precedence over the relative
    `days` look-back used by the preset ranges (1D/1W/1M/3M/6M/1Y).
    """
    if start or end:
        return {"days": None, "start_date": start, "end_date": end}
    return {"days": days, "start_date": None, "end_date": None}


def _video_time_bounds(
    days: int | None, start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """Translate a range selection into YouTube publishedAfter/Before (RFC-3339)."""
    if start or end:
        after = f"{start}T00:00:00Z" if start else None
        before = f"{end}T23:59:59Z" if end else None
        return after, before
    if days:
        now = datetime.now(timezone.utc)
        after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Pass an explicit upper bound too, so the window is [now-days, now].
        before = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return after, before
    return None, None


async def _videos_from_channels(
    query: str,
    channel_ids: list[str],
    after: str | None,
    before: str | None,
    per_channel: int = 25,
):
    """
    Merge videos across several saved channels (newest first).

    Returns a youtube_provider.VideoResult; surfaces a not-configured result
    immediately if the API key is missing.
    """
    combined = youtube_provider.VideoResult(configured=True, videos=[])
    for cid in channel_ids[:6]:
        r = await youtube_provider.search_videos(
            query, channel_id=cid, max_results=per_channel,
            published_after=after, published_before=before,
        )
        if not r.configured:
            return r
        combined.videos.extend(r.videos)
    combined.videos.sort(key=lambda v: v.published or "", reverse=True)
    return combined


def _news_models(articles) -> list[NewsArticleModel]:
    return [
        NewsArticleModel(
            title=a.title, url=a.url, source=a.source,
            snippet=a.snippet, published=a.published,
        )
        for a in articles
    ]


def _video_models(videos) -> list[VideoModel]:
    return [
        VideoModel(
            video_id=v.video_id, title=v.title, channel=v.channel,
            url=v.url, embed_url=v.embed_url, thumbnail=v.thumbnail,
            published=v.published, description=v.description,
        )
        for v in videos
    ]


def _channel_models(scope: str) -> list[ChannelModel]:
    return [
        ChannelModel(
            channel_id=c["channel_id"], title=c.get("title", c["channel_id"]),
            handle=c.get("handle"),
        )
        for c in channel_store.list_channels(scope)
    ]


def _valid_scope(scope: str) -> str:
    if scope not in channel_store.SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope '{scope}'. Must be one of: {list(channel_store.SCOPES)}",
        )
    return scope


# =============================================================================
# GET /company
# =============================================================================

@router.get("/company", response_model=CompanyResponse)
async def get_company(store: DocumentStore = Depends(get_document_store)):
    """Return the company/companies derived from the uploaded filings."""
    return company_service.derive_companies(store)


# =============================================================================
# GET /media/news  (company-specific)
# =============================================================================

@router.get("/media/news", response_model=NewsResponse)
async def media_news(
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(30, ge=1, le=30, description="Max articles (<=30)"),
    store: DocumentStore = Depends(get_document_store),
    cache: MediaCache = Depends(get_media_cache),
):
    """Recent news for the uploaded company (Tavily, finance domains)."""
    primary = company_service.primary_company(store)
    if primary is None or not (primary.name or primary.ticker):
        return NewsResponse(
            configured=bool(news_provider.tavily_api_key()),
            scope="company",
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )
    result = await news_provider.search_company_news(
        primary.name or primary.ticker or "", primary.ticker,
        max_results=max_results, **_news_range_kwargs(days, start, end),
    )
    resp = NewsResponse(
        configured=result.configured, scope="company", company=primary,
        articles=_news_models(result.articles), message=result.message,
    )
    cache.data["company_news"] = resp
    return resp


# =============================================================================
# GET /media/videos  (company-specific)
# =============================================================================

@router.get("/media/videos", response_model=VideoResponse)
async def media_videos(
    channel_id: str | None = Query(None, description="Saved channel id, or omit/'all'"),
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(25, ge=1, le=50, description="Max videos"),
    store: DocumentStore = Depends(get_document_store),
    cache: MediaCache = Depends(get_media_cache),
):
    """
    YouTube analysis videos for the uploaded company.

    - `channel_id` set → videos from that channel matching the company.
    - omitted / "all" → merged across saved channels (company-filtered); if no
      channels are saved, falls back to a keyword search by company name.
    """
    primary = company_service.primary_company(store)
    if primary is None or not (primary.name or primary.ticker):
        return VideoResponse(
            configured=bool(youtube_provider.youtube_api_key()),
            scope="company",
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )
    label = primary.name or primary.ticker or ""
    after, before = _video_time_bounds(days, start, end)

    if channel_id and channel_id != "all":
        result = await youtube_provider.search_videos(
            label, channel_id=channel_id, max_results=max_results,
            published_after=after, published_before=before,
        )
    else:
        saved = channel_store.channel_ids("company")
        if saved:
            result = await _videos_from_channels(label, saved, after, before)
        else:
            result = await youtube_provider.search_company_videos(
                label, primary.ticker, max_results=max_results,
                published_after=after, published_before=before,
            )

    resp = VideoResponse(
        configured=result.configured, scope="company",
        videos=_video_models(result.videos), message=result.message,
    )
    cache.data["company_videos"] = resp
    return resp


# =============================================================================
# GET /media/transcript
# =============================================================================

@router.get("/media/transcript", response_model=TranscriptResponse)
async def media_transcript(
    video_id: str = Query(..., description="YouTube video id"),
    cache: MediaCache = Depends(get_media_cache),
):
    """Fetch a video's full transcript (no key needed; captions permitting)."""
    tr = youtube_provider.get_transcript(video_id)
    if not tr.available:
        return TranscriptResponse(
            available=False, video_id=video_id, message=tr.message
        )

    # Cache a slice of the transcript text so the AI assistant can reference it.
    cache.transcripts[video_id] = {"text": tr.text[:4000]}
    return TranscriptResponse(
        available=True, video_id=video_id, text=tr.text,
        language=tr.language, summary=None,
    )


# =============================================================================
# /channels  (curated YouTube channel list)
# =============================================================================

@router.get("/channels", response_model=ChannelsResponse)
async def get_channels(
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """List the user's saved YouTube channels for a scope."""
    scope = _valid_scope(scope)
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


@router.post("/channels", response_model=ChannelsResponse)
async def add_channel(
    request: AddChannelRequest,
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """
    Add a channel (to the given scope) by URL, @handle, UC… id, or name.
    Resolves it to a channel_id + title via the YouTube API (an explicit UC id
    works even without a key), then persists that scope's list.
    """
    scope = _valid_scope(scope)
    raw = request.input.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Channel input cannot be empty.")

    result = await youtube_provider.resolve_channel(raw)
    if not result.ok or result.channel is None:
        raise HTTPException(
            status_code=422,
            detail=result.message or "Could not resolve that channel.",
        )

    ch = result.channel
    channel_store.add_channel(
        scope, {"channel_id": ch.channel_id, "title": ch.title, "handle": ch.handle}
    )
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


@router.delete("/channels/{channel_id}", response_model=ChannelsResponse)
async def delete_channel(
    channel_id: str,
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """Remove a saved channel from a scope."""
    scope = _valid_scope(scope)
    channel_store.remove_channel(scope, channel_id)
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


@router.patch("/channels/{channel_id}", response_model=ChannelsResponse)
async def rename_channel(
    channel_id: str,
    request: AddChannelRequest,
    scope: str = Query("company", description="'company' or 'macro'"),
):
    """Rename a saved channel's display title (reuses the `input` field)."""
    scope = _valid_scope(scope)
    title = request.input.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    channel_store.rename_channel(scope, channel_id, title)
    return ChannelsResponse(configured=True, channels=_channel_models(scope))


# =============================================================================
# GET /media/earnings  (best-effort)
# =============================================================================

@router.get("/media/earnings", response_model=EarningsResponse)
async def media_earnings(
    year: int = Query(..., description="Calendar/fiscal year, e.g. 2026"),
    quarter: int = Query(..., ge=1, le=4, description="Quarter 1-4"),
    store: DocumentStore = Depends(get_document_store),
    cache: MediaCache = Depends(get_media_cache),
):
    """
    Earnings-call transcript for a chosen quarter (e.g. 2026 Q1).

    Fetches the full transcript from investing.com first, falling back to
    Motley Fool (fool.com) if investing.com has none. Returns a graceful
    not-found / not-configured payload otherwise.
    """
    primary = company_service.primary_company(store)
    if primary is None or not (primary.name or primary.ticker):
        return EarningsResponse(
            configured=bool(news_provider.tavily_api_key()),
            year=year, quarter=quarter,
            message="No company detected yet. Upload a 10-K/10-Q first.",
        )

    doc = await news_provider.search_earnings_transcript(
        primary.name or "", primary.ticker, year, quarter
    )

    resp = EarningsResponse(
        configured=doc.configured, company=primary, year=year, quarter=quarter,
        found=doc.found, transcript=doc.text or None, source=doc.source,
        url=doc.url, title=doc.title, published=doc.published, message=doc.message,
    )
    # Cache a slice so the AI assistant can reference the latest earnings call.
    if doc.found and doc.text:
        cache.transcripts[f"earnings-{year}Q{quarter}"] = {"text": doc.text[:4000]}
    return resp


# =============================================================================
# GET /macro/news
# =============================================================================

@router.get("/macro/news", response_model=NewsResponse)
async def macro_news(
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(30, ge=1, le=30, description="Max articles (<=30)"),
    cache: MediaCache = Depends(get_media_cache),
):
    """Aggregated macro/market news (Tavily, finance domains)."""
    result = await news_provider.search_macro_news(
        max_results=max_results, **_news_range_kwargs(days, start, end)
    )
    resp = NewsResponse(
        configured=result.configured, scope="macro",
        articles=_news_models(result.articles), message=result.message,
    )
    cache.data["macro_news"] = resp
    return resp


# =============================================================================
# GET /macro/videos
# =============================================================================

@router.get("/macro/videos", response_model=VideoResponse)
async def macro_videos(
    channel_id: str | None = Query(None, description="Saved channel id, or omit/'all'"),
    days: int | None = Query(None, description="Look-back window in days (preset ranges)"),
    start: str | None = Query(None, description="Custom range start, YYYY-MM-DD"),
    end: str | None = Query(None, description="Custom range end, YYYY-MM-DD"),
    max_results: int = Query(25, ge=1, le=50, description="Max videos"),
    cache: MediaCache = Depends(get_media_cache),
):
    """
    Macro/economic YouTube videos.

    - `channel_id` set → that channel's newest videos.
    - omitted / "all" → merged newest across saved channels; if none saved,
      falls back to a broad macro keyword search.
    """
    after, before = _video_time_bounds(days, start, end)

    if channel_id and channel_id != "all":
        result = await youtube_provider.search_videos(
            channel_id=channel_id, max_results=max_results,
            published_after=after, published_before=before,
        )
    else:
        saved = channel_store.channel_ids("macro")
        if saved:
            # Empty query → browse each channel's latest uploads.
            result = await _videos_from_channels("", saved, after, before)
        else:
            result = await youtube_provider.search_macro_videos(
                max_results=max_results, published_after=after, published_before=before
            )

    resp = VideoResponse(
        configured=result.configured, scope="macro",
        videos=_video_models(result.videos), message=result.message,
    )
    cache.data["macro_videos"] = resp
    return resp


# =============================================================================
# GET /macro/sentiment
# =============================================================================

@router.get("/macro/sentiment", response_model=SentimentResponse)
async def macro_sentiment(cache: MediaCache = Depends(get_media_cache)):
    """Gemini-synthesized market sentiment from aggregated macro headlines."""
    r = await compute_market_sentiment()
    resp = SentimentResponse(
        configured=r.configured, label=r.label, score=r.score, summary=r.summary,
        indicators=[
            SentimentIndicatorModel(theme=i.theme, direction=i.direction, note=i.note)
            for i in r.indicators
        ],
        headline_count=r.headline_count, message=r.message,
    )
    cache.data["sentiment"] = resp
    return resp
