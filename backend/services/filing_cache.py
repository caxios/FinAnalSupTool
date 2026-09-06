"""
services.filing_cache
──────────────────────
Disk cache for a CompanyStore's extracted filing data — text sections (MD&A,
Risk Factors, ...), per-period financial statement tables, raw XBRL ratio
metrics, and filing metadata.

Why this exists
────────────────
Everything in ``services.storage`` is in-memory and process-local: a server
restart clears it. That's fine for the tables/text themselves (they're
re-derivable by re-fetching), but it silently breaks anything that reads a
CompanyStore *without* re-fetching — most visibly, the AI Chat assistant
answering questions about an archived Deep Analysis run, which has no reason
to ask the user to re-upload filings it already has evidence for on disk (see
``rag/history_store.py``, which persists the ANALYSIS but not the raw filing
data that fed it).

This is a CACHE, not a second source of truth: it is written best-effort after
every successful ingestion batch and read back only to REPOPULATE an otherwise
EMPTY CompanyStore — never to overwrite live, already-populated in-memory data.
One JSON file per ticker, so a corrupt or missing file for one company can
never affect another.

Serialization note: ``table_store`` DataFrames round-trip through
``DataFrame.to_dict(orient="split")`` — lossless here because this app's
extracted tables are string-cell display tables (pdfplumber rows and XBRL's
own formatted values), not a numeric type worth preserving more carefully.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from services.storage import CompanyStore, DocumentStore

logger = logging.getLogger(__name__)

# Lives beside `backend/analysis_history/` — the app's other on-disk,
# per-ticker cache — so persistent data stays together under `backend/`.
_CACHE_DIR = Path(__file__).parent.parent / "filing_cache"


def _safe(ticker: str | None) -> str:
    t = "".join(c for c in (ticker or "").strip().upper() if c.isalnum() or c in "-._")
    return t or "UNKNOWN"


def _path(ticker: str) -> Path:
    return _CACHE_DIR / f"{_safe(ticker)}.json"


def _df_to_json(df: pd.DataFrame) -> dict:
    return df.to_dict(orient="split")


def _df_from_json(d: dict) -> pd.DataFrame:
    return pd.DataFrame(
        data=d.get("data", []), columns=d.get("columns"), index=d.get("index")
    )


def save_company_store(ticker: str, store: CompanyStore) -> None:
    """
    Persist this company's extracted text/tables/metadata to disk.

    Best-effort: a cache-write failure must not fail the ingestion batch that
    triggered it. Called once per ticker after ``rebuild_merged_tables()`` —
    see ``routers/document.py`` (POST /upload) and ``services/sec_ingest.py``
    (POST /sec/fetch and the portfolio baseline auto-fetch), the two places
    ingestion actually completes.
    """
    if not (store.text_store or store.table_store):
        return
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        table_store_json = {
            period: {
                stmt_type: [_df_to_json(df) for df in dfs]
                for stmt_type, dfs in stmts.items()
            }
            for period, stmts in store.table_store.items()
        }
        payload = {
            "ticker": ticker,
            "text_store": store.text_store,
            "filing_meta": store.filing_meta,
            "metrics_store": store.metrics_store,
            "table_store": table_store_json,
        }
        _path(ticker).write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        logger.info(
            f"[filing_cache] cached {ticker}: {len(store.filing_meta)} period(s)"
        )
    except Exception as e:  # noqa: BLE001 — caching must never fail ingestion
        logger.warning(f"[filing_cache] failed to cache {ticker}: {e}")


def has_cache(ticker: str) -> bool:
    return _path(ticker).exists()


def list_cached_tickers() -> list[str]:
    """
    Every ticker with a filing cache on disk, sorted.

    Lets a cold-started process (or one that never ingested a ticker THIS
    session) discover what data actually exists, so ``GET /companies`` can
    list a company whose filings were fetched before the last restart —
    without this, a company only ever appears once something in the current
    session happens to call :func:`rehydrate_company_store` for it first.
    """
    if not _CACHE_DIR.exists():
        return []
    return sorted(f.stem for f in _CACHE_DIR.glob("*.json"))


def rehydrate_company_store(ticker: str, store: DocumentStore) -> bool:
    """
    Restore ``ticker``'s cached filing text/tables into ``store``, IF that
    ticker's CompanyStore is currently empty — this NEVER overwrites live,
    already-populated in-memory data (a fresh upload this session always wins).

    Returns True when the store has usable filing data available afterward
    (it already did, or rehydration just supplied it); False when nothing is
    available from cache either — the caller falls back further from there.
    """
    company_store = store.get_company_store(ticker)
    if company_store.text_store or company_store.table_store:
        return True  # already populated this session — nothing to do

    path = _path(ticker)
    if not path.exists():
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        company_store.text_store = payload.get("text_store") or {}
        company_store.filing_meta = payload.get("filing_meta") or {}
        company_store.metrics_store = payload.get("metrics_store") or {}
        company_store.table_store = {
            period: {
                stmt_type: [_df_from_json(d) for d in dfs]
                for stmt_type, dfs in stmts.items()
            }
            for period, stmts in (payload.get("table_store") or {}).items()
        }
        company_store.rebuild_merged_tables()
        logger.info(
            f"[filing_cache] rehydrated {ticker} from disk cache "
            f"({len(company_store.filing_meta)} period(s))"
        )
        return True
    except Exception as e:  # noqa: BLE001 — rehydration is best-effort
        logger.warning(f"[filing_cache] rehydration failed for {ticker}: {e}")
        return False
