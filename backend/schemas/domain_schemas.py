"""
schemas.domain_schemas
───────────────────────
Domain "entity" models — the reusable building blocks that describe things in
the problem domain (a company, a news article, a video, a sentiment reading).

These are nested inside the API request/response envelopes in ``api_schemas``.
Keeping them separate makes the domain vocabulary easy to find and lets the
envelope models read as a thin contract layer on top of it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# Company
# ─────────────────────────────────────────────────────────────

class CompanyInfo(BaseModel):
    """A company derived from the uploaded filings."""
    cik: int | None = None
    name: str | None = None
    ticker: str | None = None
    filing_count: int = 0


# ─────────────────────────────────────────────────────────────
# Media entities (news / video / channels / sentiment)
# ─────────────────────────────────────────────────────────────

class NewsArticleModel(BaseModel):
    title: str
    url: str
    source: str
    snippet: str = ""
    published: str | None = None


class VideoModel(BaseModel):
    video_id: str
    title: str
    channel: str
    url: str
    embed_url: str
    thumbnail: str | None = None
    published: str | None = None
    description: str = ""


class ChannelModel(BaseModel):
    channel_id: str
    title: str
    handle: str | None = None


class SentimentIndicatorModel(BaseModel):
    theme: str
    direction: str  # bullish | neutral | bearish
    note: str
