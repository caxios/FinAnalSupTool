# AI Trading & Portfolio Coach — Phased Implementation Plan

Split of `implementation_plan/trading_coach_blueprint_v2.md` into executable phases.
The blueprint states *what* the system does; these phases state *how*, at the level
of files and function signatures (same style as `mas_analy_sys_plan/`).

## Phase order & dependencies

| Phase | File | Scope | Depends on |
|---|---|---|---|
| 1 | `phase_1_persistence.md` | SQLite layer + schema. First durable store in the app. | — |
| 2 | `phase_2_portfolio_journal.md` | Holdings & trade CRUD, `entry_rationale`, backend API | 1 |
| 3 | `phase_3_price_automation.md` | yfinance intraday → execution price, avg price, ROI | 2 |
| 4 | `phase_4_journal_ui.md` | New "Portfolio" view: holdings table + trade form | 2, 3 |
| 5 | `phase_5_quant_risk_agent.md` | VaR/CVaR, vol, MDD, correlation, marginal risk contribution | 3 |
| 6 | `phase_6_coach_agent.md` | Bias detection over journal + fundamental + technical | 2, 5 |

Blueprint §4 (8-quarter baseline auto-fetch) is **not** its own phase — it is folded
into phase 2 as a side effect of adding a ticker, because it is mostly wiring the
existing `POST /sec/fetch` path to a computed year/quarter range.

## Architectural context (verified in the current codebase)

* **There is no database.** `requirements.txt` has no DB driver; `services/storage.py`
  is module-level dicts (`_document_store`, `_media_cache`, `_debate_store`) and
  `CompanyStore.upload_dir` is a `tempfile.mkdtemp()` that `main.py`'s shutdown hook
  deletes. Nothing in the MAS survives a restart.
* **One persistence precedent exists**: `rag/history_store.py` writes each completed
  analysis run to `backend/analysis_history/{TICKER}_{run_id}.json` and globs by
  ticker. That idiom suits append-only records; it does *not* suit a journal whose
  average price is recomputed on every trade. Hence SQLite in phase 1.
* **Per-company isolation is already done** (`mas_analy_sys_plan/` phases 1-5).
  `DocumentStore.get_company_store(ticker)`, `has_company`, `list_tickers`, and the
  frontend's `activeTicker` context all exist and should be reused, not rebuilt.
* **The technical pillar already exists**: `agents/technical_analysis_agent.py` +
  `providers/price_provider.py` (RSI, MACD, SMA20/50/200, swing levels, monthly
  closes). `yfinance` and `numpy` are already dependencies, so phases 3 and 5 add
  no heavy new packages.

## Open decision

Phase 1 assumes **SQLite via the stdlib `sqlite3` module** (no ORM). If you would
rather use SQLAlchemy, Postgres, or JSON-on-disk, that changes phase 1 only —
phases 2-6 talk to a repository module, not to the driver.
