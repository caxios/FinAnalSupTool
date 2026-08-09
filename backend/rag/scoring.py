"""
rag/scoring.py
──────────────
3-axis "gap analysis" scoring — computed PROGRAMMATICALLY from the field agents'
reports (never by the LLM), so the numbers are deterministic and auditable.

The three axes:
  - Fundamental : SEC fundamentals + earnings tone + forward-guidance direction
  - Sentiment   : company news + macro + YouTube consensus + earnings Q&A quality
  - Technical   : the technical agent's trend score

The gap between Fundamental and Sentiment/Technical is the product's core insight:
strong fundamentals the market hasn't priced in ("hidden gem"), or the reverse
("overvaluation warning"). Missing agents are handled by re-normalizing an axis
over just the components that are present.
"""

from __future__ import annotations


def _clamp(v: float) -> int:
    return max(0, min(100, int(round(v))))


def _weighted(components: list[tuple[float | None, float]]) -> int | None:
    """
    Weighted average over (value, weight) pairs, skipping missing values and
    re-normalizing the weights of the ones that remain. Returns None if nothing
    is present.
    """
    present = [(v, w) for v, w in components if v is not None and w > 0]
    if not present:
        return None
    total_w = sum(w for _, w in present)
    return _clamp(sum(v * w for v, w in present) / total_w)


# ── Component extractors (defensive: agents may be missing or mis-shaped) ──

def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _sec_fundamental(reports: dict) -> float | None:
    r = reports.get("sec_filings")
    return _num(r.get("fundamental_score")) if isinstance(r, dict) else None


def _earnings_tone(reports: dict) -> float | None:
    """Latest quarter's tone score (falls back to the mean of the tone trend)."""
    r = reports.get("earnings_call")
    if not isinstance(r, dict):
        return None
    trend = (r.get("longitudinal_tracking") or {}).get("tone_trend_across_quarters") or []
    scores = [_num(t.get("tone_score")) for t in trend if isinstance(t, dict)]
    scores = [s for s in scores if s is not None]
    if scores:
        return scores[-1]  # chronological → latest
    # Fall back to per-quarter management tone.
    pq = r.get("per_quarter_analysis") or []
    tones = [
        _num((q.get("management_tone") or {}).get("tone_score"))
        for q in pq if isinstance(q, dict)
    ]
    tones = [t for t in tones if t is not None]
    return tones[-1] if tones else None


_GUIDANCE_SCORE = {
    "raised": 85, "initiated": 65, "maintained": 55,
    "not_provided": 50, "withdrawn": 20, "lowered": 25,
}


def _earnings_guidance(reports: dict) -> float | None:
    """Map the most recent guidance direction to a 0-100 score."""
    r = reports.get("earnings_call")
    if not isinstance(r, dict):
        return None
    gtrend = (r.get("longitudinal_tracking") or {}).get("guidance_trend") or []
    directions = [t.get("direction") for t in gtrend if isinstance(t, dict)]
    directions = [d for d in directions if d]
    if not directions:
        pq = r.get("per_quarter_analysis") or []
        directions = [
            (q.get("forward_guidance") or {}).get("direction")
            for q in pq if isinstance(q, dict)
        ]
        directions = [d for d in directions if d]
    if not directions:
        return None
    return _GUIDANCE_SCORE.get(str(directions[-1]).lower(), 50)


_QA_QUALITY_SCORE = {"direct": 80, "partial": 50, "evasive": 25}


def _earnings_qa(reports: dict) -> float | None:
    """Average answer-quality across Q&A topics (a candor proxy for sentiment)."""
    r = reports.get("earnings_call")
    if not isinstance(r, dict):
        return None
    vals: list[float] = []
    for q in r.get("per_quarter_analysis") or []:
        if not isinstance(q, dict):
            continue
        for topic in q.get("qa_key_topics") or []:
            if isinstance(topic, dict):
                s = _QA_QUALITY_SCORE.get(str(topic.get("response_quality")).lower())
                if s is not None:
                    vals.append(s)
    if vals:
        return sum(vals) / len(vals)
    return _earnings_tone(reports)  # fall back to tone if no Q&A structure


