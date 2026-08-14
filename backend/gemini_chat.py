"""
gemini_chat.py
──────────────
Google Gemini-powered Q&A over the uploaded filing data.

The assistant answers natural-language questions ("How did gross margin
trend?", "Why did operating income turn negative in FY2025?") using ONLY
the data already in the app:

  - The merged financial statements + ratios (structured XBRL numbers)
  - The extracted filing text sections (MD&A, Risk Factors, Footnotes, …)

It does not call SEC EDGAR live — everything it sees is assembled from the
in-memory stores populated by POST /upload and handed in by the caller.

Configuration (environment variables)
─────────────────────────────────────
  GEMINI_API_KEY   (required)  — your Google AI Studio API key
  GEMINI_MODEL     (optional)  — model id, default "gemini-2.5-flash"

We call Gemini's REST API directly with httpx (already a dependency), so
there's no extra SDK to install.
"""

from __future__ import annotations

import os
import re
import logging

import httpx
import pandas as pd

from parsers.pdf_utils import SECTION_LABELS

logger = logging.getLogger(__name__)


# =============================================================================
# Errors
# =============================================================================

class GeminiRetryableError(RuntimeError):
    """
    A TRANSIENT Gemini failure that is worth retrying: rate limiting (429), a
    server-side/overload error (5xx), or a network failure.

    It subclasses RuntimeError so every existing caller that catches
    RuntimeError behaves exactly as before; callers that want to back off and
    retry (see agents/llm_utils.py) can catch this specific type instead.

    `retry_after` carries the server's own suggested wait, in seconds, when it
    supplies one — Gemini returns it as a RetryInfo detail on 429.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after_seconds(payload: dict, message: str) -> float | None:
    """
    Pull the server's suggested retry delay out of an error response.

    Preferred source is the structured RetryInfo detail ({"retryDelay": "42s"});
    we fall back to the human-readable "Please retry in 42.8s." in the message.
    """
    details = (payload.get("error") or {}).get("details") or []
    for d in details:
        delay = d.get("retryDelay") if isinstance(d, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                pass

    m = re.search(r"retry in ([0-9.]+)\s*s", message or "", re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# =============================================================================
# Configuration
# =============================================================================

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
_DEFAULT_MODEL = "gemini-2.5-flash"
_HTTP_TIMEOUT = 90.0

# NOTE: Filing text sections are no longer truncated by a static character cap.
# The whole section is passed in full, UNLESS the combined text is so large it
# exceeds the model's optimal window — in which case the caller assembles a
# RAG-retrieved excerpts block (see rag/sec_rag.py) and passes it as
# `filing_text_override`, so nothing is lost by position, only by relevance.

# Sliding window for chat history. The frontend resends the entire conversation
# every turn, and the system prompt already carries the full filing context, so
# an unbounded history makes per-turn cost grow with conversation length.
# 6 messages ≈ the last 3 exchanges — enough for pronouns and follow-ups
# ("what about the previous quarter?") without hauling the whole session along.
_MAX_HISTORY_MESSAGES = 6
_MAX_HISTORY_CHARS = 24_000     # ~6K tokens, a second guard for very long turns


def gemini_api_key() -> str | None:
    """Return the configured Gemini API key, or None if unset."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return key or None


def _model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "").strip() or _DEFAULT_MODEL


# =============================================================================
# Context Assembly
# =============================================================================

_STATEMENT_TITLES = {
    "balance_sheet": "Balance Sheet",
    "income_statement": "Income Statement",
    "cash_flow": "Cash Flow Statement",
    "ratios": "Financial Ratios",
}


