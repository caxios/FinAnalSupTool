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
    trigger_source: str = "manual",
    evolution_summary: str | None = None,
    trigger_id: int | None = None,
) -> dict:
    """
    Adopt a synthesized candidate, or write a custom rule from scratch.

    ``win_rate``/``payoff_ratio``/``expectancy`` are the EMPIRICAL figures
    behind an adopted candidate (from ``journal_analysis.synthesize_rules``) —
    left null for a hand-written custom rule with no backing statistics.

    ``trigger_source`` labels the FIRST ``rule_evolution_history`` entry
    (``change_type='created'``) this always writes — pass ``'edge_synthesis'``
    when adopting a synthesized candidate so the Evolution Timeline reads
    "v1: created from N round trips" rather than a generic "created manually".
    ``evolution_summary``/``trigger_id`` override that entry's text and link it
    to the review/proposal that produced this rule (see
    ``rule_evolution.apply_proposal``'s "new_rule" path).
    """
    rule_type = (rule_type or "").strip().lower()
    if rule_type not in RULE_TYPES:
        raise RuleError(f"rule_type must be one of {RULE_TYPES} (got {rule_type!r}).")
    title = (title or "").strip()
    if not title:
        raise RuleError("title must not be empty.")
    if trigger_source not in TRIGGER_SOURCES:
        raise RuleError(f"trigger_source must be one of {TRIGGER_SOURCES}.")

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

    summary = evolution_summary or (
        f"Created from an empirically-synthesized candidate "
        f"({win_rate:.0%} win rate, {expectancy:.0f} expectancy)."
        if win_rate is not None and expectancy is not None else
        "Created as a custom rule."
    )
    log_evolution(rule_id, 1, "created", trigger_source, summary, trigger_id=trigger_id)
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


def match_any_active_rule(proposed: dict) -> dict:
    """
    Every active rule (golden AND toxic) this trade matches, in one call —
    what :func:`rule_evolution.evaluate_trade_against_rules` needs to decide
    adherence vs. violation without calling :func:`match_active_rules` twice.
    """
    return {
        "golden": match_active_rules("golden", proposed),
        "toxic": match_active_rules("toxic", proposed),
    }


# =============================================================================
# Rule Evolution Engine — versioning, adherence tracking, audit history
# =============================================================================
# A rule is not a one-shot artifact: every review that touches a trade
# matching it is evidence for or against the rule as WRITTEN, and a review's
# qualitative feedback ("tighten the stop", "this needs a volume condition")
# is a candidate improvement. `record_adherence` is the cheap, deterministic
# half (safe to call on every review); the LLM-driven proposal synthesis in
# `services.rule_evolution` is the expensive half, run on-demand.

CHANGE_TYPES = (
    "created", "condition_refined", "stats_updated",
    "risk_tightened", "user_edited", "deprecated",
)
TRIGGER_SOURCES = ("trade_review", "journal_review", "manual", "edge_synthesis")
PROPOSAL_TYPES = ("refine_existing", "new_rule", "tighten_risk", "deprecate")
PROPOSAL_STATUSES = ("pending", "applied", "dismissed")


def set_adherence_counts(rule_id: int, adherence_count: int, violation_count: int) -> dict:
    """
    SET (never increment) this rule's ``adherence_count``/``violation_count``
    and stamp ``last_evaluated_at``.

    These are a snapshot of "how many of the journal's CURRENT closed round
    trips match this rule", not an event counter — recomputing the whole
    journal on every review (see ``rule_evolution.sync_rule_adherence_counts``)
    and setting the total here is what keeps re-reviewing the same trades from
    inflating the count. "Adherence" means the trade matched a Golden Setup
    (the user followed their own working pattern); "violation" means it
    matched a Toxic Pattern (the user repeated a known-bad one) — the caller
    decides which, this function only stores the totals.
    """
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE trading_rules SET adherence_count = ?, violation_count = ?,"
            " last_evaluated_at = ? WHERE id = ?",
            (int(adherence_count), int(violation_count), db.utc_now_iso(), int(rule_id)),
        )
        if cur.rowcount == 0:
            raise RuleError(f"No rule with id {rule_id}.")
    return get_rule(rule_id)


def apply_evolution_to_rule(
    rule_id: int, *,
    title: str | None = None,
    conditions: dict | None = None,
    description: str | None = None,
    win_rate: float | None = None,
    payoff_ratio: float | None = None,
    expectancy: float | None = None,
    notes: str | None = None,
) -> dict:
    """
    Promote a rule to its NEXT version with the given fields updated (any
    field left ``None`` keeps its current value) and return the updated row.

    Deliberately does NOT write the ``rule_evolution_history`` entry — the
    caller (``rule_evolution.apply_proposal``, or a manual edit) has the
    context (why, evidence, trigger) this function doesn't, and writes it
    itself via :func:`log_evolution` in the SAME logical operation.
    """
    rule = get_rule(rule_id)
    if rule is None:
        raise RuleError(f"No rule with id {rule_id}.")
    new_version = int(rule["version"]) + 1
    merged = {
        "title": title if title is not None else rule["title"],
        "conditions": conditions if conditions is not None else rule["conditions"],
        "description": description if description is not None else rule["description"],
        "win_rate": win_rate if win_rate is not None else rule["win_rate"],
        "payoff_ratio": payoff_ratio if payoff_ratio is not None else rule["payoff_ratio"],
        "expectancy": expectancy if expectancy is not None else rule["expectancy"],
        "notes": notes if notes is not None else rule["notes"],
    }
    with db.transaction() as conn:
        conn.execute(
            "UPDATE trading_rules SET title=?, conditions_json=?, description=?,"
            " win_rate=?, payoff_ratio=?, expectancy=?, notes=?, version=?"
            " WHERE id = ?",
            (merged["title"], json.dumps(merged["conditions"], default=str),
             merged["description"], merged["win_rate"], merged["payoff_ratio"],
             merged["expectancy"], merged["notes"], new_version, int(rule_id)),
        )
    return get_rule(rule_id)


