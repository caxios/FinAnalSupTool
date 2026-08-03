# Step 6: RAG, Analysis History, and Frontend Integration

## Objective

Add persistent storage (RAG with vector DB for efficient retrieval, plus
standard DB for analysis history), optimize the pipeline for long analysis
periods (6+ quarters), and integrate the MAS analysis into the React frontend.

---

## 1. Prerequisites

- Steps 1-5 completed: Full MAS pipeline functional end-to-end
- Pipeline validated on multiple companies with 1-2 quarter analyses
- Output quality confirmed to be actionable and accurate

---

## 2. Tasks

### 2.1 RAG (Retrieval-Augmented Generation)

#### 2.1.1 When RAG Activates

RAG is **conditionally activated** based on total data volume. For short
analysis periods, context stuffing remains simpler and equally effective.

```python
RAG_THRESHOLD_TOKENS = 150_000  # ~2-3 quarters of full data

def should_use_rag(total_estimated_tokens: int) -> bool:
    return total_estimated_tokens >= RAG_THRESHOLD_TOKENS
```

#### 2.1.2 Vector Database Setup

**Technology choice:** ChromaDB (local, zero infrastructure, pip install)

**New file:** `backend/rag/vector_store.py`

```python
"""
vector_store.py
───────────────
ChromaDB-based vector store for long-period analysis.

Collections:
  - earnings_transcripts: Chunked earnings call transcripts
  - sec_filings_text: MD&A and Risk Factors sections
  - youtube_transcripts: Video transcript chunks
  - analysis_history: Past MAS analysis results
"""

import chromadb
from chromadb.config import Settings

# Persistent local storage
_client = chromadb.PersistentClient(
    path="./chroma_data",
    settings=Settings(anonymized_telemetry=False),
)

# Collections
_earnings_collection = _client.get_or_create_collection(
    name="earnings_transcripts",
    metadata={"description": "Earnings call transcript chunks by quarter"},
)

_analysis_history = _client.get_or_create_collection(
    name="analysis_history",
    metadata={"description": "Past MAS analysis results"},
)
```

#### 2.1.3 Embedding Strategy

Use Gemini's embedding model (already have API key):

```python
async def embed_text(text: str) -> list[float]:
    """
    Generate embedding using Gemini's text-embedding model.
    Model: text-embedding-004
    """
    # Call Gemini embedding API
    # Returns 768-dimensional vector
```

#### 2.1.4 Chunking Strategy

Different data types need different chunking approaches:

| Data Type | Chunk Strategy | Chunk Size | Metadata |
|---|---|---|---|
| Earnings Call | Q&A turn boundaries (speaker change) | ~500-1000 tokens per turn | quarter, speaker_role, topic |
| SEC Filing Text | Section-level (MD&A, Risk Factors as units) | Section-capped at 2000 tokens | period, section_key |
| YouTube Transcript | Semantic paragraphs (topic shifts) | ~500 tokens | channel, video_id, published |

```python
def chunk_earnings_transcript(
    transcript: str,
    quarter: str,
) -> list[dict]:
    """
    Split an earnings call transcript into Q&A turns.
    
    Each chunk gets metadata: {quarter, speaker_role, approx_topic}
    so retrieval can target "What did management say about AI in Q3?"
    """
```

#### 2.1.5 RAG-Enhanced Earnings Call Agent

When RAG is active, the Earnings Call Agent changes its data retrieval:

```python
# WITHOUT RAG (short period):
# Feed ALL transcripts to the agent as full text

# WITH RAG (long period):
# Feed current quarter transcript in full
# For previous quarters: retrieve relevant chunks via semantic search

async def prepare_earnings_context_rag(
    current_quarter: str,
    current_transcript: str,
    query_topics: list[str],  # Key topics to search for in past quarters
) -> str:
    """
    Build context using RAG for multi-quarter analysis.
    
    1. Current quarter: full transcript
    2. Past quarters: semantic search for topic-relevant chunks
    """
    context_parts = [f"## {current_quarter} (Full Transcript)\n{current_transcript}"]
    
    for topic in query_topics:
        results = _earnings_collection.query(
            query_texts=[topic],
            n_results=5,
            where={"quarter": {"$ne": current_quarter}},  # Exclude current
        )
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            context_parts.append(
                f"## {meta['quarter']} — Related to '{topic}'\n{doc}"
            )
    
    return "\n\n".join(context_parts)
```

