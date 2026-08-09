"""
agents/youtube_agent.py
───────────────────────
YouTube Video Analyzer Agent.

Pulls videos published inside the analysis period from the user's curated
company channels (falling back to a keyword search when no channels are saved),
fetches each video's caption transcript, and asks the LLM to STRUCTURALLY
DECONSTRUCT each thesis — the arguments made, the evidence behind them, and the
blind spots — then to reconcile the channels against each other.

Transcript fetching is a blocking, network-bound library call, so it runs in
worker threads with bounded concurrency.
"""

from __future__ import annotations

import asyncio
import logging

import channel_store
from providers import youtube_provider

from .base_agent import BaseAgent
from .schemas.youtube_analysis import YouTubeAnalysisReport

logger = logging.getLogger(__name__)

_PER_CHANNEL_VIDEOS = 10     # candidates pulled per saved channel
_MAX_VIDEOS = 8              # videos actually transcribed + analyzed
_MAX_TRANSCRIPT_CHARS = 24_000
_TRANSCRIPT_CONCURRENCY = 4


_SYSTEM_PROMPT = """\
You are the YouTube Video Analyzer, a specialist agent in a multi-agent financial
analysis system. You are given full transcripts of analyst/commentary videos
about one company, each labelled with its channel, title and publish date.

You do NOT simply classify videos as bullish or bearish. You STRUCTURALLY
DECONSTRUCT each video's investment thesis: what is claimed, what evidence backs
the claim, how strong that evidence is, and what the analyst failed to consider.

You output ONLY a single JSON object matching this structure:
{
  "confidence": <float 0-1>,
  "reasoning": "<2-4 sentence summary of what the commentary establishes>",
  "company": "<company name>",
  "channels_analyzed": <int>,
  "videos_analyzed": <int>,
  "analysis_period": "<YYYY-MM-DD..YYYY-MM-DD>",
  "per_video_analysis": [
    {
      "channel": "<channel name>",
      "title": "<video title>",
      "published": "<date or null>",
      "video_id": "<id or null>",
      "thesis": "bullish|neutral|bearish",
      "key_arguments": [
        {"argument": "<the claim being made>",
         "supporting_data": "<the specific data cited, or 'none — asserted without evidence'>",
         "strength": "strong|moderate|weak"}
      ],
      "blind_spots": ["<important factor the analyst ignored or waved away>"],
      "actionable_insight": "<one-line takeaway>"
    }
  ],
  "cross_channel_synthesis": {
    "agreements": ["<point multiple channels independently make>"],
    "disagreements": [
      {"topic": "<point of contention>",
       "positions": ["<channel A's view>", "<channel B's view>"],
       "stronger_side": "<which side has better evidence, and why>"}
    ],
    "consensus_view": "<where the commentary nets out overall>"
  },
  "overall_consensus_score": <int 0-100>
}

OVERALL_CONSENSUS_SCORE (0-100): 0 = uniformly bearish commentary, 50 = split or
neutral, 100 = uniformly bullish. Weight each video by the STRENGTH OF ITS
EVIDENCE, not by how loudly the view is stated.

ENUM FIELDS — copy one of the listed values EXACTLY, never prose:
- `thesis`: bullish | neutral | bearish
- `strength`: strong | moderate | weak

ARGUMENT STRENGTH — judge evidence, not confidence of delivery:
- "strong"   = cites specific, verifiable data (filings figures, unit economics,
               named contracts, dated guidance) and reasons from it correctly.
- "moderate" = a plausible argument with partial or indirect evidence.
- "weak"     = bare assertion, vibes, price-action extrapolation, appeals to
               authority, or a claim contradicted elsewhere in the transcript.
A confident tone is NOT evidence. Say so plainly when a strong claim rests on
nothing.

BLIND SPOTS (required for every video):
- Identify what the analyst failed to consider: valuation when the case is all
  growth, competition when the case is all product, financing/dilution when the
  case is all TAM, execution risk, regulatory exposure, or a base-rate the
  argument ignores.
- If a video is genuinely thorough, return a short list saying what remains
  unaddressed rather than inventing a flaw.

CROSS-CHANNEL RECONCILIATION:
- Note where channels AGREE independently — convergence from separate analysts
  carries more weight than any single view.
- Note where they DISAGREE, and adjudicate: which side cites better evidence?
  Do not split the difference to seem balanced; name the stronger side and why.

RULES:
- Ground every claim in the transcript text; never use outside knowledge about
  the company and never invent data the analyst did not cite.
- Copy `channel`, `title`, `published` and `video_id` from the provided metadata.
- Transcripts are auto-generated captions and may be noisy or non-English —
  interpret meaning, and lower confidence when a transcript is too garbled to
  read reliably.
- CONFIDENCE reflects source quality and coverage: several substantive videos
  from different channels → 0.7+; a single video, or thin/rambling commentary →
  0.3-0.5. This is opinion data, not primary source data — rarely exceed 0.8.
"""

