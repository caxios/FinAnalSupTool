"""
market_sentiment.py
───────────────────
Market-wide sentiment synthesis for View 3.

There's no dedicated market-data feed wired in yet (VIX / Fear & Greed), so we
derive a qualitative sentiment read by aggregating recent macro headlines
(via news_provider / Tavily) and asking Gemini to synthesize:

  - an overall label (bullish / neutral / bearish) + a 0–100 score
  - a short narrative summary
  - a few themed indicator cards (theme → direction → note)

Everything degrades gracefully: if Tavily or Gemini isn't configured, we return
a structured `configured=False` payload the UI renders as a "connect a key"
state. The layout leaves clear seams to plug a numeric market-data source later.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from providers.news_provider import search_macro_news, tavily_api_key
from gemini_chat import gemini_generate, gemini_api_key

logger = logging.getLogger(__name__)


# =============================================================================
# Result types
# =============================================================================

@dataclass
class SentimentIndicator:
    theme: str
    direction: str   # "bullish" | "neutral" | "bearish"
    note: str


@dataclass
class SentimentResult:
    configured: bool
    label: str = "unknown"          # bullish | neutral | bearish | unknown
    score: int | None = None        # 0 (max bearish) – 100 (max bullish)
    summary: str = ""
    indicators: list[SentimentIndicator] = field(default_factory=list)
    headline_count: int = 0
    message: str | None = None


_SENTIMENT_SYSTEM = """\
You are a markets analyst. Given a set of recent macro/market news headlines and \
snippets, assess overall U.S. equity market sentiment. Respond with STRICT JSON \
only (no markdown, no prose) matching this schema:

{
  "label": "bullish" | "neutral" | "bearish",
  "score": <integer 0-100, where 0 is maximally bearish and 100 maximally bullish>,
  "summary": "<2-3 sentence narrative grounded in the headlines>",
  "indicators": [
    {"theme": "<short theme, e.g. Monetary Policy>",
     "direction": "bullish" | "neutral" | "bearish",
     "note": "<one short sentence>"}
  ]
}

Provide 3-5 indicators. Base everything ONLY on the provided headlines."""


def _parse_json_block(text: str) -> dict | None:
    """Extract a JSON object from a model reply (tolerates code fences)."""
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


async def compute_market_sentiment() -> SentimentResult:
    """
    Fetch macro headlines and synthesize a market-sentiment read.

    Returns a graceful `configured=False` result when the prerequisites
    (Tavily for news, Gemini for synthesis) aren't set.
    """
    if not tavily_api_key():
        return SentimentResult(
            configured=False,
            message="Market sentiment needs TAVILY_API_KEY (for macro news) to "
                    "be set on the backend.",
        )
    if not gemini_api_key():
        return SentimentResult(
            configured=False,
            message="Market sentiment needs GEMINI_API_KEY (for synthesis) to "
                    "be set on the backend.",
        )

    news = await search_macro_news(max_results=12)
    if not news.configured:
        return SentimentResult(configured=False, message=news.message)
    if not news.articles:
        return SentimentResult(
            configured=True,
            label="unknown",
            summary="No recent macro headlines were returned to assess.",
            headline_count=0,
        )

    # Build the prompt payload from headlines + snippets.
    lines = []
    for a in news.articles:
        snippet = a.snippet[:280]
        lines.append(f"- [{a.source}] {a.title}. {snippet}")
    payload = "Recent macro/market headlines:\n" + "\n".join(lines)

    try:
        raw = await gemini_generate(
            _SENTIMENT_SYSTEM, payload, temperature=0.2, max_output_tokens=900
        )
    except RuntimeError as e:
        return SentimentResult(configured=False, message=str(e))

    parsed = _parse_json_block(raw)
    if not parsed:
        # Fall back to a summary-only result if JSON parsing failed.
        return SentimentResult(
            configured=True,
            label="neutral",
            summary=raw[:600],
            headline_count=len(news.articles),
        )

    indicators = [
        SentimentIndicator(
            theme=str(i.get("theme", "")).strip(),
            direction=str(i.get("direction", "neutral")).strip().lower(),
            note=str(i.get("note", "")).strip(),
        )
        for i in parsed.get("indicators", [])
        if i.get("theme")
    ]

    score = parsed.get("score")
    try:
        score = int(score) if score is not None else None
    except (ValueError, TypeError):
        score = None

    return SentimentResult(
        configured=True,
        label=str(parsed.get("label", "neutral")).strip().lower(),
        score=score,
        summary=str(parsed.get("summary", "")).strip(),
        indicators=indicators,
        headline_count=len(news.articles),
    )
