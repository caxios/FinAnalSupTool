"""
rag/history_store.py
────────────────────
Persistent analysis history. Every completed MAS run is written to disk as JSON
(the authoritative store, survives restarts) and, best-effort, mirrored into the
`analysis_history` vector collection for later recall.

Disk layout: `backend/analysis_history/{TICKER}_{run_id}.json`. Reads glob by
ticker so histories are naturally isolated per company.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import vector_store

logger = logging.getLogger(__name__)

_HISTORY_DIR = Path(__file__).parent.parent / "analysis_history"


def _safe(ticker: str | None) -> str:
    """Filesystem-safe ticker token for filenames/globs."""
    t = re.sub(r"[^A-Za-z0-9._-]", "", (ticker or "UNKNOWN").upper())
    return t or "UNKNOWN"


def _summary(record: dict) -> dict:
    """The lightweight shape returned to the history sidebar (no full reports)."""
    tri = record.get("three_axis_scores") or {}
    mgr = record.get("manager") or {}
    return {
        "run_id": record.get("run_id"),
        "company": record.get("company"),
        "ticker": record.get("ticker"),
        "analysis_period": record.get("analysis_period"),
        "timestamp": record.get("timestamp"),
        "fundamental_score": tri.get("fundamental_score"),
        "sentiment_score": tri.get("sentiment_score"),
        "technical_score": tri.get("technical_score"),
        "fundamental_sentiment_gap": tri.get("fundamental_sentiment_gap"),
        "overall_signal": tri.get("overall_signal"),
        "signal_label": tri.get("signal_label"),
        "recommendation": mgr.get("recommendation") if isinstance(mgr, dict) else None,
        "overall_score": mgr.get("overall_score") if isinstance(mgr, dict) else None,
    }


def save_analysis(
    *,
    company: str | None,
    ticker: str | None,
    analysis_period: str,
    three_axis_scores: dict | None = None,
    manager: dict | None = None,
    reports: dict | None = None,
    debate: dict | None = None,
) -> str:
    """
    Persist a completed analysis. Returns the run_id. Never raises — a storage
    failure must not fail the analysis that just succeeded.
    """
    try:
        _HISTORY_DIR.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        run_id = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"

        record = {
            "run_id": run_id,
            "company": company,
            "ticker": ticker,
            "analysis_period": analysis_period,
            "timestamp": now.isoformat(),
            "three_axis_scores": three_axis_scores or {},
            "manager": manager,
            "reports": reports or {},
            "debate": debate,
        }
        path = _HISTORY_DIR / f"{_safe(ticker)}_{run_id}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")

        # Best-effort mirror into the vector store for semantic recall/metadata.
        tri = three_axis_scores or {}
        exec_summary = ""
        if isinstance(manager, dict):
            exec_summary = manager.get("executive_summary") or ""
        vector_store.upsert_record(
            "analysis_history",
            doc_id=run_id,
            text=exec_summary,
            metadata={
                "ticker": _safe(ticker),
                "company": company or "",
                "run_id": run_id,
                "analysis_period": analysis_period,
                "fundamental_score": tri.get("fundamental_score"),
                "sentiment_score": tri.get("sentiment_score"),
                "technical_score": tri.get("technical_score"),
                "overall_signal": tri.get("overall_signal"),
            },
        )
        logger.info(f"Saved analysis run {run_id} for {ticker or company}.")
        return run_id
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to save analysis history: {e}")
        return ""


def get_analysis_history(ticker: str, limit: int = 10) -> list[dict]:
    """Lightweight summaries of past runs for a ticker, newest first."""
    if not _HISTORY_DIR.exists():
        return []
    files = sorted(_HISTORY_DIR.glob(f"{_safe(ticker)}_*.json"), reverse=True)[:limit]
    out: list[dict] = []
    for f in files:
        try:
            out.append(_summary(json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Skipping unreadable history file {f.name}: {e}")
    return out


def get_analysis(run_id: str) -> dict | None:
    """Full stored record for a run_id, or None if not found."""
    if not _HISTORY_DIR.exists():
        return None
    matches = list(_HISTORY_DIR.glob(f"*_{run_id}.json"))
    if not matches:
        return None
    try:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read history record {run_id}: {e}")
        return None


def list_tickers() -> list[dict]:
    """Distinct tickers that have stored runs, with run counts (for the sidebar)."""
    if not _HISTORY_DIR.exists():
        return []
    counts: dict[str, int] = {}
    for f in _HISTORY_DIR.glob("*.json"):
        tk = f.name.rsplit("_", 3)[0]  # strip _YYYYmmdd_HHMMSS_mmm.json
        counts[tk] = counts.get(tk, 0) + 1
    return [{"ticker": t, "runs": n} for t, n in sorted(counts.items())]
