# Step 5: Manager Agent + 3-Axis Gap Analysis

## Objective

Implement the Manager Agent that synthesizes the debate-refined reports from
all 6 agents into a final, comprehensive analysis report. The Manager's
defining feature is **3-axis gap analysis** (Fundamental vs. Sentiment vs.
Price Action) with strict source attribution on every conclusion.

---

## 1. Prerequisites

- Steps 1-4 completed: All 6 agents + debate mechanism producing quality output
- Debate result includes `revised_positions` and `debate_insights`
- The output quality of individual agents and the debate has been manually
  validated as trustworthy

---

## 2. Tasks

### 2.1 Manager Agent

#### 2.1.1 Define Output Schema

**New file:** `backend/agents/schemas/manager.py`

The Manager Agent produces the most complex schema in the system:

```python
class SourceAttribution(BaseModel):
    """A single piece of evidence traced back to its agent."""
    agent: str              # e.g., "sec_filings", "earnings_call"
    evidence: str           # Specific data point or finding

class KeyFinding(BaseModel):
    """A major finding with full source tracing."""
    finding: str            # The conclusion
    sources: list[SourceAttribution]  # Every agent that contributed
    debate_refinement: str | None     # How debate changed this finding
    confidence: float       # 0-1

class ThreeAxisScores(BaseModel):
    fundamental_score: int     # 0-100, from SEC + Earnings
    sentiment_score: int       # 0-100, from News + Macro + YouTube + Earnings Q&A
    technical_score: int       # 0-100, from Technical Agent
    fundamental_sentiment_gap: int   # fundamental - sentiment
    fundamental_technical_gap: int   # fundamental - technical
    overall_signal: str        # See interpretation matrix

class GapAnalysis(BaseModel):
    fundamental_vs_sentiment_gap: int
    primary_gap_drivers: list[str]
    convergence_catalysts: list[str]  # Events that could close the gap
    risk_to_thesis: list[str]         # Events that could widen the gap

class DebateSummary(BaseModel):
    total_challenges: int
    accepted_and_revised: int
    rejected_with_justification: int
    key_revision: str          # Most impactful revision from debate

class ManagerReport(BaseModel):
    agent: str = "manager"
    company: str
    ticker: str
    analysis_date: str
    analysis_period: str
    
    three_axis_scores: ThreeAxisScores
    executive_summary: str
    key_findings: list[KeyFinding]
    gap_analysis: GapAnalysis
    
    convergence_catalysts: list[str]  # With source attribution
    risk_to_thesis: list[str]         # With source attribution
    
    debate_summary: DebateSummary
    
    confidence: float
    reasoning: str
```

#### 2.1.2 3-Axis Scoring Logic

The Manager Agent computes composite scores from individual agent scores.
The weights can be programmatic (computed in Python before the LLM call) or
LLM-determined. Recommended approach: **programmatic computation** for scores,
**LLM interpretation** for narrative.

```python
def compute_three_axis_scores(agent_reports: dict, debate_result) -> ThreeAxisScores:
    """
    Compute the three composite scores from individual agent scores.
    
    Uses debate-revised scores where available; falls back to original.
    """
    # Get scores (prefer debate-revised values)
    sec_score = _get_score(agent_reports, debate_result, "sec_filings", "fundamental_score")
    earnings_tone = _get_score(agent_reports, debate_result, "earnings_call", "tone_score")
    # ... etc
    
    fundamental = int(
        sec_score * 0.45 +
        earnings_tone * 0.25 +
        guidance_score * 0.30
    )
    
    sentiment = int(
        news_score * 0.35 +
        macro_score * 0.25 +
        youtube_score * 0.15 +
        earnings_qa_score * 0.25
    )
    
    technical = tech_trend_score  # Direct from Technical Agent
    
    return ThreeAxisScores(
        fundamental_score=fundamental,
        sentiment_score=sentiment,
        technical_score=technical,
        fundamental_sentiment_gap=fundamental - sentiment,
        fundamental_technical_gap=fundamental - technical,
        overall_signal=_interpret_signal(fundamental, sentiment, technical),
    )
```

#### 2.1.3 Signal Interpretation Matrix

```python
def _interpret_signal(fund: int, sent: int, tech: int) -> str:
    """
    Map the three scores to an actionable signal.
    
    Thresholds:
      High = >= 65
      Low  = < 50
      Mid  = 50-64
    """
    f_high = fund >= 65
    s_low = sent < 50
    t_low = tech < 50
    
    if f_high and s_low and t_low:
        return "hidden_gem"           # Strong fundamentals, market ignoring, price falling
    if f_high and s_low and not t_low:
        return "discovery_in_progress"  # Fundamentals strong, sentiment catching up
    if f_high and not s_low and not t_low:
        return "consensus_bullish"    # All aligned positive
    if not f_high and not s_low and not t_low:
        return "overvaluation_warning"  # Sentiment/price ahead of fundamentals
    if not f_high and s_low and t_low:
        return "justified_decline"    # All aligned negative
    if f_high and not s_low and t_low:
        return "temporary_pullback"   # Fundamentals + sentiment OK, technical dip
    return "mixed_signals"
```

#### 2.1.4 Manager Agent Implementation

**New file:** `backend/agents/manager_agent.py`

