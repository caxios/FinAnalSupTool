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


class _Sources:
    """
    Registry of citable items behind one answer.

    Every block below registers what it renders and gets back a short tag
    ("S1", "S2", …) that goes into the context next to the item. The model
    cites those tags inline, and the UI resolves each one back to this
    record — doc_id, human label, and a link to the primary source where one
    exists. Short tags (not raw doc_ids) because the model has to reproduce
    them exactly, mid-sentence, dozens of times.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._by_key: dict[str, str] = {}

    def add(
        self, *, key: str, kind: str, label: str,
        url: str | None = None, doc_id: str | None = None, excerpt: str | None = None,
    ) -> str:
        """Register (or re-use) a source; returns its tag, e.g. "S3"."""
        if key in self._by_key:
            return self._by_key[key]
        tag = f"S{len(self.items) + 1}"
        self._by_key[key] = tag
        self.items.append({
            "tag": tag, "kind": kind, "label": label,
            "url": url, "doc_id": doc_id,
            "excerpt": (excerpt or "").strip()[:400] or None,
        })
        return tag


def _edgar_filing_url(cik, accession_number: str | None) -> str | None:
    """EDGAR's filing-index page for an accession number, when both parts are
    known — the closest thing to a permalink for an XBRL fact's own filing."""
    if not cik or not accession_number:
        return None
    acc = str(accession_number).replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/"


def _period_label(row: dict) -> str:
    fp, fy = row.get("fiscal_period"), row.get("fiscal_year")
    if not fy:
        return row.get("period_end") and str(row["period_end"]) or "?"
    return f"FY{fy}" if fp in (None, "FY") else f"{fp} FY{fy}"


