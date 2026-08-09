# Step 4: True Sequential Debate & Role-Based Chat Architecture

## Objective

This step encompasses two critical milestones for the Multi-Agent System:
1. **True Sequential Debate Mechanism:** Implement an inter-agent debate system where all 6 agents sequentially talk to each other, bringing their **raw data** into the argument to produce a deep, authentic back-and-forth debate.
2. **Data Isolation & Role-Based Chat:** Refactor the Chat API and Frontend UI to allow users to chat with specific agents individually. By isolating data contexts, we prevent "token bloat" while giving the user access to the full debate history.

---

## 1. Architecture: Role-Based Chat & Data Isolation

To drastically reduce API costs and improve UX during interactive chats, the system transitions from a single "Omniscient Bot" to isolated "Specialized Personas".

### 1.1 UI/UX: Agent Monitoring Dashboard
- **Transparency:** The UI will display each agent's **Raw Data Source** alongside its **Initial Analysis** and **Debate Contributions**.
- **Individual Chat:** The user can select exactly which agent to converse with.

### 1.2 Backend Data Isolation Rules (Token Saver)
The Chat API (`/chat`) must strictly scope the LLM system prompt based on the active agent persona. 

*   **Field Agents (SEC, Earnings, News, etc.):**
    *   **Context Rule:** The prompt ONLY contains:
        1. The specific agent's Raw Input Data.
        2. The specific agent's Initial JSON Report.
        3. The full **Debate Transcript** (so they know what was discussed).
    *   **Constraint:** Field agents MUST NOT receive raw data or initial reports from other domains.
*   **Manager Agent (The Synthesizer):**
    *   **Context Rule:** The prompt ONLY contains:
        1. The 6 Initial JSON Reports from the field agents.
        2. The full **Debate Transcript**.
    *   **Constraint:** The Manager MUST NOT receive any raw text (no raw PDFs, no raw transcripts).

---

## 2. Architecture: True Sequential Debate Flow

Unlike a simple "Moderator" summarizing JSONs, this system executes a **sequential relay debate**. Each agent brings its full raw data to the table and reacts to what the previous agents said.

```text
Phase 1: Independent Analysis (Parallel)
6 Agents produce their initial JSON summaries.

Phase 2: True Sequential Debate (Relay)
    ┌───────────────────────────┐
    │ Debate Transcript (Empty) │
    └─────────────┬─────────────┘
                  ▼
          [SEC Agent Turn]
(Reads: SEC Raw Data + Transcript)
(Outputs: Argument based on SEC data) ─┐
                                       │
                                       ▼ (Appends to Transcript)
        [Earnings Agent Turn]
(Reads: Earnings Raw Data + Transcript)
(Outputs: Rebuttal/Agreement) ─────────┐
                                       │
                                       ▼
           [... Next Agents ...]

Phase 3: Manager Agent
Manager receives all Initial JSONs + the final Debate Transcript.
```

**Trade-offs:** 
- **Latency:** Because agents speak one after another, this process will take 1-2 minutes.
- **Cost:** Costs increase to ~1M tokens per run (highly affordable on Gemini Flash). 
- **Quality:** Dramatically higher reasoning quality and cross-validation compared to the moderator pattern.

---

## 3. Tasks

### 3.1 Refactor Chat API for Data Isolation
**Modify:** `backend/gemini_chat.py` and `backend/main.py`
- Modify `/chat` endpoint to accept `agent_id`.
- Route context dynamically based on `agent_id` (e.g., only SEC data for SEC Agent + Debate Transcript).
- Implement a **Chat History Sliding Window** to prevent historical token bloat during user chats.

### 3.2 Implement True Debate Module
**New file:** `backend/agents/debate.py`

#### 3.2.1 Debate Data Structures
```python
from pydantic import BaseModel

class AgentArgument(BaseModel):
    agent_id: str
    stance: str                 # "bullish" | "bearish" | "neutral"
    argument: str               # The actual text of what the agent says
    cited_evidence: list[str]   # Specific quotes/data from their raw data

class DebateTranscript(BaseModel):
    rounds: int
    history: list[AgentArgument]
    consensus_reached: bool
```

#### 3.2.2 Debate Prompting
Each agent requires a new prompt for the debate phase:
```text
You are the {agent_name}. You are participating in a round-table debate.
Here is your raw data: {raw_data}
Here is the debate so far: {debate_transcript}

Formulate your response. If another agent made a claim that contradicts your raw data, 
you MUST explicitly refute it using specific numbers or quotes from your data.
If you agree, provide supporting evidence from your data.
```

#### 3.2.3 Sequential Debate Runner
```python
async def run_sequential_debate(agent_contexts: dict) -> DebateTranscript:
    transcript = DebateTranscript(rounds=0, history=[], consensus_reached=False)
    debate_order = ["sec_filings", "earnings_call", "company_news", "youtube_analysis", "macro_market", "technical_analysis"]
    
    for round_num in range(2): # 2 rounds of debate
        for agent_id in debate_order:
            if not agent_contexts.get(agent_id):
                continue
                
            # Build prompt with agent's RAW DATA + current transcript
            prompt = build_debate_prompt(agent_id, agent_contexts[agent_id], transcript)
            
            # Call LLM sequentially
            argument = await generate_structured(prompt, AgentArgument)
            transcript.history.append(argument)
            
    return transcript
```

### 3.3 Integration into `/analyze`
- Update `/analyze` to trigger `run_sequential_debate` after Phase 1.
- Save the final `DebateTranscript` in memory so it can be injected into the Chat API and Manager Agent.

### 3.4 Concurrency Management (Phase 1)
**Modify:** `backend/main.py`
- Phase 1 still fires 6 agents simultaneously via `asyncio.gather()`. 
- Wrap the initial agent calls in `asyncio.Semaphore(2)` to prevent 429 Rate Limit errors before the sequential debate begins.

---

## 4. Success Criteria

- [ ] Chat endpoint correctly isolates raw data by `agent_id` while providing the full Debate Transcript.
- [ ] Debate runs sequentially, allowing agents to accurately cite their raw data to refute or support others.
- [ ] Manager Agent consumes the Final Transcript and JSONs without ever touching raw PDF/video data.
- [ ] Concurrency limit in Phase 1 prevents API 429 Rate Limit crashes.
