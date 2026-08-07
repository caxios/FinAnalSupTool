"""
agents/base_agent.py
────────────────────
Abstract base class + base report schema shared by every MAS agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, Field

from . import llm_utils


class AgentReport(BaseModel):
    """Base schema that every agent report must extend."""
    agent: str = Field(..., description="Agent identifier, e.g. 'sec_filings'")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="0.0-1.0 self-assessed confidence"
    )
    reasoning: str = Field(..., description="Free-text reasoning summary")


TReport = TypeVar("TReport", bound=AgentReport)


class BaseAgent(ABC):
    """
    Abstract base class for all MAS agents.

    Each agent:
      1. Receives data from the Data Layer (specific to its domain).
      2. Calls the LLM with a specialized system prompt.
      3. Returns a structured, Pydantic-validated report.
    """

    @property
    @abstractmethod
    def agent_id(self) -> str:
        """Unique identifier, e.g. 'sec_filings', 'technical_analysis'."""

    @abstractmethod
    async def analyze(self, context: dict, capture: dict | None = None) -> AgentReport:
        """
        Run the agent's analysis.

        Args:
            context: A dict containing the agent-specific input data. The exact
                     keys depend on the agent type.
            capture: Optional side-channel. When provided, the agent writes its
                     assembled RAW-DATA prompt to ``capture["raw_data"]`` so the
                     caller can reuse it (for the debate and the isolated,
                     per-agent chat) without paying to re-fetch/re-assemble it.

        Returns:
            A Pydantic model extending AgentReport with structured findings.
        """

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict | None = None,
    ) -> str:
        """
        Shared LLM call — forces JSON output and returns the raw string.

        Delegates to `llm_utils.generate_json`, which wraps the existing
        `gemini_chat` infrastructure. `response_schema` is accepted for API
        symmetry; structured validation is done via `_generate_report`.
        """
        return await llm_utils.generate_json(system_prompt, user_prompt)

    async def _generate_report(
        self,
        report_model: type[TReport],
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int = 4096,
    ) -> TReport:
        """
        Generate + validate a structured report against `report_model`, then
        stamp the agent id so callers can trust it regardless of the LLM output.
        """
        report = await llm_utils.generate_structured(
            system_prompt, user_prompt, report_model,
            max_output_tokens=max_output_tokens,
        )
        report.agent = self.agent_id
        return report
