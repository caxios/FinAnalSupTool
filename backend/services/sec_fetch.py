"""
services.sec_fetch
───────────────────
A self-contained wrapper around the ``findata`` package that resolves a
*human-facing* filing request — ticker, form type, fiscal-year range — to the
matching filings pulled straight from SEC EDGAR, each rendered to PDF.

The two public entry points split the work so the caller can report per-period
success/failure:
  • :func:`plan_filings` — one EDGAR metadata query → the list of filings that
    fall in ``[start_year, end_year]`` (one 10-K per year; all 10-Q quarters).
  • :func:`render_planned` — render a single planned filing to PDF bytes.

Design goals (see the feature spec):
  • Decoupled: this module knows about ``findata`` and about SEC filing
    conventions, and nothing about FastAPI, the DocumentStore, or the extraction
    pipeline. It hands back raw PDF bytes + provenance. It can be reused or
    swapped independently.
  • Honest: ``findata``'s filing rows carry only a *filing date*, not the
    period-of-report. So mapping a request to specific filings is a documented
    heuristic driven by the period-end date embedded in the document URL (see
    :func:`_fiscal_of`). Each planned filing's real filing date + fiscal label
    are returned so the caller can surface exactly what was fetched — the
    downstream PDF parser re-derives the authoritative period from the cover
    page regardless.
  • Loud on failure: every ``findata`` failure mode maps to a typed exception
    the API layer can turn into a structured JSON error.

Blocking note: ``find_filings`` does synchronous HTTP and ``download_filing_pdf``
spawns a headless-Chromium subprocess. Both block. Call ``plan_filings`` and
``render_planned`` from a threadpool (e.g. Starlette's ``run_in_threadpool``) so
the event loop stays free.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

import findata
from findata.sec.pdf import SECAccessBlocked

logger = logging.getLogger(__name__)

# Form types this feature supports. SEC amendments ("10-K/A") are intentionally
# excluded — findata matches form types by exact string, so a request for
# "10-K" never picks up a "10-K/A".
SUPPORTED_FORMS = ("10-K", "10-Q")

# Widest fiscal-year span (inclusive) a single request may cover, to bound SEC
# traffic and render time. Kept in sync with ``schemas.api_schemas.MAX_YEAR_SPAN``
# (the API layer rejects over-wide ranges early; this is the defensive backstop).
MAX_YEAR_SPAN = 5


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
class PlannedFiling:
    """One filing selected for a range request, *before* it is rendered.

    Produced by :func:`plan_filings` from a single EDGAR metadata query, so the
    caller can render/ingest each period independently (and report per-period
    success/failure) without re-hitting the submissions endpoint.
    """

    ticker: str
    form_type: str
    fiscal_year: int
    quarter: int | None       # None for a 10-K
    period_label: str         # "FY2022" or "FY2022 Q1"
    filename: str             # suggested on-disk name, e.g. "AAPL_10-K_2022.pdf"
    filing_date: str
    accession_number: str
    document_url: str


@dataclass
class FetchedFiling:
    """A rendered filing plus the provenance of what was actually retrieved."""

    pdf_bytes: bytes
    filename: str            # suggested on-disk name, e.g. "AAPL_10-K_2024.pdf"
    ticker: str
    form_type: str
    period_label: str        # "FY2022" or "FY2022 Q1"
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

    Fiscal year is labelled by the calendar year in which it ends; quarters are
    counted back from the fiscal-year end (Q4 is the year-end / 10-K period).

    Robust to 52/53-week fiscal calendars, where a quarter-end drifts by up to
    ~2 weeks and can land in an adjacent month (e.g. Apple's Q2 ends 2024-03-30
    one year but 2023-04-01 another). Rather than bucket strictly by month, we
    round the months-from-year-end to the nearest quarter, and allow the
    period-end to sit slightly past the nominal year-end when assigning the year.
    Verified for December-, June-, and September-end calendars.
    """
    # Fiscal year: which fiscal-year-end (month == fye_month) this period rolls
    # into. A 20-day slack absorbs week-based drift past the nominal year-end.
    y = period_end.year
    nominal_fye = date(y, fye_month, calendar.monthrange(y, fye_month)[1])
    fy = y if period_end <= nominal_fye + timedelta(days=20) else y + 1

    # Quarter: months before the fiscal-year end, rounded to the nearest quarter.
    # 0 → the year-end quarter (Q4 / the 10-K period).
    distance = (fye_month - period_end.month) % 12
    q_from_end = round(distance / 3) % 4
    quarter = 4 if q_from_end == 0 else 4 - q_from_end
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


