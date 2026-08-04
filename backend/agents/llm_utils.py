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

import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

import gemini_chat

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_RETRIES = 2


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
    """
    return await gemini_chat._gemini_call(
        system_prompt,
        [{"role": "user", "parts": [{"text": user_prompt}]}],
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
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

    On a JSON-decode or validation error, retries up to `retries` times, each
    time appending the specific error to the prompt so the model can fix its
    output. Raises RuntimeError if no valid response is produced.
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
                f"Structured output attempt {attempt + 1} failed validation: {e}"
            )
            # Feed the error back so the next attempt can self-correct.
            prompt = (
                f"{user_prompt}\n\n"
                f"Your previous response was INVALID and could not be parsed:\n{e}\n"
                f"Return ONLY a single valid JSON object matching the required "
                f"schema. Do not include any prose or code fences."
            )

    raise RuntimeError(
        f"The model did not return valid structured output after "
        f"{retries + 1} attempts. Last error: {last_error}"
    )
