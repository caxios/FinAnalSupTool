"""
agents/earnings_call_agent.py
─────────────────────────────
Earnings Call Analyzer Agent.

Fetches EVERY quarterly earnings-call transcript inside the user's analysis
period (one Tavily lookup per quarter, run concurrently) and asks the LLM for a
longitudinal read: per-quarter substance plus explicit cross-quarter tracking of
what management promised versus what they later delivered.

Transcripts are large (~30-50K tokens each). Gemini's context window absorbs a
handful of them, but each transcript is capped at `_MAX_TRANSCRIPT_CHARS` so a
long period can't blow the request size — the cap is generous enough to keep
prepared remarks AND the Q&A section, which is where the signal is.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import news_provider
import rag

from .base_agent import BaseAgent
from .schemas.earnings_call import EarningsCallReport

logger = logging.getLogger(__name__)

# Per-transcript character budget (~20K tokens each).
_MAX_TRANSCRIPT_CHARS = 80_000
# Upper bound on quarters fetched, so an over-long range can't fan out forever.
_MAX_QUARTERS = 8


def date_range_to_quarters(start: str, end: str) -> list[tuple[int, int]]:
    """
    Convert a YYYY-MM-DD range to the list of (year, quarter) it spans.

    e.g. "2025-01-01" → "2026-06-30" yields
    [(2025,1), (2025,2), (2025,3), (2025,4), (2026,1), (2026,2)].

    These are CALENDAR quarters; a company on an offset fiscal year may label
    them differently, which the transcript search tolerates.
    """
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        s, e = e, s

    out: list[tuple[int, int]] = []
    year, quarter = s.year, (s.month - 1) // 3 + 1
    last = (e.year, (e.month - 1) // 3 + 1)
    while (year, quarter) <= last:
        out.append((year, quarter))
        quarter += 1
        if quarter > 4:
            year, quarter = year + 1, 1
    return out


_SYSTEM_PROMPT = """\
You are the Earnings Call Analyzer, a specialist agent in a multi-agent financial
analysis system. You are given the full earnings-call transcripts for one company
across several consecutive quarters, each labelled with its quarter.

Your job is DEEP BUSINESS SUBSTANCE analysis across time — not sentiment
labelling. Anyone can say a call "sounded confident"; you must say what changed
in the business, what management committed to, and whether they delivered.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of the cross-quarter picture>",
  "company": "<company name>",
  "quarters_analyzed": ["Q1 2025", "Q2 2025", ...],
  "quarters_missing": ["<quarter with no transcript>", ...],
  "per_quarter_analysis": [
    {
      "quarter": "Q1 2025",
      "source": "<source domain or null>",
      "management_tone": {
        "overall": "<confident|optimistic|neutral|cautious|defensive>",
        "confidence_level": "high|medium|low",
        "tone_score": <int 0-100>,
        "detail": "<language/hedging evidence>"
      },
      "qa_key_topics": [
        {"topic": "<what analysts pressed on>", "question_count": <int>,
         "response_quality": "direct|partial|evasive",
         "note": "<what management said, and what they avoided>"}
      ],
      "business_substance": {
        "key_developments": [
          {"area": "<segment/business area>", "detail": "<substantive development with numbers cited on the call>", "significance": "high|medium|low"}
        ],
        "strategic_shifts": ["<stated change in strategy or priorities>"]
      },
      "forward_guidance": {
        "direction": "raised|maintained|lowered|initiated|withdrawn|not_provided",
        "detail": "<what was guided and to what numbers>"
      }
    }
  ],
  "longitudinal_tracking": {
    "promise_vs_delivery": [
      {"promise_quarter": "Q1 2025", "promise": "<what management said would happen>",
       "outcome_quarter": "Q2 2025", "outcome": "<what they actually reported>",
       "verdict": "delivered|partially_delivered|missed|too_early"}
    ],
    "evolving_themes": [
      {"theme": "<theme>", "trajectory": "<how it changed across quarters>", "assessment": "<what that implies>"}
    ],
    "tone_trend_across_quarters": [{"quarter": "Q1 2025", "tone_score": <int 0-100>}],
    "guidance_trend": [{"quarter": "Q1 2025", "direction": "raised|maintained|lowered|initiated|withdrawn|not_provided"}],
    "new_topics_not_in_previous": ["<topic raised for the first time>"],
    "dropped_topics": ["<topic previously discussed, now absent>"]
  }
}

