# Step 4: Round-Table Debate Mechanism

## Objective

Implement the inter-agent debate system where all 6 analysis agents
participate in a round-table discussion to challenge, validate, and refine
each other's findings. This step adds the critical collaborative intelligence
layer between independent analysis (Step 3) and final synthesis (Step 5).

---

## 1. Prerequisites

- Steps 1-3 completed: All 6 agents producing structured, schema-compliant reports
- Agent output quality validated — debate on top of poor-quality reports is pointless
- Parallel execution working reliably

---

## 2. Architecture

### 2.1 Debate Flow

```
6 Agent Reports (from Phase 1)
        │
        ▼
┌─────────────────────────────────┐
│  Round 1: Challenge             │ 1 LLM call
│  (Identify contradictions,     │
│   blind spots, reinforcements)  │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  Convergence Check              │ No LLM call (programmatic)
│  Meaningful challenges ≥ 2?     │
└──────┬──────────────┬───────────┘
       │              │
    Yes ▼           No ▼
┌──────────────┐  ┌──────────────┐
│  Round 2:    │  │  Skip to     │
│  Respond &   │  │  Manager     │
│  Revise      │  │  (early exit)│
│  1 LLM call  │  └──────────────┘
└──────┬───────┘
       │
       ▼
  Debate Result → Manager Agent (Step 5)
```

### 2.2 Key Principle: LLM as Moderator, Not Individual Agents

