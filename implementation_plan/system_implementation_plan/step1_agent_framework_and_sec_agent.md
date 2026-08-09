# Step 1: Agent Framework Setup + SEC Filings Analyzer Agent

## Objective

Establish the foundational multi-agent infrastructure and implement the first
specialized agent (SEC Filings Analyzer) as a proof-of-concept. By the end of
this step, a single agent should be able to ingest uploaded filing data and
produce a structured JSON analysis report.

---

## 1. Prerequisites

- Existing backend running (FastAPI + Gemini integration)
- At least one 10-K/10-Q uploaded and parsed (XBRL data available in memory)

---

## 2. Tasks

### 2.1 Agent Base Framework

Create the foundational agent infrastructure that all subsequent agents will
reuse.

#### 2.1.1 Define Base Agent Interface

**New file:** `backend/agents/base_agent.py`

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Any

class AgentReport(BaseModel):
    """Base schema that every agent report must extend."""
    agent: str                    # Agent identifier
    confidence: float             # 0.0 - 1.0 self-assessed confidence
    reasoning: str                # Free-text reasoning summary

class BaseAgent(ABC):
    """
    Abstract base class for all MAS agents.
    
    Each agent:
      1. Receives data from the Data Layer (specific to its domain)
      2. Calls the LLM with a specialized system prompt
      3. Returns a structured, Pydantic-validated report
    """
    
    @property
    @abstractmethod
    def agent_id(self) -> str:
        """Unique identifier, e.g. 'sec_filings', 'technical_analysis'."""
    
    @abstractmethod
    async def analyze(self, context: dict) -> AgentReport:
        """
        Run the agent's analysis.
        
        Args:
            context: A dict containing the agent-specific input data.
                     The exact keys depend on the agent type.
        
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
        Shared LLM call method using the existing gemini_chat infrastructure.
        
        Uses Gemini's structured output mode (response_mime_type + response_schema)
        when a schema is provided, to guarantee valid JSON output.
        """
        # Implementation will wrap gemini_chat._gemini_call() or a similar
        # low-level function, adding structured output params.
```

#### 2.1.2 Define Structured Output Helper

**New file:** `backend/agents/llm_utils.py`

Wrap the existing `gemini_chat._gemini_call()` to support:
- `response_mime_type: "application/json"` for forced JSON output
- `response_schema` for schema-constrained generation
- Pydantic validation with up to 2 retries on parse failure
- Consistent error handling

This module should import from the existing `gemini_chat.py` to avoid
duplicating the Gemini API key management and HTTP client logic.

#### 2.1.3 Create Agent Package Structure

```
backend/
├── agents/
│   ├── __init__.py
│   ├── base_agent.py          # Base class + AgentReport
│   ├── llm_utils.py           # Structured LLM call wrapper
│   ├── sec_filings_agent.py   # Step 1 — this file
│   └── schemas/
│       ├── __init__.py
│       └── sec_filings.py     # Pydantic models for SEC agent output
```

### 2.2 SEC Filings Analyzer Agent

#### 2.2.1 Define Output Schema

**New file:** `backend/agents/schemas/sec_filings.py`

The Pydantic model should capture:

| Field | Type | Description |
|---|---|---|
| `periods_analyzed` | `list[str]` | Period keys analyzed (e.g., `["Q2 FY2026"]`) |
| `fundamental_score` | `int` (0-100) | Overall fundamental health score |
| `financial_health` | `FinancialHealth` | Revenue/margin/debt/FCF metrics + trends |
| `multi_period_trends` | `list[TrendItem]` | Time-series trajectories for key metrics |
| `mda_insights` | `list[str]` | Key insights extracted from MD&A |
| `risk_assessment` | `list[RiskItem]` | Classified risks with severity + trend |
| `confidence` | `float` | Self-assessed confidence (0-1) |
| `reasoning` | `str` | Free-text reasoning |

The scoring rubric should be embedded in the system prompt, not the schema:
- 80+ = Revenue growing >5% YoY AND margin expanding AND positive FCF
- 60-79 = Mixed signals (e.g., revenue growing but margin compressing)
- <60 = Multiple negative trends

#### 2.2.2 Implement the Agent

**New file:** `backend/agents/sec_filings_agent.py`

Data sources (from existing in-memory stores):
- `_merged_tables` → Financial statements (Balance Sheet, Income Statement, Cash Flow, Ratios)
- `_text_store` → MD&A, Risk Factors, Business sections
- `_filing_meta` → Period metadata, data source, entity info

The agent's `analyze()` method should:
1. Receive the relevant data from the in-memory stores
2. Format it as a Markdown context (reuse/adapt `build_context()` logic from `gemini_chat.py`)
3. Call the LLM with a specialized system prompt that instructs:
   - Analyze the financial tables for trends
   - Extract key insights from MD&A text
   - Classify and assess risk factors
   - Score the overall fundamental health using the rubric
4. Parse and validate the JSON response against the Pydantic schema
5. Retry up to 2x on validation failure

#### 2.2.3 System Prompt Design

The SEC Filings Agent's system prompt should:
- Clearly define the agent's role and scope
- Include the scoring rubric with explicit thresholds
- Provide one few-shot example of the expected JSON output
- Emphasize: "Base every claim on the DATA section. Do not invent numbers."
- Specify that `confidence` should reflect data completeness (e.g., if only 1 period is uploaded, confidence should be lower than with 4 periods)

### 2.3 Wire Up the Endpoint

#### 2.3.1 Create `/analyze` Endpoint

**Modify:** `backend/main.py`

Add a new endpoint that:
1. Checks that at least one filing has been uploaded
2. Instantiates the SEC Filings Agent
3. Passes the relevant in-memory data
4. Returns the structured report as JSON

```python
@app.post("/analyze")
async def run_analysis():
    """
    Run the MAS analysis pipeline.
    Step 1: Only SEC Filings Agent.
    """
    if not _filing_meta:
        raise HTTPException(404, "No filings uploaded yet.")
    
    agent = SECFilingsAgent()
    report = await agent.analyze({
        "merged_tables": _merged_tables,
        "text_store": _text_store,
        "filing_meta": _filing_meta,
    })
    return report.model_dump()