### 2.2 Analysis History

#### 2.2.1 Storage

Save each MAS analysis run so users can:
- Compare current analysis with previous runs
- Track how the gap has changed over time
- See if previous predictions were validated

**New file:** `backend/rag/history_store.py`

```python
import json
from pathlib import Path
from datetime import datetime

_HISTORY_DIR = Path(__file__).parent.parent / "analysis_history"

def save_analysis(
    company: str,
    ticker: str,
    report: dict,
    analysis_period: str,
) -> str:
    """Save a completed analysis to disk and vector store."""
    _HISTORY_DIR.mkdir(exist_ok=True)
    
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = _HISTORY_DIR / f"{ticker}_{run_id}.json"
    
    record = {
        "run_id": run_id,
        "company": company,
        "ticker": ticker,
        "analysis_period": analysis_period,
        "timestamp": datetime.now().isoformat(),
        "report": report,
    }
    filepath.write_text(json.dumps(record, indent=2))
    
    # Also embed in vector store for semantic retrieval
    _analysis_history.add(
        documents=[json.dumps(report.get("executive_summary", ""))],
        metadatas=[{
            "ticker": ticker,
            "run_id": run_id,
            "analysis_period": analysis_period,
            "fundamental_score": report.get("three_axis_scores", {}).get("fundamental_score"),
            "sentiment_score": report.get("three_axis_scores", {}).get("sentiment_score"),
            "technical_score": report.get("three_axis_scores", {}).get("technical_score"),
            "signal": report.get("three_axis_scores", {}).get("overall_signal"),
        }],
        ids=[run_id],
    )
    
    return run_id


def get_analysis_history(ticker: str, limit: int = 10) -> list[dict]:
    """Retrieve past analyses for a ticker, newest first."""
    files = sorted(
        _HISTORY_DIR.glob(f"{ticker}_*.json"),
        reverse=True,
    )[:limit]
    return [json.loads(f.read_text()) for f in files]
```

#### 2.2.2 History API Endpoints

**Modify:** `backend/main.py`

```python
@app.get("/analysis/history")
async def analysis_history(ticker: str = Query(...)):
    """Get past analysis runs for a ticker."""
    return {"history": get_analysis_history(ticker)}

@app.get("/analysis/{run_id}")
async def get_analysis(run_id: str):
    """Get a specific past analysis by run ID."""
    ...
```

### 2.3 Frontend Integration

#### 2.3.1 New "Deep Analysis" View

Add a new view to the React frontend (alongside Dashboard, Company Media,
Macro Sentiment) that:

1. **Analysis Setup Panel:**
   - Date range picker (start date / end date)
   - "Run Analysis" button
   - Loading state with progress indicators per agent

2. **Results Display:**
   - 3-axis score visualization (radar chart or gauge meters)
   - Signal badge (e.g., "🟢 Hidden Gem" or "🔴 Overvaluation Warning")
   - Executive summary card
   - Key findings (expandable, with source attribution visible)
   - Gap analysis narrative
   - Convergence catalysts / risks

3. **Agent Detail Panels (expandable):**
   - Each agent's individual report, viewable on demand
   - Debate log (challenges + responses)

4. **History Sidebar:**
   - Past analysis runs for comparison
   - Gap score trend chart over time

#### 2.3.2 API Integration

**Modify:** `frontend/src/api.ts`

```typescript
export async function runAnalysis(startDate: string, endDate: string) {
  const response = await fetch(`${API_BASE}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start_date: startDate, end_date: endDate }),
  });
  return response.json();
}