Rather than making 6 separate LLM calls (one per agent's "response"), we use
a single LLM call per round where the model plays the role of a **debate
moderator** who speaks on behalf of all 6 agents. This is:
- More token-efficient (1 call vs. 6 per round)
- More coherent (the moderator sees all perspectives simultaneously)
- Easier to control output format

---

## 3. Tasks

### 3.1 Debate Module

**New file:** `backend/agents/debate.py`

#### 3.1.1 Debate Data Structures

```python
from pydantic import BaseModel

class Challenge(BaseModel):
    """A challenge from one agent to another."""
    challenger: str          # agent_id of the challenger
    target: str              # agent_id being challenged
    challenge_type: str      # "contradiction" | "blind_spot" | "reinforcement"
    description: str         # What the challenge is about
    supporting_evidence: str # Data from the challenger's report
    impact_level: str        # "high" | "medium" | "low"

class ChallengeResponse(BaseModel):
    """An agent's response to a challenge."""
    challenge_id: int
    target_agent: str
    action: str              # "accepted" | "rejected"
    revised_finding: str | None  # If accepted, what changed
    justification: str       # Why the agent accepted or rejected

class DebateResult(BaseModel):
    """Output of the full debate process."""
    rounds_completed: int    # 1 or 2
    early_termination: bool  # True if convergence detected after Round 1
    total_challenges: int
    challenges_accepted: int
    challenges_rejected: int
    
    challenges: list[Challenge]
    responses: list[ChallengeResponse]  # Empty if early termination
    
    # The refined positions of each agent after debate
    revised_positions: dict[str, str]   # agent_id → refined summary
    
    # Key insights that emerged from the debate
    debate_insights: list[str]
```

#### 3.1.2 Debate Moderator — Round 1 Prompt

```python
ROUND_1_SYSTEM_PROMPT = """
You are the moderator of a round-table debate between 6 financial analysis
agents. Each agent has independently analyzed the same company from their
specialized perspective. Your job is to identify:

1. CONTRADICTIONS — Where Agent A's conclusion directly conflicts with Agent B's
   data or findings. Example: SEC Agent says margins are expanding, but Earnings
   Agent notes management warned about cost pressures.

2. BLIND SPOTS — Where Agent A's conclusion would change if they had considered
   Agent B's data. Example: YouTube Agent calls the stock a "buy" without
   considering the Technical Agent's overbought RSI reading.

3. REINFORCEMENTS — Where multiple agents independently arrive at the same
   conclusion from different data sources (mutual validation).

For each challenge, specify:
- challenger (who is raising the challenge)
- target (who is being challenged)
- challenge_type (contradiction / blind_spot / reinforcement)
- description (what the challenge is about)
- supporting_evidence (specific data from the challenger's report)
- impact_level (high / medium / low — would this change the target's score or conclusion?)

Return ONLY challenges with impact_level "high" or "medium". Skip trivial
or marginal disagreements.

Respond in strict JSON matching the provided schema.
"""
```

#### 3.1.3 Debate Moderator — Round 2 Prompt

```python
ROUND_2_SYSTEM_PROMPT = """
You are the debate moderator continuing from Round 1. The following challenges
were raised:

{challenges}

For each challenge, respond on behalf of the TARGET agent:

1. If the challenge is valid and would change the target's analysis:
   - action: "accepted"
   - revised_finding: What specifically changes (e.g., "fundamental_score
     adjusted from 78 to 74 because...")
   
2. If the target agent's original analysis is still defensible despite the challenge:
   - action: "rejected"
   - justification: Why the original position holds (cite specific data)

Be rigorous — only accept challenges that genuinely warrant a revision.
Don't accept challenges just to seem collaborative.

After processing all challenges, provide a revised summary for each agent
that was impacted, and list the key insights that emerged from the debate.

Respond in strict JSON matching the provided schema.
"""
```

#### 3.1.4 Core Debate Logic

```python
async def run_debate(
    agent_reports: dict[str, dict],
    token_budget: int = 70_000,
) -> DebateResult:
    """
    Run the round-table debate across all agent reports.
    
    Three-layer safety:
      1. Hard cap: Max 2 rounds
      2. Convergence: Skip Round 2 if < 2 meaningful challenges
      3. Token budget: Compress reports if input would exceed budget
    """
    
    # --- Prepare input ---
    input_text = _format_reports_for_debate(agent_reports)
    
    # Token budget check: if the combined reports exceed the budget,
    # summarize each report before feeding to the debate
    if _estimate_tokens(input_text) > token_budget * 0.6:
        input_text = await _compress_reports(agent_reports)
    
    # --- Round 1: Challenge ---
    round1_result = await _call_llm(
        system=ROUND_1_SYSTEM_PROMPT,
        user=input_text,
        response_schema=Round1Schema,
    )
    challenges = round1_result.challenges
    
    # --- Convergence check ---
    meaningful = [c for c in challenges if c.impact_level in ("high", "medium")]
    if len(meaningful) < 2:
        return DebateResult(
            rounds_completed=1,
            early_termination=True,
            total_challenges=len(challenges),
            challenges_accepted=0,
            challenges_rejected=0,
            challenges=challenges,
            responses=[],
            revised_positions={},
            debate_insights=[
                "Agents largely agree — insufficient disagreement for debate."
            ],
        )
    
    # --- Round 2: Respond & Revise ---
    round2_input = f"{input_text}\n\nChallenges from Round 1:\n{challenges_json}"
    
    # Token budget check for Round 2
    if _estimate_tokens(round2_input) > token_budget * 0.4:
        # Only include the challenges and relevant agent summaries
        round2_input = _minimal_round2_context(challenges, agent_reports)
    
    round2_result = await _call_llm(
        system=ROUND_2_SYSTEM_PROMPT,
        user=round2_input,
        response_schema=Round2Schema,
    )
    
    return DebateResult(
        rounds_completed=2,
        early_termination=False,
        total_challenges=len(challenges),
        challenges_accepted=sum(1 for r in round2_result.responses if r.action == "accepted"),
        challenges_rejected=sum(1 for r in round2_result.responses if r.action == "rejected"),
        challenges=challenges,
        responses=round2_result.responses,
        revised_positions=round2_result.revised_positions,
        debate_insights=round2_result.debate_insights,
    )
```

### 3.2 Token Budget Management

#### 3.2.1 Report Compression

When the total token count exceeds the budget, compress each agent's report
to a condensed summary before feeding it to the debate:

```python
async def _compress_reports(reports: dict[str, dict]) -> str:
    """
    Summarize each agent's report to ~500-800 tokens while preserving
    key findings, scores, and critical data points.
    """
    # Option A: LLM-based summarization (higher quality, 1 extra LLM call)
    # Option B: Programmatic extraction of key fields only (zero LLM cost)
    #
    # Recommended: Option B for budget predictability
    compressed = {}
    for agent_id, report in reports.items():
        compressed[agent_id] = {
            "score": report.get("fundamental_score") or report.get("trend_score") or report.get("sentiment_score", {}).get("score"),
            "key_findings": report.get("mda_insights") or report.get("catalysts") or [],
            "risks": report.get("risk_assessment") or report.get("headwinds") or [],
            "confidence": report.get("confidence"),
            "reasoning": report.get("reasoning", "")[:300],
        }
    return json.dumps(compressed, indent=2)
```

#### 3.2.2 Token Estimation

```python
def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4
```

### 3.3 Integration into `/analyze`

**Modify:** `backend/main.py`

```python
from agents.debate import run_debate

@app.post("/analyze")
async def run_analysis(request: AnalyzeRequest):
    # Phase 1: Independent analysis (parallel)
    agent_reports = await _run_all_agents(request)
    
    # Phase 2: Round-table debate
    debate_result = await run_debate(agent_reports)
    
    return {
        "phase1_reports": agent_reports,
        "debate": debate_result.model_dump(),
        # Phase 3 (Manager) will be added in Step 5
    }
```

---

## 4. Verification

### 4.1 Challenge Quality

1. Run debate with real agent outputs → manually review challenges
2. Verify challenges reference specific data from the agent reports (not hallucinated)
3. Verify `impact_level` assignments are reasonable
4. Check that contradictions are genuine (not just different emphasis)

### 4.2 Convergence Detection

1. Test with agents that largely agree → should terminate after Round 1
2. Test with agents that have clear disagreements → should proceed to Round 2
3. Verify the threshold (< 2 meaningful challenges) is appropriate

### 4.3 Token Budget

1. Test with a 6-quarter analysis (large reports) → verify compression activates
2. Verify compressed reports preserve the essential information
3. Measure actual token usage vs. budget cap

### 4.4 Round 2 Quality

1. Verify "accepted" challenges lead to meaningful revisions (not rubber-stamps)
2. Verify "rejected" challenges have substantive justifications
3. Check that `revised_positions` reflect the actual changes from Round 2

---

## 5. Files Created / Modified

| Action | File |
|---|---|
| **NEW** | `backend/agents/debate.py` |
| **NEW** | `backend/agents/schemas/debate.py` |
| **MODIFY** | `backend/main.py` (add debate to `/analyze` pipeline) |

---

## 6. Success Criteria

- [ ] Round 1 produces meaningful challenges grounded in agent data
- [ ] Convergence detection correctly identifies when debate is unnecessary
- [ ] Round 2 produces balanced responses (not all accepted or all rejected)
- [ ] Token budget mechanism prevents runaway cost
- [ ] Total debate phase completes in < 30 seconds
- [ ] Debate actually improves analysis quality (subjective manual review)
