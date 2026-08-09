# Step 3: Remaining Agents (Earnings, News, Macro, YouTube)

## Objective

Implement the remaining four specialized agents, bringing the total to six
independent analysis agents. This step also introduces user-specified date
range support so that all data fetching is driven by the analysis period the
user selects.

---

## 1. Prerequisites

- Steps 1-2 completed: Agent framework functional, SEC + Technical agents working
- Parallel execution validated with 2 agents
- Existing data pipelines functional:
  - `news_provider.search_earnings_transcript()` for earnings call transcripts
  - `news_provider.search_company_news()` for company news
  - `news_provider.search_macro_news()` for macro news
  - `youtube_provider.get_transcript()` + `channel_store` for YouTube transcripts

---

## 2. Tasks

### 2.1 User-Specified Analysis Period

Before implementing the agents, add support for the user to specify the
analysis period. This date range drives ALL data fetching.

#### 2.1.1 Update `/analyze` Endpoint Signature

**Modify:** `backend/main.py`

```python
class AnalyzeRequest(BaseModel):
    start_date: str   # YYYY-MM-DD, e.g. "2025-01-01"
    end_date: str     # YYYY-MM-DD, e.g. "2026-06-30"

@app.post("/analyze")
async def run_analysis(request: AnalyzeRequest):
    ...
```

#### 2.1.2 Date Range → Quarter List Helper

Create a utility that converts a date range into a list of fiscal quarters,
so the Earnings Call Agent knows which quarters to fetch transcripts for:

```python
def date_range_to_quarters(start: str, end: str) -> list[tuple[int, int]]:
    """
    Convert a YYYY-MM-DD range to a list of (year, quarter) tuples.
    e.g., "2025-01-01" to "2026-06-30" → [(2025,1), (2025,2), ..., (2026,2)]
    """
```

### 2.2 Earnings Call Analyzer Agent

#### 2.2.1 Data Fetching Strategy

For the user-specified period, fetch ALL quarterly earnings call transcripts
within that range:

```python
# For each quarter in the range:
for year, quarter in quarters:
    doc = await news_provider.search_earnings_transcript(
        company, ticker, year, quarter
    )
    # doc.text contains the full transcript
```

**Key consideration:** Each transcript is ~30K-50K tokens. For a 6-quarter
range, that is 180K-300K tokens total. Strategies:
- For now (pre-RAG): Feed ALL transcripts to the agent. Gemini's 1M+ context
  window can handle this, but cost will be higher for longer periods.
- Annotate which quarter each transcript belongs to so the agent can track
  changes over time.

#### 2.2.2 Define Output Schema

**New file:** `backend/agents/schemas/earnings_call.py`

The schema must support **longitudinal (cross-quarter) analysis**:

| Field | Type | Description |
|---|---|---|
| `quarters_analyzed` | `list[str]` | All quarters analyzed |
| `per_quarter_analysis` | `list[QuarterAnalysis]` | Detailed per-quarter breakdown |
| `longitudinal_tracking` | `LongitudinalTracking` | Cross-quarter comparison |
| `confidence` | `float` | |
| `reasoning` | `str` | |

**QuarterAnalysis** fields:
- `management_tone` — overall, confidence_level, tone_score (0-100)
- `qa_key_topics` — topic, question_count, management response quality
- `business_substance` — key developments (area, detail, significance), strategic shifts
- `forward_guidance` — direction (raised/maintained/lowered/withdrawn), detail

**LongitudinalTracking** fields:
- `promise_vs_delivery` — what management promised in Q(n-1) vs what happened in Q(n)
- `evolving_themes` — themes that evolved across quarters (trajectory + assessment)
- `tone_trend_across_quarters` — array of tone scores per quarter
- `guidance_trend` — array of guidance directions per quarter
- `new_topics_not_in_previous` — topics that appeared for the first time
- `dropped_topics` — topics previously discussed but no longer mentioned

#### 2.2.3 System Prompt Design

