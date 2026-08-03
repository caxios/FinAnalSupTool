"""
channel_store.py
────────────────
Persistence for the user's curated YouTube channel lists.

Two independent lists are kept, one per scope:
  - "company" — channels that cover specific companies (used by View 2)
  - "macro"   — channels that cover overall market / macro news (used by View 3)

Stored as a small JSON file next to the backend (`channels.json`) so the lists
survive restarts and are shared across browsers:

    {"company": [ {channel}, … ], "macro": [ {channel}, … ]}

Each channel is a dict: {"channel_id": str, "title": str, "handle": str | None}.
On first use, if no file exists, the macro list is seeded from the optional
`YOUTUBE_CHANNELS` env var (comma-separated channel IDs) for compatibility.
"""

from __future__ import annotations

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CHANNELS_FILE = Path(__file__).parent / "channels.json"

SCOPES = ("company", "macro")

# In-memory cache: {scope: [channel, …]}. Loaded once, kept in sync on writes.
_cache: dict[str, list[dict]] | None = None


def _empty() -> dict[str, list[dict]]:
    return {s: [] for s in SCOPES}


def _seed_from_env() -> dict[str, list[dict]]:
    data = _empty()
    raw = os.environ.get("YOUTUBE_CHANNELS", "").strip()
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    data["macro"] = [{"channel_id": cid, "title": cid, "handle": None} for cid in ids]
    return data


def _normalize(raw) -> dict[str, list[dict]]:
    """Coerce loaded JSON into the {scope: list} shape (handles legacy list)."""
    data = _empty()
    if isinstance(raw, dict):
        for s in SCOPES:
            val = raw.get(s)
            if isinstance(val, list):
                data[s] = val
    elif isinstance(raw, list):
        # Legacy single shared list → treat as macro channels.
        data["macro"] = raw
    return data


def _load() -> dict[str, list[dict]]:
    if _CHANNELS_FILE.exists():
        try:
            return _normalize(json.loads(_CHANNELS_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"Could not read channels.json: {e}")
    return _seed_from_env()


def _save(data: dict[str, list[dict]]) -> None:
    try:
        _CHANNELS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Could not write channels.json: {e}")


def _all() -> dict[str, list[dict]]:
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def _scope(scope: str) -> str:
    return scope if scope in SCOPES else "company"


def list_channels(scope: str) -> list[dict]:
    """Return the channel list for a scope ('company' or 'macro')."""
    return _all()[_scope(scope)]


def channel_ids(scope: str) -> list[str]:
    return [c["channel_id"] for c in list_channels(scope)]


def add_channel(scope: str, channel: dict) -> list[dict]:
    """Add a channel to a scope (or refresh its title if the id already exists)."""
    scope = _scope(scope)
    channels = _all()[scope]
    for c in channels:
        if c["channel_id"] == channel["channel_id"]:
            c.update(channel)
            break
    else:
        channels.append(channel)
    _save(_all())
    return channels


def remove_channel(scope: str, channel_id: str) -> list[dict]:
    scope = _scope(scope)
    data = _all()
    data[scope] = [c for c in data[scope] if c["channel_id"] != channel_id]
    _save(data)
    return data[scope]


def rename_channel(scope: str, channel_id: str, title: str) -> list[dict]:
    scope = _scope(scope)
    channels = _all()[scope]
    for c in channels:
        if c["channel_id"] == channel_id:
            c["title"] = title
            break
    _save(_all())
    return channels
