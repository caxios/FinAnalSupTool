"""
services.trading_rules
────────────────────────
Persisted Golden Setup / Toxic Pattern rules — either adopted from
``journal_analysis``'s empirical rule-synthesis candidates, or written by the
user as a custom rule. Never auto-populated: a candidate stays a candidate
until the user explicitly adopts it (``POST /coach/rules``).

Read by the pre-trade coach review (``agents.coach_agent``) to check a
proposed trade against the user's own, empirically-derived patterns — a
warning grounded in "the last N times you did this, you lost money" rather
than a generic aphorism.

Match semantics
────────────────
A rule's ``conditions`` is a dict over the same three dimensions
``journal_analysis`` segments by: ``rationale_type``, ``strategy_type``,
``emotion_tag``. A proposed trade matches a rule when at least 70% of the
rule's SPECIFIED conditions (never "none"/"untagged" — those mean the
dimension wasn't part of what made the pattern) equal the proposed trade's
own values. With today's three dimensions that means: matching all specified
conditions matches at 100%, and 2 of 3 specified conditions is enough to
clear 70%; a rule specifying only one condition requires an exact match on it.
"""

from __future__ import annotations

import json
import logging

from services import db

logger = logging.getLogger(__name__)

RULE_TYPES = ("golden", "toxic", "custom")

# The dimensions a rule's conditions (and a proposed trade) are matched over.
_MATCH_DIMENSIONS = ("rationale_type", "strategy_type", "emotion_tag")
# Values on these dimensions that mean "not specified" — excluded from both
# the match denominator and the match count.
_UNSPECIFIED = {"none", "untagged", None, ""}

_MATCH_THRESHOLD = 0.7


class RuleError(Exception):
    """A rules-table operation failed for a reason the caller should surface."""


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["conditions"] = json.loads(d.pop("conditions_json") or "{}")
    except (TypeError, ValueError):
        d["conditions"] = {}
        d.pop("conditions_json", None)
    d["is_active"] = bool(d["is_active"])
    return d


def list_rules(
    rule_type: str | None = None, active_only: bool = False
) -> list[dict]:
    """All rules, newest first. Filter by type and/or active status."""
    sql = "SELECT * FROM trading_rules"
    clauses: list[str] = []
    params: list = []
    if rule_type:
        clauses.append("rule_type = ?")
        params.append(rule_type)
    if active_only:
        clauses.append("is_active = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    return [_row_to_dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def get_rule(rule_id: int) -> dict | None:
    row = db.get_connection().execute(
        "SELECT * FROM trading_rules WHERE id = ?", (int(rule_id),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def create_rule(
    rule_type: str,
    title: str,
    conditions: dict,
    description: str,
    win_rate: float | None = None,
    payoff_ratio: float | None = None,
    expectancy: float | None = None,
) -> dict:
    """
    Adopt a synthesized candidate, or write a custom rule from scratch.

    ``win_rate``/``payoff_ratio``/``expectancy`` are the EMPIRICAL figures
    behind an adopted candidate (from ``journal_analysis.synthesize_rules``) —
    left null for a hand-written custom rule with no backing statistics.
    """
    rule_type = (rule_type or "").strip().lower()
    if rule_type not in RULE_TYPES:
        raise RuleError(f"rule_type must be one of {RULE_TYPES} (got {rule_type!r}).")
    title = (title or "").strip()
    if not title:
        raise RuleError("title must not be empty.")

    now = db.utc_now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO trading_rules (rule_type, title, conditions_json,"
            " description, win_rate, payoff_ratio, expectancy, is_active,"
            " created_at) VALUES (?,?,?,?,?,?,?,1,?)",
            (rule_type, title, json.dumps(conditions or {}, default=str),
             (description or "").strip(), win_rate, payoff_ratio, expectancy, now),
        )
        rule_id = cur.lastrowid
    logger.info(f"[trading_rules] created {rule_type} rule #{rule_id}: {title}")
    return get_rule(rule_id)


def set_active(rule_id: int, is_active: bool) -> dict:
    """Toggle a rule on/off without losing its history — the UI's toggle switch."""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE trading_rules SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, int(rule_id)),
        )
        if cur.rowcount == 0:
            raise RuleError(f"No rule with id {rule_id}.")
    return get_rule(rule_id)


def delete_rule(rule_id: int) -> None:
    with db.transaction() as conn:
        cur = conn.execute("DELETE FROM trading_rules WHERE id = ?", (int(rule_id),))
        if cur.rowcount == 0:
            raise RuleError(f"No rule with id {rule_id}.")


def _match_score(proposed: dict, conditions: dict) -> float:
    keys = [
        k for k in _MATCH_DIMENSIONS
        if conditions.get(k) not in _UNSPECIFIED
    ]
    if not keys:
        return 0.0
    matched = sum(1 for k in keys if proposed.get(k) == conditions.get(k))
    return matched / len(keys)


def match_active_rules(rule_type: str, proposed: dict) -> list[dict]:
    """
    Active rules of ``rule_type`` that the proposed trade matches at or above
    the 70% threshold, each carrying its own ``match_score`` (0-1, highest first).

    ``proposed`` is ``{"rationale_type", "strategy_type", "emotion_tag"}`` —
    the same shape ``journal_analysis`` classifies a trade into.
    """
    matches = []
    for rule in list_rules(rule_type=rule_type, active_only=True):
        score = _match_score(proposed, rule["conditions"])
        if score >= _MATCH_THRESHOLD:
            matches.append({**rule, "match_score": round(score, 2)})
    return sorted(matches, key=lambda r: r["match_score"], reverse=True)