def _build_facts_table(sql_results: list[dict], sources: _Sources) -> str:
    if not sql_results:
        return ""
    rows = sorted(
        sql_results,
        key=lambda r: (r.get("fiscal_year") or 0, r.get("fiscal_period") or "", r.get("label") or ""),
    )
    lines = [
        "## 1. Verified Financial Facts (Source: SEC EDGAR XBRL — structured_db)",
        "| Fiscal Period | Concept | Value | Unit | Source |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        period = _period_label(r)
        form = r.get("form_type") or "XBRL"
        tag = sources.add(
            key=f"fact:{r.get('ticker')}:{period}:{r.get('accession_number')}",
            kind="financial_fact",
            label=f"{r.get('ticker', '')} {period} {form} — XBRL financial facts".strip(),
            url=_edgar_filing_url(r.get("cik"), r.get("accession_number")),
        )
        lines.append(
            f"| {period} | {r.get('label') or r.get('concept', '')} "
            f"| {_format_value(r.get('value'), r.get('unit'))} | {r.get('unit') or ''} | [{tag}] |"
        )
    return "\n".join(lines)


def _build_footnotes_block(footnote_results: list[dict], sources: _Sources) -> str:
    if not footnote_results:
        return ""
    lines = ["## 1b. Related Footnotes (Source: statement_footnote_links)"]
    for f in footnote_results:
        title = f.get("note_title") or f.get("note_id") or "Note"
        period_key = f.get("period_key", "?")
        tag = sources.add(
            key=f"note:{f.get('ticker')}:{period_key}:{f.get('note_id')}",
            kind="footnote",
            label=f"{title} — {period_key} filing footnote",
            url=f.get("source_url"),
            excerpt=f.get("note_text"),
        )
        lines.append(
            f"\n### [{tag}] {title} — {period_key} "
            f"[{f.get('statement_item', '')}]"
        )
        lines.append(f.get("note_text") or "")
    return "\n".join(lines)


_DOC_TYPE_LABELS = {
    "sec_filing_text": "SEC filing text",
    "earnings_transcript": "Earnings call",
    "news_article": "News",
    "youtube_transcript": "Video transcript",
}


def _excerpt_url(doc_type: str, meta: dict) -> str | None:
    """
    Best link back to the primary source for one retrieved chunk.

    News chunks carry the article URL in their own metadata. Earnings chunks
    don't (the transcript's URL lives with the cached document, not the
    chunk), so it's resolved from services.transcript_cache — which also
    covers transcripts indexed before that metadata existed, without
    re-indexing anything.
    """
    url = meta.get("url")
    if url or doc_type != "earnings_transcript":
        return url
    quarter = str(meta.get("quarter") or meta.get("period") or "")
    ticker = meta.get("ticker")
    if not ticker or "Q" not in quarter:
        return None
    year_str, _, q_str = quarter.partition("Q")
    if not (year_str.isdigit() and q_str.isdigit()):
        return None
    from services import transcript_cache

    doc = transcript_cache.get_transcript(str(ticker), int(year_str), int(q_str))
    return getattr(doc, "url", None) if doc else None


def _build_excerpts_block(search_results: list[SearchHit], sources: _Sources) -> str:
    if not search_results:
        return ""
    lines = ["## 2. Grounded Excerpts (Source: Earnings Calls / News / Filing Text)"]
    for h in search_results:
        meta = h.metadata or {}
        doc_type = meta.get("doc_type", "unknown")
        period = meta.get("period") or meta.get("published_at") or meta.get("quarter") or ""
        text = (h.text or "").strip().replace("\n", " ")
        kind_label = _DOC_TYPE_LABELS.get(doc_type, doc_type)
        section = meta.get("section_key")
        label = " — ".join(
            part for part in (
                f"{kind_label}{f' ({section})' if section else ''}",
                str(period) if period else "",
                meta.get("source") or "",
            ) if part
        )
        tag = sources.add(
            key=f"doc:{h.doc_id}", kind=doc_type, label=label,
            url=_excerpt_url(doc_type, meta), doc_id=h.doc_id, excerpt=text,
        )
        lines.append(f'- [{tag}] ({doc_type} | {period}): "{text}"')
    return "\n".join(lines)


def _build_price_block(price_results: list[dict], sources: _Sources) -> str:
    if not price_results:
        return ""
    lines = ["## 3. Price & Technicals (Source: cached market data)"]
    for p in price_results:
        window = f"{p.get('period_start', '?')} to {p.get('period_end', '?')}"
        tag = sources.add(
            key=f"price:{window}", kind="price",
            label=f"Price & technicals — {window}",
        )
        lines.append(f"\n### [{tag}] Window {window}")
        for key in (
            "current_price", "period_high", "period_low", "period_return",
            "sma_50", "sma_200", "rsi_14", "golden_cross",
            "price_vs_sma50", "price_vs_sma200",
        ):
            if p.get(key) is not None:
                lines.append(f"- {key}: {p[key]}")
    return "\n".join(lines)


def _build_insider_block(insider_results: list[dict], sources: _Sources) -> str:
    if not insider_results:
        return ""
    trades = [r for r in insider_results if r.get("row_type") == "form4"]
    filings = [r for r in insider_results if r.get("row_type") == "8k"]
    lines = ["## 4. Insider Activity & 8-K Events (Source: SEC EDGAR)"]
    if trades:
        tag = sources.add(
            key="insider:form4", kind="insider",
            label="Form 4 insider transactions (SEC EDGAR)",
            url=next((t.get("source_url") for t in trades if t.get("source_url")), None),
        )
        lines.append(f"\n### [{tag}] Form 4 insider transactions")
        for t in trades:
            action = (
                "Buy" if t.get("acquired_or_disposed") == "A"
                else "Sell" if t.get("acquired_or_disposed") == "D"
                else t.get("transaction_code_description") or "?"
            )
            role = t.get("officer_title") or ("Director" if t.get("is_director") else "")
            lines.append(
                f"- {t.get('transaction_date', '?')}: {t.get('owner_name', '?')} "
                f"({role}) {action} {t.get('amount', '?')} shares "
                f"@ {t.get('price_per_share', '?')}"
            )
    if filings:
        tag = sources.add(
            key="insider:8k", kind="filing_8k",
            label="8-K filings (SEC EDGAR)",
            url=next((f.get("document_url") for f in filings if f.get("document_url")), None),
        )
        lines.append(f"\n### [{tag}] 8-K filings")
        for f in filings:
            lines.append(f"- {f.get('filing_date', '?')}: {f.get('title', '?')}")
    return "\n".join(lines)


def build_context(
    sql_results: list[dict],
    footnote_results: list[dict],
    search_results: list[SearchHit],
    price_results: list[dict] | None = None,
    insider_results: list[dict] | None = None,
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
    context, _sources = build_context_with_sources(
        sql_results, footnote_results, search_results,
        price_results=price_results, insider_results=insider_results,
    )
    return context


def build_context_with_sources(
    sql_results: list[dict],
    footnote_results: list[dict],
    search_results: list[SearchHit],
    price_results: list[dict] | None = None,
    insider_results: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """
    Same context, plus the citation registry behind it.

    Every rendered item carries a short ``[S#]`` tag, and the returned list
    maps each tag to what it actually is — label, source link, doc_id and the
    excerpt itself. Callers that want the model to cite its evidence (the chat
    assistant) pass the tags through to the prompt and hand this list to the
    UI so a reader can click any claim back to the record it came from.
    """
    sources = _Sources()
    sections = [
        _build_facts_table(sql_results, sources),
        _build_footnotes_block(footnote_results, sources),
        _build_excerpts_block(search_results, sources),
        _build_price_block(price_results or [], sources),
        _build_insider_block(insider_results or [], sources),
    ]
    body = "\n\n".join(s for s in sections if s)
    return (f"# Context\n\n{body}" if body else ""), sources.items