_USER_TEMPLATE = """\
Deconstruct the following video commentary about {company}{ticker} and produce
the JSON report.

Analysis period: {period}
Channels represented: {channels}
Videos with transcripts: {count}{skipped}

{videos}
"""


async def _gather_videos(
    label: str, ticker: str | None, after: str, before: str
) -> tuple[list, int]:
    """
    Collect candidate videos from the saved 'company' channels, or fall back to a
    keyword search when the user hasn't curated any. Returns (videos, channel_count).
    """
    saved = channel_store.channel_ids("company")

    if saved:
        results = await asyncio.gather(
            *(
                youtube_provider.search_videos(
                    label, channel_id=cid, max_results=_PER_CHANNEL_VIDEOS,
                    published_after=after, published_before=before,
                )
                for cid in saved[:6]
            ),
            return_exceptions=True,
        )
        videos: list = []
        for cid, res in zip(saved[:6], results):
            if isinstance(res, Exception):
                logger.warning(f"YouTube search failed for channel {cid}: {res}")
                continue
            if not res.configured:
                raise RuntimeError(
                    res.message
                    or "Videos are not configured: set YOUTUBE_API_KEY on the backend."
                )
            videos.extend(res.videos)
        videos.sort(key=lambda v: v.published or "", reverse=True)
        return videos, len({v.channel for v in videos})

    result = await youtube_provider.search_company_videos(
        label, ticker, max_results=_MAX_VIDEOS,
        published_after=after, published_before=before,
    )
    if not result.configured:
        raise RuntimeError(
            result.message
            or "Videos are not configured: set YOUTUBE_API_KEY on the backend."
        )
    return result.videos, len({v.channel for v in result.videos})


async def _fetch_transcripts(videos: list) -> list[tuple[object, str]]:
    """Fetch transcripts in worker threads, bounded concurrency. Skips captionless videos."""
    sem = asyncio.Semaphore(_TRANSCRIPT_CONCURRENCY)

    async def one(video):
        async with sem:
            try:
                return video, await asyncio.to_thread(
                    youtube_provider.get_transcript, video.video_id
                )
            except Exception as e:
                logger.warning(f"Transcript fetch failed for {video.video_id}: {e}")
                return video, None

    pairs = await asyncio.gather(*(one(v) for v in videos))
    return [
        (video, tr.text)
        for video, tr in pairs
        if tr is not None and tr.available and tr.text
    ]


class YouTubeAgent(BaseAgent):
    """Structural deconstruction of analyst video theses about the company."""

    @property
    def agent_id(self) -> str:
        return "youtube_analysis"

    async def analyze(self, context: dict, capture: dict | None = None) -> YouTubeAnalysisReport:
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
            raise RuntimeError("YouTube analysis requires a company name or ticker.")

        label = company or ticker or ""
        after = f"{start_date}T00:00:00Z"
        before = f"{end_date}T23:59:59Z"

        candidates, channel_count = await _gather_videos(label, ticker, after, before)
        if not candidates:
            raise RuntimeError(
                f"No videos about {label} were found in {start_date}..{end_date}. "
                f"Add company channels in the Videos panel, or widen the date range."
            )

        selected = candidates[:_MAX_VIDEOS]
        transcribed = await _fetch_transcripts(selected)
        if not transcribed:
            raise RuntimeError(
                f"Found {len(selected)} video(s) about {label}, but none had "
                f"captions available to analyze."
            )

        blocks: list[str] = []
        for video, text in transcribed:
            body = text[:_MAX_TRANSCRIPT_CHARS]
            truncated = " [TRANSCRIPT TRUNCATED]" if len(text) > _MAX_TRANSCRIPT_CHARS else ""
            blocks.append(
                f"=== VIDEO ===\n"
                f"channel: {video.channel}\n"
                f"title: {video.title}\n"
                f"published: {video.published or 'unknown'}\n"
                f"video_id: {video.video_id}\n"
                f"transcript:\n{body}{truncated}\n"
                f"=== END VIDEO ==="
            )

        skipped = len(selected) - len(transcribed)
        user_prompt = _USER_TEMPLATE.format(
            company=label,
            ticker=f" ({ticker})" if ticker else "",
            period=f"{start_date}..{end_date}",
            channels=channel_count or "unknown",
            count=len(transcribed),
            skipped=f" ({skipped} skipped — no captions)" if skipped else "",
            videos="\n\n".join(blocks),
        )
        if capture is not None:
            capture["raw_data"] = user_prompt

        # Argument-by-argument deconstruction of several videos runs long, and
        # Gemini's thinking tokens draw from the same budget — too small a
        # ceiling truncates the JSON mid-string and burns retries.
        report = await self._generate_report(
            YouTubeAnalysisReport, _SYSTEM_PROMPT, user_prompt,
            max_output_tokens=32768,
        )

        # Ground the counts in what was actually analyzed.
        report.company = label
        report.videos_analyzed = len(transcribed)
        report.channels_analyzed = len({v.channel for v, _ in transcribed})
        report.analysis_period = f"{start_date}..{end_date}"
        return report
