"""
providers
─────────
External-service connectors used across the backend.

Each module wraps one outside data source behind a small, typed API so the
routers, services, and agents never talk to third-party SDKs directly:

  - ``news_provider``    — Tavily web/news + earnings-transcript search
  - ``youtube_provider`` — YouTube Data API search + transcript fetch
  - ``price_provider``   — yfinance price history + technical indicators, and
                           the 10-Year Treasury yield (^TNX) for macro context
  - ``edgar_xbrl``       — SEC EDGAR XBRL facts → statement/ratio tables

They are intentionally free of local (in-repo) imports so they can be reused
anywhere without pulling in the app's state or service layers.
"""
