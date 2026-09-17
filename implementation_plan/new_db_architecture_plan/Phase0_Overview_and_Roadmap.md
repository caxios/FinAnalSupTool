# Hybrid Data Architecture — Overview & Roadmap

Migrate the backend from "API call → JSON file cache → prompt stuffing" to the
multi-tier hybrid architecture (deterministic structured data + probabilistic
hybrid search, cleanly separated) so users can **search the exact data already
fetched**, not just re-ask the same live APIs.

This is the index for a 7-part plan. Each phase is its own file in this
folder; implement them in order — later phases depend on earlier ones.

## Tech-stack decisions (already made, binding for all phases)

| Diagram component | Chosen implementation | Why |
|---|---|---|
| Data Lake (S3/GCS) | Local directory tree, `backend/data_lake/` | No cloud account needed; same path convention as the diagram |
| Structured/OLAP DB (Postgres) | **DuckDB**, `backend/financial_facts.duckdb` | Embedded, zero-ops, real SQL, columnar — no server to run |
| Search Engine (OpenSearch/Qdrant) | **SQLite FTS5** (BM25) + the **existing ChromaDB** (vector), fused via app-level **RRF** | No server; reuses the vector store already in this codebase |
| Cache & Registry (Redis) | SQLite, `backend/registry.db` | Same reasoning — no server for a single-user local app |
| Agent Orchestration (LangGraph) | **LangGraph** (as named) | Pure Python library, no server, adds real graph/state-machine structure |
| Cross-Encoder Reranker | **Cohere Rerank API** | Matches this codebase's existing pattern (Gemini/Tavily/YouTube are all API-key-in-.env, never local ML inference) |

New Python dependencies across all phases: `duckdb`, `beautifulsoup4` (or `lxml`), `langgraph`, `langchain-core`, `cohere`. None require a running service.

## Current-state audit (why this migration is needed)

Verified in this codebase as of this plan:

- **No structured financial DB.** `providers/edgar_xbrl.py` fetches XBRL facts live from SEC and caches them **in-memory only** (`_facts_cache: dict[int, dict]`, per CIK, cleared on restart). Extracted values live as strings inside `pandas.DataFrame`s in `CompanyStore.table_store`, then get serialized to `filing_cache/{TICKER}.json`. There is no queryable `financial_facts` table anywhere.
- **No footnote graph.** SEC filings are only ever rendered to **PDF** (`findata.download_filing_pdf`, headless Chromium) before text/table extraction. The original HTML's DOM structure and anchor links — which is what would let a balance-sheet line item point at its footnote — is thrown away. `statement_footnote_links` does not exist.
- **Vector search exists but is narrow.** `rag/vector_store.py` (ChromaDB) + `rag/chunking.py` + `rag/sec_rag.py` + `rag/earnings_rag.py` are real, working, and already speaker-aware for earnings calls. But: (a) it's **vector-only**, no BM25/keyword layer, no fusion; (b) indexing is scoped to `{ticker}:{run_id}` — **ephemeral per MAS analysis run**, re-embedded every run rather than a stable, reusable, ticker-wide index; (c) **news is never indexed at all** — articles are cached as flat JSON (`news_cache/`) and stuffed straight into prompts; (d) it only activates above a token threshold (`RAG_THRESHOLD_TOKENS`), by design, for the MAS pipeline's own context budget — it was never meant to be a general-purpose "search everything" index.
- **No dedup/registry layer.** `company_news_agent.py` dedups only by exact URL (`seen: set[str]`). No SimHash/MinHash, no entity-alias cache.
- **No orchestration layer.** Every endpoint (`routers/chat.py`, `services/research_copilot.py`, the MAS agents in `services/pipeline.py`) is a straight-line function: fetch → prompt → LLM call. There's no query decomposition, no tool routing, no reranking.
- **No strict fact/narrative separation.** Numbers and prose are interleaved in prompts; nothing structurally prevents the LLM from "recalculating" a number that should have come verbatim from a table.

## What is explicitly OUT of scope for this migration

- **The MAS pipeline's field agents** (`agents/*_agent.py`, driven by `services/pipeline.py`) keep working exactly as they do today, including their existing `rag/sec_rag.py` / `rag/earnings_rag.py` ephemeral RAG. They have a different job (fill one fixed rubric per run under a token budget) than the new "search my fetched data" surface. Migrating them to the new stack is a possible **future** phase, not this one.
- Portfolio/journal data (`services/db.py`, SQLite `portfolio.db`) is untouched — it's already a proper relational store for the right kind of data.
- The `implementation_plan/Unified_Data_Tab_Implementation_Plan` work (news/price/insider disk caches) is untouched at the API layer; this migration adds a structured+search layer **underneath** it, not a replacement for the fetch-control UI.

## Phase list

| Phase | File | Delivers |
|---|---|---|
| 1 | `Phase1_Storage_Foundations.md` | The empty "boxes": data lake convention, DuckDB schema, FTS5 schema, registry DB schema. No ingestion logic yet. |
| 2 | `Phase2_Ingestion_Pipelines.md` | Makes each source (SEC HTML+XBRL, earnings, news) actually fill Phase 1's stores: footnote-graph parsing, persistent chunk indexing, SimHash dedup + ticker tagging. |
| 3 | `Phase3_Hybrid_Retrieval.md` | `rag/hybrid_search.py` — BM25 (FTS5) + vector (Chroma) fused via Reciprocal Rank Fusion, one `search()` API with metadata filters. |
| 4 | `Phase4_Reranking.md` | Cohere Rerank wired in front of the final top-k. |
| 5 | `Phase5_Agent_Orchestration.md` | LangGraph planner + tool router: decomposes a question, dispatches to the SQL tool / footnote tool / hybrid-search tool. |
| 6 | `Phase6_Context_Assembly_and_Integration.md` | Strict "verified facts table" vs "cited excerpts" context builder; wires the whole graph into `POST /analysis/query-data` and (optionally) `POST /chat`. |

## Cross-phase conventions (read once, apply everywhere)

- **Stable chunk IDs.** Every chunk gets one `doc_id` used identically in Chroma, in the FTS5 `chunk_registry`, and in citations (`[Doc_ID: MRVL_2025Q3_QA_02]`). Format: `{TICKER}_{PERIOD_OR_QUARTER}_{DOC_TYPE}_{SEQ}`.
- **Stable index scope.** Chunks are indexed under `{ticker}:{period_key}` (SEC) / `{ticker}:{quarter}` (earnings) / `{ticker}:{article_id}` (news) — **not** `{ticker}:{run_id}`. Re-ingesting the same period is an idempotent upsert, not a duplicate.
- **Env vars added:** `COHERE_API_KEY` (Phase 4). No others — DuckDB/SQLite/LangGraph need no keys.
- **Verification standard**, per this project's established practice: every phase must be checked against real data (a real ticker already in `filing_cache/`/`transcript_cache/`/`news_cache/`) via a script or the running server — not just `py_compile`.
