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
import logging

import httpx
import pandas as pd

from pdf_utils import SECTION_LABELS

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
_DEFAULT_MODEL = "gemini-2.5-flash"
_HTTP_TIMEOUT = 90.0

# Per-section character cap when assembling context. Filing text sections
# (especially MD&A / Risk Factors) can be enormous; this keeps the prompt
# bounded while still giving the model the substance to reason over.
_SECTION_CHAR_CAP = 12_000


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
) -> str:
    """
    Assemble the full data context (financials + ratios + filing text) that
    the assistant is allowed to reason over.

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
            text = content.strip()
            if len(text) > _SECTION_CHAR_CAP:
                text = text[:_SECTION_CHAR_CAP] + "\n…[truncated]…"
            parts.append(f"## {label}")
            parts.append(text)
            parts.append("")

    if not has_numbers and not has_text:
        return "(No filing data has been uploaded yet.)"

    return "\n".join(parts)


# =============================================================================
# System Prompt
# =============================================================================

_SYSTEM_PROMPT_TEMPLATE = """\
You are a financial analysis assistant embedded in a tool that parses SEC \
filings (10-K / 10-Q). Answer the user's questions about the companies and \
periods below using ONLY the data provided in this prompt.

Rules:
- Base every claim on the DATA section. Do not invent numbers or use outside \
knowledge about the company's actuals.
- If the data needed to answer isn't present, say so plainly and tell the \
user which filing/period to upload.
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
    history: list[dict], question: str
) -> list[dict]:
    """
    Convert a simple [{role, content}] history + the new question into
    Gemini's `contents` format. Roles map: user→"user", assistant→"model".
    """
    contents: list[dict] = []
    for msg in history:
        role = "model" if msg.get("role") in ("assistant", "model") else "user"
        text = str(msg.get("content", "")).strip()
        if not text:
            continue
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": question}]})
    return contents


async def ask_gemini(
    question: str,
    history: list[dict],
    context: str,
) -> str:
    """
    Send the question (with data context + prior turns) to Gemini and return
    the answer text.

    Raises:
        RuntimeError with a user-friendly message on configuration or API
        errors (the endpoint surfaces this as an HTTP error detail).
    """
    api_key = gemini_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set on the server. Add it to the backend "
            "environment and restart to enable the assistant."
        )

    model = _model_name()
    url = _GEMINI_URL.format(model=model)
    body = {
        "system_instruction": {"parts": [{"text": _build_system_prompt(context)}]},
        "contents": _to_gemini_contents(history, question),
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
        },
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
        logger.error(f"Gemini request failed: {e}")
        raise RuntimeError(f"Could not reach the Gemini API: {e}")

    if resp.status_code != 200:
        # Surface Gemini's error message when available.
        detail = resp.text
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        logger.error(f"Gemini API error {resp.status_code}: {detail}")
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {detail}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        # e.g. blocked by safety filters
        feedback = data.get("promptFeedback", {})
        raise RuntimeError(
            f"Gemini returned no answer. {feedback or 'Try rephrasing your question.'}"
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise RuntimeError("Gemini returned an empty answer. Try rephrasing.")

    return answer
