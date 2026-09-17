"""
services.context_builder
───────────────────────────
Renders the strict "verified numbers vs. cited prose" context block the
architecture's whole citation-discipline promise rests on:

    # Context
    ## 1. Verified Financial Facts   (from structured_db — never LLM-touched)
    ## 1b. Related Footnotes         (from structured_db, only if present)
    ## 2. Grounded Excerpts          (from hybrid_search, tagged [Doc_ID: ...])

Section 1/1b are built directly from structured_db rows — nothing here ever
sends a number through an LLM before this point, so what the synthesis model
sees for Section 1 is exactly, verbatim, what's in the database (only
formatted for readability — millions with commas, matching how the rest of
this app already displays these same values — never rounded or recomputed).
Section 2 entries carry the SAME doc_id used in Chroma/FTS5 (Phase 0's
stable-ID convention), so a citation can be traced back to its source chunk.
"""

from __future__ import annotations

from rag.hybrid_search import SearchHit


def _format_value(value: float | None, unit: str | None) -> str:
    """
    Matches providers.edgar_xbrl's own display convention (the Financials
    tab) so a number shown here is never inconsistent with what the user
    sees elsewhere in the app for the identical fact.
    """
    if value is None:
        return "—"
    if unit == "USD/shares":
        return f"{value:,.2f}"
    # USD and shares are both shown in millions.
    return f"{value / 1_000_000.0:,.1f}"


def _period_label(row: dict) -> str:
    fp, fy = row.get("fiscal_period"), row.get("fiscal_year")
    if not fy:
        return row.get("period_end") and str(row["period_end"]) or "?"
    return f"FY{fy}" if fp in (None, "FY") else f"{fp} FY{fy}"


def _build_facts_table(sql_results: list[dict]) -> str:
    if not sql_results:
        return ""
    rows = sorted(
        sql_results,
        key=lambda r: (r.get("fiscal_year") or 0, r.get("fiscal_period") or "", r.get("label") or ""),
    )
    lines = [
        "## 1. Verified Financial Facts (Source: SEC EDGAR XBRL — structured_db)",
        "| Fiscal Period | Concept | Value | Unit |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {_period_label(r)} | {r.get('label') or r.get('concept', '')} "
            f"| {_format_value(r.get('value'), r.get('unit'))} | {r.get('unit') or ''} |"
        )
    return "\n".join(lines)


def _build_footnotes_block(footnote_results: list[dict]) -> str:
    if not footnote_results:
        return ""
    lines = ["## 1b. Related Footnotes (Source: statement_footnote_links)"]
    for f in footnote_results:
        title = f.get("note_title") or f.get("note_id") or "Note"
        lines.append(
            f"\n### {title} — {f.get('period_key', '?')} "
            f"[{f.get('statement_item', '')}]"
        )
        lines.append(f.get("note_text") or "")
    return "\n".join(lines)


def _build_excerpts_block(search_results: list[SearchHit]) -> str:
    if not search_results:
        return ""
    lines = ["## 2. Grounded Excerpts (Source: Earnings Calls / News / Filing Text)"]
    for h in search_results:
        doc_type = h.metadata.get("doc_type", "unknown")
        period = h.metadata.get("period") or h.metadata.get("published_at") or h.metadata.get("quarter") or ""
        text = (h.text or "").strip().replace("\n", " ")
        lines.append(f'- [Doc_ID: {h.doc_id} | {doc_type} | {period}]: "{text}"')
    return "\n".join(lines)


def build_context(
    sql_results: list[dict],
    footnote_results: list[dict],
    search_results: list[SearchHit],
) -> str:
    """
    Assemble the full context block from the three tool outputs. Any of the
    three may be empty (e.g. a search-only question has no sql_results) —
    that section is simply omitted, not rendered as an empty header, so the
    synthesis model never has to distinguish "section present but empty"
    from "section not applicable."

    Returns "" if all three inputs are empty (a sub_task-free or fully-failed
    plan) — the caller (orchestration.graph._synthesize_node) is expected to
    handle an empty context as "nothing was found," not attempt synthesis
    over nothing.
    """
    sections = [
        "# Context",
        _build_facts_table(sql_results),
        _build_footnotes_block(footnote_results),
        _build_excerpts_block(search_results),
    ]
    body = "\n\n".join(s for s in sections[1:] if s)
    return f"# Context\n\n{body}" if body else ""
