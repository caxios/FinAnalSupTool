"""Pydantic output schemas for the MAS agents."""

from .sec_filings import SECFilingsReport
from .technical_analysis import TechnicalAnalysisReport
from .earnings_call import EarningsCallReport
from .company_news import CompanyNewsReport
from .macro_market import MacroMarketReport
from .youtube_analysis import YouTubeAnalysisReport
from .macro_history import MacroHistoryReport

__all__ = [
    "SECFilingsReport",
    "TechnicalAnalysisReport",
    "EarningsCallReport",
    "CompanyNewsReport",
    "MacroMarketReport",
    "YouTubeAnalysisReport",
    "MacroHistoryReport",
]