export async function getAnalysisHistory(ticker: string) {
  const response = await fetch(`${API_BASE}/analysis/history?ticker=${ticker}`);
  return response.json();
}
```

#### 2.3.3 Real-Time Progress (Optional Enhancement)

Since the full pipeline takes ~60-90 seconds, consider streaming progress
updates via Server-Sent Events (SSE) or WebSocket:

```python
@app.post("/analyze/stream")
async def run_analysis_stream(request: AnalyzeRequest):
    async def event_generator():
        yield f"data: {json.dumps({'phase': 1, 'status': 'running', 'agents_completed': 0})}\n\n"
        
        # Run agents, yielding progress updates...
        for i, (agent_id, report) in enumerate(completed_reports):
            yield f"data: {json.dumps({'phase': 1, 'status': 'running', 'agents_completed': i+1, 'agent': agent_id})}\n\n"
        
        yield f"data: {json.dumps({'phase': 2, 'status': 'debating'})}\n\n"
        # ... debate ...
        
        yield f"data: {json.dumps({'phase': 3, 'status': 'synthesizing'})}\n\n"
        # ... manager ...
        
        yield f"data: {json.dumps({'phase': 3, 'status': 'complete', 'report': final_report})}\n\n"
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

---

## 3. Verification

### 3.1 RAG Quality

1. Index 6 quarters of earnings transcripts → query "AI investment" →
   verify retrieved chunks are relevant and from correct quarters
2. Compare RAG-enhanced analysis with context-stuffing analysis for the same
   period → verify quality is equivalent or better
3. Measure token savings: RAG should reduce per-agent input by 40-60% for
   6+ quarter analyses

### 3.2 Analysis History

1. Run analysis 3 times over a week → verify all runs are saved and retrievable
2. Verify gap score trend tracking across multiple runs
3. Test with multiple tickers → histories are isolated per company

### 3.3 Frontend

1. Run full analysis from the UI → verify progress indicators work
2. Verify all report sections render correctly
3. Test responsiveness (mobile, tablet, desktop)
4. Verify history sidebar shows past runs and allows comparison

### 3.4 End-to-End Performance

| Analysis Period | Target Time | Target Cost |
|---|---|---|
| 1-2 quarters | < 60 seconds | < $0.30 |
| 4 quarters (with RAG) | < 90 seconds | < $0.40 |
| 6+ quarters (with RAG) | < 120 seconds | < $0.50 |

---

## 4. Files Created / Modified

| Action | File |
|---|---|
| **NEW** | `backend/rag/__init__.py` |
| **NEW** | `backend/rag/vector_store.py` |
| **NEW** | `backend/rag/history_store.py` |
| **NEW** | `backend/rag/chunking.py` |
| **NEW** | `backend/rag/embeddings.py` |
| **NEW** | `frontend/src/views/DeepAnalysis.tsx` (or similar) |
| **NEW** | `frontend/src/components/AnalysisReport.tsx` |
| **NEW** | `frontend/src/components/ThreeAxisChart.tsx` |
| **NEW** | `frontend/src/components/DebateLog.tsx` |
| **MODIFY** | `backend/main.py` (add history endpoints, RAG toggle) |
| **MODIFY** | `backend/requirements.txt` (add chromadb) |
| **MODIFY** | `frontend/src/api.ts` (add analysis API calls) |
| **MODIFY** | `frontend/src/App.tsx` (add Deep Analysis route/view) |

---

## 5. Success Criteria

- [ ] RAG activates automatically when data volume exceeds threshold
- [ ] RAG-enhanced analysis quality is equal to or better than context stuffing
- [ ] Analysis history is persisted across server restarts
- [ ] Past analyses are retrievable and comparable
- [ ] Frontend "Deep Analysis" view renders the full report attractively
- [ ] Progress indicators provide real-time feedback during analysis
- [ ] Performance targets met for all analysis period lengths
- [ ] The system is usable end-to-end by a non-technical user via the UI
