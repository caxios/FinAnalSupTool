# Refactoring Plan: Maintainability & Infrastructure (Part 2)

## Objective
Enhance the long-term maintainability, observability, and stability of the backend system by upgrading prompt management, configuration, error handling, and logging mechanisms.

## 1. Prompt Externalization (Prompt Management)
Currently, all AI instructions (e.g., `_SYSTEM_PROMPT`) are hardcoded as giant multiline strings inside the python agent files (`backend/agents/*.py`).
- Create a `backend/prompts/` directory.
- Extract all system prompts into individual `.md` or `.txt` files (e.g., `prompts/sec_agent.md`, `prompts/manager_agent.md`).
- Update the `BaseAgent` or individual agents to read these files from disk at runtime. This allows prompt engineers to tune AI behavior without touching python code and improves codebase readability.

## 2. Configuration Centralization
Currently, API keys and environment variables are loaded manually via `dotenv` directly in `main.py`, without strict type validation.
- Introduce `pydantic-settings` to strictly manage configurations.
- Create `backend/config.py` containing a `Settings` class inheriting from `BaseSettings`.
- Define required environment variables (e.g., `GEMINI_API_KEY`, `TAVILY_API_KEY`) and default system parameters (like HTTP timeouts). If a required key is missing, the app should fail fast during boot rather than crashing during a specific API call later.

## 3. Centralized Exception Handling
Currently, standard errors are handled by manually raising `HTTPException(status_code=400, ...)` scattered deeply across endpoints and utilities.
- Create `backend/exceptions.py` with custom application-level exceptions (e.g., `AgentFailureError`, `DataExtractionError`).
- Use FastAPI's `@app.exception_handler()` in `main.py` to catch these custom exceptions globally and format them into consistent JSON error responses for the frontend.
- This removes boilerplate error-handling from individual functions and guarantees the React client always receives a predictable error schema.

## 4. Structured Logging
Currently, standard python `logging.basicConfig()` is used, which dumps plain text to stdout. In a Multi-Agent System (MAS), it's difficult to trace parallel agent executions and hallucinations.
- Integrate a structured logging library like `loguru` or configure a JSON-formatter for standard logging.
- Set up logging to output to standard out (stdout) as well as persist to log files (e.g., `backend/logs/app.log`).
- Include structured context in logs (e.g., `agent_id`, `run_id`, `execution_time_ms`) to allow easy filtering and tracing of individual agent actions during the complex 3-phase analysis pipeline.
