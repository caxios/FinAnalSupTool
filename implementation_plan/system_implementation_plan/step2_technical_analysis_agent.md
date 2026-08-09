# Step 2: Technical Analysis (Price Trend) Agent

## Objective

Add the Technical Analysis Agent as the second agent in the pipeline. This step
introduces a **new data source** (`yfinance`) and validates that two agents can
run in parallel and produce independent structured reports.

---

## 1. Prerequisites

- Step 1 completed: Agent framework (`BaseAgent`, `llm_utils`) functional
- SEC Filings Agent producing valid structured output
- At least one company identified (CIK / ticker resolved from uploaded filings)

---

## 2. Tasks

### 2.1 Price Data Pipeline

#### 2.1.1 Create Price Data Provider

**New file:** `backend/price_provider.py`

This module fetches historical price/volume data and computes technical
indicators. All numeric computation happens in Python (not LLM) — the LLM
only receives pre-computed indicator values to interpret.

```python
"""
price_provider.py
─────────────────
Historical price data + technical indicator computation via yfinance.

The LLM does NOT compute these indicators — they are calculated here and
passed to the Technical Analysis Agent as pre-computed facts for interpretation.
"""

import yfinance as yf
import pandas as pd
from dataclasses import dataclass

@dataclass
class TechnicalData:
    """Pre-computed technical indicators for the LLM to interpret."""
    ticker: str
    period_start: str
    period_end: str
    current_price: float
    period_high: float
    period_low: float
    period_return: float           # e.g., 0.12 = +12%
    
    # Moving averages
    sma_50: float | None
    sma_200: float | None
    ema_20: float | None
    golden_cross: bool             # SMA50 > SMA200
    price_vs_sma50: str            # "above" | "below"
    price_vs_sma200: str           # "above" | "below"
    
    # Momentum
    rsi_14: float | None           # 0-100
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    
    # Bollinger Bands
    bb_upper: float | None
    bb_lower: float | None
    bb_position: float | None      # 0-1 (where price sits within the band)
    
    # Volume
    current_volume: int | None
    avg_volume_20d: float | None
    volume_ratio: float | None     # current / avg
    
    # Support / Resistance (from recent price action)
    recent_highs: list[float]
    recent_lows: list[float]
    
    # Monthly price summary for trend visualization
    monthly_closes: list[dict]     # [{"month": "2025-01", "close": 180.5}, ...]


async def fetch_technical_data(
    ticker: str,
    start_date: str,   # YYYY-MM-DD
    end_date: str,      # YYYY-MM-DD
) -> TechnicalData:
    """
    Fetch OHLCV data from yfinance and compute all technical indicators.
    
    All computation is done here in pandas — the LLM receives only the
    final numbers to interpret, not raw price series.
    """
    # Implementation:
    # 1. yf.download(ticker, start=start_date, end=end_date)
    # 2. Compute SMA(50), SMA(200), EMA(20)
    # 3. Compute RSI(14)
    # 4. Compute MACD(12, 26, 9)
    # 5. Compute Bollinger Bands(20, 2)
    # 6. Compute volume averages
    # 7. Identify recent swing highs/lows for S/R levels
    # 8. Aggregate monthly closes
    # 9. Return TechnicalData dataclass
```

#### 2.1.2 Add yfinance Dependency

**Modify:** `backend/requirements.txt`

Add `yfinance` to the requirements.

> **Note:** yfinance provides 15-minute delayed data, which is sufficient for
> daily-level technical analysis. If intraday precision is needed later, this
> can be swapped for Alpha Vantage or FMP.

### 2.2 Technical Analysis Agent

#### 2.2.1 Define Output Schema

**New file:** `backend/agents/schemas/technical_analysis.py`

| Field | Type | Description |
|---|---|---|
| `ticker` | `str` | Stock ticker symbol |
| `analysis_period` | `str` | Date range analyzed |
| `current_price` | `float` | Latest closing price |
| `trend_assessment` | `TrendAssessment` | Primary trend, strength, score (0-100), detail |
| `momentum_indicators` | `MomentumIndicators` | RSI, MACD, volume interpretations |
| `key_levels` | `KeyLevels` | Support and resistance levels with types |
| `pattern_recognition` | `PatternRecognition` | Current chart pattern + implication |
| `price_vs_fundamentals` | `PriceVsFundamentals` | Period return vs fundamental growth |
| `confidence` | `float` | 0-1 |
| `reasoning` | `str` | |