The Earnings Call Agent's system prompt must:
- Emphasize deep business substance analysis, not just tone/sentiment
- Instruct explicit cross-quarter comparison ("What did management promise in
  Q(n-1)? Was it delivered in Q(n)?")
- Ask for Q&A topic frequency analysis (what analysts are most concerned about)
- Require identification of management deflections/evasions
- Include rubric for tone_score

### 2.3 Company News Analyzer Agent

#### 2.3.1 Data Fetching

Fetch all company-specific news within the user-specified period:

```python
result = await news_provider.search_company_news(
    company, ticker,
    max_results=30,  # Tavily max per call
    start_date=request.start_date,
    end_date=request.end_date,
)
```

**Note:** Tavily's `max_results` is capped at 20-30 per call. For long periods,
consider splitting into sub-ranges (monthly or quarterly) and aggregating. This
ensures coverage across the entire period rather than clustering on recent articles.

#### 2.3.2 Define Output Schema

**New file:** `backend/agents/schemas/company_news.py`

| Field | Type | Description |
|---|---|---|
| `articles_analyzed` | `int` | Total articles processed |
| `analysis_period` | `str` | Date range |
| `overall_sentiment` | `SentimentScore` | label + score (0-100) |
| `business_impact_analysis` | `list[BusinessImpact]` | Per-article business impact |
| `sentiment_trend_over_period` | `list[MonthlySentiment]` | Monthly sentiment scores |
| `catalysts` | `list[CatalystItem]` | Positive business drivers |
| `headwinds` | `list[HeadwindItem]` | Negative business pressures |
| `confidence` | `float` | |
| `reasoning` | `str` | |

**BusinessImpact** fields:
- `headline`, `source`, `sentiment`
- `affected_segment` — which business segment is impacted
- `impact_type` — competitive_advantage, regulatory_headwind, market_expansion, etc.
- `magnitude` — high / medium / low
- `time_horizon` — short_term / medium_term / long_term
- `analysis` — free-text explanation of the business impact

#### 2.3.3 System Prompt Design

Key directives:
- "Do NOT just classify articles as positive/negative. For each significant article,
  analyze HOW it impacts the company's actual business operations."
- "Identify which business segment is affected and on what time horizon."
- "Track sentiment trends month-by-month across the analysis period."
- Include rubric for sentiment_score

### 2.4 Macro & Market News Analyzer Agent

#### 2.4.1 Data Fetching

```python
result = await news_provider.search_macro_news(
    max_results=30,
    start_date=request.start_date,
    end_date=request.end_date,
)
# Optionally also use compute_market_sentiment() for the latest snapshot
```

#### 2.4.2 Define Output Schema

**New file:** `backend/agents/schemas/macro_market.py`

| Field | Type | Description |
|---|---|---|
| `analysis_period` | `str` | |
| `current_market_regime` | `str` | risk_on / neutral / risk_off |
| `macro_score` | `int` (0-100) | |
| `macro_environment_over_period` | `list[PeriodRegime]` | How macro shifted over time |
| `sector_impact` | `SectorImpact` | Impact on the target company's sector |
| `key_themes` | `list[ThemeItem]` | Major macro themes + impact_on_company |
| `confidence` | `float` | |
| `reasoning` | `str` | |

### 2.5 YouTube Video Analyzer Agent

#### 2.5.1 Data Fetching

Fetch videos from curated channels within the date range, then get transcripts:

```python
# Get videos from saved channels
saved_channels = channel_store.channel_ids("company")
for cid in saved_channels:
    videos = await youtube_provider.search_videos(
        company_name, channel_id=cid, max_results=10,
        published_after=f"{start_date}T00:00:00Z",
        published_before=f"{end_date}T23:59:59Z",
    )
    for v in videos.videos:
        transcript = youtube_provider.get_transcript(v.video_id)
        # Collect transcript text for analysis
```

#### 2.5.2 Define Output Schema

**New file:** `backend/agents/schemas/youtube_analysis.py`

| Field | Type | Description |
|---|---|---|
| `channels_analyzed` | `int` | |
| `videos_analyzed` | `int` | |
| `analysis_period` | `str` | |
| `per_video_analysis` | `list[VideoAnalysis]` | Deep analysis per video |
| `cross_channel_synthesis` | `CrossChannelSynthesis` | Agreements / disagreements |
| `overall_consensus_score` | `int` (0-100) | |
| `confidence` | `float` | |
| `reasoning` | `str` | |

**VideoAnalysis** fields:
- `channel`, `title`, `published`, `thesis` (bullish/neutral/bearish)
- `key_arguments` — argument text, supporting data, strength (strong/moderate/weak)
- `blind_spots` — what the analyst missed or ignored
- `actionable_insight` — one-line takeaway

This agent does NOT just classify videos as bullish/bearish. It structurally
deconstructs each video's thesis — what data they cite, what logic they use,
where their argument is strong, where it is weak.

#### 2.5.3 System Prompt Design

- "Structurally deconstruct each video's investment thesis."
- "For each argument, evaluate its evidential strength — does the analyst cite
  specific data, or just assert opinions?"
- "Identify blind spots — important factors the analyst failed to consider."
- "Note when different channels disagree — which side has stronger evidence?"

### 2.6 Update `/analyze` for 6-Agent Parallel Execution

**Modify:** `backend/main.py`

```python
@app.post("/analyze")
async def run_analysis(request: AnalyzeRequest):
    # ... validation ...
    
    reports = await asyncio.gather(
        sec_agent.analyze({...}),
        tech_agent.analyze({...}),
        earnings_agent.analyze({...}),
        news_agent.analyze({...}),
        macro_agent.analyze({...}),
        youtube_agent.analyze({...}),
        return_exceptions=True,  # Don't crash if one agent fails
    )
    
    # Handle individual failures gracefully
    results = {}
    for agent_id, report in zip(agent_ids, reports):
        if isinstance(report, Exception):
            results[agent_id] = {"error": str(report)}
        else:
            results[agent_id] = report.model_dump()
    
    return {"agents_completed": len(results), "reports": results}
```

---

## 3. Verification

### 3.1 Per-Agent Tests

For each of the 4 new agents:
1. Run independently with real data → verify schema compliance
2. Manually review output quality (are insights grounded in data?)
3. Test with missing data (e.g., no earnings transcripts found) → graceful handling

### 3.2 Earnings Call — Longitudinal Tracking

1. Test with 3+ quarters of transcripts → verify `promise_vs_delivery` tracking
2. Verify `evolving_themes` shows meaningful progression
3. Check that `dropped_topics` accurately identifies topics that disappeared

### 3.3 Full Pipeline Test

1. Call `POST /analyze` with a 4-quarter range → all 6 agents run in parallel
2. Verify total wall-clock time ≈ slowest agent (not sum of all)
3. Verify `return_exceptions=True` handles individual agent failures gracefully

---

## 4. Files Created / Modified

| Action | File |
|---|---|
| **NEW** | `backend/agents/earnings_call_agent.py` |
| **NEW** | `backend/agents/company_news_agent.py` |
| **NEW** | `backend/agents/macro_market_agent.py` |
| **NEW** | `backend/agents/youtube_agent.py` |
| **NEW** | `backend/agents/schemas/earnings_call.py` |
| **NEW** | `backend/agents/schemas/company_news.py` |
| **NEW** | `backend/agents/schemas/macro_market.py` |
| **NEW** | `backend/agents/schemas/youtube_analysis.py` |
| **MODIFY** | `backend/main.py` (update `/analyze` for 6-agent parallel, add date range) |
| **MODIFY** | `backend/schemas.py` (add `AnalyzeRequest` model) |

---

## 5. Success Criteria

- [ ] All 6 agents produce schema-compliant JSON reports independently
- [ ] Earnings Call Agent successfully performs cross-quarter comparison
  when multiple transcripts are available
- [ ] Company News Agent identifies business impact, not just sentiment labels
- [ ] YouTube Agent deconstructs video theses with argument strength assessment
- [ ] 6 agents run in parallel — wall-clock time ≈ slowest single agent
- [ ] Individual agent failures are isolated and don't crash the pipeline
- [ ] User-specified date range correctly filters all data sources
