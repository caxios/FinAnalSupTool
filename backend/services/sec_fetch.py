"""
services.sec_fetch
───────────────────
A self-contained wrapper around the ``findata`` package that resolves a
*human-facing* filing request — ticker, form type, fiscal year, (quarter) — to
a rendered PDF pulled straight from SEC EDGAR.

Design goals (see the feature spec):
  • Decoupled: this module knows about ``findata`` and about SEC filing
    conventions, and nothing about FastAPI, the DocumentStore, or the extraction
    pipeline. It hands back raw PDF bytes + provenance. It can be reused or
    swapped independently.
  • Honest: ``findata``'s filing rows carry only a *filing date*, not the
    period-of-report. So mapping "fiscal year 2024, Q2" to a specific filing is
    a documented heuristic (see ``_select_filing``). The resolved filing's real
    filing date and form are returned so the caller can surface exactly what was
    fetched — the downstream PDF parser re-derives the authoritative period from
    the cover page regardless.
  • Loud on failure: every ``findata`` failure mode maps to a typed exception
    the API layer can turn into a structured JSON error.

Blocking note: ``find_filings`` does synchronous HTTP and ``download_filing_pdf``
spawns a headless-Chromium subprocess. Both block. Call ``fetch_filing_pdf``
from a threadpool (e.g. Starlette's ``run_in_threadpool``) so the event loop
stays free.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import findata
from findata.sec.pdf import SECAccessBlocked

logger = logging.getLogger(__name__)

# Form types this feature supports. SEC amendments ("10-K/A") are intentionally
# excluded — findata matches form types by exact string, so a request for
# "10-K" never picks up a "10-K/A".
SUPPORTED_FORMS = ("10-K", "10-Q")


# =============================================================================
# Typed errors — the API layer maps these to HTTP status codes
# =============================================================================

class SecFetchError(Exception):
    """Base class for all SEC auto-fetch failures."""


class InvalidRequest(SecFetchError):
    """The request itself is malformed (bad form type, quarter, year)."""


class TickerNotFound(SecFetchError):
    """The ticker could not be resolved to a SEC CIK."""


class FilingNotFound(SecFetchError):
    """No filing matched the requested ticker/form/period."""


class SecRateLimited(SecFetchError):
    """SEC served its automated-tool interstitial instead of the filing."""


class RenderFailed(SecFetchError):
    """PDF rendering failed (e.g. Playwright/Chromium missing or crashed)."""


# =============================================================================
# Result payload
# =============================================================================

@dataclass
class FetchedFiling:
    """A rendered filing plus the provenance of what was actually retrieved."""

    pdf_bytes: bytes
    filename: str            # suggested on-disk name, e.g. "AAPL_10-K_2024.pdf"
    ticker: str
    form_type: str
    filing_date: str         # the SEC filing date of the doc we rendered
    accession_number: str
    document_url: str


# =============================================================================
# Fiscal-period → filing resolution
# =============================================================================
#
# ``findata`` rows carry a filing date but not the period-of-report, so the
# naive "map a filing date to a fiscal year" approach is wrong for any company
# whose fiscal year doesn't end in December (e.g. Apple ends in September). The
# reliable signal is the **period-end date embedded in EDGAR primary-document
# URLs** — ``.../aapl-20240928.htm`` reports on the period ending 2024-09-28.
# Combined with the company's fiscal-year-end month (learned from its 10-Ks),
# that pins down the exact fiscal (year, quarter) for every filing.

# Matches an 8-digit YYYYMMDD in a primary-document filename, e.g.
# "aapl-20240928.htm" or "goog-20231231.htm".
_PERIOD_END_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")


def _period_end_from_url(url: str) -> date | None:
    """Extract the period-of-report date from a SEC primary-document URL.

    Returns ``None`` when the filename doesn't follow the modern
    ``ticker-YYYYMMDD`` convention (older filings use accession-style names),
    so callers can fall back to a filing-date estimate.
    """
    filename = url.rsplit("/", 1)[-1]
    for y, m, d in _PERIOD_END_RE.findall(filename):
        try:
            candidate = date(int(y), int(m), int(d))
        except ValueError:
            continue
        # Sanity bound — SEC EDGAR starts in 1994; reject stray digit runs.
        if 1994 <= candidate.year <= date.today().year + 1:
            return candidate
    return None


def _fiscal_of(period_end: date, fye_month: int) -> tuple[int, int]:
    """Map a period-end date to (fiscal_year, fiscal_quarter) for a company
    whose fiscal year ends in ``fye_month``.

    Fiscal year is labelled by the calendar year in which it ends. Quarters are
    counted from the fiscal-year start: e.g. for a September year-end (Apple),
    the quarter ending in December is Q1 and the one ending in September is Q4
    (the 10-K). Verified for December-, June-, and September-end calendars.
    """
    fy = period_end.year if period_end.month <= fye_month else period_end.year + 1
    quarter = ((period_end.month - fye_month - 1) % 12) // 3 + 1
    return fy, quarter


def _derive_fye_month(annual_rows: list[dict], default: int = 12) -> int:
    """Infer the fiscal-year-end month from the company's 10-K period-ends.

    Uses the most common period-end month across the available 10-Ks (robust to
    the occasional off-by-a-week 52/53-week filing). Falls back to December when
    no 10-K period-end can be parsed.
    """
    months: dict[int, int] = {}
    for r in annual_rows:
        pe = _period_end_from_url(r.get("document_url", ""))
        if pe:
            months[pe.month] = months.get(pe.month, 0) + 1
    if not months:
        return default
    return max(months, key=lambda m: months[m])


def _select_filing(
    rows: list[dict], form_type: str, year: int, quarter: int | None, fye_month: int
) -> dict:
    """Pick the single filing row matching the requested fiscal period.

    Each row is classified by the period-end date parsed from its document URL
    (authoritative); rows without a parseable period-end fall back to a
    filing-date estimate so they're never silently dropped.

    Raises :class:`FilingNotFound` if nothing suitable is present.
    """
    dated = [r for r in rows if r.get("filing_date") and r.get("document_url")]
    if not dated:
        raise FilingNotFound(
            f"No {form_type} filings with resolvable documents were found."
        )

    target_q = 4 if form_type == "10-K" else quarter

    def classify(r: dict) -> tuple[int, int]:
        pe = _period_end_from_url(r["document_url"])
        if pe is None:
            # Fallback: estimate the period end as ~45 days before filing.
            fd = date.fromisoformat(r["filing_date"])
            pe = date.fromordinal(fd.toordinal() - 45)
        return _fiscal_of(pe, fye_month)

    matches = [r for r in dated if classify(r) == (year, target_q)]

    if not matches:
        # Build a helpful "what IS available" hint from the same classifier.
        available = sorted(
            {f"FY{fy} Q{q}" for r in dated for (fy, q) in [classify(r)]},
            reverse=True,
        )[:8]
        want = f"fiscal year {year}" + (f" Q{quarter}" if quarter else "")
        hint = (
            f" Available: {', '.join(available)}." if available else ""
        )
        q4_note = (
            " Note: Q4 results are reported in the annual 10-K, not a 10-Q."
            if form_type == "10-Q" and quarter == 4 else ""
        )
        raise FilingNotFound(
            f"No {form_type} found for {want}.{hint}{q4_note}"
        )

    # If more than one matches (rare — e.g. an amended/refiled period), take the
    # most recently filed.
    return max(matches, key=lambda r: r["filing_date"])


def _safe_filename(ticker: str, form_type: str, year: int, quarter: int | None) -> str:
    """Build a filesystem-safe suggested name for the rendered PDF."""
    stem = f"{ticker}_{form_type}_{year}"
    if quarter:
        stem += f"_Q{quarter}"
    return re.sub(r"[^\w.\-]", "_", stem) + ".pdf"


# =============================================================================
# Public entry point
# =============================================================================

def fetch_filing_pdf(
    ticker: str,
    form_type: str,
    year: int,
    quarter: int | None = None,
) -> FetchedFiling:
    """Resolve ticker + form + fiscal period to a rendered SEC filing PDF.

    Args:
        ticker:    Stock symbol (case-insensitive), e.g. ``"AAPL"``.
        form_type: ``"10-K"`` or ``"10-Q"``.
        year:      Fiscal year, e.g. ``2024``.
        quarter:   Fiscal quarter 1–3, required for ``"10-Q"``, ignored for
                   ``"10-K"``.

    Returns:
        A :class:`FetchedFiling` with the PDF bytes and provenance.

    Raises:
        InvalidRequest, TickerNotFound, FilingNotFound, SecRateLimited,
        RenderFailed — all subclasses of :class:`SecFetchError`.

    Blocks on network + a subprocess render; call from a threadpool.
    """
    # ── Validate the request ─────────────────────────────────────────────
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise InvalidRequest("A ticker symbol is required.")

    form_type = (form_type or "").strip().upper()
    if form_type not in SUPPORTED_FORMS:
        raise InvalidRequest(
            f"Unsupported form type {form_type!r}. "
            f"Supported: {', '.join(SUPPORTED_FORMS)}."
        )

    current_year = date.today().year
    if not (1994 <= year <= current_year + 1):
        raise InvalidRequest(
            f"Year {year} is out of range (SEC EDGAR covers 1994–{current_year})."
        )

    if form_type == "10-Q":
        if quarter is None:
            raise InvalidRequest("A quarter (1–3) is required for a 10-Q.")
        if quarter not in (1, 2, 3, 4):
            raise InvalidRequest(f"Quarter must be 1–4 (got {quarter}).")
    else:
        quarter = None  # a 10-K has no quarter

    # ── Resolve candidate filings via findata ────────────────────────────
    # A generous filing-date window around the fiscal year: reports for FY N are
    # filed from N through N+1 (and can lag when amended). Windowing makes
    # findata walk EDGAR's archive shards (needed for older years) and pull a
    # wide slice before filtering. We always pull 10-K rows too — even for a
    # 10-Q request — because the company's fiscal-year-end month (learned from
    # its 10-Ks) is what makes quarter mapping correct for non-December FYEs.
    date_from = f"{year - 2}-01-01"
    date_to = f"{min(year + 2, current_year + 1)}-12-31"
    logger.info(
        f"[sec_fetch] resolving {ticker} {form_type} FY{year}"
        + (f" Q{quarter}" if quarter else "")
        + f" (filing-date window {date_from}..{date_to})"
    )
    try:
        all_rows = findata.find_filings(
            ticker,
            form_type="10-K,10-Q",
            date_from=date_from,
            date_to=date_to,
            include_archive=True,
        )
    except ValueError as e:
        # find_filings raises ValueError("Unknown ticker: …") on a bad symbol.
        raise TickerNotFound(str(e)) from e
    except Exception as e:  # noqa: BLE001 — network/parse failure talking to SEC
        raise SecFetchError(f"Failed to query SEC EDGAR: {e}") from e

    annual_rows = [r for r in all_rows if r.get("form_type") == "10-K"]
    fye_month = _derive_fye_month(annual_rows)
    logger.info(f"[sec_fetch] inferred fiscal-year-end month: {fye_month}")

    rows = [r for r in all_rows if r.get("form_type") == form_type]
    chosen = _select_filing(rows, form_type, year, quarter, fye_month)
    logger.info(
        f"[sec_fetch] selected {form_type} filed {chosen['filing_date']} "
        f"(accession {chosen.get('accession_number')})"
    )

    # ── Render the primary document to PDF (headless Chromium subprocess) ──
    try:
        pdf_bytes = findata.download_filing_pdf(chosen["document_url"])
    except SECAccessBlocked as e:
        raise SecRateLimited(str(e)) from e
    except RuntimeError as e:
        # Playwright not installed, or the render subprocess failed.
        raise RenderFailed(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise RenderFailed(f"Unexpected error rendering the filing PDF: {e}") from e

    if not pdf_bytes:
        raise RenderFailed("The rendered filing PDF was empty.")

    return FetchedFiling(
        pdf_bytes=pdf_bytes,
        filename=_safe_filename(ticker, form_type, year, quarter),
        ticker=ticker,
        form_type=form_type,
        filing_date=chosen["filing_date"],
        accession_number=chosen.get("accession_number", ""),
        document_url=chosen["document_url"],
    )