Scoring rubric for `trend_score` (0-100):
- 80+ = Strong uptrend (price above SMA50 > SMA200, RSI 50-70, positive MACD, above-avg volume)
- 60-79 = Moderate uptrend or consolidation with bullish bias
- 40-59 = Sideways / no clear trend
- 20-39 = Moderate downtrend or consolidation with bearish bias
- <20 = Strong downtrend

#### 2.2.2 Implement the Agent

**New file:** `backend/agents/technical_analysis_agent.py`

The agent's `analyze()` method should:
1. Receive ticker + date range from the orchestrator
2. Call `price_provider.fetch_technical_data()` to get pre-computed indicators
3. Format the indicator values as a structured data block for the LLM
4. Call the LLM with a system prompt that instructs:
   - Interpret the technical indicators (do NOT compute — just read the values)
   - Identify the primary trend and assess its strength
   - Identify current chart patterns from the price/volume data
   - Note any divergences (e.g., price rising on declining volume)
   - Score the overall technical outlook using the rubric
5. Parse and validate the JSON response

#### 2.2.3 System Prompt Design

Key elements:
- "You are a technical analysis specialist. You are given PRE-COMPUTED technical
  indicators. Do NOT attempt to recalculate them — interpret the values as given."
- Include rubric for trend_score
- Emphasize: pattern recognition should note reliability level (patterns are
  probabilistic, not deterministic)
- The LLM should explicitly flag when data is insufficient (e.g., <200 days of
  data means SMA200 is unreliable)

### 2.3 Parallel Execution Validation

#### 2.3.1 Update `/analyze` Endpoint

**Modify:** `backend/main.py`

Update the endpoint to run both agents in parallel:

```python
import asyncio

@app.post("/analyze")
async def run_analysis():
    if not _filing_meta:
        raise HTTPException(404, "No filings uploaded yet.")
    
    primary = _primary_company()
    if not primary or not primary.ticker:
        raise HTTPException(404, "Could not identify company ticker.")
    
    sec_agent = SECFilingsAgent()
    tech_agent = TechnicalAnalysisAgent()
    
    # Run both agents in parallel
    sec_report, tech_report = await asyncio.gather(
        sec_agent.analyze({
            "merged_tables": _merged_tables,
            "text_store": _text_store,
            "filing_meta": _filing_meta,
        }),
        tech_agent.analyze({
            "ticker": primary.ticker,
            "start_date": "2025-01-01",  # TODO: user-specified
            "end_date": "2026-06-30",
        }),
    )
    
    return {
        "agents_completed": 2,
        "sec_filings": sec_report.model_dump(),
        "technical_analysis": tech_report.model_dump(),
    }
```

---

## 3. Verification

### 3.1 Price Data Pipeline

1. Test `fetch_technical_data("AAPL", "2025-01-01", "2026-06-30")` independently
2. Verify all indicator values are within expected ranges (RSI 0-100, etc.)
3. Test with a ticker that has limited history → graceful handling of missing SMA200
4. Test with an invalid ticker → clear error message

### 3.2 Technical Agent Output

1. Call the agent with AAPL data → verify output matches schema
2. Manually check: does the `trend_assessment` align with the actual chart?
3. Verify `momentum_indicators` interpretations are consistent with the values
4. Check that `confidence` is lower when data period is short (e.g., <6 months)

### 3.3 Parallel Execution

1. Call `POST /analyze` → verify both reports are returned
2. Measure wall-clock time: should be ~max(SEC, Technical), not sum
3. Test failure isolation: if yfinance fails, SEC agent should still return its report

---

## 4. Files Created / Modified

| Action | File |
|---|---|
| **NEW** | `backend/price_provider.py` |
| **NEW** | `backend/agents/technical_analysis_agent.py` |
| **NEW** | `backend/agents/schemas/technical_analysis.py` |
| **MODIFY** | `backend/requirements.txt` (add yfinance) |
| **MODIFY** | `backend/main.py` (update `/analyze` for parallel execution) |

---

## 5. Success Criteria

- [ ] `price_provider.fetch_technical_data()` returns valid, complete indicator data
- [ ] Technical Agent produces schema-compliant JSON with meaningful analysis
- [ ] Both agents run in parallel via `asyncio.gather()` — wall-clock time ≈ max(individual)
- [ ] Failure in one agent does not crash the other
- [ ] Edge cases handled: short data period, invalid ticker, yfinance timeout
