"""
channel_store.py
────────────────
Persistence for the user's curated YouTube channel list.

Stored as a small JSON file next to the backend (`channels.json`) so the list
survives restarts and is shared across browsers. On first use, if no file
exists, the list is seeded from the optional `YOUTUBE_CHANNELS` env var
(comma-separated channel IDs) for backward compatibility.

Each channel is a dict: {"channel_id": str, "title": str, "handle": str | None}.
"""

from __future__ import annotations

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CHANNELS_FILE = Path(__file__).parent / "channels.json"

# In-memory cache of the channel list (loaded once, kept in sync on writes).
_cache: list[dict] | None = None


def _seed_from_env() -> list[dict]:
    raw = os.environ.get("YOUTUBE_CHANNELS", "").strip()
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    return [{"channel_id": cid, "title": cid, "handle": None} for cid in ids]


def _load() -> list[dict]:
    if _CHANNELS_FILE.exists():
        try:
            data = json.loads(_CHANNELS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.warning(f"Could not read channels.json: {e}")
    return _seed_from_env()


def _save(channels: list[dict]) -> None:
    try:
        _CHANNELS_FILE.write_text(
            json.dumps(channels, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Could not write channels.json: {e}")


def list_channels() -> list[dict]:
    """Return the current channel list (cached)."""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def channel_ids() -> list[str]:
    return [c["channel_id"] for c in list_channels()]


def add_channel(channel: dict) -> list[dict]:
    """Add a channel (or update its title if the id already exists)."""
    global _cache
    channels = list_channels()
    for c in channels:
        if c["channel_id"] == channel["channel_id"]:
            c.update(channel)  # refresh title/handle
            break
    else:
        channels.append(channel)
    _cache = channels
    _save(channels)
    return channels


def remove_channel(channel_id: str) -> list[dict]:
    global _cache
    channels = [c for c in list_channels() if c["channel_id"] != channel_id]
    _cache = channels
    _save(channels)
    return channels


def rename_channel(channel_id: str, title: str) -> list[dict]:
    global _cache
    channels = list_channels()
    for c in channels:
        if c["channel_id"] == channel_id:
            c["title"] = title
            break
    _cache = channels
    _save(channels)
    return channels