ENUM FIELDS — copy one of the listed values EXACTLY, never prose:
- `confidence_level` and `significance`: high | medium | low
- `response_quality`: direct | partial | evasive
- `direction`: raised | maintained | lowered | initiated | withdrawn | not_provided
  (relative to the PRIOR quarter's guidance: use `initiated` when guidance is
  issued with no prior figure to compare, and `not_provided` when management
  gave no guidance at all. Never write 'provided' — it is not a direction.)
- `verdict`: delivered | partially_delivered | missed | too_early
(`overall` is free text.) The rubric below describes the SCORE — never put a
rubric phrase in an enum field.

RUBRIC for the NUMERIC `tone_score` (0-100), judged on LANGUAGE, not results:
- 80-100 = Confident and specific: firm commitments, concrete numbers, direct
           answers to hard questions.
- 60-79  = Positive but hedged: optimistic framing with qualifiers.
- 40-59  = Neutral/balanced: measured language, mixed messages.
- 20-39  = Cautious/defensive: heavy hedging, deflected questions, blame shifted
           to external factors.
- 0-19   = Alarmed: withdrawn guidance, admissions of material problems.

MANDATORY CROSS-QUARTER WORK (the core of this analysis):
- For each consecutive pair of quarters, ask: what did management promise in
  Q(n-1), and what actually happened in Q(n)? Record every traceable commitment
  in `promise_vs_delivery` with a verdict. A commitment whose outcome quarter is
  not yet in the data → "too_early".
- Track which topics APPEAR for the first time and which QUIETLY DISAPPEAR. A
  dropped topic (e.g. a growth initiative that stops being mentioned) is a
  meaningful negative signal — do not overlook it.
- `tone_trend_across_quarters` and `guidance_trend` must have one entry per
  analyzed quarter, in chronological order.

Q&A ANALYSIS:
- Count how many questions hit each topic — repetition shows what analysts are
  actually worried about.
- Flag DEFLECTION explicitly: when management answers a different question than
  the one asked, gives no number where a number was requested, or redirects to
  boilerplate, mark `response_quality` as "partial" or "evasive" and say so in
  the note.

RULES:
- Ground EVERY claim in the transcript text provided. Never use outside knowledge
  about the company's results, and never invent figures.
- Only quarters present in the data may appear in `quarters_analyzed`.
- CONFIDENCE reflects data coverage: 3+ consecutive transcripts → 0.8+; two →
  ~0.6-0.7; a single transcript means NO real longitudinal analysis is possible,
  so return empty cross-quarter lists and set confidence ~0.35-0.5.
"""

_USER_TEMPLATE = """\
Analyze the following earnings-call transcripts for {company}{ticker} and produce
the JSON report.

Quarters WITH a transcript below (oldest → newest): {found}
Quarters in the requested range with NO transcript found: {missing}

