"""
agents
──────
Multi-Agent System (MAS) infrastructure for the Financial Analysis Support Tool.

Each specialized agent ingests domain-specific data from the app's in-memory
stores, calls the LLM with a tailored system prompt, and returns a structured,
Pydantic-validated report. The base framework (`base_agent`, `llm_utils`) is
shared by every agent so new ones can be added without touching the plumbing.
"""

from .base_agent import AgentReport, BaseAgent
from .sec_filings_agent import SECFilingsAgent
from .technical_analysis_agent import TechnicalAnalysisAgent
from .earnings_call_agent import EarningsCallAgent, date_range_to_quarters
from .company_news_agent import CompanyNewsAgent
from .macro_market_agent import MacroMarketAgent
from .youtube_agent import YouTubeAgent
from .macro_history_agent import MacroHistoryAgent
from .quant_risk_agent import QuantRiskAgent
from .coach_agent import CoachAgent
from .manager_agent import ManagerAgent
from .debate import (
    AgentArgument,
    DebateTranscript,
    run_sequential_debate,
    render_transcript,
    display_name,
    DEBATE_ORDER,
    FIELD_AGENT_IDS,
)

__all__ = [
    "AgentReport",
    "BaseAgent",
    "SECFilingsAgent",
    "TechnicalAnalysisAgent",
    "EarningsCallAgent",
    "CompanyNewsAgent",
    "MacroMarketAgent",
    "YouTubeAgent",
    "MacroHistoryAgent",
    "QuantRiskAgent",
    "CoachAgent",
    "ManagerAgent",
    "date_range_to_quarters",
    "AgentArgument",
    "DebateTranscript",
    "run_sequential_debate",
    "render_transcript",
    "display_name",
    "DEBATE_ORDER",
    "FIELD_AGENT_IDS",
]
