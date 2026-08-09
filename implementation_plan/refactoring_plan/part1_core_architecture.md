# Refactoring Plan: Core Architecture (Part 1)

## Objective
Refactor the prototype monolithic backend into a scalable, production-ready architecture by separating concerns, modularizing the codebase, and removing global state dependencies.

## 1. Router Decomposition (Thin `main.py`)
Currently, `main.py` (1,800+ lines) contains all API endpoints. We will use FastAPI's `APIRouter` to split these by domain.
- Create a `backend/routers/` directory.
- **`routers/document.py`**: Move endpoints for `/upload`, `/financials`, `/filing-text`, and `/periods`.
- **`routers/analysis.py`**: Move endpoints for `/analyze`, `/analyze/stream`, and all `/analysis/*` history endpoints.
- **`routers/chat.py`**: Move the `/chat` endpoint.
- **`routers/media.py`**: Move endpoints for `/news`, `/youtube`, `/macro`, and `/channels`.
- **Update `main.py`**: Refactor to only initialize the `FastAPI()` app, configure CORS, setup lifecycle events, and include the routers via `app.include_router()`.

## 2. State Management Independence (No Global Variables)
Remove global in-memory state dictionaries (`_text_store`, `_table_store`, `_filing_meta`, `_debate_store`) from `main.py` header.
- Create `backend/services/storage.py` (or a `repository/` layer) to manage these states via classes.
- Use FastAPI's Dependency Injection (`Depends()`) to pass the storage instance into router endpoints, decoupling state lifespan from module load time and paving the way for a real Database (e.g., PostgreSQL/Redis) replacement in the future.

## 3. Service Layer Extraction (Business Logic Isolation)
Endpoints should not contain heavy orchestration logic.
- Move the complex multi-phase execution logic inside `_analyze_pipeline` and `run_analysis_stream` out of `main.py`.
- Create `backend/services/orchestrator.py` or `backend/services/pipeline.py`.
- Routers should simply parse the incoming request, call the service layer function, and return the output.

## 4. Directory Organization
Group related massive python scripts into dedicated folders to declutter the root `backend/` directory.
- Create `backend/providers/` and move external API connectors: `news_provider.py`, `youtube_provider.py`, `price_provider.py`, `edgar_xbrl.py`.
- Create `backend/parsers/` and move document processing utilities: `pdf_utils.py`.
- Split the monolithic `schemas.py` (~1,200 lines) into smaller files (e.g., `api_schemas.py` and `domain_schemas.py`) or place them in a `backend/schemas/` directory categorized by domain.