def _history_row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["details"] = json.loads(d.pop("details_json") or "null")
    except (TypeError, ValueError):
        d["details"] = None
        d.pop("details_json", None)
    return d


def log_evolution(
    rule_id: int, version: int, change_type: str, trigger_source: str,
    summary: str, trigger_id: int | None = None, details: dict | None = None,
) -> dict:
    """Append one immutable audit-trail row — the Evolution Timeline's data source."""
    if change_type not in CHANGE_TYPES:
        raise RuleError(f"change_type must be one of {CHANGE_TYPES}.")
    if trigger_source not in TRIGGER_SOURCES:
        raise RuleError(f"trigger_source must be one of {TRIGGER_SOURCES}.")
    now = db.utc_now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO rule_evolution_history (rule_id, version, change_type,"
            " trigger_source, trigger_id, summary, details_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (int(rule_id), int(version), change_type, trigger_source, trigger_id,
             summary,
             json.dumps(details, ensure_ascii=False, default=str) if details else None,
             now),
        )
        entry_id = cur.lastrowid
    row = db.get_connection().execute(
        "SELECT * FROM rule_evolution_history WHERE id = ?", (entry_id,)
    ).fetchone()
    return _history_row_to_dict(row)


def get_rule_history(rule_id: int) -> list[dict]:
    """Chronological evolution audit trail for one rule, newest first."""
    rows = db.get_connection().execute(
        "SELECT * FROM rule_evolution_history WHERE rule_id = ?"
        " ORDER BY created_at DESC, id DESC",
        (int(rule_id),),
    ).fetchall()
    return [_history_row_to_dict(r) for r in rows]


# =============================================================================
# Evolution proposals — AI Coach suggestions awaiting user approval
# =============================================================================

def _proposal_row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["conditions"] = json.loads(d.pop("conditions_json") or "{}")
    except (TypeError, ValueError):
        d["conditions"] = {}
        d.pop("conditions_json", None)
    try:
        d["evidence_trade_ids"] = json.loads(d.pop("evidence_trade_ids") or "[]")
    except (TypeError, ValueError):
        d["evidence_trade_ids"] = []
    return d


def create_proposal(
    *, proposal_type: str, rule_type: str, title: str, conditions: dict,
    description: str, rationale: str,
    rule_id: int | None = None,
    evidence_review_id: int | None = None,
    evidence_trade_ids: list[int] | None = None,
) -> dict:
    """
    Record one AI Coach evolution proposal, ``status='pending'`` until the
    user applies or dismisses it — never auto-applied.
    """
    if proposal_type not in PROPOSAL_TYPES:
        raise RuleError(f"proposal_type must be one of {PROPOSAL_TYPES}.")
    if rule_type not in RULE_TYPES:
        raise RuleError(f"rule_type must be one of {RULE_TYPES}.")
    title = (title or "").strip()
    if not title:
        raise RuleError("title must not be empty.")
    now = db.utc_now_iso()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO rule_evolution_proposals (rule_id, proposal_type, rule_type,"
            " title, conditions_json, description, rationale, evidence_review_id,"
            " evidence_trade_ids, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
            (rule_id, proposal_type, rule_type, title,
             json.dumps(conditions or {}, default=str),
             (description or "").strip(), (rationale or "").strip(),
             evidence_review_id,
             json.dumps(evidence_trade_ids or []),
             now),
        )
        proposal_id = cur.lastrowid
    logger.info(
        f"[trading_rules] evolution proposal #{proposal_id} ({proposal_type}): {title}"
    )
    return get_proposal(proposal_id)


def get_proposal(proposal_id: int) -> dict | None:
    row = db.get_connection().execute(
        "SELECT * FROM rule_evolution_proposals WHERE id = ?", (int(proposal_id),)
    ).fetchone()
    return _proposal_row_to_dict(row) if row else None


def list_proposals(status: str | None = None) -> list[dict]:
    """Evolution proposals, newest first. Filter by status (default: all)."""
    sql = "SELECT * FROM rule_evolution_proposals"
    params: list = []
    if status:
        if status not in PROPOSAL_STATUSES:
            raise RuleError(f"status must be one of {PROPOSAL_STATUSES}.")
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC"
    return [_proposal_row_to_dict(r) for r in db.get_connection().execute(sql, params).fetchall()]


def _set_proposal_status(proposal_id: int, status: str) -> dict:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE rule_evolution_proposals SET status = ? WHERE id = ?",
            (status, int(proposal_id)),
        )
        if cur.rowcount == 0:
            raise RuleError(f"No proposal with id {proposal_id}.")
    return get_proposal(proposal_id)


def mark_proposal_applied(proposal_id: int) -> dict:
    return _set_proposal_status(proposal_id, "applied")


def dismiss_proposal(proposal_id: int) -> dict:
    proposal = get_proposal(proposal_id)
    if proposal is None:
        raise RuleError(f"No proposal with id {proposal_id}.")
    if proposal["status"] != "pending":
        raise RuleError(
            f"Proposal {proposal_id} is already '{proposal['status']}' — only a "
            f"pending proposal can be dismissed."
        )
    return _set_proposal_status(proposal_id, "dismissed")
