"""
services.company_service
─────────────────────────
Derive the company (or companies) behind ingested filings by grouping the stored
filing metadata on CIK. Shared by the /company endpoint, the media endpoints, and
the analysis pipeline so they all agree on "who" the filings are.

Since filings are stored per company (see :class:`CompanyStore`), the per-company
helpers take a ``CompanyStore``; :func:`list_companies` walks the whole registry
to answer "which companies do we have data for?".
"""

from __future__ import annotations

from schemas import CompanyInfo, CompanyResponse
from services.storage import CompanyStore, DocumentStore


def derive_companies(company_store: CompanyStore) -> CompanyResponse:
    """
    Group ONE company store's filings by CIK to identify the company.

    Normally yields a single company (that's the point of the per-ticker
    isolation), but the grouping is kept so a store holding filings that resolve
    to several CIKs still reports them all.
    """
    groups: dict[int, dict] = {}
    for meta in company_store.filing_meta.values():
        cik = meta.get("cik")
        if cik is None:
            continue
        g = groups.setdefault(cik, {"name": None, "ticker": None, "count": 0})
        g["count"] += 1
        if meta.get("entity_name"):
            g["name"] = meta["entity_name"]
        if meta.get("ticker"):
            g["ticker"] = meta["ticker"]

    companies = [
        CompanyInfo(cik=cik, name=g["name"], ticker=g["ticker"], filing_count=g["count"])
        for cik, g in groups.items()
    ]
    companies.sort(key=lambda c: c.filing_count, reverse=True)
    return CompanyResponse(
        primary=companies[0] if companies else None,
        companies=companies,
    )


def primary_company(company_store: CompanyStore) -> CompanyInfo | None:
    """The company with the most filings in this store, or None if unidentified."""
    return derive_companies(company_store).primary


def list_companies(store: DocumentStore) -> CompanyResponse:
    """
    Every company with ingested filings, across all per-ticker stores.

    Backs GET /companies, which the frontend uses to populate its company
    switcher. A store whose filings carry no CIK (an unidentified upload) still
    appears, keyed by the ticker its store is registered under, so the user can
    always reach their data.
    """
    companies: list[CompanyInfo] = []
    for tk in store.list_tickers():
        cs = store.get_company_store(tk)
        derived = derive_companies(cs)
        if derived.companies:
            companies.extend(derived.companies)
        elif cs.filing_meta:
            # Filings present but no CIK resolved — surface it anyway.
            companies.append(
                CompanyInfo(cik=None, name=None, ticker=tk,
                            filing_count=len(cs.filing_meta))
            )
    companies.sort(key=lambda c: c.filing_count, reverse=True)
    return CompanyResponse(
        primary=companies[0] if companies else None,
        companies=companies,
    )
