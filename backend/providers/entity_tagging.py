"""
providers.entity_tagging
───────────────────────────
Lightweight "which other tickers does this article also mention" tagging —
keyword/substring matching against the SEC ticker map, not statistical NER.
No spaCy/transformers dependency: this codebase calls external APIs for
everything and never runs local ML inference (see the architecture plan's
tech-stack decisions in implementation_plan/new_db_architecture_plan/), and
"which companies get name-checked in a news snippet" doesn't need a real
entity-extraction model to answer usefully.
"""

from __future__ import annotations

import logging
import re

from providers import edgar_xbrl
from services import registry_db

logger = logging.getLogger(__name__)

# A normalized title shorter than this is too generic to match safely as a
# substring (e.g. a 3-letter stripped-suffix name could spuriously match
# inside an unrelated word) — mirrors edgar_xbrl.detect_cik's own >= 4 rule
# for the identical reason.
_MIN_TITLE_LEN = 4
_MAX_MENTIONED = 10

_NON_WORD_RE = re.compile(r"[^a-z0-9 ]")


async def tag_tickers(text: str, primary_ticker: str) -> tuple[str, list[str]]:
    """
    primary_ticker is always returned as-is (uppercased, whitespace-trimmed).

    mentioned_tickers: scan `text` for OTHER companies' normalized names from
    the SEC ticker map (providers.edgar_xbrl.fetch_ticker_to_cik_map — cached
    in-memory after the first call anywhere in the app, so this costs no
    extra network round-trip on repeat calls). Matched as whole-word
    substrings so a short name/ticker can't spuriously match inside an
    unrelated word. Capped at _MAX_MENTIONED so one article mentioning a
    long list of peers (e.g. an index roundup) doesn't produce an unbounded tag list.

    This is keyword/alias matching, not full entity extraction — good enough
    for "which other tickers does this article also touch."
    """
    primary = (primary_ticker or "").strip().upper()
    ticker_map, title_to_cik = await edgar_xbrl.fetch_ticker_to_cik_map()
    cik_to_ticker = {cik: t for t, cik in ticker_map.items()}

    norm_text = f" {_NON_WORD_RE.sub(' ', (text or '').lower())} "
    mentioned: set[str] = set()
    for title, cik in title_to_cik:
        if len(title) < _MIN_TITLE_LEN:
            continue
        if f" {title} " not in norm_text:
            continue
        t = cik_to_ticker.get(cik)
        if t and t != primary:
            mentioned.add(t)
        if len(mentioned) >= _MAX_MENTIONED:
            break
    return primary, sorted(mentioned)


async def seed_entity_aliases() -> int:
    """
    One-time (idempotent) backfill: populate registry_db.entity_aliases from
    the full SEC ticker map, so a direct alias -> ticker lookup
    (registry_db.resolve_alias) is a local SQLite query instead of a re-walk
    of ~10,000 entries. NOT called automatically on every server startup
    (a full SEC ticker-map fetch + ~10k-row bulk upsert on every boot is
    unnecessary — tag_tickers() above works from the in-memory ticker map
    regardless of whether this has ever run). Call it manually to (re-)seed
    the cache, e.g. once after deploying this feature or periodically to
    pick up newly-listed tickers.

    Returns the number of rows written.
    """
    ticker_map, title_to_cik = await edgar_xbrl.fetch_ticker_to_cik_map()
    cik_to_ticker = {cik: t for t, cik in ticker_map.items()}
    rows = [
        (title, cik_to_ticker[cik], cik, "sec_ticker_map")
        for title, cik in title_to_cik
        if cik in cik_to_ticker
    ]
    n = registry_db.learn_aliases_bulk(rows)
    logger.info(f"[entity_tagging] seeded {n} entity alias(es) from the SEC ticker map")
    return n
