"""
services.ingestion
───────────────────
The per-file SEC-filing ingestion pipeline, extracted from the HTTP layer so it
can be reused by *any* source of a PDF on disk — a manual upload (POST /upload)
or an automated SEC EDGAR fetch (POST /sec/fetch) — without duplicating a line
of the extraction logic.

``ingest_pdf`` takes a PDF that already lives at ``dest`` (inside the store's
temp dir) and runs the full extraction:

  1. Detect form type (10-K/10-Q) and filing period from the cover page.
  2. Extract financial tables (XBRL-first, pdfplumber fallback).
  3. Extract qualitative text sections (MD&A, Footnotes, …).
  4. Persist everything into the injected ``DocumentStore``.

It returns a :class:`FilingMeta` describing the outcome. It does **not** rebuild
the cross-period merged tables — the caller does that once after ingesting a
batch (see ``store.rebuild_merged_tables``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from schemas import FilingMeta
from parsers.pdf_utils import (
    detect_filing_metadata,
    extract_all_sections,
    extract_tables,
)
from providers.edgar_xbrl import (
    build_xbrl_statement_tables,
    parse_period_end,
    resolve_company_identity,
)
from services.storage import DocumentStore

logger = logging.getLogger(__name__)


async def ingest_pdf(
    dest: Path,
    filename: str,
    store: DocumentStore,
) -> FilingMeta:
    """
    Run the full extraction pipeline over a single PDF already saved at ``dest``.

    Args:
        dest:     Path to the PDF on disk (must already exist, typically inside
                  ``store.upload_dir``).
        filename: Display name for the file (used in metadata + as a key
                  fallback). Usually ``dest.name``.
        store:    The DocumentStore to persist extracted data into.

    Returns:
        A FilingMeta describing what was detected and whether extraction
        succeeded, partially succeeded, or failed.
    """
    # ── Step 2: Detect form type and filing period ────────
    try:
        meta = detect_filing_metadata(dest)
    except Exception as e:
        logger.error(f"Metadata detection failed for {filename}: {e}")
        return FilingMeta(
            filename=filename,
            status="failed",
            message=f"Could not read PDF metadata: {e}",
        )

    form_type = meta.get("form_type")

    # Can't proceed without knowing the form type
    if not form_type:
        logger.warning(f"Could not detect form type for {filename}")
        return FilingMeta(
            filename=filename,
            status="failed",
            message="Could not detect form type (10-K or 10-Q) from the PDF.",
        )

    # Parse the period-end date — the basis for both a unique, sortable
    # period key and (later) matching XBRL facts to this filing.
    period_end = parse_period_end(meta.get("period"))
    # Provisional key used only if we can't derive anything better.
    provisional_key = meta.get("period_key") or Path(filename).stem

    # ── Step 3: Extract financial tables ──────────────────
    # XBRL-first: try SEC EDGAR's machine-readable XBRL data, which gives
    # exact, standardized numbers with no PDF-table guesswork. Fall back
    # to pdfplumber only if the company can't be identified or EDGAR fails.
    detected_cik: int | None = None
    xbrl_metrics: dict | None = None
    xbrl_label: str | None = None
    try:
        (xbrl_tables, detected_cik, xbrl_metrics, xbrl_label) = (
            await build_xbrl_statement_tables(
                dest,
                filename=filename,
                form_type=form_type,
                period_str=meta.get("period"),
            )
        )
    except Exception as e:
        logger.error(f"XBRL extraction failed for {filename}: {e}")
        xbrl_tables = None

    # Finalize the period key. Prefer XBRL's authoritative fiscal label
    # (e.g. "Q2 FY2026"); otherwise derive a unique, sortable key from the
    # period-end date. This MUST be unique per filing — otherwise Q1/Q2/Q3
    # of the same year would share one key and silently overwrite each
    # other (the bug where same-year uploads were "skipped").
    period_key = xbrl_label or store.derive_period_key(
        form_type, period_end, provisional_key
    )

    classified_tables: dict = {}
    data_source = "pdfplumber"

    if xbrl_tables is not None:
        classified_tables = xbrl_tables
        data_source = "xbrl"
        store.table_store[period_key] = classified_tables
        # Store the raw metrics so the Financial Ratios tab can be built.
        if xbrl_metrics is not None:
            store.metrics_store[period_key] = xbrl_metrics
        table_count = sum(len(v) for v in classified_tables.values())
        logger.info(
            f"  [{period_key}] tables from XBRL (CIK {detected_cik}): "
            f"{table_count} statement table(s)"
        )
    else:
        # No XBRL for this period — drop any stale ratio metrics so a
        # re-upload that falls back to pdfplumber doesn't show old ratios.
        store.metrics_store.pop(period_key, None)
        # Fallback: parse tables out of the PDF with pdfplumber.
        try:
            classified_tables = extract_tables(dest)
            store.table_store[period_key] = classified_tables
            table_count = sum(len(v) for v in classified_tables.values())
            logger.info(
                f"  [{period_key}] tables extracted (pdfplumber): {table_count}"
            )
        except Exception as e:
            logger.error(f"Table extraction failed for {filename}: {e}")
            classified_tables = {}

    # ── Step 4: Extract text sections ─────────────────────
    try:
        sections, page_offsets = extract_all_sections(dest, form_type=form_type)
        store.text_store[period_key] = sections
        store.page_map_store[period_key] = page_offsets
        found = [k for k, v in sections.items() if v is not None]
        logger.info(f"  Text sections extracted: {found}")
    except Exception as e:
        logger.error(f"Text extraction failed for {filename}: {e}")
        sections = {}

    # ── Step 5: Store filing metadata ─────────────────────
    # Persist the detected CIK, table source, and a `sort_date` so the
    # merged tables can be ordered chronologically. Also resolve the company
    # name/ticker (from data already fetched) so the Company Media view
    # knows which company to look up.
    entity_name: str | None = None
    ticker: str | None = None
    if detected_cik is not None:
        try:
            entity_name, ticker = await resolve_company_identity(detected_cik)
        except Exception as e:
            logger.warning(f"Company identity resolution failed: {e}")

    store.filing_meta[period_key] = {
        "filename": filename,
        "form_type": form_type,
        "period": meta.get("period"),
        "period_key": period_key,
        "cik": detected_cik,
        "entity_name": entity_name,
        "ticker": ticker,
        "data_source": data_source,
        "sort_date": period_end.isoformat() if period_end else None,
    }

    # ── Determine overall processing status ──────────────
    has_tables = bool(classified_tables and any(classified_tables.values()))
    has_text = bool(sections and any(v is not None for v in sections.values()))

    if has_tables and has_text:
        status = "success"
        message = None
    elif has_tables or has_text:
        # Got something but not everything
        status = "partial"
        parts = []
        if not has_tables:
            parts.append("no tables detected")
        if not has_text:
            parts.append("no text sections detected")
        message = "Partial extraction: " + ", ".join(parts)
    else:
        status = "failed"
        message = "No tables or text sections could be extracted from this PDF."

    return FilingMeta(
        filename=filename,
        detected_period=period_key,
        form_type=form_type,
        status=status,
        message=message,
    )
