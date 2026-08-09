"""
services
────────
The business-logic layer that sits between the thin HTTP routers and the
providers/parsers/agents. Routers parse a request, call a service, and return
the result; services own state and orchestration.

  - ``storage``         — in-memory state containers + DI providers
  - ``company_service`` — derive the company/companies from uploaded filings
  - ``media_service``   — assemble the cross-view media context for the assistant
  - ``pipeline``        — the three-phase multi-agent analysis orchestration
"""
