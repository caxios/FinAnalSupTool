"""
services.sec_ingest
───────────────────
The "fetch a fiscal range from EDGAR and ingest it" loop, extracted from
``routers/sec.py`` so more than one caller can drive it.

Two callers need this exact sequence — plan the range, render each filing to PDF,
stage it, run the shared ``ingest_pdf`` pipeline, rebuild merged tables once per
company touched:

  1. ``POST /sec/fetch``  — the user names a company + form + year range.
  2. ``services.portfolio_service`` — the 8-quarter baseline auto-fetch that
     fires when a new ticker joins the portfolio.

Keeping the loop here means the partial-failure and rate-limit behaviour is
written once. The function raises the ``sec_fetch`` domain exceptions
(``InvalidRequest``, ``TickerNotFound``, …) rather than ``HTTPException``, so the
non-HTTP caller isn't forced to catch web-layer errors; the router maps them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from schemas import FilingMeta, ResolvedFiling
from services import sec_fetch
from services.ingestion import ingest_pdf, staging_path
from services.storage import DocumentStore

logger = logging.getLogger(__name__)


@dataclass
class IngestRangeResult:
    """Outcome of one range fetch: per-period metadata plus provenance."""

    filings: list[FilingMeta] = field(default_factory=list)
    resolved: list[ResolvedFiling] = field(default_factory=list)
    affected_tickers: set[str] = field(default_factory=set)
    rate_limited: bool = False

    @property
    def succeeded(self) -> int:
        return sum(1 for f in self.filings if f.status in ("success", "partial"))


async def fetch_and_ingest_range(
    *,
    ticker: str,
    form_type: str,
    start_year: int,
    end_year: int,
    start_quarter: int | None,
    end_quarter: int | None,
    store: DocumentStore,
) -> IngestRangeResult:
    """
    Resolve a fiscal range on EDGAR, render every filing, and ingest each one.

    Partial failures are graceful: a period that fails to render or ingest comes
    back as a ``failed`` entry while the others still succeed. If SEC starts
    rate-limiting mid-run, the remaining periods are skipped (rather than
    hammering EDGAR) and reported as failed.

    Raises the ``sec_fetch`` domain exceptions if the *planning* step fails —
    at that point nothing has been fetched, so there is no partial result to
    return.
    """
    # ── Step 1: plan the range (single metadata query, blocking → threadpool) ──
    planned = await run_in_threadpool(
        sec_fetch.plan_filings,
        ticker,
        form_type,
        start_year,
        end_year,
        start_quarter,
        end_quarter,
    )

    result = IngestRangeResult()
    logger.info(f"[sec] {ticker} {form_type}: {len(planned)} filing(s) planned")

    # ── Step 2: render + ingest each period sequentially ──
    for p in planned:
        if result.rate_limited:
            # SEC throttled us earlier this run — don't keep hitting EDGAR.
            result.filings.append(FilingMeta(
                filename=p.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message="Skipped: SEC rate-limited an earlier filing in this "
                        "request. Wait a few minutes and retry a smaller range.",
            ))
            continue

        try:
            fetched = await run_in_threadpool(sec_fetch.render_planned, p)
        except sec_fetch.SecRateLimited as e:
            result.rate_limited = True
            result.filings.append(FilingMeta(
                filename=p.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message=f"SEC rate-limited this request: {e}",
            ))
            continue
        except sec_fetch.SecFetchError as e:
            # Render failure for this one period — record and keep going.
            result.filings.append(FilingMeta(
                filename=p.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message=f"Could not retrieve {p.period_label}: {e}",
            ))
            continue

        # Stage the PDF, then run the shared ingestion pipeline (which routes it
        # into the resolved company's store).
        dest = staging_path(fetched.filename)
        try:
            dest.write_bytes(fetched.pdf_bytes)
            meta = await ingest_pdf(dest, fetched.filename, store)
        except Exception as e:  # noqa: BLE001 — isolate per-period ingestion
            logger.error(f"[sec] ingest failed for {p.period_label}: {e}")
            result.filings.append(FilingMeta(
                filename=fetched.filename,
                detected_period=p.period_label,
                form_type=p.form_type,
                status="failed",
                message=f"Retrieved but failed to ingest {p.period_label}: {e}",
            ))
            continue

        result.filings.append(meta)
        if meta.ticker:
            result.affected_tickers.add(meta.ticker)
        result.resolved.append(ResolvedFiling(
            ticker=fetched.ticker,
            form_type=fetched.form_type,
            period_label=fetched.period_label,
            filing_date=fetched.filing_date,
            accession_number=fetched.accession_number or None,
            document_url=fetched.document_url,
        ))

    # ── Step 3: refresh the merged-tables cache once per company touched ──
    for tk in result.affected_tickers:
        store.get_company_store(tk).rebuild_merged_tables()

    logger.info(
        f"[sec] {ticker} {form_type}: "
        f"{result.succeeded}/{len(result.filings)} ingested"
    )
    return result
