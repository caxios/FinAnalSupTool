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
import re
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from providers import edgar_xbrl
from schemas import FilingMeta, ResolvedFiling
from services import filing_cache, sec_fetch
from services.ingestion import ingest_pdf, staging_path
from services.storage import CompanyStore, DocumentStore

logger = logging.getLogger(__name__)

_FY_KEY_RE = re.compile(r"^FY(\d{4})$")


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

    # SEC never files a Q4 10-Q — that quarter's figures live only in the
    # annual 10-K. Auto-include the 10-K for every fiscal year in this range
    # that isn't already in the store, so Q4 can be derived afterward
    # (FY minus the 9-month YTD through Q3, see Step 3) instead of being
    # permanently missing. A year whose 10-K is already ingested is skipped
    # to avoid re-rendering it.
    if form_type == "10-Q":
        norm_ticker = ticker.strip().upper()
        existing_fy: set[str] = set()
        if store.has_company(norm_ticker):
            existing_fy = set(store.get_company_store(norm_ticker).table_store.keys())
        missing_fy = [
            y for y in range(start_year, end_year + 1)
            if f"FY{y}" not in existing_fy
        ]
        if missing_fy:
            try:
                extra = await run_in_threadpool(
                    sec_fetch.plan_filings,
                    ticker, "10-K", min(missing_fy), max(missing_fy),
                )
                planned = planned + [p for p in extra if p.fiscal_year in missing_fy]
            except sec_fetch.SecFetchError as e:
                logger.warning(
                    f"[sec] could not plan Q4-source 10-K(s) for {ticker} "
                    f"FY{min(missing_fy)}-{max(missing_fy)}: {e}"
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

    # ── Step 3: derive Q4 (FY minus 9-month YTD) wherever the annual 10-K
    # and all three 10-Qs are now present, refresh the merged-tables cache
    # once per company touched,
    # and persist the raw text/tables to disk so a later server restart
    # doesn't strand the AI Chat assistant with no evidence for this ticker. ──
    for tk in result.affected_tickers:
        company_store = store.get_company_store(tk)
        if form_type == "10-Q":
            await _derive_q4_periods(company_store)
        company_store.rebuild_merged_tables()
        filing_cache.save_company_store(tk, company_store)

    logger.info(
        f"[sec] {ticker} {form_type}: "
        f"{result.succeeded}/{len(result.filings)} ingested"
    )
    return result


async def _derive_q4_periods(company_store: CompanyStore) -> None:
    """
    Synthesize each fiscal year's standalone "Q4 FY{year}" period wherever the
    10-K and all three 10-Qs are now present but Q4 hasn't been derived yet.

    Cheap even across repeated calls: ``fetch_company_facts`` is cached per
    CIK, so this costs no extra network round-trip once the 10-K has already
    been fetched once for this ticker.
    """
    fiscal_years = {
        int(m.group(1))
        for key in company_store.table_store
        if (m := _FY_KEY_RE.match(key))
    }
    for fy in fiscal_years:
        q4_key = f"Q4 FY{fy}"
        if q4_key in company_store.table_store:
            continue  # already derived
        quarter_keys = [f"Q{n} FY{fy}" for n in (1, 2, 3)]
        if not all(k in company_store.table_store for k in quarter_keys):
            continue  # need all three quarters on hand to subtract

        cik = company_store.filing_meta.get(f"FY{fy}", {}).get("cik")
        if cik is None:
            continue  # this year's 10-K wasn't XBRL-sourced (e.g. pdfplumber fallback)

        facts = await edgar_xbrl.fetch_company_facts(cik)
        if facts is None:
            continue

        derived = edgar_xbrl.build_q4_synthetic_tables(facts, fy)
        if derived is None:
            continue
        tables, metrics = derived

        company_store.table_store[q4_key] = tables
        company_store.metrics_store[q4_key] = metrics
        fy_meta = company_store.filing_meta.get(f"FY{fy}", {})
        company_store.filing_meta[q4_key] = {
            **fy_meta,
            "period_key": q4_key,
            "period": f"Q4 FY{fy} (derived: FY annual minus 9-month YTD through Q3)",
            "data_source": "xbrl-derived-q4",
        }
        logger.info(f"[sec] [{company_store.ticker}] derived {q4_key} from FY minus 9mo-YTD")