```

---

## 3. Verification

### 3.1 Functional Tests

1. Upload a 10-K PDF → call `POST /analyze` → verify response is valid JSON
   matching the Pydantic schema
2. Upload multiple filings (e.g., 2 quarters) → verify `multi_period_trends`
   contains trajectory data across periods
3. Verify `fundamental_score` is within 0-100 and the `reasoning` references
   actual data points from the filing
4. Verify `risk_assessment` items have meaningful `severity` and `trend` values

### 3.2 Quality Checks

- Run the agent on 2-3 different companies' filings and manually review:
  - Are the `mda_insights` actually present in the MD&A text?
  - Does the `fundamental_score` feel reasonable given the numbers?
  - Are risks properly classified (not hallucinated)?
- Iterate on the system prompt until output quality is acceptable

### 3.3 Error Handling

- Test with no filings uploaded → should return 404
- Test with a filing that has tables but no text sections → agent should still
  produce a report with lower `confidence`
- Test Gemini API failure → should return a clear error, not crash

---

## 4. Decisions to Finalize Before Starting

- [ ] **Orchestration framework**: LangGraph vs. custom FastAPI + asyncio
  (affects how `BaseAgent` and the `/analyze` endpoint are structured)
- [ ] **Gemini model**: Which specific model to use for all agents
  (e.g., `gemini-2.5-flash` or `gemini-2.5-pro`)
- [ ] **Structured output mode**: Whether to use Gemini's native
  `response_schema` or rely on prompt-based JSON + Pydantic validation

---

## 5. Files Created / Modified

| Action | File |
|---|---|
| **NEW** | `backend/agents/__init__.py` |
| **NEW** | `backend/agents/base_agent.py` |
| **NEW** | `backend/agents/llm_utils.py` |
| **NEW** | `backend/agents/sec_filings_agent.py` |
| **NEW** | `backend/agents/schemas/__init__.py` |
| **NEW** | `backend/agents/schemas/sec_filings.py` |
| **MODIFY** | `backend/main.py` (add `/analyze` endpoint) |

---

## 6. Success Criteria

- [ ] `POST /analyze` returns a valid, schema-compliant JSON report
- [ ] The report contains meaningful analysis grounded in actual filing data
- [ ] The agent framework (`BaseAgent`, `llm_utils`) is generic enough to
      support the next agents without modification
- [ ] Error cases are handled gracefully (no filings, API failures, parse errors)
