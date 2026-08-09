"""
services.company_service
─────────────────────────
Derive the company (or companies) behind the uploaded filings by grouping the
stored filing metadata on CIK. Shared by the /company endpoint, the media
endpoints, and the analysis pipeline so they all agree on "who" the filings are.
"""

from __future__ import annotations

from schemas import CompanyInfo, CompanyResponse
from services.storage import DocumentStore


def derive_companies(store: DocumentStore) -> CompanyResponse:
    """Group uploaded filings by CIK to identify the company/companies."""
    groups: dict[int, dict] = {}
    for meta in store.filing_meta.values():
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


def primary_company(store: DocumentStore) -> CompanyInfo | None:
    """The company with the most uploaded filings, or None if none identified."""
    return derive_companies(store).primary