def _period_label(fiscal_year: int, quarter: int | None) -> str:
    """Human-readable fiscal-period label, e.g. 'FY2022' or 'FY2022 Q1'."""
    return f"FY{fiscal_year}" + (f" Q{quarter}" if quarter else "")


def _safe_filename(ticker: str, form_type: str, year: int, quarter: int | None) -> str:
    """Build a filesystem-safe suggested name for the rendered PDF."""
    stem = f"{ticker}_{form_type}_{year}"
    if quarter:
        stem += f"_Q{quarter}"
    return re.sub(r"[^\w.\-]", "_", stem) + ".pdf"


def _classify_period_end(row: dict, fye_month: int) -> tuple[int, int]:
    """Classify a filing row into (fiscal_year, fiscal_quarter).

    Uses the authoritative period-end date from the document URL when present,
    else estimates it as ~45 days before the filing date so a row is never
    silently dropped.
    """
    pe = _period_end_from_url(row["document_url"])
    if pe is None:
        fd = date.fromisoformat(row["filing_date"])
        pe = date.fromordinal(fd.toordinal() - 45)
    return _fiscal_of(pe, fye_month)


# =============================================================================
# Public entry points
# =============================================================================

def plan_filings(
    ticker: str,
    form_type: str,
    start_year: int,
    end_year: int,
) -> list[PlannedFiling]:
    """Resolve which filings to fetch for a fiscal-year *range* — no rendering.

    A single EDGAR metadata query is classified into fiscal periods, then
    filtered to ``[start_year, end_year]``. For a 10-K this yields one annual
    report per year; for a 10-Q it yields every available quarter (Q1–Q3) in the
    range. The result is sorted oldest → newest so the caller can render/ingest
    each period in chronological order.

    Args:
        ticker:     Stock symbol (case-insensitive).
        form_type:  ``"10-K"`` or ``"10-Q"``.
        start_year: First fiscal year, inclusive.
        end_year:   Last fiscal year, inclusive.

    Raises:
        InvalidRequest, TickerNotFound, FilingNotFound, SecFetchError.

    Blocks on network I/O; call from a threadpool.
    """
    # ── Validate ─────────────────────────────────────────────────────────
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise InvalidRequest("A ticker symbol is required.")

    form_type = (form_type or "").strip().upper()
    if form_type not in SUPPORTED_FORMS:
        raise InvalidRequest(
            f"Unsupported form type {form_type!r}. "
            f"Supported: {', '.join(SUPPORTED_FORMS)}."
        )

    if start_year > end_year:
        raise InvalidRequest(
            f"start_year ({start_year}) must not be after end_year ({end_year})."
        )
    span = end_year - start_year + 1
    if span > MAX_YEAR_SPAN:
        raise InvalidRequest(
            f"Requested range spans {span} years; the maximum is {MAX_YEAR_SPAN}."
        )
    current_year = date.today().year
    if not (1994 <= start_year <= current_year + 1):
        raise InvalidRequest(
            f"start_year {start_year} is out of range "
            f"(SEC EDGAR covers 1994–{current_year})."
        )

    # ── Query EDGAR once for the whole range ─────────────────────────────
    # Window generously: reports for FY N are filed from N through N+1 (later
    # when amended). Windowing makes findata walk EDGAR's archive shards (needed
    # for older years). We always pull 10-K rows too — even for a 10-Q request —
    # because the fiscal-year-end month (learned from the 10-Ks) is what makes
    # quarter mapping correct for non-December fiscal years.
    date_from = f"{start_year - 2}-01-01"
    date_to = f"{min(end_year + 2, current_year + 1)}-12-31"
    logger.info(
        f"[sec_fetch] planning {ticker} {form_type} FY{start_year}..{end_year} "
        f"(filing-date window {date_from}..{date_to})"
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
        raise TickerNotFound(str(e)) from e
    except Exception as e:  # noqa: BLE001 — network/parse failure talking to SEC
        raise SecFetchError(f"Failed to query SEC EDGAR: {e}") from e

    annual_rows = [r for r in all_rows if r.get("form_type") == "10-K"]
    fye_month = _derive_fye_month(annual_rows)
    logger.info(f"[sec_fetch] inferred fiscal-year-end month: {fye_month}")

    rows = [
        r for r in all_rows
        if r.get("form_type") == form_type
        and r.get("filing_date") and r.get("document_url")
    ]

    # Keep the most recently filed document for each (fiscal_year, quarter) so an
    # amended/refiled period doesn't produce duplicates.
    best: dict[tuple[int, int], dict] = {}
    for r in rows:
        fy, q = _classify_period_end(r, fye_month)
        if not (start_year <= fy <= end_year):
            continue
        if form_type == "10-Q" and q not in (1, 2, 3):
            continue  # Q4 is reported in the 10-K, never a 10-Q
        # For a 10-K the quarter is irrelevant (one annual report per year); use
        # 0 so drift can't split one year's 10-K across two keys.
        key = (fy, 0 if form_type == "10-K" else q)
        if key not in best or r["filing_date"] > best[key]["filing_date"]:
            best[key] = r

    if not best:
        available = sorted(
            {
                _period_label(fy, None if form_type == "10-K" else q)
                for r in rows
                for (fy, q) in [_classify_period_end(r, fye_month)]
            },
            reverse=True,
        )[:10]
        hint = f" Available {form_type}s: {', '.join(available)}." if available else ""
        raise FilingNotFound(
            f"No {form_type} filings found for {ticker} in "
            f"FY{start_year}–FY{end_year}.{hint}"
        )

    planned: list[PlannedFiling] = []
    for (fy, q), r in best.items():
        quarter = None if form_type == "10-K" else q
        planned.append(
            PlannedFiling(
                ticker=ticker,
                form_type=form_type,
                fiscal_year=fy,
                quarter=quarter,
                period_label=_period_label(fy, quarter),
                filename=_safe_filename(ticker, form_type, fy, quarter),
                filing_date=r["filing_date"],
                accession_number=r.get("accession_number", ""),
                document_url=r["document_url"],
            )
        )

    planned.sort(key=lambda p: (p.fiscal_year, p.quarter or 0))
    logger.info(
        f"[sec_fetch] planned {len(planned)} filing(s): "
        f"{', '.join(p.period_label for p in planned)}"
    )
    return planned


def render_planned(planned: PlannedFiling) -> FetchedFiling:
    """Render a single :class:`PlannedFiling` to PDF via headless Chromium.

    Raises:
        SecRateLimited: SEC served its automated-tool interstitial.
        RenderFailed:   Playwright/Chromium missing, crashed, or empty output.

    Blocks on a subprocess render; call from a threadpool.
    """
    try:
        pdf_bytes = findata.download_filing_pdf(planned.document_url)
    except SECAccessBlocked as e:
        raise SecRateLimited(str(e)) from e
    except RuntimeError as e:
        raise RenderFailed(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise RenderFailed(f"Unexpected error rendering the filing PDF: {e}") from e

    if not pdf_bytes:
        raise RenderFailed("The rendered filing PDF was empty.")

    return FetchedFiling(
        pdf_bytes=pdf_bytes,
        filename=planned.filename,
        ticker=planned.ticker,
        form_type=planned.form_type,
        period_label=planned.period_label,
        filing_date=planned.filing_date,
        accession_number=planned.accession_number,
        document_url=planned.document_url,
    )
