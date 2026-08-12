"""
routers
───────
FastAPI ``APIRouter`` modules, one per domain. ``main.py`` includes them all.
Endpoints stay thin: parse the request, call a service, return the response.

  - ``document``  — /upload, /financials, /filing-text, /periods, /filing-pdf
  - ``analysis``  — /analyze, /analyze/stream, /analysis/*
  - ``chat``      — /chat
  - ``media``     — /company, /media/*, /macro/*, /channels
  - ``sec``       — /sec/fetch (automated SEC EDGAR retrieval → ingestion)
"""
