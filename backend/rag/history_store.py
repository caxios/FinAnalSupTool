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


def get_analysis_history(ticker: str | None = None, limit: int = 50) -> list[dict]:
    """
    Lightweight summaries of past runs, newest first.

    With a ``ticker``, scoped to that company as before. Without one, scans
    every stored run across all companies — this is what lets the history
    sidebar and cold-boot ticker selection work before any filing has been
    fetched into the in-memory ``DocumentStore`` this session.
    """
    if not _HISTORY_DIR.exists():
        return []
    pattern = f"{_safe(ticker)}_*.json" if ticker else "*.json"
    # Filenames encode the run timestamp last, but the ticker prefix sorts
    # first — a lexicographic sort would group by ticker, not by recency.
    # Sort by each file's mtime instead so a global listing is newest-first
    # across tickers.
    files = sorted(_HISTORY_DIR.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files:
        if len(out) >= limit:
            break
        try:
            out.append(_summary(json.loads(f.read_text(encoding="utf-8"))))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Skipping unreadable history file {f.name}: {e}")
    return out


def get_latest_analysis(ticker: str) -> dict | None:
    """The most recent full stored record for a ticker, or None if it has none."""
    if not _HISTORY_DIR.exists():
        return None
    files = sorted(
        _HISTORY_DIR.glob(f"{_safe(ticker)}_*.json"),
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read latest history record for {ticker}: {e}")
        return None


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


def _run_id_time(run_id: str) -> datetime | None:
    """
    Parse the UTC instant encoded in a run_id (``YYYYmmdd_HHMMSS_mmm``).

    `save_analysis` builds the id from `datetime.now(timezone.utc)`, so the id
    itself is a sortable timestamp and no file has to be opened to date a run.
    """
    parts = run_id.split("_")
    if len(parts) < 2:
        return None
    try:
        dt = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    ms = 0
    if len(parts) > 2 and parts[2].isdigit():
        ms = int(parts[2])
    return dt.replace(microsecond=ms * 1000, tzinfo=timezone.utc)


def _window_end(record: dict) -> datetime | None:
    """
    The last date whose data an analysis could have seen.

    ``analysis_period`` is stored as ``"YYYY-MM-DD..YYYY-MM-DD"`` by
    ``pipeline.analyze_pipeline``. The end of that window — not when the run
    happened — is what bounds the information inside it.
    """
    period = (record.get("analysis_period") or "").strip()
    if ".." not in period:
        return None
    try:
        end = datetime.strptime(period.split("..", 1)[1].strip(), "%Y-%m-%d")
    except ValueError:
        return None
    # End of that day, so an analysis through 2026-06-30 covers a trade stamped
    # anywhere in 2026-06-30.
    return end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)


def analysis_as_of(ticker: str, when: datetime) -> dict | None:
    """
    The stored analysis whose data window ends latest at or **before** ``when``.

    This is what makes an honest retrospective review possible. Handing the coach
    today's fundamental and technical reports to judge a trade from three months
    ago is hindsight contamination — the current technical report already knows
    which way the price went.

    The test is **what the analysis could see, not when it was run.** A run
    executed today over a window ending 2026-06-30 contains nothing that
    postdates that quarter, so it is legitimate evidence for an August trade;
    a run executed in June over a window ending today is not. Judging by run
    timestamp would get both backwards. Where ``analysis_period`` is missing or
    malformed the run timestamp is used as a conservative fallback.

    ``None`` means every stored analysis for this ticker sees past ``when``.
    Callers must treat that as "no fundamental pillar" and say so — **not** as
    licence to fall back to the latest report.
    """
    if not _HISTORY_DIR.exists():
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    best_record, best_end, best_run = None, None, None
    for f in _HISTORY_DIR.glob(f"{_safe(ticker)}_*.json"):
        # Filenames are `{TICKER}_{YYYYmmdd}_{HHMMSS}_{mmm}.json`; the ticker may
        # itself contain dots or dashes, so split from the right like list_tickers.
        parts = f.stem.rsplit("_", 3)
        if len(parts) < 4:
            continue
        run_time = _run_id_time("_".join(parts[1:]))

        try:
            record = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Skipping unreadable history file {f.name}: {e}")
            continue

        covers_until = _window_end(record) or run_time
        if covers_until is None or covers_until > when:
            continue

        # Latest window wins; between two runs over the same window, the newer
        # run is the better-informed one.
        if (best_end is None
                or covers_until > best_end
                or (covers_until == best_end and run_time and best_run
                    and run_time > best_run)):
            best_record, best_end, best_run = record, covers_until, run_time

    return best_record
