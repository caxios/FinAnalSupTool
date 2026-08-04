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

__all__ = [
    "AgentReport",
    "BaseAgent",
    "SECFilingsAgent",
    "TechnicalAnalysisAgent",
]
