"""
providers.news_dedup
──────────────────────
SimHash-based near-duplicate detection for news articles — no ML dependency,
pure hashlib + bit manipulation. Paired with services.registry_db's
news_dedup table (services.data_fetcher.fetch_company_news calls both): a
64-bit fingerprint where near-identical text (the same story syndicated
across outlets, with only minor wording differences) produces hashes that
differ in just a few bits — unlike a cryptographic hash, which differs
completely for even a one-character change and so is useless for this.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _token_hash(token: str) -> int:
    """
    A stable 64-bit hash — deliberately NOT Python's builtin ``hash()``,
    which is randomized per-process (``PYTHONHASHSEED``) and would make a
    fingerprint meaningless to compare against one computed in an earlier
    process (e.g. after a server restart).
    """
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(text: str, *, hash_bits: int = 64) -> int:
    """
    Standard SimHash over word shingles: each token votes +1/-1 on every bit
    of its own hash (repeated tokens vote repeatedly, weighting frequent
    words more), and the final fingerprint takes the sign of each bit's
    running total.

    Near-identical texts (the same story with light rewording) end up with
    fingerprints differing in only a handful of bits — see
    ``services.registry_db.find_near_duplicate``'s ``max_hamming`` threshold,
    which is what actually decides "is this a duplicate."

    Returns 0 for empty/whitespace-only text (never raises).
    """
    tokens = _tokenize(text)
    if not tokens:
        return 0
    weights = [0] * hash_bits
    for token in tokens:
        h = _token_hash(token)
        for bit in range(hash_bits):
            weights[bit] += 1 if (h >> bit) & 1 else -1
    fingerprint = 0
    for bit in range(hash_bits):
        if weights[bit] > 0:
            fingerprint |= (1 << bit)
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two fingerprints."""
    return bin(a ^ b).count("1")