def _news_sentiment(reports: dict) -> float | None:
    r = reports.get("company_news")
    if not isinstance(r, dict):
        return None
    return _num((r.get("overall_sentiment") or {}).get("score"))


def _macro(reports: dict) -> float | None:
    r = reports.get("macro_market")
    return _num(r.get("macro_score")) if isinstance(r, dict) else None


def _youtube(reports: dict) -> float | None:
    r = reports.get("youtube_analysis")
    return _num(r.get("overall_consensus_score")) if isinstance(r, dict) else None


def _technical(reports: dict) -> float | None:
    r = reports.get("technical_analysis")
    if not isinstance(r, dict):
        return None
    return _num((r.get("trend_assessment") or {}).get("trend_score"))


# ── Signal interpretation matrix (Step 5 §2.1.3) ──

def interpret_signal(fund: int | None, sent: int | None, tech: int | None) -> str:
    """Map the three scores to an actionable signal."""
    if fund is None or sent is None or tech is None:
        return "insufficient_data"
    f_high = fund >= 65
    s_low = sent < 50
    t_low = tech < 50

    if f_high and s_low and t_low:
        return "hidden_gem"              # strong fundamentals, market ignoring, price falling
    if f_high and s_low and not t_low:
        return "discovery_in_progress"   # fundamentals strong, sentiment catching up
    if f_high and not s_low and not t_low:
        return "consensus_bullish"       # all aligned positive
    if not f_high and not s_low and not t_low:
        return "overvaluation_warning"   # sentiment/price ahead of fundamentals
    if not f_high and s_low and t_low:
        return "justified_decline"       # all aligned negative
    if f_high and not s_low and t_low:
        return "temporary_pullback"      # fundamentals + sentiment ok, technical dip
    return "mixed_signals"


SIGNAL_LABELS: dict[str, dict] = {
    "hidden_gem": {"label": "Hidden Gem", "tone": "positive"},
    "discovery_in_progress": {"label": "Discovery in Progress", "tone": "positive"},
    "consensus_bullish": {"label": "Consensus Bullish", "tone": "positive"},
    "overvaluation_warning": {"label": "Overvaluation Warning", "tone": "negative"},
    "justified_decline": {"label": "Justified Decline", "tone": "negative"},
    "temporary_pullback": {"label": "Temporary Pullback", "tone": "neutral"},
    "mixed_signals": {"label": "Mixed Signals", "tone": "neutral"},
    "insufficient_data": {"label": "Insufficient Data", "tone": "neutral"},
}


def compute_three_axis_scores(reports: dict) -> dict:
    """
    Compute the three composite scores + gaps + signal from the agent reports.

    `reports` maps agent_id → report dict (successful agents only). Returns a
    JSON-friendly dict; axes with no supporting agent come back as None.
    """
    sec = _sec_fundamental(reports)
    e_tone = _earnings_tone(reports)
    e_guid = _earnings_guidance(reports)
    e_qa = _earnings_qa(reports)
    news = _news_sentiment(reports)
    macro = _macro(reports)
    yt = _youtube(reports)
    tech = _technical(reports)

    fundamental = _weighted([(sec, 0.45), (e_tone, 0.25), (e_guid, 0.30)])
    sentiment = _weighted([(news, 0.35), (macro, 0.25), (yt, 0.15), (e_qa, 0.25)])
    technical = _clamp(tech) if tech is not None else None

    def gap(a: int | None, b: int | None) -> int | None:
        return None if a is None or b is None else a - b

    signal = interpret_signal(fundamental, sentiment, technical)
    return {
        "fundamental_score": fundamental,
        "sentiment_score": sentiment,
        "technical_score": technical,
        "fundamental_sentiment_gap": gap(fundamental, sentiment),
        "fundamental_technical_gap": gap(fundamental, technical),
        "overall_signal": signal,
        "signal_label": SIGNAL_LABELS.get(signal, {}).get("label", signal),
        "signal_tone": SIGNAL_LABELS.get(signal, {}).get("tone", "neutral"),
        "components": {
            "sec_fundamental": sec, "earnings_tone": e_tone,
            "earnings_guidance": e_guid, "earnings_qa": e_qa,
            "news_sentiment": news, "macro": macro, "youtube": yt,
            "technical_trend": tech,
        },
    }
