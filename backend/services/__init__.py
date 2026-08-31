"""
services
────────
The business-logic layer that sits between the thin HTTP routers and the
providers/parsers/agents. Routers parse a request, call a service, and return
the result; services own state and orchestration.

  - ``storage``         — in-memory state containers + DI providers
  - ``db``              — durable SQLite store (portfolio + trading journal)
  - ``company_service`` — derive the company/companies from uploaded filings
  - ``media_service``   — assemble the cross-view media context for the assistant
  - ``pipeline``        — the three-phase multi-agent analysis orchestration
  - ``sec_ingest``      — shared EDGAR fetch → ingest loop for a fiscal range
  - ``portfolio_service`` — portfolio/journal repository + baseline auto-fetch
  - ``risk_metrics``    — pure portfolio risk math (VaR, CVaR, attribution)
  - ``journal_analysis``  — trade outcomes + behavioural patterns for the coach
"""