{transcripts}
"""


class EarningsCallAgent(BaseAgent):
    """Longitudinal analysis of earnings-call transcripts across quarters."""

    @property
    def agent_id(self) -> str:
        return "earnings_call"

    async def analyze(self, context: dict, capture: dict | None = None) -> EarningsCallReport:
        """
        Args:
            context: {"company": str, "ticker": str | None,
                      "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}
            capture: optional side-channel for the assembled raw-data prompt.
        """
        company = (context.get("company") or "").strip()
        ticker = (context.get("ticker") or "").strip() or None
        start_date = context["start_date"]
        end_date = context["end_date"]
        if not (company or ticker):
            raise RuntimeError("Earnings call analysis requires a company name or ticker.")

        label = company or ticker or ""
        quarters = date_range_to_quarters(start_date, end_date)
        if len(quarters) > _MAX_QUARTERS:
            # Keep the most recent quarters — they carry the most signal.
            quarters = quarters[-_MAX_QUARTERS:]

        # One transcript lookup per quarter, all in flight together.
        docs = await asyncio.gather(
            *(
                news_provider.search_earnings_transcript(label, ticker, year, q)
                for year, q in quarters
            ),
            return_exceptions=True,
        )

        # Chronological (oldest → newest) tuples of the transcripts we secured.
        fetched: list[tuple[str, str, str]] = []   # (quarter_label, source, full_text)
        found: list[str] = []
        missing: list[str] = []
        for (year, q), doc in zip(quarters, docs):
            qlabel = f"Q{q} {year}"
            if isinstance(doc, Exception):
                logger.warning(f"Transcript fetch failed for {qlabel}: {doc}")
                missing.append(qlabel)
                continue
            if not doc.configured:
                raise RuntimeError(
                    doc.message
                    or "Transcripts are not configured: set TAVILY_API_KEY on the backend."
                )
            if not doc.found or not doc.text:
                missing.append(qlabel)
                continue
            found.append(qlabel)
            fetched.append((qlabel, doc.source or "unknown", doc.text))

        if not fetched:
            raise RuntimeError(
                f"No earnings-call transcripts were found for {label} in "
                f"{start_date}..{end_date} (quarters tried: "
                f"{', '.join(f'Q{q} {y}' for y, q in quarters)})."
            )

        # ── Conditional RAG: for LONG periods, retrieve historical excerpts by
        # topic instead of stuffing every full transcript (which explodes the
        # prompt). Short periods keep the simpler full-context path. Any RAG
        # failure falls back to full stuffing, so the agent always produces.
        total_tokens = rag.estimate_tokens("".join(t for _, _, t in fetched))
        transcripts_block: str | None = None
        if len(fetched) >= 3 and rag.should_use_rag(total_tokens):
            try:
                transcripts_block = await rag.earnings_rag.prepare_context(
                    fetched, ticker=ticker,
                    run_id=str(context.get("run_id") or "adhoc"),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Earnings RAG failed, falling back to full context: {e}")
                transcripts_block = None
            if transcripts_block:
                logger.info(
                    f"Earnings agent using RAG for {len(fetched)} quarters "
                    f"(~{total_tokens:,} est. tokens)."
                )

        if transcripts_block is None:
            blocks = []
            for qlabel, source, text in fetched:
                body = text[:_MAX_TRANSCRIPT_CHARS]
                truncated = " [TRANSCRIPT TRUNCATED]" if len(text) > _MAX_TRANSCRIPT_CHARS else ""
                blocks.append(
                    f"=== TRANSCRIPT: {qlabel} (source: {source}) ===\n"
                    f"{body}{truncated}\n"
                    f"=== END TRANSCRIPT: {qlabel} ==="
                )
            transcripts_block = "\n\n".join(blocks)

        user_prompt = _USER_TEMPLATE.format(
            company=label,
            ticker=f" ({ticker})" if ticker else "",
            found=", ".join(found),
            missing=", ".join(missing) or "(none)",
            transcripts=transcripts_block,
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # Per-quarter breakdown plus cross-quarter tracking is a long report, and
        # Gemini's thinking tokens draw from the same budget — too small a
        # ceiling truncates the JSON mid-string and burns retries.
        report = await self._generate_report(
            EarningsCallReport, _SYSTEM_PROMPT, user_prompt,
            max_output_tokens=32768,
        )

        # Ground the factual bookkeeping in what we actually fetched.
        report.company = label
        report.quarters_analyzed = found
        report.quarters_missing = missing
        return report