The Manager's `analyze()` method:
1. Receives: all 6 agent reports + debate result
2. Computes 3-axis scores programmatically (§2.1.2)
3. Calls the LLM with:
   - The 3-axis scores
   - All agent reports (or debate-refined summaries if token-constrained)
   - The debate challenges + responses
4. LLM produces: executive summary, key findings (with source attribution),
   gap analysis narrative, convergence catalysts, risks

#### 2.1.5 Manager System Prompt

```python
MANAGER_SYSTEM_PROMPT = """
You are the Manager Agent — the final synthesizer of a multi-agent stock
analysis system. You receive analyses from 6 specialized agents plus the
results of their round-table debate.

Your job:
1. Produce an EXECUTIVE SUMMARY (2-3 sentences) capturing the key investment thesis.

2. Identify KEY FINDINGS — the 3-5 most important conclusions.
   CRITICAL RULE: Every finding MUST include source attribution. For each
   finding, list which agent(s) provided the evidence and what specific data
   they cited. Do NOT make claims without tracing them to an agent's report.
   
   Format per finding:
   {
     "finding": "The conclusion",
     "sources": [
       {"agent": "sec_filings", "evidence": "specific data point"},
       {"agent": "earnings_call", "evidence": "specific quote or metric"}
     ],
     "debate_refinement": "How this finding was modified by the debate (if at all)"
   }

3. Produce GAP ANALYSIS narrative — explain WHY the three scores diverge,
   what events could cause convergence, and what risks could widen the gap.

4. List CONVERGENCE CATALYSTS and RISKS TO THESIS — each with the source agent.

You are given pre-computed three-axis scores:
  Fundamental: {fundamental_score}/100
  Sentiment: {sentiment_score}/100
  Technical: {technical_score}/100
  Signal: {overall_signal}

Do NOT recompute these scores. Use them as given and focus on NARRATIVE
INTERPRETATION — explain what these numbers mean for an investor.

=== AGENT REPORTS ===
{agent_reports}

=== DEBATE RESULTS ===
{debate_result}
"""
```

### 2.2 Integration into `/analyze`

**Modify:** `backend/main.py`

Complete the full pipeline:

```python
@app.post("/analyze")
async def run_analysis(request: AnalyzeRequest):
    # Phase 1: Independent analysis (parallel)
    agent_reports = await _run_all_agents(request)
    
    # Phase 2: Round-table debate
    debate_result = await run_debate(agent_reports)
    
    # Phase 3: Manager synthesis
    manager_agent = ManagerAgent()
    final_report = await manager_agent.analyze({
        "agent_reports": agent_reports,
        "debate_result": debate_result,
        "company": primary.name,
        "ticker": primary.ticker,
        "analysis_period": f"{request.start_date} ~ {request.end_date}",
    })
    
    return {
        "final_report": final_report.model_dump(),
        "agent_reports": agent_reports,     # Include for transparency
        "debate": debate_result.model_dump(),  # Include for transparency
    }
```

### 2.3 Response Schema for Frontend

**Modify:** `backend/schemas.py`

Add response models so the frontend knows the exact shape of the analysis output:

```python
class AnalyzeResponse(BaseModel):
    """Full MAS analysis result."""
    final_report: dict       # Manager Agent output
    agent_reports: dict      # All 6 agent outputs (keyed by agent_id)
    debate: dict             # Debate result
```

---

## 3. Verification

### 3.1 Source Attribution Accuracy

The most critical quality check:
1. For each `key_finding`, verify that every `source` references data that
   actually exists in the cited agent's report
2. Verify no conclusions appear without any source attribution
3. Verify `debate_refinement` accurately reflects what changed in the debate

### 3.2 3-Axis Score Consistency

1. Compute scores manually from agent reports → compare with programmatic output
2. Verify the `overall_signal` classification matches the score combination
3. Test edge cases: all scores high, all low, one high + two low, etc.

### 3.3 End-to-End Pipeline

1. Upload filings → POST /analyze with a date range → receive full report
2. Measure total pipeline time (target: < 90 seconds for a 2-quarter analysis)
3. Verify the final report is coherent and actionable
4. Test with 2-3 different companies to ensure generalization

### 3.4 Quality Review Checklist

- [ ] Executive summary captures the core thesis in 2-3 sentences
- [ ] Key findings are non-trivial (not just restating agent scores)
- [ ] Gap analysis explains WHY the gap exists, not just that it does
- [ ] Convergence catalysts are specific events with timelines
- [ ] Risks are genuine concerns, not generic disclaimers

---

## 4. Files Created / Modified

| Action | File |
|---|---|
| **NEW** | `backend/agents/manager_agent.py` |
| **NEW** | `backend/agents/schemas/manager.py` |
| **NEW** | `backend/agents/scoring.py` (3-axis score computation) |
| **MODIFY** | `backend/main.py` (complete pipeline) |
| **MODIFY** | `backend/schemas.py` (add `AnalyzeResponse`) |

---

## 5. Success Criteria

- [ ] Manager Agent produces a coherent, actionable final report
- [ ] **Every conclusion has source attribution** (zero unattributed claims)
- [ ] 3-axis scores are computed correctly from agent scores
- [ ] Signal interpretation matches the score combination
- [ ] Debate refinements are accurately reflected in the final report
- [ ] Full pipeline (6 agents + debate + manager) completes in < 90 seconds
- [ ] Report quality is consistent across different companies
