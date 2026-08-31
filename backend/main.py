"""
main.py
───────
FastAPI application entry point for the Financial Analysis Support Tool.

This module is intentionally thin: it only
  1. loads the backend .env,
  2. constructs the FastAPI app and configures CORS,
  3. includes the domain routers, and
  4. wires up lifecycle (startup/shutdown) hooks.

All endpoints live in ``routers/`` (document, analysis, chat, media); the
business logic and state live in ``services/`` (storage, pipeline, …). See the
package docstrings for the layout.

Filing data is in-memory (see ``services.storage``) — restarting the server
clears all uploaded data. The trading portfolio and journal are the exception:
they live in SQLite (see ``services.db``) and persist across restarts.
This is a localhost prototype, not a production system.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

# Load backend/.env (if present) so API keys — GEMINI_API_KEY, TAVILY_API_KEY,
# YOUTUBE_API_KEY, etc. — can live in a file instead of shell exports. Optional:
# if python-dotenv isn't installed, we silently fall back to the real env.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import document, analysis, chat, media, sec, portfolio, coach
from services import ingestion
from services import db
from services.storage import get_document_store


# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# FastAPI App Initialization
# =============================================================================

app = FastAPI(
    title="Financial Analysis Support Tool",
    description=(
        "Parse historical SEC filings (10-K, 10-Q) from uploaded PDFs. "
        "Extract financial tables and qualitative text sections, then run a "
        "multi-agent analysis over the company's filings, media, and market data."
    ),
    version="0.1.0",
)

# Allow the React frontend dev servers to make cross-origin requests.
# Without this, browser security blocks fetch() calls from localhost:5173
# to localhost:8000 (different ports = different origins).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",   # Create React App default
        "http://localhost:5173",   # Vite default
        "http://localhost:5174",   # Vite fallback port
    ],
    allow_credentials=True,
    allow_methods=["*"],           # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],           # Allow all headers
)


# =============================================================================
# Routers
# =============================================================================

app.include_router(document.router)
app.include_router(analysis.router)
app.include_router(chat.router)
app.include_router(media.router)
app.include_router(sec.router)
app.include_router(portfolio.router)
app.include_router(coach.router)


# =============================================================================
# Startup
# =============================================================================

@app.on_event("startup")
async def startup():
    """
    Prepare the durable portfolio database.

    ``init_db`` is idempotent (every statement is CREATE ... IF NOT EXISTS), so
    this is safe on every boot and creates ``backend/portfolio.db`` on the first.
    """
    db.init_db()


# =============================================================================
# Cleanup on Shutdown
# =============================================================================

@app.on_event("shutdown")
async def cleanup():
    """
    Remove the temporary upload directories when the server shuts down.
    This prevents leftover PDF files from accumulating on disk.

    Each company has its own temp dir (see ``CompanyStore``), plus one shared
    ingestion staging dir — clean up all of them.

    The portfolio database is deliberately NOT touched here: unlike the temp
    dirs, ``backend/portfolio.db`` holds the user's holdings and journal and
    must survive the restart. We only close the connection.
    """
    dirs = [cs.upload_dir for cs in get_document_store().companies.values()]
    dirs.append(ingestion.STAGING_DIR)
    for d in dirs:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            logger.info(f"Cleaned up temp dir: {d}")

    db.close_db()