def _df_to_markdown(df: pd.DataFrame) -> str:
    """Render a DataFrame as a compact Markdown pipe table for the LLM."""
    cols = list(df.columns)
    lines = [
        "| " + " | ".join(str(c) for c in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            cells.append("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _ordered_period_keys(filing_meta: dict[str, dict]) -> list[str]:
    """Period keys sorted chronologically (oldest → newest)."""
    def key(pk: str):
        d = filing_meta.get(pk, {}).get("sort_date")
        return (0, d) if d else (1, str(pk))
    return sorted(filing_meta.keys(), key=key)


def build_context(
    merged_tables: dict[str, pd.DataFrame],
    text_store: dict[str, dict],
    filing_meta: dict[str, dict],
    extra_context: str | None = None,
    *,
    filing_text_override: str | None = None,
) -> str:
    """
    Assemble the full data context that the assistant is allowed to reason over:
    financials + ratios + filing text, plus any `extra_context` (the latest
    media/macro data cached from Views 2 & 3, pre-formatted as Markdown).

    Filing text is included IN FULL (no character cap). When the combined text is
    too large for the model's optimal window, the caller passes a pre-assembled
    RAG excerpts block as `filing_text_override`; it is then used verbatim in
    place of rendering every section from `text_store`.

    Returns a single Markdown string. Empty stores yield a short notice.
    """
    parts: list[str] = []

    # ── Which filings are loaded ──
    ordered = _ordered_period_keys(filing_meta)
    if ordered:
        parts.append("# Uploaded Filings")
        for pk in ordered:
            m = filing_meta.get(pk, {})
            parts.append(
                f"- **{pk}** — {m.get('form_type', '?')}, "
                f"period ended {m.get('period', 'unknown')} "
                f"(source: {m.get('data_source', '?')})"
            )
        parts.append("")

    # ── Financial statements + ratios ──
    has_numbers = False
    for stmt_key in ("balance_sheet", "income_statement", "cash_flow", "ratios"):
        df = merged_tables.get(stmt_key)
        if df is None or df.empty:
            continue
        has_numbers = True
        title = _STATEMENT_TITLES.get(stmt_key, stmt_key)
        note = (
            " (values in USD millions unless noted; EPS per share; "
            "ratios as multiples/percent)"
            if stmt_key != "ratios"
            else ""
        )
        parts.append(f"# {title}{note}")
        parts.append(_df_to_markdown(df))
        parts.append("")

    # ── Filing text sections ──
    if filing_text_override and filing_text_override.strip():
        # RAG-retrieved excerpts assembled by the caller (the full sections were
        # too large to stuff verbatim). Used as-is; no per-section rendering.
        has_text = True
        parts.append(filing_text_override.strip())
        parts.append("")
    else:
        has_text = False
        for pk in ordered:
            sections = text_store.get(pk, {})
            available = {k: v for k, v in sections.items() if v}
            if not available:
                continue
            has_text = True
            parts.append(f"# Filing Text — {pk}")
            for section_key, content in available.items():
                label = SECTION_LABELS.get(section_key, section_key)
                # Full section text — no static truncation. Oversized combined
                # text is handled upstream via RAG (filing_text_override).
                parts.append(f"## {label}")
                parts.append(content.strip())
                parts.append("")

    # ── Media / macro context (from Views 2 & 3, if the user has visited them) ──
    has_extra = bool(extra_context and extra_context.strip())
    if has_extra:
        parts.append(extra_context.strip())
        parts.append("")

    if not has_numbers and not has_text and not has_extra:
        return "(No filing data has been uploaded yet.)"

    return "\n".join(parts)


# =============================================================================
# System Prompt
# =============================================================================

_SYSTEM_PROMPT_TEMPLATE = """\
You are a financial analysis assistant embedded in a tool that parses SEC \
filings (10-K / 10-Q) and also gathers company news, analysis videos, and \
market-sentiment data. Answer the user's questions using ONLY the data \
provided in this prompt (financial statements, ratios, filing text, and any \
news / video transcripts / market-sentiment sections present below).

Rules:
- Base every claim on the DATA section. Do not invent numbers or use outside \
knowledge about the company's actuals.
- For news/sentiment, attribute to the source and note it reflects that \
outlet's reporting, not verified fact.
- If the data needed to answer isn't present, say so plainly and tell the \
user which filing to upload or which view (Company Media / Macro Sentiment) \
to open so it gets fetched.
- Quantify where possible: cite the specific line item, period, and value. \
When useful, compute changes/growth from the numbers given.
- Financial statement values are in USD millions unless the label says \
otherwise; EPS is per share; ratios are multiples (e.g. 1.88x) or percentages.
- Be concise and use Markdown (short paragraphs, bullet points, or small \
tables). Note important caveats (e.g. 10-Q figures are quarterly and not \
annualized).

=== DATA ===
{context}
=== END DATA ===
"""


def _build_system_prompt(context: str) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(context=context)


# =============================================================================
# Gemini Call
# =============================================================================

def _to_gemini_contents(
    history: list[dict],
    question: str,
    *,
    max_messages: int = _MAX_HISTORY_MESSAGES,
    max_chars: int = _MAX_HISTORY_CHARS,
) -> list[dict]:
    """
    Convert a simple [{role, content}] history + the new question into
    Gemini's `contents` format. Roles map: user→"user", assistant→"model".

    A SLIDING WINDOW is applied to the history, because the frontend sends the
    whole conversation on every turn and the system prompt already carries the
    full filing context. Without a bound, each turn resends every prior turn —
    cost grows quadratically with conversation length.

    Two independent bounds, both counted from the most recent message backwards:
      - `max_messages`: how many turns are kept at all.
      - `max_chars`:    a size budget, so a few very long turns (a pasted
                        transcript, a long tabular answer) can't blow the
                        request up even when the message count is small.

    The current `question` is always sent in full and is never counted against
    the budget — dropping it would defeat the purpose of the call.
    """
    normalized: list[dict] = []
    for msg in history:
        role = "model" if msg.get("role") in ("assistant", "model") else "user"
        text = str(msg.get("content", "")).strip()
        if not text:
            continue
        normalized.append({"role": role, "parts": [{"text": text}]})

    # Newest-first walk, keeping turns until either bound is reached.
    window: list[dict] = []
    used = 0
    for msg in reversed(normalized):
        if max_messages and len(window) >= max_messages:
            break
        size = len(msg["parts"][0]["text"])
        if max_chars and used + size > max_chars and window:
            break
        window.append(msg)
        used += size
    window.reverse()

    # Gemini expects the exchange to start with a user turn; a window that
    # happens to begin with a model reply would be a dangling answer anyway.
    while window and window[0]["role"] == "model":
        window.pop(0)

    dropped = len(normalized) - len(window)
    if dropped:
        logger.info(
            f"Chat history trimmed: sent {len(window)} of {len(normalized)} "
            f"prior messages ({used:,} chars), dropped {dropped} older turns."
        )

    window.append({"role": "user", "parts": [{"text": question}]})
    return window


async def _gemini_call(
    system_instruction: str,
    contents: list[dict],
    *,
    temperature: float = 0.3,
    max_output_tokens: int = 2048,
    response_mime_type: str | None = None,
) -> str:
    """
    Low-level Gemini generateContent call shared by the chat assistant, the
    one-shot helpers (transcript summaries, sentiment synthesis), and the MAS
    agents (via agents/llm_utils.py).

    Pass `response_mime_type="application/json"` to force valid-JSON output
    (used by the structured-output agents). Raises RuntimeError with a
    user-friendly message on any failure.
    """
    api_key = gemini_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set on the server. Add it to the backend "
            "environment and restart to enable AI features."
        )

    url = _GEMINI_URL.format(model=_model_name())
    generation_config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if response_mime_type:
        generation_config["responseMimeType"] = response_mime_type
    body = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": generation_config,
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as e:
        # Network-level failures (timeout, reset) are transient — retryable.
        logger.error(f"Gemini request failed: {e}")
        raise GeminiRetryableError(f"Could not reach the Gemini API: {e}")

    if resp.status_code != 200:
        detail = resp.text
        payload: dict = {}
        try:
            payload = resp.json()
            detail = payload.get("error", {}).get("message", detail)
        except Exception:
            pass
        logger.error(f"Gemini API error {resp.status_code}: {detail}")
        message = f"Gemini API error ({resp.status_code}): {detail}"
        # 429 (rate limit / quota) and 5xx (overloaded) are transient; a 400 for
        # a malformed request or a 403 for a bad key will never fix itself.
        if resp.status_code == 429 or resp.status_code >= 500:
            raise GeminiRetryableError(
                message, _retry_after_seconds(payload, detail)
            )
        raise RuntimeError(message)

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        feedback = data.get("promptFeedback", {})
        raise RuntimeError(
            f"Gemini returned no answer. {feedback or 'Try rephrasing your question.'}"
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise RuntimeError("Gemini returned an empty answer.")
    return answer


async def ask_gemini(
    question: str,
    history: list[dict],
    context: str,
) -> str:
    """
    Chat: send the question (with data context + prior turns) to Gemini.

    Raises RuntimeError on configuration/API errors (endpoint surfaces as HTTP).
    """
    return await _gemini_call(
        _build_system_prompt(context),
        _to_gemini_contents(history, question),
    )


async def ask_persona(
    question: str,
    history: list[dict],
    system_prompt: str,
) -> str:
    """
    Role-scoped chat: like `ask_gemini`, but the caller supplies the ENTIRE system
    prompt (persona + its isolated data context) instead of the generic filings
    template. Used by the per-agent ("talk to one analyst") chat, where each
    agent must be grounded ONLY in its own data + the debate transcript.

    The same history sliding window applies, so a long conversation with one
    agent doesn't inflate per-turn cost.
    """
    return await _gemini_call(
        system_prompt,
        _to_gemini_contents(history, question),
    )


async def gemini_generate(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_output_tokens: int = 1024,
) -> str:
    """
    One-shot generation helper (no chat history). Used for transcript
    summaries and market-sentiment synthesis.
    """
    return await _gemini_call(
        system,
        [{"role": "user", "parts": [{"text": user}]}],
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
