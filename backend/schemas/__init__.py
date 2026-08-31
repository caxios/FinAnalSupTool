"""
schemas
───────
Pydantic models that define the API request/response contract.

Split across two modules for readability:
  - ``domain_schemas`` — reusable domain entities (company, article, video, …)
  - ``api_schemas``    — request/response envelopes bound to endpoints

Everything is re-exported here so callers keep importing ``from schemas import X``
regardless of which module a model lives in.
"""

from __future__ import annotations

from .domain_schemas import (
    CompanyInfo,
    NewsArticleModel,
    VideoModel,
    ChannelModel,
    SentimentIndicatorModel,
)
from .api_schemas import (
    FilingMeta,
    UploadResponse,
    SecFetchRequest,
    ResolvedFiling,
    SecFetchResponse,
    FinancialTableResponse,
    FilingTextResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CompanyResponse,
    NewsResponse,
    VideoResponse,
    TranscriptResponse,
    EarningsResponse,
    ChannelsResponse,
    AddChannelRequest,
    SentimentResponse,
    AnalyzeRequest,
    HoldingCreate,
    Holding,
    TradeCreate,
    Trade,
    TradeResponse,
    PriceResolution,
    CoachReviewRequest,
    PortfolioResponse,
    HoldingCreatedResponse,
    TradesResponse,
    ErrorDetail,
)

__all__ = [
    # domain
    "CompanyInfo",
    "NewsArticleModel",
    "VideoModel",
    "ChannelModel",
    "SentimentIndicatorModel",
    # api
    "FilingMeta",
    "UploadResponse",
    "SecFetchRequest",
    "ResolvedFiling",
    "SecFetchResponse",
    "FinancialTableResponse",
    "FilingTextResponse",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "CompanyResponse",
    "NewsResponse",
    "VideoResponse",
    "TranscriptResponse",
    "EarningsResponse",
    "ChannelsResponse",
    "AddChannelRequest",
    "SentimentResponse",
    "AnalyzeRequest",
    "HoldingCreate",
    "Holding",
    "TradeCreate",
    "Trade",
    "TradeResponse",
    "PriceResolution",
    "CoachReviewRequest",
    "PortfolioResponse",
    "HoldingCreatedResponse",
    "TradesResponse",
    "ErrorDetail",
]
