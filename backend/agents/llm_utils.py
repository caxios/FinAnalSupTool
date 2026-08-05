"""
agents/llm_utils.py
───────────────────
Structured-output helper for the MAS agents.

Wraps the existing `gemini_chat._gemini_call()` (reusing its API-key management
and HTTP client) to add:

  - Forced JSON output via Gemini's `responseMimeType: "application/json"`.
  - Pydantic validation of the returned JSON, with up to N retries that feed the
    parse/validation error back to the model so it can self-correct.
  - Consistent error handling (raises RuntimeError on unrecoverable failure).

No Gemini API key logic lives here — it is imported from `gemini_chat`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TypeVar

from pydantic import BaseModel, ValidationError

import gemini_chat

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_RETRIES = 2

# Transient-failure (429 / 5xx / network) retry policy. This is SEPARATE from
# the validation retries below: being rate-limited is not the model's mistake,
# so it must not consume the attempts reserved for fixing malformed JSON.
_TRANSIENT_ATTEMPTS = 4
_BACKOFF_BASE = 1.0     # seconds; doubles each attempt
_BACKOFF_CAP = 60.0     # never sleep longer than this in one wait
_JITTER = 1.0           # random spread, so parallel agents don't retry in lockstep


def _strip_code_fences(text: str) -> str:
    """
    Remove Markdown code fences the model sometimes wraps JSON in, e.g.
    ```json … ``` — so json.loads sees clean content. JSON mode usually avoids
    this, but we stay defensive.
    """
    s = text.strip()
    if s.startswith("```"):
        # Drop the opening fence (``` or ```json) and the trailing fence.
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


async def generate_json(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.2,
    max_output_tokens: int = _DEFAULT_MAX_TOKENS,
) -> str:
    """
    One-shot generation that forces application/json output. Returns the raw
    JSON string (not parsed). Raises RuntimeError on API/config failure.

    Transient failures (rate limiting, overload, network blips) are retried with
    exponential backoff — honouring the server's own suggested delay when it
    sends one. This matters because `/analyze` fires every agent concurrently,
    so a burst can trip a per-minute limit that clears within seconds; without
    this, one 429 would destroy that agent's entire report.
    """
    last: Exception | None = None

    for attempt in range(_TRANSIENT_ATTEMPTS):
        try:
            return await gemini_chat._gemini_call(
                system_prompt,
                [{"role": "user", "parts": [{"text": user_prompt}]}],
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                response_mime_type="application/json",
            )
        except gemini_chat.GeminiRetryableError as e:
            last = e
            if attempt == _TRANSIENT_ATTEMPTS - 1:
                break
            # Prefer the server's advice; otherwise back off exponentially.
            suggested = e.retry_after
            delay = suggested if suggested is not None else _BACKOFF_BASE * (2 ** attempt)
            delay = min(delay, _BACKOFF_CAP) + random.uniform(0, _JITTER)
            logger.warning(
                f"Transient Gemini failure (attempt {attempt + 1}/"
                f"{_TRANSIENT_ATTEMPTS}), retrying in {delay:.1f}s: {e}"
            )
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"Gemini remained unavailable after {_TRANSIENT_ATTEMPTS} attempts. "
        f"Last error: {last}"
    )


def _repair_prompt(malformed: str, error: Exception) -> str:
    """
    Build the retry prompt for a failed validation.

    CRITICAL for cost: this deliberately does NOT include the original
    `user_prompt`. Agent prompts carry the raw source data — earnings-call
    transcripts, months of articles, video transcripts — which for the earnings
    agent alone measured ~154K characters (~38K tokens). Re-sending that on
    every retry multiplied input cost by the number of attempts, for no benefit:
    the model has already done the analysis, and the failure is a JSON
    syntax/schema defect, not an analysis defect.

    So the retry ships only the model's own malformed output plus the exact
    error. The schema still reaches the model via `system_prompt`, which is sent
    on every call anyway and is small by comparison. Nothing is lost, because
    every fact the model extracted is already present in the malformed output —
    this is a repair, not a re-analysis.

    The echoed output needs no length cap: it is inherently bounded by
    `max_output_tokens`, and truncating it here would destroy the very content
    the model has to repair.
    """
    return (
        "Your previous response was NOT valid against the required schema and "
        "could not be used.\n\n"
        f"=== VALIDATION ERROR ===\n{error}\n=== END ERROR ===\n\n"
        f"=== YOUR PREVIOUS OUTPUT ===\n{malformed}\n=== END PREVIOUS OUTPUT ===\n\n"
        "Fix ONLY what the error identifies and return the COMPLETE corrected "
        "JSON object. Preserve every value from your previous output that was "
        "not at fault — do not re-analyze, drop content, or summarize. If the "
        "previous output was cut off mid-way, finish it. Enum fields must use "
        "one of their permitted values exactly. Return ONLY the JSON object, "
        "with no prose and no code fences."
    )


async def generate_structured(
    system_prompt: str,
    user_prompt: str,
    model: type[T],
    *,
    temperature: float = 0.2,
    max_output_tokens: int = _DEFAULT_MAX_TOKENS,
    retries: int = _DEFAULT_RETRIES,
) -> T:
    """
    Generate JSON and validate it against a Pydantic `model`.

    On a JSON-decode or validation error, retries up to `retries` times. The
    retry sends a compact REPAIR prompt (the malformed output + the error), not
    the original data-heavy prompt — see `_repair_prompt`. Raises RuntimeError
    if no valid response is produced.
    """
    last_error: Exception | None = None
    prompt = user_prompt

    for attempt in range(retries + 1):
        raw = await generate_json(
            system_prompt, prompt,
            temperature=temperature, max_output_tokens=max_output_tokens,
        )
        text = _strip_code_fences(raw)
        try:
            data = json.loads(text)
            return model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            logger.warning(
                f"Structured output attempt {attempt + 1} failed validation "
                f"({len(prompt):,}-char prompt): {e}"
            )
            # Retry with a REPAIR prompt, never the original source data.
            prompt = _repair_prompt(text, e)

    raise RuntimeError(
        f"The model did not return valid structured output after "
        f"{retries + 1} attempts. Last error: {last_error}"
    )
